"""封单轨迹：分帧数据的形状与"帧数不够时不许给趋势"。

这个模块最容易出错的地方不是算错，而是**用一帧数据装出趋势**。所以每条断言
都盯着两件事：帧数不足时输出必须是 None 或空列表，以及"炸板后的买一金额"绝
不能被当成封单。
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.trading.seal_tape import tape


class _StubData:
    def __init__(self, frame: pd.DataFrame | None):
        self._frame = frame

    def read(self, dataset, start, end, **kwargs):
        if self._frame is None:
            return pd.DataFrame()
        return self._frame


class _StubDesk:
    def __init__(self, frame: pd.DataFrame | None):
        self.data = _StubData(frame)


def _row(stamp, code, name, pool, value, *, boards=1, open_times=0):
    return {
        "snapshot_ts": stamp,
        "trade_date": "20261104",
        "code": code,
        "name": name,
        "pool": pool,
        "last": 10.0,
        "up_limit": 10.0,
        "bid1_value": value,
        "bid1_volume": value / 1000.0,
        "ask1_volume": 0.0,
        "bid_volume_5": value / 900.0,
        "ask_volume_5": 0.0,
        "boards": boards,
        "open_times": open_times,
        "first_seal_time": "09:31:00",
        "float_mv": 3e9,
        "source": "tencent",
    }


def test_missing_tape_says_why_instead_of_returning_an_empty_trend():
    out = tape(_StubDesk(None), "20261104")

    assert out["ok"] is False
    # 必须解释"是没落盘"而不是"当天没有涨停"——这两件事读者会做出相反的决定。
    assert "hint" in out
    assert "落盘" in out["hint"] or "轨迹" in out["hint"]


def test_a_single_frame_does_not_invent_a_trend():
    frame = pd.DataFrame([
        _row("2026-11-04T09:40:00+08:00", "000001", "甲", "limit_up", 1e8),
        _row("2026-11-04T09:40:00+08:00", "000002", "乙", "limit_up", 2e8),
    ])
    out = tape(_StubDesk(frame), "20261104")

    assert out["ok"] is True
    assert out["frames"] == 1
    # 一帧的"起止差"恒为 0，排出来像"全部平稳"，那是假结论。
    assert out["sealed"]["decay_top"] == []
    assert out["sealed"]["build_top"] == []
    assert out["market"]["erosion_pct"] is None
    assert out["market"]["enough_frames"] is False
    assert any("帧" in note for note in out["caveats"])


def test_two_frames_rank_seal_decay_and_report_market_peak():
    frame = pd.DataFrame([
        _row("2026-11-04T10:00:00+08:00", "000001", "甲", "limit_up", 1.0e8),
        _row("2026-11-04T10:00:00+08:00", "000002", "乙", "limit_up", 1.0e8),
        _row("2026-11-04T14:00:00+08:00", "000001", "甲", "limit_up", 4.0e7),
        _row("2026-11-04T14:00:00+08:00", "000002", "乙", "limit_up", 1.5e8),
    ])
    out = tape(_StubDesk(frame), "20261104")

    assert out["ok"] is True
    assert out["frames"] == 2
    worst = out["sealed"]["decay_top"][0]
    assert worst["code"] == "000001"
    # 1.0 亿 → 0.4 亿，跌 60%。
    assert worst["change_pct"] == -60.0
    best = out["sealed"]["build_top"][0]
    assert best["code"] == "000002"
    assert best["change_pct"] == 50.0
    # 两帧家数都是 2，峰值在终值，回吐 0.0——这是真的 0，不是缺失。
    assert out["market"]["erosion_pct"] == 0.0


def test_a_stock_that_left_the_pool_is_flagged_and_not_counted_as_sealed():
    frame = pd.DataFrame([
        _row("2026-11-04T10:00:00+08:00", "000001", "甲", "limit_up", 1.0e8),
        _row("2026-11-04T10:00:00+08:00", "000002", "乙", "limit_up", 1.0e8),
        # 甲 14:00 已炸板：池子从 limit_up 换到 broken_board。
        _row("2026-11-04T14:00:00+08:00", "000001", "甲", "broken_board", 3.0e6),
        _row("2026-11-04T14:00:00+08:00", "000002", "乙", "limit_up", 1.2e8),
    ])
    out = tape(_StubDesk(frame), "20261104")

    early = out["sealed"]["left_early"]
    assert [row["code"] for row in early] == ["000001"]
    # 炸板后的 300 万是买一承接，不是封单——它只能出现在 broken 里。
    assert all(row["code"] != "000001" for row in out["sealed"]["decay_top"])
    assert [row["code"] for row in out["broken"]["residual_bid"]] == ["000001"]


def test_post_close_frames_are_kept_as_a_level_but_never_as_a_trend():
    """收盘后的两帧封单额必然相同——把它算成"平稳"是把"没有交易"读成"没有变化"。"""
    frame = pd.DataFrame([
        _row("2026-11-04T15:05:00+08:00", "000001", "甲", "limit_up", 1.0e8),
        _row("2026-11-04T16:20:00+08:00", "000001", "甲", "limit_up", 1.0e8),
    ])
    out = tape(_StubDesk(frame), "20261104")

    assert out["ok"] is True
    assert out["frames_total"] == 2   # 帧还在，是明天的基线
    assert out["frames"] == 0         # 但没有一帧是盘中观察
    assert out["sealed"]["decay_top"] == []
    assert out["market"]["enough_frames"] is False
    assert out["market"]["erosion_pct"] is None
    # 封单额本身仍然读得出来——它记的是收工那一刻的状态。
    assert out["sealed"]["count"] == 1
    assert any("收盘后" in note for note in out["caveats"])


def test_market_curve_counts_each_pool_separately():
    frame = pd.DataFrame([
        _row("2026-11-04T10:00:00+08:00", "000001", "甲", "limit_up", 1e8),
        _row("2026-11-04T10:00:00+08:00", "000002", "乙", "limit_up", 1e8),
        _row("2026-11-04T10:00:00+08:00", "000003", "丙", "broken_board", 1e6),
        _row("2026-11-04T14:00:00+08:00", "000001", "甲", "limit_up", 1e8),
    ])
    out = tape(_StubDesk(frame), "20261104")

    first = out["market"]["curve"][0]
    assert first["limit_up"] == 2
    assert first["broken_board"] == 1
    # 第二帧只剩一只封着 → 从 2 掉到 1，这是侵蚀。
    assert out["market"]["erosion_pct"] == -50.0


# ------------------------------------------------- 坏输入不是 0，也不是"没有变化"
#
# 下面四条走的是"数据脏了"的分支。它们的共同点是：**丢掉**或**报错**，
# 绝不补一个 0 进去。补 0 的后果是造出一个看起来正常的数——封单"掉到零"、
# "全部平稳"、"这只票没上榜"，三种误读都比没有数据更贵。

def test_a_non_numeric_seal_value_is_dropped_not_zeroed():
    """源给了"停牌"这类文字时，`_f` 返回 None。返回 0 会让它读成"封单归零"。"""
    from service.trading.seal_tape import _f

    assert _f("停牌") is None
    assert _f(object()) is None
    assert _f(123.456) == 123.46


def test_nan_is_not_a_zero_either():
    """NaN 是"这个源没给"。float('nan') 能过 float()，但过不了 isnan 那道。"""
    from service.trading.seal_tape import _f

    assert _f(float("nan")) is None
    assert _f("nan") is None


def test_a_store_that_raises_reads_as_no_data_not_as_a_crash():
    """分区读写抛异常时，调用方应该拿到"没有数据"，不是 500。

    `_frames` 捕的是 KeyError / ValueError / OSError：前两个是"这一列/这一天
    不存在"，OSError 是文件被别的进程占着——盘后落库和读盘会撞上。
    """
    class _Angry:
        def read(self, *_a, **_k):
            raise OSError("file in use")

    out = tape(type("D", (), {"data": _Angry()})(), "20261104")

    assert out["ok"] is False
    assert "没有分帧盘口数据" in out["error"]


def test_a_seal_row_whose_value_is_unreadable_is_left_out_of_the_series():
    """整只票的 bid1_value 都读不出来时，它不进封单序列。

    留一条 first_value=None 的行，下游排序和百分比都会拿到 None，
    而榜单会显示一只它其实没观察到的票。
    """
    rows = [
        _row("2026-11-04 09:30:00+08:00", "600001.SH", "甲", "limit_up", 1_000_000.0),
        _row("2026-11-04 09:33:00+08:00", "600001.SH", "甲", "limit_up", 900_000.0),
        _row("2026-11-04 09:30:00+08:00", "600002.SH", "乙", "limit_up", float("nan")),
        _row("2026-11-04 09:33:00+08:00", "600002.SH", "乙", "limit_up", float("nan")),
    ]
    out = tape(_StubDesk(pd.DataFrame(rows)), "20261104")

    codes = {row["code"] for row in out["sealed"]["decay_top"]}
    assert codes == {"600001.SH"}
    assert out["sealed"]["count"] == 1, "乙没有可读的封单，不算进封单序列"


def test_the_closing_call_auction_is_not_a_seal_collapse():
    """14:57 之后盘口被清空，那不是封单在流失。

    2026-11-04 用当天真实分帧发现：141 只涨停股里 131 只（93%）触发"衰减
    ≥30%"，衰减榜前几名全是 −100%。查一只——000096 在 14:56:28 封单 4.24 亿，
    14:57:10 变成 0，接着 86 手。仍然封着的票不可能在 42 秒里被吃干净；
    那是收盘集合竞价（沪深都是 14:57 起）把连续竞价的买一清掉了。全市场中位数
    从 5,570 万掉到 119 万，98.3% 的票在那一刻"跌破三成"。

    所以这段要和收盘后的帧一样处理：留着当收盘读数，不进趋势。
    """
    rows = [
        _row("2026-11-04 14:54:00+08:00", "600001.SH", "甲", "limit_up", 1_000_000.0),
        _row("2026-11-04 14:56:00+08:00", "600001.SH", "甲", "limit_up", 900_000.0),
        # 集合竞价：盘口被清空
        _row("2026-11-04 14:57:10+08:00", "600001.SH", "甲", "limit_up", 0.0),
        _row("2026-11-04 14:59:59+08:00", "600001.SH", "甲", "limit_up", 700.0),
    ]
    out = tape(_StubDesk(pd.DataFrame(rows)), "20261104")

    assert out["frames"] == 2, "只有 14:57 前的两帧可比"
    assert out["frames_total"] == 4
    assert out["frames_closing_auction"] == 2
    # 真实变化是 -10%，不是 -99.93%
    assert out["sealed"]["decay_top"][0]["change_pct"] == pytest.approx(-10.0)
    assert out["alerts"] == [], "跌 10% 不该报警"
    assert any("集合竞价" in c for c in out["caveats"])


def test_the_market_curve_still_counts_the_auction_frames():
    """家数不受盘口清零影响：一只票在集合竞价里掉出涨停池，那是真的掉出。

    所以 `curve` 用全部帧，只有"封单变化"用可比帧。两条口径服务的不是一件事。
    """
    rows = [
        _row("2026-11-04 14:54:00+08:00", "600001.SH", "甲", "limit_up", 1_000_000.0),
        _row("2026-11-04 14:56:00+08:00", "600001.SH", "甲", "limit_up", 900_000.0),
        _row("2026-11-04 14:59:59+08:00", "600001.SH", "甲", "limit_up", 700.0),
        _row("2026-11-04 14:59:59+08:00", "600002.SH", "乙", "broken_board", 5000.0),
    ]
    out = tape(_StubDesk(pd.DataFrame(rows)), "20261104")

    last = out["market"]["curve"][-1]
    assert last["limit_up"] == 1 and last["broken_board"] == 1, "收盘那一帧也要有家数"
    assert out["frames"] == 2, "但趋势只认前两帧"


def test_the_ranking_uses_the_stocks_own_peak_not_its_first_frame():
    """中途封板的票，首帧是封板那一瞬，拿它当基准会算出荒谬的"增长"。

    2026-11-04 实测：141 只涨停股里只有 6 只从第一帧就在池里。002821 首帧
    17.9 万、下一帧 5.2 亿，"首末变化"是 +131,135%（1,316 倍）。所以榜单按
    "相对自己峰值还剩多少"排，那个基准对几点封板的票都成立。
    """
    rows = [
        _row("2026-11-04 10:00:00+08:00", "600001.SH", "甲", "limit_up", 178_560.0),
        _row("2026-11-04 10:03:00+08:00", "600001.SH", "甲", "limit_up", 520_000_000.0),
        _row("2026-11-04 14:56:00+08:00", "600001.SH", "甲", "limit_up", 130_000_000.0),
    ]
    out = tape(_StubDesk(pd.DataFrame(rows)), "20261104")

    row = out["sealed"]["decay_top"][0]
    assert row["change_pct"] > 70000, "首末口径仍然是那个荒谬的数（保留原值，不篡改）"
    assert row["from_peak_pct"] == pytest.approx(-75.0), "相对峰值跌了 75%"
    assert row["max_value"] == 520_000_000.0


def test_the_alert_base_rate_travels_with_the_alert_list():
    """一个每天对大多数股票都成立的告警等于没有告警。

    2026-11-04 实测：141 只里 105 只触发「相对峰值跌破 30%」——那是当天的常态，
    不是 105 个异常。把发生率摆在旁边，读者才知道该不该把它当信号。
    """
    rows = []
    for i, code in enumerate(["600001.SH", "600002.SH", "600003.SH", "600004.SH"]):
        rows.append(_row("2026-11-04 10:00:00+08:00", code, f"票{i}", "limit_up", 1e8))
        # 三只跌 50%，一只只跌 5%
        rows.append(_row("2026-11-04 10:03:00+08:00", code, f"票{i}", "limit_up",
                         5e7 if i < 3 else 9.5e7))
    out = tape(_StubDesk(pd.DataFrame(rows)), "20261104")

    assert len(out["alerts"]) == 3
    assert out["decay_base_rate"] == pytest.approx(0.75)


def test_a_build_list_only_names_stocks_present_from_the_first_frame():
    """「封单在增加」只对全天都在池里的票成立。

    中途封板的票首帧是封板瞬间，任何首末比较都不可信——把它们排进"增强榜"，
    排在前面的永远是刚封板的那些，而不是封单真在加厚的。
    """
    rows = [
        # 甲：三帧都在池里（seen_ratio=1），封单从 1 亿涨到 1.5 亿
        _row("2026-11-04 10:00:00+08:00", "600001.SH", "甲", "limit_up", 1e8),
        _row("2026-11-04 13:00:00+08:00", "600001.SH", "甲", "limit_up", 1.2e8),
        _row("2026-11-04 14:56:00+08:00", "600001.SH", "甲", "limit_up", 1.5e8),
        # 乙：13:00 才封板，首帧 1 万 → 末帧 1 亿，"增长"看起来是 9,999 倍
        _row("2026-11-04 13:00:00+08:00", "600002.SH", "乙", "limit_up", 1e4),
        _row("2026-11-04 14:56:00+08:00", "600002.SH", "乙", "limit_up", 1e8),
    ]
    out = tape(_StubDesk(pd.DataFrame(rows)), "20261104")

    assert [r["code"] for r in out["sealed"]["build_top"]] == ["600001.SH"], \
        "乙是中途封板，不该出现在增强榜"


def test_the_market_note_says_the_curve_is_intraday():
    """2026-11-13 的 18:50 报告把末帧写成「收盘 37 / 21 / 40」，派生收盘是 38 / 24 / 39。

    工具自己把末帧叫「收盘」是这场混淆的源头：末帧是 14:59 的 rt_snapshots，
    收盘家数要按 `close == up_limit` 从日线重算。说明里不写明，每份复盘都会
    多一个对不上的"收盘"。
    """
    rows = [_row(f"2026-11-04T14:{minute:02d}:00+08:00", "600000", "浦发银行",
                 "limit_up", 1e8) for minute in (50, 55, 58)]

    out = tape(_StubDesk(pd.DataFrame(rows)), "20261104")

    note = out["market"]["note"]
    assert "盘中逐帧" in note
    assert "quant_facts.pools" in note


def test_tape_defaults_to_the_last_session_not_the_calendar_day():
    """周六问「昨天的封单轨迹」，应该拿到周五的，不是一句"今天没有分帧数据"。

    `tape()` 原来用日历日，于是 `quant_knowledge(topic='seal_tape')` 在周末永远
    ok:false，而同一条命令的 industries / limit_structure / news 都回落到最近交易日。
    """
    class _Market:
        def last_trading_day(self):
            return "20261104"

    desk = _StubDesk(pd.DataFrame([
        _row("2026-11-04T14:50:00+08:00", "600000", "浦发银行", "limit_up", 1e8),
        _row("2026-11-04T14:55:00+08:00", "600000", "浦发银行", "limit_up", 9e7),
    ]))
    desk.market = _Market()

    out = tape(desk, None)

    assert out["as_of"] == "20261104"
    assert out.get("ok") is not False


def test_curve_pages_recover_the_whole_early_to_late_path_without_changing_summary():
    rows = []
    for index, minute in enumerate((30, 35, 40, 45, 50)):
        rows.append(_row(f"2026-11-04T10:{minute:02d}:00+08:00", "600000",
                         "浦发银行", "limit_up", 1e8 + index * 1e6))
    desk = _StubDesk(pd.DataFrame(rows))
    full = tape(desk, "20261104", limit=200)
    pages = []
    offset = 0
    while offset is not None:
        out = tape(desk, "20261104", offset=offset, limit=2)
        page = out["market"]["pagination"]
        assert page["total"] == 5
        assert out["market"]["peak"] == full["market"]["peak"]
        assert out["market"]["erosion_pct"] == full["market"]["erosion_pct"]
        pages.extend(out["market"]["curve"])
        offset = page["next_offset"]
    assert pages == full["market"]["curve"]
    assert [row["ts"] for row in pages[:2]] == [row["ts"] for row in full["market"]["curve"][:2]]
    assert full["market"]["pagination"]["returned"] == 5
    assert full["market"]["pagination"]["truncated"] is False


def test_alert_page_keeps_the_full_day_count_and_rate():
    rows = []
    for index in range(12):
        code = f"6000{index:02d}"
        rows.append(_row("2026-11-04T10:00:00+08:00", code, str(index), "limit_up", 1e8))
        rows.append(_row("2026-11-04T14:00:00+08:00", code, str(index), "limit_up", 4e7))
    out = tape(_StubDesk(pd.DataFrame(rows)), "20261104", top=10)
    assert len(out["alerts"]) == 10
    assert out["alerts_total"] == 12
    assert out["alerts_truncated"] is True
    assert out["decay_base_rate"] == 1.0
