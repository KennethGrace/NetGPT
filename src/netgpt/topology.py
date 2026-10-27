"""
The topology module merges what devices report about their neighbors into one
graph of the network, at layer 2 or at layer 3.

At layer 2 the graph is the cabling. LLDP says which ports are cabled together.
ARP says which addresses answer behind a port, and where a switch is in the way,
its MAC table says which of its ports the address is behind, so each host hangs
off the port it is plugged into. Interfaces say which ports route and which bridge.

At layer 3 the graph is the routing: routers, the networks their interfaces are
on, and the hosts on those networks. Switches drop out, since they route nothing.
A network between just two routers is drawn as a link between them.

Every node and link keeps the evidence it came from, so a drawing can always say
why a line is there.
"""

from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Literal

from pydantic import BaseModel, Field

from netgpt.primitives import ArpEntry, DeviceError, DeviceInfo, Interface, LldpNeighbor, MacEntry

Plane = Literal["data", "management"]
PlaneChoice = Literal["data", "management", "all"]
Layer = Literal["l2", "l3"]

# A subinterface (ethernet-1/1.0, GigabitEthernet0/0.100) belongs to the port before the dot.
SUBINTERFACE = re.compile(r"^(?P<port>.+)\.\d+$")

# MAC table entries for the switch's own addresses (its IRB interfaces, reserved MACs). Every other
# kind (learnt, learned, dynamic, static, ...) says which port a MAC is behind.
OWN_ENTRIES = {"irb-interface", "irb-interface-anycast", "reserved"}

# How far an address is followed from switch to switch before giving up.
MAX_SWITCHES = 16

# Is this interface of this inventory device a management port?
ManagementCheck = Callable[[str, str], bool]

Endpoint = tuple[str, str | None]


def port_of(interface: str) -> str:
    """The physical port an interface or subinterface belongs to."""
    match = SUBINTERFACE.match(interface)
    return match.group("port") if match else interface


def mac_key(mac: str | None) -> str:
    """A MAC address in one spelling, whatever the platform's (AA:BB:.., aabb.ccdd.eeff)."""
    return re.sub(r"[^0-9a-f]", "", (mac or "").lower())


class TopologyNode(BaseModel):
    """A device, an LLDP neighbor outside the inventory, a host found by ARP, a shared segment, or an IP network."""

    id: str = Field(
        description="How to name the node: a device or system name, a host's IPv4 address, a segment ID, or a network."
    )
    kind: Literal["device", "neighbor", "host", "segment", "subnet"]
    label: str
    plane: Plane | None = Field(
        None, description="The plane a host, neighbor, segment, or network is on. None for inventory devices: they are on both."
    )
    role: str | None = None
    platform: str | None = None
    addresses: list[str] = Field(default_factory=list)
    error: str | None = Field(None, description="Why the device could not be fully queried.")
    evidence: list[str] = Field(default_factory=list)


class LinkEnd(BaseModel):
    """One end of a link: a node, and the interface on it when one is known."""

    node: str
    interface: str | None = None
    address: str | None = Field(None, description="At layer 3, this end's address with prefix length.")
    bridges: list[str] = Field(
        default_factory=list, description="At layer 2, the bridges (layer 2 network instances) this port switches in."
    )

    def __str__(self) -> str:
        return f"{self.node}:{self.interface}" if self.interface else self.node


class TopologyLink(BaseModel):
    """A cable seen by LLDP, an attachment seen by ARP, or a network two routers share."""

    id: str
    a: LinkEnd
    b: LinkEnd
    plane: Plane
    subnet: str | None = Field(None, description="At layer 3, the network a link between two routers is.")
    one_sided: bool = Field(False, description="Only one end reported this link, though both ends were asked.")
    evidence: list[str] = Field(default_factory=list)


