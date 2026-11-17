"""Invariants that must survive any legal sequence, not just the ones we wrote.

The three expensive kernel bugs found by hand — frozen cash drifting away from
the live orders, ``frozen_qty`` outliving the shares, a snapshot that would not
reload — all had the same shape: every individual call was legal, and only a
particular *order* of them produced the broken state. Example-based tests can
only cover the orders somebody thought of.

This module generates sequences instead. ``tests/kernel/test_integrity.py``
pins the rules one at a time; this pins the four facts that must hold after every
single step, including after a step that raises:

* every share is sellable, reserved, or locked — nowhere else;
* reservations equal the live orders, in cash and in shares;
* the withdrawable pool never exceeds the available one;
* ``from_snapshot(snapshot())`` is exact, and the snapshot reloads.

It is not a fuzz harness for performance (that is ``scripts/bench/kernel_fuzz*``):
the point is correctness at the boundary, so it runs a few dozen sequences in the
normal suite rather than millions on demand.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from service.kernel.calendar import TradingCalendar
from service.kernel.execution import BarExecutionModel
from service.kernel.ledger import Ledger
from service.kernel.models import Bar, D, Fill, Instrument, Intent, Side

CALENDAR = TradingCalendar.always()
INSTRUMENT = Instrument(symbol="600000.SH")
FIRST_DAY = date(2026, 11, 13)

ACTIONS = ("buy", "sell", "fill", "cancel", "settle", "expire")


def _bar(ts: datetime) -> Bar:
    return Bar(
        symbol="600000.SH", ts=ts, open=D("10.00"), high=D("10.20"), low=D("9.90"),
        close=D("10.10"), pre_close=D("10.00"), volume=5_000_000,
        amount=D("50000000"), up_limit=D("11.00"), down_limit=D("9.00"),
        prior_volume=5_000_000,
    )


def _check(ledger: Ledger) -> None:
    """The four facts that must hold after every step."""
    account = ledger.account
    live = ledger.oms.open_orders()

    assert account.cash_available >= 0
    assert account.cash_frozen >= 0
    assert account.cash_withdrawable <= account.cash_available, (
        f"withdrawable {account.cash_withdrawable} > available "
        f"{account.cash_available}")
    assert account.cash_frozen == sum(
        (o.frozen_cash for o in live if o.side is Side.BUY), D("0")), (
        "cash reservation disagrees with the live buy orders")
    assert account.equity() >= 0

    for symbol, position in account.positions.items():
        assert position.is_consistent(), (
            f"{symbol}: total {position.total_qty} != available "
            f"{position.available_qty} + frozen {position.frozen_qty} + locked "
            f"{position.locked_qty}")
        reserved = sum(o.frozen_shares for o in live
                       if o.side is Side.SELL and o.symbol == symbol)
        assert position.frozen_qty == reserved, (
            f"{symbol}: frozen {position.frozen_qty} != open sell reservations "
            f"{reserved}")

    for order in live:
        assert 0 <= order.filled_qty <= order.qty
        assert order.frozen_cash >= 0 and order.frozen_shares >= 0

    snapshot = ledger.snapshot()
    assert Ledger.from_snapshot(snapshot).snapshot() == snapshot, (
        "snapshot does not reload to itself")


def _submit(ledger: Ledger, side: Side, qty: int, ts: datetime) -> None:
    """One submission; a refusal is part of the contract, corruption is not."""
    try:
        ledger.submit(Intent(symbol="600000.SH", side=side, qty=qty),
                      ts, INSTRUMENT, _bar(ts), CALENDAR)
    except ValueError:
        pass


def _apply(ledger: Ledger, order, fill: Fill) -> None:
    try:
        ledger.apply_fill(order, fill)
    except ValueError:
        pass


@settings(max_examples=80, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.tuples(st.sampled_from(ACTIONS), st.integers(1, 500)),
                min_size=6, max_size=24))
def test_invariants_survive_any_legal_sequence(actions):
    ledger = Ledger.fresh("10000")
    day_index = 0
    ts = datetime(2026, 11, 13, 10, 0)
    matcher = BarExecutionModel()

    for action, amount in actions:
        if action == "buy":
            _submit(ledger, Side.BUY, amount, ts)
        elif action == "sell":
            _submit(ledger, Side.SELL, amount, ts)
        elif action == "fill":
            bar = _bar(ts)
            for order in list(ledger.oms.open_orders()):
                result = matcher.match(order, bar, INSTRUMENT, ts, CALENDAR)
                if isinstance(result.fill, Fill):
                    _apply(ledger, order, result.fill)
        elif action == "cancel":
            live = ledger.oms.open_orders()
            if live:
                ledger.cancel(live[0].order_id, ts, CALENDAR)
        elif action == "settle":
            # A real rollover: day orders die first, then the next session opens.
            ledger.expire_day_orders(ts)
            day_index += 1
            today = FIRST_DAY + timedelta(days=day_index)
            ledger.settle(today)
            ts = datetime(today.year, today.month, today.day, 10, 0)
        elif action == "expire":
            ledger.expire_ioc(ts)

        _check(ledger)
