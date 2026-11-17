"""Short-alpha target weights, immutable reviews and paper-ticket execution facts."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from service.adapters.pit import TradingCalendarStore
from service.kernel.clock import SHANGHAI
from service.trading import short_alpha_execution as execution
from service.trading import short_alpha_tracking as tracking
from service.trading import strategy as strategy_mod
from service.trading.allocator import allocate

DAY = "20261113"
T1 = "20261116"
T2 = "20261117"
A = "000001.SZ"
B = "000002.SZ"


class _Market:
    def __init__(self):
        self.calendar = TradingCalendarStore((DAY, T1, T2))

    def latest_bar_date(self):
        return DAY


class _Paper:
    def __init__(self, tickets=()):
        self.tickets = list(tickets)

    def list_tickets(self, limit=500, status=None):
        rows = self.tickets if status is None else [
            row for row in self.tickets if row.get("status") == status]
        return copy.deepcopy(rows[:limit])


class _Book:
    def __init__(self, positions=()):
        self.positions = list(positions)

    def summary(self):
        return {"positions": copy.deepcopy(self.positions)}


def _desk(tickets=(), positions=()):
    return SimpleNamespace(market=_Market(), paper=_Paper(tickets), book=_Book(positions))


def test_short_alpha_cap_does_not_consume_generic_allocator():
    meta = strategy_mod.get("short_alpha")
    plan = allocate("neutral", {}, book="paper", include_cash=False)

    assert meta.status == "probation" and meta.max_weight == 0.60
    assert meta.auto_allocate is False
    assert "short_alpha" not in plan["weights"]
    assert plan["skipped"]["short_alpha"] == "capital requires an explicit frozen target_weight"
    assert plan["residual_weight"] > 0


def _judgment(weights=True):
    items = [
        {"symbol": A, "role": "candidate",
         "thesis": "WorkBuddy独立判断甲的封板结构与成本后收益值得进入候选观察。",
         "evidence": [f"short-alpha-evidence:{DAY}:{A}"],
         "invalidation": "开盘结构失效或流动性不足"},
        {"symbol": B, "role": "watch",
         "thesis": "WorkBuddy独立判断乙虽列入观察也可以按较小权重参与模拟交易。",
         "evidence": [f"short-alpha-evidence:{DAY}:{B}"],
         "invalidation": "不能形成可执行开盘机会"},
    ]
    if weights:
        items[0]["target_weight"] = 0.20
        items[1]["target_weight"] = 0.10
    return {
        "day": DAY,
        "reviewed_evidence": [f"short-alpha-evidence:{DAY}:{A}",
                              f"short-alpha-evidence:{DAY}:{B}"],
        "items": items, "no_candidate_reason": "", "replay": False,
        "client_request_id": "sa-weight-test-1",
    }


def _evidence(monkeypatch):
    def fake(desk, day, *, symbols, limit, offset=0):
        return {"ok": True, "rows": [
            {"evidence_id": f"short-alpha-evidence:{day}:{symbol}", "symbol": symbol,
             "name": "甲" if symbol == A else "乙",
             "execution": {"account_ok": True, "scenario_qty": 100,
                           "scenario_budget_cny": 1500.0,
                           "impact_inputs": {"adv_cny": 100000000.0, "sigma_daily": 0.02}}}
            for symbol in symbols]}

    monkeypatch.setattr(tracking, "evidence_pack", fake)


def _freeze(desk, tmp_path, monkeypatch, weights=True):
    _evidence(monkeypatch)
    return tracking.record_judgment(desk, _judgment(weights), root=tmp_path,
                                    now=datetime(2026, 11, 13, 19, 10, tzinfo=SHANGHAI))


def _review():
    return {
        "day": DAY,
        "items": [{"symbol": A, "decision": "exit_next_open",
                   "reason": "WorkBuddy重新阅读证据后认为原失效条件已经成立，因此选择下一开盘退出。",
                   "evidence": ["daily:20261116"]}],
        "client_request_id": "short-alpha-review:20261113:1910",
    }


def test_candidate_and_watch_weights_freeze_without_creating_orders(tmp_path, monkeypatch):
    desk = _desk()
    result = _freeze(desk, tmp_path, monkeypatch)
    assert result["order_created"] is False
    assert result["paper_ledger_modified"] is False
    items = result["record"]["frozen"]["items"]
    assert [row["target_weight"] for row in items] == [0.20, 0.10]
    compact = tracking.report(desk, root=tmp_path)["days"][0]["items"]
    assert [row["target_weight"] for row in compact] == [0.20, 0.10]


def test_weight_budget_is_checked_and_legacy_rows_stay_tracking_only(tmp_path, monkeypatch):
    desk = _desk()
    _evidence(monkeypatch)
    body = _judgment()
    body["items"][0]["target_weight"] = 0.50
    body["items"][1]["target_weight"] = 0.20
    with pytest.raises(ValueError, match="exceeds short_alpha cap"):
        tracking.record_judgment(desk, body, root=tmp_path)
    assert not (tmp_path / "data" / "short_alpha_oos" / f"{DAY}.json").exists()

    legacy = _freeze(desk, tmp_path, monkeypatch, weights=False)
    assert all(row["target_weight"] is None for row in legacy["record"]["frozen"]["items"])
    view = execution.execution_status(desk, root=tmp_path)
    assert [row["state"] for row in view["days"][0]["items"]] == [
        "tracking_only", "tracking_only"]
    with pytest.raises(ValueError, match="legacy tracking stays order-free"):
        execution.record_review(desk, _review(), root=tmp_path)


def test_review_is_immutable_idempotent_and_order_free(tmp_path, monkeypatch):
    desk = _desk()
    _freeze(desk, tmp_path, monkeypatch)
    before = desk.paper.tickets.copy()
    first = execution.record_review(
        desk, _review(), root=tmp_path,
        now=datetime(2026, 11, 16, 19, 10, tzinfo=SHANGHAI))
    same = execution.record_review(
        desk, _review(), root=tmp_path,
        now=datetime(2026, 11, 16, 19, 11, tzinfo=SHANGHAI))
    assert first["created"] is True and same["created"] is False
    assert first["record"]["frozen_sha256"] == same["record"]["frozen_sha256"]
    assert first["order_created"] is False and first["paper_ledger_modified"] is False
    assert desk.paper.tickets == before

    changed = _review()
    changed["items"][0]["reason"] = "事后试图用另一个理由覆盖当时已经冻结的退出判断，必须被拒绝。"
    with pytest.raises(execution.ExecutionConflict, match="different content"):
        execution.record_review(desk, changed, root=tmp_path)

    path = next((tmp_path / "data" / "short_alpha_reviews").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["frozen"]["request"]["items"][0]["reason"] = "tampered"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(execution.ExecutionConflict, match="hash mismatch"):
        execution.execution_status(desk, root=tmp_path)


def test_execution_view_links_tickets_position_review_and_t2_boundary(tmp_path, monkeypatch):
    entry_id = f"short-alpha:{DAY}:entry:{A}"
    exit_id = f"short-alpha:{DAY}:exit:{A}"
    tickets = [
        {"ticket_id": "td-exit", "client_request_id": exit_id, "status": "queued",
         "symbol": A, "side": "SELL", "strategy": "short_alpha", "qty": 100},
        {"ticket_id": "td-entry", "client_request_id": entry_id, "status": "filled",
         "symbol": A, "side": "BUY", "strategy": "short_alpha", "qty": 100,
         "target_weight": 0.20},
    ]
    positions = [{"symbol": A, "qty": 100, "available_qty": 100, "weight": 0.18}]
    desk = _desk(tickets, positions)
    _freeze(desk, tmp_path, monkeypatch)
    execution.record_review(desk, _review(), root=tmp_path)
    tickets_before = copy.deepcopy(desk.paper.tickets)
    positions_before = copy.deepcopy(desk.book.positions)

    view = execution.execution_status(desk, root=tmp_path)
    assert view["write_side_effects"] is False
    assert view["external_account_touched"] is False
    day = view["days"][0]
    assert day["entry_window_day"] == T1 and day["exit_deadline_day"] == T2
    row = day["items"][0]
    assert row["state"] == "open"
    assert row["thesis"] == "WorkBuddy独立判断甲的封板结构与成本后收益值得进入候选观察。"
    assert row["evidence"] == [f"short-alpha-evidence:{DAY}:{A}"]
    assert row["invalidation"] == "开盘结构失效或流动性不足"
    assert row["entry_ticket"]["ticket_id"] == "td-entry"
    assert row["exit_ticket"]["status"] == "queued"
    assert row["position"]["available_qty"] == 100
    assert row["latest_review"]["decision"] == "exit_next_open"
    assert day["items"][1]["state"] == "awaiting_entry"
    assert desk.paper.tickets == tickets_before and desk.book.positions == positions_before
