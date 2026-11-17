"""A reservation must not outlive the shares it reserved.

The scenario is real and narrow: a convertible bond matures while a sell order
is still open. `CBMaturity.apply` zeroes `total/available/frozen` on the
position, the order keeps `frozen_shares = 100`, and the next `settle()` used to
restore the freeze with `max(0, total - frozen)` — leaving `frozen_qty = 100`
against a zero position. `is_consistent()` then reported False for the rest of
the book's life, and nothing looked at it.

These tests pin the fix: the excess is released on the order, so both sides of
`frozen_qty == Σ open sell frozen_shares` move together.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from service.kernel.models import Account, Position, Side
from service.kernel.oms import OrderManager

D = Decimal
CB = "113050.SH"


def _oms_with(order) -> OrderManager:
    oms = OrderManager()
    oms._open = {order.order_id: order}   # the state `settle()` reads
    return oms


def test_settle_releases_a_reservation_whose_shares_are_gone():
    account = Account(cash_available=D("5000"), positions={
        CB: Position(symbol=CB, total_qty=0, available_qty=0, frozen_qty=100),
    })
    order = SimpleNamespace(order_id="o1", symbol=CB, side=Side.SELL,
                            frozen_shares=100, frozen_cash=D("0"))
    oms = _oms_with(order)

    oms.settle(account)

    pos = account.positions[CB]
    assert pos.frozen_qty == 0
    assert pos.available_qty == 0
    assert post_shares(order) == 0, "the order must not keep a phantom reservation"
    assert account.cash_frozen == D("0")


def test_settle_releases_only_the_excess_when_some_shares_remain():
    account = Account(cash_available=D("5000"), positions={
        CB: Position(symbol=CB, total_qty=60, available_qty=60, frozen_qty=0),
    })
    order = SimpleNamespace(order_id="o1", symbol=CB, side=Side.SELL,
                            frozen_shares=100, frozen_cash=D("0"))
    oms = _oms_with(order)

    oms.settle(account)

    pos = account.positions[CB]
    assert pos.frozen_qty == 60
    assert pos.available_qty == 0
    assert post_shares(order) == 60, "only the 40 shares that vanished are released"


def post_shares(order) -> int:
    return int(order.frozen_shares)


# ---------------------------------------------------------------- session identity

def _buy(ledger, ts, qty: int = 100):
    """One filled buy at ``ts``; returns the ledger for chaining."""

    from service.kernel.calendar import TradingCalendar
    from service.kernel.execution import BarExecutionModel
    from service.kernel.models import Bar, D, Instrument, Intent

    calendar = TradingCalendar.always()
    instrument = Instrument(symbol="600000.SH")
    bar = Bar(symbol="600000.SH", ts=ts, open=D("10.00"), high=D("10.20"),
              low=D("9.90"), close=D("10.10"), pre_close=D("10.00"),
              volume=5_000_000, amount=D("50000000"),
              up_limit=D("11.00"), down_limit=D("9.00"), prior_volume=5_000_000)
    order = ledger.submit(Intent(symbol="600000.SH", side=Side.BUY, qty=qty),
                          ts, instrument, bar, calendar)
    ledger.apply_fill(order, BarExecutionModel().match(
        order, bar, instrument, ts, calendar).fill)
    return ledger


def test_a_second_settle_in_the_same_session_does_not_unlock_todays_buys():
    """``settle`` mints sellable shares; calling it twice in a day mints twice.

    The production caller is date-guarded, but the API accepted a bare
    ``settle()`` from anywhere. A retry, a new endpoint or a background task
    would have made this morning's purchase sellable this afternoon, and every
    downstream check would have agreed.
    """
    from datetime import datetime

    from service.kernel.ledger import Ledger

    day = datetime(2026, 11, 13, 9, 30)
    ledger = _buy(Ledger.fresh("10000"), day)
    ledger.settle("20261113")
    assert ledger.account.peek("600000.SH").available_qty == 100

    _buy(ledger, datetime(2026, 11, 13, 10, 5))
    position = ledger.account.peek("600000.SH")
    assert (position.total_qty, position.available_qty, position.locked_qty) == (200, 100, 100)

    ledger.settle("20261113")            # same session: must be a no-op
    assert (position.available_qty, position.locked_qty) == (100, 100), (
        "a repeat call in the same session unlocked shares bought today")

    ledger.settle("20261114")            # next session: the lock is released
    assert (position.available_qty, position.locked_qty) == (200, 0)


def test_settle_refuses_to_move_backwards():
    from service.kernel.ledger import Ledger

    ledger = Ledger.fresh("10000")
    ledger.settle("20261114")
    with pytest.raises(ValueError, match="backwards"):
        ledger.settle("20261113")


def test_settle_accepts_a_date_object():
    from datetime import date

    from service.kernel.ledger import Ledger

    ledger = Ledger.fresh("10000")
    ledger.settle(date(2026, 11, 13))
    ledger.settle("20261113")            # the same day, written the other way
    ledger.settle(date(2026, 11, 14))


def test_same_session_settlement_marker_survives_restart():
    """A restart must not unlock shares bought in the already-settled session."""
    from datetime import datetime

    from service.kernel.ledger import Ledger

    day_one = datetime(2026, 11, 12, 9, 30)
    day_two = datetime(2026, 11, 13, 10, 0)
    ledger = _buy(Ledger.fresh("10000"), day_one)
    ledger.settle("20261113")
    _buy(ledger, day_two)
    position = ledger.account.peek("600000.SH")
    assert (position.total_qty, position.available_qty, position.locked_qty) == (200, 100, 100)

    restored = Ledger.from_snapshot(ledger.snapshot())
    restored.settle("20261113")

    restored_pos = restored.account.peek("600000.SH")
    assert (restored_pos.total_qty, restored_pos.available_qty,
            restored_pos.locked_qty) == (200, 100, 100)
