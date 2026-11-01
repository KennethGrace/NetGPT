"""
The engine module is NetGPT's engine: everything it knows about the network, and
how it finds out. It asks the inventory's devices for their tables, concurrently,
keeping each device's failure beside the others' answers; merges the tables into
one graph at layer 2, at layer 3, or as OSPF sees it; walks forwarding paths
through the routing tables; makes views of the graph; runs read-only commands and
pings within the inventory's limits; and owns the poller whose counters give each
port's traffic.

The engine has faces, which only translate: the MCP server (netgpt.server) for
models, and an HTTP API for any frontend. The facts, and the limits on what may be
asked, are all here, so every face gets the same ones.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import TypeVar

import icmplib

from netgpt.forwarding import ForwardingPath, trace_path
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
    OspfInterfaces,
    OspfNeighbor,
    PingResult,
    Route,
)
from netgpt.statistics import Chart, ChartError, Metric, Poller
from netgpt.topology import Layer, PlaneChoice, Topology, build_l3, build_ospf, build_topology, ospf_mismatches, port_of
from netgpt.views import View, make_view

logger = logging.getLogger("netgpt")

# Errors anticipated from a device query: each becomes that device's error in a collection. Anything else is a bug.
EXPECTED_ERRORS = (InventoryError, GuardError, PlatformError)

MAX_SWEEP_ADDRESSES = 1024
# From a device, each ping is a command on the device: a /24 takes the better part of a minute.
MAX_DEVICE_SWEEP_ADDRESSES = 256

PrimitiveT = TypeVar("PrimitiveT")

Query = Callable[[Platform], Awaitable[list[PrimitiveT]]]


class RequestError(ValueError):
    """A request the engine refuses before asking any device, such as a network it may not sweep."""


def optional(query: Query[PrimitiveT]) -> Query[PrimitiveT]:
    """A query the topology can do without, on a platform that doesn't support it yet."""

    async def tolerant(platform: Platform) -> list[PrimitiveT]:
        try:
            return await query(platform)
        except UnsupportedError:
            return []

    return tolerant


def once(errors: Sequence[DeviceError]) -> list[DeviceError]:
    """A device that can't be reached fails every query the same way; report it once."""
    return list({(error.device, error.error): error for error in errors}.values())


