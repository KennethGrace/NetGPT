"""
Tests for polling interface counters and turning them into rates. The fake platform's
counters grow by the same amount on every read, and a fake clock sets the time
between polls, so the rates are known exactly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from netgpt.inventory import Inventory
from netgpt.primitives import InterfaceCounters
from netgpt.statistics import ChartError, Poller, describe_chart, duration, per, speed_name, step_for
from netgpt.store import MemoryStore, Rate, Sample, StoreError

from fakes import FakePlatform


class Clock:
    """The time each poll arrives, set by the test."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def reading(in_octets: int, in_errors: int = 0, transitions: int = 0) -> InterfaceCounters:
    return InterfaceCounters(
        device="leaf1",
        interface="ethernet-1/1",
        oper_state="up",
        speed_bps=10**9,
        in_octets=in_octets,
        out_octets=0,
        in_packets=0,
        out_packets=0,
        in_errors=in_errors,
        out_errors=0,
        in_discards=0,
        out_discards=0,
        carrier_transitions=transitions,
    )


def poller(inventory: Inventory, clock: Clock, window: float = 3600.0) -> Poller:
    return Poller(inventory, platforms={"fake": FakePlatform}, interval=30, store=MemoryStore(window), clock=clock)


@pytest.mark.anyio
async def test_rates_come_from_the_difference_between_two_polls(inventory: Inventory, counter_reads: dict[str, int]) -> None:
    clock = Clock()
    polls = poller(inventory, clock)
    await polls.poll()
    clock.now += 30
    await polls.poll()
    items, _ = await polls.traffic(["leaf1"], minutes=15)
    [port] = items
    assert (port.interface, port.speed, port.minutes) == ("ethernet-1/1", "1G", 0.5)
    assert (port.in_mbps, port.out_mbps, port.in_percent, port.out_percent) == (10.0, 1.0, 1.0, 0.1)
    assert (port.in_errors, port.flaps) == (2, 0)


@pytest.mark.anyio
async def test_a_device_that_fails_or_cannot_be_polled_says_why_and_the_rest_still_report(
    inventory: Inventory, counter_reads: dict[str, int]
) -> None:
    clock = Clock()
    polls = poller(inventory, clock)
    await polls.poll()
    _, errors = await polls.traffic(None, minutes=15)
    # One poll is not enough for a rate.
    assert {error.device for error in errors} == {"leaf1", "leaf2", "broken", "mystery"}
    assert "No rates yet" in next(error.error for error in errors if error.device == "leaf1")
    clock.now += 30
    await polls.poll()
    items, errors = await polls.traffic(None, minutes=15)
    assert [item.device for item in items] == ["leaf1", "leaf2"]
    reasons = {error.device: error.error for error in errors}
    assert "connection refused" in reasons["broken"] and "supported platforms: fake" in reasons["mystery"]


class FullStore(MemoryStore):
    """A store that can't keep anything."""

    async def _keep(self, samples: Sequence[Sample], rates: Sequence[Rate | None]) -> None:
        raise StoreError("could not extend file: No space left on device")


@pytest.mark.anyio
async def test_counters_the_store_cannot_keep_are_reported_for_each_device_and_polling_goes_on(
    inventory: Inventory, counter_reads: dict[str, int]
) -> None:
    polls = Poller(inventory, platforms={"fake": FakePlatform}, interval=30, store=FullStore(), clock=Clock())
    await polls.poll()
    await polls.poll()
    assert counter_reads["leaf1"] == 2
    _, errors = await polls.traffic(["leaf1"], minutes=15)
    assert [error.error for error in errors] == [
        "leaf1 answered, but its counters couldn't be kept: could not extend file: No space left on device"
    ]


