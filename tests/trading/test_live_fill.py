"""Live fills: the path that lets a woken model act inside the session.

The tests pin the two properties that make this honest: a fill stamped
``intraday`` must be backed by a real-time quote, and when that quote cannot be
trusted the order must fall through to the next open rather than invent a price.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from service.kernel.clock import SHANGHAI
from service.trading import live_fill, realtime
from service.trading.decisions import DecisionError
from tests.trading.helpers import pick_symbols, proposal


# `build` takes the clock so a test does not depend on the hour it runs:
# after midnight "today" is the next (non-trading) day and every live fill is
# refused as NON_TRADING_DAY — correct behaviour, wrong assertion.
def _quote(desk, symbol: str, **overrides):
    day = desk.market.latest_bar_date()
    snap = desk.market.snapshot(symbol, day)
    fields = {
        "symbol": symbol,
        "name": snap.name if hasattr(snap, "name") else symbol,
        "last": snap.last,
        "pre_close": snap.pre_close,
        "open": snap.open,
        "high": snap.high,
        "low": snap.low,
        "volume": float(snap.volume),
        "amount": float(snap.amount),
        "up_limit": snap.up_limit,
        "down_limit": snap.down_limit,
        # Pinned to the desk's trading day, not the wall clock: `desk.book.clock()`
        # is the real one, so a suite that ran past midnight produced a quote dated
        # the next (non-trading) day and live_fill refused it as NON_TRADING_DAY —
        # three tests failed for the time of day rather than for the code.
        "quote_ts": desk.decisions._now().isoformat(timespec="seconds"),
        "age_seconds": 1.5,
        "source": "ths",
        "is_realtime": True,
        "suspended": snap.suspended,
    }
    fields.update(overrides)
    return realtime.Quote(**fields)


def _next_session(desk):
    """The next trading day that has no published bar.

    The intraday path exists for "the session is open and today's bar is not out
    yet". This used to be pinned to 2026-10-26, which worked only while the real
    data lagged that date: the moment the daily job backfilled 2026-10-26 the bar
    became today's, and these tests silently stopped exercising the intraday case.
    Deriving the day from the data keeps the premise true after every refresh.
    """
    bar = desk.market.latest_bar_date()
    start = datetime.strptime(bar, "%Y%m%d").date()
    calendar = desk.market.calendar
    for step in range(1, 15):
        candidate = start + timedelta(days=step)
        if calendar.is_trading_day(candidate.strftime("%Y%m%d")):
            return candidate
    raise AssertionError("no trading day within two weeks of the last bar")


def _at_hour(desk, hour: int, minute: int = 0) -> datetime:
    day = _next_session(desk)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SHANGHAI)


def _at(desk, moment: datetime) -> None:
    """Point every clock in the fill path at the same moment.

    The decision service, the book and the OMS each ask "what time is it". If the
    test moved only one of them the fill would be stamped 10:30 and submitted at
    17:00, and the OMS would reject it as MARKET_CLOSED - which is exactly what
    happened the first time this was written.
    """
    desk.decisions._now = lambda: moment
    desk.book.clock = lambda: moment


# ------------------------------------------------------------------ the bar
def test_a_stored_close_cannot_be_dressed_up_as_live(desk):
    symbol = pick_symbols(desk, 1)[0]
    quote = _quote(desk, symbol, source="daily", is_realtime=False)

    live, refusal = live_fill.build(desk.market, symbol, quote, now=desk.decisions._now())
    assert live is None
    assert refusal.code == "NOT_LIVE"
    assert "伪装" in refusal.message


def test_a_stale_quote_is_refused(desk):
    symbol = pick_symbols(desk, 1)[0]
    live, refusal = live_fill.build(desk.market, symbol, _quote(desk, symbol, age_seconds=600),
                    now=desk.decisions._now())
    assert live is None
    assert refusal.code == "STALE_QUOTE"


def test_the_bar_comes_from_the_quote_not_from_the_store(desk):
    symbol = pick_symbols(desk, 1)[0]
    shot = _quote(desk, symbol, last=Decimal("7.77"))

    live, refusal = live_fill.build(desk.market, symbol, shot, now=desk.decisions._now())
    assert refusal is None
    assert live is not None
    assert live.bar.close == Decimal("7.77")
    assert live.source == "ths"
    assert live.bar.volume == int(shot.volume)
    # The instrument still comes from the store: board, ST flag, lot rules.
    assert live.instrument.symbol == symbol


# --------------------------------------------------------------- the book
@pytest.fixture
def intraday_desk(desk, monkeypatch):
    monkeypatch.setenv("FILL_MODE", "intraday")
    desk.book.fill_mode = "intraday"
    _at(desk, _at_hour(desk, 10, 30))
    return desk


def test_an_intraday_order_fills_at_the_live_price(intraday_desk):
    desk = intraday_desk
    symbol = pick_symbols(desk, 1)[0]
    shot = _quote(desk, symbol)
    desk.book.quotes = lambda symbols: {s: shot for s in symbols}

    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    assert ticket["status"] == "proposed", ticket.get("reasons")
    result = desk.decisions.approve(ticket["ticket_id"], actor="test")

    assert result["status"] == "filled", result
    fill = desk.paper.list_orders(limit=1)[0]
    assert fill["fill_basis"] == "intraday"
    # The matcher adds the stock's 10 bps of slippage to a marketable limit; what
    # must never happen is a fill at the session open, which is hours old by now.
    price = Decimal(fill["price"])
    assert shot.last <= price <= shot.last * Decimal("1.005")
    assert price != shot.open, "a live fill cannot happen at the 09:30 print"

    record = [f for f in desk.book.state["fills"] if f["symbol"] == symbol][-1]
    assert record["fill_basis"] == "intraday"
    assert record["price_source"] == "ths"
    assert record["quote_age_seconds"] == 1.5


def test_outside_the_session_the_order_is_queued_not_faked(intraday_desk):
    desk = intraday_desk
    symbol = pick_symbols(desk, 1)[0]
    _at(desk, _at_hour(desk, 20))
    shot = _quote(desk, symbol)
    desk.book.quotes = lambda symbols: {s: shot for s in symbols}

    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    result = desk.decisions.approve(ticket["ticket_id"], actor="test")

    assert result["status"] == "queued", result
    assert desk.book.actions_on(desk.market.latest_bar_date()) == 0
    recorded = [d for d in desk.paper.list_decisions(limit=10)
                if d.get("kind") == "live_refused"]
    assert recorded, "a fallback that leaves no trace is a silent rewrite"
    assert "MARKET_CLOSED" in str(recorded[0]["payload"])


def test_a_vendor_outage_refuses_rather_than_pricing_on_a_stale_close(intraday_desk):
    """No bar for today and no live quote means there is no price, full stop.

    Queueing here would be worse than refusing: the order would carry a limit
    derived from yesterday's close and then try to execute at tomorrow's open,
    which is two stale prices stacked on one decision.
    """
    desk = intraday_desk
    symbol = pick_symbols(desk, 1)[0]
    # source=daily is what the service returns when the vendors fail.
    shot = _quote(desk, symbol, source="daily", is_realtime=False)
    desk.book.quotes = lambda symbols: {s: shot for s in symbols}

    with pytest.raises(DecisionError) as exc:
        desk.decisions.propose(proposal(symbol), actor="test")
    assert exc.value.code == "DATA_NOT_READY"
    assert desk.book.actions_on(desk.market.latest_bar_date()) == 0


def test_a_suspended_stock_is_refused_by_the_matcher(intraday_desk):
    desk = intraday_desk
    symbol = pick_symbols(desk, 1)[0]
    shot = _quote(desk, symbol, suspended=True)
    desk.book.quotes = lambda symbols: {s: shot for s in symbols}

    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    result = desk.decisions.approve(ticket["ticket_id"], actor="test")

    assert result["status"] == "rejected", result
    assert "suspended" in " ".join(result.get("reasons") or []).lower()
