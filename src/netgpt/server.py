"""
The NetGPT MCP server is the engine's face for language model clients: it offers
what the engine reads and runs as read-only tools.

Every tool is read-only and reaches only the devices and networks named in the
inventory. Multi-device tools query devices concurrently and return a
Collection, so one unreachable device doesn't sink the whole answer. The engine
(netgpt.engine) does the work; this module says what each tool is for, accepts the
shapes small models send, and gives the model text to read. Only anticipated
failures (bad input, refused commands, device errors) are caught and reported.
Anything else is a bug and is allowed to fail loudly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, BeforeValidator, Field

from netgpt import __version__
from netgpt.engine import Engine, RequestError
from netgpt.forwarding import ForwardingPath, TraceError
from netgpt.guard import GuardError
from netgpt.inventory import Inventory, InventoryError
from netgpt.platforms import PlatformError
from netgpt.primitives import (
    ArpEntry,
    BgpSession,
    Collection,
    CommandOutput,
    DeviceInfo,
    Interface,
    InterfaceTraffic,
    LldpNeighbor,
    MacEntry,
    OspfInterfaces,
    OspfNeighbor,
    PingResult,
    Route,
)
from netgpt.statistics import (
    DEFAULT_INTERVAL, MAX_CHART_PORTS, Chart, ChartError, Metric, Poller, describe_chart, duration, poll_by_poll,
)
from netgpt.store import STORE_HELP, StoreError, check_store, open_store
from netgpt.topology import Layer, PlaneChoice, Topology
from netgpt.views import View, ViewError, describe

logger = logging.getLogger("netgpt")

INSTRUCTIONS = """\
NetGPT gives read-only access to the network devices in its inventory.
Call list_devices first: every other tool takes device names from that list and cannot reach anything else.
lldp_neighbors, interfaces, arp_table, mac_table, routes, bgp_sessions, ospf_interfaces, and ospf_neighbors
return typed records from every device in one call, with per-device errors alongside the results. Ask for
every device you need in one call, rather than one device at a time. topology merges them into one graph:
at layer 2 (the default) the cables, switches, and the port each host is plugged into; at layer 3 the
routers and the networks between them; as ospf, the OSPF adjacencies, areas, and costs, and what keeps a
missing adjacency from forming. forwarding_path follows the routing tables hop by hop to show where packets
from a source to a destination go, and where they are lost. Use show for anything the typed tools don't
cover; it accepts only read-only commands and says why when it refuses one.
"""

STATISTICS_INSTRUCTIONS = """\
interface_traffic reads the interface counters NetGPT polls every {interval:g} seconds: how much each port
sends and receives, now, on average, and at its busiest, how busy it is as a share of its speed, and the
errors, discards, and link flaps counted, over a window that reaches back as far as {reach}.
"""

DISPLAY_INSTRUCTIONS = """\
The user sees a map of the network beside the chat. Call display whenever a picture helps: the
topology, where a host is attached, the path between two points, where a problem is. Focus on the
part of the network the answer is about and highlight the answer itself; the rest of the network
stays on the map, dimmed and pushed aside. For the way traffic goes between two points, give display a
path (source and destination) and the server traces it through the routing tables. layer 2 (the
default) draws the cabling: switches, and which port each host is plugged into. layer 3 draws the
routers and the networks between them, without the switches. Refer to the map in your answer rather
than describing every link in words.
"""

CHART_INSTRUCTIONS = """\
Call chart to draw ports' traffic, utilization, or errors over time in the conversation. The chart keeps
updating as NetGPT polls, so use it whenever the answer is about how busy something is, or how its errors
change, and refer to it rather than listing numbers. The map shows traffic too: beside each link, a lane
for each direction, colored by its share of the port's speed and labeled with its rate, and a warning sign
with a count on any port that counted errors in the last 5 minutes. Selecting a link or device charts it.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

# What the engine refuses, and a device's failure, which the model reads as the tool's error. Anything else is a bug,
# and propagates as a crash.
ANTICIPATED = (InventoryError, GuardError, PlatformError, RequestError, TraceError, ViewError, ChartError, StoreError)


@contextlib.contextmanager
def answered() -> Iterator[None]:
    """Turn what the engine anticipates going wrong into the error the model reads."""
    try:
        yield
    except ANTICIPATED as error:
        raise ToolError(str(error)) from error


