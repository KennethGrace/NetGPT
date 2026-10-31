"""
The store module keeps the interface counters the poller reads, so that statistics
can be worked out from them afterwards. A Store is where the samples go and where
they are read back. MemoryStore keeps the last window of them in this process, and
nothing survives a restart. TimescaleStore keeps them in TimescaleDB (PostgreSQL),
for days, across restarts (dev/compose.yml runs one).

The store holds counters as the devices reported them, not rates: a rate is the
difference between two readings, and working it out on reading keeps one place
that knows a counter going backwards is a reset.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from netgpt.primitives import InterfaceCounters

# How much history MemoryStore keeps, in seconds: an hour of samples at the default interval is 120 per port.
DEFAULT_WINDOW = 3600.0
# How much raw history TimescaleStore keeps: a week, after which TimescaleDB drops it.
DATABASE_WINDOW = 7 * 24 * 3600.0

# The counters, in InterfaceCounters' order, as each sample's columns.
COUNTERS = (
    "in_octets", "out_octets", "in_packets", "out_packets", "in_errors", "out_errors", "in_discards", "out_discards",
    "carrier_transitions",
)


# The --store option, for netgpt-mcp and netgpt-chat.
STORE_HELP = (
    "Where to keep the polled counters: memory (the last hour, gone on restart; the default), or a TimescaleDB URL "
    "such as postgresql://netgpt@127.0.0.1:5432/netgpt, whose password comes from ~/.pgpass or PGPASSWORD. "
    "dev/compose.yml runs one."
)


class StoreError(Exception):
    """The store couldn't keep or give back samples; the message says why, and never holds a password."""


@dataclass(frozen=True)
class Sample:
    """One port's counters, and when they arrived (seconds since the epoch)."""

    at: float
    counters: InterfaceCounters


class Store(ABC):
    """
    Where the poller's samples are kept. Every method is async, so a store can be a
    database. window is how far back, in seconds, the store keeps samples: no
    statistic reaches further. Open it before use and close it after; the poller
    does both while it runs.
    """

    window: float

    async def open(self) -> None:
        """Get ready to keep and give back samples. Raises StoreError if it can't."""

    async def close(self) -> None:
        """Let go of whatever open() took hold of."""

    @abstractmethod
    async def add(self, at: float, readings: Sequence[InterfaceCounters]) -> None:
        """Keep one poll's readings, which arrived at a moment. Each reading names its device and port."""

    @abstractmethod
    async def ports(self) -> list[tuple[str, str]]:
        """Every port with samples, as (device, port), in no particular order."""

    @abstractmethod
    async def samples(self, device: str, port: str, since: float) -> list[Sample]:
        """
        One port's samples from a moment on, oldest first. The list starts with the
        last sample at or before the moment, so the first interval that ends after it
        can be measured.
        """


class MemoryStore(Store):
    """The last window seconds of samples, in this process. Nothing survives a restart."""

    def __init__(self, window: float = DEFAULT_WINDOW) -> None:
        self.window = window
        self._samples: dict[tuple[str, str], deque[Sample]] = {}

    async def add(self, at: float, readings: Sequence[InterfaceCounters]) -> None:
        for reading in readings:
            series = self._samples.setdefault((reading.device, reading.interface), deque())
            series.append(Sample(at, reading))
            while series[0].at < at - self.window:
                series.popleft()

    async def ports(self) -> list[tuple[str, str]]:
        return list(self._samples)

    async def samples(self, device: str, port: str, since: float) -> list[Sample]:
        series = list(self._samples.get((device, port), ()))
        start = max((index for index, sample in enumerate(series) if sample.at <= since), default=0)
        return series[start:]


