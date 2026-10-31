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
from netgpt.store import MemoryStore, Store, StoreError, rate

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


def counters(
    device: str, interface: str, in_octets: int, carrier_transitions: int | None = 0, in_errors: int = 0
) -> InterfaceCounters:
    return InterfaceCounters(
        device=device, interface=interface, oper_state="up", speed_bps=10**9, in_octets=in_octets, out_octets=0,
        in_packets=0, out_packets=0, in_errors=in_errors, out_errors=0, in_discards=0, out_discards=0,
        carrier_transitions=carrier_transitions,
    )


# A megabit per second, for a minute, in octets.
MEGABIT_MINUTE = 7_500_000


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
async def test_each_interval_between_two_polls_is_kept_as_its_rate(make_store: MakeStore) -> None:
    store = await make_store(3600)
    for at, octets in ((0, 0), (30, 3_750_000), (60, 7_500_000), (90, 7_500_000)):
        await store.add(at, [counters("leaf1", "ethernet-1/1", octets)])
    found = await store.rates("leaf1", "ethernet-1/1", since=0)
    assert [(found.start, found.end, found.in_bps, found.peak_in_bps) for found in found] == [
        (0, 30, 1e6, 1e6), (30, 60, 1e6, 1e6), (60, 90, 0, 0)
    ]
    assert (found[0].seconds, found[0].flaps, found[0].speed_bps) == (30, 0, 10**9)
    assert [found.end for found in await store.rates("leaf1", "ethernet-1/1", since=60)] == [90]
    assert await store.rates("leaf1", "ethernet-1/9", since=0) == []


@pytest.mark.anyio
async def test_an_interval_with_a_reset_or_longer_than_the_longest_is_not_measured_but_its_samples_are_kept(
    make_store: MakeStore,
) -> None:
    store = await make_store(3600)
    await store.add(0, [counters("leaf1", "ethernet-1/1", 9_000_000)])
    await store.add(30, [counters("leaf1", "ethernet-1/1", 1_000)])  # The device restarted.
    await store.add(60, [counters("leaf1", "ethernet-1/1", 3_751_000)], longest=90)
    await store.add(300, [counters("leaf1", "ethernet-1/1", 4_000_000)], longest=90)  # Four minutes unpolled.
    assert [(found.start, found.end, found.in_bps) for found in await store.rates("leaf1", "ethernet-1/1", since=0)] == [
        (30, 60, 1e6)
    ]
    assert len(await store.samples("leaf1", "ethernet-1/1", since=-1)) == 4


@pytest.mark.anyio
async def test_rollups_average_the_intervals_that_end_in_each_step_and_keep_the_busiest(make_store: MakeStore) -> None:
    store = await make_store(3600)
    # A poll a minute from 0 to 600 seconds: 1 Mbit/s, but 7 in the minute to 240, and an error in the minute to 420.
    octets = errors = 0
    for at in range(0, 660, 60):
        octets += 0 if at == 0 else 7 * MEGABIT_MINUTE if at == 240 else MEGABIT_MINUTE
        errors += at == 420
        await store.add(at, [counters("leaf1", "ethernet-1/1", octets, in_errors=errors)])

    def told(found: list) -> list[tuple]:
        return [(found.start, found.end, found.seconds, found.in_bps, found.peak_in_bps, found.in_errors) for found in found]

    # The minutes to 60, 120, 180, and 240 end in the first 5 minutes, the next five in the second, and one in the third.
    assert told(await store.rates("leaf1", "ethernet-1/1", since=0, step=300)) == [
        (0, 240, 240, 2.5e6, 7e6, 0), (240, 540, 300, 1e6, 1e6, 1), (540, 600, 60, 1e6, 1e6, 0)
    ]
    # A step begins at or after the moment: the one under way at 1 second is left out.
    assert [found.end for found in await store.rates("leaf1", "ethernet-1/1", since=1, step=300)] == [540, 600]
    [hour] = await store.rates("leaf1", "ethernet-1/1", since=0, step=3600)
    assert (hour.start, hour.end, hour.seconds, hour.peak_in_bps, hour.in_errors) == (0, 600, 600, 7e6, 1)
    assert (hour.in_bps, hour.flaps, hour.speed_bps) == (pytest.approx(1.6e6), 0, 10**9)
    ten = await store.rates("leaf1", "ethernet-1/1", since=0, step=600)
    assert [(found.end, found.seconds, found.in_bps) for found in ten] == [(540, 540, pytest.approx(15e6 / 9)), (600, 60, 1e6)]
    with pytest.raises(ValueError, match="whole number of 300 seconds"):
        await store.rates("leaf1", "ethernet-1/1", since=0, step=90)


@pytest.mark.anyio
async def test_a_rollup_counts_flaps_only_if_every_interval_in_it_did(make_store: MakeStore) -> None:
    store = await make_store(3600)
    for at, transitions in ((0, 4), (60, 5), (120, None), (180, 6), (360, 8), (420, 9)):
        await store.add(at, [counters("leaf1", "ethernet-1/1", 0, carrier_transitions=transitions)])
    assert [found.flaps for found in await store.rates("leaf1", "ethernet-1/1", since=0)] == [1, None, None, 2, 1]
    assert [found.flaps for found in await store.rates("leaf1", "ethernet-1/1", since=0, step=300)] == [None, 3]


