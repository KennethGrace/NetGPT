"""
The base platform module defines the interface every network platform implements.

A platform turns NetGPT's questions (LLDP neighbors, ARP, routes, ...) into calls
to one kind of device and returns typed primitives. Queries a platform does not
implement raise UnsupportedError, which a multi-device query reports per device
instead of failing the whole request.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from types import TracebackType
from typing import ClassVar

from netgpt.guard import CliPolicy
from netgpt.inventory import Credentials, Device, InventoryError
from netgpt.primitives import ArpEntry, CommandOutput, Interface, LldpNeighbor, MacEntry, PingResult, Route


class PlatformError(RuntimeError):
    """A device could not answer a query: an API error, a refused login, or an unexpected reply."""


class UnsupportedError(PlatformError):
    """The platform does not implement the requested query."""


class Platform(ABC):
    """
    One connection to one device. Use it as an async context manager so the
    underlying connection is always closed.
    """

    policy: ClassVar[CliPolicy]
    # Ports on the out-of-band management network, which the topology keeps apart from the data plane.
    management_interfaces: ClassVar[re.Pattern[str] | None] = None
    # How many pings a sweep may have in flight at once over one connection to the device.
    sweep_width: ClassVar[int] = 1

    def __init__(self, device: Device, credentials: Credentials) -> None:
        if device.tls_ca is not None and not device.tls_ca.is_file():
            raise InventoryError(f"TLS CA file {device.tls_ca} for {device.name} does not exist. Is the lab deployed?")
        self.device = device
        self.credentials = credentials

    async def __aenter__(self) -> Platform:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Release the connection to the device."""

    async def show(self, command: str, max_chars: int = 50_000) -> CommandOutput:
        """Run a read-only CLI command after checking it against the platform's policy."""
        checked = self.policy.check(command)
        output = await self._run_checked_cli(checked)
        truncated = len(output) > max_chars
        if truncated:
            output = output[:max_chars] + f"\n... [truncated at {max_chars} characters]"
        return CommandOutput(device=self.device.name, command=checked, output=output, truncated=truncated)

    @abstractmethod
    async def _run_checked_cli(self, command: str) -> str:
        """Run a command that has already passed the policy check and return its text output."""

    def _unsupported(self, query: str) -> UnsupportedError:
        return UnsupportedError(f"{self.device.platform} does not support {query} yet.")

    async def lldp_neighbors(self) -> list[LldpNeighbor]:
        """Return the device's LLDP adjacencies."""
        raise self._unsupported("LLDP neighbors")

    async def interfaces(self) -> list[Interface]:
        """Return the device's logical interfaces: network instance, routed or bridged, and addresses."""
        raise self._unsupported("interfaces")

    async def mac_table(self) -> list[MacEntry]:
        """Return the device's layer 2 forwarding entries across all network instances."""
        raise self._unsupported("MAC tables")

    async def arp_table(self) -> list[ArpEntry]:
        """Return the device's ARP bindings."""
        raise self._unsupported("ARP tables")

    async def routes(self, network_instance: str) -> list[Route]:
        """Return the IPv4 routing table of one network instance (VRF)."""
        raise self._unsupported("routing tables")

    async def ping(self, destination: str, count: int, network_instance: str) -> PingResult:
        """Send ICMP echo requests from the device to a destination."""
        raise self._unsupported("ping")

    async def ping_sweep(self, destinations: list[str], count: int, network_instance: str) -> list[PingResult]:
        """
        Ping every destination from the device, sweep_width at a time, and return all
        the results. A ping that fails, rather than going unanswered, is tried once more;
        if it fails again, so does the sweep, since an address it couldn't try isn't
        known to be empty.
        """
        gate = asyncio.Semaphore(self.sweep_width)

        async def one(destination: str) -> PingResult:
            async with gate:
                try:
                    return await self.ping(destination, count, network_instance)
                except UnsupportedError:
                    raise
                except PlatformError:
                    return await self.ping(destination, count, network_instance)

        results = await asyncio.gather(*(one(destination) for destination in destinations), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results
