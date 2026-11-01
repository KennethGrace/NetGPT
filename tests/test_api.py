"""
Tests for the engine's API: every route answers with a record the contract names,
only for the token, refuses in the tools' words, and shares devices' answers for a
poll interval while the MCP tools read live. The API runs in-process through httpx's
ASGI transport, on the fake platform.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from mcp import Client

from netgpt.api import TABLES, build_api
from netgpt.engine import Engine, SharedReads
from netgpt.inventory import Inventory
from netgpt.server import build_server
from netgpt.statistics import Poller

from fakes import FakePlatform
from schemas import check, published

TOKEN = "correct-horse-battery-staple"
BEARER = {"Authorization": f"Bearer {TOKEN}"}

# Each route, asked so that it answers on the fake platform, and the record it answers with.
ROUTES: dict[str, tuple[dict[str, Any], str, str]] = {
    "/devices": ({}, "DeviceInfo", "collection"),
    "/topology": ({"layer": "l3"}, "Topology", "one"),
    "/path": ({"source": "198.51.100.11", "destination": "203.0.113.5"}, "ForwardingPath", "one"),
    "/view": ({"focus": "198.51.100.11", "source": "198.51.100.11", "destination": "203.0.113.5"}, "View", "one"),
    "/traffic": ({}, "Traffic", "one"),
    "/chart": ({"port": "leaf1"}, "Chart", "one"),
}
# Each table, by its route, and the record its rows are.
TABLE_RECORDS = {
    "lldp_neighbors": "LldpNeighbor", "interfaces": "Interface", "mac_table": "MacEntry", "arp_table": "ArpEntry",
    "routes": "Route", "bgp_sessions": "BgpSession", "ospf_neighbors": "OspfNeighbor",
}


class Clock:
    """A clock for the shared reads that moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def engine(inventory: Inventory, counter_reads: dict[str, int]) -> Engine:
    """An engine on the fake platform whose poller has polled three times, 30 seconds apart."""
    now = [1000.0]
    poller = Poller(inventory, platforms={"fake": FakePlatform}, interval=30, clock=lambda: now[0])
    for _ in range(3):
        await poller.poll()
        now[0] += 30
    return Engine(inventory, platforms={"fake": FakePlatform}, statistics=poller)


@pytest.fixture
def clock() -> Clock:
    return Clock()


Open = Callable[..., httpx.AsyncClient]


@pytest.fixture
async def opener(engine: Engine, clock: Clock) -> AsyncIterator[Open]:
    """Opens clients of one API, with whatever headers and cookies a test gives them."""
    app = build_api(engine, TOKEN, SharedReads(30, clock))
    clients: list[httpx.AsyncClient] = []

    def open_client(**options: Any) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://netgpt.test", **options)
        clients.append(client)
        return client

    yield open_client
    for client in clients:
        await client.aclose()


@pytest.fixture
def api(opener: Open) -> httpx.AsyncClient:
    """A client holding the token, as a program would."""
    return opener(headers=BEARER)


@pytest.mark.anyio
async def test_every_route_answers_with_a_record_the_contract_names(api: httpx.AsyncClient, opener: Open, engine: Engine) -> None:
    served = {route.path for route in build_api(engine, TOKEN).routes}
    assert served == set(ROUTES) | {"/tables/{table}", "/schema"}
    for path, (params, record, shape) in ROUTES.items():
        response = await api.get(path, params=params)
        assert response.status_code == 200, f"{path}: {response.text}"
        check(record, response.json(), shape)
    assert set(TABLES) == set(TABLE_RECORDS) | {"ospf_interfaces"}
    for table, record in TABLE_RECORDS.items():
        response = await api.get(f"/tables/{table}")
        assert response.status_code == 200, f"{table}: {response.text}"
        check(record, response.json(), "collection")
    check("OspfInterfaces", (await api.get("/tables/ospf_interfaces")).json())
    assert (await api.get("/schema")).json() == published()


@pytest.mark.anyio
async def test_only_a_request_holding_the_token_is_answered(opener: Open) -> None:
    for refused in (opener(), opener(headers={"Authorization": "Bearer wrong"}), opener(cookies={"netgpt_token": "wrong"})):
        response = await refused.get("/devices")
        assert response.status_code == 401
        check("Refusal", response.json())
        assert "Bearer" in response.json()["error"]
    # A program sends the token as a bearer token; the page's browser sends its cookie.
    assert (await opener(headers=BEARER).get("/devices")).status_code == 200
    assert (await opener(cookies={"netgpt_token": TOKEN}).get("/devices")).status_code == 200


