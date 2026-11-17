"""Decay monitors must detect a broken edge without inventing one.

Two failure modes to avoid: a detector so twitchy that normal variance retires a
working strategy, and one so slack that it never says anything. These tests pin
both ends, plus the rule that the module reports a state and never an order.
"""

from __future__ import annotations

import numpy as np
import pytest

from service.trading import decay

BASE = decay.Baseline(mean=0.005, sigma=0.03, n=300, source="test")


def _draws(n: int, mean: float, sigma: float = 0.03, seed: int = 0) -> list[float]:
    return list(np.random.default_rng(seed).normal(mean, sigma, n))


# ------------------------------------------------------------------ baseline
def test_a_baseline_without_sigma_is_refused():
    assert decay.baseline_from_evidence(None) is None
    assert decay.baseline_from_evidence({"mean_excess": 0.004}) is None
    assert decay.baseline_from_evidence({"mean_excess": 0.004, "std": 0}) is None
    baseline = decay.baseline_from_evidence({"mean_excess": 0.004, "std": 0.03, "n": 200})
    assert baseline is not None and baseline.mean == pytest.approx(0.004)
    with pytest.raises(ValueError):
        decay.Baseline(mean=0.0, sigma=0.0)


@pytest.mark.parametrize("mean,sigma", [(float("nan"), 0.03), (0.0, float("nan")),
                                        (float("inf"), 0.03), (0.0, float("inf"))])
def test_baseline_requires_finite_moments(mean, sigma):
    with pytest.raises(ValueError, match="finite"):
        decay.Baseline(mean=mean, sigma=sigma)
    assert decay.baseline_from_evidence({"mean_excess": mean, "std": sigma}) is None


@pytest.mark.parametrize("window", [0, -1, True, 2.5])
def test_rolling_and_evaluate_require_a_positive_integer_window(window):
    with pytest.raises(ValueError, match="window"):
        decay.rolling([0.0] * 20, BASE, window=window)
    with pytest.raises(ValueError, match="window"):
        decay.evaluate([0.0] * 20, BASE, window=window)


@pytest.mark.parametrize("alpha,beta", [(0, 0.2), (1, 0.2), (0.05, 0),
                                        (0.05, 1), (float("nan"), 0.2)])
def test_sprt_requires_probabilities_between_zero_and_one(alpha, beta):
    with pytest.raises(ValueError, match="alpha and beta"):
        decay.sprt([0.0], BASE, alpha=alpha, beta=beta)


# -------------------------------------------------------------------- cusum
def test_cusum_fires_on_a_sudden_break():
    values = _draws(40, mean=-0.03, seed=2)
    out = decay.cusum(values, BASE)
    assert out["alarm"] is True
    assert out["alarm_at"] is not None and out["alarm_at"] < 20


def test_cusum_stays_quiet_while_the_edge_holds():
    values = _draws(120, mean=0.005, seed=3)
    assert decay.cusum(values, BASE)["alarm"] is False


def test_cusum_ignores_a_slow_bleed_smaller_than_its_slack():
    """A 0.2-sigma shortfall is exactly what the SPRT is for, not the CUSUM."""
    values = _draws(200, mean=0.005 - 0.2 * 0.03, seed=4)
    assert decay.cusum(values, BASE)["alarm"] is False


# ------------------------------------------------------------------ rolling
def test_rolling_uses_the_baseline_standard_error():
    flat = [0.005] * 20
    out = decay.rolling(flat, BASE, window=20)
    assert out["z"] == pytest.approx(0.0)
    assert out["alarm"] is False

    low = [0.005 - 0.02] * 20
    out = decay.rolling(low, BASE, window=20)
    assert out["z"] < -2.0 and out["alarm"] is True


def test_rolling_needs_a_full_window():
    out = decay.rolling([0.0] * 5, BASE, window=20)
    assert out["z"] is None and out["alarm"] is False


# --------------------------------------------------------------------- sprt
def test_sprt_concludes_the_edge_is_gone():
    values = _draws(400, mean=0.0, seed=5)
    out = decay.sprt(values, BASE)
    assert out["decision"] == "edge_gone"
    assert out["llr"] <= out["lower"]


def test_sprt_concludes_the_edge_is_intact():
    values = _draws(400, mean=0.005, seed=6)
    out = decay.sprt(values, BASE)
    assert out["decision"] == "edge_intact"


