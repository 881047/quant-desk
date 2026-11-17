"""Injectable clocks. Business code must not call datetime.now()."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


class Clock(Protocol):
    def now(self) -> datetime: ...
    def advance(self, ts: datetime) -> None: ...
    def today(self) -> datetime.date: ...


class SimClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=SHANGHAI)
        self._now = start.astimezone(SHANGHAI)

    def now(self) -> datetime:
        return self._now

    def advance(self, ts: datetime) -> None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=SHANGHAI)
        ts = ts.astimezone(SHANGHAI)
        if ts < self._now:
            raise ValueError(f"clock cannot move backwards: {ts} < {self._now}")
        self._now = ts

    def today(self) -> datetime.date:
        return self._now.date()


class LiveClock:
    def __init__(self) -> None:
        self._now = datetime.now(SHANGHAI)

    def now(self) -> datetime:
        self._now = datetime.now(SHANGHAI)
        return self._now

    def advance(self, ts: datetime) -> None:
        # Live clock ignores synthetic advances; kept for interface parity.
        return None

    def today(self) -> datetime.date:
        return self.now().date()
