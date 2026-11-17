"""A sentence is either testable or it is not; both answers are results.

The compiler is the line between the model's prose and the referee's arithmetic.
Its most important output is the refusal: a claim that cannot be turned into a
closed-vocabulary spec was never testable, and a desk that cannot say so will
quietly test something else on the model's behalf.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from service.research import claim as claim_mod


def test_the_measurement_carries_no_verdict_of_its_own():
    """Reading the number is the caller's job; the module must not do it."""
    state = make_state(edge=0.05)
    c = claim_mod.compile_claim("跌多了会反弹")
    out = claim_mod.evaluate(c, state)
    assert "verdict" not in out and "reasons" not in out
    assert "evidence" in out and "policy" in out
    assert out["policy"]["name"] == "desk/v1"


def test_a_dip_buying_claim_becomes_a_state_bucket():
    c = claim_mod.compile_claim("跌多了会反弹")
    assert isinstance(c, claim_mod.Claim)
    assert c.kind == "bucket"
    assert c.spec["bucket"] == "low|low"
    assert c.direction == "up"
    assert c.horizon == 20
    assert any("跌多了" in a for a in c.assumptions), "翻译必须把假设写出来"


def test_narrow_rally_maps_to_low_breadth_and_high_return():
    c = claim_mod.compile_claim("缩量上涨之后会回调")
    assert c.kind == "bucket"
    assert c.spec["bucket"] == "low|high"
    assert c.direction == "down"


def test_a_horizon_word_sets_the_forward_window():
    c = claim_mod.compile_claim("跌多了，一周内会反弹")
    assert c.horizon == 5
    assert any("一周" in a for a in c.assumptions)


def test_an_event_claim_becomes_a_research_spec():
    c = claim_mod.compile_claim("炸板股次日买入持有一天")
    assert c.kind == "event_study"
    assert c.spec["pool"] == "broken_board"
    assert c.spec["entry"] == "next_open"
    assert "hold" in c.spec


def test_a_limit_up_claim_carries_the_board_count():
    c = claim_mod.compile_claim("首板涨停次日买入")
    assert c.kind == "event_study"
    assert c.spec["pool"] == "limit_up"
    assert c.spec["boards"] == [1]


def test_a_rule_claim_becomes_an_overlay():
    c = claim_mod.compile_claim("加 5% 止损会更好")
    assert c.kind == "overlay"
    assert c.spec["rule"] == "stop_loss"


def test_an_unconditional_forecast_is_refused_not_forced_into_a_bucket():
    """"市场明天会涨" has no condition in it. Picking a bucket would invent one."""
    r = claim_mod.compile_claim("市场明天会涨")
    assert isinstance(r, claim_mod.Refusal)
    assert "无条件预测" in r.reason
    assert any("memo" in need for need in r.needs)


def test_nonsense_is_refused_with_a_list_of_what_is_missing():
    r = claim_mod.compile_claim("今天天气不错")
    assert isinstance(r, claim_mod.Refusal)
    assert r.needs and all(isinstance(n, str) for n in r.needs)


def test_empty_input_is_a_refusal_not_a_crash():
    assert isinstance(claim_mod.compile_claim("   "), claim_mod.Refusal)


