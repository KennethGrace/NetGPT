"""
The statistics module polls every device's interface counters on an interval and
turns them into rates: bits per second each way, a share of the port's speed, and
the errors, discards, and link flaps counted between polls.

Counters only grow, so a rate is the difference between two readings over the time
between them. A counter that goes backwards was reset (the device restarted, or
someone cleared it), and that interval isn't measured. Samples are kept in memory
for a window; nothing survives a restart yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise

from netgpt.inventory import Inventory, InventoryError
from netgpt.platforms import PLATFORMS, Platform, PlatformError, connect
from netgpt.primitives import DeviceError, InterfaceCounters, InterfaceTraffic

logger = logging.getLogger("netgpt")

DEFAULT_INTERVAL = 30.0
# How much history is kept, in seconds: an hour of samples at the default interval is 120 per port.
DEFAULT_WINDOW = 3600.0
# The second poll comes this soon after the first, so there are rates within seconds of starting.
FIRST_INTERVAL = 5.0

# The counters a rate is the difference of. Any of them going backwards means a reset.
COUNTED = ("in_octets", "out_octets", "in_errors", "out_errors", "in_discards", "out_discards")


@dataclass(frozen=True)
class Sample:
    """One port's counters, and when they arrived (seconds since the epoch)."""

    at: float
    counters: InterfaceCounters


@dataclass(frozen=True)
class Rate:
    """One port's traffic between two samples."""

    start: float
    end: float
    in_bps: float
    out_bps: float
    in_errors: int
    out_errors: int
    in_discards: int
    out_discards: int
    flaps: int | None


def rate(earlier: Sample, later: Sample) -> Rate | None:
    """The traffic between two samples of one port, or None if a counter was reset in between."""
    seconds = later.at - earlier.at
    deltas = {name: getattr(later.counters, name) - getattr(earlier.counters, name) for name in COUNTED}
    if seconds <= 0 or any(delta < 0 for delta in deltas.values()):
        return None
    flaps = None
    if later.counters.carrier_transitions is not None and earlier.counters.carrier_transitions is not None:
        flaps = later.counters.carrier_transitions - earlier.counters.carrier_transitions
        if flaps < 0:
            return None
    return Rate(
        start=earlier.at,
        end=later.at,
        in_bps=deltas["in_octets"] * 8 / seconds,
        out_bps=deltas["out_octets"] * 8 / seconds,
        in_errors=deltas["in_errors"],
        out_errors=deltas["out_errors"],
        in_discards=deltas["in_discards"],
        out_discards=deltas["out_discards"],
        flaps=flaps,
    )


def speed_name(bps: int | None) -> str | None:
    """Write a port speed the way vendors do: 25G, 100M."""
    if not bps:
        return None
    for unit, size in (("T", 10**12), ("G", 10**9), ("M", 10**6)):
        if bps >= size and bps % size == 0:
            return f"{bps // size}{unit}"
    return f"{bps}bps"


def summarize(samples: Sequence[Sample], since: float) -> InterfaceTraffic | None:
    """What one port's samples say since a moment: None until there are two to compare after it."""
    rates = [found for earlier, later in pairwise(samples) if later.at > since and (found := rate(earlier, later))]
    if not rates:
        return None
    latest, counters = rates[-1], samples[-1].counters

    def mbps(bps: float) -> float:
        return round(bps / 1e6, 3)

    def percent(bps: float) -> float | None:
        return round(100 * bps / counters.speed_bps, 3) if counters.speed_bps else None

    flaps = [found.flaps for found in rates]
    return InterfaceTraffic(
        device=counters.device,
        interface=counters.interface,
        oper_state=counters.oper_state,
        speed=speed_name(counters.speed_bps),
        minutes=round((latest.end - rates[0].start) / 60, 1),
        in_mbps=mbps(latest.in_bps),
        out_mbps=mbps(latest.out_bps),
        in_percent=percent(latest.in_bps),
        out_percent=percent(latest.out_bps),
        peak_in_mbps=mbps(max(found.in_bps for found in rates)),
        peak_out_mbps=mbps(max(found.out_bps for found in rates)),
        in_errors=sum(found.in_errors for found in rates),
        out_errors=sum(found.out_errors for found in rates),
        in_discards=sum(found.in_discards for found in rates),
        out_discards=sum(found.out_discards for found in rates),
        flaps=None if None in flaps else sum(flap for flap in flaps if flap is not None),
    )


