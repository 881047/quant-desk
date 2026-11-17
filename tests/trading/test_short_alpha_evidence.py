"""The kernel returns evidence, not a candidate/watch decision, for WorkBuddy."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from service.trading import board, short_alpha
from service.kernel.models import Instrument

DAY = "20261113"


def _panel():
    rows = []
    days = pd.bdate_range("2026-10-26", periods=10).strftime("%Y%m%d")
    for symbol in ("000001.SZ", "000002.SZ"):
        for i in range(40):
            value = 0.02 if symbol == "000001.SZ" else (0.08 if (i // 4) % 2 == 0 else -0.04)
            rows.append({"trade_date": days[i // 4], "ts_code": symbol,
                         "boards": 1, "boards_bucket": 1, "seal_ratio": 0.05,
                         "seal_window": "早盘", "fill_kind": "open", "close": 10.0,
                         "theme_cluster": 1, "reason": "fixture",
                         "excess_open_1": value, "excess_close_1": value,
                         "excess_close_2": value})
        rows.append({"trade_date": DAY, "ts_code": symbol, "boards": 1,
                     "boards_bucket": 1, "seal_ratio": 0.05, "seal_window": "早盘",
                     "fill_kind": "open", "close": 10.0, "theme_cluster": 1,
                     "reason": "current", "excess_open_1": np.nan,
                     "excess_close_1": np.nan, "excess_close_2": np.nan})
    return pd.DataFrame(rows)


def test_evidence_pack_has_no_kernel_verdict_or_return_based_rejection(monkeypatch):
    panel = _panel()
    monkeypatch.setattr(board, "_panel", lambda desk, day: panel)
    monkeypatch.setattr(short_alpha, "_derived_universe", lambda desk, day: (
        {"000001.SZ", "000002.SZ"}, {"000001.SZ": "甲", "000002.SZ": "乙"}))
    monkeypatch.setattr(short_alpha, "_scenario", lambda desk, symbol, day, row: {
        "account_ok": True, "total_round_trip_cost_bps": 50.0,
        "impact_inputs": {"adv_cny": 1e8, "sigma_daily": 0.02},
        "scenario_qty": 100, "scenario_budget_cny": 1500.0,
        "fill_probability": None, "order_created": False,
    })
    monkeypatch.setattr(short_alpha, "_seal_map", lambda desk, day: {})
    monkeypatch.setattr(short_alpha, "_base_compact", lambda desk, boards, day, cost: {
        "available": True, "n": 100, "n_days": 20, "gap_pp": 0.0,
    })
    desk = SimpleNamespace(market=SimpleNamespace(latest_bar_date=lambda: DAY))

    out = short_alpha.evidence_pack(desk, DAY, limit=2)

    assert out["ok"] is True
    assert [row["symbol"] for row in out["rows"]] == ["000001.SZ", "000002.SZ"]
    assert "candidates" not in out and "watch" not in out
    assert out["decision_contract"]["contains_kernel_alpha_verdict"] is False
    assert out["decision_contract"]["judgment_owner"] == "WorkBuddy"
    assert all(row["model_judgment_required"] for row in out["rows"])
    assert all("status" not in row for row in out["rows"])
    assert all(row["evidence_id"].startswith(f"short-alpha-evidence:{DAY}:")
               for row in out["rows"])
    # The mixed/negative row remains available; only WorkBuddy assigns its role.
    assert out["rows"][1]["eligible_for_model_judgment"] is True
    assert out["rows"][1]["execution"]["order_created"] is False


def test_scenario_freezes_real_impact_dataclass_with_cny_and_sigma_units():
    symbol = "000001.SZ"
    closes = [10.0 + (0.1 if i % 2 else -0.05) * i for i in range(22)]
    market = SimpleNamespace(
        instrument=lambda s, d: Instrument(symbol=s),
        adv_map=lambda d: pd.Series({symbol: 100_000_000.0}),
        history=lambda s, d, n: pd.DataFrame({"close": closes}),
    )
    desk = SimpleNamespace(
        market=market,
        book=SimpleNamespace(summary=lambda: {"equity": 5000.0, "cash_free": 5000.0}),
        paper=object(),
        limits=lambda: {"max_core_single": 0.3, "max_aggressive_single": 0.5,
                        "max_positions": 3},
    )
    result = short_alpha._scenario(desk, symbol, DAY,
                                   {"close": 10.0, "fill_kind": "open"})
    assert result["account_ok"] is True
    assert result["impact_inputs"]["adv_cny"] == 100_000_000.0
    expected_sigma = float(pd.Series(closes).pct_change().dropna().std())
    assert result["impact_inputs"]["sigma_daily"] == expected_sigma
    assert result["impact_inputs"]["sigma_daily"] > 0
