"""Batch 8: two KNOWN_ISSUES closed — cancel's rollback, reject's terminal idempotency.

**H1 · `book.cancel_order`** drops the pending row from ``state`` *before* the
commit. ``StateConflict`` was already handled, but any other commit failure left
the in-memory queue missing a ticket the database still had. It now snapshots
``state`` and restores it, the same shape ``cancel_ticket`` has always used.

**H2 · ``decisions.reject``** only guarded ``filled``. Rejecting a ``cancelled`` /
``expired`` / ``rejected`` ticket walked into ``cancel_ticket``, which rewrote the
terminal status back to ``rejected`` **and** appended a second decisions row — so
the audit said "rejected twice" for a ticket that was withdrawn once. Terminal
statuses now answer idempotently.

The ``filled`` → 409 contract is **kept** (implementation-spec decision 1, option
B). ``plan.md §3 H2-1`` says "filled 幂等返回", which contradicts the spec; the
spec wins because option A would make ``plugin/lib/index.js:513`` and
``SKILL.md:723`` say something false. So the case below asserts 409.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

import pytest

from service.kernel.clock import SHANGHAI
from service.trading.decisions import DecisionError
from service.trading.state import StateConflict
from tests.trading.helpers import pick_symbols, proposal
from tests.trading.test_book_execution_safety import ticket
from tests.trading.test_ceiling_queue import _book, _market


def _queued_ceiling(tmp_path, *, hour: int = 10, minute: int = 30):
    market = _market(open_times=1, seal=1e6, amount=2e7, low="10.60")
    book = _book(tmp_path / "p.sqlite", market)
    book.clock = lambda: datetime(2026, 1, 5, hour, minute, tzinfo=SHANGHAI)
    t = ticket(limit="11.00", qty=400)
    assert book.execute_ticket(t).status == "queued"
    return book, t["ticket_id"]


def _pin_to_continuous(desk) -> None:
    """A deterministic CONTINUOUS clock, so the queue kind does not depend on wall time."""
    day = desk.market.latest_bar_date()
    desk.book.clock = lambda: datetime.strptime(day, "%Y%m%d").replace(
        hour=10, minute=30, tzinfo=SHANGHAI)


# ---------------------------------------------------------------------------
# H1 · a failed commit must not leave the in-memory queue short a row
# ---------------------------------------------------------------------------
def test_cancel_order_rolls_back_state_when_commit_fails(tmp_path, monkeypatch):
    book, tid = _queued_ceiling(tmp_path)
    before_state = deepcopy(book.state)
    before_pending = deepcopy(book.state["pending"])
    assert [row["ticket_id"] for row in before_pending] == [tid]

    def boom(*args, **kwargs):
        raise RuntimeError("commit blew up")

    monkeypatch.setattr(book.store, "commit_ticket_state", boom)

    with pytest.raises(RuntimeError, match="commit blew up"):
        book.cancel_order(tid, actor="probe", reason="thesis died")

    # `_cancel_queued` removed the row before the commit raised. Without the
    # snapshot the book would now be missing a ticket the database still has.
    assert book.state["pending"] == before_pending
    assert book.state == before_state, "整个 state 也必须回到调用前（不只是 pending）"


def test_cancel_order_reloads_on_state_conflict(desk, monkeypatch):
    """`except StateConflict` must come *before* `except Exception` — really.

    Both clauses would catch a conflict if the order were reversed, and the wrong
    one wins silently: the book would restore its stale snapshot instead of
    reloading, so the next call collides on the same revision forever. That makes
    the clause order **behavioural**, not stylistic — and no other case here sees
    it (`#1` feeds a plain `RuntimeError`, which only the rollback clause handles).

    No real concurrency needed: raise `StateConflict` from `_cancel_queued` and
    spy `reload`, which also plants a marker state so a rollback would erase it.
    """
    _pin_to_continuous(desk)
    desk.book.fill_mode = "limit_queue"
    t = ticket(limit="11.00", qty=400)
    assert desk.book.execute_ticket(t).status == "queued"

    reload_calls: list[int] = []

    def fake_reload():
        reload_calls.append(1)
        # Stand in for "the fresh snapshot replaced the in-memory state".
        desk.book.state = {"reloaded": True}

    def conflict(*args, **kwargs):
        raise StateConflict("revision moved under us")

    monkeypatch.setattr(desk.book, "reload", fake_reload)
    monkeypatch.setattr(desk.book, "_cancel_queued", conflict)

    with pytest.raises(StateConflict):
        desk.book.cancel_order(t["ticket_id"], actor="probe", reason="gone")

    assert reload_calls == [1], (
        "StateConflict 分支必须调 reload；被 except Exception 先捕获就不会调")
    assert desk.book.state == {"reloaded": True}, (
        "reload 的结果不能被 before 回滚冲掉——否则下一次调用还撞同一个 revision")


def test_cancel_still_normalises_its_state_on_the_success_path(tmp_path):
    """The rollback must not swallow the normal path — the row really does go."""
    book, tid = _queued_ceiling(tmp_path)

    result = book.cancel_order(tid, actor="probe", reason="thesis died")

    assert result["cancelled"] is True
    assert book._pending_ticket(tid) is None
    assert book.store.get_ticket(tid)["status"] == "cancelled"


# ---------------------------------------------------------------------------
# H2 · a terminal status is an answer, not a new decision
# ---------------------------------------------------------------------------
def test_reject_is_idempotent_for_a_cancelled_ticket(desk):
    _pin_to_continuous(desk)
    desk.book.fill_mode = "next_open"
    t = ticket(limit="10.20")
    assert desk.book.execute_ticket(t).status == "queued"
    tid = t["ticket_id"]
    assert desk.decisions.cancel(tid, actor="probe", reason="no longer wanted")[
        "cancelled"] is True
    rows_after_cancel = len(desk.paper.list_decisions(100000))

    result = desk.decisions.reject(tid, "too late", actor="probe")

    assert result["status"] == "cancelled"
    assert result["rejected"] is False
    assert desk.paper.get_ticket(tid)["status"] == "cancelled", "撤掉的票不能被 reprocess 成 rejected"
    assert len(desk.paper.list_decisions(100000)) == rows_after_cancel, (
        "幂等分支不得再写一行审计（旧代码这里 +1）")


def test_reject_is_idempotent_for_an_already_rejected_ticket(desk):
    symbol = pick_symbols(desk, 1)[0]
    tid = desk.decisions.propose(proposal(symbol), actor="probe")["ticket_id"]
    first = desk.decisions.reject(tid, "changed my mind", actor="probe")
    assert first["status"] == "rejected"
    rows_after_first = len(desk.paper.list_decisions(100000))

    second = desk.decisions.reject(tid, "changed my mind again", actor="probe")

    # Assert the harm first: the old code walked into `cancel_ticket`, which
    # appended a second decisions row and rewrote the reason. Leading with the
    # row count makes the red say *what went wrong*, not "KeyError: 'rejected'".
    assert len(desk.paper.list_decisions(100000)) == rows_after_first, (
        "重复 reject 不得再写一行审计")
    stored = desk.paper.get_ticket(tid)
    assert stored["status"] == "rejected"
    assert stored["reasons"] == ["changed my mind"], "第一次的原因不能被第二次覆盖"
    assert second["status"] == "rejected"
    assert second["rejected"] is False


def test_reject_still_409s_for_a_filled_ticket(desk):
    """Option B: `filled` stays a 409. Only *terminal-but-not-filled* is idempotent."""
    filled = dict(ticket(), ticket_id="td-filled", status="filled")
    desk.paper.save_ticket(filled)

    with pytest.raises(DecisionError) as excinfo:
        desk.decisions.reject("td-filled", "too late", actor="probe")

    assert excinfo.value.code == "ALREADY_FILLED"
    assert excinfo.value.status == 409
    assert desk.paper.get_ticket("td-filled")["status"] == "filled"


def test_rejecting_a_proposed_ticket_still_rejects_it(desk):
    """The idempotent branch must not swallow the live path.

    If someone widens the terminal set to every status, a proposal can never be
    rejected — the thesis stays open forever and no other case would notice.
    """
    symbol = pick_symbols(desk, 1)[0]
    tid = desk.decisions.propose(proposal(symbol), actor="probe")["ticket_id"]

    result = desk.decisions.reject(tid, "thesis died", actor="probe")

    assert result["status"] == "rejected"
    stored = desk.paper.get_ticket(tid)
    assert stored["status"] == "rejected"
    assert stored["reasons"] == ["thesis died"]
    assert stored.get("rejected_at")
