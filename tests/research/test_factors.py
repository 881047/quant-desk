"""The cross-sectional factor layer is a measurement, never a signal.

These tests guard the three ways this layer could quietly become misleading:
a forward return that trades at the same close the factor was computed from,
an overlapping-horizon t-stat that flatters itself into significance, and a
response that grows an action / verdict / ranking field.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from service.research import factors


def _panel(n_days=90, n_names=60, seed=7):
    """A synthetic daily panel with a real, decaying cross-sectional signal."""
    rng = np.random.default_rng(seed)
    dates = [f"2026{i:04d}" for i in range(1, n_days + 1)]
    codes = [f"{i:06d}.SZ" for i in range(1, n_names + 1)]
    base = pd.DataFrame(
        [(d, c) for d in dates for c in codes], columns=["trade_date", "ts_code"]
    )
    # Factor value is stable per name with daily noise; the next-day return
    # depends on the factor with a known positive coefficient.
    signal = pd.Series(rng.normal(size=n_names), index=codes)
    # Every valuation column needs cross-sectional variation, otherwise the
    # factor is constant per day, its rank variance is zero and the IC is
    # correctly reported as unavailable. That path is tested elsewhere; this
    # fixture is for the full 9-factor x 4-horizon path.
    size = pd.Series(np.exp(rng.normal(14, 0.8, n_names)), index=codes)
    turn = pd.Series(rng.uniform(0.2, 5.0, n_names), index=codes)
    pe = pd.Series(rng.uniform(8, 60, n_names), index=codes)
    rows = []
    close_by_name = {c: 10.0 + rng.uniform(-1, 1) for c in codes}
    for d in dates:
        noise = pd.Series(rng.normal(0, 0.01, n_names), index=codes)
        ret = 0.002 * signal.reindex(codes).to_numpy() + noise.to_numpy()
        for c, r in zip(codes, ret):
            pre = close_by_name[c]
            close = pre * (1.0 + r)
            close_by_name[c] = close
            rows.append((d, c, pre, close, pre * (1 + r / 2), 1e7,
                         size[c], turn[c], pe[c], 1.5 + signal[c]))
    daily = pd.DataFrame(rows, columns=[
        "trade_date", "ts_code", "pre_close", "close", "open", "amount",
        "total_mv", "turnover_rate", "pe_ttm", "pb"])
    basic = daily[["trade_date", "ts_code", "total_mv", "turnover_rate", "pe_ttm", "pb"]].copy()
    flow = daily[["trade_date", "ts_code"]].copy()
    flow["net_mf_amount"] = rng.normal(0, 1e4, len(flow))
    return {"daily": daily, "daily_basic": basic, "moneyflow": flow}


def _store(frames):
    class Store:
        def read(self, dataset, start=None, end=None, columns=None, **_):
            frame = frames.get(dataset)
            if frame is None:
                return pd.DataFrame()
            if start:
                frame = frame[frame["trade_date"] >= str(start)]
            if end:
                frame = frame[frame["trade_date"] <= str(end)]
            if columns:
                frame = frame[[c for c in columns if c in frame.columns]]
            return frame.copy()

    return Store()


def test_forward_return_enters_at_next_open_not_same_close():
    frames = _panel(n_days=10, n_names=8)
    fwd = factors._forward_returns(frames, horizons=(1,))
    out = fwd[1]
    daily = frames["daily"]
    close = factors._wide(daily, "close")
    open_ = factors._wide(daily, "open")
    # Row t is close[t+1] / open[t+1] - 1, demeaned across names.
    first = out.index[0]
    i = close.index.get_loc(first)
    entry = open_.iloc[i + 1]
    exit_ = close.iloc[i + 1]
    raw = (exit_ / entry - 1.0)
    expected = raw - raw.mean()
    np.testing.assert_allclose(out.loc[first].to_numpy(), expected.to_numpy(),
                               rtol=1e-9, atol=1e-12)
    # The T close must not appear anywhere in the entry price.
    assert not np.allclose(raw.to_numpy(),
                           (exit_ / close.iloc[i] - 1.0).to_numpy())


def test_hac_t_penalises_overlapping_horizons():
    rng = np.random.default_rng(11)
    # A strongly autocorrelated IC series: iid t looks significant, HAC does not.
    n = 120
    shock = rng.normal(0, 1, n)
    ic = pd.Series(np.convolve(shock, np.ones(8) / 8, mode="same")
                   + 0.001) + 0.002 * np.arange(n) / n
    naive = factors._ic_stats(ic, horizon=1)
    long = factors._ic_stats(ic, horizon=10)
    assert naive["status"] == "measured"
    assert abs(long["t_stat"]) < abs(long["t_stat_iid"]), \
        "overlapping horizons must not inflate the reported t"
    assert long["hac_lags"] == 10
    assert long["p_value"] >= 0.0


def test_insufficient_days_report_no_ic():
    out = factors._ic_stats(pd.Series([0.01] * (factors.MIN_IC_DAYS - 1)))
    assert out["status"] == "insufficient"
    assert out["ic_mean"] is None and out["p_value"] is None


def test_panel_is_read_only_and_corrects_for_multiple_tests():
    frames = _panel()
    out = factors.panel(_store(frames), start="20260001", end="20260900")
    assert out["ok"] is True
    assert out["read_only"] is True
    for forbidden in ("action", "verdict", "signal", "score", "ranking",
                      "target_price", "recommendation"):
        assert forbidden not in out
    mt = out["multiple_testing"]
    assert mt["n_tests"] == len(factors.FACTOR_SPECS) * len(factors.HORIZONS)
    assert mt["method"] == "Benjamini-Hochberg FDR"
    assert out["provenance"]["entry"] == "T+1 open"
    assert out["provenance"]["fundamentals_used"] is False
    assert out["caveats"]


def test_panel_fails_closed_on_short_history_and_unknown_factor():
    frames = _panel(n_days=30)
    short = factors.panel(_store(frames), start="20260001", end="20260300")
    assert short["ok"] is False and short["status"] == "short_history"
    bad = factors.panel(_store(_panel()), start="20260001", end="20260900",
                        factors=("not_a_factor",))
    assert bad["ok"] is False and bad["status"] == "unknown_factor"


def test_summary_payload_stays_under_the_host_ceiling():
    import json
    frames = _panel()
    out = factors.panel(_store(frames), start="20260001", end="20260900")
    assert out["detail"] == "summary"
    assert len(json.dumps(out, ensure_ascii=False, allow_nan=False)) < 16000
    one = factors.panel(_store(frames), start="20260001", end="20260900",
                        factors=("mom20",))
    assert one["detail"] == "full"
    assert "by_horizon" in one["factors"][0]


def test_neutralisation_keeps_unmapped_names_and_nans():
    idx = ["20260101"]
    cols = ["A", "B", "C"]
    f = pd.DataFrame([[1.0, 2.0, np.nan]], index=idx, columns=cols)
    mv = pd.DataFrame([[10.0, 10.0, 10.0]], index=idx, columns=cols)
    industry = pd.Series(["银行", "银行"], index=["A", "B"])
    out = factors._neutralise(f, mv, industry)
    assert np.isnan(out.loc["20260101", "C"]), "missing must stay missing"
    # C is unmapped and must not be demeaned to zero by a phantom industry.
    assert out.loc["20260101", "C"] != 0.0 or np.isnan(out.loc["20260101", "C"])


# ── 1–5 日连续期限族（2026-11-12）───────────────────────────────────────────
# 期限轴和因子轴同属一个 FDR 家族。多一个期限就多一次检验，所以 dense 族的
# q 值不能拿去和默认族的在案结论横向比较；这两条由 family_scope 显式说出来。

def test_parse_horizons_resolves_families_and_rejects_garbage():
    assert factors.parse_horizons(None) == factors.HORIZONS
    assert factors.parse_horizons("default") == factors.HORIZONS
    assert factors.parse_horizons("dense") == factors.DENSE_HORIZONS == (1, 2, 3, 4, 5)
    assert factors.parse_horizons("1-5") == (1, 2, 3, 4, 5)
    assert factors.parse_horizons("1,2,3,4,5") == (1, 2, 3, 4, 5)
    assert factors.parse_horizons((3, 1, 2)) == (1, 2, 3), "normalised, primary is day 1"
    assert factors.parse_horizons("1,2,3,4,5,10") == (1, 2, 3, 4, 5, 10)
    for bad in ("1,2,0", "1,2,99", "1,1", "dense,5", "1,2,3,4,5,6,7,8,9", ""):
        if bad == "":
            continue  # empty means default, by design
        try:
            factors.parse_horizons(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"bad horizon family accepted: {bad!r}")


def test_dense_family_measures_every_day_from_1_to_5():
    frames = _panel()
    out = factors.panel(_store(frames), start="20260001", end="20260900",
                        horizons="dense", detail="full")
    assert out["ok"] is True
    assert out["horizons"] == [1, 2, 3, 4, 5]
    assert out["family_scope"]["is_default_family"] is False
    assert out["family_scope"]["comparable_to_default_family"] is False
    assert "36" in out["family_scope"]["note"]
    # 9 factors x 5 horizons = 45 tests, not 36 with two extra columns tacked on.
    assert out["multiple_testing"]["n_tests"] == len(factors.FACTOR_SPECS) * 5
    assert out["multiple_testing"]["family"] == [1, 2, 3, 4, 5]
    for row in out["factors"]:
        if row.get("status") != "measured":
            continue
        assert set(row["by_horizon"]) == {"1", "2", "3", "4", "5"}
        assert [d["h"] for d in row["decay"]] == [1, 2, 3, 4, 5]


def test_default_family_stays_the_36_test_on_book_baseline():
    frames = _panel()
    out = factors.panel(_store(frames), start="20260001", end="20260900")
    assert out["horizons"] == [1, 3, 5, 10]
    assert out["family_scope"]["is_default_family"] is True
    assert out["family_scope"]["comparable_to_default_family"] is True
    assert out["multiple_testing"]["n_tests"] == len(factors.FACTOR_SPECS) * 4


def test_bad_horizon_family_is_an_answer_not_a_500():
    from service.trading import knowledge

    # The family is validated before the desk is touched, so a bad value must
    # return a structured ok:false (never an exception the model must guess at).
    out = knowledge.factor_read(None, horizons="1,2,99")
    assert out["ok"] is False and out["status"] == "bad_horizons"
    assert out["available"]["dense"] == [1, 2, 3, 4, 5]
    assert out["read_only"] is True


def test_current_industry_snapshot_is_not_labelled_pit_neutralisation():
    frames = _panel(n_days=65, n_names=12)
    frames["index_member"] = pd.DataFrame({
        "ts_code": sorted(frames["daily"]["ts_code"].unique()),
        "l1_name": ["行业A", "行业B"] * 6,
    })
    out = factors.panel(_store(frames), start="20260001", end="20260900",
                        sw_table=frames["index_member"], factors=("mom20",),
                        horizons=[1], neutralise=True)
    neutral = out["neutralisation"]
    assert neutral["industry_enabled"] is True
    assert neutral["industry_basis"] == "current_membership_snapshot"
    assert neutral["industry_pit_safe"] is False
    assert neutral["raw_ic_available"] is True
    assert any("不是严格 PIT" in caveat for caveat in out["caveats"])
