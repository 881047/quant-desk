"""Short-alpha evidence synthesis: fixed buckets, net excess, and no fill fiction."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app
from service.trading import board, build_desk
from service.trading import short_alpha

TOKEN = {"X-Quant-Token": "change-me-local-only"}
DAY = "20261113"


def _panel() -> pd.DataFrame:
    rows = []
    days = pd.bdate_range("2026-10-26", periods=10).strftime("%Y%m%d")
    for symbol, boards_n in (("000001.SZ", 1), ("000002.SZ", 2)):
        for i in range(40):
            day_index = i // 4
            if symbol == "000001.SZ":
                value = 0.02
            else:
                # Every day is internally constant, but day means alternate. A day-block
                # bootstrap therefore crosses zero even though the event mean is +2%.
                value = 0.08 if day_index % 2 == 0 else -0.04
            rows.append({
                "trade_date": days[day_index], "ts_code": symbol,
                "boards": boards_n, "boards_bucket": boards_n,
                "seal_ratio": 0.05, "seal_tier": "高", "seal_window": "早盘",
                "fill_kind": "open", "close": 10.0, "theme_cluster": 1,
                "reason": "fixture", "excess_open_1": value,
                "excess_close_1": value, "excess_close_2": value,
            })
        rows.append({
            "trade_date": DAY, "ts_code": symbol, "boards": boards_n,
            "boards_bucket": boards_n, "seal_ratio": 0.05, "seal_tier": "高",
            "seal_window": "早盘", "fill_kind": "open", "close": 10.0,
            "theme_cluster": 1, "reason": "current", "excess_open_1": np.nan,
            "excess_close_1": np.nan, "excess_close_2": np.nan,
        })
    return pd.DataFrame(rows)


def _fake_desk():
    return SimpleNamespace(market=SimpleNamespace(latest_bar_date=lambda: DAY))


def _stub_inputs(monkeypatch, panel):
    monkeypatch.setattr(board, "_panel", lambda desk, day: panel)
    monkeypatch.setattr(short_alpha, "_derived_universe",
                        lambda desk, day: ({"000001.SZ", "000002.SZ"},
                                           {"000001.SZ": "甲", "000002.SZ": "乙"}))
    monkeypatch.setattr(short_alpha, "_scenario", lambda desk, symbol, day, row: {
        "account_ok": True, "total_round_trip_cost_bps": 50.0,
        "fill_probability": None, "order_created": False,
    })
    monkeypatch.setattr(short_alpha, "_seal_map", lambda desk, day: {
        "000001": {"at_last_frame": True, "from_peak_pct": 0.0},
        "000002": {"at_last_frame": True, "from_peak_pct": 0.0},
    })
    monkeypatch.setattr(short_alpha, "_base_compact", lambda desk, boards, day, cost: {
        "available": True, "n": 100, "n_days": 20, "gap_pp": 0.0,
    })


def test_positive_mean_with_crossing_interval_is_watch_not_candidate(monkeypatch):
    panel = _panel()
    _stub_inputs(monkeypatch, panel)
    out = short_alpha.analyze(_fake_desk(), DAY, limit=2)

    assert out["ok"] is True
    assert [row["symbol"] for row in out["candidates"]] == ["000001.SZ"]
    assert [row["symbol"] for row in out["watch"]] == ["000002.SZ"]
    candidate = out["candidates"][0]
    for key in ("t1_close", "t2_close"):
        horizon = candidate["evidence"]["horizons"][key]
        assert horizon["n"] == 40
        assert horizon["n_days"] == 10
        assert horizon["net_excess_mean"] == pytest.approx(0.015, abs=1e-6)
        assert horizon["net_excess_mean_ci95"][0] > 0
    watch = out["watch"][0]
    assert watch["horizons"]["t1_close"]["net_excess_mean"] > 0
    assert watch["horizons"]["t1_close"]["net_excess_mean_ci95"][0] < 0
    assert "action" not in candidate
    assert candidate["execution"]["order_created"] is False
    assert candidate["execution"]["fill_probability"] is None
    assert out["universe"]["matched_current"] == 2
    assert out["universe"]["reviewed"] == 2
    assert out["universe"]["excluded"] == 0


def test_zero_candidate_limit_does_not_relax_threshold_or_create_order(monkeypatch):
    _stub_inputs(monkeypatch, _panel())
    out = short_alpha.analyze(_fake_desk(), DAY, limit=0)
    assert out["candidate_count"] == 0
    assert out["candidates"] == []
    assert out["no_candidate_reason"]
    assert out["universe"]["post_review"]["candidate_limit_overflow"] == 1


def test_bucket_uses_sample_adequacy_and_cost_is_not_hidden(monkeypatch):
    panel = _panel()
    row = panel[(panel["trade_date"] == DAY) & (panel["ts_code"] == "000001.SZ")].iloc[0]
    free = short_alpha._evidence(panel, row, 0.0)
    costly = short_alpha._evidence(panel, row, 500.0)
    assert free["selected_rule"] == "板+封单+时段"
    assert free["selection_policy"] == "first_sample_adequate_bucket_not_best_return"
    assert free["horizons"]["t1_close"]["gross_excess_mean"] == pytest.approx(0.02)
    assert costly["horizons"]["t1_close"]["gross_excess_mean"] == pytest.approx(0.02)
    assert costly["horizons"]["t1_close"]["net_excess_mean"] == pytest.approx(-0.03)


def test_insufficient_independent_days_cannot_be_a_candidate():
    panel = _panel()
    panel = panel[(panel["trade_date"] == DAY)
                  | (panel["trade_date"] >= "20261106")]
    row = panel[(panel["trade_date"] == DAY) & (panel["ts_code"] == "000001.SZ")].iloc[0]
    evidence = short_alpha._evidence(panel, row, 50.0)
    assert evidence["available"] is False
    assert "n_days>=8" in evidence["reason"]


def test_recent_window_uses_market_sessions_not_event_only_days():
    panel = _panel()
    template = panel[(panel["trade_date"] == DAY) & (panel["ts_code"] == "000002.SZ")].iloc[0]
    extras = []
    for day in pd.bdate_range("20261109", periods=250).strftime("%Y%m%d"):
        other = template.copy()
        other["trade_date"] = day
        other["ts_code"] = "000003.SZ"
        other["boards"] = other["boards_bucket"] = 9
        other["excess_open_1"] = other["excess_close_1"] = other["excess_close_2"] = 0.01
        extras.append(other)
    panel = pd.concat([panel, pd.DataFrame(extras)], ignore_index=True)
    row = panel[(panel["trade_date"] == DAY) & (panel["ts_code"] == "000001.SZ")].iloc[0]
    evidence = short_alpha._evidence(panel, row, 50.0)
    expected_cutoff = sorted(panel["trade_date"].unique())[-250]
    assert evidence["recent_250"]["cutoff"] == expected_cutoff
    assert evidence["recent_250"]["cutoff"] != "20261026"
    assert evidence["recent_250"]["t1_close"]["n"] == 0


def test_real_board_panel_has_same_anchor_excess_columns():
    desk = build_desk()
    day = desk.market.latest_bar_date()
    if not day:
        pytest.skip("fixture desk has no daily bars")
    panel = board._panel(desk, day)
    if panel is None or panel.empty:
        pytest.skip("no real board panel")
    required = {"session_ret_open_1", "session_ret_close_1", "session_ret_close_2",
                "market_ret_open_1", "market_ret_close_1", "market_ret_close_2",
                "excess_open_1", "excess_close_1", "excess_close_2", "next2_close"}
    assert required <= set(panel.columns)
    valid = panel.dropna(subset=["session_ret_close_2", "market_ret_close_2", "excess_close_2"])
    assert len(valid) > 0
    assert np.allclose(valid["excess_close_2"],
                       valid["session_ret_close_2"] - valid["market_ret_close_2"],
                       atol=1e-12)
    assert np.isfinite(valid[["session_ret_close_2", "market_ret_close_2",
                              "excess_close_2"]].to_numpy()).all()


def test_http_route_is_read_only_and_passes_explicit_contract(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    seen = {}

    def fake_analyze(desk_arg, day, *, limit):
        seen.update(day=day, limit=limit)
        return {"ok": True, "as_of": day, "candidates": [], "watch": [],
                "order_created": False}

    monkeypatch.setattr(short_alpha, "analyze", fake_analyze)
    with TestClient(app) as client:
        response = client.get("/short-alpha/candidates?as_of=20261113&limit=1", headers=TOKEN)
        assert response.status_code == 200
        assert response.json()["order_created"] is False
        assert seen == {"day": DAY, "limit": 1}
        assert client.post("/short-alpha/candidates", headers=TOKEN).status_code == 405
