"""Execution conservation, atomic recovery and live-quote boundary tests."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.models import Bar, D, Instrument, Intent, OrderType, Side
from service.trading import observation
from service.trading.book import PaperBook
from service.trading.decisions import DecisionService
from service.trading.live_fill import build
from service.trading.market import Snapshot
from service.trading.state import PaperStore, StateConflict
from service.trading.stops import StopView

DAY1, DAY2 = "20260105", "20260106"
SYMBOL = "600000.SH"


def make_bar(day: str, *, open_: str = "10.10", high: str = "10.30",
             low: str = "10.05", close: str = "10.20", volume: int = 5_000_000) -> Bar:
    ts = datetime.strptime(day, "%Y%m%d").replace(hour=15, tzinfo=SHANGHAI)
    return Bar(symbol=SYMBOL, ts=ts, open=D(open_), high=D(high), low=D(low),
               close=D(close), pre_close=D("10.00"), volume=volume,
               amount=D(close) * volume, up_limit=D("11.00"), down_limit=D("9.00"))


class _Calendar:
    def calendar(self):
        return TradingCalendar.always()

    def is_trading_day(self, day: str) -> bool:
        return True

    def shift(self, day: str, n: int) -> str:
        return (datetime.strptime(day, "%Y%m%d") + timedelta(days=n)).strftime("%Y%m%d")


class StubMarket:
    def __init__(self, bars=None, *, qty_step: int = 100):
        self.bars = bars if bars is not None else {DAY1: make_bar(DAY1), DAY2: make_bar(DAY2)}
        self.calendar = _Calendar()
        self.qty_step = qty_step

    def bar(self, symbol: str, date: str):
        bar = self.bars.get(date)
        if bar is None or symbol != SYMBOL:
            return None
        return bar, Instrument(symbol=symbol, name="stub", min_qty=100, qty_step=self.qty_step)

    def instrument(self, symbol: str, date: str):
        return Instrument(symbol=symbol, name="stub", min_qty=100, qty_step=self.qty_step)

    def names(self):
        return {SYMBOL: "stub"}

    def snapshot(self, symbol: str, date: str):
        bar = self.bars.get(date)
        if bar is None:
            return None
        return Snapshot(symbol=symbol, trade_date=date, last=bar.close,
                        pre_close=bar.pre_close, open=bar.open, high=bar.high,
                        low=bar.low, up_limit=bar.up_limit, down_limit=bar.down_limit,
                        volume=float(bar.volume), amount=float(bar.amount),
                        suspended=bar.suspended, source="stub")

    def last_trading_day(self, as_of: str | None = None) -> str:
        return DAY2

    def actions(self, day: str):
        return []


def ticket(ticket_id: str = "td-test", *, side: str = "BUY", qty: int = 100,
           limit: str = "10.20", day: str = DAY1) -> dict:
    return {
        "ticket_id": ticket_id, "created_at": "2026-01-05T17:00:00+08:00",
        "expires_at": f"{day}T23:59:59+08:00", "status": "proposed",
        "symbol": SYMBOL, "side": side, "sleeve": "CORE", "strategy": "llm",
        "qty": qty, "limit_price": limit, "trade_date": day,
        "intent_type": "test", "thesis": "合成执行安全性回归测试，不接触真实账户。",
        "invalidation": "任何冻结泄漏或重复成交都使测试失败", "stop_pct": 0.05,
        "stop_price": "9.50",
    }


def make_book(db, market: StubMarket, mode: str, *, shares: int = 0) -> PaperBook:
    book = PaperBook(PaperStore(db), market, initial_cash="5000", fill_mode=mode)
    book.clock = lambda: datetime(2026, 1, 6, 10, 30, tzinfo=SHANGHAI)
    if shares:
        pos = book.ledger.account.position(SYMBOL)
        pos.total_qty = pos.available_qty = shares
        pos.avg_cost = pos.last_price = D("10.00")
        book.persist()
    return book


def quote(book: PaperBook, **overrides):
    fields = {
        "last": D("10.10"), "pre_close": D("10.00"), "high": D("10.20"),
        "low": D("9.90"), "up_limit": D("11.00"), "down_limit": D("9.00"),
        "volume": 5_000_000.0, "amount": 50_000_000.0,
        "quote_ts": book.clock().isoformat(timespec="seconds"), "age_seconds": 1.0,
        "source": "test", "is_realtime": True, "suspended": False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def assert_conserved(book: PaperBook) -> None:
    account = book.ledger.account
    assert account.cash_frozen == 0
    assert all(pos.frozen_qty == 0 for pos in account.positions.values())
    assert book.ledger.oms.open_orders() == []
    assert book.state["pending"] == []
    assert book.store.pending_ticket_ids() == []


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_queued_no_fill_releases_reservations_and_survives_restart(tmp_path, side):
    db = tmp_path / "paper.sqlite"
    market = StubMarket()
    book = make_book(db, market, "next_open", shares=300 if side == "SELL" else 0)
    limit = "10.20" if side == "BUY" else "10.60"
    cash_before = book.ledger.account.cash_available
    qty_before = book.ledger.account.position(SYMBOL).total_qty
    row = ticket(side=side, limit=limit)
    assert book.execute_ticket(row).status == "queued"
    market.bars[DAY2] = (make_bar(DAY2, open_="10.60", high="10.80", low="10.50", close="10.70")
                         if side == "BUY" else make_bar(DAY2))

    assert book.settle_pending(DAY2)[0]["status"] == "cancelled"
    assert_conserved(book)
    assert book.ledger.account.cash_available == cash_before
    assert book.ledger.account.position(SYMBOL).total_qty == qty_before
    assert book.state["fills"] == []

    restarted = make_book(db, market, "next_open")
    restarted.rollover(DAY2)  # no open order may be silently dropped by settle
    assert_conserved(restarted)
    assert restarted.ledger.account.cash_available == cash_before
    assert restarted.ledger.account.position(SYMBOL).total_qty == qty_before


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_queued_partial_cancels_remainder_and_restart_is_idempotent(tmp_path, side):
    db = tmp_path / "paper.sqlite"
    market = StubMarket({DAY1: make_bar(DAY1), DAY2: make_bar(DAY2, volume=1500)},
                        qty_step=1 if side == "SELL" else 100)
    book = make_book(db, market, "next_open", shares=300 if side == "SELL" else 0)
    row = ticket(side=side, qty=200, limit="10.20" if side == "BUY" else "9.90")
    book.execute_ticket(row)

    outcome = book.settle_pending(DAY2)[0]
    assert outcome["status"] == "filled" and outcome["qty"] == (150 if side == "SELL" else 100)
    assert_conserved(book)
    assert len(book.state["fills"]) == 1
    assert book.store.get_ticket(row["ticket_id"])["status"] == "filled"
    cash_after = book.ledger.account.cash_available
    qty_after = book.ledger.account.position(SYMBOL).total_qty

    restarted = make_book(db, market, "next_open")
    assert_conserved(restarted)
    assert restarted.execute_ticket(row).status == "already_filled"
    assert len(restarted.state["fills"]) == 1
    assert restarted.ledger.account.cash_available == cash_after
    assert restarted.ledger.account.position(SYMBOL).total_qty == qty_after


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_live_no_fill_releases_reservations(tmp_path, side):
    db = tmp_path / "paper.sqlite"
    book = make_book(db, StubMarket(), "intraday", shares=300 if side == "SELL" else 0)
    limit = "9.80" if side == "BUY" else "10.30"
    row = ticket(side=side, limit=limit, day=DAY2)
    book.quotes = lambda symbols: {SYMBOL: quote(book)}

    result = book.execute_ticket(row)
    assert not result.ok and result.status == "rejected"
    assert_conserved(book)
    assert book.state["fills"] == []
    assert make_book(db, StubMarket(), "intraday").state["fills"] == []


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_live_partial_cancels_remainder_and_finalizes_provenance(tmp_path, side):
    db = tmp_path / "paper.sqlite"
    market = StubMarket(qty_step=1 if side == "SELL" else 100)
    book = make_book(db, market, "intraday", shares=300 if side == "SELL" else 0)
    row = ticket(side=side, qty=200, limit="10.20" if side == "BUY" else "9.90", day=DAY2)
    book.quotes = lambda symbols: {SYMBOL: quote(book, volume=1500.0)}

    result = book.execute_ticket(row)
    assert result.ok and result.qty == (150 if side == "SELL" else 100)
    assert_conserved(book)
    stored = book.store.get_ticket(row["ticket_id"])
    assert stored["fill"]["basis"] == "intraday"
    assert stored["fill"]["price_source"] == "test"
    assert stored["fill"]["quote_ts"] == book.clock().isoformat(timespec="seconds")
    if side == "BUY":
        assert stored["stop_basis"] == "fill"
        expected = (D(stored["fill"]["price"]) * D("0.95")).quantize(D("0.01"))
        assert D(stored["stop_price_final"]) == expected

    restarted = make_book(db, StubMarket(), "intraday")
    assert restarted.execute_ticket(row).status == "already_filled"
    assert len(restarted.state["fills"]) == 1


def test_decision_reject_atomically_removes_pending_and_blocks_settlement(tmp_path):
    db = tmp_path / "paper.sqlite"
    market = StubMarket()
    book = make_book(db, market, "next_open")
    row = ticket()
    book.execute_ticket(row)
    decisions = DecisionService(book, market, book.store, None, guard=object())

    rejected = decisions.reject(row["ticket_id"], "operator cancels this test")
    assert rejected["status"] == "rejected"
    assert_conserved(book)
    assert book.settle_pending(DAY2) == []
    assert book.state["fills"] == []
    assert make_book(db, market, "next_open").store.get_ticket(row["ticket_id"])["status"] == "rejected"


def test_settle_refuses_a_pending_row_whose_ticket_is_not_queued(tmp_path):
    book = make_book(tmp_path / "paper.sqlite", StubMarket(), "next_open")
    row = ticket()
    book.execute_ticket(row)
    stored = book.store.get_ticket(row["ticket_id"])
    stored["status"] = "rejected"
    book.store.save_ticket(stored)

    outcomes = book.settle_pending(DAY2)
    assert [item["reason"] for item in outcomes] == ["TICKET_NOT_QUEUED"]
    assert book.state["fills"] == []
    assert_conserved(book)


def test_fill_commit_rolls_back_every_table_and_in_memory_account(tmp_path, monkeypatch):
    db = tmp_path / "paper.sqlite"
    book = make_book(db, StubMarket(), "close")
    row = ticket(day=DAY2, limit="10.20")
    cash_before = book.ledger.account.cash_available

    with monkeypatch.context() as patch:
        def fail_ticket_write(conn, ticket_row):
            raise sqlite3.OperationalError("injected ticket write failure")

        patch.setattr(book.store, "_save_ticket_conn", fail_ticket_write)
        with pytest.raises(sqlite3.OperationalError):
            book.execute_ticket(row)

    assert book.ledger.account.cash_available == cash_before
    assert book.ledger.account.position(SYMBOL).total_qty == 0
    assert book.state["fills"] == []
    assert PaperStore(db).list_orders() == []
    assert PaperStore(db).load_state()["fills"] == []

    assert book.execute_ticket(row).status == "filled"
    restarted = make_book(db, StubMarket(), "close")
    assert restarted.execute_ticket(row).status == "already_filled"
    assert len(restarted.state["fills"]) == 1


def test_stale_process_cannot_last_write_win_the_account(tmp_path):
    db = tmp_path / "paper.sqlite"
    market = StubMarket()
    first = make_book(db, market, "close")
    stale = make_book(db, market, "close")
    assert first.execute_ticket(ticket("td-first", day=DAY2)).status == "filled"
    cash_after = first.ledger.account.cash_available

    with pytest.raises(StateConflict):
        stale.execute_ticket(ticket("td-stale", day=DAY2))

    restarted = make_book(db, market, "close")
    assert [row["ticket_id"] for row in restarted.state["fills"]] == ["td-first"]
    assert restarted.ledger.account.cash_available == cash_after
    assert restarted.store.get_fill("td-stale") is None


def test_legacy_fill_plus_pending_recovers_without_double_execution(tmp_path):
    db = tmp_path / "paper.sqlite"
    market = StubMarket()
    book = make_book(db, market, "next_open")
    row = ticket()
    book.execute_ticket(row)
    pending = deepcopy(book.state["pending"])
    book.settle_pending(DAY2)
    cash_after = book.ledger.account.cash_available
    qty_after = book.ledger.account.position(SYMBOL).total_qty

    # Shape left by the old writer: fill/account committed, pending/ticket lagged.
    book.state["pending"] = pending
    book.store.save_state(book.state)
    stored = book.store.get_ticket(row["ticket_id"])
    stored["status"] = "queued"
    book.store.save_ticket(stored)

    recovered = make_book(db, market, "next_open")
    outcomes = recovered.settle_pending(DAY2)
    assert [item["status"] for item in outcomes] == ["already_filled"]
    assert_conserved(recovered)
    assert len(recovered.state["fills"]) == 1
    assert recovered.ledger.account.cash_available == cash_after
    assert recovered.ledger.account.position(SYMBOL).total_qty == qty_after


def test_legacy_schema_is_upgraded_and_json_rows_are_indexed(tmp_path):
    db = tmp_path / "legacy.sqlite"
    fill = {"ticket_id": "td-old", "fill_id": "fill-old", "order_id": "O00000001",
            "qty": 100, "price": "10.00", "fees": "5.00"}
    pending = {"ticket_id": "td-pending", "symbol": SYMBOL}
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE paper_state (
                        id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL,
                        updated_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO paper_state VALUES (1,?,?)",
                     (json.dumps({"fills": [fill], "pending": [pending]}), "old"))

    store = PaperStore(db)
    payload, revision = store.load_state_with_revision()
    assert revision == 0
    assert payload["fills"] == [fill]
    assert store.get_fill("td-old") == fill
    assert store.pending_ticket_ids() == ["td-pending"]


@pytest.mark.parametrize("overrides, code", [
    ({"quote_ts": None}, "BAD_QUOTE_TS"),
    ({"quote_ts": "not-a-time"}, "BAD_QUOTE_TS"),
    ({"quote_ts": "20260105T10:30:00+08:00"}, "NON_TRADING_DAY"),
    ({"age_seconds": -1.0}, "BAD_QUOTE_AGE"),
    ({"age_seconds": float("nan")}, "BAD_QUOTE_AGE"),
    ({"volume": float("nan")}, "BAD_QUOTE"),
    ({"amount": float("nan")}, "BAD_QUOTE"),
    ({"amount": float("inf")}, "BAD_QUOTE"),
])
def test_live_quote_boundaries_are_refused(tmp_path, overrides, code):
    book = make_book(tmp_path / "paper.sqlite", StubMarket(), "intraday")
    live, refusal = build(book.market, SYMBOL, quote(book, **overrides), now=book.clock())
    assert live is None
    assert refusal.code == code


def test_mark_receipt_uses_the_same_daily_or_live_snapshot(tmp_path):
    book = make_book(tmp_path / "paper.sqlite", StubMarket(), "next_open", shares=100)
    book.mark_prices(DAY2)
    assert book.mark_receipt(SYMBOL) == {
        "price_source": "daily", "price_as_of": DAY2,
        "price_age_seconds": None, "is_realtime": False,
    }

    market = StubMarket(bars={})
    live_book = make_book(tmp_path / "live.sqlite", market, "next_open", shares=100)
    live_book.quotes = lambda symbols: {SYMBOL: quote(live_book)}
    live_book.mark_prices(DAY2, live=True)
    receipt = live_book.mark_receipt(SYMBOL)
    assert receipt["price_source"] == "test"
    assert receipt["price_as_of"] == live_book.clock().isoformat(timespec="seconds")
    assert receipt["is_realtime"] is True


def test_production_snapshot_restores_open_order_and_reservations(tmp_path):
    db = tmp_path / "paper.sqlite"
    market = StubMarket()
    book = make_book(db, market, "next_open")
    inst = market.instrument(SYMBOL, DAY1)
    bar = market.bars[DAY1]
    order = book.ledger.submit(
        Intent(SYMBOL, Side.BUY, qty=100, order_type=OrderType.LIMIT,
               limit_price=D("10.00"), client_order_id="restart-me"),
        bar.ts.replace(hour=10), inst, bar, TradingCalendar.always(),
    )
    book.persist()

    restored = make_book(db, market, "next_open")
    assert restored.ledger.oms.orders[order.order_id].is_open
    assert restored.ledger.oms.client_index["restart-me"].order_id == order.order_id
    account = restored.ledger.account
    assert account.cash_available + account.cash_frozen == D("5000")
    restored.ledger.cancel(order.order_id, bar.ts.replace(hour=10), TradingCalendar.always())
    assert account.cash_available == D("5000")
    assert account.cash_frozen == D("0")


def test_stop_gate_separates_live_breach_from_unknown_price(tmp_path, monkeypatch):
    market = StubMarket(bars={})
    book = make_book(tmp_path / "paper.sqlite", market, "next_open", shares=100)
    desk = SimpleNamespace(market=market, book=book, paper=book.store)
    view = StopView(symbol=SYMBOL, name="stub", qty=100, available=100,
                    stop=D("9.00"), last=D("8.00"), low=D("8.00"), sleeve="CORE",
                    ticket_id="td-stop", breached=True, pierce_only=False, blocked=False)
    monkeypatch.setattr("service.trading.stops.open_stops", lambda *a, **k: [view])
    monkeypatch.setattr("service.trading.stops.stopped_symbols", lambda *a, **k: {SYMBOL})

    gate = observation.stop_gate(desk, DAY1, {SYMBOL: quote(book, last=D("8.00"))})
    assert gate == {"breached": [SYMBOL], "unknown": [], "no_stop": []}
    stale = quote(book, last=D("8.00"), is_realtime=False)
    gate = observation.stop_gate(desk, DAY1, {SYMBOL: stale})
    assert gate == {"breached": [], "unknown": [SYMBOL], "no_stop": []}


def test_a_position_without_a_stop_ticket_is_not_an_unpriced_position(
        tmp_path, monkeypatch):
    """`no stop written` and `no price available` are different states.

    They used to share the `unknown` bucket, and `propose` refuses every buy
    while `unknown` is non-empty. One position bought without a stop therefore
    blocked all future buys with a message saying the *price* could not be
    obtained — while the price was right there, and waiting for quotes could
    never clear it.
    """
    market = StubMarket()
    book = make_book(tmp_path / "paper.sqlite", market, "next_open", shares=100)
    desk = SimpleNamespace(market=market, book=book, paper=book.store)
    monkeypatch.setattr("service.trading.stops.open_stops", lambda *a, **k: [])
    monkeypatch.setattr("service.trading.stops.stopped_symbols", lambda *a, **k: set())

    gate = observation.stop_gate(desk, DAY1, {SYMBOL: quote(book)})
    assert gate["breached"] == []
    assert gate["unknown"] == []
    assert gate["no_stop"] == [SYMBOL]


def test_proposal_claim_is_unique_across_connections_and_released_at_terminal(tmp_path):
    db = tmp_path / "paper.sqlite"
    fingerprint = "same-normalized-intent"
    # Both stores are built *before* the barrier. Constructing `PaperStore` inside
    # the raced section made this test hang the whole suite: the constructor runs
    # `executescript(DDL)` and an FTS rebuild, both of which want SQLite's write
    # lock, so one side could fail there and never reach `barrier.wait()` — while
    # the other waited for a second party that was already dead. The contention
    # under test is `save_proposal`, not `__init__`, so it starts here.
    stores = [PaperStore(db), PaperStore(db)]
    barrier = threading.Barrier(2)

    def claim(work):
        store, ticket_id = work
        barrier.wait(timeout=30)   # a dead worker raises, instead of hanging
        return store.save_proposal(ticket(ticket_id), fingerprint)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, zip(stores, ["td-first", "td-second"])))
    assert sum(created for _, created in results) == 1
    winner = next(row for row, created in results if created)
    store = PaperStore(db)
    assert store.proposal_for_fingerprint(fingerprint)["ticket_id"] == winner["ticket_id"]

    winner["status"] = "rejected"
    store.save_ticket(winner)
    assert store.proposal_for_fingerprint(fingerprint) is None
    replacement, created = store.save_proposal(ticket("td-replacement"), fingerprint)
    assert created and replacement["ticket_id"] == "td-replacement"


def test_a_request_id_still_answers_after_the_ticket_is_done(tmp_path):
    """句柄是关于**请求**的承诺，不是待处理意图的去重键。

    指纹在票据进入终态时被释放（重建同一意图是合法的）；调用方的 id 不能跟着释放，
    否则"成交后客户端才超时、再重试"会生成第二张票——正是这个句柄要防的那一次。
    """
    store = PaperStore(tmp_path / "paper.sqlite")
    request_id = "propose-7f3a9c11"
    first, created = store.save_proposal(ticket("td-first"), "fp-1",
                                         client_request_id=request_id)
    assert created

    # Same id, different fingerprint: the model reworded its thesis on retry.
    again, created = store.save_proposal(ticket("td-second"), "fp-2",
                                         client_request_id=request_id)
    assert not created and again["ticket_id"] == "td-first"

    first["status"] = "filled"
    store.save_ticket(first)
    assert store.proposal_for_fingerprint("fp-1") is None, "指纹认领应随终态释放"

    later, created = store.save_proposal(ticket("td-third"), "fp-3",
                                         client_request_id=request_id)
    assert not created and later["ticket_id"] == "td-first"
    assert store.proposal_for_request_id(request_id)["ticket_id"] == "td-first"


def test_a_second_process_does_not_leave_the_book_permanently_stuck(tmp_path):
    """Two desks over one sqlite is the normal case, not the exotic one.

    09:26 `prepare_session`, the five-minute `monitor`, the 17:30 `daily_job` and
    any manual backfill each build their own `Desk` over `data/paper.sqlite`. The
    service keeps one `PaperBook` alive for days, so its `_revision` is only ever
    advanced by its own writes: the first commit from any other process made every
    later write from the service return 409 — and the advice in that 409 ("re-read
    /account") could not help, because `/account` reads the in-memory copy.
    """
    db = tmp_path / "paper.sqlite"
    market = StubMarket()
    long_running = make_book(db, market, "next_open")
    other_process = make_book(db, market, "next_open")

    # The short-lived process moves the durable state forward (a new session day,
    # a kill switch, a corporate action — any of them calls persist()).
    other_process.set_kill_switch(True)

    with pytest.raises(StateConflict):
        long_running.set_kill_switch(False)

    # The conflict must leave memory equal to disk, not a hybrid. Before the fix
    # the in-memory copy kept `kill_switch=False` while sqlite held True.
    assert long_running.state["kill_switch"] is True
    assert long_running._revision == PaperStore(db).load_state_with_revision()[1]

    # And the next attempt must succeed — the desk is not bricked until restart.
    long_running.set_kill_switch(False)
    assert PaperStore(db).load_state()["kill_switch"] is False


def test_reading_equity_does_not_move_the_drawdown_peak(tmp_path):
    """``equity()`` 是读，不该改写回撤基准。

    它曾经顺手 ``peak_equity = max(...)``，而且只改内存：抬升要等下一次
    ``persist()`` 才落盘，进程在中间重启峰值就退回去了——回撤被低估、风控被
    放宽。现在只有 ``mark_prices``（显式看价）能抬峰值。
    """
    market = StubMarket()
    book = make_book(tmp_path / "paper.sqlite", market, "next_open", shares=100)
    acct = book.ledger.account
    acct.peak_equity = D("5500")
    acct.position(SYMBOL).last_price = D("20.00")     # 5000 现金 + 2000 市值

    assert book.equity() == D("7000")
    assert acct.peak_equity == D("5500"), "读一次账户就把峰值改了"

    book.mark_prices(DAY1)                            # 显式标记才抬
    assert acct.peak_equity > D("5500")


def test_legacy_snapshot_recovers_settled_session_from_book_day(tmp_path):
    """The first restart after the upgrade must not repeat the same-day unlock."""
    db = tmp_path / "paper.sqlite"
    market = StubMarket()
    book = make_book(db, market, "next_open", shares=200)
    pos = book.ledger.account.peek(SYMBOL)
    pos.available_qty = 100
    pos.locked_qty = 100
    book.state["day"] = DAY1
    book.ledger.oms._settled_session = None       # snapshot written by the old reader
    book.persist()

    restored = make_book(db, market, "next_open")
    restored.ledger.settle(DAY1)
    restored_pos = restored.ledger.account.peek(SYMBOL)
    assert (restored_pos.available_qty, restored_pos.locked_qty) == (100, 100)

    restored.ledger.settle(DAY2)
    assert (restored_pos.available_qty, restored_pos.locked_qty) == (200, 0)
