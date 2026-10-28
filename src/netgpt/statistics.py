"""
The statistics module polls every device's interface counters on an interval and
turns them into rates: bits per second each way, a share of the port's speed, and
the errors, discards, and link flaps counted between polls.

Counters only grow, so a rate is the difference between two readings over the time
between them. A counter that goes backwards was reset (the device restarted, or
someone cleared it), and that interval isn't measured. Samples are kept in memory
for a window; nothing survives a restart yet.

A Chart is a few ports' rates over time, which the web page draws in the
conversation and keeps asking for again as the poller takes new samples.
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
from typing import Literal

from pydantic import BaseModel, Field

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

Metric = Literal["traffic", "percent", "errors"]
UNITS: dict[str, str] = {"traffic": "Mbit/s", "percent": "% of port speed", "errors": "errors and discards per poll"}
# The page has six colors for ports, each port's in and out drawn in one of them.
MAX_CHART_PORTS = 6


class ChartError(ValueError):
    """A chart asked for ports that NetGPT doesn't poll; the message names the ones it does."""


class ChartLine(BaseModel):
    """One line on a chart: one port's traffic, utilization, or errors, one way."""

    port: str = Field(description="The device and port, as device:port.")
    direction: Literal["in", "out"]
    points: list[tuple[float, float]] = Field(
        description="(time, value) at the end of each interval between polls; time in seconds since the epoch."
    )


class Chart(BaseModel):
    """A few ports' rates over the last minutes, for the page to draw and keep up to date."""

    title: str
    metric: Metric
    unit: str
    minutes: float
    interval: float = Field(description="Seconds between polls: how often the chart has something new.")
    ports: list[str] = Field(description="The ports charted, as device:port, in the order they are colored.")
    lines: list[ChartLine]


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
    speed_bps: int | None


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
        speed_bps=later.counters.speed_bps,
    )


def speed_name(bps: int | None) -> str | None:
    """Write a port speed the way vendors do: 25G, 100M."""
    if not bps:
        return None
    for unit, size in (("T", 10**12), ("G", 10**9), ("M", 10**6)):
        if bps >= size and bps % size == 0:
            return f"{bps // size}{unit}"
    return f"{bps}bps"


def rates_since(samples: Sequence[Sample], since: float) -> list[Rate]:
    """The rates between one port's samples, for the intervals that end after a moment."""
    return [found for earlier, later in pairwise(samples) if later.at > since and (found := rate(earlier, later))]


def summarize(samples: Sequence[Sample], since: float) -> InterfaceTraffic | None:
    """What one port's samples say since a moment: None until there are two to compare after it."""
    rates = rates_since(samples, since)
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


def chart_value(found: Rate, metric: Metric, direction: Literal["in", "out"]) -> float | None:
    """One interval's value on a chart, or None for utilization on a port of unknown speed."""
    if metric == "errors":
        return float(found.in_errors + found.in_discards if direction == "in" else found.out_errors + found.out_discards)
    bps = found.in_bps if direction == "in" else found.out_bps
    if metric == "traffic":
        return round(bps / 1e6, 3)
    return round(100 * bps / found.speed_bps, 4) if found.speed_bps else None


def describe_chart(chart: Chart) -> str:
    """What the model reads about a chart it drew: each line's latest value, average, and peak."""
    lines = [
        f"Charted {chart.unit} for {', '.join(chart.ports)} over the last {chart.minutes:g} minutes. The chart is in "
        f"the conversation, and it updates every {chart.interval:g} seconds."
    ]
    for line in chart.lines:
        values = [value for _, value in line.points]
        if not values:
            lines.append(f"{line.port} {line.direction}: nothing yet.")
            continue
        average = sum(values) / len(values)
        lines.append(f"{line.port} {line.direction}: now {values[-1]:g}, average {average:.3g}, peak {max(values):g}.")
    lines.append("Refer to the chart in your answer rather than repeating every number.")
    return "\n".join(lines)


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
        self._management = {
            device.name: platforms[device.platform].management_interfaces
            for device in inventory.devices
            if device.platform in platforms
        }
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

    def ports(self) -> list[tuple[str, str]]:
        """Every port polled so far, as (device, port), in inventory order and then port order."""
        order = {device.name: index for index, device in enumerate(self._inventory.devices)}
        return sorted(self._samples, key=lambda key: (order.get(key[0], len(order)), port_order(key[1])))

    def resolve(self, names: list[str]) -> list[tuple[str, str]]:
        """
        The ports some names mean: device:port (or "device port") for one port, or a
        device for all its ports except management. Raises ChartError naming the ports
        polled when a name matches none of them.
        """
        polled = self.ports()
        found: list[tuple[str, str]] = []
        for name in names:
            device, _, port = name.strip().replace(" ", ":", 1).partition(":")
            if port:
                matches = [(device, port)] if (device, port) in polled else []
            else:
                management = self._management.get(device)
                matches = [key for key in polled if key[0] == device and not (management and management.match(key[1]))]
            if not matches:
                known = ", ".join(f"{device}:{port}" for device, port in polled) or "none yet: polling has just begun"
                raise ChartError(f"No polled port is called '{name}'. Name a device, or one of its ports: {known}.")
            found.extend(match for match in matches if match not in found)
        if len(found) > MAX_CHART_PORTS:
            raise ChartError(
                f"That is {len(found)} ports; a chart has room for {MAX_CHART_PORTS}. Name fewer devices, or single ports."
            )
        return found

    def chart(self, title: str, names: list[str], metric: Metric, minutes: float) -> Chart:
        """Chart some ports, named as resolve() takes them, over the last minutes."""
        ports = self.resolve(names)
        minutes = min(minutes, self.window / 60)
        since = self._clock() - minutes * 60
        lines = []
        for device, port in ports:
            found = rates_since(self._samples[(device, port)], since)
            for direction in ("in", "out"):
                points = [(item.end, value) for item in found if (value := chart_value(item, metric, direction)) is not None]
                lines.append(ChartLine(port=f"{device}:{port}", direction=direction, points=points))
        return Chart(
            title=title,
            metric=metric,
            unit=UNITS[metric],
            minutes=minutes,
            interval=self.interval,
            ports=[f"{device}:{port}" for device, port in ports],
            lines=lines,
        )