@pytest.mark.anyio
async def test_a_counter_that_goes_backwards_was_reset_and_that_interval_is_not_measured(inventory: Inventory) -> None:
    clock = Clock(60)
    polls = poller(inventory, clock)
    for at, octets in ((0, 9_000_000), (30, 1_000), (60, 3_751_000)):
        await polls.store.add(at, [reading(octets)])
    [summary], _ = await polls.traffic(["leaf1"], minutes=15)
    assert (summary.in_mbps, summary.peak_in_mbps, summary.minutes) == (1.0, 1.0, 0.5)


@pytest.mark.anyio
async def test_the_latest_rate_is_the_last_interval_and_the_average_peak_and_counts_cover_the_window(
    inventory: Inventory,
) -> None:
    clock = Clock(60)
    polls = poller(inventory, clock)
    await polls.store.add(0, [reading(0)])
    await polls.store.add(30, [reading(37_500_000, in_errors=5)])
    await polls.store.add(60, [reading(41_250_000, in_errors=6, transitions=2)])
    [summary], _ = await polls.traffic(["leaf1"], minutes=1)
    assert (summary.in_mbps, summary.average_in_mbps, summary.peak_in_mbps) == (1.0, 5.5, 10.0)
    assert (summary.in_errors, summary.flaps, summary.oper_state) == (6, 2, "up")
    # Asking about only the last quarter minute leaves the busy interval out.
    [recent], _ = await polls.traffic(["leaf1"], minutes=0.25)
    assert (recent.average_in_mbps, recent.peak_in_mbps, recent.in_errors) == (1.0, 1.0, 1)


@pytest.mark.anyio
async def test_polls_more_than_three_intervals_apart_are_not_measured(inventory: Inventory, counter_reads: dict[str, int]) -> None:
    clock = Clock()
    polls = poller(inventory, clock)
    await polls.poll()
    clock.now += 91
    await polls.poll()
    _, errors = await polls.traffic(["leaf1"], minutes=15)
    assert "No rates yet" in errors[0].error
    clock.now += 90
    await polls.poll()
    [port], _ = await polls.traffic(["leaf1"], minutes=15)
    assert port.minutes == 1.5


