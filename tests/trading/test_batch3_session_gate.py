"""Batch 3 regressions: the live-fill gate judges the *matching* window.

``book._execute_live`` used to decide "can I fill?" with ``session_state``, whose
``in_session`` window runs to **15:05**. That is the right answer for
``/market/status`` (a 15:01 frame still belongs to today's session) and the wrong
answer for a fill, which produced two bugs:

* 15:00-15:05: the order reached ``oms.submit``, which refused it as AFTER_HOURS,
  and ``_reject_execution`` wrote a **terminal** rejection — a tail decision that
  could never be recovered;
* 14:57-15:00: AUCTION_CLOSE matched at ``bar.close``, which for a live bar is the
  14:58 tape price used as the 15:00 close, with no slippage.

The gate now uses ``calendar.session_at(now)`` — the same window ``matcher.match``
uses — and only CONTINUOUS may fill. Everything else returns ``None`` so
``execute_ticket`` queues the ticket instead of killing or mispricing it.

Every case below pins ``decisions._now`` **and** ``book.clock`` to one moment on
the fixture's trading day (2026-11-05) and injects a fresh realtime quote; the
fill mode is switched to ``intraday`` (the default ``next_open`` / conftest
``close`` never reach ``_execute_live``).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from service.kernel.clock import SHANGHAI
from service.trading import live_fill
from tests.trading.helpers import pick_symbols, proposal
from tests.trading.test_live_fill import _quote


@pytest.fixture
def intraday_desk(desk, monkeypatch):
    monkeypatch.setenv("FILL_MODE", "intraday")
    desk.book.fill_mode = "intraday"
    return desk


def _pin(desk, day: str, hour: int, minute: int) -> datetime:
    """Point decisions' clock *and* the book's clock at the same instant."""
    moment = datetime.strptime(day, "%Y%m%d").replace(
        hour=hour, minute=minute, second=0, tzinfo=SHANGHAI)
    desk.decisions._now = lambda: moment
    desk.book.clock = lambda: moment
    return moment


def _setup_at(desk, hour: int, minute: int, symbol: str | None = None):
    """Pin the clocks, then build one fresh quote at that timestamp."""
    day = desk.market.latest_bar_date()
    _pin(desk, day, hour, minute)
    symbol = symbol or pick_symbols(desk, 1)[0]
    shot = _quote(desk, symbol, age_seconds=5.0, is_realtime=True)
    desk.book.quotes = lambda symbols: {s: shot for s in symbols}
    return day, symbol


def _mode_desk(desk, monkeypatch, mode: str):
    """Switch the settlement mode on the *existing* desk.

    The conftest fixture builds with ``FILL_MODE=close``; setting the attribute
    is what ``execute_ticket``/``propose`` read, and the env is set too so a
    re-read (or a nested ``build_desk``) agrees.
    """
    monkeypatch.setenv("FILL_MODE", mode)
    desk.book.fill_mode = mode
    return desk


def _cash(desk):
    return desk.book.ledger.account.cash_available


def _buy(desk, symbol):
    return desk.decisions.propose(proposal(symbol), actor="test")


# ---------------------------------------------------------------------------
# C1 · AFTER_HOURS (15:01) is queued, not terminally rejected
# ---------------------------------------------------------------------------
def test_after_hours_approve_queues_next_open_instead_of_terminal_reject(intraday_desk):
    desk = intraday_desk
    day, symbol = _setup_at(desk, 15, 1)
    cash_before = _cash(desk)

    ticket = _buy(desk, symbol)
    assert ticket["status"] == "proposed", ticket.get("reasons")
    result = desk.decisions.approve(ticket["ticket_id"], actor="test")

    # Old code: oms.submit(15:01) -> AFTER_HOURS -> terminal rejected.
    assert result["status"] == "queued", result
    stored = result["ticket"]
    assert stored["status"] == "queued"
    assert not stored.get("rejected_at"), "尾盘单不能被置为终态"
    assert stored["queue_kind"] == "next_open"
    assert stored["fill_basis"] == "next_open"

    pending = desk.book.state["pending"]
    assert len(pending) == 1
    assert pending[0]["target_day"] == desk.book._next_session(day)

    # No fill and no cash movement: this is the "C1 现金不变" cross-check.
    assert desk.book.state["fills"] == []
    assert _cash(desk) == cash_before


# ---------------------------------------------------------------------------
# C3 · AUCTION_CLOSE (14:58) queues instead of pricing at the 14:58 tape
# ---------------------------------------------------------------------------
def test_auction_close_approve_queues_instead_of_pricing_at_intraday(intraday_desk):
    desk = intraday_desk
    day, symbol = _setup_at(desk, 14, 58)
    cash_before = _cash(desk)

    ticket = _buy(desk, symbol)
    result = desk.decisions.approve(ticket["ticket_id"], actor="test")

    # Old code: _match_close priced at bar.close == the 14:58 quote, with no
    # slippage — a tape print passed off as the 15:00 closing price.
    assert result["status"] == "queued", result
    assert result["ticket"]["queue_kind"] == "next_open"
    assert result["ticket"]["fill_basis"] == "next_open"
    assert desk.book.state["pending"][0]["target_day"] == desk.book._next_session(day)
    assert desk.book.state["fills"] == []
    assert _cash(desk) == cash_before


# ---------------------------------------------------------------------------
# PRE_OPEN (09:26) keeps its *same-day* open queue (no regression)
# ---------------------------------------------------------------------------
def test_pre_open_approve_keeps_same_day_open_queue(intraday_desk):
    desk = intraday_desk
    day, symbol = _setup_at(desk, 9, 26)
    cash_before = _cash(desk)

    ticket = _buy(desk, symbol)
    result = desk.decisions.approve(ticket["ticket_id"], actor="test")

    assert result["status"] == "queued", result
    stored = result["ticket"]
    assert stored["queue_kind"] == "open"
    assert stored["fill_basis"] == "open_auction"
    # The open queue bets today's opening print, not tomorrow's.
    assert desk.book.state["pending"][0]["target_day"] == day
    assert desk.book.state["fills"] == []
    assert _cash(desk) == cash_before


# ---------------------------------------------------------------------------
# AUCTION_OPEN (09:16) joins the *same-day* open queue, not a terminal reject
# ---------------------------------------------------------------------------
def test_auction_open_approve_queues_same_day_open(intraday_desk):
    """09:15-09:25: ``can_submit`` allows the order but ``match`` cannot print.

    The matcher returns MARKET_CLOSED for a non-CONTINUOUS session, so before
    this batch an intraday ticket created at 09:16 went through ``oms.submit``,
    was refused, and ``_reject_execution`` wrote a **terminal** rejection. The
    exchange's own model is that the order joins this session's opening print —
    ``execute_ticket`` now routes AUCTION_OPEN to the same-day ``open`` queue
    (``queue_kind="open"``, ``fill_basis="open_auction"``, ``target_day=today``),
    and ``_execute_live`` names AUCTION_OPEN too as a second line of defence.
    """
    desk = intraday_desk
    day, symbol = _setup_at(desk, 9, 16)
    cash_before = _cash(desk)

    ticket = _buy(desk, symbol)
    assert ticket["status"] == "proposed", ticket.get("reasons")
    result = desk.decisions.approve(ticket["ticket_id"], actor="test")

    assert result["status"] == "queued", result
    stored = result["ticket"]
    assert stored["status"] == "queued"
    assert not stored.get("rejected_at"), "开盘竞价单不能被置为终态"
    assert stored["queue_kind"] == "open"
    assert stored["fill_basis"] == "open_auction"
    # Same day — the opening auction of *this* session, not tomorrow's open.
    assert desk.book.state["pending"][0]["target_day"] == day
    assert desk.book.state["fills"] == []
    assert _cash(desk) == cash_before


# ---------------------------------------------------------------------------
# CONTINUOUS (10:30) still fills intraday — the positive control
# ---------------------------------------------------------------------------
def test_continuous_approve_still_fills_intraday(intraday_desk):
    desk = intraday_desk
    day, symbol = _setup_at(desk, 10, 30)
    cash_before = _cash(desk)

    ticket = _buy(desk, symbol)
    result = desk.decisions.approve(ticket["ticket_id"], actor="test")

    assert result["status"] == "filled", result
    assert result["fill"]["fill_id"]
    assert result["ticket"]["fill_basis"] == "intraday"
    assert len(desk.book.state["fills"]) == 1
    # Continuous trading is untouched: the order actually spends cash.
    assert _cash(desk) < cash_before


# ---------------------------------------------------------------------------
# SELL is direction-agnostic: a 14:58 sell queues too (not a guard bypass)
# ---------------------------------------------------------------------------
def test_auction_close_sell_queues_instead_of_filling(intraday_desk):
    desk = intraday_desk
    # Give the desk a sellable position first (bought on an earlier session).
    day_buy, symbol = _setup_at(desk, 10, 30)
    bought = _buy(desk, symbol)
    assert desk.decisions.approve(bought["ticket_id"], actor="test")["status"] == "filled"
    position = desk.book.ledger.account.positions[symbol]
    position.available_qty = position.total_qty
    position.locked_qty = 0
    desk.book.persist()
    cash_before = _cash(desk)

    _setup_at(desk, 14, 58)
    sell = desk.decisions.propose(
        proposal(symbol, side="SELL", qty=100, target_weight=None), actor="test")
    assert sell["status"] == "proposed", sell.get("reasons")
    result = desk.decisions.approve(sell["ticket_id"], actor="test")

    # Not an immediate fill at the tail print; the ticket stays recoverable.
    assert result["status"] == "queued", result
    assert result["ticket"]["status"] == "queued"
    assert result["ticket"]["fill_basis"] == "next_open"
    assert _cash(desk) == cash_before, "尾盘卖单不该在错误价格上成交"
    assert desk.book.ledger.account.positions[symbol].total_qty > 0, "仓位仍在"


# ---------------------------------------------------------------------------
# C4 · propose annotates the session and whether it will queue to next open
# ---------------------------------------------------------------------------
def test_propose_flags_will_queue_next_open_in_auction_close(intraday_desk):
    desk = intraday_desk
    _setup_at(desk, 14, 58)

    ticket = _buy(desk, pick_symbols(desk, 1)[0])

    assert ticket["session"] == "AUCTION_CLOSE"
    assert ticket["will_queue_next_open"] is True


def test_propose_after_hours_marks_queue(intraday_desk):
    """15:01 is AFTER_HOURS: still a next-open queue, and the ticket says so."""
    desk = intraday_desk
    _setup_at(desk, 15, 1)

    ticket = _buy(desk, pick_symbols(desk, 1)[0])

    assert ticket["session"] == "AFTER_HOURS"
    assert ticket["will_queue_next_open"] is True


# ---------------------------------------------------------------------------
# C4 × fill_mode · "will this wait for the next open" depends on the mode too
#
# These three cases exist because the first four C4 cases all ran under
# ``intraday`` — the non-default mode — so a session-only formula looked correct
# while being wrong for the default ``next_open`` and the conftest ``close``.
# ---------------------------------------------------------------------------
def test_will_queue_next_open_in_next_open_mode(desk, monkeypatch):
    """``next_open`` waits for an open in every window except the opening auction."""
    _mode_desk(desk, monkeypatch, "next_open")
    # Two *different* symbols: ``propose`` dedupes by intent fingerprint, and two
    # identical ``proposal(symbol)`` bodies would return the first ticket.
    symbols = pick_symbols(desk, 2)
    assert len(symbols) == 2

    _setup_at(desk, 10, 30, symbols[0])
    continuous = _buy(desk, symbols[0])
    assert continuous["session"] == "CONTINUOUS"
    # Session alone would say False; the mode makes this wait for the next open.
    assert continuous["will_queue_next_open"] is True

    _setup_at(desk, 9, 16, symbols[1])
    auction_open = _buy(desk, symbols[1])
    assert auction_open["session"] == "AUCTION_OPEN"
    # The opening auction joins *today's* open, so it does not queue to tomorrow.
    assert auction_open["will_queue_next_open"] is False


def test_will_queue_next_open_in_close_mode(desk, monkeypatch):
    """``close`` settles this session's close, so it never queues to the next open."""
    _mode_desk(desk, monkeypatch, "close")

    _setup_at(desk, 15, 1)
    ticket = _buy(desk, pick_symbols(desk, 1)[0])

    assert ticket["session"] == "AFTER_HOURS"
    # Session alone would say True; close-mode settles this session instead.
    assert ticket["will_queue_next_open"] is False


