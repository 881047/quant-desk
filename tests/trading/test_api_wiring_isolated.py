"""Isolated API wiring tests; deliberately never imports the production ASGI app."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.api import security
from service.api import trading as trading_api
from service.trading import allocator, arena, regime
from service.trading import playbook as pb
from service.trading import strategy as strategy_mod

TOKEN = {"X-Quant-Token": "isolated-test-token"}


@pytest.fixture
def activation_client(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_api, "ROOT", tmp_path)
    monkeypatch.setattr(security, "TOKEN", "isolated-test-token")
    app = FastAPI()
    app.include_router(trading_api.router)
    with TestClient(app) as client:
        yield client


def _draft(root, title="v2"):
    return pb.propose(root, f"# {title}\n" + "新规则：单笔风险降到 3%。" * 5)


def test_agent_token_cannot_activate_with_replayable_constant(activation_client, tmp_path):
    draft = _draft(tmp_path)
    response = activation_client.post(
        "/playbook/activate",
        json={"draft_id": draft["draft_id"], "confirm": "ACTIVATE"},
        headers=TOKEN,
    )
    assert response.status_code == 422
    constant = activation_client.post(
        "/playbook/activate",
        json={"draft_id": draft["draft_id"], "challenge": "ACTIVATE"},
        headers=TOKEN,
    )
    assert constant.status_code == 422
    assert pb.current(tmp_path)["version"] is None


def test_activation_challenge_is_single_use(activation_client, tmp_path):
    draft = _draft(tmp_path)
    issued = pb.issue_challenge(tmp_path, draft["draft_id"])
    body = {"draft_id": draft["draft_id"], "challenge": issued["challenge"]}

    activated = activation_client.post("/playbook/activate", json=body, headers=TOKEN)
    assert activated.status_code == 200, activated.text
    replayed = activation_client.post("/playbook/activate", json=body, headers=TOKEN)
    assert replayed.status_code == 403


def test_activation_challenge_is_bound_to_draft_id_and_hash(activation_client, tmp_path):
    first = _draft(tmp_path, "first")
    second = _draft(tmp_path, "second")
    issued = pb.issue_challenge(tmp_path, first["draft_id"])

    other = activation_client.post(
        "/playbook/activate",
        json={"draft_id": second["draft_id"], "challenge": issued["challenge"]},
        headers=TOKEN,
    )
    assert other.status_code == 403

    draft_path = tmp_path / "playbook" / first["file"]
    draft_path.write_text(draft_path.read_text(encoding="utf-8") + "\n篡改。", encoding="utf-8")
    changed = activation_client.post(
        "/playbook/activate",
        json={"draft_id": first["draft_id"], "challenge": issued["challenge"]},
        headers=TOKEN,
    )
    assert changed.status_code == 403
    assert pb.current(tmp_path)["version"] is None


@pytest.mark.parametrize("mean_excess", [-0.02, None])
def test_strategies_uses_scoreboard_decisions_for_excess(monkeypatch, mean_excess):
    cases = [{"case_id": "c1", "strategy": "llm", "pnl_pct": 0.5}]
    decisions = [{"case_id": "c1", "excess": -0.02}]
    desk = SimpleNamespace(
        market=SimpleNamespace(latest_bar_date=lambda: "20261028"),
        paper=SimpleNamespace(list_cases=lambda limit: cases),
    )
    calls = []
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(
        regime, "compute", lambda market, day: SimpleNamespace(label="neutral"))
    monkeypatch.setattr(strategy_mod, "describe", lambda label: [])

    def fake_scoreboard(value):
        calls.append(("scoreboard", value))
        return {"decisions": decisions}

    def fake_stats(case_rows, decision_rows):
        calls.append(("stats", case_rows, decision_rows))
        return {"llm": allocator.StrategyStats(
            n=1, mean_excess=mean_excess, max_drawdown=-0.01,
        )}

    monkeypatch.setattr(arena, "scoreboard", fake_scoreboard)
    monkeypatch.setattr(allocator, "stats_from_cases", fake_stats)
    monkeypatch.setattr(allocator, "allocate", lambda label, stats: {"weights": {}})

    result = trading_api.strategies()
    assert calls == [("scoreboard", desk), ("stats", cases, decisions)]
    assert result["stats"]["llm"]["n"] == 1
    assert result["stats"]["llm"]["mean_excess"] == mean_excess
