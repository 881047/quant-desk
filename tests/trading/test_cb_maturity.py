"""A bond that stops existing must stop being a position.

The desk opened the convertible channel before it had the lifecycle: `cb_basic`
knows the delist date, but nothing read it. A held bond would keep its last
print forever, and every number that starts from equity — drawdown, the daily
loss breaker, the stop view — would inherit a price nobody can transact at.

What this covers: principal at par on the delisting session, and a time fence
that refuses new buys inside the last two weeks.
What it does not: the final coupon, the 20% interest tax, voluntary conversion,
and the forced-redemption call date (the announcement feed carries titles, not a
redemption schedule).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from service.kernel.corp_action import CBMaturity
from service.kernel.models import Account, D, Position


def _account_with_bond(symbol: str = "110081.SH", qty: int = 10,
                       avg_cost: str = "91.00") -> Account:
    account = Account(cash_available=D("1000"), peak_equity=D("2000"))
    account.positions[symbol] = Position(
        symbol=symbol, total_qty=qty, available_qty=qty,
        avg_cost=D(avg_cost), last_price=D("95.00"), sleeve="CORE")
    return account


def _bond_day(desk) -> str:
    """The last session the *bond* feed covers.

    ``latest_bar_date()`` answers from the stock feed, and the two are ingested
    on separate schedules — during that gap a bond has no bar on the stock
    calendar's "today", so picking the day from there made the frame empty and
    the failure surfaced as ``.iloc[0]`` rather than as "wrong date".
    """
    day = desk.market.dataset_cover_end("cb_daily")
    assert day, "cb_daily has no published partition"
    return day


def test_redemption_pays_par_and_closes_the_position():
    account = _account_with_bond()
    row = CBMaturity("110081.SH", date(2026, 11, 2)).apply(account)

    assert row["applied"] and row["kind"] == "cb_maturity"
    assert row["qty"] == 10 and row["price"] == "100"
    assert account.cash_available == D("2000")          # 1,000 + 10 × 100
    assert account.positions["110081.SH"].total_qty == 0
    assert account.positions["110081.SH"].available_qty == 0
    # 10 × (100 − 91), realised, no commission on a redemption.
    assert account.realized_pnl == D("90")


def test_redemption_without_a_position_is_a_no_op():
    account = Account(cash_available=D("5000"))
    row = CBMaturity("110081.SH", date(2026, 11, 2)).apply(account)
    assert row["applied"] is False and row["reason"] == "NO_POSITION"
    assert account.cash_available == D("5000")


def test_maturity_without_position_still_expires_an_open_buy():
    """A delisted bond cannot leave an unfilled buy reserving cash forever."""
    from datetime import datetime

    from service.kernel.calendar import TradingCalendar
    from service.kernel.clock import SHANGHAI
    from service.kernel.ledger import Ledger
    from service.kernel.models import Bar, Board, Instrument, Intent, OrderType, Side

    symbol = "113001.SH"
    inst = Instrument(symbol, board=Board.CB, min_qty=10, qty_step=10,
                      price_tick=D("0.001"), t_plus=0, limit_pct=D("0.20"),
                      stamp_duty=D("0"), transfer_fee=D("0"))
    ts = datetime(2026, 11, 4, 9, 30, tzinfo=SHANGHAI)
    bar = Bar(symbol=symbol, ts=ts, open=D("100"), high=D("110"), low=D("90"),
              close=D("100"), pre_close=D("100"), volume=1_000_000,
              amount=D("100000000"), up_limit=D("120"), down_limit=D("80"))
    ledger = Ledger.fresh("10000")
    order = ledger.submit(
        Intent(symbol, Side.BUY, qty=10, order_type=OrderType.LIMIT,
               limit_price=D("95")), ts, inst, bar, TradingCalendar.always())
    assert order.is_open and ledger.account.cash_frozen > 0

    row = CBMaturity(symbol, date(2026, 11, 4)).apply(
        ledger.account, oms=ledger.oms, ts=ts.replace(hour=15))

    assert row["applied"] is False and row["reason"] == "NO_POSITION"
    assert row["expired_orders"] == 1
    assert not order.is_open
    assert ledger.account.cash_frozen == D("0")
    assert ledger.account.cash_available == D("10000")


def test_book_maturity_without_position_still_expires_an_open_buy(desk, monkeypatch):
    """The production event loop must not skip lifecycle cleanup on an empty position."""
    from datetime import datetime

    from service.kernel.calendar import TradingCalendar
    from service.kernel.clock import SHANGHAI
    from service.kernel.models import Intent, OrderType, Side

    day = _bond_day(desk)
    frame = desk.market.cb_daily(day)
    symbol = str(frame.iloc[0]["ts_code"])
    bar, inst = desk.market.bar(symbol, day)
    ts = datetime.strptime(day, "%Y%m%d").replace(hour=9, minute=30, tzinfo=SHANGHAI)
    order = desk.book.ledger.submit(
        Intent(symbol, Side.BUY, qty=inst.min_qty, order_type=OrderType.LIMIT,
               limit_price=bar.open, client_order_id="delisting-open-buy"),
        ts, inst, bar, TradingCalendar.always())
    assert order.is_open and desk.book.ledger.account.cash_frozen > 0
    desk.book.state["actions_through"] = desk.market.calendar.prev(day)
    event = CBMaturity(symbol, date(int(day[:4]), int(day[4:6]), int(day[6:8])))
    monkeypatch.setattr(desk.market, "cb_maturities", lambda _day: [event])
    monkeypatch.setattr(desk.market, "actions", lambda _day: [])
    monkeypatch.setattr(desk.market, "cb_coupons", lambda _day: [])

    desk.book.apply_corporate_actions(day)

    assert not order.is_open
    assert desk.book.ledger.account.cash_frozen == D("0")


def test_the_master_yields_events_on_their_own_delist_date(desk):
    master = desk.market.cb_master()
    sample = next(((code, info) for code, info in master.items()
                   if len(info["delist_date"]) == 8), None)
    assert sample is not None, "the bond master carries delisting dates"
    code, info = sample

    assert any(event.symbol == code
               for event in desk.market.cb_maturities(info["delist_date"]))
    assert code not in {event.symbol
                        for event in desk.market.cb_maturities("20260105")} or \
        info["delist_date"] == "20260105"


def test_the_book_redeems_a_held_bond_on_its_last_session(desk, monkeypatch):
    day = desk.market.latest_bar_date()
    symbol = "110081.SH"
    desk.book.ledger.account.positions[symbol] = Position(
        symbol=symbol, total_qty=10, available_qty=10, avg_cost=D("91"),
        last_price=D("95"), sleeve="CORE")
    desk.book.state["actions_through"] = desk.market.calendar.prev(day)

    event = CBMaturity(symbol, date(int(day[:4]), int(day[4:6]), int(day[6:8])))
    monkeypatch.setattr(desk.market, "cb_maturities", lambda _day: [event])
    monkeypatch.setattr(desk.market, "actions", lambda _day: [])

    before = desk.book.ledger.account.cash_available
    applied = desk.book.apply_corporate_actions(day)

    assert any(row.get("kind") == "cb_maturity" for row in applied)
    assert desk.book.ledger.account.positions[symbol].total_qty == 0
    assert desk.book.ledger.account.cash_available == before + D("1000")


def test_a_bond_inside_the_last_two_weeks_cannot_be_bought(desk, monkeypatch):
    day = _bond_day(desk)
    frame = desk.market.cb_daily(day)
    frame = frame[(frame["vol"] > 0) & (frame["close"] < 135)]
    symbol = str(frame.iloc[0]["ts_code"])
    price = D(str(frame.iloc[0]["close"]))

    monkeypatch.setattr(desk.market, "cb_days_to_delist", lambda *a, **k: 5)
    result = desk.guard.preflight(desk.book, symbol=symbol, side="BUY",
                                  sleeve="CORE", qty=10, price=price,
                                  date=day, strategy="llm")
    check = next(row for row in result.checks if row.name == "cb_maturity")
    assert check.ok is False and "5 sessions" in check.detail


def test_a_bond_far_from_delisting_passes(desk, monkeypatch):
    day = _bond_day(desk)
    frame = desk.market.cb_daily(day)
    frame = frame[(frame["vol"] > 0) & (frame["close"] < 135)]
    symbol = str(frame.iloc[0]["ts_code"])
    price = Decimal(str(frame.iloc[0]["close"]))

    monkeypatch.setattr(desk.market, "cb_days_to_delist", lambda *a, **k: 400)
    result = desk.guard.preflight(desk.book, symbol=symbol, side="BUY",
                                  sleeve="CORE", qty=10, price=price,
                                  date=day, strategy="llm")
    check = next(row for row in result.checks if row.name == "cb_maturity")
    assert check.ok is True


def test_selling_is_never_blocked_by_the_fence(desk, monkeypatch):
    day = _bond_day(desk)
    frame = desk.market.cb_daily(day)
    frame = frame[(frame["vol"] > 0) & (frame["close"] < 135)]
    symbol = str(frame.iloc[0]["ts_code"])
    price = Decimal(str(frame.iloc[0]["close"]))
    desk.book.ledger.account.positions[symbol] = Position(
        symbol=symbol, total_qty=10, available_qty=10, avg_cost=D("90"),
        last_price=price, sleeve="CORE")

    monkeypatch.setattr(desk.market, "cb_days_to_delist", lambda *a, **k: 1)
    result = desk.guard.preflight(desk.book, symbol=symbol, side="SELL",
                                  sleeve="CORE", qty=10, price=price,
                                  date=day, strategy="llm")
    assert not any(row.name == "cb_maturity" for row in result.checks)
