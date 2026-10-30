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
from netgpt.primitives import DeviceError, DeviceInfo, Interface, OspfInterface, OspfNeighbor
from netgpt.topology import build_ospf

from fakes import FIXTURES, recorded_wan, replay

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


# The OSPF view.

ROUTER_IDS = {"r1": "10.0.0.1", "r2": "10.0.0.2"}
ROUTERS = [DeviceInfo(name=name, platform="nokia_srlinux", host=name) for name in ROUTER_IDS]


def ospf_interface(device: str, address: str, **settings: object) -> OspfInterface:
    """An OSPF interface on ethernet-1/1.0, point-to-point in area 0 unless settings say otherwise."""
    fields = dict(area="0.0.0.0", network_type="point-to-point", cost=10, hello_interval=10, dead_interval=40, mtu=1500)
    fields.update(settings)
    return OspfInterface(device=device, network_instance="default", router_id=ROUTER_IDS[device], interface="ethernet-1/1.0",
                         address=address, **fields)


def routed(device: str, prefix: str) -> Interface:
    return Interface(device=device, name="ethernet-1/1.0", network_instance="default", mode="routed", addresses=[prefix])


def hears(device: str, other: str, address: str, state: str) -> OspfNeighbor:
    return OspfNeighbor(device=device, network_instance="default", area="0.0.0.0", interface="ethernet-1/1.0",
                        router_id=ROUTER_IDS[other], address=address, state=state)


LINK = [routed("r1", "10.1.0.0/31"), routed("r2", "10.1.0.1/31")]


@pytest.mark.anyio
async def test_the_ospf_view_draws_the_routers_their_adjacencies_and_the_lans_they_advertise() -> None:
    graph = (await recorded_wan()).ospf()
    routers = {node.id: node.areas for node in graph.nodes if node.kind == "device"}
    assert routers == {
        "hq-core": ["0.0.0.0", "0.0.0.1"], "hq-dc": ["0.0.0.1"], "ops-core": ["0.0.0.0", "0.0.0.2"],
        "ops-dc": ["0.0.0.2"], "branch": ["0.0.0.0"],
    }  # The switch runs no OSPF, so it isn't here; the cores are in two areas each.
    adjacencies = {link.subnet: link for link in graph.links if link.subnet}
    assert len(adjacencies) == 5
    assert all((link.a.state, link.b.state, link.problems) == ("full", "full", []) for link in adjacencies.values())
    slow = adjacencies["10.1.0.4/31"]  # Ops to the branch, 1G
    assert {(end.node, end.cost) for end in (slow.a, slow.b)} == {("branch", 100), ("ops-core", 100)}
    lans = {node.id: node.areas for node in graph.nodes if node.kind == "subnet"}
    assert lans == {
        "10.10.10.0/24": ["0.0.0.0"], "10.10.100.0/24": ["0.0.0.1"], "10.20.10.0/24": ["0.0.0.0"],
        "10.20.100.0/24": ["0.0.0.2"], "10.30.10.0/24": ["0.0.0.0"],
    }


def test_two_routers_in_different_areas_are_drawn_with_the_areas_they_disagree_on() -> None:
    ospf = [ospf_interface("r1", "10.1.0.0"), ospf_interface("r2", "10.1.0.1", area="0.0.0.1")]
    graph = build_ospf(ROUTERS, LINK, ospf, [], [])
    [link] = graph.links
    assert (link.subnet, link.a.state, link.b.state, link.one_sided) == ("10.1.0.0/31", None, None, False)
    assert link.problems == [
        "The areas differ: 0.0.0.0 at r1 ethernet-1/1.0, 0.0.0.1 at r2 ethernet-1/1.0.",
        "r1 doesn't hear r2 on ethernet-1/1.0.",
        "r2 doesn't hear r1 on ethernet-1/1.0.",
    ]


def test_an_adjacency_stuck_in_exstart_is_drawn_with_the_mtus_that_differ() -> None:
    ospf = [ospf_interface("r1", "10.1.0.0"), ospf_interface("r2", "10.1.0.1", mtu=9000)]
    stuck = [hears("r1", "r2", "10.1.0.1", "exstart"), hears("r2", "r1", "10.1.0.0", "exstart")]
    graph = build_ospf(ROUTERS, LINK, ospf, stuck, [])
    [link] = graph.links
    assert link.problems == [
        "The MTUs differ: 1500 at r1 ethernet-1/1.0, 9000 at r2 ethernet-1/1.0.",
        "r1 sees its adjacency with r2 at exstart, not full.",
        "r2 sees its adjacency with r1 at exstart, not full.",
    ]


def test_a_neighbor_heard_by_one_end_only_is_a_one_sided_link() -> None:
    ospf = [ospf_interface("r1", "10.1.0.0"), ospf_interface("r2", "10.1.0.1", passive=True)]
    graph = build_ospf(ROUTERS, LINK, ospf, [hears("r1", "r2", "10.1.0.1", "init")], [])
    [link] = graph.links
    assert link.one_sided
    assert link.problems == [
        "r2 ethernet-1/1.0 is passive, so it sends no hellos.",
        "r1 sees its adjacency with r2 at init, not full.",
        "r2 doesn't hear r1 on ethernet-1/1.0.",
    ]


def test_a_router_on_the_link_that_runs_no_ospf_there_is_named() -> None:
    graph = build_ospf(ROUTERS, LINK, [ospf_interface("r1", "10.1.0.0")], [], [])
    [link] = graph.links
    assert {link.a.node, link.b.node} == {"r1", "r2"}
    assert link.problems == [
        "r2 ethernet-1/1.0 is on 10.1.0.0/31 but doesn't run OSPF there, so r1 ethernet-1/1.0 hears no neighbor."
    ]


def test_a_router_ospf_could_not_be_read_from_is_drawn_with_why() -> None:
    graph = build_ospf(ROUTERS, LINK, [ospf_interface("r1", "10.1.0.0")], [], [DeviceError(device="r2", error="r2 timed out")])
    assert {node.id: node.error for node in graph.nodes} == {"r1": None, "r2": "r2 timed out"}


def test_a_neighbor_outside_the_inventory_is_drawn_by_its_router_id() -> None:
    outsider = OspfNeighbor(device="r1", network_instance="default", area="0.0.0.0", interface="ethernet-1/2.0",
                            router_id="192.0.2.9", address="10.9.0.1", state="full")
    graph = build_ospf(ROUTERS, [], [ospf_interface("r1", "10.1.0.0")], [outsider], [])
    assert [(node.id, node.kind) for node in graph.nodes] == [("r1", "device"), ("192.0.2.9", "neighbor")]
    [link] = graph.links
    assert {link.a.node, link.b.node} == {"r1", "192.0.2.9"} and "full" in (link.a.state, link.b.state)
