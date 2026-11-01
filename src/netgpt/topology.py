"""
The topology module merges what devices report about their neighbors into one
graph of the network, at layer 2, at layer 3, or as OSPF sees it.

At layer 2 the graph is the cabling. LLDP says which ports are cabled together.
ARP says which addresses answer behind a port, and where a switch is in the way,
its MAC table says which of its ports the address is behind, so each host hangs
off the port it is plugged into. Interfaces say which ports route and which bridge.

At layer 3 the graph is the routing: routers, the networks their interfaces are
on, and the hosts on those networks. Switches drop out, since they route nothing.
A network between just two routers is drawn as a link between them.

In the OSPF view the graph is the routing protocol: the routers that run OSPF,
the adjacencies between them with each end's area, cost, and state, and the
networks they advertise without forming adjacencies (passive interfaces). Two
OSPF interfaces on one network with no full adjacency are drawn too, with what
their settings disagree on, since a missing neighbor is often the question.

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

from netgpt.primitives import (
    ArpEntry,
    DeviceError,
    DeviceInfo,
    Interface,
    LldpNeighbor,
    MacEntry,
    OspfInterface,
    OspfNeighbor,
)

Plane = Literal["data", "management"]
PlaneChoice = Literal["data", "management", "all"]
Layer = Literal["l2", "l3", "ospf"]

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
    router_id: str | None = Field(None, description="In the OSPF view, the router's OSPF router ID.")
    areas: list[str] = Field(
        default_factory=list, description="In the OSPF view, the areas the router is in. More than one is an area border router."
    )
    evidence: list[str] = Field(default_factory=list)


class LinkEnd(BaseModel):
    """One end of a link: a node, and the interface on it when one is known."""

    node: str
    interface: str | None = None
    address: str | None = Field(None, description="At layer 3, this end's address with prefix length.")
    bridges: list[str] = Field(
        default_factory=list, description="At layer 2, the bridges (layer 2 network instances) this port switches in."
    )
    area: str | None = Field(None, description="In the OSPF view, the area this end's interface is in.")
    cost: int | None = Field(None, description="In the OSPF view, this end's cost to send across the link.")
    state: str | None = Field(
        None, description="In the OSPF view, the adjacency's state as this end sees it; none if it doesn't hear the other end."
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
    problems: list[str] = Field(
        default_factory=list,
        description="In the OSPF view, why the link has no full adjacency, or what its two ends disagree on.",
    )
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
            if network.prefixlen == network.max_prefixlen:
                continue  # A loopback's single address names no network; the router's addresses list it.
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


# The settings both ends of a link must agree on before OSPF makes them neighbors.
OSPF_AGREEMENT = (
    ("areas", "area"),
    ("network types", "network_type"),
    ("hello intervals", "hello_interval"),
    ("dead intervals", "dead_interval"),
    ("MTUs", "mtu"),
    ("area types", "area_type"),
    ("authentication types", "authentication"),
)


def _ospf_says(interface: OspfInterface) -> str:
    """What a router says about one of its OSPF interfaces, in a line."""
    settings = [f"area {interface.area}", interface.network_type or "network type unknown", f"cost {interface.cost}"]
    settings += [f"hello {interface.hello_interval}s, dead {interface.dead_interval}s", f"MTU {interface.mtu}"]
    if interface.area_type and interface.area_type != "normal":
        settings.append(f"a {interface.area_type} area")
    if interface.authentication and interface.authentication != "none":
        settings.append(f"{interface.authentication} authentication, keychain {interface.keychain}")
    if interface.passive:
        settings.append("passive")
    return f"{interface.device} {interface.interface} runs OSPF: {', '.join(settings)}."


# The settings in OSPF_AGREEMENT that each interface has of its own, by their names there. Areas differ by design,
# and an area's type is one setting per router, so how the other interfaces are set says nothing about either.
OSPF_INTERFACE_SETTINGS = {
    "network types": "network type", "hello intervals": "hello interval", "dead intervals": "dead interval", "MTUs": "MTU",
    "authentication types": "authentication type",
}


def _end(interface: OspfInterface) -> str:
    return f"{interface.device} {interface.interface}"


def _how_the_others_are_set(setting: str, field: str, ends: list[OspfInterface], others: list[OspfInterface]) -> str | None:
    """
    How the other OSPF interfaces that send hellos have one setting two ends disagree
    on, so the end set unlike the rest is a fact the reader has, not a guess. Which
    end is wrong stays the reader's to decide.
    """
    counts: dict[object, int] = defaultdict(int)
    for other in others:
        if getattr(other, field) is not None:
            counts[getattr(other, field)] += 1
    if not counts:
        return None
    total = sum(counts.values())
    if len(counts) > 1:
        tally = sorted(counts.items(), key=lambda item: -item[1])
        # The setting's name once, with the most common value: "5 have authentication type none and 3 have md5".
        parts = [f"{count} {'has' if count == 1 else 'have'} {setting + ' ' if index == 0 else ''}{value}"
                 for index, (value, count) in enumerate(tally)]
        return f"Of the other {total} OSPF interfaces that aren't passive, {', '.join(parts[:-1])} and {parts[-1]}."
    [(value, _)] = counts.items()
    others_have = (
        f"The other OSPF interface that isn't passive has {setting} {value}" if total == 1
        else f"The other {total} OSPF interfaces that aren't passive all have {setting} {value}"
    )
    alike = [end for end in ends if getattr(end, field) == value]
    if not alike:
        return f"{others_have}, unlike either end."
    unlike = next(end for end in ends if end is not alike[0])
    return f"{others_have}, as {_end(alike[0])} does; {_end(unlike)} is the only one with {getattr(unlike, field)}."


def ospf_disagreements(ends: list[OspfInterface], others: list[OspfInterface]) -> list[str]:
    """
    What two OSPF interfaces on one network disagree on, from their settings alone:
    each setting that differs, with how the other OSPF interfaces that send hellos
    (others) have it when it is an interface's own, and an end that is passive.
    """
    lines = []
    first, second = ends
    for name, field in OSPF_AGREEMENT:
        mine, theirs = getattr(first, field), getattr(second, field)
        if mine is not None and theirs is not None and mine != theirs:
            lines.append(f"The {name} differ: {mine} at {_end(first)}, {theirs} at {_end(second)}.")
            if name in OSPF_INTERFACE_SETTINGS:
                rest = _how_the_others_are_set(OSPF_INTERFACE_SETTINGS[name], field, ends, others)
                lines += [rest] if rest else []
    lines += [f"{_end(end)} is passive, so it sends no hellos." for end in ends if end.passive]
    return lines


def _others(ends: list[OspfInterface], interfaces: list[OspfInterface]) -> list[OspfInterface]:
    """The interfaces, in the ends' network instance, that send hellos and aren't either end."""
    taken = {(end.device, end.interface) for end in ends}
    return [
        interface for interface in interfaces
        if not interface.passive and interface.network_instance == ends[0].network_instance
        and (interface.device, interface.interface) not in taken
    ]


def _addresses(interfaces: Iterable[Interface]) -> dict[tuple[str, str], list[ipaddress.IPv4Interface]]:
    """Each interface's IPv4 addresses, with their prefix lengths, by device and interface name."""
    found: dict[tuple[str, str], list[ipaddress.IPv4Interface]] = defaultdict(list)
    for interface in interfaces:
        for address in interface.addresses:
            try:
                found[(interface.device, interface.name)].append(ipaddress.IPv4Interface(address))
            except ValueError:
                continue
    return found


