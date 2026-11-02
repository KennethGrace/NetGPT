"""
Tests for the topology graph and the views drawn on it. The lab's graph is built
from tables recorded on all four SR Linux devices (a spine, two leaves, and a
switch behind leaf2), parsed by the real driver, so the merging is checked
against what the devices actually say.
"""

from __future__ import annotations

import re

import pytest

from netgpt.primitives import ArpEntry, DeviceError, DeviceInfo, Interface, LldpNeighbor, MacEntry
from netgpt.topology import Topology, TopologyNode, build_l3, build_topology
from netgpt.views import ViewError, describe, focused, make_view, named

from fakes import LAB, RecordedLab, SwitchedNetwork, recorded_lab

HOST1, HOST2, HOST3 = "192.168.11.2", "192.168.12.2", "192.168.12.3"
LEAF1_UPLINK = "leaf1:ethernet-1/49--spine1:ethernet-1/1"
LEAF2_UPLINK = "leaf2:ethernet-1/49--spine1:ethernet-1/2"
SWITCH_UPLINK = "leaf2:ethernet-1/1--switch1:ethernet-1/1"
HOST1_LINK = f"{HOST1}--leaf1:ethernet-1/1"
HOST2_LINK = f"{HOST2}--switch1:ethernet-1/2"
HOST3_LINK = f"{HOST3}--switch1:ethernet-1/3"


def is_management(device: str, interface: str) -> bool:
    return re.match(r"^mgmt\d+$", interface) is not None


@pytest.fixture
async def tables(monkeypatch: pytest.MonkeyPatch) -> RecordedLab:
    """Every lab device's tables, parsed from recorded responses."""
    monkeypatch.setenv("NETGPT_TEST_PASSWORD", "not-used-by-the-mock")
    return await recorded_lab()


@pytest.fixture
def lab(tables: RecordedLab) -> Topology:
    return tables.topology(is_management)


def node(topology: Topology, node_id: str):
    return next(candidate for candidate in topology.nodes if candidate.id == node_id)


@pytest.mark.anyio
async def test_the_lab_data_plane_is_a_spine_two_leaves_and_a_switch(lab: Topology) -> None:
    data = lab.on_plane("data")
    assert [(n.id, n.kind) for n in data.nodes] == [
        ("spine1", "device"), ("leaf1", "device"), ("leaf2", "device"), ("switch1", "device"),
        (HOST1, "host"), (HOST2, "host"), (HOST3, "host"),
    ]
    assert sorted(link.id for link in data.links) == sorted(
        [LEAF1_UPLINK, LEAF2_UPLINK, SWITCH_UPLINK, HOST1_LINK, HOST2_LINK, HOST3_LINK]
    )
    assert not any(link.one_sided for link in data.links)
    uplink = next(link for link in data.links if link.id == LEAF1_UPLINK)
    assert len(uplink.evidence) == 2  # both ends report the cable
    # leaf2 heard host3 on its cable to switch1, whose MAC table says which port host3 is behind.
    host3 = next(link for link in data.links if link.id == HOST3_LINK)
    assert host3.evidence == [
        "ARP on leaf2 ethernet-1/1.0: 192.168.12.3 is at AA:C1:AB:FE:3F:C0; "
        "MAC table on switch1 (lan): AA:C1:AB:FE:3F:C0 on ethernet-1/3.0."
    ]


@pytest.mark.anyio
async def test_a_devices_addresses_come_from_its_interfaces(lab: Topology) -> None:
    assert node(lab, "spine1").addresses == ["172.20.20.11", "192.168.0.0", "192.168.0.2"]
    assert node(lab, "leaf1").addresses == ["172.20.20.21", "192.168.11.1", "192.168.0.1"]
    assert node(lab, "switch1").addresses == ["172.20.20.41"]  # it bridges; its only address is management


@pytest.mark.anyio
async def test_without_interfaces_an_address_across_a_cable_belongs_to_the_far_end(tables: RecordedLab) -> None:
    # leaf1 and leaf2 each ARP for spine1's end of their /31, so neither becomes a host.
    topology = build_topology(LAB, tables.neighbors, tables.arp, [], is_management)
    assert node(topology, "spine1").addresses == ["172.20.20.11", "192.168.0.0", "192.168.0.2"]


