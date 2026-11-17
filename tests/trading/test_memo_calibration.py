"""Brier 分必须带参照，而且参照只有一种算法是对的。

`calibration()` 原来只给一个数。11-03 补了参照，但那版有两处错：

1. 基准取常数 0.5。文献里标准参照是**气候基准** `B_c = s(1-s)`（s 是样本基础率），
   0.5 只是 s=0.5 的特例，而且是最弱的一条。本账 s=0.5714，`B_c=0.2449` 而不是 0.25。
2. 更严重：字段叫 `skill_vs_constant`，算的却是**差值** `B_ref − B`。技巧分的定义是
   `BSS = 1 − B/B_ref`。同一组数据，差值是 −0.05（"略有不如"），BSS 是 −0.23
   （"比什么都不知道差 23%"）。名字承诺了后者，给的是前者——正是这一轮在清的那类毛病。

所以这里钉三件事：BSS 用 `1 − B/B_c` 算、基准用基础率而不是 0.5、样本全同一边时
BSS 是 null（没有定义）而不是 0（那会被读成"和不知道一样好"）。
"""

from __future__ import annotations

import pytest


def _score_all(desk, pairs: list[tuple[float, int]]) -> None:
    memo_id = desk.paper.add_memo("t", "x", predictions=[
        {"claim": f"c{i}", "kind": "market_up", "horizon": 1, "probability": p}
        for i, (p, _y) in enumerate(pairs)
    ])
    desk.paper.close_memo(memo_id, {"predictions": [
        {"claim": f"c{i}", "kind": "market_up", "probability": p, "outcome": float(y)}
        for i, (p, y) in enumerate(pairs)
    ]})


def test_bss_is_one_minus_b_over_bc_not_a_difference(desk):
    """(0.8→跌, 0.2→涨)：B=0.64，s=0.5，B_c=0.25。

    BSS 应为 1 − 0.64/0.25 = −1.56。若误算成差值（0.25 − 0.64 = −0.39），
    这条断言会失败——两个数差四倍，读者对「我有多差」的印象完全不同。
    """
    _score_all(desk, [(0.8, 0), (0.2, 1)])

    out = desk.paper.calibration()

    assert out["brier"] == pytest.approx(0.64)
    assert out["base_rate"] == pytest.approx(0.5)
    assert out["brier_climatology"] == pytest.approx(0.25)
    assert out["bss"] == pytest.approx(-1.56)
    assert "skill_vs_constant" not in out, "差值冒充技巧分，已删"


def test_the_baseline_is_the_base_rate_not_half(desk):
    """样本三涨一跌：s=0.75，B_c=0.1875，不是 0.25。

    押 0.5 在这份不均衡样本上不是"不知道"，是"明确地押错方向"。用 0.25 当参照
    会高估预测者——这正是文献说 0.5 是最弱基准的原因。
    """
    _score_all(desk, [(0.7, 1), (0.7, 1), (0.7, 1), (0.3, 0)])

    out = desk.paper.calibration()

    assert out["base_rate"] == pytest.approx(0.75)
    assert out["brier_climatology"] == pytest.approx(0.1875)
    assert out["brier_climatology"] != pytest.approx(0.25)
    # B = (0.09*3 + 0.09)/4 = 0.09；BSS = 1 - 0.09/0.1875 = 0.52
    assert out["brier"] == pytest.approx(0.09)
    assert out["bss"] == pytest.approx(0.52)


def test_a_sharp_and_correct_set_beats_climatology(desk):
    """反例：全押对且都很笃定，BSS 必须为正——否则这个字段永远在说"你很差"。"""
    _score_all(desk, [(0.9, 1), (0.9, 1), (0.1, 0), (0.1, 0)])

    out = desk.paper.calibration()

    assert out["brier"] == pytest.approx(0.01)
    assert out["base_rate"] == pytest.approx(0.5)
    assert out["bss"] == pytest.approx(0.96)


def test_a_one_sided_sample_has_no_skill_score_rather_than_zero(desk):
    """全涨或全跌时 B_c=0，技巧分没有定义。

    返回 0 会被读成"和只会报基础率一样好"——而实际在这个样本上，"只会报基础率"
    是完美预测，任何偏离它的概率都更差。
    """
    _score_all(desk, [(0.6, 1), (0.4, 1)])

    out = desk.paper.calibration()

    # B = ((0.6-1)^2 + (0.4-1)^2)/2 = (0.16 + 0.36)/2 = 0.26
    assert out["brier"] == pytest.approx(0.26)
    assert out["brier_climatology"] == pytest.approx(0.0)
    assert out["bss"] is None, "没有定义，不是零"
    assert "没有定义" in out["reading"]


def test_no_predictions_reports_none_not_zero(desk):
    """没有预测时 `brier=0` 会被读成"完美校准"。"""
    out = desk.paper.calibration()

    assert out["n"] == 0
    assert out["brier"] is None
    assert out["bss"] is None
    assert out["base_rate"] is None
