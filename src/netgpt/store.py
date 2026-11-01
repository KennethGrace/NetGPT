"""
The store module keeps the interface counters the poller reads, so that statistics
can be worked out from them afterwards. A Store is where the samples go and where
they are read back. MemoryStore keeps the last window of them in this process, and
nothing survives a restart. TimescaleStore keeps them in TimescaleDB (PostgreSQL),
for days, across restarts (dev/compose.yml runs one).

As each sample arrives, the store works out the rate over the interval since that
port's previous one (rate()), once, so one place knows that a counter going
backwards is a reset. It keeps both: the counters as the devices reported them, and
each interval's rate. A rollup combines the intervals that end in a step of time
(5 minutes, an hour, a day) into one average, with the busiest interval's rate beside
it, so a long window is read without reading every poll. TimescaleDB keeps 5-minute
and hourly rollups for months and years after the polls themselves are gone.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from netgpt.primitives import InterfaceCounters

DAY = 24 * 3600.0
# How much history MemoryStore keeps, in seconds: an hour of samples at the default interval is 120 per port.
DEFAULT_WINDOW = 3600.0
# How much raw history TimescaleStore keeps, every sample and every interval's rate: a week, after which TimescaleDB
# drops it.
DATABASE_WINDOW = 7 * DAY
# TimescaleStore's rollups, by step in seconds, and how long each is kept: 5-minute averages for 90 days, and hourly
# ones for two years.
ROLLUPS = {300: 90 * DAY, 3600: 730 * DAY}
# Every rollup's step is a whole number of these.
SHORTEST_STEP = 300

# The counters, in InterfaceCounters' order, as each sample's columns.
COUNTERS = (
    "in_octets", "out_octets", "in_packets", "out_packets", "in_errors", "out_errors", "in_discards", "out_discards",
    "carrier_transitions",
)
# The counters a rate is the difference of. Any of them going backwards means a reset.
COUNTED = ("in_octets", "out_octets", "in_errors", "out_errors", "in_discards", "out_discards")


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


@dataclass(frozen=True)
class Rate:
    """
    One port's traffic over a stretch of time: one interval between two polls, or a
    rollup of every interval that ended in a step. Bits per second are averages over
    the seconds measured, and the peaks are the busiest single interval's. Errors,
    discards, and flaps are counts over the whole stretch.
    """

    start: float
    end: float
    seconds: float
    in_bps: float
    out_bps: float
    peak_in_bps: float
    peak_out_bps: float
    in_errors: int
    out_errors: int
    in_discards: int
    out_discards: int
    flaps: int | None
    speed_bps: int | None


def rate(earlier: Sample, later: Sample, longest: float | None = None) -> Rate | None:
    """
    The traffic between two samples of one port, or None if a counter was reset in
    between, or if they are more than longest seconds apart.
    """
    seconds = later.at - earlier.at
    deltas = {name: getattr(later.counters, name) - getattr(earlier.counters, name) for name in COUNTED}
    if seconds <= 0 or (longest is not None and seconds > longest) or any(delta < 0 for delta in deltas.values()):
        return None
    flaps = None
    if later.counters.carrier_transitions is not None and earlier.counters.carrier_transitions is not None:
        flaps = later.counters.carrier_transitions - earlier.counters.carrier_transitions
        if flaps < 0:
            return None
    in_bps, out_bps = deltas["in_octets"] * 8 / seconds, deltas["out_octets"] * 8 / seconds
    return Rate(
        start=earlier.at,
        end=later.at,
        seconds=seconds,
        in_bps=in_bps,
        out_bps=out_bps,
        peak_in_bps=in_bps,
        peak_out_bps=out_bps,
        in_errors=deltas["in_errors"],
        out_errors=deltas["out_errors"],
        in_discards=deltas["in_discards"],
        out_discards=deltas["out_discards"],
        flaps=flaps,
        speed_bps=later.counters.speed_bps,
    )


def combine(rates: Sequence[Rate]) -> Rate:
    """Several stretches of one port's traffic as one: the average over all their seconds, the busiest, and the counts."""
    seconds = sum(found.seconds for found in rates)
    flaps = [found.flaps for found in rates]
    speeds = [found.speed_bps for found in rates if found.speed_bps is not None]
    return Rate(
        start=min(found.start for found in rates),
        end=max(found.end for found in rates),
        seconds=seconds,
        in_bps=sum(found.in_bps * found.seconds for found in rates) / seconds,
        out_bps=sum(found.out_bps * found.seconds for found in rates) / seconds,
        peak_in_bps=max(found.peak_in_bps for found in rates),
        peak_out_bps=max(found.peak_out_bps for found in rates),
        in_errors=sum(found.in_errors for found in rates),
        out_errors=sum(found.out_errors for found in rates),
        in_discards=sum(found.in_discards for found in rates),
        out_discards=sum(found.out_discards for found in rates),
        flaps=None if None in flaps else sum(flap for flap in flaps if flap is not None),
        speed_bps=max(speeds, default=None),
    )


