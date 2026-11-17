"""Power: the difference between "not yet" and "never with this design".

These tests pin the arithmetic and the one semantic decision that matters: a
positive point estimate that misses significance is only `underpowered` when the
sample could not have seen the bar in the first place. Otherwise it is `rejected`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from service.research import power


def _trades(days: int, per_day: int, mean: float, sigma: float, seed: int = 0) -> pd.DataFrame:
    """Daily clusters: every trade on a day shares that day's shock."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(days):
        shock = mean + rng.normal(0.0, sigma)
        for _ in range(per_day):
            rows.append({"trade_date": f"d{d:05d}", "excess": shock + rng.normal(0, sigma / 3)})
    return pd.DataFrame(rows)


def test_mde_falls_with_the_square_root_of_the_sample():
    assert power.mde(0.02, 100) == pytest.approx(power.mde(0.02, 400) * 2, rel=1e-9)
    assert power.mde(0.02, 1) == float("inf")
    assert power.mde(0.0, 100) == float("inf")


def test_required_n_inverts_the_mde():
    target = power.mde(0.03, 250)
    assert power.required_n(target, 0.03) == pytest.approx(250, abs=2)
    assert power.required_n(0.0, 0.03) is None
    assert power.required_n(0.01, 0.0) is None


def test_a_short_volatile_sample_cannot_see_the_bar():
    # 10 volatile days, one trade each, at 85 bps: the 2x stress floor is 170 bps
    # of daily mean edge, and the MDE here is ~565 bps — the design genuinely
    # cannot see it. (Forty days of the same tape would give ~220 bps of MDE
    # against the same 170 bps bar, i.e. it *can* see it. The old assertion called
    # that underpowered too, because it compared a daily MDE to a per-trade cost
    # and never multiplied by trades per day.)
    trades = _trades(days=10, per_day=1, mean=0.002, sigma=0.06)
    result = power.assess(trades, cost_bps=85)
    assert result["underpowered"] is True
    assert result["mde_bps"] > result["floor_bps_per_day"]


def test_a_long_quiet_sample_can_see_it():
    trades = _trades(days=500, per_day=3, mean=0.002, sigma=0.02)
    result = power.assess(trades, cost_bps=85)
    assert result["underpowered"] is False
    assert result["mde_bps"] < result["floor_bps_per_day"]


def test_without_a_bar_nothing_is_called_underpowered():
    trades = _trades(days=30, per_day=4, mean=0.002, sigma=0.08)
    assert power.assess(trades, cost_bps=0)["underpowered"] is False


def test_the_effect_is_a_day_mean_not_a_trade_mean():
    """Two days, one with ten trades, one with one: they count equally."""
    frame = pd.DataFrame({
        "trade_date": ["d1"] * 10 + ["d2"],
        "excess": [0.02] * 10 + [0.0],
    })
    result = power.assess(frame, cost_bps=0)
    assert result["effect"] == pytest.approx(0.01)
    assert result["trades_per_day"] == pytest.approx(5.5)


def test_required_trades_uses_the_observed_signal_rate():
    trades = _trades(days=60, per_day=6, mean=0.010, sigma=0.05, seed=3)
    result = power.assess(trades, cost_bps=50)
    assert result["required_days"] is not None
    assert result["required_trades"] >= result["required_days"] * 5


def test_empty_input_is_not_an_error():
    assert power.assess(pd.DataFrame(), cost_bps=85)["n"] == 0
    one_day = pd.DataFrame({"trade_date": ["d1"], "excess": [0.01]})
    assert power.assess(one_day, cost_bps=85)["days"] == 1


def test_power_alpha_matches_referee_policy_and_marks_independence_limit():
    result = power.assess(_trades(days=30, per_day=4, mean=0.002, sigma=0.03))
    assert result["alpha"] == 0.10
    assert result["independence"]["unit"].startswith("trade_date")
    assert result["independence"]["days"] == 30
    assert "自相关" in result["independence"]["assumption"]
