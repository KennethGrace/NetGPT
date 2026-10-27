"""
The forwarding module works out the path packets take through the network by
following each device's routing table, the way the devices themselves would.

At each device the walk takes the longest route matching the destination,
resolves the route's next hop down to an interface (a static route's next hop is
often just an address, reached through another route), and follows the cable on
that interface to the next node, through any switches in the way (their MAC
tables placed every host on the port it is behind, so the walk knows which way
each one bridges). The walk ends where the packet is delivered, or
where it would be lost: no route, a route that drops it, a loop, a next hop
nothing answers for, or a device NetGPT can't ask. Nothing is sent on the
network; the path is read from the devices' state.
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict, deque
from ipaddress import IPv4Address
from typing import Literal

from pydantic import BaseModel, Field

from netgpt.primitives import DeviceError, NextHop, Route
from netgpt.topology import LinkEnd, Topology, TopologyLink, TopologyNode, port_of

MAX_HOPS = 32
MAX_RECURSION = 8

Outcome = Literal["delivered", "no_route", "dropped", "loop", "unresolved", "left_inventory", "unreadable"]

# The way to a node across a link, as (link, node reached) steps.
Steps = list[tuple[str, str]]


class TraceError(ValueError):
    """The source or destination of a path can't be found."""


class Hop(BaseModel):
    """One node on a forwarding path, and what it does with the packet."""

    node: str = Field(description="The node's ID in the topology.")
    route: Route | None = Field(None, description="The route that matched on this device. None for hosts and switches.")
    interfaces: list[str] = Field(default_factory=list, description="The interfaces the packet leaves by.")
    next: list[str] = Field(default_factory=list, description="The next nodes; more than one for equal-cost paths.")
    note: str = Field(description="What happens here, in a few words.")


class ForwardingPath(BaseModel):
    """The path packets take from a source to a destination, read from each device's routing table."""

    source: str
    destination: str
    network_instance: str
    outcome: Outcome
    detail: str = Field(description="What happened, in a sentence or two.")
    hops: list[Hop] = Field(default_factory=list)
    nodes: list[str] = Field(default_factory=list, description="Every node on the path, shared segments included, in order.")
    links: list[str] = Field(default_factory=list, description="The topology links the path uses.")
    trouble: list[str] = Field(default_factory=list, description="The nodes where the path, or a branch of it, is lost.")
    errors: list[DeviceError] = Field(default_factory=list)

    def summary(self) -> str:
        """The path hop by hop, in one line."""
        steps = []
        for hop in self.hops:
            if hop.next:
                if hop.route is not None:
                    reason = f" ({hop.route.protocol} {hop.route.prefix})"
                else:  # a switch says which port it bridges out of; a source host says nothing
                    reason = f" (bridges out {', '.join(hop.interfaces)})" if hop.interfaces else ""
                steps.append(f"{hop.node}{reason} → {', '.join(hop.next)}")
            elif hop.route is not None or hop.node in self.trouble:
                steps.append(f"{hop.node} ({hop.note})")
        return f"{'; '.join(steps)}. {self.detail}"


def longest_match(routes: list[Route], address: IPv4Address) -> Route | None:
    """The active route with the longest prefix that contains the address."""
    best: Route | None = None
    best_length = -1
    for route in routes:
        if route.active is False:
            continue
        try:
            network = ipaddress.IPv4Network(route.prefix, strict=False)
        except ValueError:
            continue
        if address in network and network.prefixlen > best_length:
            best, best_length = route, network.prefixlen
    return best


def is_connected(route: Route | None) -> bool:
    """Whether a route is a directly connected network: an interface and no next router."""
    return route is not None and any(hop.interface and not hop.address for hop in route.next_hops)


def adjacencies(routes: list[Route], hop: NextHop, depth: int = 0) -> list[tuple[str | None, str]]:
    """
    Resolve a next hop to the neighbors it reaches, as (neighbor address, interface)
    pairs, following recursive routes the way a router resolves them. A neighbor
    address of None means the destination itself is on that interface's network.
    """
    if hop.action != "forward":
        return []
    if hop.interface:
        return [(hop.address, hop.interface)]
    if hop.address is None or depth >= MAX_RECURSION:
        return []
    try:
        route = longest_match(routes, IPv4Address(hop.address))
    except ValueError:
        return []
    if route is None:
        return []
    # Through a connected route, the next hop's address is itself the neighbor.
    return [
        (neighbor or hop.address, interface)
        for inner in route.next_hops
        for neighbor, interface in adjacencies(routes, inner, depth + 1)
    ]


