"""家数与连板高度：从派生池读，并把厂商的数字留在旁边。

这一组测试锁的是「哪一版算数」以及「另一版去哪了」。同花顺不是另一种口径，
是名单不全：2026-09-01~10-30 的 38 个交易日里，它的涨停家数只有 16 天与交易所
限价逐只一致，58% 的日子偏低，平均少 1.6 只，2026-10-26 漏 16 只、多认 2 只（净 45 vs 59）；
2026-09-02 还把 5 板报成 4 板。而 `max_boards >= 4` 是情绪闸门的输入，
`limit_up >= 40` 是另一个——所以漏掉一只高位板，标签就会从主升掉到修复。
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.trading import sentiment as sed
from service.trading.tape import breadth, pool_health, promotion_series, summarize_limit


class FakeStore:
    """只实现 pool_health 会用到的那点语义：日期过滤、列选择、等值过滤。"""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames

    def read(self, name, start=None, end=None, columns=None, filters=None):
        frame = self.frames.get(name, pd.DataFrame()).copy()
        if frame.empty:
            return frame
        if "trade_date" in frame.columns:
            days = frame["trade_date"].astype(str)
            if start:
                frame = frame[days >= str(start)]
            if end:
                frame = frame[frame["trade_date"].astype(str) <= str(end)]
        elif "cal_date" in frame.columns:
            days = frame["cal_date"].astype(str)
            if start:
                frame = frame[days >= str(start)]
            if end:
                frame = frame[frame["cal_date"].astype(str) <= str(end)]
        for column, _, value in filters or ():
            if column in frame.columns:
                frame = frame[frame[column] == value]
        return frame[columns] if columns and not frame.empty else frame


def _pools(rows: list[tuple[str, str, int]]) -> pd.DataFrame:
    """(pool, code, boards) — 派生池的形状，只留用得上的列。"""
    return pd.DataFrame({
        "pool": [pool for pool, _, _ in rows],
        "code": [code for _, code, _ in rows],
        "boards": [boards for _, _, boards in rows],
    })


# ----------------------------------------------------------------- breadth

def test_counts_come_from_the_derived_pool_including_the_beijing_split():
    store = FakeStore({"limit_pool_t0": _pools([
        ("limit_up", "000001.SZ", 3),
        ("limit_up", "600000.SH", 1),
        ("limit_up", "920592.BJ", 1),
        ("broken_board", "000002.SZ", 0),
        ("limit_down", "000003.SZ", 0),
    ])})

    got = breadth(store, "20261026")

    assert got["limit_up"] == 3
    # 30% 涨跌幅的北交所算不算「涨停家数」是个选择，所以两个数都给：
    # 全量 3 只，剔掉北交所 2 只。漏报则不是选择。
    assert got["limit_up_ex_bj"] == 2
    assert got["broken"] == 1
    assert got["limit_down"] == 1
    assert got["max_boards"] == 3
    assert got["tiers"] == {"1板": 2, "3板": 1}
    assert got["break_rate"] == pytest.approx(0.25)


def test_a_missing_derived_partition_is_none_not_zero():
    """没建这天 = 不知道，不是「没有涨停」。这个区别就是这张表存在的理由。"""
    assert breadth(FakeStore({}), "20261026") is None
    assert breadth(FakeStore({"limit_pool_t0": pd.DataFrame()}), "20261026") is None


def test_tiers_roll_the_top_bucket_instead_of_listing_sixteen_keys():
    store = FakeStore({"limit_pool_t0": _pools([
        ("limit_up", "000001.SZ", 6),
        ("limit_up", "000002.SZ", 9),
        ("limit_up", "000003.SZ", 1),
    ])})

    assert breadth(store, "20261026")["tiers"] == {"1板": 1, "6板及以上": 2}


# ------------------------------------------------------- summarize_limit

def _ths_up():
    return pd.DataFrame([
        {"ts_code": "A", "name": "a", "continue_cnt": 2, "continue_text": "2连板",
         "seal_money": 100, "reason": "AI", "limit_up_time": "09:35"},
        {"ts_code": "B", "name": "b", "continue_cnt": 1, "continue_text": "首板",
         "seal_money": 300, "reason": "AI", "limit_up_time": "10:30"},
    ])


def test_derived_counts_replace_the_vendor_ones_but_do_not_delete_them():
    derived = {"source": "derived:limit_pool_t0", "limit_up": 5, "limit_up_ex_bj": 5,
               "broken": 3, "limit_down": 2, "max_boards": 4,
               "tiers": {"1板": 3, "4板": 2}, "break_rate": 0.375}

    out = summarize_limit(None, _ths_up(), pd.DataFrame(), pd.DataFrame(),
                          pd.DataFrame(), breadth=derived)

    assert out["counts"]["limit_up"] == 5
    assert out["counts"]["source"] == "derived:limit_pool_t0"
    # 同花顺自己说的 2 只没有被抹掉，只是换了位置。
    # 键名必须带厂商：`facts.vendor_eastmoney` 是另一个源，两个都叫 vendor
    # 会让同一天的东财 82 与同花顺 86 看起来像有人算错了。
    assert out["counts"]["vendor_ths"]["limit_up"] == 2
    assert out["max_streak"] == 4
    assert out["tiers"] == {"1板": 3, "4板": 2}


def test_touched_comes_from_the_same_pool_as_its_numerator():
    """炸板率 = 炸板 / (涨停 + 炸板)，三项必须同源。

    以前 touched 取厂商的 `touched_limit_up_stock_count`，而分子可能来自另一家；
    混着算出来的比率既不是 A 家的也不是 B 家的。
    """
    derived = {"source": "derived:limit_pool_t0", "limit_up": 5, "limit_up_ex_bj": 5,
               "broken": 3, "limit_down": 0, "max_boards": 2, "tiers": {},
               "break_rate": 0.375}
    stat = pd.DataFrame([{"level": "one", "stock_count": 1, "promotion_rate": None,
                          "seal_rate": None, "touched_limit_up_stock_count": 99}])

    out = summarize_limit(stat, _ths_up(), pd.DataFrame(), pd.DataFrame(),
                          pd.DataFrame(), breadth=derived)

    assert out["counts"]["touched"] == 8          # 5 + 3, not the vendor's 99
    assert out["counts"]["break_rate"] == 0.375
    assert out["seal_rate"] == pytest.approx(0.625)   # 5 / (5 + 3), same pool


# ------------------------------------------------------------- sentiment

def _em_pools(up_rows, broken_rows, down_rows=()):
    return {"limit_up_pool": pd.DataFrame(up_rows),
            "broken_board_pool": pd.DataFrame(broken_rows),
            "limit_down_pool": pd.DataFrame(down_rows)}


def test_the_panel_uses_breadth_for_the_gate_inputs():
    """2026-09-02 的缩影：厂商漏了那只最高板，标签就从主升掉到修复。

    厂商池：45 只涨停、最高 3 板、20 只炸板 -> 修复（高度不够）。
    派生池：49 只、最高 5 板、32 只炸板 -> 主升（40+ 家、4 板以上、炸板率 0.395 < 0.475）。
    ``max_boards >= 4`` 和 ``limit_up >= 40`` 都是闸门输入，所以这不是小数点问题。
    """
    pools = _em_pools(
        [{"boards": 1 + (i % 3), "seal_amount": 1e8} for i in range(45)],
        [{"boards": 1}] * 20,
    )
    derived = {"source": "derived:limit_pool_t0", "limit_up": 49, "limit_up_ex_bj": 49,
               "broken": 32, "limit_down": 5, "max_boards": 5,
               "tiers": {}, "break_rate": 0.3951}

    with_breadth = sed.panel(pools, breadth=derived)
    without = sed.panel(pools)

    assert without["tone"] == "修复"
    assert with_breadth["tone"] == "主升"
    assert with_breadth["limit_up"] == 49
    assert with_breadth["max_boards"] == 5
    assert with_breadth["counts_source"] == "derived:limit_pool_t0"
    assert with_breadth["vendor_counts"]["limit_up"] == 45


def test_each_definition_is_gated_on_its_own_threshold():
    """派生 38% 对厂商 35%：两个数不是同一个量，不能共用一个阈值。

    246 个重叠交易日上，派生比厂商高 4.84pp。用同一个 35% 去卡，一致率 87.8%；
    各用各的（38% / 35%），95.1%。2026-10-20 那天（厂商 0.3494、派生 0.3596）
    换算之后两边都过闸门——之前那次"翻转"其实是拿错了尺子。
    """
    pools = _em_pools([{"boards": 4, "seal_amount": 1e8}] * 57, [{"boards": 1}] * 26)
    derived = {"source": "derived:limit_pool_t0", "limit_up": 57, "limit_up_ex_bj": 57,
               "broken": 32, "limit_down": 1, "max_boards": 4,
               "tiers": {}, "break_rate": 0.3596}

    verdict = sed.gates(sed.panel(pools, breadth=derived))

    assert verdict["checks"]["break_rate_ok"] is True
    assert "borderline" not in verdict


def test_a_gate_that_straddles_the_two_definitions_says_so():
    """两边各用自己那一版阈值**仍然**给出相反结论时，才由读者决定。

    这种情况不该由代码替读者选一版，也不该取平均——平均值会得到一个两边都对不上的数。
    """
    pools = _em_pools([{"boards": 4, "seal_amount": 1e8}] * 57, [{"boards": 1}] * 19)
    derived = {"source": "derived:limit_pool_t0", "limit_up": 57, "limit_up_ex_bj": 57,
               "broken": 37, "limit_down": 1, "max_boards": 4,
               "tiers": {}, "break_rate": 0.3936}

    panel = sed.panel(pools, breadth=derived)
    verdict = sed.gates(panel)

    assert panel["break_rate"] == 0.3936            # > 0.38 -> 不过
    assert panel["break_rate_vendor"] == pytest.approx(19 / 76, abs=1e-3)   # < 0.35 -> 过
    assert verdict["checks"]["break_rate_ok"] is False
    assert verdict["borderline"]["derived"] == 0.3936
    assert verdict["borderline"]["gate"] == {"derived": 0.38, "vendor": 0.35}


def test_no_borderline_when_both_definitions_agree():
    pools = _em_pools([{"boards": 4, "seal_amount": 1e8}] * 50, [{"boards": 1}] * 5)
    derived = {"source": "derived:limit_pool_t0", "limit_up": 52, "limit_up_ex_bj": 52,
               "broken": 6, "limit_down": 1, "max_boards": 4,
               "tiers": {}, "break_rate": 0.1034}

    verdict = sed.gates(sed.panel(pools, breadth=derived))

    assert "borderline" not in verdict
    assert verdict["checks"]["break_rate_ok"] is True


# ------------------------------------------------------------ pool_health

def _pool_day(day: str, rows: list[tuple[str, str, int]]) -> pd.DataFrame:
    """派生池的一天，带 trade_date —— pool_health 按日期过滤。"""
    return _pools(rows).assign(trade_date=day)


def _calendar(days: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"cal_date": days, "is_open": [1] * len(days)})


def test_a_fresh_pool_says_where_the_numbers_came_from():
    store = FakeStore({
        "limit_pool_t0": _pool_day("20261030", [("limit_up", "A", 5),
                                                ("limit_up", "B", 2),
                                                ("limit_up", "C", 1)]),
        "trade_cal": _calendar(["20261030"]),
    })

    health = pool_health(store, "20261030")

    assert health["status"] == "fresh"
    assert health["max_boards"] == 5
    assert health["sessions_behind"] == 0
    assert "派生池" in health["impact"]


def test_a_missing_partition_is_reported_as_missing_not_as_zero_limit_ups():
    """没建 ≠ 没有涨停。这条路径以前是静默退回厂商口径，数字照样出来。"""
    store = FakeStore({
        "limit_pool_t0": _pool_day("20261029", [("limit_up", "A", 3)]),
        "trade_cal": _calendar(["20261029", "20261030"]),
    })

    health = pool_health(store, "20261030")

    assert health["status"] == "missing"
    assert health["limit_up"] is None
    assert health["last_derived"] == "20261029"
    # 差一个交易日，不是"差一天"：停牌周里那两件事不一样。
    assert health["sessions_behind"] == 1
    assert "退回厂商" in health["impact"]


def test_an_all_first_board_day_is_flagged_as_unheated():
    """单日重建、没有预热：每只票都是 1 板。这一天不能直接引用连板高度。"""
    store = FakeStore({
        "limit_pool_t0": _pool_day("20261028", [("limit_up", c, 1) for c in "ABCDEF"]),
        "trade_cal": _calendar(["20261028"]),
    })

    health = pool_health(store, "20261028")

    assert health["status"] == "unheated"
    assert health["max_boards"] == 1
    # 说"疑似"，不说"错误"：真可能全是首板。
    assert "预热" in health["impact"]


def test_a_small_pool_is_not_accused_of_being_unheated():
    """只有 3 只涨停时全场首板很正常，不该触发告警。"""
    store = FakeStore({
        "limit_pool_t0": _pool_day("20261028", [("limit_up", c, 1) for c in "ABC"]),
        "trade_cal": _calendar(["20261028"]),
    })

    assert pool_health(store, "20261028")["status"] == "fresh"


def test_a_year_boundary_reset_does_not_become_a_zero_promotion_rate():
    """2010–2022 的分区没带预热，每年首个交易日 boards 全是 1。

    那时算出来的晋级率是 0.0——一个看起来像"全部断板"的数字，而真相是"没有前一天可比"。
    """
    rows = [("20211231", "A", 3), ("20211231", "B", 2),
            ("20220104", "A", 1), ("20220104", "C", 1)]
    store = FakeStore({"limit_pool_t0": _pools([("limit_up", code, boards)
                                                for _, code, boards in rows]).assign(
        trade_date=[day for day, _, _ in rows])})

    series = promotion_series(store, ["20211231", "20220104"])

    for level in series["20220104"]:
        assert level["promotion_rate"] is None
        assert level.get("reset") is True