@pytest.mark.anyio
async def test_the_management_mesh_is_one_shared_segment(lab: Topology) -> None:
    # Every device hears all the others on mgmt0: that is a bridge, not a mesh of cables.
    management = lab.on_plane("management")
    segment = node(management, "segment-1")
    assert (segment.kind, segment.label, segment.plane) == ("segment", "mgmt0 segment", "management")
    assert sorted(link.id for link in management.links) == [
        "172.20.20.1--segment-1", "leaf1:mgmt0--segment-1", "leaf2:mgmt0--segment-1", "segment-1--spine1:mgmt0",
        "segment-1--switch1:mgmt0",
    ]
    assert "segment-1" not in {n.id for n in lab.on_plane("data").nodes}


@pytest.mark.anyio
async def test_a_cable_only_one_end_reports_is_flagged(tables: RecordedLab) -> None:
    unreported = [n for n in tables.neighbors if not (n.device == "spine1" and n.interface == "ethernet-1/1")]
    links = {link.id: link for link in build_topology(LAB, unreported, tables.arp, [], is_management).links}
    assert links[LEAF1_UPLINK].one_sided and not links[LEAF2_UPLINK].one_sided

    # A device that couldn't be asked can't contradict anything; it carries the error instead.
    failed = [DeviceError(device="spine1", error="spine1: connection refused")]
    topology = build_topology(LAB, unreported, tables.arp, failed, is_management)
    assert not any(link.one_sided for link in topology.links)
    assert node(topology, "spine1").error == "spine1: connection refused"


def test_neighbors_outside_the_inventory_become_nodes() -> None:
    neighbors = [LldpNeighbor(device="leaf1", interface="ethernet-1/2", neighbor="server9", neighbor_interface="eno1")]
    topology = build_topology(LAB[1:2], neighbors, [], [], is_management)
    server = node(topology, "server9")
    assert (server.kind, server.plane) == ("neighbor", "data")
    assert [link.id for link in topology.links] == ["leaf1:ethernet-1/2--server9:eno1"]


@pytest.mark.anyio
async def test_focus_keeps_the_rest_on_the_map_but_pushes_it_aside(lab: Topology) -> None:
    view = make_view(lab, "Where host2 is", [HOST2], [], {})
    assert view.emphasis == {
        HOST2: "focus", "switch1": "near", "spine1": "context", "leaf1": "context", "leaf2": "context",
        HOST1: "context", HOST3: "context",
        HOST2_LINK: "near", SWITCH_UPLINK: "context", HOST3_LINK: "context", LEAF2_UPLINK: "context",
        LEAF1_UPLINK: "context", HOST1_LINK: "context",
    }
    assert len(view.nodes) == 7  # nothing is dropped


@pytest.mark.anyio
async def test_highlighting_the_nodes_of_a_path_lights_the_links_between_them(lab: Topology) -> None:
    path = [HOST1, "leaf1", "spine1", "leaf2", "switch1", HOST2]
    view = make_view(lab, "host1 to host2", [], path, {"spine1": "the only way across"})
    assert set(view.highlight) == {*path, LEAF1_UPLINK, LEAF2_UPLINK, SWITCH_UPLINK, HOST1_LINK, HOST2_LINK}
    assert view.emphasis[HOST3] == "near" and view.emphasis[HOST3_LINK] == "near"
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
    assert f"spine1, leaf1, leaf2, switch1, {HOST1}, {HOST2}, {HOST3}" in message
    with pytest.raises(ViewError, match="no link between leaf1 and spine1"):
        make_view(lab, "", [], ["leaf1:ethernet-1/1--spine1"], {})  # the wrong port on leaf1
    with pytest.raises(ViewError, match="segment-1"):
        make_view(lab, "", [], [], {"segment-1": "on the management plane only"})


