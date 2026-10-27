"""
The topology module merges what devices report about their neighbors into one
graph of the network.

LLDP says which ports are cabled together, and ARP says which addresses answer
behind a port. Every node and link keeps the evidence it came from, so a drawing
can always say why a line is there.
"""

from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from netgpt.primitives import ArpEntry, DeviceError, DeviceInfo, LldpNeighbor

Plane = Literal["data", "management"]
PlaneChoice = Literal["data", "management", "all"]

# A subinterface (ethernet-1/1.0, GigabitEthernet0/0.100) belongs to the port before the dot.
SUBINTERFACE = re.compile(r"^(?P<port>.+)\.\d+$")

# Is this interface of this inventory device a management port?
ManagementCheck = Callable[[str, str], bool]

Endpoint = tuple[str, str | None]


def port_of(interface: str) -> str:
    """The physical port an interface or subinterface belongs to."""
    match = SUBINTERFACE.match(interface)
    return match.group("port") if match else interface


class TopologyNode(BaseModel):
    """A device, an LLDP neighbor outside the inventory, a host found by ARP, or a shared segment."""

    id: str = Field(description="How to name the node: a device or system name, a host's IPv4 address, or a segment ID.")
    kind: Literal["device", "neighbor", "host", "segment"]
    label: str
    plane: Plane | None = Field(
        None, description="The plane a host, neighbor, or segment sits on. None for inventory devices, which are on both."
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

    def __str__(self) -> str:
        return f"{self.node}:{self.interface}" if self.interface else self.node


class TopologyLink(BaseModel):
    """A cable seen by LLDP, or an attachment seen by ARP, between two nodes."""

    id: str
    a: LinkEnd
    b: LinkEnd
    plane: Plane
    one_sided: bool = Field(False, description="Only one end reported this link, though both ends were asked.")
    evidence: list[str] = Field(default_factory=list)


class Topology(BaseModel):
    """The network as a graph. Devices that could not be queried are listed in errors."""

    nodes: list[TopologyNode] = Field(default_factory=list)
    links: list[TopologyLink] = Field(default_factory=list)
    errors: list[DeviceError] = Field(default_factory=list)

    def on_plane(self, plane: PlaneChoice) -> Topology:
        """The part of the graph on one plane. Inventory devices are on every plane."""
        if plane == "all":
            return self
        return Topology(
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

    def link(self, a: LinkEnd, b: LinkEnd, plane: Plane, evidence: str, reporter: str) -> TopologyLink:
        a, b = sorted((a, b), key=str)
        link_id = f"{a}--{b}"
        if link_id not in self.links:
            self.links[link_id] = TopologyLink(id=link_id, a=a, b=b, plane=plane)
        link = self.links[link_id]
        if evidence not in link.evidence:
            link.evidence.append(evidence)
        self.reporters[link_id].add(reporter)
        return link


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
    try:
        return tuple(ipaddress.IPv4Address(node.id).packed)
    except ValueError:
        return ()


def build_topology(
    devices: list[DeviceInfo],
    neighbors: list[LldpNeighbor],
    arp: list[ArpEntry],
    errors: list[DeviceError],
    is_management: ManagementCheck,
) -> Topology:
    """Merge every device's LLDP and ARP records into one graph of the network."""
    graph = _Graph()
    inventory = {device.name: device for device in devices}
    failed: dict[str, list[str]] = defaultdict(list)
    for error in errors:
        failed[error.device].append(error.error)
    for device in devices:
        graph.node(
            device.name,
            kind="device",
            role=device.role,
            platform=device.platform,
            addresses=[device.host],
            error="; ".join(dict.fromkeys(failed[device.name])) or None,
            evidence=[f"In the inventory, managed at {device.host}."],
        )

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

    managed_at = {device.host: device.name for device in devices}
    for entry in arp:
        endpoint: Endpoint = (entry.device, port_of(entry.interface))
        plane = "management" if management(endpoint) else "data"
        evidence = f"ARP on {entry.device} {entry.interface}: {entry.ip_address} is at {entry.mac or 'an unknown MAC'}"
        if entry.ip_address in managed_at:
            continue  # An inventory device's own management address; nothing new.
        heard = seen.get(endpoint, [])
        segment = segment_of.get(endpoint)
        if segment is None and len(heard) == 1:
            # A point-to-point port: the address belongs to the neighbor at the other end.
            owner = graph.nodes[_neighbor_id(heard[0])]
            if entry.ip_address not in owner.addresses:
                owner.addresses.append(entry.ip_address)
            owner.evidence.append(f"{evidence}, the LLDP neighbor on that port.")
            continue
        host = graph.node(entry.ip_address, kind="host", plane=plane, addresses=[entry.ip_address])
        if plane == "data":
            host.plane = "data"  # Seen on the data plane anywhere puts a host on it.
        host.evidence.append(f"{evidence}.")
        attach = LinkEnd(node=segment) if segment else LinkEnd(node=entry.device, interface=endpoint[1])
        graph.link(attach, LinkEnd(node=entry.ip_address), plane, f"{evidence}.", entry.device)

    # Both ends of a cable between two answering devices should report it.
    answered = {device for device in inventory if not failed[device]}
    for link in graph.links.values():
        ends = {link.a.node, link.b.node}
        link.one_sided = ends <= answered and len(ends) == 2 and len(graph.reporters[link.id]) == 1

    order = {"device": 0, "neighbor": 1, "host": 2, "segment": 3}
    position = {device.name: index for index, device in enumerate(devices)}
    nodes = sorted(
        graph.nodes.values(),
        key=lambda node: (order[node.kind], position.get(node.id, 0), _address_key(node), node.id),
    )
    links = sorted(graph.links.values(), key=lambda link: link.id)
    return Topology(nodes=nodes, links=links, errors=errors)

