"""Terminal intent states become readable on the ticket, not only in a trace."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from service import settings
from service.kernel.clock import SHANGHAI
from service.trading import watch

DAY = "20261106"
_NOW = datetime(2026, 11, 6, 10, 0, 0, tzinfo=SHANGHAI)
_CONDITION = {"kind": "price", "op": "lte", "value": 10.0}
_EXPIRED = {**_CONDITION, "valid_until": "2026-11-06T09:35:00+08:00"}


@pytest.fixture(autouse=True)
def _switch_off(monkeypatch):
    """Terminal recording is observation-only; keep the money path closed."""
    monkeypatch.setattr(settings, "flag", lambda key: False)


class _Store:
    def __init__(self, tickets):
        self.tickets = {t["ticket_id"]: dict(t) for t in tickets}
        self.saved: list[dict] = []
        self.released: list[str] = []
        self.fail_writes = False

    def get_ticket(self, ticket_id):
        # Real store does ``json.loads(row["payload"])``: a fresh object each
        # time, so a failed save cannot leak a half-written ticket into memory.
        current = self.tickets.get(ticket_id)
        return dict(current) if current is not None else None

    def save_ticket(self, ticket):
        if self.fail_writes:
            raise RuntimeError("disk full")
        self.tickets[ticket["ticket_id"]] = dict(ticket)
        self.saved.append(dict(ticket))

    def release_proposal_claim(self, ticket_id):
        self.released.append(ticket_id)
        return 1


class _Desk:
    def __init__(self, tickets):
        self.store = _Store(tickets)
        self.decisions = self

    def list_tickets(self, **kwargs):
        return [dict(t) for t in self.store.tickets.values()
                if t.get("status") == "armed"]


def _ticket(ticket_id: str, condition) -> dict:
    return {"ticket_id": ticket_id, "symbol": "600000.SH", "qty": 100,
            "status": "armed", "contingency": condition}


def _run(desk, captured=None):
    return watch._run_intents(
        desk, DAY, captured if captured is not None else pd.DataFrame(),
        now=_NOW, base=None)


def test_denied_and_expired_are_recorded_on_the_ticket(tmp_path):
    desk = _Desk([_ticket("td-denied", _CONDITION),
                  _ticket("td-expired", _EXPIRED)])
    rows = _run(desk)
    assert {r["ticket_id"]: r["status"] for r in rows} == {
        "td-denied": "denied", "td-expired": "expired_untriggered"}
    denied = desk.store.tickets["td-denied"]
    expired = desk.store.tickets["td-expired"]
    assert denied["intent_terminal_state"] == "denied"
    assert denied["intent_terminal_reason"] == "unobservable:price"
    assert expired["intent_terminal_state"] == "expired_untriggered"
    assert expired["intent_terminal_reason"] == "past_valid_until"
    assert denied["intent_terminal_at"] == _NOW.isoformat(timespec="seconds")


def test_recording_does_not_change_the_state_machine():
    """Only an observation is added; promotion/expiry remain approve's job."""
    desk = _Desk([_ticket("td-x", _EXPIRED)])
    _run(desk)
    stored = desk.store.tickets["td-x"]
    assert stored["status"] == "armed"
    assert stored["contingency"] == _EXPIRED


def test_recording_is_idempotent():
    desk = _Desk([_ticket("td-x", _CONDITION)])
    _run(desk)
    first = dict(desk.store.tickets["td-x"])
    _run(desk)
    assert desk.store.tickets["td-x"] == first
    assert len(desk.store.saved) == 1, "a second frame must not append a second record"


def test_triggered_ticket_gets_no_terminal_record():
    captured = pd.DataFrame({"code": ["600000"], "last": [9.0]})
    desk = _Desk([_ticket("td-hit", _CONDITION)])
    rows = _run(desk, captured)
    assert rows[0]["status"] == "triggered"
    assert "intent_terminal_state" not in desk.store.tickets["td-hit"]
    assert desk.store.saved == []


def test_write_failure_never_breaks_the_watch():
    desk = _Desk([_ticket("td-x", _CONDITION)])
    desk.store.fail_writes = True
    rows = _run(desk)
    assert rows[0]["status"] == "denied"
    assert "intent_terminal_state" not in desk.store.tickets["td-x"]


def test_terminal_record_fields_are_optional_and_absent_by_default():
    """A fresh armed ticket must not carry any of the three fields."""
    ticket = _ticket("td-fresh", _CONDITION)
    assert "intent_terminal_state" not in ticket
    assert "intent_terminal_at" not in ticket
    assert "intent_terminal_reason" not in ticket


def test_only_expired_releases_the_fingerprint_slot():
    """D-18: denied may be a transient missing quote, so it keeps the slot."""
    desk = _Desk([_ticket("td-denied", _CONDITION),
                  _ticket("td-expired", _EXPIRED)])
    _run(desk)
    assert desk.store.released == ["td-expired"]


def test_expired_ticket_is_not_evaluated_again():
    """No duplicate shadow-trace lines for the same terminal event."""
    desk = _Desk([_ticket("td-expired", _EXPIRED)])
    first = _run(desk)
    assert first[0]["status"] == "expired_untriggered"
    assert _run(desk) == []
    assert desk.store.released == ["td-expired"], "release must be idempotent"


def test_a_failed_ticket_write_leaves_the_ticket_retryable():
    """Release precedes the write so a disk error cannot strand the slot."""
    desk = _Desk([_ticket("td-expired", _EXPIRED)])
    desk.store.fail_writes = True
    _run(desk)
    assert desk.store.released == ["td-expired"]
    assert "intent_terminal_state" not in desk.store.tickets["td-expired"]
    desk.store.fail_writes = False
    _run(desk)
    assert desk.store.tickets["td-expired"]["intent_terminal_state"] == "expired_untriggered"
