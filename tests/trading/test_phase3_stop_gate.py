"""F2 regressions: one stop-gate predicate, one refusal shape, one BUY-only veto.

These four cases pin the four places the single ``observation.evaluate_stop_gate``
predicate is now consulted, and the one place it must never be. Every case is
written so that reverting to ``000aba3`` turns it red:

* F2-1  the idempotent-retry short circuit used to ``return previous``
        unconditionally, so a BUY proposed before a breach could be replayed
        verbatim after the breach.
* F2-2  guard against over-fixing: terminal tickets are answers, not live
        intents. Includes a ``proposed``+BUY control so the test is genuinely
        red on old code (where the replay never consulted the gate at all).
* F2-3  ``approve`` used to re-run only ``guard.preflight``, which had no stop
        item; a breach appearing between propose and approve went unnoticed.
* F2-4  the new ``Check("stop_gate", ...)`` must exist on BUY and must NOT exist
        on SELL. The BUY-half assertion is what makes this red on old code; the
        SELL-half protects "sells are always allowed".

The synthetic ``_Book``/``_Market`` in ``test_guard_semantics.py`` have no
``.store``/``.quotes``/``market.store``; the new guard Check is fail-closed, so
any BUY preflight built on them must have ``evaluate_stop_gate`` stubbed. We stub
the module attribute (guard imports it lazily inside ``preflight``).
"""

from __future__ import annotations

import pytest

from service.trading import observation
from service.trading.decisions import DecisionError
from tests.trading.helpers import pick_symbols, proposal


def _breached_gate(market, book, day):
    """A holding is through its stop; no unknown, no missing-stop names."""
    return {"breached": ["600000.SH"], "unknown": [], "no_stop": []}


# ---------------------------------------------------------------------------
# F2-1 · the retry handle cannot be used to bypass the open-violation gate
# ---------------------------------------------------------------------------
def test_idempotent_retry_cannot_bypass_open_violation(desk, monkeypatch):
    symbol = pick_symbols(desk, 1)[0]
    body = proposal(symbol, client_request_id="phase3-retry-X")
    first = desk.decisions.propose(body)
    assert first["status"] == "proposed", first.get("reasons")
    assert str(first["side"]).upper() == "BUY"

    # The breach happens *after* the first proposal committed. Old code's
    # short-circuit (decisions.py:253-256) was `if previous is not None:
    # return previous` — it skipped the stop gate entirely and handed back the
    # stale BUY ticket. Patch the shared predicate to simulate that later frame.
    monkeypatch.setattr(observation, "evaluate_stop_gate", _breached_gate,
                        raising=False)

    with pytest.raises(DecisionError) as err:
        desk.decisions.propose(dict(
            body, thesis="同一句柄重试：措辞换了，但此刻风险预算已经违约，不该拿回旧票。"))
    assert err.value.code == "OPEN_VIOLATION"
    assert err.value.status == 409

    # No silent pass-through and no second ticket: the original is still open.
    assert desk.paper.get_ticket(first["ticket_id"])["status"] == "proposed"
    pending = [t for t in desk.paper.list_tickets(limit=50)
               if t["status"] == "proposed"]
    assert len(pending) == 1, "重试不能变成第二张待批票据"


