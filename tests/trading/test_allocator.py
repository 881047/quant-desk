"""Capital allocation: boring on purpose, explainable in one line per strategy."""

from __future__ import annotations

import pytest

from service.trading.allocator import StrategyStats, _factor, allocate, stats_from_cases
from service.trading.strategy import REGISTRY, StrategyMeta


def test_an_ineligible_strategy_gets_exactly_zero(monkeypatch):
    # A real registry no longer holds a regime-restricted strategy (the only one
    # was retired by P21), so the rule is tested on a temporary one.
    monkeypatch.setitem(REGISTRY, "regime_only", StrategyMeta(
        key="regime_only", name="只在 risk_on", kind="signal",
        regimes=("risk_on",), status="live", max_weight=0.2))
    plan = allocate("stressed", book="paper")
    assert plan["weights"].get("regime_only") is None
    assert "不在允许区间" in plan["skipped"]["regime_only"]


def test_the_core_book_is_funded_separately():
    plan = allocate("neutral", book="paper")
    assert "core_etf" not in plan["weights"]
    assert "core book" in plan["skipped"]["core_etf"]


def test_an_unproven_strategy_is_halved_and_capped():
    plan = allocate("stressed", book="paper")
    assert plan["weights"]["llm"] <= REGISTRY["llm"].max_weight + 1e-9
    assert any("未证明" in r for r in plan["reasons"]["llm"])


def test_a_retired_strategy_gets_no_capital_at_all():
    plan = allocate("stressed", book="paper")
    assert "cb_link_down" not in plan["weights"]
    assert plan["skipped"]["cb_link_down"] == "status=retired"


def test_a_discredited_strategy_is_cut_harder_than_an_unproven_one():
    meta = REGISTRY["llm"]
    fresh, _ = _factor(meta, StrategyStats())
    proven, _ = _factor(meta, StrategyStats(
        n=40, mean_excess=0.01, std_excess=0.01, max_drawdown=-0.01))
    discredited, _ = _factor(meta, StrategyStats(
        n=30, mean_excess=-0.01, std_excess=0.01, max_drawdown=-0.02))
    assert discredited < fresh < proven


def test_a_proven_strategy_is_given_more_room():
    proven = allocate("neutral", {"llm": StrategyStats(
        n=40, mean_excess=0.01, std_excess=0.01, max_drawdown=-0.01)},
        book="paper")
    fresh = allocate("neutral", {}, book="paper")
    assert proven["weights"]["llm"] >= fresh["weights"]["llm"]


def test_drawdown_throttles_a_strategy():
    meta = REGISTRY["llm"]
    healthy, _ = _factor(meta, StrategyStats(
        n=30, mean_excess=0.005, std_excess=0.01, max_drawdown=-0.01))
    drawn, why = _factor(meta, StrategyStats(
        n=30, mean_excess=0.005, std_excess=0.01, max_drawdown=-0.12))
    assert drawn < healthy
    assert any("回撤" in r for r in why)


def test_stats_come_from_tagged_cases():
    cases = [
        {"strategy": "llm", "pnl_pct": 0.02},
        {"strategy": "llm", "pnl_pct": -0.01},
        {"strategy": "cb_link_down", "pnl_pct": 0.005},
        {"pnl_pct": 0.01},  # untagged defaults to llm
    ]
    stats = stats_from_cases(cases)
    assert stats["llm"].n == 0
    assert stats["llm"].raw_closed == 3
    assert stats["llm"].unscored == 3
    assert stats["cb_link_down"].raw_closed == 1
    assert stats["llm"].max_drawdown <= 0.0


def test_allocation_never_exceeds_one():
    plan = allocate("neutral", {}, book="paper")
    assert sum(plan["weights"].values()) + plan["cash_weight"] == pytest.approx(1.0)


