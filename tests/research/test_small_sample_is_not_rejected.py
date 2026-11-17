"""样本不够时，「没看清」不能写成「看清了是坏的」。

裁判的合成规则是 `rejected > testing > underpowered > promoted`——最坏的门决定判词。
这条规则本身是对的（保守），但它有一个致命的相互作用：

    样本门失败 → testing = True
    紧接着 mean_excess <= 0 → rejected = True
    rejected 优先 → 输出 rejected

于是一笔只有 5 个样本、恰好这 5 笔都亏的假设，判词是 `rejected`（"这个策略不行"），
而不是 `testing`（"样本不够"）。reasons 里"sample too small: 5 < 100"排在后面，
第一眼看到的是 rejected。而这个判词会被写进 `hypotheses.status` 冻结下来，
再被 `knowledge.ledger` 发给模型当"已测过、不行的结构"。

另一半原因是 `bootstrap_mean` 在少于 10 个观测时返回 `ci=[0,0]`、`p=1.0` 这些
哨兵值。它们是"算不出来"，但会被读成"零宽区间（确定）"和"p=1（完全没效应）"。
`arena.py:228` 早就不打印这种区间（"a zero-width interval reads as certainty"），
裁判这边一直没跟上。
"""

from __future__ import annotations

from service.research import runner


def _best(n: int, mean_excess: float, p_value=None) -> dict:
    return {"trades": {"n": n, "mean_excess": mean_excess, "p_value": p_value,
                       "ci_low": None, "ci_high": None, "std": 0.0,
                       "mean": mean_excess, "days": 0, "p_basis": "none"},
            "metrics": {"sharpe": 0.5, "n": n}}


def _verdict(best, *, power=None, account=None):
    return runner._verdict(
        best,
        {"dsr": 0.0, "trials": 1},
        # desk/v2 tightened pbo_max 0.50 -> 0.35. This fixture is about the
        # synthesis of sample / p-value gates, not PBO, so keep PBO clearly
        # passing instead of letting it become the dominant failing gate.
        {"pbo": 0.1, "ok": True},
        {"oos_sharpe_p05": -1.0, "positive": 0},
        # Sharpe 用正数：这个桩要测的是合成规则，不是成本压测自己会不会失败。
        {runner.PRIMARY_STRESS: {"metrics": {"sharpe": 0.5}}},
        {"implementable": True, "median_signals_per_day": 1.0,
         "median_price": 10.0, "capital_one_lot_each": 1000.0,
         "max_names_affordable": 1, "account_equity": 5000.0},
        power=power,
        account=account,
    )


def test_five_losing_trades_are_underpowered_not_rejected():
    """5 笔全亏不能判 rejected。均值是噪声，用它否掉一个策略是把噪声当证据。"""
    reading, reasons, record = _verdict(_best(5, -0.02))

    assert reading == "testing", reading
    assert any("sample too small" in r for r in reasons)

    gates = {g["gate"]: g for g in record["gates"]}
    assert gates["sample_trades"]["passed"] is False
    # 收益类门记的是"没测"，不是"没过"
    for name in ("mean_excess", "p_value", "pbo", "cost_stress_sharpe"):
        assert gates[name]["passed"] is None, name
        assert "not_judged" in gates[name], name
    assert "没测" in record["verdict_from"]


def test_enough_trades_still_get_judged():
    """反例必须能成立。否则上一条只是"永远返回 testing"的另一种坏。"""
    reading, reasons, record = _verdict(_best(500, -0.02, p_value=0.01))

    assert reading == "rejected"
    gates = {g["gate"]: g for g in record["gates"]}
    assert gates["mean_excess"]["passed"] is False
    assert "not_judged" not in gates["mean_excess"]


def test_a_missing_p_value_is_not_a_failed_threshold():
    """p 算不出来时不能拿它去比阈值：比 None 会崩，当 1.0 会读成"不显著"。"""
    reading, reasons, record = _verdict(_best(500, 0.01, p_value=None))

    assert reading == "testing", reading
    gates = {g["gate"]: g for g in record["gates"]}
    assert gates["p_value"]["passed"] is None
    assert gates["p_value"]["not_computable"] is True
    assert "not computable" in " ".join(reasons)


def test_trade_stats_refuses_to_print_a_degenerate_interval():
    """少于 10 笔时 bootstrap 返回 ci=[0,0]、p=1.0，那些是哨兵值不是测量结果。"""
    import pandas as pd

    frame = pd.DataFrame({"net": [0.01, -0.02, 0.03], "excess": [0.005, -0.01, 0.02]})
    out = runner._trade_stats(frame)

    assert out["n"] == 3
    assert out["p_value"] is None
    assert out["ci_low"] is None and out["ci_high"] is None
    assert "算不出来" in out["interval_note"]


def test_no_trades_at_all_carries_no_interval_either():
    import pandas as pd

    out = runner._trade_stats(pd.DataFrame({"net": [], "excess": []}))
    assert out["n"] == 0
    assert out["p_value"] is None and out["ci_low"] is None


def test_shadow_gate_does_not_crash_on_a_missing_p_value():
    """`None > 0.05` 会崩；把它当 1.0 又会说成"不显著"。只能是"测不出来"。"""
    advance, blockers = runner.shadow_gate(
        _best(500, 0.01, p_value=None),
        {"dsr": 0.95}, {"pbo": 0.1, "ok": True},
        {"oos_sharpe_p05": 0.3, "positive": 4},
        {runner.PRIMARY_STRESS: {"metrics": {"sharpe": 0.5}}},
        {"implementable": True},
    )

    assert advance is False
    assert "p-unavailable" in blockers
