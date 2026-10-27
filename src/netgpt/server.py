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
import ipaddress
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, TypeVar

import icmplib
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, BeforeValidator

from netgpt import __version__
from netgpt.guard import GuardError
from netgpt.inventory import Inventory, InventoryError
from netgpt.platforms import PLATFORMS, Platform, PlatformError
from netgpt.primitives import (
    ArpEntry,
    Collection,
    CommandOutput,
    DeviceError,
    DeviceInfo,
    LldpNeighbor,
    MacEntry,
    PingResult,
    Route,
)
from netgpt.forwarding import ForwardingPath, TraceError, trace_path
from netgpt.topology import PlaneChoice, Topology, build_topology, port_of
from netgpt.views import View, ViewError, describe, make_view

logger = logging.getLogger("netgpt")

INSTRUCTIONS = """\
NetGPT gives read-only access to the network devices in its inventory.
Call list_devices first: every other tool takes device names from that list and cannot reach anything else.
lldp_neighbors, arp_table, mac_table, and routes query many devices at once and return typed records,
with per-device errors alongside the results. topology merges every device's LLDP and ARP tables into
one graph of devices, hosts, and the links between them. forwarding_path follows the routing tables
hop by hop to show where packets from a source to a destination go, and where they are lost. Use show
for anything the typed tools don't cover; it accepts only read-only commands and says why when it
refuses one.
"""

