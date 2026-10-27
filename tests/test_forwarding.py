"""
Tests for forwarding paths, walked through the routing tables recorded on the
lab's three SR Linux devices, plus a small two-spine fabric for equal-cost paths.
"""

from __future__ import annotations

import re

import pytest

from netgpt.forwarding import TraceError, trace_path
from netgpt.primitives import ArpEntry, DeviceInfo, LldpNeighbor, NextHop, Route
from netgpt.topology import Topology, build_topology

from fakes import LAB, recorded_lab

HOST1, HOST2 = "192.168.11.2", "192.168.12.2"
LabState = tuple[Topology, dict[str, list[Route]]]


def is_management(device: str, interface: str) -> bool:
    return re.match(r"^mgmt\d+$", interface) is not None


@pytest.fixture
async def lab(monkeypatch: pytest.MonkeyPatch) -> LabState:
    monkeypatch.setenv("NETGPT_TEST_PASSWORD", "not-used-by-the-mock")
    neighbors, bindings, routes = await recorded_lab()
    return build_topology(LAB, neighbors, bindings, [], is_management), routes


def matched(path, node: str) -> str:
    route = next(hop.route for hop in path.hops if hop.node == node)
    return f"{route.protocol} {route.prefix}"


@pytest.mark.anyio
async def test_host_to_host_follows_each_routing_table(lab: LabState) -> None:
    topology, routes = lab
    path = trace_path(topology, routes, {}, HOST1, HOST2)
    assert path.outcome == "delivered"
    assert path.nodes == [HOST1, "leaf1", "spine1", "leaf2", HOST2]
    assert [matched(path, device) for device in ("leaf1", "spine1", "leaf2")] == [
        "static 192.168.0.0/16", "static 192.168.12.0/24", "local 192.168.12.0/24",
    ]
    # leaf1's static next hop is only an address; its /31 says which interface reaches it.
    assert [hop.interfaces for hop in path.hops[1:4]] == [["ethernet-1/49.0"], ["ethernet-1/2.0"], ["ethernet-1/1.0"]]
    assert len(path.links) == 4 and path.trouble == []
    assert path.summary() == (
        f"{HOST1} → leaf1; leaf1 (static 192.168.0.0/16) → spine1; spine1 (static 192.168.12.0/24) → leaf2; "
        f"leaf2 (local 192.168.12.0/24) → {HOST2}. Delivered to {HOST2}."
    )


@pytest.mark.anyio
async def test_the_return_path_is_its_own_walk(lab: LabState) -> None:
    topology, routes = lab
    path = trace_path(topology, routes, {}, HOST2, HOST1)
    assert (path.outcome, path.nodes) == ("delivered", [HOST2, "leaf2", "spine1", "leaf1", HOST1])
    assert matched(path, "spine1") == "static 192.168.11.0/24"


@pytest.mark.anyio
async def test_a_device_address_is_delivered_to_the_device(lab: LabState) -> None:
    topology, routes = lab
    path = trace_path(topology, routes, {}, HOST1, "192.168.12.1")
    assert (path.outcome, path.nodes[-1], matched(path, "leaf2")) == ("delivered", "leaf2", "host 192.168.12.1/32")


@pytest.mark.anyio
async def test_where_the_packet_is_lost_is_marked(lab: LabState) -> None:
    topology, routes = lab
    no_route = trace_path(topology, routes, {}, HOST1, "10.9.9.9")
    assert (no_route.outcome, no_route.trouble) == ("no_route", ["leaf1"])
    assert no_route.detail == "leaf1 has no route to 10.9.9.9."

    nobody_home = trace_path(topology, routes, {}, HOST1, "192.168.12.99")
    assert (nobody_home.outcome, nobody_home.trouble) == ("unresolved", ["leaf2"])
    assert "nothing there answers ARP" in nobody_home.detail

    unreadable = trace_path(topology, routes, {"spine1": "spine1: connection refused"}, HOST1, HOST2)
    assert (unreadable.outcome, unreadable.trouble) == ("unreadable", ["spine1"])


