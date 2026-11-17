"""D-13: an armed intent can actually be withdrawn, through either entry point."""

from __future__ import annotations

import json

import pytest

from service import settings
from service.trading.decisions import DecisionError
from tests.trading.helpers import pick_symbols, proposal

_CONDITION = {"kind": "price", "op": "lte", "value": 10.0}


@pytest.fixture
def trigger_on(monkeypatch):
    """Only the intent switch is forced on; every other flag keeps its value."""
    original = settings.flag

    def flag(key: str) -> bool:
        if key == "trigger.enabled":
            return True
        return original(key)

    monkeypatch.setattr(settings, "flag", flag)


def _account(desk) -> tuple:
    book = desk.book
    return (float(book.equity()),
            json.dumps(book.summary()["positions"], ensure_ascii=False,
                       sort_keys=True, default=str),
            json.dumps(book.state.get("pending") or [], ensure_ascii=False,
                       sort_keys=True, default=str))


def _armed(desk) -> dict:
    symbol = pick_symbols(desk, 1)[0]
    req = {**proposal(symbol), "contingency": _CONDITION}
    ticket = desk.decisions.propose(req, actor="agent")
    first = desk.decisions.approve(ticket["ticket_id"], actor="agent")
    assert first["status"] == "armed"
    return ticket


def test_reject_armed_marks_cancelled_and_never_spends(desk, trigger_on):
    ticket = _armed(desk)
    before = _account(desk)
    out = desk.decisions.reject(ticket["ticket_id"], "changed my mind", actor="agent")
    assert out["status"] == "cancelled"
    assert out["rejected"] is False, "D-21: an approved intent was withdrawn, not refused"
    stored = desk.decisions.get(ticket["ticket_id"])
    assert stored["status"] == "cancelled"
    assert stored["cancelled_at"]
    assert stored["contingency"] == _CONDITION
    assert _account(desk) == before, "withdrawing an intent must not touch the ledger"


def test_cancel_armed_really_releases_it(desk, trigger_on):
    """Before D-13 this returned ``not_queued`` and left the ticket armed."""
    ticket = _armed(desk)
    before = _account(desk)
    out = desk.decisions.cancel(ticket["ticket_id"], reason="withdrawn")
    assert out["cancelled"] is True
    assert out["status"] == "cancelled"
    assert out["released_qty"] == 0
    assert desk.decisions.get(ticket["ticket_id"])["status"] == "cancelled"
    assert _account(desk) == before


def test_second_cancel_and_second_reject_are_idempotent(desk, trigger_on):
    ticket = _armed(desk)
    desk.decisions.cancel(ticket["ticket_id"])
    again = desk.decisions.cancel(ticket["ticket_id"])
    assert again["cancelled"] is False
    assert again["status"] == "cancelled"
    rejected = desk.decisions.reject(ticket["ticket_id"], "again")
    assert rejected["status"] == "cancelled"
    assert rejected["rejected"] is False
    assert desk.decisions.get(ticket["ticket_id"])["status"] == "cancelled"


def test_cancelled_intent_cannot_be_promoted_by_a_later_frame(desk, trigger_on):
    ticket = _armed(desk)
    desk.decisions.cancel(ticket["ticket_id"])
    before = _account(desk)
    out = desk.decisions.approve(ticket["ticket_id"], actor="trigger:probe")
    assert out["status"] == "cancelled"
    assert _account(desk) == before, "a cancelled intent must never re-enter execution"


def test_withdrawal_does_not_rewrite_the_ledger_state(desk, trigger_on):
    """D-19: no order, no cash — the state argument only carries the revision."""
    ticket = _armed(desk)
    before = _account(desk)
    account_before = json.dumps(desk.book.state.get("account"), ensure_ascii=False,
                                sort_keys=True, default=str)
    updated_before = desk.book.state.get("updated_at")
    desk.decisions.cancel(ticket["ticket_id"])
    assert _account(desk) == before
    assert json.dumps(desk.book.state.get("account"), ensure_ascii=False,
                      sort_keys=True, default=str) == account_before
    assert desk.book.state.get("updated_at") == updated_before


def test_filled_ticket_still_cannot_be_cancelled(desk, trigger_on):
    """D-13 must not weaken the existing 'fill already won' answer."""
    ticket = _armed(desk)
    desk.decisions.approve(ticket["ticket_id"], actor="trigger:probe")
    status = desk.decisions.get(ticket["ticket_id"])["status"]
    if status == "filled":
        out = desk.decisions.cancel(ticket["ticket_id"])
        assert out["cancelled"] is False
        assert out["status"] == "filled"
        with pytest.raises(DecisionError):
            desk.decisions.reject(ticket["ticket_id"], "too late")
    else:
        # Guard can legitimately queue or refuse; the contract is that nothing
        # is silently rewritten to cancelled after the execution path ran.
        assert status != "cancelled"


def test_reject_on_a_normal_proposal_is_unchanged(desk, trigger_on):
    """D-13 only adds an armed branch; ordinary proposals still reject as before."""
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol), actor="agent")
    assert ticket["status"] == "proposed"
    out = desk.decisions.reject(ticket["ticket_id"], "no thesis", actor="agent")
    assert out["status"] == "rejected"
