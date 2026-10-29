"""
The NetGPT MCP server exposes read-only network tools to language model clients.

Every tool is read-only and reaches only the devices and networks named in the
inventory. Multi-device tools query devices concurrently and return a
Collection, so one unreachable device doesn't sink the whole answer. Only
anticipated failures (bad input, refused commands, device errors) are caught and
reported. Anything else is a bug and is allowed to fail loudly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

import icmplib
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, BeforeValidator, Field

from netgpt import __version__
from netgpt.guard import GuardError
from netgpt.inventory import Inventory, InventoryError
from netgpt.platforms import PLATFORMS, Platform, PlatformError, platform_for
from netgpt.platforms import connect as connect_to
from netgpt.platforms.base import UnsupportedError
from netgpt.primitives import (
    ArpEntry,
    BgpSession,
    Collection,
    CommandOutput,
    DeviceError,
    DeviceInfo,
    Interface,
    InterfaceTraffic,
    LldpNeighbor,
    MacEntry,
    PingResult,
    Route,
)
from netgpt.forwarding import ForwardingPath, TraceError, trace_path
from netgpt.statistics import DEFAULT_INTERVAL, DEFAULT_WINDOW, MAX_CHART_PORTS, Chart, ChartError, Metric, Poller, describe_chart
from netgpt.topology import Layer, PlaneChoice, Topology, build_l3, build_topology, port_of
from netgpt.views import View, ViewError, describe, make_view

logger = logging.getLogger("netgpt")

INSTRUCTIONS = """\
NetGPT gives read-only access to the network devices in its inventory.
Call list_devices first: every other tool takes device names from that list and cannot reach anything else.
lldp_neighbors, interfaces, arp_table, mac_table, routes, and bgp_sessions return typed records from
every device in one call, with per-device errors alongside the results. Ask for every device you need in one call, rather
than one device at a time. topology merges them into one graph: at layer 2
(the default) the cables, switches, and the port each host is plugged into; at layer 3 the routers and
the networks between them. forwarding_path follows the routing tables hop by hop to show where packets
from a source to a destination go, and where they are lost. Use show for anything the typed tools don't
cover; it accepts only read-only commands and says why when it refuses one.
"""

STATISTICS_INSTRUCTIONS = """\
interface_traffic reads the interface counters NetGPT polls every {interval:g} seconds: how much each port
sends and receives, how busy it is as a share of its speed, and the errors, discards, and link flaps
counted lately.
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

# Errors we anticipate from a device query. Anything else propagates as a crash.
EXPECTED_ERRORS = (InventoryError, GuardError, PlatformError)

MAX_SWEEP_ADDRESSES = 1024
# From a device, each ping is a command on the device: a /24 takes the better part of a minute.
MAX_DEVICE_SWEEP_ADDRESSES = 256

PrimitiveT = TypeVar("PrimitiveT")



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


Minutes = Annotated[
    float, Field(description=f"How far back to look, in minutes. NetGPT keeps the last {DEFAULT_WINDOW / 60:g} minutes.")
]


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


