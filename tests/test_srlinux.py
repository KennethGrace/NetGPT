"""
Tests for the SR Linux driver. They replay JSON-RPC responses recorded from leaf1
in the containerlab lab (SR Linux 25.10.5) through httpx's mock transport, so the
parsers are checked against real device output rather than against assumptions.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from netgpt.inventory import Credentials, Device
from netgpt.platforms import PlatformError
from netgpt.platforms.srlinux import SRLinuxPlatform

FIXTURES = Path(__file__).parent / "fixtures" / "srlinux"


def replay(fixtures: dict[str, str]) -> tuple[httpx.MockTransport, list[str]]:
    """
    Answer each JSON-RPC request with the fixture whose key prefixes its CLI
    command or YANG path. Returns the transport and the list of requests made.
    """
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        command = json.loads(request.content)["params"]["commands"][0]
        key = command["path"] if isinstance(command, dict) else command
        requests.append(key)
        name = next(fixture for prefix, fixture in fixtures.items() if key.startswith(prefix))
        return httpx.Response(200, json=json.loads((FIXTURES / name).read_text()))

    return httpx.MockTransport(handler), requests


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def lab_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETGPT_TEST_PASSWORD", "not-used-by-the-mock")


def leaf1(fixtures: dict[str, str]) -> tuple[SRLinuxPlatform, list[str]]:
    transport, requests = replay(fixtures)
    device = Device(name="leaf1", host="172.20.20.21", platform="nokia_srlinux", credentials="lab")
    credentials = Credentials(username="admin", password_env="NETGPT_TEST_PASSWORD")
    return SRLinuxPlatform(device, credentials, transport=transport), requests


@pytest.mark.anyio
async def test_lldp_neighbors() -> None:
    platform, _ = leaf1({"/system/lldp": "lldp.json"})
    async with platform:
        neighbors = await platform.lldp_neighbors()
    fabric = [(n.interface, n.neighbor, n.neighbor_interface) for n in neighbors if n.interface != "mgmt0"]
    assert fabric == [("ethernet-1/49", "spine1", "ethernet-1/1")]


@pytest.mark.anyio
async def test_arp_table_shows_fabric_and_attached_host() -> None:
    platform, _ = leaf1({"/interface": "arp.json"})
    async with platform:
        entries = {(e.interface, e.ip_address, e.mac) for e in await platform.arp_table()}
    assert ("ethernet-1/49.0", "192.168.0.0", "1A:11:04:FF:00:01") in entries
    assert ("ethernet-1/1.0", "192.168.11.2", "AA:C1:AB:53:FD:C8") in entries


@pytest.mark.anyio
async def test_mac_table_without_bridging_is_empty() -> None:
    platform, _ = leaf1({"/network-instance": "mac_table_empty.json"})
    async with platform:
        assert await platform.mac_table() == []


@pytest.mark.anyio
async def test_routes_resolve_next_hops() -> None:
    platform, _ = leaf1({"/network-instance": "route_table.json"})
    async with platform:
        routes = {route.prefix: route for route in await platform.routes("default")}
    assert (routes["192.168.0.0/16"].protocol, routes["192.168.0.0/16"].next_hops) == ("static", ["192.168.0.0"])
    # Connected routes point at the interface, not at the router's own address.
    assert (routes["192.168.11.0/24"].protocol, routes["192.168.11.0/24"].next_hops) == ("local", ["ethernet-1/1.0"])
    assert routes["192.168.0.0/31"].next_hops == ["ethernet-1/49.0"]
    assert routes["192.168.0.0/16"].active is True and routes["192.168.0.0/16"].preference == 5


@pytest.mark.anyio
async def test_show_returns_cli_text() -> None:
    platform, _ = leaf1({"show version": "show_version.json"})
    async with platform:
        result = await platform.show("show version")
    assert "Software Version     : v25.10.5" in result.output


@pytest.mark.anyio
async def test_ping_across_the_fabric() -> None:
    platform, requests = leaf1({"ping": "ping_ok.json"})
    async with platform:
        result = await platform.ping("192.168.12.2", 3, "default")
    assert (result.source, result.sent, result.received) == ("leaf1", 3, 3)
    assert result.rtt_avg_ms is not None
    assert requests == ["ping 192.168.12.2 network-instance default -c 3 -W 1"]


@pytest.mark.anyio
@pytest.mark.parametrize("fixture", ["ping_unreachable.json", "ping_unreachable_error.json"])
async def test_unanswered_ping_is_a_result_whichever_way_srlinux_reports_it(fixture: str) -> None:
    platform, _ = leaf1({"ping": fixture})
    async with platform:
        result = await platform.ping("192.168.99.9", 2, "default")
    assert (result.sent, result.received, result.rtt_avg_ms) == (2, 0, None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "destination, count, network_instance",
    [
        ("192.168.12.2; tools system reboot", 3, "default"),
        ("spine1", 3, "default"),
        ("192.168.12.2", 3, "default -c 100000"),
        ("192.168.12.2", 50, "default"),
    ],
)
async def test_ping_validates_before_sending(destination: str, count: int, network_instance: str) -> None:
    platform, requests = leaf1({})
    async with platform:
        with pytest.raises(PlatformError):
            await platform.ping(destination, count, network_instance)
    assert requests == []
