"""
Fakes shared by the tests: a platform that answers from canned data, a transport
that replays recorded SR Linux responses, and an Ollama server that streams
scripted replies.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx

from netgpt.guard import NOKIA_SRLINUX
from netgpt.platforms import Platform, PlatformError
from netgpt.inventory import Credentials, Device
from netgpt.platforms.srlinux import SRLinuxPlatform
from netgpt.primitives import ArpEntry, DeviceInfo, LldpNeighbor, NextHop, Route

FIXTURES = Path(__file__).parent / "fixtures" / "srlinux"


def replay(fixtures: dict[str, str]) -> tuple[httpx.MockTransport, list[str]]:
    """
    Answer each SR Linux JSON-RPC request with the fixture whose key prefixes its
    CLI command or YANG path. Returns the transport and the list of requests made.
    """
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        command = json.loads(request.content)["params"]["commands"][0]
        key = command["path"] if isinstance(command, dict) else command
        requests.append(key)
        name = next(fixture for prefix, fixture in fixtures.items() if key.startswith(prefix))
        return httpx.Response(200, json=json.loads((FIXTURES / name).read_text()))

    return httpx.MockTransport(handler), requests


# The containerlab lab, and the responses recorded from each of its devices.
LAB = [
    DeviceInfo(name="spine1", platform="nokia_srlinux", host="172.20.20.11", role="spine"),
    DeviceInfo(name="leaf1", platform="nokia_srlinux", host="172.20.20.21", role="leaf"),
    DeviceInfo(name="leaf2", platform="nokia_srlinux", host="172.20.20.22", role="leaf"),
]
RECORDED = {
    "spine1": {"/system/lldp": "lldp_spine1.json", "/interface": "arp_spine1.json", "/network-instance": "route_table_spine1.json"},
    "leaf1": {"/system/lldp": "lldp.json", "/interface": "arp.json", "/network-instance": "route_table.json"},
    "leaf2": {"/system/lldp": "lldp_leaf2.json", "/interface": "arp_leaf2.json", "/network-instance": "route_table_leaf2.json"},
}


async def recorded_lab() -> tuple[list[LldpNeighbor], list[ArpEntry], dict[str, list[Route]]]:
    """
    Every lab device's LLDP, ARP, and routing tables, parsed by the real SR Linux
    driver from recorded responses. Needs NETGPT_TEST_PASSWORD set to anything.
    """
    credentials = Credentials(username="admin", password_env="NETGPT_TEST_PASSWORD")
    neighbors: list[LldpNeighbor] = []
    bindings: list[ArpEntry] = []
    routes: dict[str, list[Route]] = {}
    for info in LAB:
        transport, _ = replay(RECORDED[info.name])
        device = Device(name=info.name, host=info.host, platform=info.platform, credentials="lab")
        async with SRLinuxPlatform(device, credentials, transport=transport) as platform:
            neighbors += await platform.lldp_neighbors()
            bindings += await platform.arp_table()
            routes[info.name] = await platform.routes("default")
    return neighbors, bindings, routes


class FakePlatform(Platform):
    """
    Answers from canned data: each device is cabled to spine1 (outside the
    inventory) on ethernet-1/49, with one host behind ethernet-1/1 and a default
    route toward spine1. A device named 'broken' fails like an unreachable device.
    """

    policy = NOKIA_SRLINUX
    management_interfaces = re.compile(r"^mgmt\d+$")
    commands_run: list[str] = []

    async def _run_checked_cli(self, command: str) -> str:
        FakePlatform.commands_run.append(command)
        return f"{self.device.name}: ran {command}"

    async def lldp_neighbors(self) -> list[LldpNeighbor]:
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        return [LldpNeighbor(device=self.device.name, interface="ethernet-1/49", neighbor="spine1")]

    async def arp_table(self) -> list[ArpEntry]:
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        host = {"leaf1": "198.51.100.11", "leaf2": "198.51.100.12"}[self.device.name]
        return [ArpEntry(device=self.device.name, interface="ethernet-1/1.0", ip_address=host, mac="02:00:00:00:00:01")]

    async def routes(self, network_instance: str) -> list[Route]:
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        hosts = {"leaf1": "198.51.100.8/30", "leaf2": "198.51.100.12/30"}[self.device.name]
        table = [
            (hosts, "local", NextHop(interface="ethernet-1/1.0")),
            ("10.0.0.0/31", "local", NextHop(interface="ethernet-1/49.0")),
            ("0.0.0.0/0", "static", NextHop(address="10.0.0.1")),
        ]
        return [
            Route(device=self.device.name, network_instance=network_instance, prefix=prefix, protocol=protocol, next_hops=[hop])
            for prefix, protocol, hop in table
        ]


def calls(name: str, arguments: dict[str, Any], call_id: str = "call_1", content: str = "") -> dict[str, Any]:
    """A model reply that calls one tool."""
    tool_calls = [{"id": call_id, "function": {"name": name, "arguments": arguments}}]
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def says(text: str) -> dict[str, Any]:
    """A model reply that answers."""
    return {"role": "assistant", "content": text}


class ScriptedOllama:
    """
    Streams the scripted replies to /api/chat in order, and keeps every request.
    A reply given as a list is streamed as those chunks. With gated=True, each
    reply waits for gate to be set, so a test can hold an answer in flight.
    """

    def __init__(self, *replies: dict[str, Any] | list[dict[str, Any]], gated: bool = False) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        if not gated:
            self.gate.set()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.entered.set()
        await self.gate.wait()
        if not self.replies:
            return httpx.Response(404, json={"error": "model 'missing' not found"})
        reply = self.replies.pop(0)
        chunks = reply if isinstance(reply, list) else [reply]
        lines = [json.dumps({"message": chunk, "done": False}) for chunk in chunks]
        lines.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}))
        return httpx.Response(200, text="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://ollama.test", transport=httpx.MockTransport(self.handler))

    def messages(self, request: int) -> list[dict[str, Any]]:
        return self.requests[request]["messages"]
