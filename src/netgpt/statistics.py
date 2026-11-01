"""
The statistics module polls every device's interface counters on an interval and
turns them into rates: bits per second each way, a share of the port's speed, and
the errors, discards, and link flaps counted between polls.

Counters only grow, so a rate is the difference between two readings over the time
between them. A counter that goes backwards was reset (the device restarted, or
someone cleared it), and that interval isn't measured. The samples are kept in a
store (netgpt.store), which works out each interval's rate as it arrives: in memory
for a window, unless the poller is given another. A window too long to read poll by
poll is read from the store's rollups, in the shortest step that keeps a chart's
line to a few hundred points.

A Chart is a few ports' rates over time, which the web page draws in the
conversation and keeps asking for again as the poller takes new samples.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from netgpt.inventory import Inventory, InventoryError
from netgpt.platforms import PLATFORMS, Platform, PlatformError, connect
from netgpt.primitives import DeviceError, InterfaceTraffic
from netgpt.store import MemoryStore, Rate, Store, StoreError, combine

logger = logging.getLogger("netgpt")

DEFAULT_INTERVAL = 30.0
# The second poll comes this soon after the first, so there are rates within seconds of starting.
FIRST_INTERVAL = 5.0
# Two readings more than this many intervals apart aren't measured: polls were missed, or nothing was polling.
LONGEST_GAP = 3
# A window is read poll by poll while that is at most this many points a line, and from rollups past it, in the
# shortest of these steps (seconds) that keeps to it.
MOST_POINTS = 400
STEPS = (300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600, 48 * 3600)

Metric = Literal["traffic", "percent", "errors"]
UNITS: dict[str, str] = {"traffic": "Mbit/s", "percent": "% of port speed", "errors": "errors and discards per {step}"}
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
    step: int | None = Field(
        None, description="Seconds each point covers when the window is read from rollups; none when each is one poll's."
    )
    ports: list[str] = Field(description="The ports charted, as device:port, in the order they are colored.")
    lines: list[ChartLine]


def speed_name(bps: int | None) -> str | None:
    """Write a port speed the way vendors do: 25G, 100M."""
    if not bps:
        return None
    for unit, size in (("T", 10**12), ("G", 10**9), ("M", 10**6)):
        if bps >= size and bps % size == 0:
            return f"{bps // size}{unit}"
    return f"{bps}bps"


def duration(seconds: float) -> str:
    """A length of time as people say it: 15 minutes, 3 hours, 7 days."""
    for name, size in (("day", 24 * 3600), ("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            count = int(seconds // size)
            return f"{count} {name}" + ("s" if count != 1 else "")
    return f"{seconds / 60:g} minutes"


def per(step: int | None) -> str:
    """What one point covers: a poll, 5 minutes, an hour."""
    if step is None:
        return "poll"
    return duration(step).removeprefix("1 ")


def poll_by_poll(interval: float) -> float:
    """The longest window, in seconds, read poll by poll at an interval."""
    return MOST_POINTS * interval


def step_for(seconds: float, interval: float) -> int | None:
    """The step a window is read in: none (poll by poll) while it fits MOST_POINTS, or else the shortest that does."""
    if seconds <= poll_by_poll(interval):
        return None
    return next((step for step in STEPS if seconds / step <= MOST_POINTS), STEPS[-1])


def summarize(device: str, port: str, rates: Sequence[Rate], latest: Rate, oper_state: str | None) -> InterfaceTraffic:
    """
    What one port's rates over a window say: rates read poll by poll or in rollups,
    and the latest interval between polls.
    """
    whole = combine(rates)

    def mbps(bps: float) -> float:
        return round(bps / 1e6, 3)

    def percent(bps: float) -> float | None:
        return round(100 * bps / latest.speed_bps, 3) if latest.speed_bps else None

    return InterfaceTraffic(
        device=device,
        interface=port,
        oper_state=oper_state,
        speed=speed_name(latest.speed_bps),
        minutes=round((whole.end - whole.start) / 60, 1),
        in_mbps=mbps(latest.in_bps),
        out_mbps=mbps(latest.out_bps),
        in_percent=percent(latest.in_bps),
        out_percent=percent(latest.out_bps),
        average_in_mbps=mbps(whole.in_bps),
        average_out_mbps=mbps(whole.out_bps),
        peak_in_mbps=mbps(whole.peak_in_bps),
        peak_out_mbps=mbps(whole.peak_out_bps),
        in_errors=whole.in_errors,
        out_errors=whole.out_errors,
        in_discards=whole.in_discards,
        out_discards=whole.out_discards,
        flaps=whole.flaps,
    )


def port_order(name: str) -> list[str | int]:
    """Sort ports as people number them: ethernet-1/2 before ethernet-1/10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def chart_value(found: Rate, metric: Metric, direction: Literal["in", "out"]) -> float | None:
    """One point's value on a chart (an interval's, or a rollup's average), or None for utilization at an unknown speed."""
    if metric == "errors":
        return float(found.in_errors + found.in_discards if direction == "in" else found.out_errors + found.out_discards)
    bps = found.in_bps if direction == "in" else found.out_bps
    if metric == "traffic":
        return round(bps / 1e6, 3)
    return round(100 * bps / found.speed_bps, 4) if found.speed_bps else None


