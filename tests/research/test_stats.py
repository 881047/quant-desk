from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from service.kernel.costs import CostModel
from service.kernel.models import D
from service.research import stats
from service.research.spec import (
    SpecError,
    build_trades,
    fee_instrument,
    load_panel,
    market_returns,
    parse_spec,
    portfolio_series,
    scan_pool,
)


# ------------------------------------------------------------------ normal
def test_normal_ppf_inverts_cdf():
    for p in (0.01, 0.25, 0.5, 0.9, 0.975, 0.999):
        assert stats.norm_cdf(stats.norm_ppf(p)) == pytest.approx(p, abs=1e-6)


def test_counts_and_rejects():
    assert stats.norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    with pytest.raises(ValueError):
        stats.norm_ppf(1.0)


# --------------------------------------------------------------- metrics
def test_metrics_of_flat_series_is_zero():
    m = stats.metrics(pd.Series([0.0] * 50))
    assert m["sharpe"] == 0.0
    assert m["max_drawdown"] == 0.0
    assert m["n"] == 50


def test_metrics_of_steady_growth():
    r = pd.Series([0.001] * 252)
    m = stats.metrics(r)
    assert m["total_return"] == pytest.approx((1.001 ** 252) - 1, rel=1e-3)
    assert m["max_drawdown"] == 0.0
    assert m["sharpe"] > 10  # zero variance -> huge Sharpe, still finite


# ---------------------------------------------------------- deflated sharpe
def test_dsr_falls_when_many_trials_are_tried():
    rng = np.random.default_rng(11)
    r = pd.Series(rng.normal(0.0009, 0.01, 500))
    one = stats.deflated_sharpe(r, trials=1)
    many = stats.deflated_sharpe(r, trials=50,
                                 trial_sharpes=list(rng.normal(0, 0.03, 50)))
    assert one["dsr"] > many["dsr"]
    assert 0.0 <= many["dsr"] <= 1.0


def test_dsr_of_noise_is_not_significant():
    rng = np.random.default_rng(3)
    r = pd.Series(rng.normal(0.0, 0.01, 400))
    assert stats.deflated_sharpe(r, trials=10)["dsr"] < 0.9


# ------------------------------------------------------------------- PBO
def test_pbo_is_high_when_selection_is_noise():
    # A single CSCV run is noisy (70 heavily overlapping splits), so average
    # across seeds: under the null PBO must centre on 0.5.
    values = []
    for seed in range(12):
        rng = np.random.default_rng(seed)
        m = pd.DataFrame(rng.normal(0.0, 0.01, size=(400, 8)))
        out = stats.cscv_pbo(m, blocks=8)
        assert out["splits"] == math.comb(8, 4)
        values.append(out["pbo"])
    assert 0.35 < float(np.mean(values)) < 0.65


def test_pbo_is_low_when_one_config_really_wins():
    rng = np.random.default_rng(5)
    m = pd.DataFrame(rng.normal(0.0, 0.01, size=(400, 8)))
    m[0] = rng.normal(0.003, 0.008, 400)
    assert stats.cscv_pbo(m, blocks=8)["pbo"] < 0.2


def test_pbo_with_single_trial_is_invalid_not_zero():
    m = pd.DataFrame({"only": np.random.default_rng(1).normal(0, 0.01, 100)})
    out = stats.cscv_pbo(m)
    assert out["pbo"] is None
    assert out["ok"] is False


# ------------------------------------------------------------------ CPCV
def test_cpcv_folds_are_disjoint_and_embargoed():
    folds = stats.cpcv_folds(120, groups=6, k_test=2, embargo=3)
    assert len(folds) == math.comb(6, 2)
    for train, test in folds:
        assert not set(train) & set(test)
        gaps = [min(abs(int(t) - int(x)) for x in train) for t in test]
        assert min(gaps) > 3


def test_select_and_test_picks_the_persistent_winner():
    rng = np.random.default_rng(9)
    m = pd.DataFrame(rng.normal(0.0, 0.01, size=(300, 5)))
    m[2] = rng.normal(0.002, 0.008, 300)
    out = stats.select_and_test(m, groups=5, k_test=2)
    assert out["paths"] == math.comb(5, 2)
    assert out["oos_sharpe_p05"] > 0
    assert out["selection_stability"] >= 0.8


def test_cpcv_non_finite_returns_fail_instead_of_becoming_zero():
    matrix = pd.DataFrame(np.random.default_rng(9).normal(size=(120, 3)))
    matrix.iloc[3, 1] = np.inf
    out = stats.select_and_test(matrix)
    assert out["ok"] is False
    assert out["paths"] == 0


