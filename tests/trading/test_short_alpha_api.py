"""HTTP contract for kernel evidence and WorkBuddy-owned short-alpha judgments."""

from __future__ import annotations

from fastapi.testclient import TestClient

from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app
from service.trading import short_alpha
from service.trading import short_alpha_tracking as tracking
from service.trading import short_alpha_execution as execution

TOKEN = {"X-Quant-Token": "change-me-local-only"}


def _patch_desk(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)


def test_evidence_route_is_read_only_and_paginated(desk, monkeypatch):
    _patch_desk(desk, monkeypatch)
    seen = {}

    def fake(desk_arg, day, *, symbols, limit, offset):
        seen.update(day=day, symbols=symbols, limit=limit, offset=offset)
        return {"ok": True, "as_of": day, "rows": [],
                "decision_contract": {"contains_kernel_alpha_verdict": False}}

    monkeypatch.setattr(short_alpha, "evidence_pack", fake)
    with TestClient(app) as client:
        response = client.get(
            "/short-alpha/evidence?as_of=20261113&symbols=000001.SZ,000002.SZ&limit=2&offset=2",
            headers=TOKEN)
        assert response.status_code == 200
        assert response.json()["decision_contract"]["contains_kernel_alpha_verdict"] is False
        assert seen == {"day": "20261113", "symbols": ["000001.SZ", "000002.SZ"],
                        "limit": 2, "offset": 2}
        assert client.post("/short-alpha/evidence", headers=TOKEN).status_code == 405


def _body():
    return {
        "day": "20261113",
        "reviewed_evidence": ["short-alpha-evidence:20261113:000001.SZ"],
        "items": [],
        "no_candidate_reason": "WorkBuddy阅读证据后明确没有候选。",
        "replay": False,
        "client_request_id": "sa-api-test-1",
    }


def test_judgment_route_returns_compact_information_only_receipt(desk, monkeypatch):
    _patch_desk(desk, monkeypatch)
    seen = {}

    def fake(desk_arg, judgment):
        seen.update(judgment)
        record = {
            "signal_day": "20261113", "judgment_status": "explicit_no_candidate",
            "frozen_sha256": "abc123",
            "frozen": {"capture_kind": "prospective", "items": []},
        }
        return {"ok": True, "created": True, "record": record}

    monkeypatch.setattr(tracking, "record_judgment", fake)
    with TestClient(app) as client:
        response = client.post("/short-alpha/judgments", json=_body(), headers=TOKEN)
        assert response.status_code == 200
        payload = response.json()
        assert payload["judgment_owner"] == "WorkBuddy"
        assert payload["kernel_substituted_selection"] is False
        assert payload["order_created"] is False
        assert payload["paper_ledger_modified"] is False
        assert "frozen" not in payload and "evidence_pack" not in payload
        assert seen["day"] == "20261113"


def test_judgment_rejects_order_fields_and_frozen_conflict(desk, monkeypatch):
    _patch_desk(desk, monkeypatch)

    def conflict(desk_arg, judgment):
        raise tracking.TrackingConflict("already frozen")

    monkeypatch.setattr(tracking, "record_judgment", conflict)
    with TestClient(app) as client:
        illegal = _body() | {"price": 10.2}
        assert client.post("/short-alpha/judgments", json=illegal,
                           headers=TOKEN).status_code == 422
        response = client.post("/short-alpha/judgments", json=_body(), headers=TOKEN)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "SHORT_ALPHA_JUDGMENT_FROZEN"


def test_review_route_freezes_reason_without_order_side_effects(desk, monkeypatch):
    _patch_desk(desk, monkeypatch)
    seen = {}

    def fake(desk_arg, review):
        seen.update(review)
        return {"ok": True, "created": True, "record": {
            "signal_day": "20261113", "review_id": "short-alpha-review:20261113:1910",
            "captured_at": "2026-11-16T19:10:00+08:00", "frozen_sha256": "review-hash"}}

    monkeypatch.setattr(execution, "record_review", fake)
    body = {"day": "20261113", "items": [{
                "symbol": "000001.SZ", "decision": "exit_next_open",
                "reason": "WorkBuddy重新阅读证据后认为原失效条件已经成立，因此选择下一开盘退出。",
                "evidence": ["daily:20261116"]}],
            "client_request_id": "short-alpha-review:20261113:1910"}
    with TestClient(app) as client:
        response = client.post("/short-alpha/reviews", json=body, headers=TOKEN)
        assert response.status_code == 200
        payload = response.json()
        assert payload["order_created"] is False
        assert payload["paper_ledger_modified"] is False
        assert payload["review_id"] == body["client_request_id"]
        assert seen["items"][0]["decision"] == "exit_next_open"
        assert client.post("/short-alpha/reviews", json=body | {"confirm": True},
                           headers=TOKEN).status_code == 422


def test_execution_route_is_read_only_and_day_scoped(desk, monkeypatch):
    _patch_desk(desk, monkeypatch)
    seen = {}

    def fake(desk_arg, *, day, limit):
        seen.update(day=day, limit=limit)
        return {"ok": True, "days": [], "write_side_effects": False,
                "external_account_touched": False}

    monkeypatch.setattr(execution, "execution_status", fake)
    with TestClient(app) as client:
        response = client.get("/short-alpha/execution?day=20261113&limit=3", headers=TOKEN)
        assert response.status_code == 200
        assert response.json()["write_side_effects"] is False
        assert seen == {"day": "20261113", "limit": 3}
        assert client.post("/short-alpha/execution", headers=TOKEN).status_code == 405
        assert client.get("/short-alpha/execution?day=bad", headers=TOKEN).status_code == 422


def test_tracking_route_defaults_to_prospective(desk, monkeypatch):
    _patch_desk(desk, monkeypatch)
    seen = {}

    def fake(desk_arg, *, scope, limit, as_of):
        seen.update(scope=scope, limit=limit, as_of=as_of)
        return {"ok": True, "scope": scope, "days": []}

    monkeypatch.setattr(tracking, "report", fake)
    with TestClient(app) as client:
        response = client.get("/short-alpha/tracking?limit=4", headers=TOKEN)
        assert response.status_code == 200
        assert seen == {"scope": "prospective", "limit": 4, "as_of": None}
        assert client.get("/short-alpha/tracking?scope=invalid",
                          headers=TOKEN).status_code == 422
