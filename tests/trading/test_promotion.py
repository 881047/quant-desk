"""晋级率由派生池自己算，不是从那张 5 天的表里读。

上游 `ths_limit_stat` 只留 5 个交易日。用它撑「涨停结构」这一维，等于用一周的样本
回答一个需要季度先验的问题。而 `limit_pool_t0` 从 2010 年起就有每只票的 `boards`，
晋级率的分母分子都在里面：

    昨日 k 板的只数  →  其中今天到了 k+1 板的只数

分母必须是**昨日**的 k 板，不是今日的 k+1 板。弄反了每个档位都会算出 1.0——那不是
晋级率，那是"今天所有 k+1 板昨天都是 k 板"的同义反复。
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.trading.tape import promotion_series, summarize_limit


class FakeStore:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames

    def read(self, name, start=None, end=None, columns=None):
        frame = self.frames.get(name, pd.DataFrame()).copy()
        return frame[columns] if columns and not frame.empty else frame


def _pool(rows: list[tuple[str, str, int]]) -> pd.DataFrame:
    """(day, code, boards) — the derived pool's shape, trimmed to what matters."""
    return pd.DataFrame({
        "trade_date": [day for day, _, _ in rows],
        "code": [code for _, code, _ in rows],
        "pool": ["limit_up"] * len(rows),
        "boards": [boards for _, _, boards in rows],
    })


def test_the_denominator_is_yesterdays_count_not_todays():
    rows = [
        # 昨天：A/B 首板，C 二板，D 三板
        ("20261029", "A", 1), ("20261029", "B", 1),
        ("20261029", "C", 2), ("20261029", "D", 3),
        # 今天：A 晋级二板，B 断板（不在池里），C 晋级三板，D 晋级四板，E 新首板
        ("20261030", "A", 2),
        ("20261030", "C", 3), ("20261030", "D", 4),
        ("20261030", "E", 1),
    ]
    series = promotion_series(FakeStore({"limit_pool_t0": _pool(rows)}),
                              ["20261029", "20261030"])
    rows_today = {r["level"]: r for r in series["20261030"]}

    # 2 只昨日首板，1 只晋级 → 0.5。除以今日 2 板数（1 只）会得到 1.0，那是错的。
    assert rows_today["two"]["base"] == 2
    assert rows_today["two"]["promoted"] == 1
    assert rows_today["two"]["count"] == 1
    assert rows_today["two"]["promotion_rate"] == pytest.approx(0.5)

    assert rows_today["three"]["promotion_rate"] == pytest.approx(1.0)
    assert rows_today["four"]["promotion_rate"] == pytest.approx(1.0)
    # 没有五板以上的底数：不可计算，不是 0。
    assert rows_today["five_plus"]["promotion_rate"] is None
    assert rows_today["five_plus"]["base"] == 0


def test_the_series_covers_every_adjacent_pair_it_was_given():
    rows = []
    for day, code, board in (("20261028", "A", 1), ("20261029", "A", 2),
                             ("20261030", "A", 3)):
        rows.append((day, code, board))
    series = promotion_series(FakeStore({"limit_pool_t0": _pool(rows)}),
                              ["20261028", "20261030"])

    assert sorted(series) == ["20261029", "20261030"]
    assert series["20261029"][0]["promoted"] == 1


def test_no_pool_means_no_series_rather_than_zeros():
    assert promotion_series(FakeStore({}), ["20261030"]) == {}
    empty = pd.DataFrame({"trade_date": [], "code": [], "pool": [], "boards": []})
    assert promotion_series(FakeStore({"limit_pool_t0": empty}), ["20261030"]) == {}


def _derived() -> list[dict]:
    return [
        {"level": "two", "board": 2, "count": 1, "promoted": 1, "base": 2,
         "promotion_rate": 0.5},
        {"level": "three", "board": 3, "count": 1, "promoted": 1, "base": 1,
         "promotion_rate": 1.0},
    ]


def _vendor() -> pd.DataFrame:
    return pd.DataFrame({
        "level": ["two", "three", "one"],
        "stock_count": [1.0, 1.0, 9.0],
        "promotion_rate": [0.1, 1.0, None],
    })


def test_the_derived_series_wins_and_says_so():
    summary = summarize_limit(_vendor(), None, None, None, None, promotion=_derived())

    assert summary["promotion_source"] == "derived:limit_pool_t0"
    assert {r["level"]: r["promotion_rate"] for r in summary["promotion"]}["two"] == 0.5