# ------------------------------------------------------------- bootstrap
def test_bootstrap_detects_a_positive_mean():
    rng = np.random.default_rng(2)
    out = stats.bootstrap_mean(pd.Series(rng.normal(0.01, 0.02, 300)))
    assert out["ci_low"] > 0
    assert out["p_value"] < 0.01


def test_bootstrap_of_zero_mean_is_not_significant():
    rng = np.random.default_rng(2)
    out = stats.bootstrap_mean(pd.Series(rng.normal(0.0, 0.02, 300)))
    assert out["p_value"] > 0.05


# ------------------------------------------------------- spec / trades
def test_spec_rejects_unknown_keys_and_bad_values():
    with pytest.raises(SpecError):
        parse_spec('{"pool":"limit_up","qty":100}')
    with pytest.raises(SpecError):
        parse_spec('{"pool":"crypto"}')
    with pytest.raises(SpecError):
        parse_spec('{"hold":99}')
    with pytest.raises(SpecError, match="0..60"):
        parse_spec({"hold": 61})
    # A 60-session hold is the point of the widened bound: the referee used to
    # stop at two days, so it could not test the horizon that survives cost.
    assert parse_spec({"hold": 60}).hold == 60
    assert parse_spec({"hold": 5}).name().endswith("h5c")
    assert parse_spec(None).pool == "limit_up"
    assert parse_spec(None).hold == 1


def test_panel_rejects_infinite_prices_instead_of_diluting_sample(store):
    daily = store.data["daily"]
    daily.loc[0, "close"] = float("inf")
    with pytest.raises(SpecError, match="close contains Inf"):
        load_panel(store, "20240101", "20240131")


def test_scan_pool_applies_threshold_and_liquidity(store):
    panel = load_panel(store, "20240101", "20240131")
    winners = scan_pool(panel, "winners", threshold=0.05, min_amount_cny=1e7)
    assert set(winners["pool"]) == {"winners"}
    assert (winners["ret"] >= 0.05).all()
    assert "600001.SH" in set(winners["ts_code"])

    losers = scan_pool(panel, "losers", threshold=0.03, min_amount_cny=1e7)
    assert (losers["ret"] <= -0.03).all()
    if not losers.empty:
        assert {"600001.SH"} <= set(losers["ts_code"])

    assert scan_pool(panel, "losers", threshold=0.03, min_amount_cny=1e15).empty


def test_same_day_round_trip_is_rejected():
    """A-share T+1: you cannot buy at the open and sell at that close."""
    with pytest.raises(SpecError):
        parse_spec({"pool": "broken_board", "entry": "next_open", "hold": 0})
    with pytest.raises(SpecError):
        parse_spec({"pool": "broken_board", "entry": "next_close", "hold": 0})
    with pytest.raises(SpecError):
        parse_spec({"pool": "limit_up", "hold": 0, "exit_at": "open"})


def test_entry_at_the_limit_is_not_tradable(store):
    panel = load_panel(store, "20240101", "20240131")
    pools = store.read("limit_pool_t0")
    spec = parse_spec({"pool": "limit_up", "boards": [1], "entry": "next_open", "hold": 1})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    assert list(trades["ts_code"]) == ["600001.SH", "600003.SH"]


def test_suspended_entry_rolls_to_the_first_actual_bar(store):
    limits = store.data["stk_limit"]
    limits.loc[limits["ts_code"] == "600003.SH", "down_limit"] = 0.0
    panel = load_panel(store, "20240101", "20240131")
    pools = store.read("limit_pool_t0")
    spec = parse_spec({"pool": "limit_up", "boards": [1], "hold": 1})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    trade = trades.set_index("ts_code").loc["600003.SH"]
    assert trade["entry_date"] == "20240104"
    assert trade["entry_price"] == pytest.approx(8.4)
    assert trade["exit_date"] == "20240105"
    assert trade["exit_price"] == pytest.approx(8.7)