class _Walk:
    """One trace through the graph, following routes from device to device."""

    def __init__(
        self,
        topology: Topology,
        routes: dict[str, list[Route]],
        unreadable: dict[str, str],
        destination: IPv4Address,
    ) -> None:
        self.topology = topology
        self.routes = routes
        self.unreadable = unreadable
        self.destination = destination
        self.kinds = {node.id: node.kind for node in topology.nodes}
        self.ends: dict[str, list[tuple[TopologyLink, LinkEnd, LinkEnd]]] = defaultdict(list)
        for link in topology.links:
            self.ends[link.a.node].append((link, link.a, link.b))
            self.ends[link.b.node].append((link, link.b, link.a))
        self.owner: dict[str, str] = {}
        for node in topology.nodes:
            for address in node.addresses:
                self.owner.setdefault(address, node.id)
        self.hops: dict[str, Hop] = {}
        self.nodes: list[str] = []
        self.links: list[str] = []
        self.trouble: list[str] = []
        self.endings: list[tuple[Outcome, str]] = []

    def on_path(self, *nodes: str, steps: Steps = ()) -> None:
        """Add nodes to the path, and the links (and nodes) of steps taken."""
        for link, node in steps:
            if link not in self.links:
                self.links.append(link)
            nodes += (node,)
        for node in nodes:
            if node not in self.nodes:
                self.nodes.append(node)

    def beyond(self, link: TopologyLink, far: LinkEnd, origin: str) -> dict[str, Steps]:
        """
        Where a frame sent across a link can get to: the node at the far end and,
        where that end is a shared segment or a switch's bridged port, everything
        beyond it in the same layer 2 network. Each node comes with the steps that
        reach it.
        """
        found: dict[str, Steps] = {far.node: [(link.id, far.node)]}
        queue = deque([far])
        while queue:
            entered = queue.popleft()
            if self.kinds.get(entered.node) == "segment":
                bridges = None
            elif entered.bridges:
                bridges = set(entered.bridges)
            else:
                continue  # A routed port, a host, or a node that doesn't say: the frame stops here.
            for onward, here, there in self.ends[entered.node]:
                if there.node in found or there.node == origin:
                    continue
                if bridges is not None and not bridges & set(here.bridges):
                    continue
                found[there.node] = [*found[entered.node], (onward.id, there.node)]
                queue.append(there)
        return found

    def neighbors(self, node: str) -> dict[str, Steps]:
        """Everything a node reaches at layer 2: what it is linked to, and what is beyond any switch or segment."""
        found: dict[str, Steps] = {}
        for link, _, there in self.ends[node]:
            for reached, steps in self.beyond(link, there, node).items():
                found.setdefault(reached, steps)
        return found

    def bridged(self, sender: str, steps: Steps) -> str | None:
        """
        Put the switches a frame crosses on the path as hops of their own, and
        return the first node after the sender (segments are passed through).
        """
        through = [(link, node) for link, node in steps if self.kinds.get(node) != "segment"]
        for (_, switch), (onward, after) in zip(through, through[1:]):
            out = next((here.interface for link, here, _ in self.ends[switch] if link.id == onward), None)
            hop = self.hop(switch, f"bridges it out {out}" if out else "bridges it on")
            if out and out not in hop.interfaces:
                hop.interfaces.append(out)
            if after not in hop.next:
                hop.next.append(after)
        return through[0][1] if through else None

    def hop(self, node: str, note: str, route: Route | None = None) -> Hop:
        if node not in self.hops:
            self.hops[node] = Hop(node=node, route=route, note=note)
        self.on_path(node)
        return self.hops[node]

    def end(self, outcome: Outcome, node: str, detail: str) -> None:
        if (outcome, detail) not in self.endings:
            self.endings.append((outcome, detail))
        if outcome != "delivered" and node not in self.trouble:
            self.trouble.append(node)

    def across(self, device: str, interface: str, address: str, connected: bool) -> tuple[str | None, Steps]:
        """
        The node out of a device's interface that the packet goes to, and the steps
        that reach it. Through a shared segment or a switch, the address picks the
        node. For a connected network the destination must actually be there; across
        a cable straight to a next router, the cable decides.
        """
        port = port_of(interface)
        cabled = [(link, there) for link, here, there in self.ends[device] if here.interface and port_of(here.interface) == port]
        candidates: dict[str, Steps] = {}
        for link, there in cabled:
            for reached, steps in self.beyond(link, there, device).items():
                candidates.setdefault(reached, steps)
        owner = self.owner.get(address)
        if owner in candidates:
            return owner, candidates[owner]
        if connected:
            return None, []
        if len(cabled) == 1 and self.kinds.get(cabled[0][1].node) != "segment" and not cabled[0][1].bridges:
            link, there = cabled[0]
            return there.node, [(link.id, there.node)]
        # No cable seen on that port: the address's owner, if anything answers for it.
        return (owner, []) if not cabled else (None, [])

    def walk(self, device: str, came_from: tuple[str, ...]) -> None:
        if device in came_from:
            self.end("loop", device, f"The packet comes back to {device}: a forwarding loop.")
            return
        if len(came_from) >= MAX_HOPS:
            self.end("loop", device, f"The path is longer than {MAX_HOPS} hops.")
            return
        if device in self.unreadable:
            self.hop(device, "routes unreadable")
            self.end("unreadable", device, f"Couldn't read {device}'s routing table: {self.unreadable[device]}")
            return
        route = longest_match(self.routes.get(device, []), self.destination)
        if route is None:
            self.hop(device, f"no route to {self.destination}")
            self.end("no_route", device, f"{device} has no route to {self.destination}.")
            return
        match = f"{route.protocol} {route.prefix}"
        if any(next_hop.action == "receive" for next_hop in route.next_hops):
            self.hop(device, f"{match}: its own address", route)
            self.end("delivered", device, f"Delivered to {device}, which owns {self.destination}.")
            return
        pairs = list(dict.fromkeys(pair for next_hop in route.next_hops for pair in adjacencies(self.routes[device], next_hop)))
        if not pairs:
            dropped = any(next_hop.action == "drop" for next_hop in route.next_hops)
            self.hop(device, f"{match}: dropped" if dropped else f"{match}: no usable next hop", route)
            outcome: Outcome = "dropped" if dropped else "unresolved"
            self.end(outcome, device, f"{device} matches {match}, which {'drops the packet' if dropped else 'has no usable next hop'}.")
            return
        hop = self.hop(device, match, route)
        for neighbor, interface in pairs:
            if interface not in hop.interfaces:
                hop.interfaces.append(interface)
            connected = neighbor is None
            node, steps = self.across(device, interface, neighbor or str(self.destination), connected)
            if node is None:
                if connected:
                    detail = f"{self.destination} is on {device}'s network on {interface}, but nothing there answers ARP for it."
                else:
                    detail = f"{device} sends to {neighbor} on {interface}, but NetGPT can't tell which node that is."
                self.end("unresolved", device, detail)
                continue
            first = self.bridged(device, steps) or node
            if first not in hop.next:
                hop.next.append(first)
            self.on_path(steps=steps)
            if connected:
                self.hop(node, "destination" if self.kinds.get(node) != "device" else f"owns {self.destination}")
                self.end("delivered", node, f"Delivered to {node}.")
            elif self.kinds.get(node) == "device":
                self.walk(node, came_from + (device,))
            else:
                self.hop(node, "outside the inventory")
                self.end("left_inventory", node, f"{device} hands the packet to {node}, which NetGPT can't ask about its routes.")