def as_names(value: object) -> object:
    """
    Small models often pass one name where the schema asks for a list, and now and
    then an object keyed by the names. Accept both rather than spend a round trip
    on a validation error; the schema stays a list.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return list(value)
    return value


Names = Annotated[list[str] | None, BeforeValidator(as_names)]
# Small models ask one device at a time, a reply each, unless told plainly that one call covers them all.
DeviceNames = Annotated[
    Names, Field(description="The devices to query, from list_devices, all in this one call. Leave it out to query every device.")
]


Minutes = Annotated[float, Field(description="How far back to look, in minutes: 60 is an hour, 1440 a day, 10080 a week.")]


ChartPorts = Annotated[
    list[str],
    BeforeValidator(as_names),
    Field(description="Ports as device:port (leaf1:ethernet-1/49), links as device--device, or devices for all their ports."),
]


class PathEnds(BaseModel):
    """The two ends of a path to trace."""

    source: str
    destination: str


def as_path_ends(value: object) -> object:
    """Accept [source, destination] as well as the object."""
    if isinstance(value, list | tuple) and len(value) == 2:
        return {"source": value[0], "destination": value[1]}
    return value


def build_server(engine: Engine, display: bool = False) -> MCPServer:
    """
    Build the MCP server on an engine. display adds the tool that draws the network;
    only offer it to a client that can show the drawing. If the engine polls, the
    server keeps it polling while a client is connected, and offers interface_traffic
    to read the counters.
    """
    statistics = engine.statistics
    instructions = (
        INSTRUCTIONS
        + (STATISTICS_INSTRUCTIONS.format(interval=statistics.interval, reach=duration(statistics.reach)) if statistics else "")
        + (DISPLAY_INSTRUCTIONS if display else "")
        + (CHART_INSTRUCTIONS if display and statistics else "")
    )

    @contextlib.asynccontextmanager
    async def polling(_: MCPServer) -> AsyncIterator[dict[str, Any]]:
        async with engine.running():
            yield {}

    server = MCPServer(name="netgpt", version=__version__, instructions=instructions, lifespan=polling)

    @server.tool(annotations=READ_ONLY)
    def list_devices() -> list[DeviceInfo]:
        """List the devices NetGPT can query, with their platform, address, and role."""
        return engine.devices()

    @server.tool(annotations=READ_ONLY)
    async def show(device: str, command: str) -> CommandOutput:
        """
        Run one read-only CLI command on a device and return its text output.
        The command is in the device's own CLI, which may not be Cisco's:
        list_devices gives each device's platform. Where a typed tool covers the
        question, use it instead. Commands that could change the device, write
        files, or chain several commands together are refused, with the reason.
        """
        with answered():
            return await engine.show(device, command)

    @server.tool(annotations=READ_ONLY)
    async def lldp_neighbors(devices: DeviceNames = None) -> Collection[LldpNeighbor]:
        """
        Return every device's LLDP adjacencies: which interface is cabled to which remote system and port.
        One call covers every device; name devices only to leave the others out.
        """
        return await engine.lldp_neighbors(devices)

    @server.tool(annotations=READ_ONLY)
    async def interfaces(devices: DeviceNames = None) -> Collection[Interface]:
        """
        Return each device's logical interfaces: the network instance (VRF or bridge) each is in,
        whether it routes or bridges, its IPv4 and IPv6 addresses, and whether it is up. network_instance is null for
        a subinterface in no network instance: nothing passes through it, whether it is up or not.
        One call covers every device; name devices only to leave the others out.
        """
        return await engine.interfaces(devices)

    @server.tool(annotations=READ_ONLY)
    async def mac_table(devices: DeviceNames = None) -> Collection[MacEntry]:
        """
        Return every device's layer 2 forwarding (MAC table) entries, per network instance.
        One call covers every device; name devices only to leave the others out.
        """
        return await engine.mac_table(devices)

    @server.tool(annotations=READ_ONLY)
    async def arp_table(devices: DeviceNames = None) -> Collection[ArpEntry]:
        """
        Return every device's ARP bindings of IPv4 addresses to MAC addresses.
        One call covers every device; name devices only to leave the others out.
        """
        return await engine.arp_table(devices)

    @server.tool(annotations=READ_ONLY)
    async def routes(devices: DeviceNames = None, network_instance: str = "default") -> Collection[Route]:
        """
        Return every device's routes in one network instance (VRF): IPv4, and IPv6 where the platform reads it.
        One call covers every device; name devices only to leave the others out.
        """
        return await engine.routes(devices, network_instance)

    @server.tool(annotations=READ_ONLY)
    async def bgp_sessions(devices: DeviceNames = None) -> Collection[BgpSession]:
        """
        Return every device's BGP sessions, in every network instance: the peer's address and AS, the local
        address and AS the session uses, its state, the last error, the address families it carries, and
        how many prefixes it received. Only an Established session exchanges routes; Active means it is
        trying to connect and failing. One call covers every device; name devices only to leave the others out.
        """
        return await engine.bgp_sessions(devices)

    @server.tool(annotations=READ_ONLY)
    async def ospf_neighbors(devices: DeviceNames = None) -> Collection[OspfNeighbor]:
        """
        Return every device's OSPF neighbors: the interface and area each is heard on, its router ID and
        address, and the adjacency's state. Only a full adjacency exchanges routes. A neighbor that should be
        there and isn't sends no hellos this router accepts: compare both ends with ospf_interfaces.
        One call covers every device; name devices only to leave the others out.
        """
        return await engine.ospf_neighbors(devices)

    @server.tool(annotations=READ_ONLY)
    async def ospf_interfaces(devices: DeviceNames = None) -> OspfInterfaces:
        """
        Return the interfaces each device runs OSPF on: the area and its type (normal, stub, or nssa), network
        type, cost, whether it is passive, its authentication (none, or the algorithm, such as md5, and the
        keychain), hello and dead intervals, MTU, the DR and BDR of a broadcast network, and how many neighbors
        it hears. Two routers on a link become neighbors only when their areas, area types, network types,
        authentication, and timers match and neither end is passive; mismatched MTUs stop them in exstart.
        mismatches names, for each network two of the devices share, the settings their ends disagree on, and
        how the other OSPF interfaces are set, which says which end is set unlike the rest.
        Authentication keys are never read, so two ends with the same algorithm can still disagree on the key.
        Each end's cost is its own direction's.
        One call covers every device; name devices only to leave the others out.
        """
        return await engine.ospf_interfaces(devices)

    if statistics is not None:

        @server.tool(
            annotations=READ_ONLY,
            description=(
                "Return each port's traffic and errors over the last minutes, from the interface counters NetGPT polls "
                f"every {statistics.interval:g} seconds: megabits per second in and out between the latest two polls, the "
                "same as a percent of the port's speed, the average over the window, the busiest interval's rates, and "
                "the errors, discards, and link flaps counted. Use it to find busy or saturated links, and links that "
                f"drop or damage packets. It looks back as far as {duration(statistics.reach)}. One call covers every "
                "device; name devices only to leave the others out."
            ),
        )
        async def interface_traffic(devices: DeviceNames = None, minutes: Minutes = 15) -> Collection[InterfaceTraffic]:
            with answered():
                return await engine.traffic(devices, minutes)

    @server.tool(annotations=READ_ONLY)
    async def topology(plane: PlaneChoice = "data", layer: Layer = "l2", network_instance: str = "default") -> Topology:
        """
        Return the network as a graph merged from every device's LLDP, ARP, and MAC tables and
        interfaces, with the evidence for every node and link. layer l2 (the default) is the
        cabling: devices, LLDP neighbors outside the inventory, shared segments, and hosts on the
        switch port their MAC is behind. layer l3 is the routing in one network instance: routers,
        the networks (subnets) they are on, and the hosts on those networks; switches drop out, and
        a network between just two routers is a link between them. layer ospf is OSPF in one network
        instance: the routers that run it and the areas each is in, an adjacency between each pair
        of neighbors with each end's area, cost, and state, and the networks advertised from passive
        interfaces. Two OSPF interfaces on one network that aren't full neighbors are a link too, and
        its problems say what keeps them apart. The data plane is the default; the management network
        rarely matters to a question.
        """
        return await engine.topology(plane, layer, network_instance)

    @server.tool(annotations=READ_ONLY)
    async def forwarding_path(source: str, destination: str, network_instance: str = "default") -> ForwardingPath:
        """
        Work out the path packets take from a source to a destination by following each
        device's routing table: the route that matches at every hop, the interface the
        packet leaves by, and where the path ends, delivered or lost (no route, a loop, a
        next hop nothing answers for). The source is a device, a host's address, or any
        address on a network a device is connected to; the destination is an IPv4 address.
        Nothing is sent on the network: the path is read from the devices' state.
        """
        with answered():
            return await engine.forwarding_path(source, destination, network_instance)

    if display:

        @server.tool(name="display", annotations=READ_ONLY)
        async def display_view(
            title: str,
            focus: Names = None,
            highlight: Names = None,
            path: Annotated[PathEnds | None, BeforeValidator(as_path_ends)] = None,
            notes: dict[str, str] | None = None,
            plane: PlaneChoice = "data",
            layer: Layer = "l2",
            network_instance: str = "default",
        ) -> Annotated[CallToolResult, View]:
            """
            Draw the network on the user's map. The whole network is always drawn: nodes in
            focus move to the centre, and everything else stays on the map, dimmed and pushed
            aside. Omit focus to show the whole network. highlight marks the answer itself.
            path takes a source and a destination: the server traces the way packets go
            between them through the routing tables and highlights it hop by hop, with
            where it is lost if it is. layer l2 (the default) draws the cabling and switches;
            l3 draws routers and the networks (subnets) between them, in network_instance; ospf
            draws OSPF's adjacencies with their costs and areas, and links that should be
            adjacencies but aren't.
            Name nodes by device name, host address, or network (192.168.12.0/24), and links
            by their two nodes (leaf1--spine1) or by one end (leaf1:ethernet-1/49). notes
            attach a few words to a node or link, keyed by its name.
            """
            ends = (path.source, path.destination) if path is not None else None
            with answered():
                view = await engine.view(title, focus, highlight, notes, plane, layer, network_instance, ends)
            # The model reads a summary; the page draws the view from the structured content.
            return CallToolResult(
                content=[TextContent(type="text", text=describe(view))],
                structured_content=view.model_dump(mode="json"),
            )

    if display and statistics is not None:

        @server.tool(
            name="chart",
            annotations=READ_ONLY,
            description=(
                "Draw a chart in the conversation of ports' rates over the last minutes. It keeps updating every "
                f"{statistics.interval:g} seconds as NetGPT polls. Name ports as device:port (leaf1:ethernet-1/49), a "
                "link as device--device for the first device's port on it, or a device for all its ports except "
                f"management; up to {MAX_CHART_PORTS} ports. metric is traffic "
                "(Mbit/s in and out, the default), percent (of each port's speed), or errors (errors and discards in "
                "each interval between polls, in and out). Over a window longer than "
                f"{duration(poll_by_poll(statistics.interval))}, each point is the average over 5 minutes or more, "
                f"and errors are counted per point. It looks back as far as {duration(statistics.reach)}."
            ),
        )
        async def chart(
            title: str, ports: ChartPorts, metric: Metric = "traffic", minutes: Minutes = 15
        ) -> Annotated[CallToolResult, Chart]:
            with answered():
                drawn = await engine.chart(title, ports, metric, minutes)
            # The model reads a summary; the page draws the chart from the structured content.
            return CallToolResult(
                content=[TextContent(type="text", text=describe_chart(drawn))],
                structured_content=drawn.model_dump(mode="json"),
            )

    @server.tool(annotations=READ_ONLY)
    async def ping(device: str, destination: str, count: int = 3, network_instance: str = "default") -> PingResult:
        """
        Ping an IPv4 address from a device, inside one of its network instances
        (VRFs), and return how many replies came back. Use this to test reachability
        through the network itself.
        """
        with answered():
            return await engine.ping(device, destination, count, network_instance)

    @server.tool(annotations=READ_ONLY)
    async def ping_sweep(
        network: str,
        device: str | None = None,
        network_instance: str = "default",
        count: int | None = None,
        timeout: float = 1.0,
    ) -> list[PingResult]:
        """
        Ping every address in an IPv4 network and return the addresses that answered.
        Name a device to sweep from it, inside one of its network instances (VRFs): that
        reaches the networks behind the device, and fills its ARP table (and switches'
        MAC tables) on the way, so topology can place what answered. Without a device,
        the sweep runs from the NetGPT host itself, which may reach only the management
        network. Only networks inside the inventory's sweep networks are allowed, up to
        /22 from the NetGPT host and /24 from a device, where each ping is slower. count
        is echo requests per address: 2 from the NetGPT host and 1 from a device unless
        given. timeout applies to sweeps from the NetGPT host.
        """
        with answered():
            return await engine.ping_sweep(network, device, network_instance, count, timeout)

    return server


def main() -> None:
    """Run the NetGPT MCP server over stdio."""
    parser = argparse.ArgumentParser(description="Serve NetGPT's read-only network tools over MCP (stdio).")
    parser.add_argument(
        "--inventory",
        type=Path,
        default=os.environ.get("NETGPT_INVENTORY"),
        help="Path to the inventory YAML file (default: $NETGPT_INVENTORY).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="Seconds between polls of every device's interface counters, which interface_traffic reads. "
        "0 turns polling, and the tool, off (default: %(default)g).",
    )
    parser.add_argument("--store", metavar="memory|URL", help=STORE_HELP)
    parser.add_argument("--log-level", default="INFO", help="Logging level, written to stderr.")
    args = parser.parse_args()
    if args.inventory is None:
        parser.error("--inventory is required (or set NETGPT_INVENTORY).")
    if args.poll_interval < 0:
        parser.error("--poll-interval can't be negative; 0 turns polling off.")

    # stdout carries the MCP protocol, so logs must go to stderr.
    logging.basicConfig(level=args.log_level, stream=sys.stderr)
    inventory = Inventory.load(Path(args.inventory))
    statistics = None
    if args.poll_interval > 0:
        try:
            store = open_store(args.store)
            # Find out now if the store can't be used, rather than when the first client connects.
            asyncio.run(check_store(store))
        except StoreError as error:
            parser.error(str(error))
        statistics = Poller(inventory, interval=args.poll_interval, store=store)
    server = build_server(Engine(inventory, statistics=statistics))
    server.run("stdio")