def first_step(since: float, step: int) -> float:
    """Where the first whole step after a moment begins, counting steps from the epoch."""
    return math.ceil(since / step) * step


def roll_up(rates: Sequence[Rate], step: int, since: float) -> list[Rate]:
    """
    One port's intervals combined by the step each ended in, for the steps that begin
    at or after a moment, oldest first. Steps are counted from the epoch, as
    TimescaleDB's time_bucket counts them.
    """
    begin = first_step(since, step)
    steps: dict[float, list[Rate]] = {}
    for found in rates:
        bucket = math.floor(found.end / step) * step
        if bucket >= begin:
            steps.setdefault(bucket, []).append(found)
    return [combine(steps[bucket]) for bucket in sorted(steps)]


def check_step(step: int | None) -> None:
    if step is not None and (step <= 0 or step % SHORTEST_STEP):
        raise ValueError(f"A rollup's step is a whole number of {SHORTEST_STEP} seconds, not {step}.")


class Store(ABC):
    """
    Where the poller's samples are kept, with each interval's rate. Every method is
    async, so a store can be a database. window is how far back, in seconds, the
    store keeps every sample and interval; reach is how far back it keeps anything,
    rollups included, and no statistic reaches further. Open it before use and close
    it after; the poller does both while it runs.
    """

    def __init__(self, window: float) -> None:
        self.window = window
        # Each port's latest sample, which the next one's interval starts from.
        self._latest: dict[tuple[str, str], Sample] = {}

    @property
    def reach(self) -> float:
        return self.window

    async def open(self) -> None:
        """Get ready to keep and give back samples. Raises StoreError if it can't."""

    async def close(self) -> None:
        """Let go of whatever open() took hold of."""

    async def add(self, at: float, readings: Sequence[InterfaceCounters], longest: float | None = None) -> None:
        """
        Keep one poll's readings, which arrived at a moment, and each port's rate since
        its previous reading. Each reading names its device and port. Two readings more
        than longest seconds apart aren't measured: polls were missed, or nothing was
        polling, and one average over the gap would stand in for every poll in it.
        """
        keys = [(reading.device, reading.interface) for reading in readings]
        if missing := [key for key in keys if key not in self._latest]:
            self._latest.update(await self._recall(missing, at))
        samples = [Sample(at, reading) for reading in readings]
        rates = [
            rate(self._latest[key], sample, longest) if key in self._latest else None for key, sample in zip(keys, samples)
        ]
        await self._keep(samples, rates)
        for key, sample in zip(keys, samples):
            # A sample that arrived at the same moment as the latest was still read after it.
            if key not in self._latest or self._latest[key].at <= sample.at:
                self._latest[key] = sample

    async def _recall(self, ports: Sequence[tuple[str, str]], before: float) -> dict[tuple[str, str], Sample]:
        """Each port's latest sample kept before a moment, for ports this store hasn't had a sample of since it opened."""
        return {}

    @abstractmethod
    async def _keep(self, samples: Sequence[Sample], rates: Sequence[Rate | None]) -> None:
        """Keep one poll's samples, and beside each the rate over the interval it ends (None when it wasn't measured)."""

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

    @abstractmethod
    async def rates(self, device: str, port: str, since: float, step: int | None = None) -> list[Rate]:
        """
        One port's traffic from a moment on, oldest first. Without a step, each interval
        between polls that ends after the moment. With one (seconds, a whole number of
        SHORTEST_STEP), the intervals rolled up by the step each ended in, for the steps
        that begin at or after the moment.
        """


