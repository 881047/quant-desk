"""The reference engine, checked against the ledger's invariants across days.

``TradingEngine`` is the executable specification the golden scenarios run
against: it is the only place that decides *when* the day rolls over, when open
orders die, and when T+1 inventory unlocks. Those are boundary decisions, and
boundaries are where named scenarios stop and property tests start — every golden
case is a sequence someone thought of.

The invariants are the same ones ``test_ledger_invariants`` asserts, imported
rather than copied: two definitions of "the books balance" would be the defect
this suite exists to catch.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.engine import StaticDataView, TradingEngine
from service.kernel.execution import BarExecutionModel
from service.kernel.ledger import Ledger
from service.kernel.models import Bar, D, Instrument, Intent, OrderType, Side
from service.kernel.risk import RiskConfig, RiskEngine
from tests.kernel.test_ledger_invariants import assert_invariants

SYMBOL = "600000.SH"
RICH = "600519.SH"
INSTRUMENTS = {
    SYMBOL: Instrument(SYMBOL, min_qty=100, qty_step=100, limit_pct=D("0.10")),
    RICH: Instrument(RICH, min_qty=100, qty_step=100, limit_pct=D("0.10")),
}
OPENING = D("30000")
DAY0 = datetime(2026, 3, 2, tzinfo=SHANGHAI)


def _bar(symbol: str, ts: datetime, step: int) -> Bar:
    """A deterministic bar: a slow drift with a wide band, so fills are legal."""
    inst = INSTRUMENTS[symbol]
    pre = D("20.00") + D(step) * D("0.05")
    up, down = inst.band(pre)
    open_ = pre
    return Bar(
        symbol=symbol, ts=ts, open=open_,
        high=min(up, open_ + D("0.40")), low=max(down, open_ - D("0.40")),
        close=open_ + D("0.05"), pre_close=pre,
        volume=5_000_000, amount=D(5_000_000) * open_,
        up_limit=up, down_limit=down, prior_volume=5_000_000,
    )


def _timeline(days: int, bars_per_day: int) -> list[tuple[datetime, str]]:
    """Open bars plus one closing-auction bar, which is what triggers the roll."""
    stamps: list[tuple[datetime, str]] = []
    for day in range(days):
        base = DAY0 + timedelta(days=day)
        for i in range(bars_per_day):
            stamps.append((base.replace(hour=10, minute=i * 5), "continuous"))
        stamps.append((base.replace(hour=14, minute=58), "close"))
    return stamps


OPS = st.lists(
    st.tuples(
        st.sampled_from([SYMBOL, RICH]),
        st.sampled_from([Side.BUY, Side.SELL]),
        st.integers(1, 4),          # lots
        st.booleans(),              # limit order?
        st.integers(-8, 8),         # limit offset in ticks
        st.booleans(),              # cancel every open order first
    ),
    min_size=1,
    max_size=18,
)


@given(OPS)
@settings(max_examples=40, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_the_reference_engine_keeps_the_books_balanced(ops):
    """Random orders, three sessions, closing auctions included.

    The assertion is not "this order filled" — most are rejected, and that is a
    correct outcome. It is "whatever the engine decided, the ledger still adds up
    after the rollover, the expiry and the unlock".
    """
    active_days = 3
    timeline = _timeline(active_days, bars_per_day=3)
    calendar = TradingCalendar.always()
    data = {
        ts: {symbol: _bar(symbol, ts, step)
             for symbol in INSTRUMENTS for step in range(len(timeline))}
        for step, (ts, _) in enumerate(timeline)
    }
    ledger = Ledger.fresh(str(OPENING))
    engine = TradingEngine(
        clock=_Clock(timeline[0][0]), calendar=calendar, data=StaticDataView(data),
        instruments=INSTRUMENTS, ledger=ledger,
        exec_model=BarExecutionModel(),
        risk=RiskEngine(RiskConfig(max_positions=2)),
    )

    for index, (ts, _) in enumerate(timeline):
        if index >= len(ops):
            intents = []
        else:
            symbol, side, lots, is_limit, offset, cancel = ops[index]
            inst = INSTRUMENTS[symbol]
            bar = data[ts][symbol]
            limit = None
            if is_limit:
                limit = (bar.open + D(offset) * inst.price_tick).quantize(inst.price_tick)
                limit = min(max(limit, bar.down_limit), bar.up_limit)
            intents = [Intent(
                symbol, side, qty=lots * 100,
                order_type=OrderType.LIMIT if is_limit else OrderType.MARKET,
                limit_price=limit,
                client_order_id=f"o{index}",
            )]
            if cancel:
                for order in list(ledger.oms.open_orders()):
                    ledger.cancel(order.order_id, ts, calendar)

        engine.step(ts, intents)
        assert_invariants(ledger, OPENING, f"bar {index} ({ts:%m-%d %H:%M})")

    # A cash-only day after the last one: the engine must expire yesterday's
    # orders and unlock whatever was bought, without any bar to trade.
    tail = DAY0 + timedelta(days=active_days, hours=10)
    engine.step(tail, [])
    assert ledger.oms.open_orders() == [], "a day order must not survive the session"
    for pos in ledger.account.positions.values():
        assert pos.locked_qty == 0, "the rollover must release the T+1 lock"
    assert_invariants(ledger, OPENING, "after rollover")


class _Clock:
    """A clock that only moves forward, like the real one."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, ts: datetime) -> None:
        self._now = max(self._now, ts)

    def today(self):
        return self._now.date()


def test_the_generator_reaches_a_fill():
    """A property test that never fills would assert nothing.

    The generated sequences produce roughly six fills per nine bars (measured over
    twenty seeds). This pins that the machinery can reach the interesting state at
    all, so tightening a band or a cap in the fixtures cannot quietly turn the
    test above into "everything was rejected, therefore nothing broke".
    """
    timeline = _timeline(2, bars_per_day=3)
    data = {ts: {s: _bar(s, ts, i) for s in INSTRUMENTS}
            for i, (ts, _) in enumerate(timeline)}
    ledger = Ledger.fresh(str(OPENING))
    engine = TradingEngine(
        clock=_Clock(timeline[0][0]), calendar=TradingCalendar.always(),
        data=StaticDataView(data), instruments=INSTRUMENTS, ledger=ledger,
        exec_model=BarExecutionModel(), risk=RiskEngine(RiskConfig(max_positions=2)),
    )

    filled = 0
    for index, (ts, _) in enumerate(timeline):
        result = engine.step(ts, [Intent(SYMBOL, Side.BUY, qty=100,
                                         client_order_id=f"reach-{index}")])
        filled += len(result.fills)
        assert_invariants(ledger, OPENING, f"reach {index}")

    assert filled >= 1
    assert ledger.account.peek(SYMBOL).total_qty == filled * 100