@pytest.mark.anyio
async def test_a_long_window_is_read_in_rollups_and_its_peak_is_still_the_busiest_interval(inventory: Inventory) -> None:
    clock = Clock(6 * 3600)
    polls = poller(inventory, clock, window=7 * 24 * 3600)
    # Six hours of a poll every 30 seconds at 1 Mbit/s, but 9 Mbit/s for the half minute to the third hour.
    octets = 0
    for at in range(0, 6 * 3600 + 1, 30):
        octets += 0 if at == 0 else 9 * 3_750_000 if at == 3 * 3600 else 3_750_000
        await polls.store.add(at, [reading(octets, in_errors=at // 3600)])
    [summary], _ = await polls.traffic(["leaf1"], minutes=6 * 60)
    assert (summary.in_mbps, summary.peak_in_mbps, summary.in_errors) == (1.0, 9.0, 6)
    assert summary.average_in_mbps == pytest.approx(1 + 8 / 720, abs=0.001)
    chart = await polls.chart("Six hours", ["leaf1"], "traffic", 6 * 60)
    # 720 polls are too many points for a line; 5-minute averages are 72, and one more for the 5 minutes under way.
    assert (chart.step, len(chart.lines[0].points), chart.unit) == (300, 73, "Mbit/s")
    assert max(value for _, value in chart.lines[0].points) == pytest.approx(1 + 8 / 10)
    assert "over the last 6 hours. Each point is the average over 5 minutes." in describe_chart(chart)
    errors = await polls.chart("Errors", ["leaf1"], "errors", 6 * 60)
    assert errors.unit == "errors and discards per 5 minutes"
    assert sum(value for _, value in errors.lines[0].points) == 6


def test_a_window_is_read_poll_by_poll_until_its_lines_would_be_too_long() -> None:
    assert [step_for(seconds, 30) for seconds in (3600, 400 * 30, 400 * 30 + 1, 7 * 86400, 730 * 86400)] == [
        None, None, 300, 1800, 2 * 86400
    ]
    assert step_for(86400, 300) == None and step_for(86400, 10) == 300


def test_lengths_of_time_are_said_as_people_say_them() -> None:
    assert [duration(seconds) for seconds in (150, 900, 3600, 90 * 60, 7 * 86400)] == [
        "2.5 minutes", "15 minutes", "1 hour", "90 minutes", "7 days"
    ]
    assert [per(step) for step in (None, 300, 3600, 3 * 3600, 86400)] == ["poll", "5 minutes", "hour", "3 hours", "day"]


@pytest.mark.anyio
async def test_figures_go_back_no_further_than_the_window(inventory: Inventory, counter_reads: dict[str, int]) -> None:
    clock = Clock(0)
    polls = poller(inventory, clock, window=60)
    for at in (0, 30, 60, 90):
        clock.now = at
        await polls.poll()
    items, _ = await polls.traffic(["leaf1"], minutes=60)
    assert items[0].minutes == 1.0
    # The poll at 0 seconds is more than a window older than the last, and is forgotten.
    assert [sample.at for sample in await polls.store.samples("leaf1", "ethernet-1/1", since=0)] == [30, 60, 90]


@pytest.mark.anyio
async def test_polling_runs_in_the_background_while_anything_needs_it(inventory: Inventory, counter_reads: dict[str, int]) -> None:
    polls = Poller(inventory, platforms={"fake": FakePlatform}, interval=0.01)
    async with polls.running():
        async with polls.running():
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.05)
        assert counter_reads["leaf1"] >= 3
    reads = counter_reads["leaf1"]
    await asyncio.sleep(0.05)
    assert counter_reads["leaf1"] == reads


def test_port_speeds_are_written_the_way_vendors_write_them() -> None:
    assert [speed_name(speed) for speed in (25 * 10**9, 100 * 10**6, 10**12, None)] == ["25G", "100M", "1T", None]


@pytest.mark.anyio
async def test_a_chart_draws_each_port_both_ways_in_the_metric_asked_for(inventory: Inventory, counter_reads: dict[str, int]) -> None:
    clock = Clock()
    polls = poller(inventory, clock)
    for _ in range(3):
        await polls.poll()
        clock.now += 30
    clock.now -= 30
    traffic = await polls.chart("Leaves", ["leaf1", "leaf2:ethernet-1/1"], "traffic", 15)
    assert traffic.ports == ["leaf1:ethernet-1/1", "leaf2:ethernet-1/1"] and traffic.unit == "Mbit/s"
    assert [(line.port, line.direction, line.points) for line in traffic.lines[:2]] == [
        ("leaf1:ethernet-1/1", "in", [(1030, 10.0), (1060, 10.0)]),
        ("leaf1:ethernet-1/1", "out", [(1030, 1.0), (1060, 1.0)]),
    ]
    assert [value for _, value in (await polls.chart("t", ["leaf1"], "percent", 15)).lines[0].points] == [1.0, 1.0]
    assert [value for _, value in (await polls.chart("t", ["leaf1"], "errors", 15)).lines[0].points] == [2.0, 2.0]


@pytest.mark.anyio
async def test_a_chart_of_names_nobody_polls_or_too_many_ports_is_refused(inventory: Inventory, counter_reads: dict[str, int]) -> None:
    polls = poller(inventory, Clock())
    await polls.poll()
    with pytest.raises(ChartError, match="Name a device, or one of its ports: leaf1:ethernet-1/1, leaf2:ethernet-1/1."):
        await polls.chart("t", ["leaf1:ethernet-1/9"], "traffic", 15)
    await polls.store.add(1000, [reading(0).model_copy(update={"interface": f"ethernet-1/{port}"}) for port in range(2, 8)])
    with pytest.raises(ChartError, match="That is 7 ports; a chart has room for 6"):
        await polls.chart("t", ["leaf1"], "traffic", 15)
