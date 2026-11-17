"""The ticket path, checked against the same identities as the bare ledger.

`PaperBook` is where the kernel meets a queue, a restart and a volume cap. Two
things only exist here: a ticket that is *waiting* (cash and shares reserved but
no order yet) and a fill that is *partial* (the order got a fraction of what was
asked for). Both are places where a reservation can outlive the trade that
created it.

The partial-fill remainder is deliberately not re-queued: one ticket has one
terminal outcome, and a live remainder holding cash overnight is a worse failure
than a smaller position. That is a product decision, so these tests do not assert
it should change — they assert it leaves nothing behind, and that the record says
which of the two happened.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.ledger import Ledger
from service.kernel.models import Bar, D, Instrument, Intent, Side, money
from service.trading.book import PaperBook
from service.trading.state import PaperStore

from .test_execution_next_open import DAY1, DAY2, SYMBOL, StubMarket, make_bar, ticket

DAY3, DAY4 = "20260107", "20260108"


def _book(tmp_path, bars, cash: str = "5000") -> PaperBook:
    return PaperBook(PaperStore(tmp_path / "paper.sqlite"), StubMarket(bars, DAY2),
                     initial_cash=cash)


def _assert_books_balance(book: PaperBook) -> None:
    """cash + book value == opening capital + realized, and nothing double-counted."""
    account = book.ledger.account
    book_value = sum((D(p.total_qty) * p.avg_cost for p in account.positions.values()),
                     D("0"))
    opening = D(book.state["initial_cash"])
    lhs = money(account.cash_total() + book_value)
    assert abs(lhs - (opening + account.realized_pnl)) <= D("0.05"), (
        f"cash+book {lhs} != opening+realized {opening + account.realized_pnl}"
    )
    for pos in account.positions.values():
        assert pos.is_consistent(), pos
    reserved = money(sum(
        (o.frozen_cash for o in book.ledger.oms.open_orders() if o.side is Side.BUY),
        D("0"),
    ))
    assert account.cash_frozen == reserved


@pytest.fixture
def two_days(tmp_path):
    bars = {
        DAY1: make_bar(DAY1, open_="9.95", high="10.05", low="9.90", close="10.00"),
        DAY2: make_bar(DAY2, open_="10.10", high="10.30", low="10.05", close="10.20",
                       pre_close="10.00"),
    }
    return tmp_path, bars


def test_a_partial_fill_retires_the_remainder_and_leaves_no_reservation(two_days):
    tmp_path, bars = two_days
    # A quarter of the ask clears: 400 shares become 100, a legal lot.
    bars[DAY2] = replace(bars[DAY2], fill_ratio=0.25)
    book = _book(tmp_path, bars)
    book.rollover(DAY1)
    book.execute_ticket(ticket(qty=400))

    book.rollover(DAY2)

    account = book.ledger.account
    pos = account.peek(SYMBOL)
    assert pos.total_qty == 100
    assert (pos.available_qty, pos.frozen_qty, pos.locked_qty) == (0, 0, 100)
    assert account.cash_frozen == D("0"), "the unfilled 300 shares must not keep cash"
    assert book.ledger.oms.open_orders() == [], "the remainder is retired, not left live"
    assert book.summary()["pending"] == []
    _assert_books_balance(book)


def test_the_retired_remainder_says_why(two_days):
    """The order record must carry the reason, not the mechanism.

    ``_retire_order`` passes a sentence ("only a quarter filled"), but ``cancel``
    used to overwrite it with ``CANCELLED`` — so the audit kept how the order died
    and lost why. It was the reverse of intended: only an order retired *outside*
    the cancel window (falling back to ``expire``) kept the explanation.
    """
    tmp_path, bars = two_days
    bars[DAY2] = replace(bars[DAY2], fill_ratio=0.25)
    book = _book(tmp_path, bars)
    book.rollover(DAY1)
    book.execute_ticket(ticket(qty=400))
    book.rollover(DAY2)

    assert [o.reason for o in book.ledger.oms.orders.values()] == [
        "QUEUED_PARTIAL_REMAINDER"
    ]


def test_a_manual_cancel_can_say_why():
    """The public cancel path keeps its default sentence and accepts a better one."""
    calendar = TradingCalendar.always()
    ledger = Ledger.fresh("20000")
    ts = datetime(2026, 1, 5, 10, 0, tzinfo=SHANGHAI)
    bar = Bar(symbol=SYMBOL, ts=ts, open=D("10.00"), high=D("10.20"), low=D("9.80"),
              close=D("10.10"), pre_close=D("10.00"), volume=1_000_000,
              amount=D("10100000"), up_limit=D("11.00"), down_limit=D("9.00"))
    order = ledger.submit(Intent(SYMBOL, Side.BUY, qty=100), ts,
                          Instrument(SYMBOL), bar, calendar)
    assert order.is_open

    ledger.cancel(order.order_id, ts, calendar, reason="CHASE_CALLED_OFF")
    assert order.reason == "CHASE_CALLED_OFF"


def test_the_t_plus_lock_survives_reopening_the_book(two_days):
    tmp_path, bars = two_days
    book = _book(tmp_path, bars)
    book.rollover(DAY1)
    book.execute_ticket(ticket())
    book.rollover(DAY2)

    reopened = _book(tmp_path, bars)
    pos = reopened.ledger.account.peek(SYMBOL)
    assert (pos.total_qty, pos.available_qty, pos.locked_qty) == (100, 0, 100)
    assert reopened.ledger.account.cash_frozen == D("0")
    _assert_books_balance(reopened)


def test_executing_the_same_ticket_twice_does_not_fill_twice(two_days):
    tmp_path, bars = two_days
    book = _book(tmp_path, bars)
    book.rollover(DAY1)
    book.execute_ticket(ticket())
    book.rollover(DAY2)
    first = book.ledger.account.peek(SYMBOL).total_qty

    again = book.execute_ticket(ticket())

    assert again.status == "already_filled"
    assert book.ledger.account.peek(SYMBOL).total_qty == first
    assert len(book.state["fills"]) == 1
    _assert_books_balance(book)


def test_a_sell_after_the_lock_clears_balances(two_days):
    """Buy at DAY2's open, settle on DAY3, sell at DAY4's open: exact round trip."""
    tmp_path, bars = two_days
    bars[DAY3] = make_bar(DAY3, open_="10.40", high="10.60", low="10.30",
                          close="10.50", pre_close="10.20")
    bars[DAY4] = make_bar(DAY4, open_="10.45", high="10.60", low="10.40",
                          close="10.55", pre_close="10.50")
    book = _book(tmp_path, bars)
    book.rollover(DAY1)
    book.execute_ticket(ticket())
    book.rollover(DAY2)
    assert book.ledger.account.peek(SYMBOL).available_qty == 0

    book.rollover(DAY3)  # settles DAY2's buy into deliverable inventory
    assert book.ledger.account.peek(SYMBOL).available_qty == 100

    sell = dict(ticket(ticket_id="td-sell"), side="SELL", qty=100,
                limit_price="10.30", trade_date=DAY3)
    assert book.execute_ticket(sell).status == "queued"
    book.rollover(DAY4)

    pos = book.ledger.account.peek(SYMBOL)
    assert pos.total_qty == 0 and pos.avg_cost == D("0")
    _assert_books_balance(book)

    buy_fill, sell_fill = book.state["fills"]
    cost = D(buy_fill["qty"]) * D(buy_fill["price"]) + D(buy_fill["fees"])
    proceeds = D(sell_fill["qty"]) * D(sell_fill["price"]) - D(sell_fill["fees"])
    assert abs(book.ledger.account.realized_pnl - money(proceeds - cost)) <= D("0.01"), (
        "realized must be the fills' arithmetic, not a residual of rounding"
    )