def timestamp(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


class TimescaleStore(Store):
    """
    Samples in TimescaleDB, kept for window seconds (a week unless told otherwise),
    across restarts. The URL names no password: libpq finds it in ~/.pgpass
    (chmod 600) or PGPASSWORD, so it is never on a command line or in a log.
    """

    def __init__(self, url: str, window: float = DATABASE_WINDOW) -> None:
        from psycopg.conninfo import conninfo_to_dict

        try:
            settings = conninfo_to_dict(url)
        except Exception as error:
            raise StoreError(f"Not a PostgreSQL URL: {error}") from None
        if settings.get("password"):
            raise StoreError(
                "Put the store's password in ~/.pgpass (chmod 600) or PGPASSWORD, not in its URL, where anyone who can "
                "list processes sees it."
            )
        self.url = url
        self.window = window
        self._pool: Any = None

    async def open(self) -> None:
        from psycopg import AsyncConnection, Error
        from psycopg_pool import AsyncConnectionPool, PoolTimeout

        if self._pool is not None:
            return
        # One connection first, to make the tables: a pool that can't connect only says it timed out, and this
        # says why (refused, no such database, password).
        try:
            async with await AsyncConnection.connect(self.url, connect_timeout=10) as connection:
                await self._create(connection)
        except Error as error:
            raise StoreError(f"Can't use the store at {self.url}: {error}") from None
        # Each connection is checked as it is handed out, so one the server dropped (a restart) is replaced
        # rather than failing a poll.
        pool = AsyncConnectionPool(self.url, min_size=1, max_size=4, open=False, check=AsyncConnectionPool.check_connection)
        try:
            await pool.open(wait=True, timeout=10)
        except PoolTimeout as error:
            await pool.close()
            raise StoreError(f"Can't use the store at {self.url}: {error}") from None
        self._pool = pool

    async def _create(self, connection: Any) -> None:
        """Make the tables if they aren't there, and drop raw samples older than the window."""
        columns = ", ".join(f"{name} numeric(20)" + (" NOT NULL" if name != "carrier_transitions" else "") for name in COUNTERS)
        # Two NetGPTs starting at once would both make the tables, and IF NOT EXISTS doesn't stop two at the same
        # moment. This is one transaction, and the lock is held until it commits.
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext('netgpt.store'))")
        # Counters are unsigned 64-bit, past bigint's reach, so they are numeric.
        await connection.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS interface_samples (at timestamptz NOT NULL, device text NOT NULL, "
            f"interface text NOT NULL, oper_state text, speed_bps bigint, {columns})"
        )
        await connection.execute(
            "SELECT create_hypertable('interface_samples', by_range('at', INTERVAL '1 day'), if_not_exists => TRUE)"
        )
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS interface_samples_port ON interface_samples (device, interface, at DESC)"
        )
        # Every port ever polled, so listing them doesn't scan a week of samples.
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS interface_ports (device text NOT NULL, interface text NOT NULL, "
            "last_at timestamptz NOT NULL, PRIMARY KEY (device, interface))"
        )
        await connection.execute("SELECT remove_retention_policy('interface_samples', if_exists => TRUE)")
        await connection.execute(
            "SELECT add_retention_policy('interface_samples', make_interval(secs => %s))", (self.window,)
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _ready(self) -> Any:
        if self._pool is None:
            raise StoreError("The store isn't open.")
        return self._pool

    async def add(self, at: float, readings: Sequence[InterfaceCounters]) -> None:
        from psycopg import Error

        when = timestamp(at)
        names = ", ".join(COUNTERS)
        try:
            async with self._ready().connection() as connection:
                async with connection.cursor() as cursor:
                    copy_sql = f"COPY interface_samples (at, device, interface, oper_state, speed_bps, {names}) FROM STDIN"
                    async with cursor.copy(copy_sql) as copy:
                        for reading in readings:
                            counters = [getattr(reading, name) for name in COUNTERS]
                            await copy.write_row([when, reading.device, reading.interface, reading.oper_state,
                                                  reading.speed_bps, *counters])
                    await cursor.executemany(
                        "INSERT INTO interface_ports (device, interface, last_at) VALUES (%s, %s, %s) "
                        "ON CONFLICT (device, interface) "
                        "DO UPDATE SET last_at = greatest(interface_ports.last_at, excluded.last_at)",
                        [(reading.device, reading.interface, when) for reading in readings],
                    )
        except Error as error:
            raise StoreError(f"Couldn't keep the samples from {at:.0f}: {error}") from None

    async def ports(self) -> list[tuple[str, str]]:
        from psycopg import Error

        try:
            async with self._ready().connection() as connection:
                rows = await (await connection.execute("SELECT device, interface FROM interface_ports")).fetchall()
        except Error as error:
            raise StoreError(f"Couldn't list the ports: {error}") from None
        return [(device, interface) for device, interface in rows]

    async def samples(self, device: str, port: str, since: float) -> list[Sample]:
        from psycopg import Error

        columns = "at, oper_state, speed_bps, " + ", ".join(COUNTERS)
        where = "device = %(device)s AND interface = %(port)s"
        # The last sample at or before the moment, then every one after it.
        query = (
            f"SELECT * FROM ((SELECT {columns} FROM interface_samples WHERE {where} AND at <= %(since)s "
            f"ORDER BY at DESC LIMIT 1) UNION ALL (SELECT {columns} FROM interface_samples WHERE {where} "
            "AND at > %(since)s)) found ORDER BY at"
        )
        values = {"device": device, "port": port, "since": timestamp(since)}
        try:
            async with self._ready().connection() as connection:
                rows = await (await connection.execute(query, values)).fetchall()
        except Error as error:
            raise StoreError(f"Couldn't read {device}:{port}'s samples: {error}") from None
        samples = []
        for at, oper_state, speed_bps, *counters in rows:
            values = {name: None if value is None else int(value) for name, value in zip(COUNTERS, counters)}
            reading = InterfaceCounters(device=device, interface=port, oper_state=oper_state, speed_bps=speed_bps, **values)
            samples.append(Sample(at.timestamp(), reading))
        return samples


async def check_store(store: Store) -> None:
    """Open a store and close it again, so one that can't be used is found at startup. Raises StoreError."""
    await store.open()
    await store.close()


def open_store(url: str | None) -> Store:
    """The store a --store option names: memory (the default), or a postgresql:// URL for TimescaleDB."""
    if url is None or url == "memory":
        return MemoryStore()
    if url.startswith(("postgresql://", "postgres://")):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            raise StoreError("The TimescaleDB store needs psycopg: pip install 'netgpt[store]'.") from None
        return TimescaleStore(url)
    raise StoreError(f"--store takes memory or a postgresql:// URL, not {url!r}.")