def test_gross_and_cost_are_explicit(store):
    panel = load_panel(store, "20240101", "20240131")
    pools = store.read("limit_pool_t0")
    spec = parse_spec({"pool": "limit_up", "boards": [1], "hold": 1, "cost_bps": 10})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    row = trades.iloc[0]
    assert row["entry_price"] == pytest.approx(10.5)
    assert row["exit_price"] == pytest.approx(11.55)  # T+2 close, not T+1
    assert row["gross"] == pytest.approx(11.55 / 10.5 - 1)
    # Costs are the kernel's, not a flat 2 x cost_bps. Two actual entries on
    # this signal date split the CNY 5,000 account into CNY 2,500 tickets.
    instrument = fee_instrument("limit_up")
    fees = (CostModel().buy_fees(D("2500"), instrument)
            + CostModel().sell_fees(D("2500"), instrument))
    expected = float(fees) / 2500 + 2 * 10 / 10_000
    assert row["cost"] == pytest.approx(expected, rel=1e-6)
    assert row["net"] == pytest.approx(row["gross"] - row["cost"])


def test_open_exit_uses_the_next_open(store):
    panel = load_panel(store, "20240101", "20240131")
    pools = store.read("limit_pool_t0")
    spec = parse_spec({"pool": "limit_up", "boards": [1], "hold": 1, "exit_at": "open"})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    row = trades.iloc[0]
    assert row["exit_price"] == pytest.approx(11.0)
    rows = trades.attrs["daily_rows"]
    rows = rows[rows["trade_id"] == row["trade_id"]]
    # entry leg covers the entry-day close, the final leg only the overnight gap
    assert list(rows["kind"]) == ["entry", "open_exit"]
    assert rows[rows["kind"] == "open_exit"]["ret"].iloc[0] == pytest.approx(11.0 / 11.0 - 1)


def test_hold_one_adds_a_second_leg(store):
    panel = load_panel(store, "20240101", "20240131")
    pools = store.read("limit_pool_t0")
    spec = parse_spec({"pool": "limit_up", "boards": [1], "hold": 1})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    rows = trades.attrs["daily_rows"]
    assert sorted(set(rows["leg"])) == [0, 1]
    assert rows.groupby("trade_id").size().iloc[0] == 2
    # Cost is charged once, on the entry leg.
    assert rows[rows["leg"] == 1]["ret"].iloc[0] == pytest.approx(11.55 / 11.0 - 1)


def test_portfolio_series_uses_equal_weight(store):
    panel = load_panel(store, "20240101", "20240131")
    pools = store.read("limit_pool_t0")
    spec = parse_spec({"pool": "limit_up", "boards": [1], "hold": 1})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    series = portfolio_series(trades.attrs["daily_rows"])
    assert len(series) == 3
    cost = float(trades["cost"].iloc[0])
    assert series.iloc[0] == pytest.approx(11.0 / 10.5 - 1 - cost)
    deferred_entry = 8.6 / 8.4 - 1 - cost
    assert series.iloc[1] == pytest.approx((11.55 / 11.0 - 1 + deferred_entry) / 2)
    assert series.iloc[2] == pytest.approx(8.7 / 8.6 - 1)


def test_benchmark_matches_the_holding_window(store):
    """Each leg of the benchmark must match the leg the trade actually held."""
    panel = load_panel(store, "20240101", "20240131")
    pools = store.read("limit_pool_t0")
    spec = parse_spec({"pool": "limit_up", "boards": [1], "hold": 1, "exit_at": "open"})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    row = trades.iloc[0]
    intraday_0103 = 0.5 * (11.0 / 10.5 - 1)
    overnight_0104 = ((11.0 / 11.0 - 1) + (11.5 / 11.0 - 1) + (8.4 / 8.0 - 1)) / 3
    expected = (1 + intraday_0103) * (1 + overnight_0104) - 1
    assert row["benchmark"] == pytest.approx(expected, rel=1e-6)
    assert row["excess"] == pytest.approx((1 + row["net"]) / (1 + expected) - 1, rel=1e-6)


def test_selection_happens_before_the_fill_check(store_bb_blocked):
    """A name must not be skipped because of how it opened the next morning."""
    panel = load_panel(store_bb_blocked, "20240101", "20240131")
    pools = store_bb_blocked.read("limit_pool_t0")
    spec = parse_spec({"pool": "broken_board", "entry": "next_open", "hold": 1,
                       "max_per_day": 1, "rank_by": "close_to_limit", "rank_dir": "asc"})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    # 600001 is the deepest drop and also opens limit-up: the day is un-tradable,
    # and the trader could not have known that when picking it.
    assert trades.empty


def test_max_per_day_keeps_one_trade(store_two_tradable):
    panel = load_panel(store_two_tradable, "20240101", "20240131")
    pools = store_two_tradable.read("limit_pool_t0")
    spec = parse_spec({"pool": "limit_up", "boards": [1], "hold": 1, "max_per_day": 1})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    assert len(trades) == 1