@pytest.mark.anyio
async def test_the_model_reads_a_summary_not_the_graph(lab: Topology) -> None:
    summary = describe(make_view(lab, "Where host2 is", [HOST2], [HOST2_LINK], {}))
    assert summary.splitlines() == [
        'The user now sees "Where host2 is": the data plane at layer 2 (cables, switches, and where hosts are plugged in), '
        "7 nodes and 6 links.",
        f"In focus: switch1, {HOST2}. 3 other nodes are dimmed and pushed aside.",
        f"Highlighted: switch1, {HOST2} and 1 link.",
    ]


@pytest.mark.anyio
async def test_the_lab_at_layer_3_is_three_routers_and_two_lans(tables: RecordedLab, lab: Topology) -> None:
    l3 = build_l3(LAB, lab, tables.interfaces, is_management)
    assert [(n.id, n.kind) for n in l3.nodes] == [
        ("spine1", "device"), ("leaf1", "device"), ("leaf2", "device"),
        (HOST1, "host"), (HOST2, "host"), (HOST3, "host"),
        ("192.168.11.0/24", "subnet"), ("192.168.12.0/24", "subnet"),
    ]
    assert sorted((link.id, link.subnet) for link in l3.links if link.subnet) == [
        ("leaf1:ethernet-1/49.0--spine1:ethernet-1/1.0", "192.168.0.0/31"),
        ("leaf2:ethernet-1/49.0--spine1:ethernet-1/2.0", "192.168.0.2/31"),
    ]


# ---- A switch in the way: hosts on the port their MAC is behind, and the layer 3 view


def test_hosts_behind_a_switch_hang_off_the_port_their_mac_is_on() -> None:
    network = SwitchedNetwork()
    topology = network.topology(is_management)
    links = {link.id: link for link in topology.links}
    assert sorted(links) == [
        "10.1.0.2--sw1:ethernet-1/2", "10.1.0.3--sw1:ethernet-1/3",
        "r0:ethernet-1/1--r1:ethernet-1/49", "r1:ethernet-1/1--sw1:ethernet-1/1",
    ]
    # ARP on r1 heard the hosts on its cable to sw1; sw1's MAC table says which port each is behind.
    assert links["10.1.0.2--sw1:ethernet-1/2"].evidence == [
        "ARP on r1 ethernet-1/1.0: 10.1.0.2 is at AA:00:00:00:00:02; "
        "MAC table on sw1 (lan): AA:00:00:00:00:02 on ethernet-1/2.0."
    ]
    uplink = links["r1:ethernet-1/1--sw1:ethernet-1/1"]
    assert (uplink.a.bridges, uplink.b.bridges) == ([], ["lan"])  # r1 routes on its end; sw1 bridges
    # Routers' addresses come from their own interfaces, not from who ARPs for them.
    assert node(topology, "r1").addresses == ["172.20.20.20", "10.0.0.1", "10.1.0.1"]
    assert node(topology, "sw1").addresses == ["172.20.20.40"]


def test_a_mac_the_switch_has_forgotten_leaves_the_host_behind_the_switch() -> None:
    network = SwitchedNetwork()
    network.macs = [entry for entry in network.macs if entry.mac != network.MAC3]
    links = {link.id: link for link in network.topology(is_management).links}
    behind = links["10.1.0.3--sw1"]  # on sw1, but no port: an honest "somewhere behind sw1"
    assert behind.a.node == network.HOST3 and behind.b.bridges == ["lan"]
    assert "aged out" in behind.evidence[0]


def test_an_address_on_a_vlan_interface_is_found_through_the_bridge() -> None:
    # A layer 3 switch routes for VLAN 10 on irb0.10 and bridges it out of its ports.
    devices = [DeviceInfo(name="core", platform="nokia_srlinux", host="172.20.20.50", role="core")]
    interfaces = [
        Interface(device="core", name="irb0.10", network_instance="default", mode="routed", addresses=["10.2.0.1/24"]),
        Interface(device="core", name="ethernet-1/5.10", network_instance="vlan10", mode="bridged"),
    ]
    arp = [ArpEntry(device="core", interface="irb0.10", ip_address="10.2.0.5", mac="aa:00:00:00:00:05")]
    macs = [MacEntry(device="core", network_instance="vlan10", mac="AA:00:00:00:00:05", interface="ethernet-1/5.10")]
    topology = build_topology(devices, [], arp, [], is_management, macs, interfaces)
    assert [link.id for link in topology.links] == ["10.2.0.5--core:ethernet-1/5"]


