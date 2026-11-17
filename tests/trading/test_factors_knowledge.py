"""The factor topic and the two P0 consistency fixes (2026-11-12)."""

from __future__ import annotations

import json

import pandas as pd

from service.trading import knowledge


def _fake_desk(hypotheses):
    class Paper:
        def list_hypotheses(self, limit=200):
            return hypotheses

    class Desk:
        paper = Paper()

    return Desk()


def test_ledger_scope_is_derived_from_the_book_not_hardcoded():
    rows = [
        {"id": 1, "status": "rejected", "result": json.dumps({
            "spec": {"entry": "next_open"}, "gate_reading": "rejected"})},
        {"id": 6, "status": "rejected", "result": json.dumps({
            "spec": {"entry": "t_close"}, "gate_reading": "rejected"})},
        {"id": 7, "status": "rejected", "result": json.dumps({
            "spec": {"entry": "next_limit"}, "gate_reading": "rejected"})},
    ]
    scope = knowledge._ledger_scope([
        {"id": r["id"], "spec": json.loads(r["result"])["spec"]} for r in rows
    ])
    assert scope["entry_tested"]["t_close"] == [6]
    assert scope["entry_tested"]["next_limit"] == [7]
    assert "t_close" not in scope["entry_not_tested"]
    assert "next_close" in scope["entry_not_tested"]
    assert scope["source"].startswith("hypotheses")
    assert "默认网格" in scope["grid_scope"]


def test_factors_is_a_market_topic_hint():
    assert "factors" in knowledge.MARKET_TOPICS
    out = knowledge._needs_symbol("analog")
    assert "factors" in out["market_topics"]
    assert set(out["market_topics"]) == set(knowledge.MARKET_TOPICS)


def test_factor_read_fails_closed_without_calendar():
    class Store:
        def read(self, *args, **kwargs):
            return pd.DataFrame()

    class Market:
        store = Store()
        latest_bar_date = lambda self: "20261111"  # noqa: E731

    class Desk:
        market = Market()

    out = knowledge.factor_read(Desk(), window=120)
    assert out["ok"] is False and out["status"] == "short_calendar"
    assert out["read_only"] is True
