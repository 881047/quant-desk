"""A-share trading sessions and cancellation rules."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time

from .clock import SHANGHAI
from .models import Session

AUCTION_OPEN_START = time(9, 15)
AUCTION_OPEN_NO_CANCEL = time(9, 20)
AUCTION_OPEN_END = time(9, 25)
PRE_OPEN_END = time(9, 30)
MORNING_END = time(11, 30)
AFTERNOON_START = time(13, 0)
CONTINUOUS_END = time(14, 57)
AUCTION_CLOSE_END = time(15, 0)
AFTER_HOURS_END = time(15, 30)


def _as_shanghai(ts: datetime) -> datetime:
    """Interpret naive inputs as exchange time and normalize aware inputs to it."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=SHANGHAI)
    return ts.astimezone(SHANGHAI)


@dataclass(frozen=True)
class TradingCalendar:
    # An empty set means "every day trades" — that is what ``always()`` builds,
    # and the reference engine and golden scenarios rely on it. A real calendar
    # is built with ``from_dates``. This is the one convention in the kernel that
    # fails open, so it is stated here instead of being inferred from the code.
    trading_days: frozenset[date]

    @classmethod
    def always(cls) -> TradingCalendar:
        return cls(frozenset())

    @classmethod
    def from_dates(cls, days: Iterable[date]) -> TradingCalendar:
        return cls(frozenset(days))

    def is_trading_day(self, d: date) -> bool:
        return not self.trading_days or d in self.trading_days

    def session_at(self, ts: datetime) -> Session:
        ts = _as_shanghai(ts)
        if not self.is_trading_day(ts.date()):
            return Session.CLOSED
        t = ts.timetz().replace(tzinfo=None)
        if AUCTION_OPEN_START <= t < AUCTION_OPEN_END:
            return Session.AUCTION_OPEN
        if AUCTION_OPEN_END <= t < PRE_OPEN_END:
            return Session.PRE_OPEN
        if PRE_OPEN_END <= t < MORNING_END:
            return Session.CONTINUOUS
        if AFTERNOON_START <= t < CONTINUOUS_END:
            return Session.CONTINUOUS
        if CONTINUOUS_END <= t < AUCTION_CLOSE_END:
            return Session.AUCTION_CLOSE
        if AUCTION_CLOSE_END <= t < AFTER_HOURS_END:
            return Session.AFTER_HOURS
        return Session.CLOSED

    def can_submit(self, ts: datetime) -> bool:
        return self.session_at(ts) in {
            Session.AUCTION_OPEN,
            Session.PRE_OPEN,
            Session.CONTINUOUS,
            Session.AUCTION_CLOSE,
        }

    def can_cancel(self, ts: datetime) -> bool:
        ts = _as_shanghai(ts)
        session = self.session_at(ts)
        if session in (Session.AUCTION_CLOSE, Session.PRE_OPEN):
            # 09:25-09:30 is the gap between the opening auction and continuous
            # trading: orders are accepted, cancellations are not. Treating it as
            # a normal pre-open window let a paper cancel succeed at 09:26 — a
            # sequence the exchange cannot produce.
            return False
        if session == Session.AUCTION_OPEN:
            t = ts.timetz().replace(tzinfo=None)
            return t < AUCTION_OPEN_NO_CANCEL
        return session == Session.CONTINUOUS

    # The two "is it fill time" predicates lived here with zero callers: the
    # auction windows are already carried by ``session_at`` (09:25-09:30 is
    # ``PRE_OPEN``, 14:57-15:00 is ``AUCTION_CLOSE``), and a helper nobody asks
    # cannot be verified against the rule it claims to encode. The boundary
    # constants remain, so a future live path can ask the question without a
    # second copy of the times.
