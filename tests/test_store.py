"""
Tests for the stores that keep the poller's samples. Each test of what every store
must do runs against every kind of store, so the TimescaleDB store answers exactly
as the one in memory. The TimescaleDB store is tested only when NETGPT_TEST_STORE
names a database the tests may write to (postgresql://netgpt@127.0.0.1:5432/netgpt_test
with dev/compose.yml); each test works in a schema of its own, dropped afterwards.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from netgpt.primitives import InterfaceCounters
from netgpt.store import MemoryStore, Store, StoreError

TEST_STORE = os.environ.get("NETGPT_TEST_STORE")

MakeStore = Callable[[float], Awaitable[Store]]


@contextlib.asynccontextmanager
async def timescale_stores() -> AsyncIterator[MakeStore]:
    """Makes TimescaleDB stores in a schema of the test's own, given the window each keeps."""
    import psycopg
    from psycopg.conninfo import make_conninfo

    from netgpt.store import TimescaleStore

    assert TEST_STORE
    schema = f"test_{secrets.token_hex(4)}"
    async with await psycopg.AsyncConnection.connect(TEST_STORE, autocommit=True) as connection:
        # In public, so dropping the test's schema leaves it for the next test.
        await connection.execute("CREATE EXTENSION IF NOT EXISTS timescaledb SCHEMA public")
        await connection.execute(f"CREATE SCHEMA {schema}")
    url = make_conninfo(TEST_STORE, options=f"-c search_path={schema},public")
    opened: list[Store] = []

    async def make(window: float) -> Store:
        store = TimescaleStore(url, window)
        await store.open()
        opened.append(store)
        return store

    try:
        yield make
    finally:
        for store in opened:
            await store.close()
        async with await psycopg.AsyncConnection.connect(TEST_STORE, autocommit=True) as connection:
            await connection.execute(f"DROP SCHEMA {schema} CASCADE")


async def memory_store(window: float) -> Store:
    return MemoryStore(window)


@pytest.fixture
async def timescale() -> AsyncIterator[MakeStore]:
    """Makes TimescaleDB stores, given the window each keeps; skipped without a test database."""
    if not TEST_STORE:
        pytest.skip("Set NETGPT_TEST_STORE to a TimescaleDB URL to test the TimescaleDB store.")
    async with timescale_stores() as make:
        yield make


@pytest.fixture(params=["memory", "timescale"])
async def make_store(request: pytest.FixtureRequest) -> AsyncIterator[MakeStore]:
    """Makes a store of one kind, given the window it keeps."""
    if request.param == "memory":
        yield memory_store
        return
    if not TEST_STORE:
        pytest.skip("Set NETGPT_TEST_STORE to a TimescaleDB URL to test the TimescaleDB store.")
    async with timescale_stores() as make:
        yield make


def counters(device: str, interface: str, in_octets: int, carrier_transitions: int | None = 0) -> InterfaceCounters:
    return InterfaceCounters(
        device=device, interface=interface, oper_state="up", speed_bps=10**9, in_octets=in_octets, out_octets=0,
        in_packets=0, out_packets=0, in_errors=0, out_errors=0, in_discards=0, out_discards=0,
        carrier_transitions=carrier_transitions,
    )


@pytest.mark.anyio
async def test_samples_start_with_the_last_one_before_the_moment_so_the_first_interval_counts(make_store: MakeStore) -> None:
    store = await make_store(3600)
    for at in (0, 30, 60, 90):
        await store.add(at, [counters("leaf1", "ethernet-1/1", at * 1000)])
    assert [sample.at for sample in await store.samples("leaf1", "ethernet-1/1", since=45)] == [30, 60, 90]
    assert [sample.at for sample in await store.samples("leaf1", "ethernet-1/1", since=60)] == [60, 90]
    assert [sample.at for sample in await store.samples("leaf1", "ethernet-1/1", since=-10)] == [0, 30, 60, 90]
    assert [sample.counters.in_octets for sample in await store.samples("leaf1", "ethernet-1/1", since=60)] == [60_000, 90_000]


@pytest.mark.anyio
async def test_every_port_of_every_device_polled_is_kept_apart(make_store: MakeStore) -> None:
    store = await make_store(3600)
    await store.add(0, [counters("leaf1", "ethernet-1/1", 1), counters("leaf1", "ethernet-1/2", 2)])
    await store.add(0, [counters("leaf2", "ethernet-1/1", 3)])
    assert sorted(await store.ports()) == [("leaf1", "ethernet-1/1"), ("leaf1", "ethernet-1/2"), ("leaf2", "ethernet-1/1")]
    [sample] = await store.samples("leaf2", "ethernet-1/1", since=0)
    assert sample.counters.in_octets == 3
    assert await store.samples("leaf2", "ethernet-1/9", since=0) == []


@pytest.mark.anyio
async def test_counters_come_back_as_the_device_reported_them(make_store: MakeStore) -> None:
    store = await make_store(3600)
    # An unsigned 64-bit counter near its top, past what a signed one holds, and a port that doesn't count flaps.
    reading = counters("leaf1", "ethernet-1/1", 2**64 - 1, carrier_transitions=None)
    await store.add(1_730_000_000.25, [reading])
    [sample] = await store.samples("leaf1", "ethernet-1/1", since=0)
    assert (sample.at, sample.counters) == (1_730_000_000.25, reading)


@pytest.mark.anyio
async def test_samples_older_than_the_window_are_forgotten_in_memory() -> None:
    store = MemoryStore(60)
    for at in (0, 30, 60, 90):
        await store.add(at, [counters("leaf1", "ethernet-1/1", at)])
    assert [sample.at for sample in await store.samples("leaf1", "ethernet-1/1", since=0)] == [30, 60, 90]


@pytest.mark.anyio
async def test_timescaledb_drops_samples_older_than_the_window(timescale: MakeStore) -> None:
    import psycopg

    from netgpt.store import TimescaleStore

    store = await timescale(3600)
    assert isinstance(store, TimescaleStore)
    # The store's URL puts the test's schema first; ask TimescaleDB what it will drop from it, and when.
    async with await psycopg.AsyncConnection.connect(store.url) as connection:
        found = await connection.execute(
            "SELECT config->>'drop_after' FROM timescaledb_information.jobs "
            "WHERE hypertable_name = 'interface_samples' AND hypertable_schema = current_schema()"
        )
        assert await found.fetchall() == [("01:00:00",)]


@pytest.mark.anyio
async def test_samples_in_timescaledb_survive_a_restart(timescale: MakeStore) -> None:
    before = await timescale(3600)
    await before.add(0, [counters("leaf1", "ethernet-1/1", 1)])
    await before.add(30, [counters("leaf1", "ethernet-1/1", 2)])
    await before.close()
    after = await timescale(3600)
    assert await after.ports() == [("leaf1", "ethernet-1/1")]
    assert [sample.counters.in_octets for sample in await after.samples("leaf1", "ethernet-1/1", since=0)] == [1, 2]


def test_a_store_url_with_a_password_in_it_is_refused_without_saying_the_password() -> None:
    pytest.importorskip("psycopg")
    from netgpt.store import TimescaleStore

    with pytest.raises(StoreError, match="~/.pgpass") as refused:
        TimescaleStore("postgresql://netgpt:hunter2@127.0.0.1:5432/netgpt")
    assert "hunter2" not in str(refused.value)
