"""
The views module turns what the model wants to show into a view of the network:
the graph, with every node and link marked as focus, near the focus, or context.

Devices supply the facts (the topology, and the forwarding path read from their
routing tables). The model supplies only editorial choices: which parts of the
graph to focus on and highlight, and short notes. It names graph elements, and
every name is checked against the graph before anything is drawn.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

from netgpt.forwarding import ForwardingPath
from netgpt.primitives import DeviceError
from netgpt.topology import Layer, LinkEnd, PlaneChoice, Topology, TopologyLink, TopologyNode, port_of

Emphasis = Literal["focus", "near", "context"]

# The ways a view may name a link by its two nodes: leaf1--spine1, leaf1 <-> spine1, leaf1 → spine1.
LINK_SEPARATOR = re.compile(r"\s*(?:<-+>|-{2,}>?|->|↔|→|—|–)\s*")


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
    layer: Layer = "l2"
    plane: PlaneChoice
    nodes: list[TopologyNode]
    links: list[TopologyLink]
    emphasis: dict[str, Emphasis] = Field(description="The emphasis of every node and link, by ID.")
    highlight: list[str] = Field(default_factory=list, description="IDs of the highlighted nodes and links.")
    notes: dict[str, str] = Field(default_factory=dict, description="Notes on nodes and links, by ID.")
    path: ForwardingPath | None = Field(None, description="The forwarding path the view shows, if it shows one.")
    trouble: list[str] = Field(default_factory=list, description="IDs of the nodes where the path is lost.")
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


def _routed(graph: Topology, path: ForwardingPath) -> tuple[set[str], set[str]]:
    """
    A path traced through the cabling, drawn on the layer 3 graph: the routers and
    hosts it passes, and the networks it crosses between them. Switches aren't on
    the layer 3 graph, so the path goes straight across them.
    """
    present = {node.id for node in graph.nodes}
    subnets = {node.id for node in graph.nodes if node.kind == "subnet"}
    hops = {hop.node: hop for hop in path.hops}
    touching: dict[str, list[tuple[TopologyLink, LinkEnd, LinkEnd]]] = defaultdict(list)
    for link in graph.links:
        touching[link.a.node].append((link, link.a, link.b))
        touching[link.b.node].append((link, link.b, link.a))

    def onward(node: str, seen: tuple[str, ...] = ()) -> list[str]:
        """The next nodes on the layer 3 graph, looking past switches."""
        found: list[str] = []
        for after in hops[node].next if node in hops else []:
            if after in present:
                found.append(after)
            elif after not in seen:
                found += onward(after, (*seen, after))
        return found

    nodes = present & set(path.nodes)
    links: set[str] = set()
    for hop in path.hops:
        if hop.node not in present:
            continue
        for after in onward(hop.node):
            direct = [link for link, _, there in touching[hop.node] if there.node == after]
            if direct:
                links.add(direct[0].id)
                continue
            # Through a network both are on, the one the packet leaves by if there are several.
            shared = [
                (link, here, there.node)
                for link, here, there in touching[hop.node]
                if there.node in subnets and any(other.node == after for _, _, other in touching[there.node])
            ]
            leaving = [step for step in shared if step[1].interface in hop.interfaces]
            for link, _, subnet in (leaving or shared)[:1]:
                links.add(link.id)
                links |= {other_link.id for other_link, _, other in touching[subnet] if other.node == after}
                nodes |= {subnet, after}
    return nodes, links


def make_view(
    topology: Topology,
    title: str,
    focus: list[str],
    highlight: list[str],
    notes: dict[str, str],
    plane: PlaneChoice = "data",
    path: ForwardingPath | None = None,
) -> View:
    """
    Check a view's names against the graph and work out the emphasis of every
    element. A forwarding path is highlighted exactly, hop by hop, with each hop's
    decision as its note unless the model wrote one.
    """
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
            f"There is nothing on the {plane} plane at {'layer 3' if graph.layer == 'l3' else 'layer 2'} "
            f"called {', '.join(resolver.unknown)}. "
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
    trouble: list[str] = []
    if path is not None:
        # Exactly the links the packet crosses: a link between two hops it doesn't use stays unlit.
        if graph.layer == "l2":
            on_path = {node.id for node in graph.nodes} & set(path.nodes)
            crossed = set(by_id) & set(path.links)
        else:
            on_path, crossed = _routed(graph, path)
        lit_nodes |= on_path
        lit_links |= crossed
        focus_nodes |= on_path
        focus_links |= crossed
        for hop in path.hops:
            if hop.node in on_path:
                noted.setdefault(hop.node, hop.note)
        trouble = [node for node in path.trouble if node in on_path]

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
        layer=graph.layer,
        plane=plane,
        nodes=graph.nodes,
        links=graph.links,
        emphasis=emphasis,
        highlight=sorted(lit_nodes | lit_links),
        notes=noted,
        path=path,
        trouble=trouble,
        errors=graph.errors,
    )


def describe(view: View) -> str:
    """Tell the model what the user now sees, in a few lines."""
    focused = [node.id for node in view.nodes if view.emphasis[node.id] == "focus"]
    aside = sum(1 for node in view.nodes if view.emphasis[node.id] == "context")
    layer = {
        "l2": "layer 2 (cables, switches, and where hosts are plugged in)",
        "l3": "layer 3 (routers and the networks between them)",
    }
    planes = "all planes" if view.plane == "all" else f"the {view.plane} plane"
    lines = [
        f'The user now sees "{view.title}": {planes} at {layer[view.layer]}, {len(view.nodes)} nodes and {len(view.links)} links.'
    ]
    if len(focused) == len(view.nodes):
        lines.append("Nothing is singled out; the whole graph is in focus.")
    else:
        dimmed = f" {aside} other node{'s are' if aside != 1 else ' is'} dimmed and pushed aside." if aside else ""
        lines.append(f"In focus: {', '.join(focused)}.{dimmed}")
    lit_nodes = [node.id for node in view.nodes if node.id in view.highlight]
    lit_links = sum(1 for link in view.links if link.id in view.highlight)
    if lit_nodes or lit_links:
        between = f" and {lit_links} link{'s' if lit_links != 1 else ''}" if lit_links else ""
        lines.append(f"Highlighted: {', '.join(lit_nodes) or 'no nodes'}{between}.")
    if view.path is not None:
        lines.append(f"Forwarding path, {view.path.outcome}: {view.path.summary()}")
    one_sided = [link.id for link in view.links if link.one_sided]
    if one_sided:
        lines.append(f"Only one end reports these links: {', '.join(one_sided)}.")
    if view.errors:
        lines.append(f"Could not query: {', '.join(sorted({error.device for error in view.errors}))}.")
    return "\n".join(lines)
