"""
The store module keeps the interface counters the poller reads, so that statistics
can be worked out from them afterwards. A Store is where the samples go and where
they are read back. MemoryStore keeps the last window of them in this process, and
nothing survives a restart; a store backed by a database can keep them longer.

The store holds counters as the devices reported them, not rates: a rate is the
difference between two readings, and working it out on reading keeps one place
that knows a counter going backwards is a reset.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from netgpt.primitives import InterfaceCounters

# How much history MemoryStore keeps, in seconds: an hour of samples at the default interval is 120 per port.
DEFAULT_WINDOW = 3600.0


@dataclass(frozen=True)
class Sample:
    """One port's counters, and when they arrived (seconds since the epoch)."""

    at: float
    counters: InterfaceCounters


class Store(ABC):
    """
    Where the poller's samples are kept. Every method is async, so a store can be a
    database. window is how far back, in seconds, the store keeps samples: no
    statistic reaches further.
    """

    window: float

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