class MemoryStore(Store):
    """The last window seconds of samples and rates, in this process. Nothing survives a restart."""

    def __init__(self, window: float = DEFAULT_WINDOW) -> None:
        super().__init__(window)
        self._samples: dict[tuple[str, str], deque[Sample]] = {}
        self._rates: dict[tuple[str, str], deque[Rate]] = {}

    async def _keep(self, samples: Sequence[Sample], rates: Sequence[Rate | None]) -> None:
        for sample, found in zip(samples, rates):
            key = (sample.counters.device, sample.counters.interface)
            series = self._samples.setdefault(key, deque())
            series.append(sample)
            while series[0].at < sample.at - self.window:
                series.popleft()
            intervals = self._rates.setdefault(key, deque())
            if found is not None:
                intervals.append(found)
            while intervals and intervals[0].end < sample.at - self.window:
                intervals.popleft()

    async def ports(self) -> list[tuple[str, str]]:
        return list(self._samples)

    async def samples(self, device: str, port: str, since: float) -> list[Sample]:
        series = list(self._samples.get((device, port), ()))
        start = max((index for index, sample in enumerate(series) if sample.at <= since), default=0)
        return series[start:]

    async def rates(self, device: str, port: str, since: float, step: int | None = None) -> list[Rate]:
        check_step(step)
        intervals = list(self._rates.get((device, port), ()))
        if step is None:
            return [found for found in intervals if found.end > since]
        return roll_up(intervals, step, since)