def _same_network(
    walk: _Walk,
    attached: dict[str, Steps],
    routes: dict[str, list[Route]],
    connected: list[str],
    address: IPv4Address | None,
) -> str | None:
    """
    The network a source and destination share, when the destination is within reach
    at layer 2 and on the same network as the source, as a router on it sees it.
    """
    if address is None or walk.owner.get(str(walk.destination)) not in attached:
        return None
    for router in connected:
        route = longest_match(routes.get(router, []), address)
        if route is not None and walk.destination in ipaddress.IPv4Network(route.prefix, strict=False):
            return route.prefix
    return None


def _find(topology: Topology, name: str) -> TopologyNode | None:
    wanted = name.strip().lower()
    for node in topology.nodes:
        if node.id.lower() == wanted or wanted in (address.lower() for address in node.addresses):
            return node
    return None


def _destination(topology: Topology, name: str) -> IPv4Address:
    try:
        return IPv4Address(name.strip())
    except ValueError:
        pass
    node = _find(topology, name)
    if node is None:
        raise TraceError(f"The destination '{name}' is not an IPv4 address or a node NetGPT knows.")
    if node.kind == "host":
        return IPv4Address(node.id)
    raise TraceError(f"Name one of {node.id}'s addresses as the destination: {', '.join(node.addresses) or 'none known'}.")