def test_a_disagreement_is_shown_not_averaged():
    summary = summarize_limit(_vendor(), None, None, None, None, promotion=_derived())
    spread = summary["promotion_spread"]

    assert spread["max_abs_diff"] == pytest.approx(0.4)
    assert {r["level"]: r["promotion_rate"] for r in spread["vendor"]}["two"] == 0.1
    assert "不取平均" in spread["note"]


def test_agreement_within_two_points_is_not_reported():
    """差 1–2 个百分点是口径，不是冲突；把它报出来只会制造噪声。"""
    close = pd.DataFrame({"level": ["two"], "stock_count": [1.0], "promotion_rate": [0.51]})
    summary = summarize_limit(close, None, None, None, None, promotion=_derived())

    assert "promotion_spread" not in summary


def test_without_a_derived_series_it_falls_back_to_the_vendor_table():
    """5 天的表还在，只是降级成兜底——派生池读不到时不能直接没有这个字段。"""
    summary = summarize_limit(_vendor(), None, None, None, None)

    assert summary["promotion_source"] == "ths_limit_stat"
    assert {r["level"]: r["promotion_rate"] for r in summary["promotion"]}["two"] == 0.1
    assert "promotion_spread" not in summary


class RecordingStore(FakeStore):
    """Records every dataset it was asked for — a read is a dependency, too."""

    def __init__(self, frames):
        super().__init__(frames)
        self.reads: list[str] = []

    def read(self, name, start=None, end=None, columns=None):
        self.reads.append(name)
        return super().read(name, start, end, columns)


def test_the_series_never_touches_the_vendor_tables():
    """上游的池子会漏报，所以晋级率连读都不读它。

    20261026 那天同花顺只给 45 只涨停、派生池 59 只，派生池多出的 5 只全部收在
    涨停价上。只要分子还从上游取，漏报就会变成一个看起来像"晋级率低"的数字。
    这条测试锁住的是依赖关系本身，不是某一天的数值。
    """
    rows = [("20261029", "A", 1), ("20261029", "B", 1),
            ("20261030", "A", 2)]
    store = RecordingStore({"limit_pool_t0": _pool(rows)})

    series = promotion_series(store, ["20261029", "20261030"])

    # 只读派生池与交易日历。上游那三张表（ths_limit_up / _break / _stat）一次都不碰，
    # 因为它们的名单会漏，漏了就会变成一个看起来正常的低晋级率。
    assert store.reads == ["limit_pool_t0", "trade_cal"]
    assert not any(name.startswith("ths_") for name in store.reads)
    assert series["20261030"][0]["promotion_rate"] == pytest.approx(0.5)


def test_a_genuine_all_first_board_day_is_not_read_as_a_reset():
    """全场首板是真的会发生的，不能把它当成"没预热"而拒绝给数。

    先用「今天最高板 ≤1 而昨天 ≥2」判断，4,083 个交易日对里有 73 天是那个形状，
    修好数据后**全部是真数据**。改成看重叠票（连着封板的必须恰好 +1）后命中 0 天。
    """
    rows = [
        ("20261029", "A", 3), ("20261029", "B", 2),      # 昨天有高度
        ("20261030", "C", 1), ("20261030", "D", 1),      # 今天全是新首板，A/B 都断板
    ]
    series = promotion_series(FakeStore({"limit_pool_t0": _pool(rows)}),
                              ["20261029", "20261030"])

    rows_today = {r["level"]: r for r in series["20261030"]}
    assert all(r.get("reset") is None for r in rows_today.values())
    # 没有重叠票，是否重置无从判断，但也不该把这一天标成算不出来：
    # 昨日 2 板 1 只、今日 3 板 0 只 -> 晋级率 0.0（一个真实的 0，不是 None）。
    assert rows_today["three"]["base"] == 1
    assert rows_today["three"]["promotion_rate"] == pytest.approx(0.0)


def test_a_streak_that_did_not_increment_between_two_sessions_is_a_reset():
    """同一只票连着两天在板上，boards 必须恰好 +1；停在 1 就是计数器被重置了。"""
    rows = [
        ("20261029", "A", 3),
        ("20261030", "A", 1),                             # 应该 4
    ]
    series = promotion_series(FakeStore({"limit_pool_t0": _pool(rows)}),
                              ["20261029", "20261030"])

    assert all(r.get("reset") is True for r in series["20261030"])
    assert all(r["promotion_rate"] is None for r in series["20261030"])
