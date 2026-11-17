"""Attribution must not peek. These tests are mostly about that.

A matched benchmark is only useful if its inputs are known at the signal close.
The dangerous operations are silent: a rolling window that includes tomorrow, a
forward column picked with the wrong offset, a forward return that uses the exit
price to *select* the peer group. Each one still produces a plausible number.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from service.research import attribution
from service.research.spec import build_trades, load_panel, market_returns, parse_spec


def _toy_panel(days: int = 90, symbols: int = 8, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=days).strftime("%Y%m%d")
    frame = []
    for i in range(symbols):
        close = 10.0 * np.cumprod(1 + rng.normal(0, 0.02, days))
        for d, c in zip(dates, close, strict=True):
            frame.append({"ts_code": f"{600000 + i:06d}.SH", "trade_date": d,
                          "close": float(c), "amount": float(c * 1e6)})
    panel = pd.DataFrame(frame)
    return panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


# ------------------------------------------------------------------ conventions
def test_forward_returns_agree_with_build_trades(store):
    panel = load_panel(store, "20240101", "20240131")
    pools = store.read("limit_pool_t0")
    spec = parse_spec({"pool": "limit_up", "boards": [1], "hold": 1})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    fwd = attribution.forward_returns(panel, spec)

    row = trades.iloc[0]
    value = fwd[(fwd["ts_code"] == row["ts_code"]) &
                (fwd["trade_date"] == row["trade_date"])]["fwd_ret"].iloc[0]
    assert value == pytest.approx(row["gross"])


def test_exposures_ignore_everything_after_the_signal_date():
    """Change the future, not the past: the exposures at T must not move."""
    panel = _toy_panel()
    before = attribution.build_exposures(panel)
    cutoff = sorted(panel["trade_date"].unique())[39]

    future = panel["trade_date"] > cutoff
    shocked = panel.copy()
    shocked.loc[future, "close"] *= 10.0
    shocked.loc[future, "amount"] *= 10.0
    after = attribution.build_exposures(shocked)

    left = before[before["trade_date"] == cutoff].set_index("ts_code").drop(columns="trade_date")
    right = after[after["trade_date"] == cutoff].set_index("ts_code").drop(columns="trade_date")
    pd.testing.assert_frame_equal(left, right)


def test_load_panel_preserves_and_builds_adjusted_close(store):
    daily = store.data["daily"]
    daily["adj_factor"] = 1.25
    panel = load_panel(store, "20240101", "20240131")
    expected = daily["close"] * 1.25
    pd.testing.assert_series_equal(panel["adj_close"].reset_index(drop=True),
                                   expected.reset_index(drop=True), check_names=False)
    assert "adj_f1" in panel and "adj_p1" in panel


def test_exposures_carry_no_market_cap_without_basics():
    panel = _toy_panel()
    rows = attribution.build_exposures(panel)
    assert "log_mcap" not in rows.columns
    assert {"beta_60", "rev_20", "log_amount"} <= set(rows.columns)


# -------------------------------------------------------------------- matching
def _universe(fwd_map: dict[str, float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    symbols = list(fwd_map)
    exposures = pd.DataFrame({
        "trade_date": ["20240102"] * len(symbols),
        "ts_code": symbols,
        "rev_20": np.arange(len(symbols), dtype=float),   # the only thing that varies
        "beta_60": [0.0] * len(symbols),
        "log_amount": [0.0] * len(symbols),
    })
    fwd = pd.DataFrame({"trade_date": ["20240102"] * len(symbols), "ts_code": symbols,
                        "fwd_ret": [fwd_map[s] for s in symbols]})
    return exposures, fwd


def _thirty(extra: dict[str, float] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Thirty names, reversal = index, and +1% for every name at index >= 10.

    The trades in the tests below sit at 12 / 17 / 22 — inside the high-reversal
    group but not adjacent to each other, so their nearest neighbours (11/13/14,
    16/18/19, 21/23/24) are never other trades. Without that gap the peer group
    contaminates itself and the test would be measuring the fixture, not the code.
    """
    symbols = [f"6000{i:02d}.SH" for i in range(30)]
    extra = extra or {}
    universe = {s: (0.01 if i >= 10 else 0.0) + extra.get(s, 0.0)
                for i, s in enumerate(symbols)}
    exposures = pd.DataFrame({
        "trade_date": ["20240102"] * len(symbols),
        "ts_code": symbols,
        "rev_20": np.arange(len(symbols), dtype=float),
        "beta_60": [0.0] * len(symbols),
        "log_amount": [0.0] * len(symbols),
    })
    fwd = pd.DataFrame({"trade_date": ["20240102"] * len(symbols), "ts_code": symbols,
                        "fwd_ret": [universe[s] for s in symbols]})
    return exposures, fwd


