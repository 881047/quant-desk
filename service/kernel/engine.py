"""Reference engine: one loop that drives submit → match → settle.

**Not the production path.** `PaperBook` composes `Ledger` + `BarExecutionModel`
directly, because the desk needs ticket queues, a guard re-check at fill time and
two fill bases (`next_open` / `intraday`) that this loop does not model. What
remains here is the executable specification the golden scenarios run against:
if `step()` and `PaperBook` ever disagree about a session boundary or an expiry,
`tests/golden/` is where it shows up.

The earlier docstring claimed "the single code path for backtest and paper".
That was false in both directions — production never called it, and the research
layer evaluates the same rules vectorised — so the claim is gone rather than
quietly maintained. `tests/kernel/test_boundaries.py` now fails if a production
module imports this one, which keeps "reference implementation" a fact instead of
a comment.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from .calendar import AUCTION_CLOSE_END, TradingCalendar
from .clock import SHANGHAI, Clock
from .execution import BarExecutionModel, FillResult
from .ledger import Ledger
from .models import (
    Account,
    Bar,
    D,
    Fill,
    Instrument,
    Intent,
    Order,
    RejectReason,
    RiskDecision,
    Session,
)
from .risk import RiskEngine


class MarketDataView(Protocol):
    def bars(self, ts: datetime) -> dict[str, Bar]: ...


class StaticDataView:
    def __init__(self, bars_by_ts: dict[datetime, dict[str, Bar]]) -> None:
        self._bars = bars_by_ts

    def bars(self, ts: datetime) -> dict[str, Bar]:
        return self._bars.get(ts, {})


@dataclass
class StepResult:
    ts: datetime
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    alerts: list[RiskDecision] = field(default_factory=list)


class TradingEngine:
    def __init__(
        self,
        clock: Clock,
        calendar: TradingCalendar,
        data: MarketDataView,
        instruments: dict[str, Instrument],
        ledger: Ledger,
        exec_model: BarExecutionModel | None = None,
        risk: RiskEngine | None = None,
    ) -> None:
        self.clock = clock
        self.calendar = calendar
        self.data = data
        self.instruments = instruments
        self.ledger = ledger
        self.exec_model = exec_model or BarExecutionModel()
        self.risk = risk or RiskEngine()
        self.kill_switch = False
        self.safe_mode = False
        self.daily_pnl_pct = D("0")
        self.consecutive_losses = 0
        self._day: date | None = None
        self._day_start_equity = self.account.equity()

    @property
    def account(self) -> Account:
        return self.ledger.account

    @staticmethod
    def _exchange_time(ts: datetime) -> datetime:
        if ts.tzinfo is None:
            return ts.replace(tzinfo=SHANGHAI)
        return ts.astimezone(SHANGHAI)

    def prices(self, bars: dict[str, Bar], ts: datetime | None = None) -> dict[str, object]:
        """Use open/live marks before the close; close marks only at day end."""
        end_of_day = ts is not None and (
            self.calendar.session_at(ts) == Session.AUCTION_CLOSE
            or self._exchange_time(ts).timetz().replace(tzinfo=None) >= AUCTION_CLOSE_END
        )
        return {
            symbol: (bar.close if end_of_day else
                     bar.open if bar.open > 0 else bar.pre_close)
            for symbol, bar in bars.items()
        }

    def _roll_day(self, day: date) -> None:
        if self._day == day:
            return
        if self._day is not None:
            self.consecutive_losses = (
                self.consecutive_losses + 1 if self.daily_pnl_pct < 0 else 0
            )
        self._day = day
        # Use the previous session's marks for the baseline. Applying the new
        # bar first would hide an overnight gap inside the denominator.
        self._day_start_equity = self.account.equity()
        self.daily_pnl_pct = D("0")

    def _update_daily_pnl(self, prices: dict[str, object]) -> None:
        if self._day_start_equity <= 0:
            self.daily_pnl_pct = D("0")
            return
        equity = self.account.equity(prices)
        self.daily_pnl_pct = (equity - self._day_start_equity) / self._day_start_equity

    def step(self, ts: datetime, intents: Iterable[Intent] | None = None) -> StepResult:
        ts = self._exchange_time(ts)
        self.clock.advance(ts)
        day = ts.date()
        previous_day = self._day
        stale_orders = previous_day is None and any(
            order.created_ts is not None
            and self._exchange_time(order.created_ts).date() < day
            for order in self.ledger.oms.open_orders()
        )
        if (previous_day is not None and previous_day != day) or stale_orders:
            # The first bar of a new session must release yesterday's orders and
            # unlock inventory even when no 15:00 bar was ever supplied.
            self.ledger.expire_day_orders(ts)
            # The session this rollover opens. A second call in the same day is
            # a no-op rather than a second T+1 unlock; the close branch below
            # relies on that, because the morning already settled this day.
            self.ledger.settle(day)
        self._roll_day(day)
        bars = self.data.bars(ts)
        prices = self.prices(bars, ts)
        self._update_daily_pnl(prices)
        result = StepResult(ts=ts)

        for intent in intents or []:
            bar = bars.get(intent.symbol)
            instrument = self.instruments.get(intent.symbol)
            if bar is None or instrument is None:
                result.alerts.append(
                    RiskDecision(
                        False,
                        RejectReason.NOT_IN_UNIVERSE,
                        None,
                        "missing bar/instrument",
                    )
                )
                continue
            decision = self.risk.check(
                intent,
                self.account,
                instrument,
                bar,
                prices,
                kill_switch=self.kill_switch,
                safe_mode=self.safe_mode,
                daily_pnl_pct=self.daily_pnl_pct,
                consecutive_losses=self.consecutive_losses,
            )
            if not decision.allowed:
                result.alerts.append(decision)
                continue
            order = self.ledger.submit(intent, ts, instrument, bar, self.calendar)
            result.orders.append(order)

        for order in list(self.ledger.oms.open_orders()):
            bar = bars.get(order.symbol)
            instrument = self.instruments.get(order.symbol)
            if bar is None or instrument is None:
                continue
            matched_bar_ts = self._exchange_time(bar.ts)
            if order.last_matched_bar_ts == matched_bar_ts:
                continue
            match = self.exec_model.match(order, bar, instrument, ts, self.calendar)
            if isinstance(match, FillResult) and match.fill is not None:
                applied = self.ledger.apply_fill(order, match.fill)
                order.last_matched_bar_ts = matched_bar_ts
                if applied:
                    result.fills.append(match.fill)

        # A market order gets exactly one chance: the bar it was sent against.
        # Leaving the remainder open would let it fill at tomorrow's open, which
        # is a price nobody quoted to this order.
        self.ledger.expire_ioc(ts)

        self.account.update_peak(prices)

        if (self.calendar.session_at(ts) == Session.AUCTION_CLOSE
                or ts.timetz().replace(tzinfo=None) >= AUCTION_CLOSE_END):
            # Day orders die at the close, before the T+1 rollover. Doing it the
            # other way round would let a stale order freeze cash that settle()
            # has already declared available.
            self.ledger.expire_day_orders(ts)
            self.ledger.settle(day)

        self._update_daily_pnl(prices)
        return result

    def run(
        self,
        bars_by_ts: dict[datetime, dict[str, Bar]],
        intents_by_ts: dict[datetime, list[Intent]] | None = None,
    ) -> list[StepResult]:
        intents_by_ts = intents_by_ts or {}
        results = []
        for ts in sorted(bars_by_ts):
            results.append(self.step(ts, intents_by_ts.get(ts)))
        return results