class Topology(BaseModel):
    """The network as a graph at one layer. Devices that could not be queried are listed in errors."""

    layer: Layer = "l2"
    nodes: list[TopologyNode] = Field(default_factory=list)
    links: list[TopologyLink] = Field(default_factory=list)
    errors: list[DeviceError] = Field(default_factory=list)

    def on_plane(self, plane: PlaneChoice) -> Topology:
        """The part of the graph on one plane. Inventory devices are on every plane."""
        if plane == "all":
            return self
        return Topology(
            layer=self.layer,
            nodes=[node for node in self.nodes if node.plane in (None, plane)],
            links=[link for link in self.links if link.plane == plane],
            errors=self.errors,
        )


class _Graph:
    """Nodes and links under construction, merged by ID."""

    def __init__(self) -> None:
        self.nodes: dict[str, TopologyNode] = {}
        self.links: dict[str, TopologyLink] = {}
        self.reporters: dict[str, set[str]] = defaultdict(set)

    def node(self, node_id: str, **fields: object) -> TopologyNode:
        if node_id not in self.nodes:
            fields.setdefault("label", node_id)
            self.nodes[node_id] = TopologyNode(id=node_id, **fields)
        return self.nodes[node_id]

    def link(self, a: LinkEnd, b: LinkEnd, plane: Plane, evidence: str, reporter: str, **fields: object) -> TopologyLink:
        a, b = sorted((a, b), key=str)
        link_id = f"{a}--{b}"
        if link_id not in self.links:
            self.links[link_id] = TopologyLink(id=link_id, a=a, b=b, plane=plane, **fields)
        link = self.links[link_id]
        if evidence not in link.evidence:
            link.evidence.append(evidence)
        self.reporters[link_id].add(reporter)
        return link

    def sorted(self, devices: list[DeviceInfo]) -> tuple[list[TopologyNode], list[TopologyLink]]:
        """Devices in inventory order, then everything else by kind and address."""
        order = {"device": 0, "neighbor": 1, "host": 2, "segment": 3, "subnet": 4}
        position = {device.name: index for index, device in enumerate(devices)}
        nodes = sorted(
            self.nodes.values(),
            key=lambda node: (order[node.kind], position.get(node.id, 0), _address_key(node), node.id),
        )
        return nodes, sorted(self.links.values(), key=lambda link: link.id)


def _segments(seen: dict[Endpoint, list[LldpNeighbor]]) -> dict[Endpoint, str]:
    """
    Find shared segments. A port that hears more than one LLDP neighbor is on a
    switch or bridge that doesn't speak LLDP itself, along with every port it hears.
    Returns the segment ID of every port on one.
    """
    parent: dict[Endpoint, Endpoint] = {}

    def root(endpoint: Endpoint) -> Endpoint:
        parent.setdefault(endpoint, endpoint)
        while parent[endpoint] != endpoint:
            parent[endpoint] = parent[parent[endpoint]]
            endpoint = parent[endpoint]
        return endpoint

    for port, records in seen.items():
        if len(records) > 1:
            for record in records:
                parent[root((_neighbor_id(record), record.neighbor_interface))] = root(port)
    members: dict[Endpoint, list[Endpoint]] = defaultdict(list)
    for endpoint in list(parent):
        members[root(endpoint)].append(endpoint)
    groups = sorted((sorted(group, key=str) for group in members.values()), key=lambda group: str(group[0]))
    return {endpoint: f"segment-{number}" for number, group in enumerate(groups, 1) for endpoint in group}


def _neighbor_id(record: LldpNeighbor) -> str:
    return record.neighbor or record.neighbor_chassis_id or f"unknown neighbor of {record.device} {record.interface}"


def _address_key(node: TopologyNode) -> tuple[int, ...]:
    """Hosts and networks in address order."""
    try:
        network = ipaddress.IPv4Network(node.id)
    except ValueError:
        return ()
    return (*network.network_address.packed, network.prefixlen)