def build_server(
    inventory: Inventory,
    platforms: dict[str, type[Platform]] = PLATFORMS,
    display: bool = False,
    statistics: Poller | None = None,
) -> MCPServer:
    """
    Build the MCP server for an inventory. Platforms can be swapped out for testing.
    display adds the tool that draws the network; only offer it to a client that can
    show the drawing. With a poller, the server polls interface counters while a
    client is connected, and offers interface_traffic to read them.
    """
    instructions = (
        INSTRUCTIONS
        + (STATISTICS_INSTRUCTIONS.format(interval=statistics.interval) if statistics else "")
        + (DISPLAY_INSTRUCTIONS if display else "")
        + (CHART_INSTRUCTIONS if display and statistics else "")
    )

    @contextlib.asynccontextmanager
    async def polling(_: MCPServer) -> AsyncIterator[dict[str, Any]]:
        if statistics is None:
            yield {}
            return
        async with statistics.running():
            yield {}

    server = MCPServer(name="netgpt", version=__version__, instructions=instructions, lifespan=polling)

    def platform_class(name: str) -> type[Platform]:
        return platform_for(inventory, name, platforms)

    def connect(name: str) -> Platform:
        return connect_to(inventory, name, platforms)

    async def collect(
        devices: list[str] | None,
        query: Callable[[Platform], Awaitable[list[PrimitiveT]]],
    ) -> tuple[list[PrimitiveT], list[DeviceError]]:
        names = devices or [device.name for device in inventory.devices]

        async def one(name: str) -> tuple[list[PrimitiveT], DeviceError | None]:
            try:
                async with connect(name) as platform:
                    return await query(platform), None
            except EXPECTED_ERRORS as error:
                logger.info("Query failed on %s: %s", name, error)
                return [], DeviceError(device=name, error=str(error))

        items: list[PrimitiveT] = []
        errors: list[DeviceError] = []
        for found, error in await asyncio.gather(*(one(name) for name in names)):
            items.extend(found)
            if error is not None:
                errors.append(error)
        return items, errors

    def is_management(device: str, interface: str) -> bool:
        pattern = platforms.get(inventory.get(device).platform, Platform).management_interfaces
        return pattern is not None and pattern.match(port_of(interface)) is not None

    def optional(query: Callable[[Platform], Awaitable[list[PrimitiveT]]]) -> Callable[[Platform], Awaitable[list[PrimitiveT]]]:
        """A query the topology can do without, on a platform that doesn't support it yet."""

        async def tolerant(platform: Platform) -> list[PrimitiveT]:
            try:
                return await query(platform)
            except UnsupportedError:
                return []

        return tolerant

    async def gather_topology() -> tuple[Topology, list[Interface]]:
        """The layer 2 graph, and every device's interfaces, which layer 3 and the path walk need too."""
        (neighbors, lldp_errors), (bindings, arp_errors), (macs, mac_errors), (found, interface_errors) = await asyncio.gather(
            collect(None, lambda platform: platform.lldp_neighbors()),
            collect(None, lambda platform: platform.arp_table()),
            collect(None, optional(lambda platform: platform.mac_table())),
            collect(None, optional(lambda platform: platform.interfaces())),
        )
        # A device that can't be reached fails every query the same way; report it once.
        failures = lldp_errors + arp_errors + mac_errors + interface_errors
        errors = list({(error.device, error.error): error for error in failures}.values())
        return build_topology(list_devices(), neighbors, bindings, errors, is_management, macs, found), found

    def at_layer(topology: Topology, found: list[Interface], layer: Layer, network_instance: str) -> Topology:
        return topology if layer == "l2" else build_l3(list_devices(), topology, found, is_management, network_instance)

    async def trace(
        source: str, destination: str, network_instance: str
    ) -> tuple[Topology, list[Interface], ForwardingPath]:
        """Trace a path, returning the topology it was traced on, and the interfaces, as well."""
        (topology, found), (routes, failures) = await asyncio.gather(
            gather_topology(), collect(None, lambda platform: platform.routes(network_instance))
        )
        tables: dict[str, list[Route]] = {}
        for route in routes:
            tables.setdefault(route.device, []).append(route)
        # A switch with no interface in the network instance has no routing table there to read.
        answered = {interface.device for interface in found}
        present = {interface.device for interface in found if interface.network_instance == network_instance}
        unreadable = {
            failure.device: failure.error for failure in failures if failure.device in present or failure.device not in answered
        }
        return topology, found, trace_path(topology, tables, unreadable, source, destination, network_instance)

    async def link_port(name: str) -> str:
        """A link named device--device, as the first device's port on it (from LLDP); any other name as it is."""
        ends = [end.strip() for end in name.split("--")]
        if len(ends) != 2:
            return name
        known = {device.name for device in inventory.devices}
        if ends[0] not in known:
            ends.reverse()
        neighbors, _ = await collect([ends[0]], lambda platform: platform.lldp_neighbors())
        ports = [neighbor.interface for neighbor in neighbors if neighbor.neighbor == ends[1]]
        if not ports:
            raise ChartError(f"LLDP shows no link from {ends[0]} to {ends[1]}. Name a port as device:port instead.")
        return f"{ends[0]}:{ports[0]}"

    @server.tool(annotations=READ_ONLY)
    def list_devices() -> list[DeviceInfo]:
        """List the devices NetGPT can query, with their platform, address, and role."""
        return [
            DeviceInfo(name=device.name, platform=device.platform, host=device.host, role=device.role)
            for device in inventory.devices
        ]

    @server.tool(annotations=READ_ONLY)
    async def show(device: str, command: str) -> CommandOutput:
        """
        Run one read-only CLI command on a device and return its text output.
        The command is in the device's own CLI, which may not be Cisco's:
        list_devices gives each device's platform. Where a typed tool covers the
        question, use it instead. Commands that could change the device, write
        files, or chain several commands together are refused, with the reason.
        """
        try:
            # Refuse a command before resolving credentials or opening a connection.
            platform_class(device).policy.check(command)
            async with connect(device) as platform:
                return await platform.show(command)
        except EXPECTED_ERRORS as error:
            raise ToolError(str(error)) from error

    @server.tool(annotations=READ_ONLY)
    async def lldp_neighbors(devices: DeviceNames = None) -> Collection[LldpNeighbor]:
        """
        Return every device's LLDP adjacencies: which interface is cabled to which remote system and port.
        One call covers every device; name devices only to leave the others out.
        """
        items, errors = await collect(devices, lambda platform: platform.lldp_neighbors())
        return Collection[LldpNeighbor](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def interfaces(devices: DeviceNames = None) -> Collection[Interface]:
        """
        Return each device's logical interfaces: the network instance (VRF or bridge) each is in,
        whether it routes or bridges, its IPv4 and IPv6 addresses, and whether it is up. network_instance is null for
        a subinterface in no network instance: nothing passes through it, whether it is up or not.
        One call covers every device; name devices only to leave the others out.
        """
        items, errors = await collect(devices, lambda platform: platform.interfaces())
        return Collection[Interface](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def mac_table(devices: DeviceNames = None) -> Collection[MacEntry]:
        """
        Return every device's layer 2 forwarding (MAC table) entries, per network instance.
        One call covers every device; name devices only to leave the others out.
        """
        items, errors = await collect(devices, lambda platform: platform.mac_table())
        return Collection[MacEntry](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def arp_table(devices: DeviceNames = None) -> Collection[ArpEntry]:
        """
        Return every device's ARP bindings of IPv4 addresses to MAC addresses.
        One call covers every device; name devices only to leave the others out.
        """
        items, errors = await collect(devices, lambda platform: platform.arp_table())
        return Collection[ArpEntry](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def routes(devices: DeviceNames = None, network_instance: str = "default") -> Collection[Route]:
        """
        Return every device's routes in one network instance (VRF): IPv4, and IPv6 where the platform reads it.
        One call covers every device; name devices only to leave the others out.
        """
        items, errors = await collect(devices, lambda platform: platform.routes(network_instance))
        return Collection[Route](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def bgp_sessions(devices: DeviceNames = None) -> Collection[BgpSession]:
        """
        Return every device's BGP sessions, in every network instance: the peer's address and AS, the local
        address and AS the session uses, its state, the last error, the address families it carries, and
        how many prefixes it received. Only an Established session exchanges routes; Active means it is
        trying to connect and failing. One call covers every device; name devices only to leave the others out.
        """
        items, errors = await collect(devices, lambda platform: platform.bgp_sessions())
        return Collection[BgpSession](items=items, errors=errors)

    if statistics is not None:

        @server.tool(
            annotations=READ_ONLY,
            description=(
                "Return each port's traffic and errors over the last minutes, from the interface counters NetGPT polls "
                f"every {statistics.interval:g} seconds: megabits per second in and out between the latest two polls, the "
                "same as a percent of the port's speed, the busiest interval's rates, and the errors, discards, and link "
                "flaps counted. Use it to find busy or saturated links, and links that drop or damage packets. One call "
                "covers every device; name devices only to leave the others out."
            ),
        )
        def interface_traffic(devices: DeviceNames = None, minutes: Minutes = 15) -> Collection[InterfaceTraffic]:
            items, errors = statistics.traffic(devices, minutes)
            return Collection[InterfaceTraffic](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def topology(plane: PlaneChoice = "data", layer: Layer = "l2", network_instance: str = "default") -> Topology:
        """
        Return the network as a graph merged from every device's LLDP, ARP, and MAC tables and
        interfaces, with the evidence for every node and link. layer l2 (the default) is the
        cabling: devices, LLDP neighbors outside the inventory, shared segments, and hosts on the
        switch port their MAC is behind. layer l3 is the routing in one network instance: routers,
        the networks (subnets) they are on, and the hosts on those networks; switches drop out, and
        a network between just two routers is a link between them. The data plane is the default;
        the management network rarely matters to a question.
        """
        return at_layer(*await gather_topology(), layer, network_instance).on_plane(plane)

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
        try:
            return (await trace(source, destination, network_instance))[2]
        except TraceError as error:
            raise ToolError(str(error)) from error

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
            l3 draws routers and the networks (subnets) between them, in network_instance.
            Name nodes by device name, host address, or network (192.168.12.0/24), and links
            by their two nodes (leaf1--spine1) or by one end (leaf1:ethernet-1/49). notes
            attach a few words to a node or link, keyed by its name.
            """
            try:
                if path is not None:
                    topology, found, traced = await trace(path.source, path.destination, network_instance)
                else:
                    (topology, found), traced = await gather_topology(), None
                graph = at_layer(topology, found, layer, network_instance)
                view = make_view(graph, title, focus or [], highlight or [], notes or {}, plane, traced)
            except (TraceError, ViewError) as error:
                raise ToolError(str(error)) from error
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
                "each interval between polls, in and out)."
            ),
        )
        async def chart(
            title: str, ports: ChartPorts, metric: Metric = "traffic", minutes: Minutes = 15
        ) -> Annotated[CallToolResult, Chart]:
            try:
                drawn = statistics.chart(title, [await link_port(name) for name in ports], metric, minutes)
            except ChartError as error:
                raise ToolError(str(error)) from error
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
        try:
            async with connect(device) as platform:
                return await platform.ping(destination, count, network_instance)
        except EXPECTED_ERRORS as error:
            raise ToolError(str(error)) from error

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
        try:
            target = ipaddress.IPv4Network(network, strict=False)
            inventory.check_sweep(target)
        except (ValueError, InventoryError) as error:
            raise ToolError(str(error)) from error
        limit = MAX_SWEEP_ADDRESSES if device is None else MAX_DEVICE_SWEEP_ADDRESSES
        if target.num_addresses > limit:
            where = "" if device is None else " from a device"
            raise ToolError(f"{target} has {target.num_addresses} addresses; the limit{where} is {limit}.")
        addresses = [str(address) for address in target.hosts()] or [str(target.network_address)]
        if device is not None:
            try:
                async with connect(device) as platform:
                    results = await platform.ping_sweep(addresses, count or 1, network_instance)
            except EXPECTED_ERRORS as error:
                raise ToolError(str(error)) from error
            return [result for result in results if result.received]
        hosts = await icmplib.async_multiping(
            addresses, count=count or 2, timeout=timeout, privileged=False, concurrent_tasks=128
        )
        return [
            PingResult(
                source="netgpt",
                destination=host.address,
                sent=host.packets_sent,
                received=host.packets_received,
                rtt_avg_ms=host.avg_rtt,
            )
            for host in hosts
            if host.is_alive
        ]

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
    parser.add_argument("--log-level", default="INFO", help="Logging level, written to stderr.")
    args = parser.parse_args()
    if args.inventory is None:
        parser.error("--inventory is required (or set NETGPT_INVENTORY).")
    if args.poll_interval < 0:
        parser.error("--poll-interval can't be negative; 0 turns polling off.")

    # stdout carries the MCP protocol, so logs must go to stderr.
    logging.basicConfig(level=args.log_level, stream=sys.stderr)
    inventory = Inventory.load(Path(args.inventory))
    statistics = Poller(inventory, interval=args.poll_interval) if args.poll_interval > 0 else None
    server = build_server(inventory, statistics=statistics)
    server.run("stdio")