@pytest.mark.anyio
async def test_a_refusal_says_what_the_api_would_answer(api: httpx.AsyncClient) -> None:
    refusals = [
        ("/tables/nope", {}, 400, "The tables are lldp_neighbors, interfaces"),
        ("/topology", {"layer": "l7"}, 400, "layer: Input should be 'l2', 'l3' or 'ospf'."),
        ("/topology", {"layers": "l3"}, 400, "layers isn't a parameter here; the route takes plane, layer, network_instance."),
        ("/devices", {"device": "leaf1"}, 400, "the route takes no parameters"),
        ("/view", {"focus": "leaf9"}, 400, "'leaf9'"),
        ("/view", {"source": "198.51.100.11"}, 400, "A path needs both a source and a destination."),
        ("/path", {"source": "nowhere", "destination": "203.0.113.5"}, 400, "'nowhere'"),
        ("/path", {"source": "198.51.100.11"}, 400, "destination: Field required."),
        ("/chart", {"port": "leaf9:ethernet-1/1"}, 400, "leaf1:ethernet-1/1"),
        ("/nowhere", {}, 404, "The routes are devices, topology, path, view, tables/{table}, traffic, chart, schema."),
    ]
    for path, params, status, says in refusals:
        response = await api.get(path, params=params)
        assert (response.status_code, says in response.json()["error"]) == (status, True), (path, params, response.text)
        check("Refusal", response.json())
    posted = await api.post("/devices")
    assert posted.status_code == 405 and "GET" in posted.json()["error"]


@pytest.mark.anyio
async def test_a_device_s_answer_is_shared_until_it_is_a_poll_old(
    api: httpx.AsyncClient, clock: Clock, table_reads: list[tuple[str, str]]
) -> None:
    first = await api.get("/topology")
    read = len(table_reads)
    assert read and first.headers["age"] == "0"
    clock.now += 10
    again = await api.get("/topology")
    assert (len(table_reads), again.headers["age"], again.json()) == (read, "10", first.json())
    # A path reads the same tables, and only the routes are new.
    await api.get("/path", params={"source": "198.51.100.11", "destination": "203.0.113.5"})
    assert {table for _, table in table_reads[read:]} == {"routes"}
    clock.now += 21
    late = await api.get("/topology")
    assert len(table_reads) > read and late.headers["age"] == "0"


@pytest.mark.anyio
async def test_requests_that_arrive_together_wait_for_one_read(
    api: httpx.AsyncClient, table_reads: list[tuple[str, str]]
) -> None:
    await asyncio.gather(*(api.get("/topology") for _ in range(4)))
    assert table_reads.count(("leaf1", "lldp_neighbors")) == 1


@pytest.mark.anyio
async def test_a_failed_device_is_not_asked_again_on_every_request(
    api: httpx.AsyncClient, table_reads: list[tuple[str, str]]
) -> None:
    for _ in range(3):
        topology = (await api.get("/topology")).json()
    assert "broken" in {error["device"] for error in topology["errors"]}
    assert table_reads.count(("broken", "lldp_neighbors")) == 1


@pytest.mark.anyio
async def test_no_cache_reads_every_device_again(
    api: httpx.AsyncClient, clock: Clock, table_reads: list[tuple[str, str]]
) -> None:
    await api.get("/topology")
    clock.now += 10
    live = await api.get("/topology", headers={"Cache-Control": "no-cache"})
    assert (table_reads.count(("leaf1", "lldp_neighbors")), live.headers["age"]) == (2, "0")


@pytest.mark.anyio
async def test_the_mcp_tools_read_live_while_the_api_shares(
    api: httpx.AsyncClient, engine: Engine, table_reads: list[tuple[str, str]]
) -> None:
    await api.get("/topology")
    async with Client(build_server(engine)) as client:
        await client.call_tool("topology", {})
        await client.call_tool("topology", {})
    await api.get("/topology")
    # Once for the API, which shared it with its second request, and twice for the model.
    assert table_reads.count(("leaf1", "lldp_neighbors")) == 3


@pytest.mark.anyio
async def test_an_engine_that_doesn_t_poll_serves_no_traffic_or_charts(inventory: Inventory) -> None:
    app = build_api(Engine(inventory, platforms={"fake": FakePlatform}), TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://netgpt.test", headers=BEARER) as api:
        for path in ("/traffic", "/chart"):
            response = await api.get(path)
            assert response.status_code == 404
            assert response.json()["error"].endswith("The routes are devices, topology, path, view, tables/{table}, schema.")
        assert (await api.get("/devices")).status_code == 200