def test_an_address_on_the_switchs_own_mac_is_the_switchs() -> None:
    # sw1 routes for its LAN on an IRB interface: its MAC table lists that MAC as its own.
    network = SwitchedNetwork()
    network.arp.append(ArpEntry(device="r1", interface="ethernet-1/1.0", ip_address="10.1.0.254", mac="1A:00:00:00:0F:FE"))
    network.macs.append(
        MacEntry(device="sw1", network_instance="lan", mac="1A:00:00:00:0F:FE", interface="irb0.0", entry_type="irb-interface")
    )
    topology = network.topology(is_management)
    assert "10.1.0.254" in node(topology, "sw1").addresses
    assert "10.1.0.254" not in {n.id for n in topology.nodes}


def port_channel_network() -> tuple[list[DeviceInfo], list[LldpNeighbor], list[Interface]]:
    """
    A core and an access switch joined by a port-channel of two cables. Each end's
    LLDP is heard on the members, and each sends its ports' short names: the core
    sends Eth1/1, and the access switch Gi1/0/51.
    """
    devices = [
        DeviceInfo(name="core", platform="cisco_nxos", host="172.20.20.60", role="core"),
        DeviceInfo(name="access", platform="cisco_ios", host="172.20.20.61", role="access"),
    ]
    neighbors = [
        LldpNeighbor(device="core", interface="Ethernet1/1", neighbor="access", neighbor_interface="Gi1/0/51"),
        LldpNeighbor(device="core", interface="Ethernet1/2", neighbor="access", neighbor_interface="Gi1/0/52"),
        LldpNeighbor(device="access", interface="GigabitEthernet1/0/51", neighbor="core", neighbor_interface="Eth1/1"),
        LldpNeighbor(device="access", interface="GigabitEthernet1/0/52", neighbor="core", neighbor_interface="Eth1/2"),
    ]
    interfaces = [
        Interface(device="core", name="Vlan10", network_instance="default", mode="routed", addresses=["10.3.0.1/24"]),
        Interface(device="core", name="Ethernet1/1", network_instance="default", mode="bridged", aggregate="port-channel10"),
        Interface(device="core", name="Ethernet1/2", network_instance="default", mode="bridged", aggregate="port-channel10"),
        Interface(device="core", name="port-channel10", network_instance="default", mode="bridged"),
        Interface(device="access", name="GigabitEthernet1/0/51", network_instance="default", mode="bridged", aggregate="Port-channel1"),
        Interface(device="access", name="GigabitEthernet1/0/52", network_instance="default", mode="bridged", aggregate="Port-channel1"),
        Interface(device="access", name="Port-channel1", network_instance="default", mode="bridged"),
        Interface(device="access", name="GigabitEthernet1/0/10", network_instance="default", mode="bridged"),
    ]
    return devices, neighbors, interfaces


def test_both_ends_of_a_cable_are_one_link_when_each_sends_a_short_name_for_its_port() -> None:
    devices, neighbors, interfaces = port_channel_network()
    topology = build_topology(devices, neighbors, [], [], is_management, [], interfaces)
    assert sorted(link.id for link in topology.links) == [
        "access:GigabitEthernet1/0/51--core:Ethernet1/1", "access:GigabitEthernet1/0/52--core:Ethernet1/2",
    ]
    assert not any(link.one_sided for link in topology.links)