TRADES = ["600012.SH", "600017.SH", "600022.SH"]


def test_matching_removes_a_characteristic_driven_edge():
    """High-reversal names earn +1%: matched on reversal, the edge disappears."""
    exposures, fwd = _thirty()
    trades = pd.DataFrame({
        "trade_date": ["20240102"] * 3,
        "ts_code": TRADES,
        "net": [0.01] * 3,
        "excess": [0.01] * 3,
    })

    out = attribution.match_benchmark(trades, exposures, fwd, k=3)
    assert out["matched_excess"].abs().max() < 1e-9


def test_matching_keeps_an_edge_the_factors_do_not_explain():
    """Same reversal edge, plus +0.5% that belongs to these three names only."""
    exposures, fwd = _thirty({s: 0.005 for s in TRADES})
    trades = pd.DataFrame({
        "trade_date": ["20240102"] * 3,
        "ts_code": TRADES,
        "net": [0.015] * 3,
        "excess": [0.010] * 3,
    })

    out = attribution.match_benchmark(trades, exposures, fwd, k=3)
    assert out["matched_excess"].mean() == pytest.approx(0.005)


def test_a_trade_without_characteristics_is_kept_but_flagged():
    exposures, fwd = _universe({"600001.SH": 0.0, "600002.SH": 0.0})
    trades = pd.DataFrame({"trade_date": ["20240102"], "ts_code": ["999999.SH"],
                           "net": [0.02], "excess": [0.02]})
    out = attribution.match_benchmark(trades, exposures, fwd, k=2)
    assert out["matched_excess"].isna().all()
    assert attribution.summarise(out)["coverage"] == 0.0


def test_summary_reports_raw_and_matched_side_by_side():
    exposures, fwd = _universe({f"60000{i}.SH": 0.01 for i in range(12)})
    trades = pd.DataFrame({
        "trade_date": ["20240102"] * 6,
        "ts_code": [f"60000{i}.SH" for i in range(6)],
        "net": [0.01] * 6,
        "excess": [0.008] * 6,
    })
    out = attribution.match_benchmark(trades, exposures, fwd, k=3)
    summary = attribution.summarise(out)
    assert summary["n"] == 6
    assert summary["coverage"] == 1.0
    assert summary["mean_equal_weight_excess"] == pytest.approx(0.008)
    assert summary["mean_matched_excess"] == pytest.approx(0.0, abs=1e-9)


def test_matching_needs_at_least_two_factors():
    exposures, fwd = _universe({"600001.SH": 0.0})
    trades = pd.DataFrame({"trade_date": ["20240102"], "ts_code": ["600001.SH"],
                           "net": [0.0], "excess": [0.0]})
    with pytest.raises(ValueError):
        attribution.match_benchmark(trades, exposures[["trade_date", "ts_code"]], fwd)


# ----------------------------------------------------------------------- gate
def test_promotion_gate_requires_a_residual_with_evidence():
    good = {"n": 300, "coverage": 1.0, "mean_matched_excess": 0.002, "t_stat": 2.4}
    assert attribution.blocks_promotion(good) == (False, "")

    blocked, why = attribution.blocks_promotion({**good, "n": 47})
    assert blocked and "sample" in why

    blocked, why = attribution.blocks_promotion({**good, "mean_matched_excess": -0.001})
    assert blocked and "residual alpha" in why

    blocked, why = attribution.blocks_promotion({**good, "t_stat": 1.3})
    assert blocked and "residual t" in why

    blocked, _ = attribution.blocks_promotion(None)
    assert blocked


def test_low_attribution_coverage_blocks_even_with_a_large_t_stat():
    summary = {"n": 300, "coverage": 0.40,
               "mean_matched_excess": 0.002, "t_stat": 4.0}
    blocked, why = attribution.blocks_promotion(summary)
    assert blocked and "coverage" in why


def test_exposures_prefer_adjusted_close_when_available():
    panel = _toy_panel(days=30, symbols=3)
    panel["adj_close"] = panel["close"]
    cutoff = sorted(panel["trade_date"].unique())[10]
    panel.loc[panel["trade_date"] <= cutoff, "adj_close"] *= 2.0
    adjusted = attribution.build_exposures(panel)
    last = adjusted["trade_date"].max()
    row = adjusted[adjusted["trade_date"] == last].iloc[0]
    source = panel[panel["ts_code"] == row["ts_code"]].set_index("trade_date")["adj_close"]
    assert row["rev_20"] == pytest.approx(source.loc[last] / source.shift(20).loc[last] - 1)
    raw = panel[panel["ts_code"] == row["ts_code"]].set_index("trade_date")["close"]
    assert row["rev_20"] != pytest.approx(raw.loc[last] / raw.shift(20).loc[last] - 1)