DISPLAY_INSTRUCTIONS = """\
The user sees a map of the network beside the chat. Call display whenever a picture helps: the
topology, where a host is attached, the path between two points, where a problem is. Focus on the
part of the network the answer is about and highlight the answer itself; the rest of the network
stays on the map, dimmed and pushed aside. For the way traffic goes between two points, give display a
path (source and destination) and the server traces it through the routing tables. Refer to the map in
your answer rather than describing every link in words.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

# Errors we anticipate from a device query. Anything else propagates as a crash.
EXPECTED_ERRORS = (InventoryError, GuardError, PlatformError)

MAX_SWEEP_ADDRESSES = 1024

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
DeviceNames = Names


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
    inventory: Inventory, platforms: dict[str, type[Platform]] = PLATFORMS, display: bool = False
) -> MCPServer:
    """
    Build the MCP server for an inventory. Platforms can be swapped out for testing.
    display adds the tool that draws the network; only offer it to a client that can
    show the drawing.
    """
    instructions = INSTRUCTIONS + (DISPLAY_INSTRUCTIONS if display else "")
    server = MCPServer(name="netgpt", version=__version__, instructions=instructions)

    def platform_class(name: str) -> type[Platform]:
        device = inventory.get(name)
        platform = platforms.get(device.platform)
        if platform is None:
            supported = ", ".join(sorted(platforms))
            raise InventoryError(f"Device {name} has platform '{device.platform}'; supported platforms: {supported}.")
        return platform

    def connect(name: str) -> Platform:
        device = inventory.get(name)
        return platform_class(name)(device, inventory.credentials_for(device))

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

    async def gather_topology() -> Topology:
        (neighbors, lldp_errors), (bindings, arp_errors) = await asyncio.gather(
            collect(None, lambda platform: platform.lldp_neighbors()),
            collect(None, lambda platform: platform.arp_table()),
        )
        # A device that can't be reached fails both queries the same way; report it once.
        errors = list({(error.device, error.error): error for error in lldp_errors + arp_errors}.values())
        return build_topology(list_devices(), neighbors, bindings, errors, is_management)

    async def trace(source: str, destination: str, network_instance: str) -> tuple[Topology, ForwardingPath]:
        """Trace a path, returning the topology it was traced on as well."""
        topology, (routes, failures) = await asyncio.gather(
            gather_topology(), collect(None, lambda platform: platform.routes(network_instance))
        )
        tables: dict[str, list[Route]] = {}
        for route in routes:
            tables.setdefault(route.device, []).append(route)
        unreadable = {failure.device: failure.error for failure in failures}
        return topology, trace_path(topology, tables, unreadable, source, destination, network_instance)

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
        Commands that could change the device, write files, or chain several
        commands together are refused, with the reason.
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
        """Return LLDP adjacencies: which interface is cabled to which remote system and port. Omit devices to query all of them."""
        items, errors = await collect(devices, lambda platform: platform.lldp_neighbors())
        return Collection[LldpNeighbor](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def mac_table(devices: DeviceNames = None) -> Collection[MacEntry]:
        """Return layer 2 forwarding (MAC table) entries. Omit devices to query all of them."""
        items, errors = await collect(devices, lambda platform: platform.mac_table())
        return Collection[MacEntry](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def arp_table(devices: DeviceNames = None) -> Collection[ArpEntry]:
        """Return ARP bindings of IPv4 addresses to MAC addresses. Omit devices to query all of them."""
        items, errors = await collect(devices, lambda platform: platform.arp_table())
        return Collection[ArpEntry](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def routes(devices: DeviceNames = None, network_instance: str = "default") -> Collection[Route]:
        """Return the IPv4 routing table of one network instance (VRF). Omit devices to query all of them."""
        items, errors = await collect(devices, lambda platform: platform.routes(network_instance))
        return Collection[Route](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def topology(plane: PlaneChoice = "data") -> Topology:
        """
        Return the network as a graph merged from every device's LLDP and ARP tables:
        devices, LLDP neighbors outside the inventory, hosts found by ARP, and shared
        segments, with the links between them and the evidence for each. The data
        plane is the default; the management network rarely matters to a question.
        """
        return (await gather_topology()).on_plane(plane)

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
            return (await trace(source, destination, network_instance))[1]
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
        ) -> Annotated[CallToolResult, View]:
            """
            Draw the network on the user's map. The whole network is always drawn: nodes in
            focus move to the centre, and everything else stays on the map, dimmed and pushed
            aside. Omit focus to show the whole network. highlight marks the answer itself.
            path takes a source and a destination: the server traces the way packets go
            between them through the routing tables and highlights it hop by hop, with
            where it is lost if it is. Name nodes by device name or host address, and links
            by their two nodes (leaf1--spine1) or by one end (leaf1:ethernet-1/49). notes
            attach a few words to a node or link, keyed by its name.
            """
            try:
                if path is not None:
                    topology, traced = await trace(path.source, path.destination, "default")
                else:
                    topology, traced = await gather_topology(), None
                view = make_view(topology, title, focus or [], highlight or [], notes or {}, plane, traced)
            except (TraceError, ViewError) as error:
                raise ToolError(str(error)) from error
            # The model reads a summary; the page draws the view from the structured content.
            return CallToolResult(
                content=[TextContent(type="text", text=describe(view))],
                structured_content=view.model_dump(mode="json"),
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
    async def ping_sweep(network: str, count: int = 2, timeout: float = 1.0) -> list[PingResult]:
        """
        Ping every address in an IPv4 network from the NetGPT host itself (not
        from a device) and return the addresses that answered. Only networks
        inside the inventory's sweep networks are allowed, up to /22 in size.
        """
        try:
            target = ipaddress.IPv4Network(network, strict=False)
            inventory.check_sweep(target)
        except (ValueError, InventoryError) as error:
            raise ToolError(str(error)) from error
        if target.num_addresses > MAX_SWEEP_ADDRESSES:
            raise ToolError(f"{target} has {target.num_addresses} addresses; the limit is {MAX_SWEEP_ADDRESSES}.")
        addresses = [str(address) for address in target.hosts()] or [str(target.network_address)]
        hosts = await icmplib.async_multiping(
            addresses, count=count, timeout=timeout, privileged=False, concurrent_tasks=128
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
    parser.add_argument("--log-level", default="INFO", help="Logging level, written to stderr.")
    args = parser.parse_args()
    if args.inventory is None:
        parser.error("--inventory is required (or set NETGPT_INVENTORY).")

    # stdout carries the MCP protocol, so logs must go to stderr.
    logging.basicConfig(level=args.log_level, stream=sys.stderr)
    server = build_server(Inventory.load(Path(args.inventory)))
    server.run("stdio")
