"""
The topology module merges what devices report about their neighbors into one
graph of the network, and resolves the views the model draws on that graph.

Devices supply the facts. LLDP says which ports are cabled together, and ARP says
which addresses answer behind a port. Every node and link keeps the evidence it
came from, so a drawing can always say why a line is there. The model supplies
only editorial choices: which parts of the graph a view focuses on and which it
highlights. It names graph elements, and every name is checked against the graph
before anything is drawn.
"""

from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Literal

from pydantic import BaseModel, Field

from netgpt.primitives import ArpEntry, DeviceError, DeviceInfo, LldpNeighbor

Plane = Literal["data", "management"]
PlaneChoice = Literal["data", "management", "all"]
Emphasis = Literal["focus", "near", "context"]

# A subinterface (ethernet-1/1.0, GigabitEthernet0/0.100) belongs to the port before the dot.
SUBINTERFACE = re.compile(r"^(?P<port>.+)\.\d+$")

# The ways a view may name a link by its two nodes: leaf1--spine1, leaf1 <-> spine1, leaf1 → spine1.
LINK_SEPARATOR = re.compile(r"\s*(?:<-+>|-{2,}>?|->|↔|→|—|–)\s*")

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


# Views: what the model chooses to show.


class ViewError(ValueError):
    """A view named graph elements that aren't there."""


class View(BaseModel):
    """
    A drawing of the network: the graph on one plane, with what the model chose to
    show. Every node and link has an emphasis: focus (what the view is about), near
    (one hop from the focus), or context (everything else, kept on screen but
    pushed aside). With nothing focused, the whole graph is in focus.
    """

    title: str
    plane: PlaneChoice
    nodes: list[TopologyNode]
    links: list[TopologyLink]
    emphasis: dict[str, Emphasis] = Field(description="The emphasis of every node and link, by ID.")
    highlight: list[str] = Field(default_factory=list, description="IDs of the highlighted nodes and links.")
    notes: dict[str, str] = Field(default_factory=dict, description="Notes the model attached to nodes and links, by ID.")
    errors: list[DeviceError] = Field(default_factory=list)


class _Resolver:
    """Finds the graph elements a view names, and collects the names it can't find."""

    def __init__(self, graph: Topology) -> None:
        self.graph = graph
        self.unknown: list[str] = []
        self.nodes = {node.id.lower(): node.id for node in graph.nodes}
        for node in graph.nodes:
            for address in node.addresses:
                self.nodes.setdefault(address.lower(), node.id)
        self.links = {link.id.lower(): link.id for link in graph.links}

    def node(self, name: str) -> str | None:
        return self.nodes.get(name.strip().lower())

    def end(self, name: str) -> tuple[str, str | None] | None:
        """A node, or one end of a link written node:interface, as (node, port)."""
        if (node := self.node(name)) is not None:
            return node, None
        node_name, colon, interface = name.strip().partition(":")
        if colon and (node := self.node(node_name)) is not None:
            return node, port_of(interface.strip()).lower()
        return None

    @staticmethod
    def touches(end: LinkEnd, wanted: tuple[str, str | None]) -> bool:
        node, port = wanted
        return end.node == node and (port is None or (end.interface is not None and port_of(end.interface).lower() == port))

    def find(self, name: str) -> tuple[set[str], set[str]]:
        """The nodes and links one name refers to; records the name as unknown if nothing."""
        if (node := self.node(name)) is not None:
            return {node}, set()
        if (link := self.links.get(name.strip().lower())) is not None:
            return set(), {link}
        parts = LINK_SEPARATOR.split(name.strip())
        if len(parts) == 2 and None not in (ends := [self.end(part) for part in parts]):
            # Either end may come first, and each may be a node or a node:interface.
            first, second = ends
            between = {
                link.id
                for link in self.graph.links
                if (self.touches(link.a, first) and self.touches(link.b, second))
                or (self.touches(link.b, first) and self.touches(link.a, second))
            }
            if between:
                return set(), between
            self.unknown.append(f"{name!r} (no link between {first[0]} and {second[0]})")
            return set(), set()
        if (port := self.end(name)) is not None and port[1] is not None:
            at = {link.id for link in self.graph.links if self.touches(link.a, port) or self.touches(link.b, port)}
            if at:
                return set(), at
        self.unknown.append(repr(name))
        return set(), set()

    def find_all(self, names: Iterable[str]) -> tuple[set[str], set[str]]:
        nodes: set[str] = set()
        links: set[str] = set()
        for name in names:
            found_nodes, found_links = self.find(name)
            nodes |= found_nodes
            links |= found_links
        return nodes, links