def _interface_evidence(interfaces: list[Interface]) -> list[str]:
    """What a device's interfaces say about it, in a line or two."""
    routed = [f"{interface.name} {', '.join(interface.addresses)}" for interface in interfaces if interface.addresses]
    bridges: dict[str, list[str]] = defaultdict(list)
    for interface in interfaces:
        if interface.mode == "bridged":
            bridges[interface.network_instance or "a bridge"].append(interface.name)
    lines = [f"Routed interfaces: {'; '.join(routed)}."] if routed else []
    lines += [f"Bridged in {bridge}: {', '.join(names)}." for bridge, names in sorted(bridges.items())]
    return lines


def build_topology(
    devices: list[DeviceInfo],
    neighbors: list[LldpNeighbor],
    arp: list[ArpEntry],
    errors: list[DeviceError],
    is_management: ManagementCheck,
    macs: Iterable[MacEntry] = (),
    interfaces: Iterable[Interface] = (),
) -> Topology:
    """
    Merge every device's LLDP, ARP, and MAC tables, and what its interfaces say,
    into one graph of the network at layer 2.
    """
    graph = _Graph()
    inventory = {device.name: device for device in devices}
    failed: dict[str, list[str]] = defaultdict(list)
    for error in errors:
        failed[error.device].append(error.error)
    interfaces_of: dict[str, list[Interface]] = defaultdict(list)
    for interface in interfaces:
        interfaces_of[interface.device].append(interface)
    for device in devices:
        own = [address.split("/")[0] for interface in interfaces_of[device.name] for address in interface.addresses]
        graph.node(
            device.name,
            kind="device",
            role=device.role,
            platform=device.platform,
            addresses=list(dict.fromkeys([device.host, *own])),
            error="; ".join(dict.fromkeys(failed[device.name])) or None,
            evidence=[f"In the inventory, managed at {device.host}.", *_interface_evidence(interfaces_of[device.name])],
        )

    # The bridges each port switches in, the addresses each device owns, and where switches have seen each MAC.
    bridges_at: dict[Endpoint, set[str]] = defaultdict(set)
    owned: dict[str, str] = {}
    for interface in interfaces:
        if interface.mode == "bridged" and interface.network_instance:
            bridges_at[(interface.device, port_of(interface.name))].add(interface.network_instance)
        for address in interface.addresses:
            owned.setdefault(address.split("/")[0], interface.device)
    learned: dict[tuple[str, str], list[MacEntry]] = defaultdict(list)
    for entry in macs:
        learned[(entry.device, mac_key(entry.mac))].append(entry)

    def management(endpoint: Endpoint) -> bool:
        node, interface = endpoint
        return node in inventory and interface is not None and is_management(node, port_of(interface))

    seen: dict[Endpoint, list[LldpNeighbor]] = defaultdict(list)
    for record in neighbors:
        seen[(record.device, record.interface)].append(record)
    segment_of = _segments(seen)

    for segment_id in sorted(set(segment_of.values())):
        ports = sorted((endpoint for endpoint, segment in segment_of.items() if segment == segment_id), key=str)
        names = {interface for _, interface in ports}
        on_management = any(management(port) for port in ports)
        graph.node(
            segment_id,
            kind="segment",
            label=f"{names.pop()} segment" if len(names) == 1 and None not in names else "shared segment",
            plane="management" if on_management else "data",
            evidence=[
                f"LLDP: {device} {interface} hears {len(seen[(device, interface)])} neighbors, "
                "so it shares a segment with them."
                for device, interface in ports
                if len(seen.get((device, interface), [])) > 1
            ],
        )

    for (device, interface), records in seen.items():
        for record in records:
            remote: Endpoint = (_neighbor_id(record), record.neighbor_interface)
            plane: Plane = "management" if management((device, interface)) or management(remote) else "data"
            evidence = f"LLDP on {device} {interface}: {remote[0]} port {remote[1] or 'unknown'}"
            if remote[0] not in inventory:
                neighbor = graph.node(remote[0], kind="neighbor", plane=plane)
                if plane == "data":
                    neighbor.plane = "data"  # Seen on the data plane anywhere puts a neighbor on it.
                if record.neighbor_chassis_id and f"Chassis {record.neighbor_chassis_id}." not in neighbor.evidence:
                    neighbor.evidence.append(f"Chassis {record.neighbor_chassis_id}.")
                if evidence not in neighbor.evidence:
                    neighbor.evidence.append(evidence)
            segment = segment_of.get((device, interface))
            if segment is not None:
                # Both ends hang off the segment. The far end may not speak for itself.
                for node, port in ((device, interface), remote):
                    graph.link(LinkEnd(node=node, interface=port), LinkEnd(node=segment), plane, evidence, device)
            else:
                graph.link(
                    LinkEnd(node=device, interface=interface),
                    LinkEnd(node=remote[0], interface=remote[1]),
                    plane,
                    evidence,
                    device,
                )

    def forwarding(device: str, mac: str, bridges: set[str] | None) -> MacEntry | None:
        """Where a switch's MAC table says a MAC is, in one of the given bridges (or any)."""
        for entry in learned.get((device, mac), []):
            if bridges is None or entry.network_instance in bridges:
                return entry
        return None

    def locate(entry: ArpEntry) -> tuple[str | None, LinkEnd | None, list[str]]:
        """
        Follow an address from the port it was heard on to where it is. Across a
        cable to a switch, the switch's MAC table says which of its ports the
        address is behind, and so on until a port with no cable: the station is
        plugged in there. Returns the node that owns the address, when it belongs
        to one already on the graph, or else where the station hangs off, with the
        trail of MAC table entries followed.
        """
        mac = mac_key(entry.mac)
        node, port = entry.device, port_of(entry.interface)
        trail: list[str] = []
        if (node, port) not in seen and mac and (found := forwarding(node, mac, None)) is not None:
            # Heard on a VLAN interface (IRB, SVI): the bridge's own MAC table says which port.
            trail.append(f"MAC table on {node} ({found.network_instance}): {entry.mac} on {found.interface}")
            port = port_of(found.interface)
        for _ in range(MAX_SWITCHES):
            if (segment := segment_of.get((node, port))) is not None:
                return None, LinkEnd(node=segment), trail
            heard = seen.get((node, port), [])
            if not heard:
                return None, LinkEnd(node=node, interface=port, bridges=sorted(bridges_at.get((node, port), ()))), trail
            far, far_port = _neighbor_id(heard[0]), heard[0].neighbor_interface
            bridges = bridges_at.get((far, port_of(far_port))) if far_port else None
            if far not in inventory or not bridges:
                # A routed port, or a neighbor that can't be asked: the address is the neighbor's.
                return far, None, trail
            found = forwarding(far, mac, bridges) if mac else None
            if found is None:
                trail.append(f"{far} bridges {far_port}, but its MAC table doesn't list {entry.mac or 'the MAC'} (aged out?)")
                return None, LinkEnd(node=far, bridges=sorted(bridges)), trail
            trail.append(f"MAC table on {far} ({found.network_instance}): {entry.mac} on {found.interface}")
            if (found.entry_type or "").lower() in OWN_ENTRIES or port_of(found.interface) == port_of(far_port):
                return far, None, trail
            node, port = far, port_of(found.interface)
        return None, LinkEnd(node=node, interface=port), trail

    managed_at = {device.host: device.name for device in devices}
    for entry in arp:
        endpoint: Endpoint = (entry.device, port_of(entry.interface))
        plane = "management" if management(endpoint) else "data"
        evidence = f"ARP on {entry.device} {entry.interface}: {entry.ip_address} is at {entry.mac or 'an unknown MAC'}"
        if entry.ip_address in managed_at:
            continue  # An inventory device's own management address; nothing new.
        if entry.ip_address in owned:
            continue  # An address an inventory device reports on its own interfaces.
        owner, attach, trail = locate(entry)
        said = "; ".join([evidence, *trail])
        if owner is not None:
            node = graph.nodes[owner]
            if entry.ip_address not in node.addresses:
                node.addresses.append(entry.ip_address)
            node.evidence.append(f"{said}, the LLDP neighbor on that port." if not trail else f"{said}: {owner}'s address.")
            continue
        host = graph.node(entry.ip_address, kind="host", plane=plane, addresses=[entry.ip_address])
        if plane == "data":
            host.plane = "data"  # Seen on the data plane anywhere puts a host on it.
        host.evidence.append(f"{said}.")
        graph.link(attach, LinkEnd(node=entry.ip_address), plane, f"{said}.", entry.device)

    for link in graph.links.values():
        for end in (link.a, link.b):
            if end.interface is not None and not end.bridges:
                end.bridges = sorted(bridges_at.get((end.node, port_of(end.interface)), ()))

    # Both ends of a cable between two answering devices should report it.
    answered = {device for device in inventory if not failed[device]}
    for link in graph.links.values():
        ends = {link.a.node, link.b.node}
        link.one_sided = ends <= answered and len(ends) == 2 and len(graph.reporters[link.id]) == 1

    nodes, links = graph.sorted(devices)
    return Topology(layer="l2", nodes=nodes, links=links, errors=errors)


