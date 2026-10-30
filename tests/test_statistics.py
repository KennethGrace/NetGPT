"""
Tests for polling interface counters and turning them into rates. The fake platform's
counters grow by the same amount on every read, and a fake clock sets the time
between polls, so the rates are known exactly.
"""

from __future__ import annotations

import asyncio

import pytest

from netgpt.inventory import Inventory
from netgpt.primitives import InterfaceCounters
from netgpt.statistics import ChartError, Poller, rate, speed_name, summarize
from netgpt.store import MemoryStore, Sample

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


def test_a_counter_that_goes_backwards_was_reset_and_that_interval_is_not_measured() -> None:
    before, reset, after = Sample(0, reading(9_000_000)), Sample(30, reading(1_000)), Sample(60, reading(3_751_000))
    assert rate(before, reset) is None
    summary = summarize([before, reset, after], since=0)
    assert summary is not None and (summary.in_mbps, summary.peak_in_mbps, summary.minutes) == (1.0, 1.0, 0.5)


def test_the_latest_rate_is_the_last_interval_and_the_peak_and_counts_cover_the_window() -> None:
    samples = [
        Sample(0, reading(0)),
        Sample(30, reading(37_500_000, in_errors=5)),
        Sample(60, reading(41_250_000, in_errors=6, transitions=2)),
    ]
    summary = summarize(samples, since=0)
    assert summary is not None
    assert (summary.in_mbps, summary.peak_in_mbps, summary.in_errors, summary.flaps) == (1.0, 10.0, 6, 2)
    # Asking about only the last half minute leaves the busy interval out.
    recent = summarize(samples, since=45)
    assert recent is not None and (recent.peak_in_mbps, recent.in_errors) == (1.0, 1)


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