@pytest.mark.anyio
async def test_a_forwarding_loop_is_caught(lab: LabState) -> None:
    topology, routes = lab
    # Point spine1's route for host2's network back at leaf1.
    looped = [
        route.model_copy(update={"next_hops": [NextHop(address="192.168.0.1")]}) if route.prefix == "192.168.12.0/24" else route
        for route in routes["spine1"]
    ]
    path = trace_path(topology, {**routes, "spine1": looped}, {}, HOST1, HOST2)
    assert (path.outcome, path.trouble) == ("loop", ["leaf1"])
    assert path.detail == "The packet comes back to leaf1: a forwarding loop."


@pytest.mark.anyio
async def test_sources_can_be_devices_or_any_address_on_a_connected_network(lab: LabState) -> None:
    topology, routes = lab
    from_device = trace_path(topology, routes, {}, "leaf1", HOST2)
    assert from_device.nodes[0] == "leaf1" and from_device.outcome == "delivered"
    unseen = trace_path(topology, routes, {}, "192.168.11.77", HOST2)
    assert unseen.nodes[0] == "leaf1" and unseen.detail.startswith("192.168.11.77 isn't on the map")
    with pytest.raises(TraceError, match="'host1' is not an address or a node"):
        trace_path(topology, routes, {}, "host1", HOST2)
    with pytest.raises(TraceError, match="Name one of leaf2's addresses"):
        trace_path(topology, routes, {}, HOST1, "leaf2")


def route(device: str, prefix: str, protocol: str, *hops: NextHop) -> Route:
    return Route(device=device, network_instance="default", prefix=prefix, protocol=protocol, next_hops=list(hops))


def test_equal_cost_paths_branch_and_meet_again() -> None:
    # leaf1 has two uplinks and a default route over both; leaf2 sits behind both spines.
    devices = [DeviceInfo(name=name, platform="nokia_srlinux", host=f"172.20.20.{index}") for index, name in enumerate(["leaf1", "spine1", "spine2", "leaf2"], 1)]
    cables = [("leaf1", "ethernet-1/49", "spine1", "ethernet-1/1"), ("leaf1", "ethernet-1/50", "spine2", "ethernet-1/1"),
              ("spine1", "ethernet-1/2", "leaf2", "ethernet-1/49"), ("spine2", "ethernet-1/2", "leaf2", "ethernet-1/50")]
    neighbors = [LldpNeighbor(device=a, interface=i, neighbor=b, neighbor_interface=j) for a, i, b, j in cables]
    neighbors += [LldpNeighbor(device=b, interface=j, neighbor=a, neighbor_interface=i) for a, i, b, j in cables]
    host = [ArpEntry(device="leaf2", interface="ethernet-1/1.0", ip_address=HOST2)]
    topology = build_topology(devices, neighbors, host, [], is_management)

    def connected(device: str, prefix: str, interface: str) -> Route:
        return route(device, prefix, "local", NextHop(interface=interface))

    routes = {
        "leaf1": [connected("leaf1", "10.0.1.0/31", "ethernet-1/49.0"), connected("leaf1", "10.0.2.0/31", "ethernet-1/50.0"),
                  route("leaf1", "0.0.0.0/0", "static", NextHop(address="10.0.1.1"), NextHop(address="10.0.2.1"))],
        "spine1": [connected("spine1", "10.0.3.0/31", "ethernet-1/2.0"), route("spine1", "192.168.12.0/24", "static", NextHop(address="10.0.3.1"))],
        "spine2": [connected("spine2", "10.0.4.0/31", "ethernet-1/2.0"), route("spine2", "192.168.12.0/24", "static", NextHop(address="10.0.4.1"))],
        "leaf2": [connected("leaf2", "192.168.12.0/24", "ethernet-1/1.0")],
    }
    # No ARP entries for the next hops: the cables alone say where each uplink goes.
    path = trace_path(topology, routes, {}, "leaf1", HOST2)
    assert path.outcome == "delivered"
    assert path.hops[0].next == ["spine1", "spine2"]
    assert path.hops[0].interfaces == ["ethernet-1/49.0", "ethernet-1/50.0"]
    assert set(path.nodes) == {"leaf1", "spine1", "spine2", "leaf2", HOST2} and len(path.links) == 5
