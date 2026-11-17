"""Batch 7: the order-level cancel path — withdraw a queued order, not a proposal.

``reject`` closes a *proposal* (the thesis never traded). ``cancel`` withdraws an
*order* that was already queued: the pending row is dropped and the ticket ends
``cancelled``. Before this batch there was no way to do that at all — a queued
order could only wait for the open or expire, so "I changed my mind" had no
expression and the only exit was ``reject``, which writes ``rejected`` (a lie
about what happened).

Three rules the cases below pin, because each is easy to get wrong:

* **idempotent** — a second cancel reports ``cancelled: False``, not an error and
  not a second commit;
* **filled wins** — an already-filled ticket answers ``status="filled"`` with
  ``cancelled: False`` (200, never 409): cancel is a *request*, so it reports the
  fact instead of failing;
* **``can_cancel`` only binds ``ceiling``** — a ceiling order rests at the
  exchange today, so 14:57–15:00 is closed to cancellation; an ``open`` /
  ``next_open`` intent is exempt (withdrawing tomorrow's order after hours is
  normal). ``next_open`` entries have *no* ``queue_kind`` key at all, which is why
  the implementation must use ``entry.get("queue_kind")``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app
from service.kernel.clock import SHANGHAI
from service.kernel.models import D
from tests.trading.test_book_execution_safety import (
    DAY1,
    StubMarket,
    make_bar,
    make_book,
    ticket,
)
from tests.trading.test_ceiling_queue import _book, _market

TOKEN = {"X-Quant-Token": "change-me-local-only"}


@pytest.fixture
def client(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    with TestClient(app) as c:
        yield c


def _queued_ceiling(tmp_path, *, hour: int = 10, minute: int = 30):
    """A ``ceiling`` order sitting in the queue, with the clock pinned."""
    market = _market(open_times=1, seal=1e6, amount=2e7, low="10.60")
    book = _book(tmp_path / "p.sqlite", market)
    book.clock = lambda: datetime(2026, 1, 5, hour, minute, tzinfo=SHANGHAI)
    t = ticket(limit="11.00", qty=400)
    assert book.execute_ticket(t).status == "queued"
    assert book._pending_ticket(t["ticket_id"]) is not None
    return book, t["ticket_id"]


def _queued_next_open(tmp_path, *, hour: int = 20, minute: int = 0):
    market = StubMarket({DAY1: make_bar(DAY1)})
    book = make_book(tmp_path / "p.sqlite", market, "next_open")
    book.clock = lambda: datetime(2026, 1, 6, hour, minute, tzinfo=SHANGHAI)
    t = ticket(limit="10.20")
    assert book.execute_ticket(t).status == "queued"
    return book, t["ticket_id"]


# ---------------------------------------------------------------------------
# S1 / S1b · a queued order is withdrawn, and cancelling twice is harmless
# ---------------------------------------------------------------------------
def test_cancel_withdraws_a_queued_ceiling_order(tmp_path):
    book, tid = _queued_ceiling(tmp_path)

    result = book.cancel_order(tid, actor="probe", reason="thesis died")

    assert result["status"] == "cancelled"
    assert result["cancelled"] is True
    assert result["released_qty"] == 400

    stored = book.store.get_ticket(tid)
    assert stored["status"] == "cancelled"
    assert stored["reasons"] == ["thesis died"]
    assert stored.get("cancelled_at"), "撤销必须留时间戳"
    assert book._pending_ticket(tid) is None
    assert book.summary()["pending"] == []

    # The audit row is how a later reader learns who withdrew it and why.
    row = book.store.list_decisions(1)[0]
    assert row["kind"] == "cancel"
    assert row["ticket_id"] == tid
    assert row["actor"] == "probe"
    assert row["payload"]["reason"] == "thesis died"


def test_cancel_is_idempotent_and_commits_once(tmp_path):
    book, tid = _queued_ceiling(tmp_path)
    assert book.cancel_order(tid, actor="probe", reason="x")["cancelled"] is True

    again = book.cancel_order(tid, actor="probe", reason="x")

    assert again["status"] == "cancelled"
    assert again["cancelled"] is False, "第二次撤单不该再提交一次"
    assert again["released_qty"] == 0
    # One commit, not two: the release/审计 must not repeat.
    cancels = [r for r in book.store.list_decisions(10) if r["kind"] == "cancel"]
    assert len(cancels) == 1


# ---------------------------------------------------------------------------
# The core invariant · a queued ticket never froze cash, so cancel releases none
# ---------------------------------------------------------------------------
def test_cancel_does_not_touch_cash_or_frozen(tmp_path):
    """The promise is released, the cash is not "unfrozen" — because it never was.

    A queued ticket has no ledger order, so ``queued_buy_commitment`` is computed
    from ``pending`` rather than from a reservation. Cancelling therefore removes
    the *promise* (commitment → 0) while leaving ``(cash_available, cash_frozen,
    cash_withdrawable)`` byte-for-byte identical. This is the difference the
    implementation warns about: hand-deducting ``cash_frozen`` inside
    ``cancel_order`` would double-count, and this is the only case that sees it.
    """
    book, tid = _queued_ceiling(tmp_path)
    entry = book._pending_ticket(tid)

    account = book.ledger.account
    before = (account.cash_available, account.cash_frozen, account.cash_withdrawable)
    committed_before = book.queued_buy_commitment()
    # Without a live commitment the invariant cannot fail — the case would be
    # vacuous, so pin the precondition rather than trusting the fixture.
    assert committed_before > 0, "排队买单必须体现为承诺，否则这条不变量测不到东西"

    result = book.cancel_order(tid, actor="probe", reason="changed my mind")

    after = (account.cash_available, account.cash_frozen, account.cash_withdrawable)
    assert after == before, f"撤单不得动现金/冻结：{before} → {after}"
    assert book.queued_buy_commitment() == D("0"), "承诺必须随 pending 行一起消失"
    # `released_qty` is the whole entry qty: nothing had filled.
    assert result["released_qty"] == entry["qty"]


# ---------------------------------------------------------------------------
# S2 · an already-filled ticket reports the fact instead of raising
# ---------------------------------------------------------------------------
def test_cancel_reports_a_filled_ticket_instead_of_409(tmp_path):
    market = StubMarket({DAY1: make_bar(DAY1, open_="10.10", high="10.30",
                                        low="10.05", close="10.20")})
    book = make_book(tmp_path / "p.sqlite", market, "close")
    t = ticket(limit="10.20")
    assert book.execute_ticket(t).status == "filled"

    result = book.cancel_order(t["ticket_id"], actor="probe", reason="too late")

    assert result["status"] == "filled"
    assert result["cancelled"] is False
    assert result["released_qty"] == 0
    # The fill is untouched: no rollback, no rewrite to "cancelled".
    stored = book.store.get_ticket(t["ticket_id"])
    assert stored["status"] == "filled"
    assert stored.get("fill"), "已成交票的成交记录不能被撤单抹掉"


# ---------------------------------------------------------------------------
# S3 · a ceiling order cannot be withdrawn in the closing auction
# ---------------------------------------------------------------------------
def test_a_ceiling_order_cannot_be_cancelled_in_the_closing_auction(tmp_path):
    book, tid = _queued_ceiling(tmp_path, hour=14, minute=58)

    result = book.cancel_order(tid, actor="probe", reason="too late")

    assert result["status"] == "blocked"
    assert result["cancelled"] is False
    assert result["reasons"] == ["CANNOT_CANCEL_IN_AUCTION"]
    # Refused means refused: the order is still there, not silently gone.
    assert book._pending_ticket(tid) is not None
    assert book.store.get_ticket(tid)["status"] == "queued"


# ---------------------------------------------------------------------------
# S4 · a next_open intent is exempt, even after hours
# ---------------------------------------------------------------------------
def test_a_next_open_order_can_be_cancelled_after_hours(tmp_path):
    book, tid = _queued_next_open(tmp_path, hour=20, minute=0)
    # `next_open` entries carry no `queue_kind` key at all — hence `.get` in the
    # implementation. Asserting it here keeps a future `entry["queue_kind"]`
    # refactor honest.
    assert book._pending_ticket(tid).get("queue_kind") is None

    result = book.cancel_order(tid, actor="probe", reason="no longer wanted")

    assert result["status"] == "cancelled"
    assert result["cancelled"] is True, "盘后撤次日单是正常操作，不该被 can_cancel 拦"
    assert book._pending_ticket(tid) is None


# ---------------------------------------------------------------------------
# S4b · an ``open`` queue is exempt too — the window binds ``ceiling`` only
# ---------------------------------------------------------------------------
def test_an_open_queue_order_is_exempt_from_the_cancel_window(tmp_path):
    """At 09:26 ``can_cancel`` is False, yet an ``open`` order can still be pulled.

    ``open`` joins *today's* opening auction and is not resting at the exchange
    under a cancellation ban the way a ``ceiling`` order is. The exemption is
    therefore by ``queue_kind``, not by "this window is closed for everyone" — if
    someone tightens `queue_kind == "ceiling"` into a blanket
    `if not can_cancel(...)`, this is the case that fails.
    """
    book = make_book(tmp_path / "p.sqlite", StubMarket({DAY1: make_bar(DAY1)}),
                     "next_open")
    book.clock = lambda: datetime(2026, 1, 5, 9, 26, tzinfo=SHANGHAI)
    t = ticket(limit="10.20")
    assert book.execute_ticket(t).status == "queued"
    assert book._pending_ticket(t["ticket_id"]).get("queue_kind") == "open"

    result = book.cancel_order(t["ticket_id"], actor="probe", reason="changed mind")

    assert result["status"] == "cancelled"
    assert result["cancelled"] is True, "09:26 只对 ceiling 关闭撤单，open 队列应可撤"


# ---------------------------------------------------------------------------
# S5 · an unknown ticket is a report, not an exception
# ---------------------------------------------------------------------------
def test_cancel_of_an_unknown_ticket_is_not_found(tmp_path):
    market = _market(open_times=1, seal=1e6, amount=2e7, low="10.60")
    book = _book(tmp_path / "p.sqlite", market)

    result = book.cancel_order("td-does-not-exist", actor="probe")

    assert result["status"] == "not_found"
    assert result["cancelled"] is False


# ---------------------------------------------------------------------------
# HTTP contract · 200 with the fact, 404 for a ticket that never existed
# ---------------------------------------------------------------------------
def test_the_cancel_route_withdraws_a_queued_order(client, desk):
    # `limit_queue` is the only mode in which a ceiling order queues without a
    # matching bar; the route itself is mode-agnostic.
    desk.book.fill_mode = "limit_queue"
    day = desk.market.latest_bar_date()
    desk.book.clock = lambda: datetime.strptime(day, "%Y%m%d").replace(
        hour=10, minute=30, tzinfo=SHANGHAI)
    t = ticket(limit="11.00", qty=400)
    assert desk.book.execute_ticket(t).status == "queued"

    response = client.post(
        f"/decision/{t['ticket_id']}/cancel",
        json={"reason": "changed my mind"}, headers=TOKEN,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["cancelled"] is True
    assert desk.book.store.get_ticket(t["ticket_id"])["status"] == "cancelled"


def test_the_cancel_route_404s_for_an_unknown_ticket(client):
    response = client.post("/decision/td-does-not-exist/cancel", json={}, headers=TOKEN)

    assert response.status_code == 404, "不存在的票不能返回 200（会被读成撤单成功）"
    # A 404 alone does not prove the route exists — an *unrouted* path also 404s,
    # with FastAPI's `{"detail": "Not Found"}`. The cancel handler raises
    # `DecisionError("NOT_FOUND")`, whose detail is `{"code", "message"}`. Pin the
    # shape so this case cannot pass on a tree that has no cancel route at all.
    detail = response.json()["detail"]
    assert isinstance(detail, dict), f"路由不存在时 detail 是字符串：{detail!r}"
    assert detail.get("code") == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Tool contract · the new tool is registered AND named in SKILL.md
#
# `skill_contract.test.mjs` (Node) enforces the same invariant; this guard makes
# it fail in the Python suite too, so "registered but undocumented" cannot ship
# on a machine where only pytest ran. The *count* (67) is owned by
# `tests/test_docs_numbers.py` — not repeated here.
# ---------------------------------------------------------------------------
def test_quant_cancel_is_registered_and_documented():
    root = Path(__file__).resolve().parents[2]
    js = (root / "plugin/lib/index.js").read_text(encoding="utf-8")
    skill = (root / "plugin/skills/quant-desk/SKILL.md").read_text(encoding="utf-8")

    facade = (root / "plugin/lib/facade.js").read_text(encoding="utf-8")

    assert "'quant_cancel'" in js or '"quant_cancel"' in js, "内部 handler 未注册"
    assert "'paper.cancel': ['quant_cancel'" in facade, "paper.cancel 未进入公开 catalog"
    assert 'quant_order(book="paper", action="cancel"' in skill, "撤单入口未在 SKILL.md 点名"
    # Withdrawing an order must not be described as selling it.
    assert "不是卖出" in skill or "撤单不是卖出" in skill
