"""
Tests for the stores that keep the poller's samples. Each test runs against every
kind of store, so a store backed by a database answers exactly as the one in memory.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from netgpt.primitives import InterfaceCounters
from netgpt.store import MemoryStore, Store

STORES: dict[str, Callable[[float], Store]] = {"memory": MemoryStore}


@pytest.fixture(params=list(STORES))
def make_store(request: pytest.FixtureRequest) -> Callable[[float], Store]:
    """Makes a store of one kind, given the window it keeps."""
    return STORES[request.param]


def counters(device: str, interface: str, in_octets: int) -> InterfaceCounters:
    return InterfaceCounters(
        device=device, interface=interface, in_octets=in_octets, out_octets=0, in_packets=0, out_packets=0,
        in_errors=0, out_errors=0, in_discards=0, out_discards=0,
    )


@pytest.mark.anyio
async def test_samples_start_with_the_last_one_before_the_moment_so_the_first_interval_counts(
    make_store: Callable[[float], Store],
) -> None:
    store = make_store(3600)
    for at in (0, 30, 60, 90):
        await store.add(at, [counters("leaf1", "ethernet-1/1", at * 1000)])
    assert [sample.at for sample in await store.samples("leaf1", "ethernet-1/1", since=45)] == [30, 60, 90]
    assert [sample.at for sample in await store.samples("leaf1", "ethernet-1/1", since=60)] == [60, 90]
    assert [sample.at for sample in await store.samples("leaf1", "ethernet-1/1", since=-10)] == [0, 30, 60, 90]
    assert [sample.counters.in_octets for sample in await store.samples("leaf1", "ethernet-1/1", since=60)] == [60_000, 90_000]


@pytest.mark.anyio
async def test_every_port_of_every_device_polled_is_kept_apart(make_store: Callable[[float], Store]) -> None:
    store = make_store(3600)
    await store.add(0, [counters("leaf1", "ethernet-1/1", 1), counters("leaf1", "ethernet-1/2", 2)])
    await store.add(0, [counters("leaf2", "ethernet-1/1", 3)])
    assert sorted(await store.ports()) == [("leaf1", "ethernet-1/1"), ("leaf1", "ethernet-1/2"), ("leaf2", "ethernet-1/1")]
    [sample] = await store.samples("leaf2", "ethernet-1/1", since=0)
    assert sample.counters.in_octets == 3
    assert await store.samples("leaf2", "ethernet-1/9", since=0) == []


@pytest.mark.anyio
async def test_samples_older_than_the_window_are_forgotten(make_store: Callable[[float], Store]) -> None:
    store = make_store(60)
    for at in (0, 30, 60, 90):
        await store.add(at, [counters("leaf1", "ethernet-1/1", at)])
    assert store.window == 60
    assert [sample.at for sample in await store.samples("leaf1", "ethernet-1/1", since=0)] == [30, 60, 90]