def test_sprt_keeps_waiting_on_short_evidence():
    out = decay.sprt(_draws(8, mean=0.0, seed=7), BASE)
    assert out["decision"] == "continue"


# ----------------------------------------------------------------- evaluate
def test_a_short_record_is_not_a_verdict():
    out = decay.evaluate(_draws(12, mean=-0.02, seed=8), BASE, window=20)
    assert out["state"] == "insufficient"
    assert "need at least" in out["note"]


def test_an_intact_edge_reads_ok():
    out = decay.evaluate(_draws(60, mean=0.005, seed=9), BASE, window=20)
    assert out["state"] == "ok"


def test_a_broken_edge_reads_decaying():
    out = decay.evaluate(_draws(300, mean=-0.01, seed=10), BASE, window=20)
    assert out["state"] == "decaying"


def test_watch_is_a_single_alarm_not_a_verdict():
    """One detector firing is a question; two agreeing is an answer."""
    out = decay.evaluate(_draws(30, mean=-0.03, seed=11), BASE, window=20)
    assert out["state"] in {"watch", "decaying"}
    if out["state"] == "watch":
        assert out["cusum"]["alarm"] or out["rolling"]["alarm"]


def test_decay_never_emits_an_order():
    out = decay.evaluate(_draws(300, mean=-0.01, seed=10), BASE)
    text = str(out).lower()
    for forbidden in ("action", "sell_all", "liquidate", "reduce", "should"):
        assert forbidden not in text


# ------------------------------------------------------------------ monitor
def test_monitor_filters_by_strategy_and_orders_by_close_time():
    cases = [
        {"strategy": "a", "pnl_pct": 0.01, "closed_at": "2026-01-02"},
        {"strategy": "b", "pnl_pct": -0.05, "closed_at": "2026-01-01"},
        {"strategy": "a", "pnl_pct": 0.02, "closed_at": "2026-01-03"},
    ]
    baseline = decay.Baseline(mean=0.01, sigma=0.02)
    out = decay.monitor(cases, baseline, strategy="a", window=10)
    assert out["filtered"] == 2
    assert out["n"] == 2
    assert out["state"] == "insufficient"


def test_slippage_measures_the_fill_against_the_open():
    fills = [
        {"symbol": "600000.SH", "trade_date": "20260105", "price": 10.02,
         "fill_basis": "next_open"},
        {"symbol": "600000.SH", "trade_date": "20260106", "price": 9.98,
         "fill_basis": "next_open"},
        {"symbol": "600000.SH", "trade_date": "20260107", "price": 99.0,
         "fill_basis": "intraday"},
    ]
    bars = {("600000.SH", "20260105"): 10.0, ("600000.SH", "20260106"): 10.0}
    out = decay.slippage(fills, bars, side="BUY")
    assert out["n"] == 2
    assert out["mean_bps"] == pytest.approx(0.0, abs=0.01)

    buy_only = decay.slippage(fills[:1], bars, side="BUY")
    assert buy_only["mean_bps"] == pytest.approx(20.0)


def test_slippage_returns_nothing_rather_than_a_guess():
    out = decay.slippage([], {})
    assert out["n"] == 0 and out["mean_bps"] is None


def test_cagr_baseline_is_derived_for_books_without_trades():
    baseline = decay.baseline_from_evidence({"cagr": 0.096, "sharpe": 1.33})
    assert baseline is not None
    assert baseline.source == "evidence:cagr/sharpe"
    assert baseline.mean == pytest.approx(0.096 / 252)
    assert baseline.sigma == pytest.approx((0.096 / 1.33) / np.sqrt(252))
    assert decay.baseline_from_evidence({"cagr": 0.1, "sharpe": 0}) is None


def test_monitor_nav_turns_a_curve_into_daily_returns():
    nav = [{"trade_date": f"d{i:03d}", "nav": 100 * 1.001 ** i} for i in range(30)]
    out = decay.monitor_nav(nav, decay.Baseline(mean=0.001, sigma=0.0005), window=10)
    assert out["basis"] == "daily_nav"
    assert out["n"] == 29


def test_a_bleeding_nav_curve_reads_decaying():
    nav = [{"trade_date": f"d{i:03d}", "nav": 100 * 0.99 ** i} for i in range(60)]
    out = decay.monitor_nav(nav, decay.Baseline(mean=0.001, sigma=0.005), window=20)
    assert out["state"] == "decaying"