def test_a_short_name_that_could_be_two_ports_is_left_as_it_was_sent() -> None:
    devices = [DeviceInfo(name="core", platform="cisco_nxos", host="172.20.20.60"), DeviceInfo(name="access", platform="cisco_ios", host="172.20.20.61")]
    neighbors = [LldpNeighbor(device="core", interface="Ethernet1/1", neighbor="access", neighbor_interface="Tw1/0/1")]
    interfaces = [
        Interface(device="access", name="TwoGigabitEthernet1/0/1", mode="bridged"),
        Interface(device="access", name="TwentyFiveGigE1/0/1", mode="bridged"),
    ]
    topology = build_topology(devices, neighbors, [], [], is_management, [], interfaces)
    assert [link.id for link in topology.links] == ["access:Tw1/0/1--core:Ethernet1/1"]


def test_a_host_behind_a_port_channel_is_followed_to_the_switch_its_members_are_cabled_to() -> None:
    devices, neighbors, interfaces = port_channel_network()
    arp = [ArpEntry(device="core", interface="Vlan10", ip_address="10.3.0.20", mac="0200.0000.0020")]
    macs = [
        # The MAC tables name the port-channels, not the cables LLDP is heard on.
        MacEntry(device="core", network_instance="default", mac="0200.0000.0020", interface="port-channel10", vlan=10),
        MacEntry(device="access", network_instance="default", mac="0200.0000.0020", interface="GigabitEthernet1/0/10", vlan=10),
    ]
    topology = build_topology(devices, neighbors, arp, [], is_management, macs, interfaces)
    host = next(link for link in topology.links if "10.3.0.20" in link.id)
    assert host.id == "10.3.0.20--access:GigabitEthernet1/0/10"
    assert host.evidence == [
        "ARP on core Vlan10: 10.3.0.20 is at 0200.0000.0020; MAC table on core (default): 0200.0000.0020 on port-channel10; "
        "MAC table on access (default): 0200.0000.0020 on GigabitEthernet1/0/10."
    ]


def test_a_mac_the_far_switch_has_on_its_port_channel_back_is_the_far_switch_s() -> None:
    devices, neighbors, interfaces = port_channel_network()
    arp = [ArpEntry(device="core", interface="Vlan10", ip_address="10.3.0.2", mac="0200.0000.0002")]
    macs = [
        MacEntry(device="core", network_instance="default", mac="0200.0000.0002", interface="port-channel10", vlan=10),
        MacEntry(device="access", network_instance="default", mac="0200.0000.0002", interface="Port-channel1", vlan=10),
    ]
    topology = build_topology(devices, neighbors, arp, [], is_management, macs, interfaces)
    assert "10.3.0.2" in node(topology, "access").addresses
    assert "10.3.0.2" not in {n.id for n in topology.nodes}


def test_an_address_at_the_switch_s_own_gateway_mac_is_the_switch_s() -> None:
    # A gateway address the core shares with its vPC peer by VRRP: its own MAC table says the MAC is its own.
    devices, neighbors, interfaces = port_channel_network()
    arp = [ArpEntry(device="core", interface="Vlan10", ip_address="10.3.0.254", mac="0000.5e00.010a")]
    macs = [MacEntry(device="core", network_instance="default", mac="0000.5e00.010a", interface="sup-eth1", entry_type="gateway", vlan=10)]
    topology = build_topology(devices, neighbors, arp, [], is_management, macs, interfaces)
    assert "10.3.0.254" in node(topology, "core").addresses
    assert "10.3.0.254" not in {n.id for n in topology.nodes}