def make_view(
    topology: Topology,
    title: str,
    focus: list[str],
    highlight: list[str],
    notes: dict[str, str],
    plane: PlaneChoice = "data",
) -> View:
    """Check a view's names against the graph and work out the emphasis of every element."""
    graph = topology.on_plane(plane)
    resolver = _Resolver(graph)
    focus_nodes, focus_links = resolver.find_all(focus)
    lit_nodes, lit_links = resolver.find_all(highlight)
    noted: dict[str, str] = {}
    for name, note in notes.items():
        found_nodes, found_links = resolver.find(name)
        for element in found_nodes | found_links:
            noted[element] = note
    if resolver.unknown:
        named = ", ".join(node.id for node in graph.nodes)
        raise ViewError(
            f"There is nothing on the {plane} plane called {', '.join(resolver.unknown)}. "
            f"Its nodes are: {named}. Name a link by its two nodes (a--b) or by one end (device:interface)."
        )

    by_id = {link.id: link for link in graph.links}

    def ends(links: set[str]) -> set[str]:
        return {end.node for link_id in links for end in (by_id[link_id].a, by_id[link_id].b)}

    def between(nodes: set[str]) -> set[str]:
        return {link.id for link in graph.links if link.a.node in nodes and link.b.node in nodes}

    # A link's ends are part of what it names, and a link between two named nodes is named too.
    lit_nodes |= ends(lit_links)
    lit_links |= between(lit_nodes)
    focus_nodes |= lit_nodes | ends(focus_links)
    focus_links |= lit_links | between(focus_nodes)

    emphasis: dict[str, Emphasis] = {}
    if not focus_nodes:
        emphasis = {element: "focus" for element in [node.id for node in graph.nodes] + list(by_id)}
    else:
        touching = {link.id for link in graph.links if link.a.node in focus_nodes or link.b.node in focus_nodes}
        near_nodes = ends(touching) - focus_nodes
        for node in graph.nodes:
            emphasis[node.id] = "focus" if node.id in focus_nodes else "near" if node.id in near_nodes else "context"
        for link in graph.links:
            emphasis[link.id] = "focus" if link.id in focus_links else "near" if link.id in touching else "context"

    return View(
        title=title.strip() or "The network",
        plane=plane,
        nodes=graph.nodes,
        links=graph.links,
        emphasis=emphasis,
        highlight=sorted(lit_nodes | lit_links),
        notes=noted,
        errors=graph.errors,
    )


def describe(view: View) -> str:
    """Tell the model what the user now sees, in a few lines."""
    focused = [node.id for node in view.nodes if view.emphasis[node.id] == "focus"]
    aside = sum(1 for node in view.nodes if view.emphasis[node.id] == "context")
    lines = [f'The user now sees "{view.title}": the {view.plane} plane, {len(view.nodes)} nodes and {len(view.links)} links.']
    if len(focused) == len(view.nodes):
        lines.append("Nothing is singled out; the whole graph is in focus.")
    else:
        lines.append(f"In focus: {', '.join(focused)}. {aside} other nodes are dimmed and pushed aside.")
    lit_nodes = [node.id for node in view.nodes if node.id in view.highlight]
    lit_links = sum(1 for link in view.links if link.id in view.highlight)
    if lit_nodes or lit_links:
        between = f" and {lit_links} link{'s' if lit_links != 1 else ''}" if lit_links else ""
        lines.append(f"Highlighted: {', '.join(lit_nodes) or 'no nodes'}{between}.")
    one_sided = [link.id for link in view.links if link.one_sided]
    if one_sided:
        lines.append(f"Only one end reports these links: {', '.join(one_sided)}.")
    if view.errors:
        lines.append(f"Could not query: {', '.join(sorted({error.device for error in view.errors}))}.")
    return "\n".join(lines)