def trace_path(
    topology: Topology,
    routes: dict[str, list[Route]],
    unreadable: dict[str, str],
    source: str,
    destination: str,
    network_instance: str = "default",
) -> ForwardingPath:
    """
    Follow the routing tables from a source toward a destination. The source is a
    device, a node's address, or any address on a network a device is connected to;
    a host's first hop is the device it is attached to.
    """
    target = _destination(topology, destination)
    walk = _Walk(topology, routes, unreadable, target)
    devices = [node.id for node in topology.nodes if node.kind == "device"]
    # A device with no routing table here only switches: it is never where a host's packets enter.
    routers = [device for device in devices if routes.get(device) or device in unreadable]
    start = _find(topology, source)
    if start is not None and start.kind == "device":
        gateways = [start.id]
    else:
        try:
            address = IPv4Address(source.strip())
        except ValueError:
            address = None
        if start is None and address is None:
            raise TraceError(
                f"The source '{source}' is not an address or a node NetGPT knows. "
                f"Nodes: {', '.join(node.id for node in topology.nodes)}."
            )
        # A host sends through a router on its own network: one it reaches at layer 2.
        connected = [d for d in routers if address and is_connected(longest_match(routes.get(d, []), address))]
        if start is not None:
            attached = walk.neighbors(start.id)
            sending = walk.hop(start.id, "source")
            if (local := _same_network(walk, attached, routes, connected, address)) is not None:
                # The destination is on the host's own network: the host reaches it directly, no router.
                steps = attached[walk.owner[str(target)]]
                sending.next.append(walk.bridged(start.id, steps) or steps[-1][1])
                walk.on_path(steps=steps)
                walk.hop(steps[-1][1], "destination")
                switches = [node for _, node in steps[:-1] if walk.kinds.get(node) == "device"]
                through = f", bridged by {', '.join(switches)}" if switches else ""
                walk.end("delivered", steps[-1][1], f"{target} is on {source}'s own network, {local}: reached directly{through}.")
                gateways = []
            else:
                gateways = [d for d in attached if d in routers and (d in connected or not connected)]
                for gateway in gateways:
                    first = walk.bridged(start.id, attached[gateway]) or gateway
                    if first not in sending.next:
                        sending.next.append(first)
                    walk.on_path(steps=attached[gateway])
                if not gateways:
                    raise TraceError(f"Can't tell where {source} enters the network: no device is connected to its network.")
        else:
            gateways = connected
            if not gateways:
                raise TraceError(f"Can't tell where {source} enters the network: no device is connected to its network.")

    for gateway in gateways:
        walk.walk(gateway, ())

    failures = [(outcome, detail) for outcome, detail in walk.endings if outcome != "delivered"]
    outcome: Outcome = failures[0][0] if failures else "delivered"
    detail = " ".join(dict.fromkeys(detail for _, detail in (failures or walk.endings)))
    if start is None:
        detail = f"{source} isn't on the map; its network is connected to {', '.join(gateways)}. {detail}"
    return ForwardingPath(
        source=source,
        destination=str(target),
        network_instance=network_instance,
        outcome=outcome,
        detail=detail,
        hops=list(walk.hops.values()),
        nodes=walk.nodes,
        links=walk.links,
        trouble=walk.trouble,
        errors=[DeviceError(device=device, error=error) for device, error in unreadable.items()],
    )