class Engine:
    """
    NetGPT's engine for one inventory. Platforms can be swapped out for testing.
    With a poller, it polls interface counters while a face holds it running, and
    can say what each port's traffic has been.
    """

    def __init__(
        self, inventory: Inventory, platforms: dict[str, type[Platform]] = PLATFORMS, statistics: Poller | None = None
    ) -> None:
        self.inventory = inventory
        self.platforms = platforms
        self.statistics = statistics

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[Engine]:
        """Poll in the background while the block runs, if the engine polls. Faces that hold it at once share one poller."""
        if self.statistics is None:
            yield self
            return
        async with self.statistics.running():
            yield self

    def devices(self) -> list[DeviceInfo]:
        """The inventory's devices, as a client may see them: no credentials."""
        return [
            DeviceInfo(name=device.name, platform=device.platform, host=device.host, role=device.role)
            for device in self.inventory.devices
        ]

    def is_management(self, device: str, interface: str) -> bool:
        """Whether an interface is on its device's management network, which the graphs keep apart."""
        pattern = self.platforms.get(self.inventory.get(device).platform, Platform).management_interfaces
        return pattern is not None and pattern.match(port_of(interface)) is not None

    async def collect(self, devices: list[str] | None, query: Query[PrimitiveT]) -> tuple[list[PrimitiveT], list[DeviceError]]:
        """Ask every device named (or every device) the same question at once, keeping each one's failure."""
        names = devices or [device.name for device in self.inventory.devices]

        async def one(name: str) -> tuple[list[PrimitiveT], DeviceError | None]:
            try:
                async with connect_to(self.inventory, name, self.platforms) as platform:
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

    # The tables, from every device named, or every device.

    async def lldp_neighbors(self, devices: list[str] | None = None) -> Collection[LldpNeighbor]:
        items, errors = await self.collect(devices, lambda platform: platform.lldp_neighbors())
        return Collection[LldpNeighbor](items=items, errors=errors)

    async def interfaces(self, devices: list[str] | None = None) -> Collection[Interface]:
        items, errors = await self.collect(devices, lambda platform: platform.interfaces())
        return Collection[Interface](items=items, errors=errors)

    async def mac_table(self, devices: list[str] | None = None) -> Collection[MacEntry]:
        items, errors = await self.collect(devices, lambda platform: platform.mac_table())
        return Collection[MacEntry](items=items, errors=errors)

    async def arp_table(self, devices: list[str] | None = None) -> Collection[ArpEntry]:
        items, errors = await self.collect(devices, lambda platform: platform.arp_table())
        return Collection[ArpEntry](items=items, errors=errors)

    async def routes(self, devices: list[str] | None = None, network_instance: str = "default") -> Collection[Route]:
        items, errors = await self.collect(devices, lambda platform: platform.routes(network_instance))
        return Collection[Route](items=items, errors=errors)

    async def bgp_sessions(self, devices: list[str] | None = None) -> Collection[BgpSession]:
        items, errors = await self.collect(devices, lambda platform: platform.bgp_sessions())
        return Collection[BgpSession](items=items, errors=errors)

    async def ospf_neighbors(self, devices: list[str] | None = None) -> Collection[OspfNeighbor]:
        items, errors = await self.collect(devices, lambda platform: platform.ospf_neighbors())
        return Collection[OspfNeighbor](items=items, errors=errors)

    async def ospf_interfaces(self, devices: list[str] | None = None) -> OspfInterfaces:
        """The OSPF interfaces, and the settings that keep routers sharing a network from being neighbors."""
        (items, ospf_errors), (found, interface_errors) = await asyncio.gather(
            self.collect(devices, lambda platform: platform.ospf_interfaces()),
            self.collect(devices, optional(lambda platform: platform.interfaces())),
        )
        return OspfInterfaces(items=items, errors=once(ospf_errors + interface_errors), mismatches=ospf_mismatches(found, items))

    # The graphs, and the paths through them.

    async def layer2(self) -> tuple[Topology, list[Interface]]:
        """The layer 2 graph, and every device's interfaces, which layer 3 and the path walk need too."""
        (neighbors, lldp_errors), (bindings, arp_errors), (macs, mac_errors), (found, interface_errors) = await asyncio.gather(
            self.collect(None, lambda platform: platform.lldp_neighbors()),
            self.collect(None, lambda platform: platform.arp_table()),
            self.collect(None, optional(lambda platform: platform.mac_table())),
            self.collect(None, optional(lambda platform: platform.interfaces())),
        )
        errors = once(lldp_errors + arp_errors + mac_errors + interface_errors)
        return build_topology(self.devices(), neighbors, bindings, errors, self.is_management, macs, found), found

    async def at_layer(self, topology: Topology, found: list[Interface], layer: Layer, network_instance: str) -> Topology:
        """The layer 2 graph redrawn at another layer, in one network instance."""
        if layer == "l2":
            return topology
        if layer == "l3":
            return build_l3(self.devices(), topology, found, self.is_management, network_instance)
        (ospf, interface_errors), (heard, neighbor_errors) = await asyncio.gather(
            self.collect(None, lambda platform: platform.ospf_interfaces()),
            self.collect(None, lambda platform: platform.ospf_neighbors()),
        )
        errors = once(topology.errors + interface_errors + neighbor_errors)
        return build_ospf(self.devices(), found, ospf, heard, errors, network_instance)

    async def topology(self, plane: PlaneChoice = "data", layer: Layer = "l2", network_instance: str = "default") -> Topology:
        """The network as one graph at a layer, on a plane."""
        return (await self.at_layer(*await self.layer2(), layer, network_instance)).on_plane(plane)

    async def trace(
        self, source: str, destination: str, network_instance: str
    ) -> tuple[Topology, list[Interface], ForwardingPath]:
        """Trace a path, returning the layer 2 graph it was traced on, and the interfaces, as well."""
        (topology, found), (routes, failures) = await asyncio.gather(
            self.layer2(), self.collect(None, lambda platform: platform.routes(network_instance))
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

    async def forwarding_path(self, source: str, destination: str, network_instance: str = "default") -> ForwardingPath:
        """The path packets take from a source to a destination, read from the routing tables. Raises TraceError."""
        return (await self.trace(source, destination, network_instance))[2]

    async def view(
        self,
        title: str,
        focus: list[str] | None = None,
        highlight: list[str] | None = None,
        notes: dict[str, str] | None = None,
        plane: PlaneChoice = "data",
        layer: Layer = "l2",
        network_instance: str = "default",
        path: tuple[str, str] | None = None,
    ) -> View:
        """
        A view of the network at a layer, with the focus, highlight, and notes asked
        for, and the path between two points if asked. Raises ViewError for a name
        the graph doesn't have, and TraceError for a path it can't trace.
        """
        if path is not None:
            topology, found, traced = await self.trace(*path, network_instance)
        else:
            (topology, found), traced = await self.layer2(), None
        graph = await self.at_layer(topology, found, layer, network_instance)
        return make_view(graph, title, focus or [], highlight or [], notes or {}, plane, traced)

    # The polled counters.

    def poller(self) -> Poller:
        """The engine's poller. Raises RequestError if it doesn't poll."""
        if self.statistics is None:
            raise RequestError("NetGPT isn't polling interface counters.")
        return self.statistics

    async def traffic(self, devices: list[str] | None = None, minutes: float = 15) -> Collection[InterfaceTraffic]:
        """Each port's traffic and errors over the window. Raises StoreError if the store fails."""
        items, errors = await self.poller().traffic(devices, minutes)
        return Collection[InterfaceTraffic](items=items, errors=errors)

    async def link_port(self, name: str) -> str:
        """A link named device--device, as the first device's port on it (from LLDP); any other name as it is."""
        ends = [end.strip() for end in name.split("--")]
        if len(ends) != 2:
            return name
        known = {device.name for device in self.inventory.devices}
        if ends[0] not in known:
            ends.reverse()
        neighbors, _ = await self.collect([ends[0]], lambda platform: platform.lldp_neighbors())
        ports = [neighbor.interface for neighbor in neighbors if neighbor.neighbor == ends[1]]
        if not ports:
            raise ChartError(f"LLDP shows no link from {ends[0]} to {ends[1]}. Name a port as device:port instead.")
        return f"{ends[0]}:{ports[0]}"

    async def chart(self, title: str, ports: list[str], metric: Metric = "traffic", minutes: float = 15) -> Chart:
        """Ports' rates over the window, with links named device--device. Raises ChartError and StoreError."""
        poller = self.poller()
        return await poller.chart(title, [await self.link_port(name) for name in ports], metric, minutes)

    # Commands, each run when asked: read-only, on inventory devices, within the inventory's limits.

    async def show(self, device: str, command: str) -> CommandOutput:
        """One read-only CLI command's output. Raises GuardError, InventoryError, and PlatformError."""
        # Refuse a command before resolving credentials or opening a connection.
        platform_for(self.inventory, device, self.platforms).policy.check(command)
        async with connect_to(self.inventory, device, self.platforms) as platform:
            return await platform.show(command)

    async def ping(self, device: str, destination: str, count: int = 3, network_instance: str = "default") -> PingResult:
        """Echo requests from a device, in one of its network instances. Raises InventoryError and PlatformError."""
        async with connect_to(self.inventory, device, self.platforms) as platform:
            return await platform.ping(destination, count, network_instance)

    async def ping_sweep(
        self,
        network: str,
        device: str | None = None,
        network_instance: str = "default",
        count: int | None = None,
        timeout: float = 1.0,
    ) -> list[PingResult]:
        """
        The addresses in a network that answer ICMP, from a device or from the NetGPT
        host. Only networks inside the inventory's sweep networks, and no more than
        the limits. Raises RequestError, InventoryError, and PlatformError.
        """
        try:
            target = ipaddress.IPv4Network(network, strict=False)
        except ValueError as error:
            raise RequestError(str(error)) from None
        self.inventory.check_sweep(target)
        limit = MAX_SWEEP_ADDRESSES if device is None else MAX_DEVICE_SWEEP_ADDRESSES
        if target.num_addresses > limit:
            where = "" if device is None else " from a device"
            raise RequestError(f"{target} has {target.num_addresses} addresses; the limit{where} is {limit}.")
        addresses = [str(address) for address in target.hosts()] or [str(target.network_address)]
        if device is not None:
            async with connect_to(self.inventory, device, self.platforms) as platform:
                results = await platform.ping_sweep(addresses, count or 1, network_instance)
            return [result for result in results if result.received]
        hosts = await icmplib.async_multiping(
            addresses, count=count or 2, timeout=timeout, privileged=False, concurrent_tasks=128
        )
        return [
            PingResult(
                source="netgpt", destination=host.address, sent=host.packets_sent, received=host.packets_received,
                rtt_avg_ms=host.avg_rtt,
            )
            for host in hosts
            if host.is_alive
        ]
