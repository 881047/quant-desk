"""情绪状态转移：分段计数与条件分布。

这套逻辑回答的是"今天这种日子，历史上之后几天怎么走"。它最容易被误读的地方
不是指标算错，而是**把连续的天数当成独立的样本**——546 个"强情绪"交易日可能
只来自 8 段事件。所以每条断言都盯着 n_days / n_episodes 这一对。
"""

from service.trading.cycle import _episodes, daily_metrics, transition


def test_episodes_splits_on_long_gaps():
    days = ["20261102", "20261103", "20261104", "20261120", "20261121"]

    assert _episodes(days) == 2


def test_episodes_of_nothing_is_zero():
    assert _episodes([]) == 0


def test_daily_metrics_break_rate_matches_its_definition(desk):
    frame = daily_metrics(desk.data, "20261026", "20261102")

    assert not frame.empty
    row = frame[frame["trade_date"] == "20261102"].iloc[0]
    expected = row["broken"] / (row["limit_up"] + row["broken"])
    assert abs(row["break_rate"] - expected) < 1e-9


def test_transition_reports_episodes_not_only_days(desk):
    out = transition(desk, "20261102")

    assert out["ok"] is True
    cohort = out["cohort"]
    assert cohort["n_days"] > 0
    # 段数必须小于等于天数，且为正：一段持续行情会被算成几十天，
    # 只有段数接近真实证据量，所以两个都要给。
    assert 0 < cohort["n_episodes"] <= cohort["n_days"]


def test_transition_forward_includes_the_sample_size(desk):
    out = transition(desk, "20261102")

    assert out["ok"] is True
    for key, block in out["forward"].items():
        assert key.endswith("d")
        assert "n" in block, f"{key} 缺样本量，读者无法判断这个均值可不可信"


def test_transition_on_a_day_without_a_pool_is_an_error(desk):
    out = transition(desk, "20991231")

    assert out["ok"] is False
    assert "error" in out
