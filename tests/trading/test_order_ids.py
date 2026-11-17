"""Order identity across restarts.

``orders`` is keyed by ``order_id``, so an id that restarts at ``O00000001``
after a process restart overwrites the audit row of the first order the desk
ever sent. The counter therefore lives in the account payload *and* is recovered
from the table itself.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.models import Bar, D, Instrument
from service.trading.book import PaperBook
from service.trading.market import Snapshot
from service.trading.state import PaperStore

DAY1, DAY2 = "20260105", "20260106"
SYMBOL = "600000.SH"


def make_bar(day: str, close: str = "10.00") -> Bar:
    ts = datetime.strptime(day, "%Y%m%d").replace(hour=15, tzinfo=SHANGHAI)
    return Bar(
        symbol=SYMBOL, ts=ts, open=D(close), high=D(close), low=D(close), close=D(close),
        pre_close=D("10.00"), volume=5_000_000, amount=D(close) * 5_000_000,
        up_limit=D("11.00"), down_limit=D("9.00"),
    )


class _Cal:
    def calendar(self):
        return TradingCalendar.always()

    def is_trading_day(self, day: str) -> bool:
        return True


class StubMarket:
    def __init__(self, bars: dict[str, Bar], last: str):
        self.bars = bars
        self.calendar = _Cal()
        self.last = last

    def bar(self, symbol: str, date: str):
        bar = self.bars.get(date)
        if bar is None or symbol != SYMBOL:
            return None
        return bar, Instrument(symbol=symbol, name="stub", min_qty=100, qty_step=100)

    def snapshot(self, symbol: str, date: str | None = None):
        bar = self.bars.get(date or self.last)
        if bar is None or symbol != SYMBOL:
            return None
        return Snapshot(symbol=symbol, trade_date=date or self.last, last=bar.close,
                        pre_close=bar.pre_close, open=bar.open, high=bar.high, low=bar.low,
                        up_limit=bar.up_limit, down_limit=bar.down_limit,
                        volume=float(bar.volume), amount=float(bar.amount),
                        suspended=False, source="stub")

    def last_trading_day(self, as_of: str | None = None) -> str:
        return self.last


def ticket(ticket_id: str) -> dict:
    return {
        "ticket_id": ticket_id,
        "symbol": SYMBOL,
        "side": "BUY",
        "qty": 100,
        "limit_price": "10.00",
        "trade_date": DAY1,
        "sleeve": "CORE",
        "intent_type": "test",
        "thesis": "重启后的订单号必须继续递增，否则审计表里两笔订单会共用一个 id。",
        "invalidation": "若第二笔订单号等于第一笔，则本测试失败",
        "stop_price": "9.50",
    }


@pytest.fixture
def bars() -> dict[str, Bar]:
    return {DAY1: make_bar(DAY1), DAY2: make_bar(DAY2)}


def a_book(db, bars) -> PaperBook:
    book = PaperBook(PaperStore(db), StubMarket(bars, DAY2), initial_cash="5000")
    book.rollover(DAY1)
    return book


def test_order_ids_do_not_restart_after_a_process_restart(tmp_path, monkeypatch, bars):
    monkeypatch.setenv("FILL_MODE", "close")
    db = tmp_path / "paper.sqlite"

    first = a_book(db, bars).execute_ticket(ticket("td-a"))
    assert first.ok and first.status == "filled"

    # A new process: new store, new ledger, same file.
    second = a_book(db, bars).execute_ticket(ticket("td-b"))
    assert second.ok and second.status == "filled"

    assert second.order_id != first.order_id
    assert (first.order_id, second.order_id) == ("O00000001", "O00000002")


def test_the_audit_table_keeps_both_orders(tmp_path, monkeypatch, bars):
    monkeypatch.setenv("FILL_MODE", "close")
    db = tmp_path / "paper.sqlite"

    a_book(db, bars).execute_ticket(ticket("td-a"))
    a_book(db, bars).execute_ticket(ticket("td-b"))

    orders = PaperStore(db).list_orders(limit=50)
    assert len(orders) == 2
    assert {o["order_id"] for o in orders} == {"O00000001", "O00000002"}


def test_a_crashed_counter_is_recovered_from_the_table(tmp_path, monkeypatch, bars):
    """The payload can lag the table; the id must still move forward."""
    monkeypatch.setenv("FILL_MODE", "close")
    db = tmp_path / "paper.sqlite"

    book = a_book(db, bars)
    book.execute_ticket(ticket("td-a"))
    payload = book.state
    payload["account"].pop("order_seq", None)   # simulate a save that lost the counter
    book.store.save_state(payload)

    assert PaperStore(db).max_order_seq() == 1
    assert a_book(db, bars).ledger.oms.seq == 1


def test_a_second_ticket_cannot_overwrite_the_first_orders_row(tmp_path):
    """Two tickets can share an order id; only one of them may own the row."""
    store = PaperStore(tmp_path / "paper.sqlite")
    first = {"order_id": "O00000001", "ts": "2026-10-23T14:55:00+08:00",
             "symbol": SYMBOL, "status": "FILLED", "side": "BUY", "qty": 100,
             "price": "9.24", "ticket_id": "td-20261023-aaaa1111"}
    second = dict(first, ts="2026-10-28T13:01:10+08:00", side="SELL", price="9.42",
                  ticket_id="td-20261028-bbbb2222")

    assert store.save_order(first) == "O00000001"
    stored_as = store.save_order(second)
    assert stored_as == "O00000001-bbbb2222", "the second order must not take the id"

    rows = store.list_orders(limit=10)
    assert len(rows) == 2, "both audit rows survive"
    assert {r["ticket_id"] for r in rows} == {first["ticket_id"], second["ticket_id"]}
    by_ticket = {r["ticket_id"]: r for r in rows}
    assert by_ticket[first["ticket_id"]]["price"] == "9.24"
    assert by_ticket[second["ticket_id"]]["collision_with"]["ticket_id"] == first["ticket_id"]


def test_the_same_ticket_still_updates_its_own_row(tmp_path):
    store = PaperStore(tmp_path / "paper.sqlite")
    order = {"order_id": "O00000002", "ts": "2026-10-28T09:30:00+08:00",
             "symbol": SYMBOL, "status": "NEW", "side": "BUY", "qty": 100,
             "price": "10.00", "ticket_id": "td-20261028-cccc3333"}
    store.save_order(order)
    store.save_order(dict(order, status="FILLED"))

    rows = store.list_orders(limit=10)
    assert len(rows) == 1
    assert rows[0]["status"] == "FILLED"
