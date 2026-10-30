"""
Tests for reading OSPF from SR Linux. They replay JSON-RPC responses recorded from
the WAN lab (lab/wan/, SR Linux 25.10.5): three sites joined by links of 25G, 10G,
and 1G, with each datacenter in an area of its own.
"""

from __future__ import annotations

import json

import pytest

from netgpt.inventory import Credentials, Device
from netgpt.platforms.srlinux import SRLinuxPlatform, strip_module_prefixes

from fakes import FIXTURES, replay

OSPF = "/network-instance[name=*]/protocols/ospf"
ROUTES = "/network-instance[name=default]/route-table"


@pytest.fixture(autouse=True)
def lab_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NETGPT_TEST_PASSWORD", "not-used-by-the-mock")


def wan(name: str) -> SRLinuxPlatform:
    """A WAN lab device, answering from its recorded responses."""
    transport, _ = replay({OSPF: f"wan/{name}/ospf.json", ROUTES: f"wan/{name}/route_table.json"})
    device = Device(name=name, host="172.20.21.11", platform="nokia_srlinux", credentials="lab")
    credentials = Credentials(username="admin", password_env="NETGPT_TEST_PASSWORD")
    return SRLinuxPlatform(device, credentials, transport=transport)


@pytest.mark.anyio
async def test_each_ospf_interface_says_its_area_cost_and_whether_it_is_passive() -> None:
    async with wan("hq-core") as platform:
        interfaces = {interface.interface: interface for interface in await platform.ospf_interfaces()}
    assert {name: (interface.area, interface.cost, interface.passive) for name, interface in interfaces.items()} == {
        "ethernet-1/1.0": ("0.0.0.0", 4, False),  # 25G to Ops
        "ethernet-1/20.0": ("0.0.0.0", 10, False),  # 10G to the branch
        "ethernet-1/3.0": ("0.0.0.1", 4, False),  # HQ's datacenter, an area of its own
        "ethernet-1/10.0": ("0.0.0.0", 4, True),  # the campus LAN: advertised, but no hellos
        "system0.0": ("0.0.0.0", 0, True),
    }
    assert {interface.router_id for interface in interfaces.values()} == {"10.0.0.1"}
    wan_link = interfaces["ethernet-1/1.0"]
    assert (wan_link.network_type, wan_link.state, wan_link.address) == ("point-to-point", "point-to-point", "10.1.0.0")
    assert (wan_link.hello_interval, wan_link.dead_interval, wan_link.mtu, wan_link.neighbors) == (10, 40, 1500, 1)
    # A point-to-point link elects no designated router; SR Linux says 0.0.0.0.
    assert wan_link.designated_router is None
    assert interfaces["ethernet-1/10.0"].designated_router == "10.0.0.1"


@pytest.mark.anyio
async def test_each_ospf_neighbor_is_named_by_its_router_id_with_the_adjacency_s_state() -> None:
    async with wan("hq-core") as platform:
        neighbors = await platform.ospf_neighbors()
    found = {(neighbor.interface, neighbor.area, neighbor.router_id, neighbor.address, neighbor.state) for neighbor in neighbors}
    assert found == {
        ("ethernet-1/1.0", "0.0.0.0", "10.0.0.2", "10.1.0.1", "full"),
        ("ethernet-1/20.0", "0.0.0.0", "10.0.0.3", "10.1.0.3", "full"),
        ("ethernet-1/3.0", "0.0.0.1", "10.0.0.11", "10.10.0.1", "full"),
    }
    # SR Linux counts an adjacency's up-time in hundredths of a second.
    recorded = strip_module_prefixes(json.loads((FIXTURES / "wan/hq-core/ospf.json").read_text()))
    hundredths = {
        neighbor["router-id"]: neighbor["up-time"]
        for instance in recorded["result"][0]["network-instance"]
        for ospf in instance["protocols"]["ospf"]["instance"]
        for area in ospf["area"]
        for interface in area["interface"]
        for neighbor in interface.get("neighbor", [])
    }
    assert {neighbor.router_id: neighbor.uptime_seconds for neighbor in neighbors} == {
        router: value // 100 for router, value in hundredths.items()
    }


@pytest.mark.anyio
async def test_a_device_that_runs_no_ospf_has_no_ospf_records() -> None:
    async with wan("hq-sw1") as platform:
        assert await platform.ospf_interfaces() == []
        assert await platform.ospf_neighbors() == []


@pytest.mark.anyio
async def test_the_branch_reaches_ops_through_hq_rather_than_over_its_own_1g_link() -> None:
    async with wan("branch") as platform:
        routes = {route.prefix: route for route in await platform.routes("default")}
    to_ops = routes["10.20.100.0/24"]
    assert (to_ops.protocol, to_ops.metric) == ("ospfv2", 22)  # 10 to HQ, 4 to Ops, 4 to its datacenter, 4 onto the LAN
    assert [(hop.address, hop.interface) for hop in to_ops.next_hops] == [("10.1.0.2", "ethernet-1/1.0")]