def test_without_the_cap_all_actual_entries_trade(store_two_tradable):
    panel = load_panel(store_two_tradable, "20240101", "20240131")
    pools = store_two_tradable.read("limit_pool_t0")
    spec = parse_spec({"pool": "limit_up", "boards": [1], "hold": 1})
    trades = build_trades(panel, pools, spec, market_returns(panel))
    assert len(trades) == 3


def test_t0_pool_may_round_trip_but_stocks_may_not():
    t0 = parse_spec({"pool": "cb", "entry": "next_open", "hold": 0})
    assert t0.hold == 0 and t0.pool == "cb"
    with pytest.raises(SpecError):
        parse_spec({"pool": "winners", "entry": "next_open", "hold": 0})


def test_cb_pool_keeps_only_hard_down_days():
    frame = pd.DataFrame([
        {"ts_code": "113001.SH", "trade_date": "20260105", "close": 96.0, "close_p1": 100.0, "amount": 1e5},
        {"ts_code": "113002.SH", "trade_date": "20260105", "close": 99.0, "close_p1": 100.0, "amount": 1e5},
    ])
    from service.research.spec import cb_pool

    out = cb_pool(frame, threshold=0.03)
    assert list(out["code"]) == ["113001.SH"]
    assert out.iloc[0]["pct_chg"] == pytest.approx(-4.0)


def test_cb_trade_uses_the_same_day_close(monkeypatch):
    from service.research.spec import EventStudy, build_trades

    # The forward-bar check needs the following days to exist in the calendar.
    panel = pd.DataFrame([
        {"ts_code": "113001.SH", "trade_date": "20260105", "open": 96.0, "close": 95.0,
         "close_p1": 100.0, "date_f1": "20260106", "open_f1": 94.0, "close_f1": 96.0,
         "limit_f1": None, "date_f2": "20260107", "open_f2": 96.0, "close_f2": 96.5,
         "date_f3": "20260108", "open_f3": 96.5, "close_f3": 96.8},
        {"ts_code": "113001.SH", "trade_date": "20260106", "open": 94.0, "close": 96.0,
         "close_p1": 95.0, "date_f1": "20260107", "open_f1": 96.0, "close_f1": 96.5,
         "limit_f1": None, "date_f2": "20260108", "open_f2": 96.5, "close_f2": 96.8,
         "date_f3": None, "open_f3": None, "close_f3": None},
        {"ts_code": "113001.SH", "trade_date": "20260107", "open": 96.0, "close": 96.5,
         "close_p1": 96.0, "date_f1": "20260108", "open_f1": 96.5, "close_f1": 96.8,
         "limit_f1": None, "date_f2": None, "open_f2": None, "close_f2": None,
         "date_f3": None, "open_f3": None, "close_f3": None},
        {"ts_code": "113001.SH", "trade_date": "20260108", "open": 96.5, "close": 96.8,
         "close_p1": 96.5, "date_f1": None, "open_f1": None, "close_f1": None,
         "limit_f1": None, "date_f2": None, "open_f2": None, "close_f2": None,
         "date_f3": None, "open_f3": None, "close_f3": None},
    ])
    pools = pd.DataFrame([{"ts_code": "113001.SH", "trade_date": "20260105", "pool": "cb",
                           "boards": 0, "name": "", "amount": 1e5, "pct_chg": -5.0,
                           "limit_price": None}])
    spec = EventStudy(pool="cb", entry="next_open", hold=0, cost_bps=10)
    trades = build_trades(panel, pools, spec, None)
    row = trades.iloc[0]
    assert row["entry_price"] == pytest.approx(94.0)
    assert row["exit_price"] == pytest.approx(96.0)  # same-day close is legal for T+0
    rows = trades.attrs["daily_rows"]
    assert list(rows["kind"]) == ["entry"]


def test_cb_link_pool_maps_a_stock_event_to_its_bond():
    from service.research.spec import cb_link_pool

    class Store:
        def read(self, dataset, *args, **kwargs):
            if dataset == "cb_basic":
                return pd.DataFrame([{"ts_code": "113001.SH", "stk_code": "600001.SH",
                                      "list_date": "20200101", "delist_date": None,
                                      "conv_price": 10.0}])
            if dataset == "limit_pool_t0":
                return pd.DataFrame([{"code": "600001.SH", "trade_date": "20260105",
                                      "pool": "limit_up", "boards": 1, "pct_chg": 10.0,
                                      "close": 11.0, "name": "甲股份"}])
            return pd.DataFrame()

    panel = pd.DataFrame([{"ts_code": "113001.SH", "trade_date": "20260105",
                           "close": 120.0, "close_p1": 118.0}])
    out = cb_link_pool(panel, Store(), "limit_up", ())
    row = out.iloc[0]
    assert row["ts_code"] == "113001.SH"
    assert row["cb_ret"] == pytest.approx(120 / 118 - 1)
    # parity = 100 / 10 * 11 = 110 -> premium = 120 / 110 - 1
    assert row["premium"] == pytest.approx(120 / 110 - 1)