# ---------------------------------------------------------------------------
# F2-2 · terminal tickets are returned verbatim; only proposed+BUY is re-asked
# ---------------------------------------------------------------------------
def test_terminal_ticket_retry_is_verbatim(desk, monkeypatch):
    symbol = pick_symbols(desk, 1)[0]
    base = desk.decisions.propose(
        proposal(symbol, client_request_id="phase3-probe-base"))
    assert base["status"] == "proposed"

    # Materialise one ticket per terminal status through the real store, each
    # with its own caller handle. Copies keep every field the store needs
    # (created_at / expires_at / symbol / side / sleeve).
    terminal: dict[str, tuple[str, dict]] = {}
    for status in ("rejected", "filled", "queued"):
        rid = f"phase3-probe-{status}"
        row = dict(base, ticket_id=f"{base['ticket_id']}-{status}",
                   status=status, client_request_id=rid)
        desk.paper.save_ticket(row)
        desk.paper.claim_request(rid, row["ticket_id"])
        terminal[status] = (rid, row)

    calls: list[str] = []

    def tripping_gate(market, book, day):
        calls.append(str(day))
        # If a terminal ticket were wrongly re-checked, this breached gate
        # would raise a 409 and the "verbatim" assertions below would fail.
        return _breached_gate(market, book, day)

    monkeypatch.setattr(observation, "evaluate_stop_gate", tripping_gate,
                        raising=False)

    for status, (rid, row) in terminal.items():
        again = desk.decisions.propose(proposal(symbol, client_request_id=rid))
        assert again["ticket_id"] == row["ticket_id"], f"{status} 重试不是原样返回"
        assert again["status"] == status
    assert calls == [], "终态票是答案，不是活的买入意图，不该再问一次止损门禁"

    # Control: a still-proposed BUY *must* consult the gate. Without this half,
    # "calls == []" would be vacuous. On old code this control returns the old
    # ticket instead of raising, so the case is red there too.
    with pytest.raises(DecisionError) as err:
        desk.decisions.propose(dict(
            proposal(symbol, client_request_id="phase3-probe-base"),
            thesis="控制组：仍是 proposed 的 BUY，破线后重试必须被止损门禁拦下。"))
    assert err.value.code == "OPEN_VIOLATION"
    assert calls, "控制组必须真的调用了门禁（旧代码不会 → 本用例在旧代码下会红）"


# ---------------------------------------------------------------------------
# F2-3 · approve re-checks the gate and rejects with a named stop_gate Check
# ---------------------------------------------------------------------------
def test_approve_rechecks_stop_gate(desk, monkeypatch):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))
    assert ticket["status"] == "proposed", ticket.get("reasons")

    # Propose happened with no breach. Between propose and approve the holding
    # breaks its stop. Old approve (decisions.py:797-811) only ran
    # guard.preflight, which had no stop item, so the ticket filled.
    monkeypatch.setattr(observation, "evaluate_stop_gate", _breached_gate,
                        raising=False)

    result = desk.decisions.approve(ticket["ticket_id"])
    assert result["status"] == "rejected", result

    checks = result["ticket"]["checks"]["checks"]
    gate = [row for row in checks if row["name"] == "stop_gate"]
    assert gate, "approve 的 guard 结果里必须有 stop_gate 这一项（旧代码没有 → 会红）"
    assert gate[0]["ok"] is False, "破线状态下这张 BUY 必须被拦"
    assert any("stop_gate" in reason for reason in result["reasons"])


# ---------------------------------------------------------------------------
# F2-4 · the veto is BUY-only; SELL is never blocked by a breached stop
# ---------------------------------------------------------------------------
def test_sell_branch_has_no_stop_gate_veto(monkeypatch):
    from tests.trading.test_guard_semantics import _preflight

    seen: list[str] = []

    def tripping_gate(market, book, day):
        seen.append(str(day))
        return _breached_gate(market, book, day)

    monkeypatch.setattr(observation, "evaluate_stop_gate", tripping_gate,
                        raising=False)

    buy = _preflight(monkeypatch, side="BUY")
    sell = _preflight(monkeypatch, side="SELL")

    buy_gate = [row for row in buy.checks if row.name == "stop_gate"]
    assert buy_gate, "新代码 BUY 分支必须新增 stop_gate 检查项（旧代码没有 → 会红）"
    assert buy_gate[0].ok is False, "破线状态下 BUY 被拦"

    sell_names = [row.name for row in sell.checks]
    assert "stop_gate" not in sell_names, "SELL 分支不得出现 stop_gate 检查项"
    assert sell.ok, "卖出永远放行，不能被破线门禁否决"
    assert seen == ["20260107"], "门禁只应由 BUY 分支调用一次，SELL 一次都不问"
