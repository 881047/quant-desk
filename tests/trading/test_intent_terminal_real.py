"""D-18 / D-20 against the **real** ticket store, not a FakeStore.

The synthetic tests in ``test_intent_terminal_record.py`` prove the control
flow; these prove that ``state.save_ticket`` / ``release_proposal_claim`` /
``book.reload()`` and the two idempotency keys behave the way the docs say.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from service import settings
from service.kernel.clock import SHANGHAI
from service.trading import intent, watch
from tests.trading.helpers import pick_symbols, proposal

DAY = "20261106"
_NOW = datetime(2026, 11, 6, 10, 0, 0, tzinfo=SHANGHAI)
_EXPIRED = {"kind": "price", "op": "lte", "value": 10.0,
            "valid_until": "2026-11-06T09:35:00+08:00"}
_LIVE = {"kind": "price", "op": "lte", "value": 10.0}


@pytest.fixture
def trigger_on(monkeypatch):
    original = settings.flag

    def flag(key: str) -> bool:
        if key == "trigger.enabled":
            return True
        return original(key)

    monkeypatch.setattr(settings, "flag", flag)


def _arm(desk, condition, **overrides):
    symbol = pick_symbols(desk, 1)[0]
    req = {**proposal(symbol), "contingency": condition, **overrides}
    ticket = desk.decisions.propose(req, actor="agent")
    first = desk.decisions.approve(ticket["ticket_id"], actor="agent")
    assert first["status"] == "armed"
    return req, ticket


def test_real_store_roundtrip_and_fingerprint_slot_is_reusable(
        desk, trigger_on, tmp_path):
    req, ticket = _arm(desk, _EXPIRED)
    rows = watch._run_intents(desk, DAY, pd.DataFrame(), now=_NOW, base=tmp_path)
    assert rows[0]["status"] == "expired_untriggered"

    stored = desk.decisions.get(ticket["ticket_id"])
    assert stored["intent_terminal_state"] == "expired_untriggered"
    assert stored["intent_terminal_reason"] == "past_valid_until"
    assert stored["status"] == "armed", "D-14: observation only, no state ruling"

    # A reader that never writes re-reads the book on its own; the record must
    # survive that reload rather than living only in an in-memory dict.
    desk.book.reload()
    assert desk.decisions.get(ticket["ticket_id"])["intent_terminal_at"]

    # Same condition, new intent: the fingerprint slot was released.
    again = desk.decisions.propose(dict(req), actor="agent")
    assert again["ticket_id"] != ticket["ticket_id"]


def test_client_request_id_promise_survives_the_release(desk, trigger_on, tmp_path):
    """Deleting proposal_claims must never break 'same id, same ticket'."""
    req, ticket = _arm(desk, _EXPIRED, client_request_id="fixed-handle-1")
    watch._run_intents(desk, DAY, pd.DataFrame(), now=_NOW, base=tmp_path)
    retry = desk.decisions.propose(dict(req), actor="agent")
    assert retry["ticket_id"] == ticket["ticket_id"]


def test_intent_eval_failure_is_contained_and_never_promotes(
        desk, trigger_on, tmp_path, monkeypatch):
    """D-20: a shadow-trace write failure must not kill the frame or spend money."""
    req, ticket = _arm(desk, _LIVE)

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(intent, "run", boom)
    monkeypatch.setattr(
        desk.decisions, "approve",
        lambda *_a, **_k: pytest.fail("must not approve after an eval failure"))
    rows = watch._run_intents(desk, DAY, pd.DataFrame(), now=_NOW, base=tmp_path)
    assert rows[0]["status"] == "error"
    assert "intent_eval" in rows[0]["reasons"][0]
    assert "intent_terminal_state" not in desk.decisions.get(ticket["ticket_id"])