def test_will_queue_next_open_matches_behaviour_in_next_open(desk, monkeypatch):
    """The flag and the queue must be the same fact, not two guesses."""
    _mode_desk(desk, monkeypatch, "next_open")
    day, symbol = _setup_at(desk, 10, 30)

    ticket = _buy(desk, symbol)
    assert ticket["will_queue_next_open"] is True

    result = desk.decisions.approve(ticket["ticket_id"], actor="test")
    assert result["status"] == "queued", result
    assert result["ticket"]["queue_kind"] == "next_open"
    assert desk.book.state["pending"][0]["target_day"] == desk.book._next_session(day)


def test_propose_flags_continuous_as_not_queueing(intraday_desk):
    desk = intraday_desk
    _setup_at(desk, 10, 30)

    ticket = _buy(desk, pick_symbols(desk, 1)[0])

    assert ticket["session"] == "CONTINUOUS"
    assert ticket["will_queue_next_open"] is False


def test_propose_flags_pre_open_as_not_queueing(intraday_desk):
    """PRE_OPEN joins *today's* opening print, so it must not claim "next open"."""
    desk = intraday_desk
    _setup_at(desk, 9, 26)

    ticket = _buy(desk, pick_symbols(desk, 1)[0])

    assert ticket["session"] == "PRE_OPEN"
    assert ticket["will_queue_next_open"] is False


# ---------------------------------------------------------------------------
# Non-trading day · live_fill.build refuses (explicit calendar)
# ---------------------------------------------------------------------------
def test_live_fill_refuses_a_non_trading_day():
    """A quote dated on a non-trading day cannot become a Bar.

    This is the ``build_live`` half of C3: ``_execute_live`` now queues on a
    non-trading day via ``session_at`` -> CLOSED, but the price layer must still
    refuse to *construct* a bar from a quote stamped on a non-trading day.
    """
    now = datetime(2026, 11, 7, 10, 30, tzinfo=SHANGHAI)   # a Saturday
    quote = SimpleNamespace(
        symbol="600000.SH", is_realtime=True, source="ths",
        quote_ts=now.isoformat(), age_seconds=5.0,
        last=11.0, pre_close=10.0, high=11.0, low=10.0,
        up_limit=11.0, down_limit=9.0, volume=100_000, amount=1_100_000.0,
        suspended=False,
    )
    market = SimpleNamespace(calendar=SimpleNamespace(is_trading_day=lambda day: False))

    live, refusal = live_fill.build(market, "600000.SH", quote, now=now)

    assert live is None
    assert refusal is not None and refusal.code == "NON_TRADING_DAY"
