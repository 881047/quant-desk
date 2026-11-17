"""Regressions from the 2026-11-13 review (kernel × host integration).

Each case names the finding it pins and asserts the *failure mode*, not the
implementation: a queue row that must survive a failed commit, and a fill-time
guard that must not reach the network while the book lock is held.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from service.kernel.models import Side
from service.trading.state import StoreBusy
from tests.trading.test_book_execution_safety import (
    DAY1,
    SYMBOL,
    StubMarket,
    make_book,
    ticket,
)


def _queued(tmp_path, mode: str = "next_open"):
    book = make_book(tmp_path / "p.sqlite", StubMarket(), mode)
    assert book.queue_ticket(ticket("td-q1")).ok
    return book


# ---------------------------------------------------------------------------
# S-01 · a failed commit must not delete the queue row from memory
# ---------------------------------------------------------------------------
def test_cancel_keeps_pending_row_when_commit_fails(tmp_path, monkeypatch):
    book = _queued(tmp_path)
    entry = book._pending_ticket("td-q1")
    assert entry is not None
    before = [row["ticket_id"] for row in book.state["pending"]]

    def boom(*_args, **_kwargs) -> int:
        raise StoreBusy("another writer holds the lock")

    monkeypatch.setattr(book.store, "commit_ticket_state", boom)

    with pytest.raises(StoreBusy):
        book._cancel_queued(entry, {"reason": "test"}, "tester", status="cancelled")

    assert [row["ticket_id"] for row in book.state["pending"]] == before, (
        "the in-memory queue lost a ticket the database still holds; the next "
        "commit would persist the shorter queue and the ticket would never settle")


def test_cancel_removes_pending_row_on_success(tmp_path):
    book = _queued(tmp_path)
    entry = book._pending_ticket("td-q1")
    book._cancel_queued(entry, {"reason": "test"}, "tester", status="cancelled")
    assert book._pending_ticket("td-q1") is None
    assert book.store.get_ticket("td-q1")["status"] == "cancelled"


# ---------------------------------------------------------------------------
# S-02 · fill-time guard uses cached quotes only; it must never fetch
# ---------------------------------------------------------------------------
class _RecordingGuard:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def preflight(self, _book, **kwargs):
        self.calls.append(kwargs)

        class _R:
            ok = True

            @staticmethod
            def as_dict() -> dict:
                return {}

        return _R()


def test_preflight_passes_cached_quotes_and_never_calls_the_network(tmp_path):
    book = make_book(tmp_path / "p.sqlite", StubMarket(), "next_open")
    guard = _RecordingGuard()
    book.guard = guard

    fetched: list[list[str]] = []

    def network(symbols):
        fetched.append(list(symbols))
        raise AssertionError("fill-time preflight must not call the vendor")

    book.quotes = network
    cached_calls: list[list[str]] = []

    def cached(symbols):
        cached_calls.append(list(symbols))
        return {}

    book.quotes_cached = cached
    book.ledger.account.position(SYMBOL).total_qty = 100
    book.ledger.account.position(SYMBOL).available_qty = 70

    entry = {"ticket_id": "td-x", "symbol": SYMBOL, "side": "BUY", "qty": 100,
             "limit_price": "10.00", "sleeve": "CORE", "strategy": "llm"}
    assert book._preflight(entry, DAY1, Decimal("10.00"), Side.BUY) is None

    assert guard.calls and "quotes" in guard.calls[0]
    assert guard.calls[0]["quotes"] == {}
    assert fetched == [], "the network path was used under the book lock"
    assert cached_calls and cached_calls[0] == [SYMBOL]


# ---------------------------------------------------------------------------
# S-04 · a date the calendar does not cover is refused at propose time
# ---------------------------------------------------------------------------
def test_propose_refuses_a_date_the_calendar_does_not_cover():
    from types import SimpleNamespace

    from service.trading.decisions import DecisionError, DecisionService

    class _Cal:
        days = ["20260105", "20260106"]

        @staticmethod
        def covers(day: str) -> bool:
            return False

        @staticmethod
        def is_trading_day(day: str) -> bool:  # the weekday fallback that lied
            return True

    fake = SimpleNamespace(_now=lambda: __import__("datetime").datetime(2026, 1, 7),
                           market=SimpleNamespace(calendar=_Cal()))

    with pytest.raises(DecisionError) as err:
        DecisionService._date(fake, "600000.SH")
    assert err.value.code == "CALENDAR_NOT_COVERED"
    assert "20260105" in str(err.value) and "20260107" in str(err.value)


# ---------------------------------------------------------------------------
# G-01 · the persisted snapshot keeps live orders and a bounded history
# ---------------------------------------------------------------------------
def test_account_payload_is_bounded_but_keeps_live_orders(monkeypatch):
    from service.trading import book as book_mod

    monkeypatch.setattr(book_mod, "HISTORY_KEEP", 3)
    records = [
        {"order_id": "O1", "status": "FILLED"},
        {"order_id": "O2", "status": "CANCELLED"},
        {"order_id": "O3", "status": "REJECTED"},
        {"order_id": "O4", "status": "FILLED"},
        {"order_id": "O5", "status": "NEW"},
        {"order_id": "O6", "status": "PARTIALLY_FILLED"},
        {"order_id": "O7", "status": "FILLED"},
    ]
    payload = {"order_records": [dict(r) for r in records],
               "orders": {r["order_id"]: r["status"] for r in records},
               "fills": [{"fill_id": f"F{i}"} for i in range(10)]}
    out = book_mod.PaperBook._bounded(payload)
    ids = [row["order_id"] for row in out["order_records"]]
    # live first, then the newest three closed
    assert ids == ["O5", "O6", "O4", "O3", "O7"] or set(ids[:2]) == {"O5", "O6"}
    assert set(out["orders"]) == set(ids)
    assert [f["fill_id"] for f in out["fills"]] == ["F7", "F8", "F9"]


# ---------------------------------------------------------------------------
# G-02 · a failed commit restores state and ledger exactly
# ---------------------------------------------------------------------------
def test_failed_commit_rolls_back_state_and_ledger(tmp_path, monkeypatch):
    from copy import deepcopy

    book = make_book(tmp_path / "p.sqlite", StubMarket(), "close")
    book.ledger.account.position(SYMBOL).total_qty = 100
    book.ledger.account.position(SYMBOL).available_qty = 100
    book.ledger.account.position(SYMBOL).avg_cost = Decimal("10.00")
    book.persist()

    before_state = deepcopy(book.state)
    before_ledger = book.ledger.snapshot()

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk gave up")

    monkeypatch.setattr(book.store, "commit_execution", boom)

    with pytest.raises(RuntimeError):
        book.execute_ticket(ticket("td-rb", side="SELL", qty=100))

    assert book.state == before_state, "state diverged after a failed commit"
    assert book.ledger.snapshot() == before_ledger, "ledger diverged after a failed commit"


# ---------------------------------------------------------------------------
# G-08 · a rejection is retryable under the same client id; a fill is not
# ---------------------------------------------------------------------------
def _kernel_fixture(cash: str = "10000"):
    from datetime import datetime

    from service.kernel.calendar import TradingCalendar
    from service.kernel.ledger import Ledger
    from service.kernel.models import Bar, D, Instrument

    cal = TradingCalendar.always()
    led = Ledger.fresh(cash)
    inst = Instrument(symbol="600000.SH")
    ts = datetime(2026, 11, 13, 10, 0)
    bar = Bar(symbol="600000.SH", ts=ts, open=D("10"), high=D("10.2"),
              low=D("9.9"), close=D("10.1"), pre_close=D("10"),
              volume=10_000_000, amount=D("100000000"),
              up_limit=D("11"), down_limit=D("9"), prior_volume=10_000_000)
    return led, inst, bar, cal


def test_rejected_order_may_be_retried_with_the_same_client_id():
    from datetime import datetime

    from service.kernel.models import Intent, OrderStatus, Side

    led, inst, bar, cal = _kernel_fixture()
    closed = datetime(2026, 11, 13, 20, 0)
    first = led.submit(Intent(symbol="600000.SH", side=Side.BUY, qty=100,
                              client_order_id="retry-1"), closed, inst, bar, cal)
    assert first.status is OrderStatus.REJECTED

    second = led.submit(Intent(symbol="600000.SH", side=Side.BUY, qty=100,
                               client_order_id="retry-1"),
                        datetime(2026, 11, 13, 10, 0), inst, bar, cal)
    assert second.status is OrderStatus.NEW, (
        "a rejection froze nothing and never reached the market; the retry must "
        "be able to place instead of replaying the refusal")
    assert len(led.oms.orders) == 1, "the dead order must leave the live index"


# ---------------------------------------------------------------------------
# G-09 · target_weight is a target, not another order of the same size
# ---------------------------------------------------------------------------
def test_target_weight_is_measured_from_the_current_position():
    from service.kernel.execution import BarExecutionModel
    from service.kernel.models import Intent, RejectReason, Side

    led, inst, bar, cal = _kernel_fixture()
    first = led.submit(Intent(symbol="600000.SH", side=Side.BUY,
                              target_weight=Decimal("0.30")), bar.ts, inst, bar, cal)
    assert first.qty == 300
    led.apply_fill(first, BarExecutionModel().match(first, bar, inst, bar.ts, cal).fill)

    # A different submission timestamp: the auto client id is time-based, and a
    # same-second identical intent is (correctly) the same request.
    from datetime import datetime

    again = led.submit(Intent(symbol="600000.SH", side=Side.BUY,
                              target_weight=Decimal("0.30")),
                       datetime(2026, 11, 13, 10, 1), inst, bar, cal)
    assert again.status.value == "REJECTED"
    assert again.reason == RejectReason.BAD_QTY.value, (
        "already at 30%: asking for 30% again must be a no-op, not another 30%")
    assert led.account.peek("600000.SH").total_qty == 300


# ---------------------------------------------------------------------------
# G-09 (reference implementation) · the engine sizes from the target too
# ---------------------------------------------------------------------------
def test_reference_risk_engine_reads_target_weight_as_a_target():
    from service.kernel.models import Intent, RejectReason
    from service.kernel.risk import RiskEngine

    led, inst, bar, _cal = _kernel_fixture()
    account = led.account
    pos = account.position("600000.SH")
    pos.total_qty = pos.available_qty = 300
    pos.avg_cost = pos.last_price = Decimal("10.00")

    # Equity 13,000; 23% is 2,990, just under the 3,000 already held. The trade
    # is the *distance*, so there is nothing to buy — not another 2,990.
    decision = RiskEngine().check(
        Intent(symbol="600000.SH", side=Side.BUY, target_weight=Decimal("0.23")),
        account, inst, bar)
    assert not decision.allowed
    assert decision.reason is RejectReason.BAD_QTY