def vpc_pair(entry_type: str, oper_state: str, lldp: bool = True) -> Topology:
    """
    Two gateways that both ARP for a host, which is cabled to core2 alone: core1's MAC table names its own port of
    the same number, as NX-OS does with its vPC peer's MACs, and that port has no cable.
    """
    devices = [DeviceInfo(name=name, platform="cisco_nxos", host=f"192.0.2.{index}")
               for index, name in enumerate(("core1", "core2"), start=1)]
    mac = "0200.0000.00ea"
    neighbors = [LldpNeighbor(device="core2", interface="Ethernet1/24", neighbor="server1", neighbor_interface="eth0",
                              neighbor_chassis_id=mac)] if lldp else []
    interfaces = [Interface(device=device, name="Ethernet1/24", network_instance="default", mode="bridged",
                            oper_state=oper_state if device == "core1" else "up") for device in ("core1", "core2")]
    interfaces += [Interface(device=device, name="Vlan254", network_instance="default", mode="routed",
                             addresses=[f"198.51.100.{index}/24"]) for index, device in enumerate(("core1", "core2"), start=2)]
    arp = [ArpEntry(device=device, interface="Vlan254", ip_address="198.51.100.234", mac=mac) for device in ("core1", "core2")]
    macs = [MacEntry(device="core1", network_instance="default", mac=mac, interface="Ethernet1/24", entry_type=entry_type, vlan=254),
            MacEntry(device="core2", network_instance="default", mac=mac, interface="Ethernet1/24", entry_type="dynamic", vlan=254)]
    return build_topology(devices, neighbors, arp, [], is_management, macs, interfaces)


@pytest.mark.parametrize(("entry_type", "oper_state"), [("secondary", "up"), ("dynamic", "down")])
def test_a_mac_entry_the_switch_didn_t_learn_or_on_a_port_that_is_down_doesn_t_place_a_host(
    entry_type: str, oper_state: str
) -> None:
    topology = vpc_pair(entry_type, oper_state)
    # Placed once, where core2's walk ended: the LLDP neighbor that is the host, not a phantom on core1's port.
    assert "198.51.100.234" not in {item.id for item in topology.nodes}
    server = node(topology, "server1")
    assert server.addresses == ["198.51.100.234"] and server.macs == ["0200.0000.00ea"]
    why = "an entry it didn't learn itself" if entry_type == "secondary" else "a port that is down"
    # What core1 saw stays as evidence, with why its MAC table entry wasn't followed.
    assert f"ARP on core1 Vlan254: 198.51.100.234 is at 0200.0000.00ea; MAC table on core1 (default): 0200.0000.00ea " \
           f"on Ethernet1/24, {why}." in server.evidence


def test_two_walks_that_end_at_ports_both_place_the_host() -> None:
    # A host with a NIC on each gateway, and no LLDP: both walks found a port, so both are kept.
    topology = vpc_pair("dynamic", "up", lldp=False)
    assert {link.id for link in topology.links if "198.51.100.234" in link.id} == {
        "198.51.100.234--core1:Ethernet1/24", "198.51.100.234--core2:Ethernet1/24",
    }


def test_layer_3_is_the_routers_and_the_networks_between_them() -> None:
    network = SwitchedNetwork()
    l3 = build_l3(network.devices, network.topology(is_management), network.interfaces, is_management)
    assert l3.layer == "l3"
    assert [(n.id, n.kind) for n in l3.nodes] == [
        ("r0", "device"), ("r1", "device"), ("10.1.0.2", "host"), ("10.1.0.3", "host"), ("10.1.0.0/24", "subnet"),
    ]  # sw1 routes nothing, so it isn't here
    links = {link.id: link for link in l3.links}
    assert sorted(links) == [
        "10.1.0.0/24--10.1.0.2", "10.1.0.0/24--10.1.0.3", "10.1.0.0/24--r1:ethernet-1/1.0",
        "r0:ethernet-1/1.0--r1:ethernet-1/49.0",
    ]
    # Two routers on a /31 are a link between them, not a network node.
    point_to_point = links["r0:ethernet-1/1.0--r1:ethernet-1/49.0"]
    assert (point_to_point.subnet, point_to_point.a.address, point_to_point.b.address) == (
        "10.0.0.0/31", "10.0.0.0/31", "10.0.0.1/31",
    )
    assert links["10.1.0.0/24--r1:ethernet-1/1.0"].b.address == "10.1.0.1/24"


def test_a_loopback_s_single_address_is_the_router_s_not_a_network() -> None:
    network = SwitchedNetwork()
    loopbacks = [
        Interface(device="r0", name="lo0.0", network_instance="default", mode="routed", addresses=["10.255.0.1/32"]),
        Interface(device="r1", name="lo0.0", network_instance="default", mode="routed", addresses=["10.255.0.2/32"]),
    ]
    l3 = build_l3(network.devices, network.topology(is_management), network.interfaces + loopbacks, is_management)
    assert [node.id for node in l3.nodes if node.kind == "subnet"] == ["10.1.0.0/24"]
    assert not [link for link in l3.links if "lo0.0" in link.id]