def describe_chart(chart: Chart) -> str:
    """What the model reads about a chart it drew: each line's latest value, average, and peak."""
    each = f" Each point is the average over {duration(chart.step)}." if chart.step and chart.metric != "errors" else ""
    lines = [
        f"Charted {chart.unit} for {', '.join(chart.ports)} over the last {duration(chart.minutes * 60)}.{each} The "
        f"chart is in the conversation, and it updates every {chart.interval:g} seconds."
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
    seconds, and keeps the samples in a store: the last hour in memory, unless it is
    given another. Run it with running(), which polls in the background until the
    block ends.
    """

    def __init__(
        self,
        inventory: Inventory,
        platforms: dict[str, type[Platform]] = PLATFORMS,
        interval: float = DEFAULT_INTERVAL,
        store: Store | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """clock is only for tests, which set the time each sample arrives."""
        self.interval = interval
        self.store = store or MemoryStore()
        self._inventory = inventory
        self._platforms = platforms
        self._clock = clock
        self._management = {
            device.name: platforms[device.platform].management_interfaces
            for device in inventory.devices
            if device.platform in platforms
        }
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
            try:
                await self.store.add(self._clock(), readings, longest=LONGEST_GAP * self.interval)
            except StoreError as error:
                logger.warning("Keeping %s's counters failed: %s", name, error)
                self._errors[name] = f"{name} answered, but its counters couldn't be kept: {error}"
                return
            self._errors.pop(name, None)

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
        """
        Poll in the background while the block runs, with the store open. Nested blocks
        share one poller. Raises StoreError if the store can't be opened.
        """
        if self._users == 0:
            await self.store.open()
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
                await self.store.close()

    @property
    def reach(self) -> float:
        """How far back, in seconds, statistics reach: as far as the store keeps anything."""
        return self.store.reach

    async def summary(self, device: str, port: str, since: float, step: int | None) -> InterfaceTraffic | None:
        """One port's traffic since a moment, read in a step: None until there are two polls to compare after it."""
        rates = await self.store.rates(device, port, since, step)
        if not rates:
            return None
        latest = rates[-1]
        if step is not None:
            # The latest interval between polls ends where the latest rollup does. Once the store has dropped the
            # polls, the rollup's average is the latest there is.
            latest = (await self.store.rates(device, port, since=latest.end - 0.001) or [latest])[-1]
        last = await self.store.samples(device, port, since=self._clock())
        return summarize(device, port, rates, latest, last[-1].counters.oper_state if last else None)

    async def traffic(self, devices: list[str] | None, minutes: float) -> tuple[list[InterfaceTraffic], list[DeviceError]]:
        """Each port's traffic over the last minutes, on the named devices or all of them, and why any device has none."""
        known = [device.name for device in self._inventory.devices]
        names = devices or known
        seconds = min(minutes * 60, self.reach)
        since, step = self._clock() - seconds, step_for(seconds, self.interval)
        items: list[InterfaceTraffic] = []
        errors: list[DeviceError] = []
        polled = await self.ports()
        for name in names:
            try:
                self._inventory.get(name)
            except InventoryError as error:
                errors.append(DeviceError(device=name, error=str(error)))
                continue
            ports = [port for device, port in polled if device == name]
            found = [summary for port in ports if (summary := await self.summary(name, port, since, step))]
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

    async def ports(self) -> list[tuple[str, str]]:
        """Every port polled so far, as (device, port), in inventory order and then port order."""
        order = {device.name: index for index, device in enumerate(self._inventory.devices)}
        return sorted(await self.store.ports(), key=lambda key: (order.get(key[0], len(order)), port_order(key[1])))

    async def resolve(self, names: list[str]) -> list[tuple[str, str]]:
        """
        The ports some names mean: device:port (or "device port") for one port, or a
        device for all its ports except management. Raises ChartError naming the ports
        polled when a name matches none of them.
        """
        polled = await self.ports()
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

    async def chart(self, title: str, names: list[str], metric: Metric, minutes: float) -> Chart:
        """Chart some ports, named as resolve() takes them, over the last minutes."""
        ports = await self.resolve(names)
        seconds = min(minutes * 60, self.reach)
        since, step = self._clock() - seconds, step_for(seconds, self.interval)
        lines = []
        for device, port in ports:
            found = await self.store.rates(device, port, since, step)
            for direction in ("in", "out"):
                points = [(item.end, value) for item in found if (value := chart_value(item, metric, direction)) is not None]
                lines.append(ChartLine(port=f"{device}:{port}", direction=direction, points=points))
        return Chart(
            title=title,
            metric=metric,
            unit=UNITS[metric].format(step=per(step)),
            minutes=seconds / 60,
            interval=self.interval,
            step=step,
            ports=[f"{device}:{port}" for device, port in ports],
            lines=lines,
        )