def timestamp(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


# Each rollup's columns, which every step's rollup has alike, so one can be rolled up again into longer steps.
ROLLUP_COLUMNS = (
    "min(start) AS start, max(at) AS at, sum({seconds}) AS seconds, sum({in_bits}) AS in_bits, "
    "sum({out_bits}) AS out_bits, max({peak_in}) AS peak_in_bps, max({peak_out}) AS peak_out_bps, "
    "sum(in_errors) AS in_errors, sum(out_errors) AS out_errors, sum(in_discards) AS in_discards, "
    "sum(out_discards) AS out_discards, sum(flaps) AS flaps, sum({intervals}) AS intervals, "
    "sum({flaps_counted}) AS flaps_counted, max(speed_bps) AS speed_bps"
)
# From the intervals themselves: each is its own peak, and counts one interval.
OF_INTERVALS = ROLLUP_COLUMNS.format(
    seconds="extract(epoch FROM at - start)", in_bits="in_bps * extract(epoch FROM at - start)",
    out_bits="out_bps * extract(epoch FROM at - start)", peak_in="in_bps", peak_out="out_bps", intervals="1",
    flaps_counted="(flaps IS NOT NULL)::int",
)
# From a shorter rollup.
OF_ROLLUPS = ROLLUP_COLUMNS.format(
    seconds="seconds", in_bits="in_bits", out_bits="out_bits", peak_in="peak_in_bps", peak_out="peak_out_bps",
    intervals="intervals", flaps_counted="flaps_counted",
)
# Each rollup's continuous aggregate, what it is made from, and that source's time column.
ROLLUP_VIEWS = {
    300: ("interface_rates_5m", "interface_rates", OF_INTERVALS, "at"),
    3600: ("interface_rates_1h", "interface_rates_5m", OF_ROLLUPS, "bucket"),
}


class TimescaleStore(Store):
    """
    Samples and rates in TimescaleDB, kept for window seconds (a week unless told
    otherwise), and their rollups for longer, across restarts. The URL names no
    password: libpq finds it in ~/.pgpass (chmod 600) or PGPASSWORD, so it is never
    on a command line or in a log.
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
        if window < 3600:
            raise StoreError("A TimescaleDB store keeps at least an hour of samples, so its rollups can be refreshed.")
        super().__init__(window)
        self.url = url
        self._pool: Any = None

    @property
    def reach(self) -> float:
        return max(self.window, *ROLLUPS.values())

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
        """Make the tables and rollups if they aren't there, and drop what is older than each keeps."""
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
        # Each interval between two polls of a port: from start to at, the sample that ends it.
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS interface_rates (start timestamptz NOT NULL, at timestamptz NOT NULL, "
            "device text NOT NULL, interface text NOT NULL, in_bps double precision NOT NULL, "
            "out_bps double precision NOT NULL, in_errors bigint NOT NULL, out_errors bigint NOT NULL, "
            "in_discards bigint NOT NULL, out_discards bigint NOT NULL, flaps bigint, speed_bps bigint)"
        )
        await connection.execute(
            "SELECT create_hypertable('interface_rates', by_range('at', INTERVAL '1 day'), if_not_exists => TRUE)"
        )
        await connection.execute("CREATE INDEX IF NOT EXISTS interface_rates_port ON interface_rates (device, interface, at DESC)")
        # Every port ever polled, so listing them doesn't scan a week of samples.
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS interface_ports (device text NOT NULL, interface text NOT NULL, "
            "last_at timestamptz NOT NULL, PRIMARY KEY (device, interface))"
        )
        # The rollups are continuous aggregates, read in real time: what TimescaleDB hasn't stored yet, it works out
        # from the source when asked.
        for step, (view, source, rolled, time) in ROLLUP_VIEWS.items():
            bucket = f"time_bucket(INTERVAL '{step} seconds', {time})"
            await connection.execute(
                f"CREATE MATERIALIZED VIEW IF NOT EXISTS {view} WITH (timescaledb.continuous, "
                f"timescaledb.materialized_only = false) AS SELECT {bucket} AS bucket, device, interface, {rolled} "
                f"FROM {source} GROUP BY {bucket}, device, interface WITH NO DATA"
            )
        # Each step's rollup is stored once the step is over. A refresh works out what is new since the last one,
        # back as far as its start offset: far, so a pause in refreshing leaves no step unstored, but not past what
        # the source keeps, or it would empty steps whose intervals are gone.
        refresh = {300: timedelta(seconds=self.window / 2), 3600: timedelta(seconds=ROLLUPS[300] / 3)}
        for step, (view, *_) in ROLLUP_VIEWS.items():
            await connection.execute(f"SELECT remove_continuous_aggregate_policy('{view}', if_exists => TRUE)")
            await connection.execute(
                f"SELECT add_continuous_aggregate_policy('{view}', start_offset => %s, end_offset => %s, "
                "schedule_interval => %s)",
                (refresh[step], timedelta(seconds=step), timedelta(seconds=step)),
            )
        keeps = {"interface_samples": self.window, "interface_rates": self.window}
        keeps.update({ROLLUP_VIEWS[step][0]: seconds for step, seconds in ROLLUPS.items()})
        for table, seconds in keeps.items():
            await connection.execute(f"SELECT remove_retention_policy('{table}', if_exists => TRUE)")
            await connection.execute(f"SELECT add_retention_policy('{table}', %s)", (timedelta(seconds=seconds),))

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _ready(self) -> Any:
        if self._pool is None:
            raise StoreError("The store isn't open.")
        return self._pool

    async def _recall(self, ports: Sequence[tuple[str, str]], before: float) -> dict[tuple[str, str], Sample]:
        from psycopg import Error

        columns = "s.at, s.oper_state, s.speed_bps, " + ", ".join(f"s.{name}" for name in COUNTERS)
        query = (
            f"SELECT p.device, p.interface, {columns} FROM unnest(%s::text[], %s::text[]) AS p(device, interface) "
            "CROSS JOIN LATERAL (SELECT * FROM interface_samples WHERE device = p.device AND interface = p.interface "
            "AND at < %s ORDER BY at DESC LIMIT 1) s"
        )
        devices, interfaces = [device for device, _ in ports], [interface for _, interface in ports]
        try:
            async with self._ready().connection() as connection:
                rows = await (await connection.execute(query, (devices, interfaces, timestamp(before)))).fetchall()
        except Error as error:
            raise StoreError(f"Couldn't read the latest samples before {before:.0f}: {error}") from None
        return {(device, interface): self._sample(device, interface, row) for device, interface, *row in rows}

    async def _keep(self, samples: Sequence[Sample], rates: Sequence[Rate | None]) -> None:
        from psycopg import Error

        names = ", ".join(COUNTERS)
        try:
            async with self._ready().connection() as connection:
                async with connection.cursor() as cursor:
                    copy_sql = f"COPY interface_samples (at, device, interface, oper_state, speed_bps, {names}) FROM STDIN"
                    async with cursor.copy(copy_sql) as copy:
                        for sample in samples:
                            reading = sample.counters
                            counters = [getattr(reading, name) for name in COUNTERS]
                            await copy.write_row([timestamp(sample.at), reading.device, reading.interface,
                                                  reading.oper_state, reading.speed_bps, *counters])
                    copy_sql = (
                        "COPY interface_rates (start, at, device, interface, in_bps, out_bps, in_errors, out_errors, "
                        "in_discards, out_discards, flaps, speed_bps) FROM STDIN"
                    )
                    async with cursor.copy(copy_sql) as copy:
                        for sample, found in zip(samples, rates):
                            if found is None:
                                continue
                            await copy.write_row([
                                timestamp(found.start), timestamp(found.end), sample.counters.device,
                                sample.counters.interface, found.in_bps, found.out_bps, found.in_errors, found.out_errors,
                                found.in_discards, found.out_discards, found.flaps, found.speed_bps,
                            ])
                    await cursor.executemany(
                        "INSERT INTO interface_ports (device, interface, last_at) VALUES (%s, %s, %s) "
                        "ON CONFLICT (device, interface) "
                        "DO UPDATE SET last_at = greatest(interface_ports.last_at, excluded.last_at)",
                        [(sample.counters.device, sample.counters.interface, timestamp(sample.at)) for sample in samples],
                    )
        except Error as error:
            at = samples[0].at if samples else 0
            raise StoreError(f"Couldn't keep the samples from {at:.0f}: {error}") from None

    async def ports(self) -> list[tuple[str, str]]:
        from psycopg import Error

        try:
            async with self._ready().connection() as connection:
                rows = await (await connection.execute("SELECT device, interface FROM interface_ports")).fetchall()
        except Error as error:
            raise StoreError(f"Couldn't list the ports: {error}") from None
        return [(device, interface) for device, interface in rows]

    @staticmethod
    def _sample(device: str, port: str, row: Sequence[Any]) -> Sample:
        """A sample from its row: at, oper_state, speed_bps, then the counters."""
        at, oper_state, speed_bps, *counters = row
        values = {name: None if value is None else int(value) for name, value in zip(COUNTERS, counters)}
        reading = InterfaceCounters(device=device, interface=port, oper_state=oper_state, speed_bps=speed_bps, **values)
        return Sample(at.timestamp(), reading)

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
        return [self._sample(device, port, row) for row in rows]

    async def rates(self, device: str, port: str, since: float, step: int | None = None) -> list[Rate]:
        from psycopg import Error

        check_step(step)
        values: dict[str, Any] = {"device": device, "port": port}
        where = "device = %(device)s AND interface = %(port)s"
        if step is None:
            query = (
                "SELECT start, at, in_bps, out_bps, in_errors, out_errors, in_discards, out_discards, flaps, speed_bps "
                f"FROM interface_rates WHERE {where} AND at > %(since)s ORDER BY at"
            )
            values["since"] = timestamp(since)
        else:
            # The longest rollup the step is a whole number of, rolled up again into the step.
            view = ROLLUP_VIEWS[max(rollup for rollup in ROLLUP_VIEWS if step % rollup == 0)][0]
            query = (
                f"SELECT {OF_ROLLUPS} FROM {view} WHERE {where} AND bucket >= %(begin)s "
                "GROUP BY time_bucket(make_interval(secs => %(step)s), bucket, origin => %(origin)s) ORDER BY 2"
            )
            values.update(begin=timestamp(first_step(since, step)), step=step, origin=timestamp(0))
        try:
            async with self._ready().connection() as connection:
                rows = await (await connection.execute(query, values)).fetchall()
        except Error as error:
            raise StoreError(f"Couldn't read {device}:{port}'s rates: {error}") from None
        if step is None:
            return [
                Rate(start=start.timestamp(), end=at.timestamp(), seconds=(at - start).total_seconds(), in_bps=in_bps,
                     out_bps=out_bps, peak_in_bps=in_bps, peak_out_bps=out_bps, in_errors=in_errors,
                     out_errors=out_errors, in_discards=in_discards, out_discards=out_discards, flaps=flaps,
                     speed_bps=speed_bps)
                for start, at, in_bps, out_bps, in_errors, out_errors, in_discards, out_discards, flaps, speed_bps in rows
            ]
        found = []
        for (start, at, seconds, in_bits, out_bits, peak_in, peak_out, in_errors, out_errors, in_discards, out_discards,
             flaps, intervals, flaps_counted, speed_bps) in rows:
            found.append(Rate(
                start=start.timestamp(), end=at.timestamp(), seconds=float(seconds), in_bps=in_bits / float(seconds),
                out_bps=out_bits / float(seconds), peak_in_bps=peak_in, peak_out_bps=peak_out, in_errors=int(in_errors),
                out_errors=int(out_errors), in_discards=int(in_discards), out_discards=int(out_discards),
                # A port that doesn't count flaps in every interval has no count for the step.
                flaps=int(flaps) if flaps_counted == intervals else None,
                speed_bps=None if speed_bps is None else int(speed_bps),
            ))
        return found


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