def test_a_counter_that_goes_backwards_was_reset_and_that_interval_is_not_measured() -> None:
    from netgpt.store import Sample

    before, reset = Sample(0, counters("leaf1", "ethernet-1/1", 9_000_000)), Sample(30, counters("leaf1", "ethernet-1/1", 1))
    assert rate(before, reset) is None and rate(reset, before) is None
    assert rate(before, Sample(400, counters("leaf1", "ethernet-1/1", 9_000_000)), longest=90) is None


@pytest.mark.anyio
async def test_samples_and_rates_older_than_the_window_are_forgotten_in_memory() -> None:
    store = MemoryStore(60)
    for at in (0, 30, 60, 90):
        await store.add(at, [counters("leaf1", "ethernet-1/1", at)])
    assert [sample.at for sample in await store.samples("leaf1", "ethernet-1/1", since=0)] == [30, 60, 90]
    assert [found.end for found in await store.rates("leaf1", "ethernet-1/1", since=-1)] == [30, 60, 90]
    assert store.reach == 60


@pytest.mark.anyio
async def test_timescaledb_drops_samples_older_than_the_window(timescale: MakeStore) -> None:
    import psycopg

    from netgpt.store import TimescaleStore

    store = await timescale(3600)
    assert isinstance(store, TimescaleStore)
    # The store's URL puts the test's schema first; ask TimescaleDB what it will drop from it, and when.
    async with await psycopg.AsyncConnection.connect(store.url) as connection:
        found = await connection.execute(
            "SELECT hypertable_name, config->>'drop_after' FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention' AND hypertable_schema = current_schema()"
        )
        assert dict(await found.fetchall()) == {
            "interface_samples": "01:00:00", "interface_rates": "01:00:00", "interface_rates_5m": "90 days",
            "interface_rates_1h": "730 days",
        }
    assert store.reach == 730 * 24 * 3600


@pytest.mark.anyio
async def test_timescaledb_refreshes_each_rollup_no_further_back_than_its_source_keeps(timescale: MakeStore) -> None:
    import psycopg

    from netgpt.store import TimescaleStore

    store = await timescale(3600)
    assert isinstance(store, TimescaleStore)
    async with await psycopg.AsyncConnection.connect(store.url) as connection:
        found = await connection.execute(
            "SELECT hypertable_name, config->>'start_offset', config->>'end_offset' FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_refresh_continuous_aggregate' AND hypertable_schema = current_schema()"
        )
        refreshes = {view: (start, end) for view, start, end in await found.fetchall()}
    # Half the hour the intervals are kept, and a third of the 90 days the 5-minute rollups are.
    assert refreshes == {"interface_rates_5m": ("00:30:00", "00:05:00"), "interface_rates_1h": ("30 days", "01:00:00")}


@pytest.mark.anyio
async def test_samples_in_timescaledb_survive_a_restart(timescale: MakeStore) -> None:
    before = await timescale(3600)
    await before.add(0, [counters("leaf1", "ethernet-1/1", 1)])
    await before.add(30, [counters("leaf1", "ethernet-1/1", 2)])
    await before.close()
    after = await timescale(3600)
    assert await after.ports() == [("leaf1", "ethernet-1/1")]
    assert [sample.counters.in_octets for sample in await after.samples("leaf1", "ethernet-1/1", since=0)] == [1, 2]
    # The first interval after the restart starts from the last sample before it.
    await after.add(60, [counters("leaf1", "ethernet-1/1", 3_750_002)])
    assert [(found.start, found.end, found.in_bps) for found in await after.rates("leaf1", "ethernet-1/1", since=30)] == [
        (30, 60, 1e6)
    ]


@pytest.mark.anyio
async def test_a_timescaledb_store_keeps_working_after_the_database_drops_its_connections(timescale: MakeStore) -> None:
    import psycopg

    store = await timescale(3600)
    await store.add(0, [counters("leaf1", "ethernet-1/1", 1)])
    # As a database restart would: end every connection the store holds. The tests' database is theirs alone.
    async with await psycopg.AsyncConnection.connect(TEST_STORE, autocommit=True) as connection:
        await connection.execute(
            "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity "
            "WHERE datname = current_database() AND usename = current_user AND pid <> pg_backend_pid()"
        )
    await store.add(30, [counters("leaf1", "ethernet-1/1", 2)])
    assert [sample.counters.in_octets for sample in await store.samples("leaf1", "ethernet-1/1", since=0)] == [1, 2]


@pytest.mark.anyio
async def test_two_timescaledb_stores_opening_at_once_set_up_one_database(timescale: MakeStore) -> None:
    import asyncio

    import psycopg

    from netgpt.store import TimescaleStore

    stores = await asyncio.gather(*(timescale(3600) for _ in range(4)))
    store = stores[0]
    assert isinstance(store, TimescaleStore)
    async with await psycopg.AsyncConnection.connect(store.url) as connection:
        found = await connection.execute(
            "SELECT hypertable_name, proc_name, count(*) FROM timescaledb_information.jobs "
            "WHERE hypertable_schema = current_schema() GROUP BY 1, 2"
        )
        jobs = await found.fetchall()
    # Each table's retention policy and each rollup's refresh, once, however many stores set them.
    assert len(jobs) == 6 and {count for *_, count in jobs} == {1}


def test_a_store_url_with_a_password_in_it_is_refused_without_saying_the_password() -> None:
    pytest.importorskip("psycopg")
    from netgpt.store import TimescaleStore

    with pytest.raises(StoreError, match="~/.pgpass") as refused:
        TimescaleStore("postgresql://netgpt:hunter2@127.0.0.1:5432/netgpt")
    assert "hunter2" not in str(refused.value)
