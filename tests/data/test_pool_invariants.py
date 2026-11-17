"""派生池的不变式检查器：它必须能抓住那两类真实发生过的错。

检查器本身失效的话，下一轮重建之后没人知道数据有没有坏——所以这里用两个
"曾经真的写进过库"的形状来验证它：停牌接板、以及涨停/炸板互斥。
"""

from __future__ import annotations

import pandas as pd

from scripts.pool_invariants import check


def _row(day: str, code: str, pool: str, boards: int,
         close: float, high: float, limit: float) -> dict:
    return {"trade_date": day, "code": code, "pool": pool, "boards": boards,
            "close": close, "high": high, "limit_price": limit}


def test_a_clean_pool_passes():
    pools = pd.DataFrame([
        _row("20261029", "A", "limit_up", 1, 11.0, 11.0, 11.0),
        _row("20261030", "A", "limit_up", 2, 12.1, 12.1, 12.1),
        _row("20261030", "B", "broken_board", 0, 10.5, 11.0, 11.0),
    ])
    sessions = ["20261029", "20261030"]

    assert check(pools, sessions) == []


def test_a_streak_that_skips_a_session_is_caught():
    """停牌 bug 的形状：中间少一个交易日，复牌日却接着数。"""
    pools = pd.DataFrame([
        _row("20261029", "A", "limit_up", 5, 11.0, 11.0, 11.0),
        _row("20261102", "A", "limit_up", 6, 12.1, 12.1, 12.1),   # 中间隔了 10-30
    ])
    sessions = ["20261029", "20261030", "20261102"]

    rules = {p["rule"] for p in check(pools, sessions)}

    # 断层之后必须重新从 1 开始；接着数就是那条把 6 板变成 13 板的 bug。
    assert "streak_continues_across_gap" in rules


def test_a_board_that_is_both_sealed_and_broken_is_caught():
    pools = pd.DataFrame([
        _row("20261030", "A", "limit_up", 1, 11.0, 11.0, 11.0),
        _row("20261030", "A", "broken_board", 0, 10.4, 11.0, 11.0),
    ])

    rules = {p["rule"] for p in check(pools, ["20261030"])}

    assert "limit_up_and_broken_same_day" in rules


def test_a_broken_row_that_closed_at_the_ceiling_is_caught():
    """炸板行必须收在涨停价**下方**；收在涨停价上就是涨停行，分错池了。"""
    pools = pd.DataFrame([_row("20261030", "A", "broken_board", 0, 11.0, 11.0, 11.0)])

    rules = {p["rule"] for p in check(pools, ["20261030"])}

    assert "broken_close_at_ceiling" in rules