def test_cb_link_is_t0_and_validates_the_link():
    spec = parse_spec({"pool": "cb_link", "link_pool": "limit_down", "hold": 0})
    assert spec.lot == 10 and spec.link_pool == "limit_down"
    with pytest.raises(SpecError):
        parse_spec({"pool": "cb_link", "link_pool": "whatever"})


def test_block_bootstrap_is_stricter_than_iid_when_trades_cluster():
    """Twenty identical trades a day are one observation, not twenty."""
    rng = np.random.default_rng(1)
    day = rng.normal(0.001, 0.05, 120)
    iid = stats.bootstrap_mean(pd.Series(np.repeat(day, 20)))
    block = stats.block_bootstrap_mean(pd.Series(day), block=5)
    # The interval is the tell: iid treats 2,400 correlated trades as 2,400 draws
    # and reports an interval roughly sqrt(20) too narrow.
    assert (block["ci_high"] - block["ci_low"]) > 3 * (iid["ci_high"] - iid["ci_low"])
    assert block["n"] == 120
    assert block["ci_low"] <= block["mean"] <= block["ci_high"]


def test_block_bootstrap_null_has_no_edge_weighting():
    """Non-wrapping blocks over-weight interior rows and shift the H0 centre."""
    values = np.zeros(11)
    values[0] = 10.0
    out = stats.block_bootstrap_mean(pd.Series(values), block=2, n_boot=4000)
    assert out["block_scheme"] == "circular"
    assert abs(out["null_center"]) < 0.03


def test_a_bootstrap_p_value_is_never_zero():
    """2000 resamples cannot resolve better than 1/2001, so 0.0 is not a number
    this procedure can produce — and sorted into a BH family it becomes the one
    discovery the procedure reports."""
    every_resample_positive = np.full(60, 0.02)
    boot = stats.bootstrap_mean(every_resample_positive, n_boot=500)
    assert boot["p_value"] == pytest.approx(1 / 501, abs=1e-6)
    assert boot["p_value"] > 0

    blocks = stats.block_bootstrap_mean(
        pd.Series(every_resample_positive), block=5, n_boot=400)
    assert blocks["p_value"] == pytest.approx(1 / 401, abs=1e-6)
    assert blocks["p_value"] > 0


def test_missing_returns_are_dropped_not_fabricated_as_flat_sessions():
    clean = stats.clean_returns([0.01, np.nan, np.inf, -np.inf, -0.02])
    assert list(clean) == [0.01, -0.02]
    assert stats.metrics([0.01, np.nan, np.inf])["n"] == 1


def test_drawdown_includes_the_initial_unit_of_wealth():
    assert stats.max_drawdown(pd.Series([-0.10, 0.05])) == pytest.approx(-0.10)


def test_pbo_uses_average_ranks_for_ties():
    matrix = pd.DataFrame(np.zeros((100, 4)))
    out = stats.cscv_pbo(matrix, blocks=4)
    assert out["ok"] is True
    assert out["pbo"] == 0.0
    assert out["mean_logit"] == 0.0


def test_pbo_non_finite_or_short_samples_fail_closed():
    matrix = pd.DataFrame(np.random.default_rng(4).normal(size=(100, 3)))
    matrix.iloc[0, 0] = np.inf
    out = stats.cscv_pbo(matrix, blocks=8)
    assert out["ok"] is False and out["pbo"] is None
    assert stats.cscv_pbo(matrix.iloc[:10])["pbo"] is None


def test_cpcv_label_spans_purge_overlapping_training_labels():
    n = 120
    spans = np.arange(n) + 1
    spans[18] = 45  # a long training label reaches into the next group
    folds = stats.cpcv_folds(n, groups=6, k_test=2, label_spans=spans)
    assert folds
    for train, test in folds:
        assert 18 not in train or not any(18 <= t < 45 for t in test)
        for i in train:
            assert not any(i <= t < spans[i] or t <= i < spans[t] for t in test)
