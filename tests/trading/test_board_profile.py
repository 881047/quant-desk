"""封板体检的口径测试。

这里保护的几条，每一条都对应一次真实踩坑：

* **当日涨停股必须在面板里**。此前面板按「有次日收益」过滤，于是模型问当天
  这一只票时它根本不存在——封单额回退到另一个数据源、当日强度分位直接消失。
* **n 不够就不给数**。带 n 的噪声最危险，它看起来像被测量过。
* **没有 action / recommendation**。内核给事实，判断是模型的事。
* **降级阶梯必须一路记 n**，让读者看见条件收紧到什么程度样本就没了。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from service.trading import board


class _Data:
    def __init__(self, frames):
        self._frames = frames

    def read(self, name, start=None, end=None, columns=None):
        if name not in self._frames:
            raise KeyError(name)
        frame = self._frames[name]
        if columns:
            keep = [c for c in columns if c in frame.columns]
            return frame[keep] if keep else frame
        return frame


class _Market:
    def __init__(self, last="20260225"):
        self._last = last

    def latest_bar_date(self):
        return self._last

    def snapshot(self, symbol, day):
        return None

    def instrument(self, symbol, day):
        from service.kernel.models import Instrument

        return Instrument(symbol=symbol, name=symbol, asset="stock")


class _Desk:
    def __init__(self, frames, last="20260225"):
        self.data = _Data(frames)
        self.market = _Market(last)


def _sessions(n: int) -> list[str]:
    """n 个连续的工作日，起点固定，方便断言。"""
    out = []
    day = pd.Timestamp("2025-01-02")
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
        day += pd.Timedelta(days=1)
    return out


def _frames(n_history: int = 40, *, open_all=None, low_all=None):
    """n_history 个历史涨停样本 + 最后一天（"今天"）。

    所有样本都是 1 板、首封 09:35、封单 1 亿 / 流通 60 亿。价格按 ±1% 交替
    演进，所以每个事件的次日收益也是 ±1% 交替、均值正好为 0——任何把「今天」
    也算进收益的错误都会让断言失败。
    """
    days = _sessions(n_history + 1)
    codes = ["600000.SH"] * len(days)
    up = pd.DataFrame({
        "ts_code": codes,
        "trade_date": days,
        "continue_cnt": [1] * len(days),
        "seal_money": [1e8] * len(days),
        "limit_up_time": ["09:35"] * len(days),
    })
    # 收盘序列：ret[i] = close[i+1]/close[i] - 1 交替 ±1%，最后一天没有下一行
    closes = [10.0]
    for i in range(len(days) - 1):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 0.99))
    bars = pd.DataFrame({
        "ts_code": codes, "trade_date": days,
        "close": closes,
        "open": closes if open_all is None else [open_all] * len(days),
        "low": closes if low_all is None else [low_all] * len(days),
    })
    basic = pd.DataFrame({"ts_code": codes, "trade_date": days,
                          "circ_mv": [600000.0] * len(days)})   # 万元 → 60 亿
    return {"ths_limit_up": up, "daily": bars, "daily_basic": basic,
            "limit_up_pool": pd.DataFrame({
                "code": ["600000"], "open_times": [0.0],
                "first_seal_time": ["09:35:00"], "last_seal_time": ["09:35:00"],
                "seal_amount": [1e8], "close": [10.0]})}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path_factory):
    """清模块缓存；缓存文件指向临时目录，绝不写进 data/cache。"""
    board._PANEL.clear()
    board._BASE_EVENTS.clear()
    monkeypatch.setattr(board, "_write_cached", lambda day, frame: None)
    # 触板事件表的读写保持真函数，只把落点换成临时目录——挡住写的话，
    # "落盘能不能读回来"就永远测不到了。
    tmp = tmp_path_factory.mktemp("base_cache")
    monkeypatch.setattr(board, "_BASE_FILE", tmp / "base.parquet")
    monkeypatch.setattr(board, "_BASE_META", tmp / "base.json")
    yield
    board._PANEL.clear()
    board._BASE_EVENTS.clear()


def test_seal_window_boundaries():
    assert board._seal_bucket(9 * 60 + 30).startswith("1_")
    assert board._seal_bucket(10 * 60).startswith("1_")        # 10:00 整仍算早盘
    assert board._seal_bucket(10 * 60 + 1).startswith("2_")
    assert board._seal_bucket(11 * 60 + 30).startswith("2_")
    assert board._seal_bucket(13 * 60 + 5).startswith("3_")
    assert board._seal_bucket(14 * 60 + 1).startswith("4_")
    assert board._seal_bucket(15 * 60).startswith("4_")


def test_to_minute_rejects_junk():
    assert board._to_minute("09:35") == 575
    assert board._to_minute("14:56:21") == 896
    assert board._to_minute(None) is None
    assert board._to_minute("") is None
    assert board._to_minute("nan") is None


def test_stats_refuses_a_thin_bucket():
    got = board._stats(pd.Series([0.01] * (board.MIN_BUCKET_N - 1)), cost_bps=100)
    assert got["insufficient"] is True
    assert got["n"] == board.MIN_BUCKET_N - 1
    assert "mean" not in got, "样本不够时不能给出任何统计量"


def test_stats_carries_n_and_deducts_cost():
    values = pd.Series([0.02] * 40)
    got = board._stats(values, cost_bps=100)
    assert got["n"] == 40
    assert got["mean"] == pytest.approx(0.02)
    assert got["net_mean"] == pytest.approx(0.02 - 0.01)   # 100bps = 1%
    assert got["cost_bps"] == pytest.approx(100)


def test_stats_uses_trade_day_blocks_for_the_mean_interval():
    days = pd.Series([20260101 + index // 4 for index in range(40)])
    values = pd.Series([(index // 4 - 4.5) / 100 for index in range(40)])
    got = board._stats(values, cost_bps=100, days=days)
    assert got["n_days"] == 10
    assert got["ci_method"] == "day_block_bootstrap_event_weighted_mean"
    assert got["ci_status"] == "ok"
    assert all(value is not None for value in got["mean_ci95"])
    assert got["net_mean_ci95"][0] == pytest.approx(got["mean_ci95"][0] - 0.01, abs=1e-4)


def test_day_interval_uses_circular_contiguous_days_and_block_sensitivity():
    day_values = np.array([-0.04, -0.02, 0.03, 0.05, -0.01, 0.02, 0.04, -0.03])
    counts = np.array([1, 2, 1, 3, 2, 1, 2, 4])
    days = np.repeat(np.arange(20260101, 20260109), counts)
    values = np.repeat(day_values, counts)

    got = board._day_block_interval(pd.Series(values), pd.Series(days),
                                    reps=200, block=2)

    assert got["block_scheme"] == "circular"
    assert got["block_days"] == 2
    assert set(got["sensitivity"]) == {"1", "2", "5"}

    # Independent reference: four overlapping blocks of two adjacent calendar
    # groups, wrapping at the end. Event sums/counts preserve event weighting.
    starts = np.random.default_rng(20261115).integers(0, 8, size=(200, 4))
    indices = (starts[..., None] + np.arange(2)) % 8
    estimates = ((day_values * counts)[indices].sum(axis=(1, 2))
                 / counts[indices].sum(axis=(1, 2)))
    expected = np.quantile(estimates, [0.025, 0.975])
    assert got["ci95"] == pytest.approx(expected)


def test_stats_does_not_turn_one_crowded_day_into_forty_independent_events():
    got = board._stats(pd.Series([0.01] * 40), cost_bps=100,
                       days=pd.Series([20260101] * 40))
    assert got["n"] == 40
    assert got["n_days"] == 1
    assert got["ci_status"] == "insufficient_days_min_8"
    assert got["ci_block_days"] is None
    assert got["mean_ci95"] == [None, None]


def test_today_is_in_the_panel_even_without_a_forward_return():
    """当日涨停股还没有 T+1 收盘价，但它必须能被查到自己的封单。"""
    desk = _Desk(_frames())
    today = _sessions(41)[-1]
    out = board.profile(desk, "600000.SH", today)

    assert out["ok"] is True
    assert out["is_limit_up"] is True
    assert out["seal_money"] == pytest.approx(1e8)
    # 当日只有它一只涨停时，「在当日排第几」没有信息量，所以不给这个字段
    assert "seal_ratio_pctile_today" not in out
    assert "seal_ratio_pctile_hist" in out, "历史分位才是这里该有的分位"


def test_history_distribution_excludes_the_day_without_a_forward_return():
    """今天的 NaN 收益不能进分布，否则会被当成 0% 拉低均值。"""
    desk = _Desk(_frames())
    today = _sessions(41)[-1]
    out = board.profile(desk, "600000.SH", today)
    # 内核不给"该看哪一层"，所以取最紧那一级自己读
    chosen = out["conditional"]["ladder"][0]

    # 40 条历史样本，+1% 与 -1% 各二十条，均值应当正好是 0
    assert chosen["n"] == 40
    assert chosen["close"]["mean"] == pytest.approx(0.0, abs=1e-9)


def test_ladder_widens_and_always_reports_n():
    desk = _Desk(_frames())
    today = _sessions(41)[-1]
    ladder = board.profile(desk, "600000.SH", today)["conditional"]["ladder"]

    assert len(ladder) == 4
    ns = [step["n"] for step in ladder]
    assert ns == sorted(ns), "放宽条件后样本量只能变大"
    assert all("n" in step for step in ladder)


def test_ladder_degrades_to_null_when_even_the_loosest_bucket_is_thin():
    desk = _Desk(_frames(n_history=5))
    today = _sessions(6)[-1]
    got = board.profile(desk, "600000.SH", today)["conditional"]

    assert all(step.get("insufficient") for step in got["ladder"]), (
        "最宽的一档都不够 30 条时，每一级都只能说样本不足")
    assert all(step["insufficient"] for step in got["ladder"])


def test_a_non_limit_up_name_gets_no_distribution():
    """面板里有数据，但问的这只票不在涨停名单里——要说清楚，且不给分布。"""
    desk = _Desk(_frames())
    out = board.profile(desk, "000002.SZ", _sessions(41)[-1])

    assert out["ok"] is True
    assert out["is_limit_up"] is False
    assert "conditional" not in out
    assert "不在涨停名单" in out["note"]


def test_there_is_no_action_field_anywhere():
    """红线：内核不给建议。"""
    desk = _Desk(_frames())
    out = board.profile(desk, "600000.SH", _sessions(41)[-1])
    flat: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                flat.append(str(key).lower())
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(out)
    forbidden = {"action", "recommendation", "recommend", "should_buy", "signal",
                 "rating", "score", "verdict_buy"}
    assert not (forbidden & set(flat)), f"出现了判断字段：{forbidden & set(flat)}"


def _panel_row(open_: float, low: float, close: float) -> pd.DataFrame:
    """一行最小面板，只够 `_fillable` 判档用。"""
    return pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20260105"],
                         "open": [open_], "low": [low], "close": [close],
                         "fill_kind": ["one_word" if (open_ >= close * 0.999
                                                      and low >= close * 0.999)
                                       else ("opened_after_seal"
                                             if open_ >= close * 0.999
                                             else "sealed_intraday")]})


def test_fillable_reads_the_three_way_split_from_daily_bars():
    """可成交性只看开盘价与最低价相对涨停价的位置，不依赖东财池。

    涨停股的 ``close`` 就是涨停价，所以这三行覆盖了全部三种情况：
    """
    one_word = board._fillable(_panel_row(10.0, 10.0, 10.0), "600000.SH", "20260105")
    assert one_word["fill_shape"] == "sealed_all_day"
    assert "没有任何可成交时刻" in one_word["basis"]

    broken = board._fillable(_panel_row(10.0, 9.4, 10.0), "600000.SH", "20260105")
    assert broken["fill_shape"] == "opened_intraday"

    intraday = board._fillable(_panel_row(9.5, 9.4, 10.0), "600000.SH", "20260105")
    assert intraday["fill_shape"] == "sealed_before_limit"
    assert "封板之前就能买" in intraday["basis"]


def test_fillable_says_unknown_for_a_name_outside_the_panel():
    panel = _panel_row(9.5, 9.4, 10.0)
    got = board._fillable(panel, "000002.SZ", "20260105")
    assert got["fill_shape"] == "unknown"


def test_validate_refuses_when_one_side_is_empty():
    """所有样本都在 split 之前时，样本外为空——要说"不足"，不是给一个 0。"""
    desk = _Desk(_frames(n_history=400))
    today = _sessions(401)[-1]
    out = board.validate(desk, day=today, split=today)

    assert out["ok"] is False
    assert "不足" in out["error"]


def test_validate_pins_the_cuts_to_the_in_sample_side():
    """切点从样本内定；样本外一段不参与拟合。"""
    days = _sessions(1201)
    desk = _Desk(_frames(n_history=1200))
    out = board.validate(desk, day=days[-1], split=days[600])

    assert out["ok"] is True
    assert out["in_sample"]["rows"] > 0
    assert out["out_of_sample"]["rows"] > 0
    assert out["in_sample"]["rows"] + out["out_of_sample"]["rows"] == 1201
    assert out["buckets"], "至少应有一个桶"
    assert all("decay_pp" in row for row in out["buckets"])
    # 切点必须来自样本内：这里封单强度全相同，三分位切点应当收敛到同一个值
    cuts = out["tier_cuts_from_in_sample"]
    assert cuts["弱≤"] == cuts["中≤"]


def _base_frames(n_seal: int = 40, n_break: int = 20, *, with_prev: bool = True,
                 one_word: int = 0):
    """触板样本：封住的赚 2%，炸板的亏 5%，各给两行日线好让 shift(-1) 有下一根。

    ``low`` 决定这一笔"买不买得到"：低于涨停价才算能成交。默认全部可成交，
    ``one_word=n`` 把前 n 笔做成全天没跌破的一字（限价单排不到队）。
    """
    days = _sessions(n_seal + n_break + 2)
    pool_rows, bar_rows = [], []
    for i in range(n_seal + n_break):
        code = f"6{i:05d}.SH"
        sealed = i < n_seal
        signal_day, next_day = days[i], days[i + 1]
        row = {"trade_date": signal_day, "code": code,
               "pool": "limit_up" if sealed else "broken_board",
               "boards": 1 if sealed else 0, "limit_price": 10.0,
               # 前 one_word 笔全天没跌破涨停价
               "low": 10.0 if i < one_word else 9.5}
        if with_prev:
            row["prev_boards"] = 0
        pool_rows.append(row)
        bar_rows.append({"ts_code": code, "trade_date": signal_day,
                         "open": 10.0, "close": 10.0})
        bar_rows.append({"ts_code": code, "trade_date": next_day,
                         "open": 10.0 * (1.02 if sealed else 0.95),
                         "close": 10.0 * (1.02 if sealed else 0.95)})
    return {"limit_pool_t0": pd.DataFrame(pool_rows),
            "daily": pd.DataFrame(bar_rows)}


def test_base_rate_counts_the_boards_that_broke():
    """conditional 只含封住的样本；base_rate 必须把炸板那一边算进来。

    两个数合起来才是打板的期望：P(封住) × 封住之后的值 + (1−P) × 炸板之后的值。
    少了后一半，"3 板强封单 +2.91%"会被读成"打板能赚 2.91%"。
    """
    desk = _Desk(_base_frames(n_seal=40, n_break=20))
    out = board._base_rate(desk, 1, "20260301", cost_bps=92.0)

    assert out["available"] is True
    assert out["n"] == 60
    assert out["resealed"] == 40
    assert out["reseal_rate"] == pytest.approx(40 / 60, abs=1e-6)
    assert out["if_reseal"]["mean"] == pytest.approx(0.02, abs=1e-6)
    assert out["if_break"]["mean"] == pytest.approx(-0.05, abs=1e-6)
    # 门槛 = (成本 − 炸板均值) / (封住均值 − 炸板均值)
    assert out["required_reseal_rate"] == pytest.approx(
        (0.0092 + 0.05) / (0.02 + 0.05), abs=1e-6)
    # 实际回封率 66.7% 低于 84.6% 的门槛 → gap 为正，说的是"不够"
    assert out["gap_pp"] > 0


def test_base_rate_refuses_to_guess_when_prev_boards_is_missing():
    """老分区没有 prev_boards。用 boards 顶替会把"3 进 4 失败"记成"首板失败"。

    身位错了，回封率就按错的身位算——这比不给数更糟，因为它看起来像量过。
    """
    desk = _Desk(_base_frames(with_prev=False))
    out = board._base_rate(desk, 1, "20260301", cost_bps=92.0)

    assert out["available"] is False
    assert "prev_boards" in out["note"]


def test_base_rate_says_infeasible_instead_of_a_threshold_above_one():
    """门槛 >100% 不是"再准一点就行"，是"这个票面做不了"。

    一手 342 元的票，往返成本约 320bps（¥5 最低佣金占大头）。把它写成 102%
    会让读者以为差的是选股精度，其实差的是票面大小。
    """
    desk = _Desk(_base_frames(n_seal=40, n_break=20))
    out = board._base_rate(desk, 1, "20260301", cost_bps=1200.0)

    assert out["required_reseal_rate"] > 1.0
    assert out["feasible"] is False
    assert "覆盖不了" in out["reading"]


def test_setup_table_says_no_edge_when_the_base_rate_is_short():
    """门槛没到就是 no_edge——不给"可以考虑"这种中间态。

    中间态在盘口里会被读成"可以"。而这张表的用途恰恰是让"今天不做"有一个
    可复核的依据，不是给一个可以被修辞绕过去的措辞。
    """
    desk = _Desk(_base_frames(n_seal=40, n_break=20))
    table = board.setup_table(desk, "20260301", heights=(1,))

    row = table["rows"][0]
    assert row["available"] is True
    # 判词删了，判据还在：gap 为正 = 实际回封率低于这道门槛。
    assert row["gap_pp"] > 0
    assert "verdict" not in row, "代码不该替读者把判断做完"
    assert "成交概率" in table["still_missing"], "缺什么要写在表旁边"


def test_setup_table_says_has_edge_when_the_gap_closes():
    """反例必须能成立，否则 no_edge 只是一个永远响的告警。"""
    days = _sessions(62)
    pool_rows, bar_rows = [], []
    for i in range(60):
        code = f"6{i:05d}.SH"
        sealed = i < 40
        signal_day, next_day = days[i], days[i + 1]
        pool_rows.append({"trade_date": signal_day, "code": code,
                          "pool": "limit_up" if sealed else "broken_board",
                          "boards": 1 if sealed else 0, "prev_boards": 0,
                          "limit_price": 10.0, "low": 9.5})
        # 封住赚 30%、炸板只亏 1%：门槛会低到 3%，而回封率是 67%
        ret = 0.30 if sealed else -0.01
        bar_rows.append({"ts_code": code, "trade_date": signal_day,
                         "open": 10.0, "close": 10.0})
        bar_rows.append({"ts_code": code, "trade_date": next_day,
                         "open": 10.0 * (1 + ret), "close": 10.0 * (1 + ret)})
    desk = _Desk({"limit_pool_t0": pd.DataFrame(pool_rows),
                  "daily": pd.DataFrame(bar_rows)})

    row = board.setup_table(desk, "20260301", heights=(1,))["rows"][0]
    # 反例：门槛被跨过去了，gap 转负。结论由读者下，读数在这里。
    assert row["gap_pp"] < 0
    assert "verdict" not in row


class _Book:
    def summary(self):
        return {"equity": 5000.0}


class _DeskWithBook(_Desk):
    """带账户的 desk：让 setup_table 走到"按实际票面算成本"那条分支。

    没有这一层，测试永远在 ticket=0、cost=0 的路上跑——线上那一版
    `Instrument(asset=...)` 的参数错误就是这样漏过去的。
    """

    def __init__(self, frames, last="20260301"):
        super().__init__(frames, last)
        self.book = _Book()

    def limits(self):
        return {"max_core_single": 0.30, "max_positions": 3}


def test_setup_table_prices_the_account_not_a_constant():
    desk = _DeskWithBook(_base_frames(n_seal=40, n_break=20))
    table = board.setup_table(desk, "20260301", heights=(1,))

    # 典型仓位 min(5000×30%, 5000/3) = 1500；不是"一手 1000 元"
    assert table["ticket_cny"] == 1500.0
    assert table["cost_bps"] > 0, "成本必须真的算出来，不能是兜底的 0"


def test_base_events_survive_a_restart_through_the_disk_cache(tmp_path, monkeypatch):
    """触板事件表要能跨重启复用：冷算 3.5 秒，而服务重启可能就在 09:20。

    不落盘的话，看门狗在早盘重启一次，第一次 topic=board 就付这 3.5 秒，
    正好卡在 09:26 的十五分钟窗口里。
    """
    frame = pd.DataFrame({"attempt": [1, 1], "resealed": [True, False],
                          "ret_close": [0.02, -0.05], "buyable": [True, True]})
    monkeypatch.setattr(board, "_BASE_FILE", tmp_path / "base.parquet")
    monkeypatch.setattr(board, "_BASE_META", tmp_path / "base.json")

    board._write_base_cached("20260301", frame)
    got = board._read_base_cached("20260301")

    assert got is not None and len(got) == 2
    assert board._read_base_cached("20260302") is None, "日期不符不能拿来用"


def test_a_cache_from_an_older_schema_is_discarded(tmp_path, monkeypatch):
    """少一列的旧缓存读起来像"这个维度不支持"，不会报错——所以必须按 schema 挡。"""
    thin = pd.DataFrame({"attempt": [1], "ret_close": [0.02]})   # 缺 resealed
    monkeypatch.setattr(board, "_BASE_FILE", tmp_path / "base.parquet")
    monkeypatch.setattr(board, "_BASE_META", tmp_path / "base.json")

    thin.to_parquet(tmp_path / "base.parquet", index=False)
    (tmp_path / "base.json").write_text('{"day": "20260301", "rows": 1}', encoding="utf-8")

    assert board._read_base_cached("20260301") is None


def _skewed_frames(one_word_sealed: int = 80, buyable_sealed: int = 6,
                   buyable_broken: int = 14):
    """全样本好看、买得到的那组很难看——这正是真实数据的形状。

    一字（全天没跌破涨停价）按构造就是回封的，占比越大，全样本回封率越高；
    而它们恰恰是限价单排不到队的那批。真实数据里一字占比从首板 2.1% 升到
    7 板 28.6%，于是"回封率随身位上升"完全是这一组抬上去的。
    """
    total = one_word_sealed + buyable_sealed + buyable_broken
    days = _sessions(total + 2)
    pool_rows, bar_rows = [], []
    for i in range(total):
        code = f"6{i:05d}.SH"
        if i < one_word_sealed:
            sealed, low = True, 10.0          # 一字：没跌破
        elif i < one_word_sealed + buyable_sealed:
            sealed, low = True, 9.5           # 跌破后回封
        else:
            sealed, low = False, 9.5
        signal_day, next_day = days[i], days[i + 1]
        pool_rows.append({"trade_date": signal_day, "code": code,
                          "pool": "limit_up" if sealed else "broken_board",
                          "boards": 1 if sealed else 0, "prev_boards": 0,
                          "limit_price": 10.0, "low": low})
        ret = 0.02 if sealed else -0.05
        bar_rows.append({"ts_code": code, "trade_date": signal_day,
                         "open": 10.0, "close": 10.0})
        bar_rows.append({"ts_code": code, "trade_date": next_day,
                         "open": 10.0 * (1 + ret), "close": 10.0 * (1 + ret)})
    return {"limit_pool_t0": pd.DataFrame(pool_rows),
            "daily": pd.DataFrame(bar_rows)}


def test_base_rate_splits_the_boards_you_can_buy_from_the_ones_you_cannot():
    """一字组回封率按构造是 100%，不能和买得到的那组合成一个数。

    这是 2026-11-03 上线 setup 表时犯的错：全样本回封率随身位上升，于是"打高位板"
    看起来更接近门槛。而涨幅全部来自买不到的一字——7 板一字占 28.6%，首板 2.1%。
    """
    desk = _Desk(_skewed_frames())
    out = board._base_rate(desk, 1, "20260301", cost_bps=92.0)

    assert out["n"] == 100
    assert out["reseal_rate"] == pytest.approx(0.86, abs=1e-6), "全样本确实好看"

    assert out["one_word"]["n"] == 80
    assert out["one_word"]["reseal_rate"] == pytest.approx(1.0), "一字按构造恒为 100%"
    assert out["buyable"]["n"] == 20
    assert out["buyable"]["reseal_rate"] == pytest.approx(0.30, abs=1e-6)
    assert out["buyable_share"] == pytest.approx(0.20, abs=1e-6)
    assert out["one_word_share"] == pytest.approx(0.80, abs=1e-6)
    assert "排队优先权" in out["one_word"]["note"]


def test_setup_table_gap_follows_what_you_can_actually_buy():
    """全样本 86% 回封率、买得到只有 30%——gap 必须按后者算。

    如果按全样本判，这张表会在一个"买到的全是炸板"的身位上写 has_edge。
    那比不给数更糟：它把一个反面证据包装成入场许可。
    """
    desk = _DeskWithBook(_skewed_frames())
    table = board.setup_table(desk, "20260301", heights=(1,))

    row = table["rows"][0]
    assert row["reseal_rate"] == pytest.approx(0.30, abs=1e-6), "决策口径是 buyable"
    assert row["reseal_rate_all"] == pytest.approx(0.86, abs=1e-6), "全样本留着对账"
    assert row["n_buyable"] == 20
    assert row["gap_pp"] > 0, "30% 的回封率远低于门槛"
    assert "verdict" not in row
    assert "buyable" in table["basis"]


def test_pool_style_six_digit_symbol_is_the_same_board_fact_as_ts_code():
    frames = _frames(4)
    day = frames["ths_limit_up"]["trade_date"].max()
    short = board.profile(_Desk(frames), "600000", day=day)
    full = board.profile(_Desk(frames), "600000.SH", day=day)
    assert short["ok"] is True and short["is_limit_up"] is True
    assert short["symbol"] == full["symbol"] == "600000.SH"
    assert short["close"] == full["close"]
    assert board.profile(_Desk(frames), "not-a-symbol", day=day)["status"] == "bad_symbol"


def test_setup_cost_uses_the_live_aggressive_sleeve_and_labels_scenario(monkeypatch):
    desk = _DeskWithBook(_skewed_frames())
    desk.paper = object()
    desk.limits = lambda: {
        "max_core_single": 0.30,
        "max_aggressive_single": 0.50,
        "max_positions": 3,
    }
    monkeypatch.setattr("service.trading.persona.current",
                        lambda paper: {"open_sleeves": ["AGGRESSIVE"]})
    table = board.setup_table(desk, "20260301", heights=(1,))
    assert table["sizing"]["cap_key"] == "max_aggressive_single"
    assert table["sizing"]["scenario_only"] is True
    assert table["ticket_cny"] > 1500.0
    assert "实际委托" in table["sizing"]["note"]