def _ospf_networks(
    ospf_interfaces: Iterable[OspfInterface], prefixes: dict[tuple[str, str], list[ipaddress.IPv4Interface]]
) -> dict[ipaddress.IPv4Network, list[OspfInterface]]:
    """
    The OSPF interfaces on each network, in address order, found from their devices'
    own addresses (prefixes, from _addresses). An interface's /32 is on no network.
    """
    members: dict[ipaddress.IPv4Network, list[OspfInterface]] = defaultdict(list)
    for interface in ospf_interfaces:
        found = prefixes.get((interface.device, interface.interface), [])
        exact = [prefix for prefix in found if str(prefix.ip) == interface.address]
        chosen = (exact or found)[:1]
        if chosen and chosen[0].network.prefixlen < chosen[0].network.max_prefixlen:
            members[chosen[0].network].append(interface)
    return dict(sorted(members.items(), key=lambda item: (item[0].network_address, item[0].prefixlen)))


def ospf_mismatches(interfaces: Iterable[Interface], ospf_interfaces: Iterable[OspfInterface]) -> list[str]:
    """
    What keeps routers that share a network from becoming neighbors, from their
    settings alone: for each network with two routers' OSPF interfaces on it, in each
    network instance, what the two ends disagree on (ospf_disagreements), after the network.
    """
    ospf_interfaces = list(ospf_interfaces)
    prefixes = _addresses(interfaces)
    mismatches = []
    for instance in sorted({interface.network_instance for interface in ospf_interfaces}):
        mine = [interface for interface in ospf_interfaces if interface.network_instance == instance]
        for network, ends in _ospf_networks(mine, prefixes).items():
            if len(ends) == 2 and ends[0].device != ends[1].device:
                where = f"{network}" if instance == "default" else f"{network} in {instance}"
                mismatches += [f"{where}: {line}" for line in ospf_disagreements(ends, _others(ends, mine))]
    return mismatches