def test_a_layer_3_view_names_networks_and_says_which_layer_it_is() -> None:
    network = SwitchedNetwork()
    l3 = build_l3(network.devices, network.topology(is_management), network.interfaces, is_management)
    view = make_view(l3, "The routing", ["10.1.0.0/24"], [], {})
    assert view.layer == "l3" and view.emphasis["10.1.0.0/24"] == "focus"
    assert "at layer 3 (routers and the networks between them)" in describe(view)
    with pytest.raises(ViewError, match="nothing on the data plane at layer 3 called 'sw1'"):
        make_view(l3, "", ["sw1"], [], {})


def test_a_host_s_node_has_the_mac_arp_found_it_at_and_a_device_s_has_none() -> None:
    topology = SwitchedNetwork().topology(is_management)
    assert node(topology, "10.1.0.2").macs == ["AA:00:00:00:00:02"]
    assert all(not item.macs for item in topology.nodes if item.kind == "device")


def test_focus_on_a_host_is_the_host_its_cable_and_the_switch_at_the_far_end_without_its_evidence() -> None:
    around = focused(SwitchedNetwork().topology(is_management), ["10.1.0.2"])
    assert [link.id for link in around.links] == ["10.1.0.2--sw1:ethernet-1/2"]
    assert {item.id: bool(item.evidence) for item in around.nodes} == {"sw1": False, "10.1.0.2": True}


@pytest.mark.parametrize("name", ["aa00.0000.0003", "aa-00-00-00-00-03", "AA:00:00:00:00:03"])
def test_a_host_is_found_by_its_mac_in_any_spelling(name: str) -> None:
    topology = SwitchedNetwork().topology(is_management)
    assert [item.id for item in focused(topology, [name]).nodes if item.kind == "host"] == ["10.1.0.3"]
    assert make_view(topology, "", [], [name], {}).highlight == ["10.1.0.3"]


def test_a_network_with_no_node_of_its_own_names_the_hosts_on_it() -> None:
    around = focused(SwitchedNetwork().topology(is_management), ["10.1.0.0/24"])
    assert {item.id for item in around.nodes if item.kind == "host"} == {"10.1.0.2", "10.1.0.3"}


def big_graph(hosts: int) -> Topology:
    devices = [TopologyNode(id=name, kind="device", label=name) for name in ("core1.example.net", "core2.example.net")]
    found = [TopologyNode(id=f"10.0.0.{index}", kind="host", label=f"10.0.0.{index}", addresses=[f"10.0.0.{index}"])
             for index in range(1, hosts + 1)]
    return Topology(nodes=devices + found)


def test_a_device_is_found_by_its_name_without_the_domain() -> None:
    assert [item.id for item in focused(big_graph(1), ["CORE1"]).nodes] == ["core1.example.net"]


def test_a_name_on_a_big_graph_that_isnt_there_is_refused_with_its_devices_and_how_many_hosts() -> None:
    with pytest.raises(ViewError) as refused:
        focused(big_graph(50), ["core3"])
    message = str(refused.value)
    assert "Its nodes are: core1.example.net, core2.example.net, and 50 hosts, named by address." in message
    assert "10.0.0.1" not in message


def test_an_address_two_nodes_hold_names_them_both() -> None:
    # A gateway address a pair of switches share.
    graph = big_graph(1)
    for device in graph.nodes[:2]:
        device.addresses.append("10.0.0.254")
    assert {item.id for item in focused(graph, ["10.0.0.254"]).nodes} == {"core1.example.net", "core2.example.net"}
    assert named(graph, ["10.0.0.254", "10.0.0.1"]) == {
        "10.0.0.254": {"core1.example.net", "core2.example.net"}, "10.0.0.1": {"10.0.0.1"},
    }
