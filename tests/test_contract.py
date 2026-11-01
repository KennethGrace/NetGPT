"""
Tests for the contract: the JSON Schema of every record NetGPT sends outside
itself. The repository's copy must be the one the code writes, and what NetGPT
actually sends must match it: each tool's answer through MCP, the page's traffic
and charts, views of the recorded labs, and the recorded Juniper switches' tables.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp import Client
from pydantic import BaseModel

from netgpt import contract
from netgpt.chat import Chat
from netgpt.engine import Engine
from netgpt.forwarding import trace_path
from netgpt.inventory import Credentials, Device, Inventory
from netgpt.platforms.junos import JunosPlatform
from netgpt.server import build_server
from netgpt.statistics import Poller
from netgpt.views import make_view
from netgpt.web import build_app

from fakes import FakePlatform, NetconfReplay, ScriptedOllama, recorded_lab, recorded_wan
from schemas import SCHEMA, check, published

TOKEN = "correct-horse-battery-staple"

# Each tool, called so that it answers on the fake platform, and the record it answers with: one record, a list of
# them (which MCP sends under "result"), or a collection (items and errors).
CALLS: dict[str, tuple[dict[str, Any], str, str]] = {
    "list_devices": ({}, "DeviceInfo", "list"),
    "show": ({"device": "leaf1", "command": "show version"}, "CommandOutput", "one"),
    "lldp_neighbors": ({}, "LldpNeighbor", "collection"),
    "interfaces": ({}, "Interface", "collection"),
    "mac_table": ({}, "MacEntry", "collection"),
    "arp_table": ({}, "ArpEntry", "collection"),
    "routes": ({}, "Route", "collection"),
    "bgp_sessions": ({}, "BgpSession", "collection"),
    "ospf_neighbors": ({}, "OspfNeighbor", "collection"),
    "ospf_interfaces": ({}, "OspfInterfaces", "one"),
    "interface_traffic": ({}, "InterfaceTraffic", "collection"),
    "topology": ({}, "Topology", "one"),
    "forwarding_path": ({"source": "198.51.100.11", "destination": "203.0.113.5"}, "ForwardingPath", "one"),
    "display": ({"title": "Out of leaf1", "path": {"source": "198.51.100.11", "destination": "203.0.113.5"}}, "View", "one"),
    "chart": ({"title": "leaf1", "ports": ["leaf1"]}, "Chart", "one"),
    "ping": ({"device": "leaf1", "destination": "192.0.2.1"}, "PingResult", "one"),
    "ping_sweep": ({"network": "192.0.2.0/29", "device": "leaf1"}, "PingResult", "list"),
}


def test_the_schema_in_the_repository_is_the_one_the_code_writes() -> None:
    assert SCHEMA.read_text() == contract.text(contract.schema()), (
        "The contract changed: run netgpt-schema --out schema/netgpt.schema.json, and read the diff."
    )


def test_the_schema_is_json_schema_and_says_which_netgpt_wrote_it() -> None:
    from netgpt import __version__

    document = published()
    Draft202012Validator.check_schema(document)
    assert (document["$schema"], document["version"]) == (contract.DIALECT, __version__)


def test_every_property_is_required_because_every_one_is_sent() -> None:
    for name, record in published()["$defs"].items():
        assert set(record.get("required", [])) == set(record.get("properties", {})), name


@pytest.mark.anyio
async def test_every_record_a_tool_answers_with_is_in_the_contract(inventory: Inventory) -> None:
    poller = Poller(inventory, platforms={"fake": FakePlatform}, interval=30)
    engine = Engine(inventory, platforms={"fake": FakePlatform}, statistics=poller)
    async with Client(build_server(engine, display=True)) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == set(CALLS)
    defs = set(published()["$defs"])
    for tool in tools:
        output = tool.output_schema or {}
        # A collection, or a list MCP wraps, is a shape of the records it holds, which are its $defs.
        wrapper = re.fullmatch(r"Collection\[\w+\]|\w+Output", output.get("title", ""))
        named = set(output.get("$defs", {})) | (set() if wrapper else {output.get("title")})
        assert named <= defs, f"{tool.name} answers with {sorted(named - defs)}, which the contract doesn't name"


@pytest.mark.anyio
async def test_what_every_tool_sends_matches_the_schema(inventory: Inventory, counter_reads: dict[str, int]) -> None:
    now = [1000.0]
    poller = Poller(inventory, platforms={"fake": FakePlatform}, interval=30, clock=lambda: now[0])
    for _ in range(3):
        await poller.poll()
        now[0] += 30
    engine = Engine(inventory, platforms={"fake": FakePlatform}, statistics=poller)
    async with Client(build_server(engine, display=True)) as client:
        for tool, (arguments, record, shape) in CALLS.items():
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, f"{tool}: {result.content[0].text}"
            check(record, result.structured_content, shape)


@pytest.mark.anyio
async def test_the_traffic_and_charts_the_page_reads_match_the_schema(inventory: Inventory, counter_reads: dict[str, int]) -> None:
    now = [1000.0]
    poller = Poller(inventory, platforms={"fake": FakePlatform}, interval=30, clock=lambda: now[0])
    for _ in range(3):
        await poller.poll()
        now[0] += 30
    engine = Engine(inventory, platforms={"fake": FakePlatform}, statistics=poller)
    async with Client(build_server(engine, display=True)) as client, ScriptedOllama().client() as http:
        chat = Chat(client, http, "scripted", {}, think=True)
        await chat.start()
        app = build_app(chat, TOKEN, engine)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://netgpt.test") as page:
            await page.get("/", params={"token": TOKEN})
            traffic = (await page.get("/api/traffic")).json()
            chart = (await page.post("/api/chart", json={"title": "x", "ports": ["leaf1"], "metric": "traffic", "minutes": 15})).json()
    assert traffic["ports"] and chart["lines"]
    check("Traffic", traffic)
    check("Chart", chart)


@pytest.mark.anyio
async def test_views_of_the_recorded_labs_match_the_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETGPT_TEST_PASSWORD", "not-used-by-the-mock")
    lab, wan = await recorded_lab(), await recorded_wan()
    layer2 = lab.topology(lambda device, interface: re.match(r"^mgmt\d+$", interface) is not None)
    path = trace_path(layer2, lab.routes, {}, "192.168.11.2", "192.168.12.2")
    assert path.outcome == "delivered"
    ospf = wan.ospf()
    for view in (make_view(layer2, "host1 to host2", [], [], {}, path=path), make_view(ospf, "OSPF", [], [], {})):
        check("View", view.model_dump(mode="json"))
    check("Topology", ospf.model_dump(mode="json"))
    tables = {
        "LldpNeighbor": lab.neighbors, "ArpEntry": lab.arp, "MacEntry": lab.macs, "Interface": lab.interfaces + wan.interfaces,
        "Route": [route for routes in (*lab.routes.values(), *wan.routes.values()) for route in routes],
        "OspfInterface": wan.ospf_interfaces, "OspfNeighbor": wan.ospf_neighbors,
    }
    for record, found in tables.items():
        assert found, record
        for item in found:
            check(record, item.model_dump(mode="json"))


@pytest.mark.anyio
async def test_the_recorded_juniper_switches_tables_match_the_schema() -> None:
    found: dict[str, list[BaseModel]] = defaultdict(list)
    for name in ("core", "south", "north"):
        device = Device(name=name, host="192.0.2.1", platform="juniper_junos", credentials="lab")
        platform = JunosPlatform(device, Credentials(username="netgpt", password_env="UNUSED"), session=NetconfReplay(name))
        found["LldpNeighbor"] += await platform.lldp_neighbors()
        found["Interface"] += await platform.interfaces()
        found["MacEntry"] += await platform.mac_table()
        found["ArpEntry"] += await platform.arp_table()
        found["Route"] += await platform.routes("default")
        found["BgpSession"] += await platform.bgp_sessions()
    for record, items in found.items():
        assert items, record
        for item in items:
            check(record, item.model_dump(mode="json"))
