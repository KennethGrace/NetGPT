"""
Tests for the topology graph and the views drawn on it. The lab's graph is built
from LLDP and ARP tables recorded on all three SR Linux devices, parsed by the
real driver, so the merging is checked against what the devices actually say.
"""

from __future__ import annotations

import re

import pytest

from netgpt.inventory import Credentials, Device
from netgpt.platforms.srlinux import SRLinuxPlatform
from netgpt.primitives import ArpEntry, DeviceError, DeviceInfo, LldpNeighbor
from netgpt.topology import Topology, ViewError, build_topology, describe, make_view

from fakes import replay

DEVICES = [
    DeviceInfo(name="spine1", platform="nokia_srlinux", host="172.20.20.11", role="spine"),
    DeviceInfo(name="leaf1", platform="nokia_srlinux", host="172.20.20.21", role="leaf"),
    DeviceInfo(name="leaf2", platform="nokia_srlinux", host="172.20.20.22", role="leaf"),
]
RECORDED = {
    "spine1": ("lldp_spine1.json", "arp_spine1.json"),
    "leaf1": ("lldp.json", "arp.json"),
    "leaf2": ("lldp_leaf2.json", "arp_leaf2.json"),
}
HOST1, HOST2 = "192.168.11.2", "192.168.12.2"
LEAF1_UPLINK = "leaf1:ethernet-1/49--spine1:ethernet-1/1"
LEAF2_UPLINK = "leaf2:ethernet-1/49--spine1:ethernet-1/2"
HOST1_LINK = f"{HOST1}--leaf1:ethernet-1/1"
HOST2_LINK = f"{HOST2}--leaf2:ethernet-1/1"


def is_management(device: str, interface: str) -> bool:
    return re.match(r"^mgmt\d+$", interface) is not None


@pytest.fixture
async def tables(monkeypatch: pytest.MonkeyPatch) -> tuple[list[LldpNeighbor], list[ArpEntry]]:
    """Every lab device's LLDP and ARP records, parsed from recorded responses."""
    monkeypatch.setenv("NETGPT_TEST_PASSWORD", "not-used-by-the-mock")
    credentials = Credentials(username="admin", password_env="NETGPT_TEST_PASSWORD")
    neighbors: list[LldpNeighbor] = []
    bindings: list[ArpEntry] = []
    for info in DEVICES:
        lldp, arp = RECORDED[info.name]
        transport, _ = replay({"/system/lldp": lldp, "/interface": arp})
        device = Device(name=info.name, host=info.host, platform=info.platform, credentials="lab")
        async with SRLinuxPlatform(device, credentials, transport=transport) as platform:
            neighbors += await platform.lldp_neighbors()
            bindings += await platform.arp_table()
    return neighbors, bindings


@pytest.fixture
def lab(tables: tuple[list[LldpNeighbor], list[ArpEntry]]) -> Topology:
    return build_topology(DEVICES, *tables, [], is_management)


def node(topology: Topology, node_id: str):
    return next(candidate for candidate in topology.nodes if candidate.id == node_id)


@pytest.mark.anyio
async def test_the_lab_data_plane_is_two_leaves_under_a_spine_with_a_host_each(lab: Topology) -> None:
    data = lab.on_plane("data")
    assert [(n.id, n.kind) for n in data.nodes] == [
        ("spine1", "device"), ("leaf1", "device"), ("leaf2", "device"), (HOST1, "host"), (HOST2, "host"),
    ]
    assert sorted(link.id for link in data.links) == sorted([LEAF1_UPLINK, LEAF2_UPLINK, HOST1_LINK, HOST2_LINK])
    assert not any(link.one_sided for link in data.links)
    uplink = next(link for link in data.links if link.id == LEAF1_UPLINK)
    assert len(uplink.evidence) == 2  # both ends report the cable


@pytest.mark.anyio
async def test_addresses_heard_across_a_cable_belong_to_the_far_end(lab: Topology) -> None:
    # leaf1 and leaf2 each ARP for spine1's end of their /31, so neither becomes a host.
    assert node(lab, "spine1").addresses == ["172.20.20.11", "192.168.0.0", "192.168.0.2"]
    assert node(lab, "leaf1").addresses == ["172.20.20.21", "192.168.0.1"]


@pytest.mark.anyio
async def test_the_management_mesh_is_one_shared_segment(lab: Topology) -> None:
    # Every device hears both others on mgmt0: that is a bridge, not three cables.
    management = lab.on_plane("management")
    segment = node(management, "segment-1")
    assert (segment.kind, segment.label, segment.plane) == ("segment", "mgmt0 segment", "management")
    assert sorted(link.id for link in management.links) == [
        "172.20.20.1--segment-1", "leaf1:mgmt0--segment-1", "leaf2:mgmt0--segment-1", "segment-1--spine1:mgmt0",
    ]
    assert "segment-1" not in {n.id for n in lab.on_plane("data").nodes}