def _ospf_problems(ends: list[OspfInterface], states: dict[str, str | None], others: list[OspfInterface]) -> list[str]:
    """
    What keeps two OSPF interfaces on one network from a full adjacency: the settings
    they disagree on (ospf_disagreements), and how far the adjacency got from each side.
    """
    problems = ospf_disagreements(ends, others)
    if all(state == "full" for state in states.values()):
        return problems
    # Two routers alone on a network always elect a DR, so between them even 2-way is stuck.
    for end in ends:
        state = states[end.device]
        other = next(other for other in ends if other is not end)
        if state is None:
            problems.append(f"{end.device} doesn't hear {other.device} on {end.interface}.")
        elif state != "full":
            problems.append(f"{end.device} sees its adjacency with {other.device} at {state}, not full.")
    return problems


def build_ospf(
    devices: list[DeviceInfo],
    interfaces: Iterable[Interface],
    ospf_interfaces: Iterable[OspfInterface],
    neighbors: Iterable[OspfNeighbor],
    errors: list[DeviceError],
    network_instance: str = "default",
) -> Topology:
    """
    The network as OSPF sees it, in one network instance: the routers that run it,
    the adjacencies between them, and the networks they advertise from passive
    interfaces. Two OSPF interfaces on one network are a link whether or not they
    became neighbors; one that doesn't hear the other, or whose settings differ,
    says so in the link's problems.
    """
    graph = _Graph()
    info = {device.name: device for device in devices}
    mine = [interface for interface in ospf_interfaces if interface.network_instance == network_instance]
    heard = [neighbor for neighbor in neighbors if neighbor.network_instance == network_instance]
    router_of = {interface.router_id: interface.device for interface in mine if interface.router_id}
    id_of = {device: router_id for router_id, device in router_of.items()}

    for name, device in info.items():
        own = [interface for interface in mine if interface.device == name]
        if not own:
            continue
        areas = sorted({interface.area for interface in own})
        router_id = own[0].router_id
        role = f" in area {areas[0]}" if len(areas) == 1 else f" in areas {', '.join(areas)}: an area border router"
        graph.node(
            name, kind="device", role=device.role, platform=device.platform, router_id=router_id, areas=areas,
            evidence=[f"{name} runs OSPF with router ID {router_id}{role}."],
        )
    for error in errors:
        device = info.get(error.device)
        graph.node(error.device, kind="device", error=error.error, role=device.role if device else None,
                   platform=device.platform if device else None)

    interfaces = list(interfaces)
    prefixes = _addresses(interfaces)
    on_network: dict[ipaddress.IPv4Network, list[tuple[str, str, ipaddress.IPv4Interface]]] = defaultdict(list)
    for interface in interfaces:
        if interface.network_instance == network_instance and interface.mode == "routed":
            for prefix in prefixes.get((interface.device, interface.name), []):
                on_network[prefix.network].append((interface.device, interface.name, prefix))

    def state_of(device: str, interface: str, other: str) -> str | None:
        """How far device's adjacency with other got on interface, or None if it doesn't hear it."""
        other_id = id_of.get(other)
        for neighbor in heard:
            if neighbor.device == device and neighbor.interface == interface and neighbor.router_id == other_id:
                return neighbor.state
        return None

    paired: set[tuple[str, str]] = set()
    for network, ends in _ospf_networks(mine, prefixes).items():
        routers = {end.device for end in ends}
        evidence = [_ospf_says(end) for end in ends]
        if len(ends) == 2 and len(routers) == 2:
            states = {end.device: state_of(end.device, end.interface, other.device) for end, other in (ends, ends[::-1])}
            a, b = (
                LinkEnd(node=end.device, interface=end.interface, address=str(prefix), area=end.area, cost=end.cost,
                        state=states[end.device])
                for end, prefix in ((end, next(p for p in prefixes[(end.device, end.interface)] if p.network == network))
                                    for end in ends)
            )
            link = graph.link(a, b, "data", evidence[0], ends[0].device, subnet=str(network))
            for line in evidence[1:] + [f"{end.device} hears {other.device} on {end.interface}: {states[end.device]}."
                                        for end, other in (ends, ends[::-1]) if states[end.device]]:
                if line not in link.evidence:
                    link.evidence.append(line)
            link.problems = _ospf_problems(ends, states, _others(ends, mine))
            link.one_sided = sum(state is not None for state in states.values()) == 1
            paired |= {(end.device, end.interface) for end in ends}
            continue
        silent = [
            (device, name, prefix) for device, name, prefix in on_network[network] if device in info and device not in routers
        ]
        if len(ends) == 1 and not ends[0].passive and silent:
            # Another router is on the network but doesn't run OSPF there, so this end hears no one.
            end, (device, name, prefix) = ends[0], silent[0]
            if device not in graph.nodes:
                graph.node(device, kind="device", role=info[device].role, platform=info[device].platform)
            own = next((p for p in prefixes[(end.device, end.interface)] if p.network == network), None)
            link = graph.link(
                LinkEnd(node=end.device, interface=end.interface, address=str(own) if own else None, area=end.area,
                        cost=end.cost),
                LinkEnd(node=device, interface=name, address=str(prefix)),
                "data", evidence[0], end.device, subnet=str(network),
            )
            link.problems = [
                f"{device} {name} is on {network} but doesn't run OSPF there, so {end.device} {end.interface} hears no neighbor."
            ]
            paired.add((end.device, end.interface))
            continue
        # A network with one OSPF router on it (a LAN it advertises) or several (a shared segment): a node.
        areas = sorted({end.area for end in ends})
        subnet = graph.node(str(network), kind="subnet", plane="data", areas=areas, evidence=evidence)
        for end in ends:
            graph.link(
                LinkEnd(node=end.device, interface=end.interface, area=end.area, cost=end.cost), LinkEnd(node=subnet.id),
                "data", _ospf_says(end), end.device,
            )
            paired.add((end.device, end.interface))

    # A neighbor no inventory router accounts for: a router outside the inventory.
    for neighbor in heard:
        if (neighbor.device, neighbor.interface) in paired or neighbor.router_id in router_of:
            continue
        graph.node(neighbor.router_id, kind="neighbor", plane="data", router_id=neighbor.router_id,
                   evidence=[f"{neighbor.device} hears router {neighbor.router_id} at {neighbor.address}."])
        graph.link(
            LinkEnd(node=neighbor.device, interface=neighbor.interface, area=neighbor.area, state=neighbor.state),
            LinkEnd(node=neighbor.router_id, address=neighbor.address), "data",
            f"{neighbor.device} hears {neighbor.router_id} on {neighbor.interface}: {neighbor.state}.", neighbor.device,
        )

    nodes, links = graph.sorted(devices)
    return Topology(layer="ospf", nodes=nodes, links=links, errors=errors)