def test_raw_pnl_is_not_relabelled_as_excess_or_promoted():
    cases = [{"strategy": "llm", "pnl_pct": 0.01} for _ in range(30)]
    stats = stats_from_cases(cases)["llm"]

    assert stats.mean_raw == pytest.approx(0.01)
    assert stats.mean_excess is None
    assert stats.std_excess is None
    assert not stats.proven and not stats.discredited
    factor, reasons = _factor(REGISTRY["llm"], stats)
    assert factor == pytest.approx(0.5)
    assert stats.raw_closed == 30 and stats.unscored == 30
    assert any("缺少同窗口超额样本" in reason for reason in reasons)


def test_existing_same_window_excess_takes_precedence_over_raw_pnl():
    cases = [{"strategy": "llm", "pnl_pct": 0.50,
              "excess": -0.01 - (i % 2) * 0.01} for i in range(30)]
    stats = stats_from_cases(cases)["llm"]

    assert stats.mean_raw == pytest.approx(0.50)
    assert stats.mean_excess == pytest.approx(-0.015)
    assert stats.discredited and not stats.proven


def test_excess_sample_count_excludes_cases_without_a_benchmark():
    cases = ([{"strategy": "llm", "pnl_pct": 0.03, "excess_pct": 0.01}] * 2
             + [{"strategy": "llm", "pnl_pct": 0.03}] * 28)
    stats = stats_from_cases(cases)["llm"]

    assert stats.n == 2
    assert stats.raw_closed == 30 and stats.unscored == 28
    assert stats.basis == "arena_same_window_excess"
    assert stats.mean_excess == pytest.approx(0.01)
    assert stats.mean_raw == pytest.approx(0.03)


def test_arena_decisions_join_by_case_id_and_leave_unmatched_cases_raw():
    cases = [
        {"case_id": "c1", "strategy": "llm", "pnl_pct": 0.03},
        {"case_id": "c2", "strategy": "llm", "pnl_pct": 0.04},
        {"case_id": "c3", "strategy": "llm", "pnl_pct": 0.05},
    ]
    decisions = [
        {"case_id": "c1", "excess": 0.01, "market": 0.02, "directed_by": "agent"},
        {"case_id": "c3", "excess": 0.50, "market": 0.00, "directed_by": "user"},
    ]
    stats = stats_from_cases(cases, decisions)["llm"]

    assert stats.n == 1
    assert stats.mean_excess == pytest.approx(0.01)
    assert stats.mean_raw == pytest.approx(0.04)
    assert stats.raw_closed == 3 and stats.unscored == 2
    assert stats.basis == "arena_same_window_excess"


def test_no_cash_mode_still_gives_the_capped_residual_an_owner():
    plan = allocate("neutral", {}, book="paper", include_cash=False)

    assert plan["cash_weight"] == 0.0
    assert plan["residual_weight"] > 0.0
    assert plan["residual_owner"] == "unallocated"
    assert sum(plan["weights"].values()) + plan["residual_weight"] == pytest.approx(1.0)


def test_cap_redistribution_runs_past_the_old_five_iteration_limit(monkeypatch):
    from service.trading import allocator as allocator_mod

    raw = [0.22896447175510432, 0.3997302596532619, 0.02394436521296651,
           0.11439004332248981, 0.008929168419031224, 0.10717660758116662,
           0.1168650840559796]
    caps = [0.16553440198598357, 0.40928267634537535, 0.03433771017335607,
            0.1333395976682598, 0.011399552290236956, 0.11836323394815361,
            0.12653372431332502]
    keys = [f"cap_{i}" for i in range(7)]
    factors = dict(zip(keys, raw))
    for key, cap in zip(keys, caps):
        monkeypatch.setitem(REGISTRY, key, StrategyMeta(
            key=key, name=key, kind="signal", regimes=("neutral",),
            status="live", max_weight=cap, book="cap-test"))
    monkeypatch.setattr(allocator_mod, "_factor",
                        lambda meta, stats: (factors[meta.key], []))

    plan = allocate("neutral", book="cap-test")

    assert len(plan["weights"]) == 7
    for key, cap in zip(keys, caps):
        assert plan["weights"][key] <= cap + 0.0001
    assert sum(plan["weights"].values()) + plan["residual_weight"] == pytest.approx(
        1.0, abs=0.001)