@pytest.mark.anyio
async def test_a_cable_only_one_end_reports_is_flagged(tables: tuple[list[LldpNeighbor], list[ArpEntry]]) -> None:
    neighbors, bindings = tables
    unreported = [n for n in neighbors if not (n.device == "spine1" and n.interface == "ethernet-1/1")]
    links = {link.id: link for link in build_topology(DEVICES, unreported, bindings, [], is_management).links}
    assert links[LEAF1_UPLINK].one_sided and not links[LEAF2_UPLINK].one_sided

    # A device that couldn't be asked can't contradict anything; it carries the error instead.
    failed = [DeviceError(device="spine1", error="spine1: connection refused")]
    topology = build_topology(DEVICES, unreported, bindings, failed, is_management)
    assert not any(link.one_sided for link in topology.links)
    assert node(topology, "spine1").error == "spine1: connection refused"


def test_neighbors_outside_the_inventory_become_nodes() -> None:
    neighbors = [LldpNeighbor(device="leaf1", interface="ethernet-1/2", neighbor="server9", neighbor_interface="eno1")]
    topology = build_topology(DEVICES[1:2], neighbors, [], [], is_management)
    server = node(topology, "server9")
    assert (server.kind, server.plane) == ("neighbor", "data")
    assert [link.id for link in topology.links] == ["leaf1:ethernet-1/2--server9:eno1"]


@pytest.mark.anyio
async def test_focus_keeps_the_rest_on_the_map_but_pushes_it_aside(lab: Topology) -> None:
    view = make_view(lab, "Where host2 is", [HOST2], [], {})
    assert view.emphasis == {
        HOST2: "focus", "leaf2": "near", "spine1": "context", "leaf1": "context", HOST1: "context",
        HOST2_LINK: "near", LEAF2_UPLINK: "context", LEAF1_UPLINK: "context", HOST1_LINK: "context",
    }
    assert len(view.nodes) == 5  # nothing is dropped


@pytest.mark.anyio
async def test_highlighting_the_nodes_of_a_path_lights_the_links_between_them(lab: Topology) -> None:
    view = make_view(lab, "host1 to host2", [], [HOST1, "leaf1", "spine1", "leaf2", HOST2], {"spine1": "the only way across"})
    assert set(view.highlight) == {HOST1, "leaf1", "spine1", "leaf2", HOST2, LEAF1_UPLINK, LEAF2_UPLINK, HOST1_LINK, HOST2_LINK}
    assert set(view.emphasis.values()) == {"focus"}
    assert view.notes == {"spine1": "the only way across"}


@pytest.mark.anyio
async def test_links_can_be_named_by_their_ends_or_a_port(lab: Topology) -> None:
    names = [
        "leaf1--spine1", "spine1 <-> leaf1", "leaf1 → spine1", "leaf1:ethernet-1/49", "leaf1:ethernet-1/49.0", LEAF1_UPLINK,
        # gemma4:e4b wrote link IDs with the ends swapped; either order names the same cable.
        "spine1:ethernet-1/1--leaf1:ethernet-1/49", "spine1:ethernet-1/1--leaf1",
    ]
    for name in names:
        assert make_view(lab, "", [], [name], {}).highlight == sorted(["leaf1", "spine1", LEAF1_UPLINK]), name
    # A node can be named by any of its addresses.
    assert make_view(lab, "", ["192.168.0.0"], [], {}).emphasis["spine1"] == "focus"


@pytest.mark.anyio
async def test_unknown_names_are_refused_with_the_names_that_exist(lab: Topology) -> None:
    with pytest.raises(ViewError) as refused:
        make_view(lab, "", ["host1", "leaf1--leaf2"], [], {})
    message = str(refused.value)
    assert "'host1'" in message and "no link between leaf1 and leaf2" in message
    assert f"spine1, leaf1, leaf2, {HOST1}, {HOST2}" in message
    with pytest.raises(ViewError, match="no link between leaf1 and spine1"):
        make_view(lab, "", [], ["leaf1:ethernet-1/1--spine1"], {})  # the wrong port on leaf1
    with pytest.raises(ViewError, match="segment-1"):
        make_view(lab, "", [], [], {"segment-1": "on the management plane only"})


@pytest.mark.anyio
async def test_the_model_reads_a_summary_not_the_graph(lab: Topology) -> None:
    summary = describe(make_view(lab, "Where host2 is", [HOST2], [HOST2_LINK], {}))
    assert summary.splitlines() == [
        'The user now sees "Where host2 is": the data plane, 5 nodes and 4 links.',
        f"In focus: leaf2, {HOST2}. 2 other nodes are dimmed and pushed aside.",
        f"Highlighted: leaf2, {HOST2} and 1 link.",
    ]
