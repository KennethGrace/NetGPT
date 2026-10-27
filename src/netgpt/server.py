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
from typing import TypeVar

import icmplib
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

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

logger = logging.getLogger("netgpt")

INSTRUCTIONS = """\
NetGPT gives read-only access to the network devices in its inventory.
Call list_devices first: every other tool takes device names from that list and cannot reach anything else.
lldp_neighbors, arp_table, mac_table, and routes query many devices at once and return typed records,
with per-device errors alongside the results. Use show for anything they don't cover; it accepts only
read-only commands and says why when it refuses one.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

# Errors we anticipate from a device query. Anything else propagates as a crash.
EXPECTED_ERRORS = (InventoryError, GuardError, PlatformError)

MAX_SWEEP_ADDRESSES = 1024

PrimitiveT = TypeVar("PrimitiveT")


def build_server(inventory: Inventory, platforms: dict[str, type[Platform]] = PLATFORMS) -> MCPServer:
    """Build the MCP server for an inventory. Platforms can be swapped out for testing."""
    server = MCPServer(name="netgpt", version=__version__, instructions=INSTRUCTIONS)

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
    async def lldp_neighbors(devices: list[str] | None = None) -> Collection[LldpNeighbor]:
        """Return LLDP adjacencies: which interface is cabled to which remote system and port. Omit devices to query all of them."""
        items, errors = await collect(devices, lambda platform: platform.lldp_neighbors())
        return Collection[LldpNeighbor](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def mac_table(devices: list[str] | None = None) -> Collection[MacEntry]:
        """Return layer 2 forwarding (MAC table) entries. Omit devices to query all of them."""
        items, errors = await collect(devices, lambda platform: platform.mac_table())
        return Collection[MacEntry](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def arp_table(devices: list[str] | None = None) -> Collection[ArpEntry]:
        """Return ARP bindings of IPv4 addresses to MAC addresses. Omit devices to query all of them."""
        items, errors = await collect(devices, lambda platform: platform.arp_table())
        return Collection[ArpEntry](items=items, errors=errors)

    @server.tool(annotations=READ_ONLY)
    async def routes(devices: list[str] | None = None, network_instance: str = "default") -> Collection[Route]:
        """Return the IPv4 routing table of one network instance (VRF). Omit devices to query all of them."""
        items, errors = await collect(devices, lambda platform: platform.routes(network_instance))
        return Collection[Route](items=items, errors=errors)

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