def port_order(name: str) -> list[str | int]:
    """Sort ports as people number them: ethernet-1/2 before ethernet-1/10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


class Poller:
    """
    Polls every device in the inventory for its interface counters, every interval
    seconds, and keeps the samples from the last window seconds. Run it with
    running(), which polls in the background until the block ends.
    """

    def __init__(
        self,
        inventory: Inventory,
        platforms: dict[str, type[Platform]] = PLATFORMS,
        interval: float = DEFAULT_INTERVAL,
        window: float = DEFAULT_WINDOW,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """clock is only for tests, which set the time each sample arrives."""
        self.interval = interval
        self.window = window
        self._inventory = inventory
        self._platforms = platforms
        self._clock = clock
        self._samples: dict[tuple[str, str], deque[Sample]] = {}
        # Each device's error from its latest poll, while it keeps failing.
        self._errors: dict[str, str] = {}
        self._users = 0
        self._task: asyncio.Task[None] | None = None

    async def poll(self) -> None:
        """Sample every device once."""

        async def one(name: str) -> None:
            try:
                async with connect(self._inventory, name, self._platforms) as platform:
                    readings = await platform.interface_counters()
            except (InventoryError, PlatformError) as error:
                logger.info("Polling %s failed: %s", name, error)
                self._errors[name] = str(error)
                return
            at = self._clock()
            self._errors.pop(name, None)
            for reading in readings:
                series = self._samples.setdefault((name, reading.interface), deque())
                series.append(Sample(at, reading))
                while series[0].at < at - self.window:
                    series.popleft()

        await asyncio.gather(*(one(device.name) for device in self._inventory.devices))

    async def run(self) -> None:
        """Poll until cancelled: once now, again shortly after, then every interval."""
        await self.poll()
        await asyncio.sleep(min(FIRST_INTERVAL, self.interval))
        loop = asyncio.get_running_loop()
        due = loop.time()
        while True:
            await self.poll()
            # Keep to the interval however long a poll takes, without bunching up after a slow one.
            due = max(due + self.interval, loop.time())
            await asyncio.sleep(due - loop.time())

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[Poller]:
        """Poll in the background while the block runs. Nested blocks share one poller."""
        self._users += 1
        if self._task is None:
            self._task = asyncio.create_task(self.run())
        try:
            yield self
        finally:
            self._users -= 1
            if self._users == 0 and self._task is not None:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
                self._task = None

    def traffic(self, devices: list[str] | None, minutes: float) -> tuple[list[InterfaceTraffic], list[DeviceError]]:
        """Each port's traffic over the last minutes, on the named devices or all of them, and why any device has none."""
        known = [device.name for device in self._inventory.devices]
        names = devices or known
        since = self._clock() - min(minutes * 60, self.window)
        items: list[InterfaceTraffic] = []
        errors: list[DeviceError] = []
        for name in names:
            try:
                self._inventory.get(name)
            except InventoryError as error:
                errors.append(DeviceError(device=name, error=str(error)))
                continue
            ports = sorted((port for device, port in self._samples if device == name), key=port_order)
            found = [summary for port in ports if (summary := summarize(self._samples[(name, port)], since))]
            items.extend(found)
            if name in self._errors:
                errors.append(DeviceError(device=name, error=self._errors[name]))
            elif not found:
                errors.append(
                    DeviceError(
                        device=name,
                        error=f"No rates yet: polling takes two samples {FIRST_INTERVAL:.0f} seconds apart to begin with, "
                        f"then one every {self.interval:g} seconds.",
                    )
                )
        return items, errors