def build_l3(
    devices: list[DeviceInfo],
    l2: Topology,
    interfaces: Iterable[Interface],
    is_management: ManagementCheck,
    network_instance: str = "default",
) -> Topology:
    """
    The network at layer 3, in one network instance: the routers, the networks
    their interfaces are on, and the hosts and neighbors (from the layer 2 graph)
    with addresses on those networks.
    """
    graph = _Graph()
    by_id = {node.id: node for node in l2.nodes}
    members: dict[ipaddress.IPv4Network, list[tuple[str, LinkEnd, str]]] = defaultdict(list)
    for interface in interfaces:
        if interface.network_instance != network_instance or interface.mode != "routed":
            continue
        for address in interface.addresses:
            try:
                network = ipaddress.IPv4Interface(address).network
            except ValueError:
                continue
            end = LinkEnd(node=interface.device, interface=interface.name, address=address)
            members[network].append((interface.device, end, f"{interface.device} {interface.name} is {address}."))

    routers = {device for found in members.values() for device, _, _ in found}
    for node in l2.nodes:
        if node.kind == "device" and (node.id in routers or node.error):
            graph.node(node.id, **node.model_dump(exclude={"id"}))
        if node.kind not in ("host", "neighbor"):
            continue
        for address in node.addresses:
            try:
                ip = ipaddress.IPv4Address(address)
            except ValueError:
                continue
            network = next((network for network in members if ip in network), None)
            if network is not None:
                members[network].append((node.id, LinkEnd(node=node.id), f"{address} answers ARP on {network}."))

    for network, found in sorted(members.items(), key=lambda item: (item[0].network_address, item[0].prefixlen)):
        # Only inventory devices' ends name an interface, and only they have management ports.
        on_management = any(end.interface and is_management(node, port_of(end.interface)) for node, end, _ in found)
        plane: Plane = "management" if on_management else "data"
        for node, _, _ in found:
            if node not in graph.nodes:
                graph.node(node, **by_id[node].model_dump(exclude={"id"}))
        kinds = [graph.nodes[node].kind for node, _, _ in found]
        if network.prefixlen >= 30 and len(found) == 2 and "host" not in kinds:
            # A point-to-point network between two routers is just a link between them.
            (first, a, said_a), (second, b, said_b) = found
            link = graph.link(a, b, plane, said_a, first, subnet=str(network))
            if said_b not in link.evidence:
                link.evidence.append(said_b)
            continue
        subnet = graph.node(str(network), kind="subnet", plane=plane, evidence=[said for _, _, said in found])
        for node, end, said in found:
            graph.link(end, LinkEnd(node=subnet.id), plane, said, node)

    nodes, links = graph.sorted(devices)
    return Topology(layer="l3", nodes=nodes, links=links, errors=l2.errors)
