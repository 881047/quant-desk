"""The boundary between measuring and trading, enforced at the HTTP layer.

The desk's premise is that the model decides and the kernel does arithmetic. A
route that sizes a position, picks a weight, or closes a breach hands the
decision back to code, and once that route exists a scheduler will eventually
call it. These tests exist so that re-adding one is a failing build, not a
design debate.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app

TOKEN = {"X-Quant-Token": "change-me-local-only"}


@pytest.fixture
def client(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    with TestClient(app) as c:
        yield c


def test_there_is_no_stop_enforcement_endpoint(client):
    for method in ("post", "get"):
        r = getattr(client, method)("/risk/stops/enforce?confirm=true", headers=TOKEN)
        assert r.status_code in (404, 405), f"{method.upper()} still exists: {r.status_code}"


def test_core_execute_refuses_to_pick_the_weights(client):
    """Without targets the server has nothing to execute.

    This used to succeed: the server computed the rule's allocation and booked
    it, so the request body was a signature on the kernel's own order list.
    """
    body = {"thesis": "我认为这条 ETF 规则在当前环境下仍然值得遵守，原因写在证据里。",
            "evidence": ["daily:20261023"], "invalidation": "若趋势闸门关闭则不应持有"}
    r = client.post("/core/execute", json=body, headers=TOKEN)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any("targets" in str(item.get("loc", "")) for item in detail)


def test_core_execute_rejects_a_code_outside_the_universe(client):
    # preview takes targets only (extra="forbid"), so send exactly that
    r = client.post("/core/preview", json={"targets": {"000001.SZ": 0.3}}, headers=TOKEN)
    assert r.status_code == 422
    assert "not in the core universe" in r.json()["detail"]["message"]


def test_core_execute_rejects_a_book_that_adds_up_to_more_than_the_account(client):
    body = {"targets": {"510300.SH": 0.7, "510500.SH": 0.7}}
    r = client.post("/core/preview", json=body, headers=TOKEN)
    assert r.status_code == 422
    assert "sum" in r.json()["detail"]["message"]


def test_preview_prices_without_booking(client, desk):
    before = len(desk.paper.core_trades(limit=10_000))
    r = client.post("/core/preview",
                    json={"targets": {"510300.SH": 0.5}}, headers=TOKEN)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["booked"] is False
    assert payload["targets"]["510300.SH"] == pytest.approx(0.5)
    assert payload["regime"]["source"] == "caller-supplied targets"
    assert len(desk.paper.core_trades(limit=10_000)) == before, "preview must not trade"


def test_execute_books_the_callers_own_targets(client, desk, monkeypatch):
    # The core sleeve was retired on 2026-11-01 (its defensive lot costs 2.8x the
    # account), so /core/execute answers 409 by default. This test is about the
    # *other* half of the contract — that a caller-supplied allocation is what
    # gets booked — so it has to say which world it is testing.
    import dataclasses

    import service.trading.core as core_mod

    monkeypatch.setattr(core_mod, "UNIVERSE_CFG",
                        dataclasses.replace(core_mod.UNIVERSE_CFG, retired=False))
    r = client.post("/core/execute", json={
        "targets": {"510300.SH": 0.4},
        "thesis": "我看好大盘，主动把四成资金放在沪深300上，理由与规则的输出无关。",
        "evidence": ["daily:20261023"],
        "invalidation": "若指数跌破两百日线则退出",
    }, headers=TOKEN)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["applied"] is True
    assert payload["plan"]["regime"]["source"] == "caller-supplied targets"
    booked = {t["symbol"] for t in desk.paper.core_trades(limit=100)}
    assert booked <= {"510300.SH"}, f"only the caller's targets may be bought: {booked}"

    logged = [d for d in desk.paper.list_decisions(limit=5) if d.get("kind") == "core_execute"]
    assert logged, "the decision log must record who decided"
    stored = logged[0]["payload"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored["targets"] == {"510300.SH": 0.4}
    assert "四成资金" in stored["thesis"]