# ------------------------------------------------------------------ judging
def make_state(edge: float, days: int = 200, run: int = 5) -> pd.DataFrame:
    index = pd.Index([f"2024{d:04d}" for d in range(1, days + 1)])
    bucket = []
    for i in range(0, days, run):
        bucket += ["low|low"] * run if (i // run) % 2 == 0 else ["mid|mid"] * run
    rng = np.random.default_rng(3)
    fwd = np.where(np.array(bucket) == "low|low",
                   edge + rng.normal(0, 0.01, days), rng.normal(0, 0.01, days))
    return pd.DataFrame({"bucket": bucket[:days],
                         "fwd_5d": fwd * 0.25,
                         "fwd_20d": fwd}, index=index)


def test_bucket_block_bootstrap_does_not_overweight_interior_days():
    values = np.zeros(11)
    values[0] = 10.0
    means = claim_mod._block_means(values, 2, 4000, np.random.default_rng(7))
    assert abs(float(means.mean()) - float(values.mean())) < 0.03


def test_a_real_edge_shows_up_in_the_evidence():
    state = make_state(edge=0.05)
    c = claim_mod.compile_claim("跌多了会反弹")
    out = claim_mod.evaluate(c, state)
    e = out["evidence"]
    # 桶内日子也算进基准，差异被稀释；0.05 的效应读出来约 0.025
    assert e["diff"] > 0.02
    assert e["p_two_sided"] < 0.01
    assert e["episodes"] >= 10
    assert claim_mod.threshold_reading(e, out["policy"])["reading"] == "consistent"


def test_no_edge_labels_as_indistinguishable():
    state = make_state(edge=0.0)
    c = claim_mod.compile_claim("跌多了会反弹")
    out = claim_mod.evaluate(c, state)
    assert claim_mod.threshold_reading(out["evidence"], out["policy"])["reading"] == (
        "indistinguishable")


def test_an_edge_in_the_wrong_direction_labels_as_contradicted():
    state = make_state(edge=-0.05)
    c = claim_mod.compile_claim("跌多了会反弹")
    out = claim_mod.evaluate(c, state)
    assert out["evidence"]["sign"] == "contrary"
    assert claim_mod.threshold_reading(out["evidence"], out["policy"])["reading"] == (
        "contradicted")


def test_too_few_episodes_refuses_to_judge():
    state = make_state(edge=0.05, days=40, run=10)   # 4 episodes
    c = claim_mod.compile_claim("跌多了会反弹")
    out = claim_mod.evaluate(c, state)
    assert out["ok"] is False
    assert "段数" in out["error"]


def test_an_event_claim_is_compiled_but_not_run():
    c = claim_mod.compile_claim("炸板股次日买入持有一天")
    out = claim_mod.evaluate(c)
    assert out["ok"] is True
    assert out["status"] == "not_run"
    assert "run_research" in out["next"]


def test_refusal_rate_is_the_first_number_worth_measuring():
    report = claim_mod.refusal_rate([
        "跌多了会反弹", "市场明天会涨", "炸板股次日买入", "今天天气不错"])
    assert report["total"] == 4
    assert report["refused"] == 2
    assert report["refusal_rate"] == pytest.approx(0.5)
    assert report["by_kind"] == {"bucket": 1, "event_study": 1}


# ------------------------------------------------------- overlay claims
def _book(paths: list[list[float]], cost: float = 0.004) -> pd.DataFrame:
    """A trade table with legs, small enough to reason about by hand."""
    days = [f"202401{d:02d}" for d in range(2, 12)]
    trades, legs = [], []
    for tid, closes in enumerate(paths):
        entry, n = closes[0], len(closes)
        legs.append({"trade_id": tid, "date": days[0], "leg": 0,
                     "ret": closes[0] / entry - 1 - cost, "kind": "entry"})
        for k in range(1, n):
            legs.append({"trade_id": tid, "date": days[k], "leg": k,
                         "ret": closes[k] / closes[k - 1] - 1, "kind": "day"})
        gross = closes[-1] / entry - 1
        trades.append({"trade_id": tid, "entry_date": days[0],
                       "exit_date": days[n - 1], "entry_price": entry,
                       "exit_price": closes[-1], "cost": cost,
                       "gross": gross, "net": gross - cost})
    frame = pd.DataFrame(trades)
    frame.attrs["daily_rows"] = pd.DataFrame(legs)
    return frame


def test_a_stop_claim_without_a_level_is_refused():
    """3% and 12% are different hypotheses; picking one would answer a question
    nobody asked."""
    r = claim_mod.compile_claim("加个止损会更好")
    assert isinstance(r, claim_mod.Refusal)
    assert any("多少" in n for n in r.needs)


def test_the_level_is_parsed_out_of_the_sentence():
    c = claim_mod.compile_claim("加 5% 止损会更好")
    assert c.kind == "overlay"
    assert c.spec["rule"] == "stop_loss"
    assert c.spec["threshold"] == pytest.approx(0.05)
    assert any("5%" in a or "0.05" in a for a in c.assumptions)


def test_a_filter_claim_is_refused_until_it_names_a_condition():
    r = claim_mod.compile_claim("只做会更好")
    assert isinstance(r, claim_mod.Refusal)
    assert any("条件" in n for n in r.needs)


def test_an_overlay_without_a_book_says_exactly_what_it_needs():
    c = claim_mod.compile_claim("加 5% 止损会更好")
    out = claim_mod.evaluate(c)
    assert out["status"] == "not_run"
    assert "daily_rows" in out["next"]


def test_an_overlay_measures_two_arms_and_still_no_verdict():
    c = claim_mod.compile_claim("加 5% 止损会更好")
    book = _book([[10.0, 9.0, 8.0, 7.5], [10.0, 10.1, 10.0, 10.2]])
    out = claim_mod.evaluate(c, trades=book)

    assert out["ok"] is True
    assert "verdict" not in out and "reasons" not in out
    e = out["evidence"]
    assert e["n_trades"] == 2 and e["n_fired"] == 1
    assert e["sign"] == "agrees"
    assert e["paired_diff"]["n"] == 2
    assert e["affected_diff"]["n"] == 1
    assert out["policy"]["name"] == "desk/v1"


def test_a_missing_leg_table_is_reported_not_raised():
    c = claim_mod.compile_claim("加 5% 止损会更好")
    bare = pd.DataFrame([{"trade_id": 0, "entry_price": 10.0, "cost": 0.004,
                          "gross": -0.2, "net": -0.204}])
    out = claim_mod.evaluate(c, trades=bare)
    assert out["ok"] is False
    assert "daily_rows" in out["error"]


def test_a_holding_period_claim_is_an_overlay_with_days():
    c = claim_mod.compile_claim("持有 3 天会更好")
    assert c.kind == "overlay"
    assert c.spec["rule"] == "time_exit"
    assert c.spec["days"] == 3


def test_a_pool_that_also_names_a_holding_period_stays_an_event_study():
    """「炸板股持有 2 天」是换一个持有期的事件研究，不是套在别人账本上的规则。"""
    c = claim_mod.compile_claim("炸板股持有 2 天会更好")
    assert c.kind == "event_study"
    assert c.spec["pool"] == "broken_board"


# ------------------------------------------------------- what to book
def test_the_booked_p_value_is_the_tail_the_claim_asserted():
    """「跌多了会反弹」和「跌多了会回调」在同一份数据上是彼此的镜像。

    记账时用的 p 必须是主张方向那一侧的尾，否则同一批数据既能支持一个主张、
    又能以同一组数字支持它的反面。
    """
    state = make_state(edge=0.05)
    up = claim_mod.compile_claim("跌多了会反弹")
    down = claim_mod.compile_claim("跌多了会回调")
    look_up = claim_mod.as_look(up, claim_mod.evaluate(up, state))
    look_down = claim_mod.as_look(down, claim_mod.evaluate(down, state))

    assert look_up["kind"] == "bucket" and look_down["kind"] == "bucket"
    assert look_up["p_value"] == pytest.approx(1 - look_down["p_value"], abs=1e-6)
    assert look_up["p_value"] < 0.05
    assert look_down["p_value"] > 0.95


def test_an_overlay_look_carries_the_paired_p_and_the_counts():
    c = claim_mod.compile_claim("加 5% 止损会更好")
    book = _book([[10.0, 9.0, 8.0, 7.5], [10.0, 10.1, 10.0, 10.2]])
    evaluation = claim_mod.evaluate(c, trades=book)
    look = claim_mod.as_look(c, evaluation)

    assert look["kind"] == "overlay"
    assert look["rule"] == "stop_loss" and look["params"] == {"threshold": 0.05}
    assert look["p_value"] == evaluation["evidence"]["paired_diff"]["p_value"]
    assert look["n_trades"] == 2 and look["n_fired"] == 1
    assert 0 < look["p_value"] <= 1


def test_nothing_is_booked_when_nothing_was_measured():
    c = claim_mod.compile_claim("加 5% 止损会更好")
    assert claim_mod.as_look(c, claim_mod.evaluate(c)) is None
    event = claim_mod.compile_claim("炸板股次日买入持有一天")
    assert claim_mod.as_look(event, claim_mod.evaluate(event)) is None


def test_two_sided_claim_is_not_automatically_contradicted():
    state = make_state(edge=0.05)
    claim = claim_mod.compile_claim("跌多了会反弹或回调")
    assert claim.direction == "two_sided"
    evaluated = claim_mod.evaluate(claim, state)
    evidence = evaluated["evidence"]
    assert evidence["sign"] == "nonzero"
    assert claim_mod.threshold_reading(evidence, evaluated["policy"])["reading"] == "consistent"
