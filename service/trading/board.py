"""封板结构：这只票今天的板长什么样，同结构的票历史上之后怎么走。

内核在这里只做一件事：把「涨停」这个二元事实拆成可比较的结构，并给出同结构
样本的前向分布。它不说该不该买——``action`` 字段不存在，也不该存在。

三个口径必须分开，混在一起会得出互相矛盾的数：

1. **结构**（板数 / 封单强度 / 首封时段）决定前向分布的形状。2021-2026 的
   90,013 个涨停样本里，3 板 × 强封单组次日收益均值 +2.91%、弱封单组 +0.70%，
   而「全部涨停」只有一个数，两个极端都被它抹平了。
2. **成交可行性**决定这个分布能不能被吃到。全天封死买不到，开过板才买得到。
   日线的 ``low`` 判不了这件事——它包含封板之前的成交，几乎总是低于涨停价——
   所以只有东财的 ``open_times`` 能给，而它只回溯约 20 个交易日。这一维缺数据
   时返回 ``unknown``，不用日线猜。
3. **买入锚点**决定收益从哪算起。这里固定用 T 日收盘价（= 涨停价）买入、
   下一交易日收盘卖出。用「次日开盘买入」测出来的是另一个动作的期望
   （实测 -0.05%），不能拿来回答「打板赚不赚钱」。

分桶阈值是研究产物，不在 ``configs/params.yaml``：它们会随分布漂移而调整，
写进配置会让人以为可以随手改。这与 ``persona.REGISTRY`` 的处理一致。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from service.adapters.symbols import ts_code as canonical_ts_code
from service.kernel.clock import SHANGHAI

from .impact import round_trip_cost_bps

# 桶内样本少于此值就不给统计量。30 是 ``research.min_bucket_n`` 的同一条线：
# 更少的样本算出来的中位数与 p10 只是噪声，而带 n 的噪声最危险——它看起来
# 像是被测量过。
MIN_BUCKET_N = 30

# 首封时段。分界不是均匀切的：09:30-10:00 封板的票占了 49%（44,079/90,013），
# 而且次日收益比尾盘封板高 1.8 个百分点，值得单独一档。
SEAL_WINDOWS: tuple[tuple[int, str], ...] = (
    (600, "1_开盘09:30-10:00"),
    (690, "2_上午10:00-11:30"),
    (840, "3_午后13:00-14:00"),
    (10_000, "4_尾盘14:00后"),
)

# 属性与状态类标签不是题材。``ST板块`` 出现 6,331 次、``新股`` 595 次，
# 它们描述的是这只票的**身份**不是它今天为什么涨；``成交量创历史新高`` /
# ``超500只个股涨停`` 描述的是**市场**不是票。混进来会把"今天全市场都在涨"
# 算成一个有 500 只成员的题材，然后得出"共振极强"的结论。
#
# 财报季标签（一季报/中报/三季报/年报增长）同样要剔除，理由不是它们不重要，
# 而是它们**季节性地一起出现**：2026-11-02 有 22 只涨停挂着"三季报增长"，
# 那不是 22 笔资金在抱同一个逻辑，是所有人都在同一周发财报。留着一个 22 只的
# 簇，会让当天每一只带这个标签的票都被读成"板块共振极强"。
NOISE_TAGS = frozenset({
    "ST板块", "新股", "次新股", "小盘股", "其它", "其他", "举牌", "摘帽",
    "扭亏", "业绩预增", "业绩增长", "业绩超预期", "成交量创历史新高",
    "成交额创历史新高", "超100只个股涨停", "超200只个股涨停",
    "超300只个股涨停", "超500只个股涨停",
    "一季报增长", "中报增长", "三季报增长", "年报增长", "季报增长",
    "一季报减亏", "中报减亏", "三季报减亏", "年报减亏", "季报减亏",
    "一季报扭亏", "中报扭亏", "三季报扭亏", "年报扭亏", "季报扭亏",
    "融资融券", "深股通", "沪股通", "MSCI", "标普道琼斯", "富时罗素", "转融券",
})

# 题材簇的分档。2-3 只与独苗的差别小到可以忽略（+0.10pp），真正的跃升在
# 4 只：1 板样本里 4 只+ 比独苗高 0.64pp，控制封单强度后每一档仍然高 0.5-0.8pp。
CLUSTER_BUCKETS: tuple[tuple[int, str], ...] = ((1, "1只"), (3, "2-3只"), (10**6, "4只+"))

# 回看多少自然日。1500 约等于 6 年，覆盖 ths_limit_up 的全部历史（2020-12 起）。
LOOKBACK_DAYS = 2200

# 一天一次的面板重算：全市场 6 年 join 约 11 秒，模型会连着问好几只票。
_PANEL: dict[str, pd.DataFrame] = {}

# 触板事件表（含炸板）同样按日冻结。实测冷算 4.8 秒、11.9 万行——不缓存的话
# 每问一只票就付一次，而 board 是个会被连着问的工具。
_BASE_EVENTS: dict[str, pd.DataFrame] = {}

# 面板按日冻结，所以它和 discoveries 一样值得落盘：首次约 11 秒，之后再问
# 任何一只票都是毫秒级。写盘失败不阻断回答——缓存是加速，不是依赖。
_ROOT = Path(__file__).resolve().parents[2]
_CACHE_DIR = _ROOT / "data" / "cache"
_PANEL_FILE = _CACHE_DIR / "board_panel.parquet"
_PANEL_META = _CACHE_DIR / "board_panel.json"
# 触板事件表也落盘：它要读 6 年日线（3.5 秒），而服务被看门狗重启的时刻
# 完全可能在早盘——那时第一次 topic=board 就付这 3.5 秒，正好卡在 09:26 的
# 十五分钟窗口里。写盘之后重启也只花一次小 parquet 的读。
_BASE_FILE = _CACHE_DIR / "board_base_events.parquet"
_BASE_META = _CACHE_DIR / "board_base_events.json"
_BASE_SCHEMA = {"attempt", "resealed", "ret_close", "buyable"}


def _read_base_cached(day: str) -> pd.DataFrame | None:
    if not (_BASE_FILE.exists() and _BASE_META.exists()):
        return None
    try:
        meta = json.loads(_BASE_META.read_text(encoding="utf-8"))
        if str(meta.get("day")) != str(day):
            return None
        frame = pd.read_parquet(_BASE_FILE)
    except (OSError, ValueError, KeyError):
        return None
    # 结构变过就丢弃：旧缓存少一列，读起来像"这个维度不支持"，不会报错。
    if not _BASE_SCHEMA <= set(frame.columns):
        return None
    return frame if len(frame) else None


def _write_base_cached(day: str, frame: pd.DataFrame) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _BASE_FILE.with_name(_BASE_FILE.name + ".tmp")
        frame.to_parquet(tmp, index=False)
        os.replace(tmp, _BASE_FILE)
        _BASE_META.write_text(
            json.dumps({"day": str(day), "rows": len(frame),
                        "built_at": datetime.now(SHANGHAI).isoformat(timespec="seconds")}),
            encoding="utf-8")
    except OSError:
        pass


def _read_cached(day: str) -> pd.DataFrame | None:
    if not (_PANEL_FILE.exists() and _PANEL_META.exists()):
        return None
    try:
        meta = json.loads(_PANEL_META.read_text(encoding="utf-8"))
        if str(meta.get("day")) != day:
            return None
        frame = pd.read_parquet(_PANEL_FILE)
    except (OSError, ValueError, KeyError):
        return None
    # 面板结构变过（加入 ret_open / theme_cluster）时，旧缓存不能再用：它会让
    # exit.open 或 theme 整块消失，而缺失读起来像"这个结构不支持这个维度"。
    if not {"ret_open", "theme_cluster", "cluster_bucket", "fill_kind",
            "excess_close_1", "excess_close_2", "next2_close"} <= set(frame.columns):
        return None
    return frame if len(frame) else None


def _write_cached(day: str, frame: pd.DataFrame) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _PANEL_FILE.with_name(_PANEL_FILE.name + ".tmp")
        frame.to_parquet(tmp, index=False)
        os.replace(tmp, _PANEL_FILE)
        _PANEL_META.write_text(
            json.dumps({"day": day, "rows": len(frame),
                        "built_at": datetime.now(SHANGHAI).isoformat(timespec="seconds")},
                       ensure_ascii=False),
            encoding="utf-8")
    except (OSError, ValueError):
        pass


def _f(value, digits: int = 6) -> float | None:
    """JSON 安全的浮点：NaN/inf 一律 null，不是 0。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return round(number, digits)


def _seal_bucket(minute: int) -> str:
    for edge, name in SEAL_WINDOWS:
        if minute <= edge:
            return name
    return SEAL_WINDOWS[-1][1]


def _to_minute(stamp) -> float | None:
    """``HH:MM`` / ``HH:MM:SS`` → 距 00:00 的分钟数。"""
    text = str(stamp or "").strip()
    if len(text) < 4 or ":" not in text:
        return None
    try:
        hour, minute = text.split(":")[:2]
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return None


def seal_window(stamp) -> str | None:
    """封板时间 → 时段标签。

    公开出来是给 API 层复用的：分界只在 ``SEAL_WINDOWS`` 里写一次，池子列表
    与封板体检用的必须是同一套档位，否则两处的"早盘封板"不是同一批票。
    """
    minute = _to_minute(stamp)
    return _seal_bucket(int(minute)) if minute is not None else None


def split_seal_window(label: str | None) -> tuple[int | None, str | None]:
    """``"1_开盘09:30-10:00"`` → ``(1, "开盘09:30-10:00")``。

    序号写进 ``SEAL_WINDOWS`` 的名字里，是为了历史面板按字符串排序时天然
    按时段先后——那是内部编码。接口把它原样吐出去以后，前端得靠正则剥前缀，
    "给机器读的编码让人来剥"方向反了（docs/14 ... A2）。原 ``seal_window``
    字段保持不动，新消费者用拆开的两个字段。
    """
    text = str(label or "")
    no_text, sep, rest = text.partition("_")
    if sep and no_text.isdigit():
        return int(no_text), (rest or None)
    return None, (text or None)


def session_returns(bars: pd.DataFrame) -> pd.DataFrame:
    """连续交易日的前瞻收益与全市场等权基准。

    这是短线研究与样本外结算唯一的收益标签实现。``trade_date`` 是信号日 T；
    除原 T 收盘锚点外，还保留 T+1 开盘到 T+1/T+2 收盘的可执行评价腿。
    缺出口、非连续交易日或检测到除权跳变均为 NaN，绝不按 0 收益入账。
    """
    bars = bars.copy()
    bars["trade_date"] = bars["trade_date"].astype(str)
    bars = bars.sort_values(["ts_code", "trade_date"])
    if "pre_close" not in bars.columns:
        bars["pre_close"] = np.nan
    grouped = bars.groupby("ts_code")
    bars["next_close"] = grouped["close"].shift(-1)
    bars["next_open"] = grouped["open"].shift(-1)
    bars["next_trade_date"] = grouped["trade_date"].shift(-1)
    bars["next_pre_close"] = grouped["pre_close"].shift(-1)
    bars["next2_close"] = grouped["close"].shift(-2)
    bars["next2_trade_date"] = grouped["trade_date"].shift(-2)
    bars["next2_pre_close"] = grouped["pre_close"].shift(-2)

    sessions = sorted(bars["trade_date"].dropna().unique())
    next_session = {day: sessions[i + 1] for i, day in enumerate(sessions[:-1])}
    next2_session = {day: sessions[i + 2] for i, day in enumerate(sessions[:-2])}
    close_now = pd.to_numeric(bars["close"], errors="coerce")
    close_next = pd.to_numeric(bars["next_close"], errors="coerce")
    open_next = pd.to_numeric(bars["next_open"], errors="coerce")
    pre_next = pd.to_numeric(bars["next_pre_close"], errors="coerce")
    pre_next2 = pd.to_numeric(bars["next2_pre_close"], errors="coerce")
    pre1_ok = pre_next.isna() | np.isclose(pre_next, close_now, rtol=0, atol=0.005)
    pre2_ok = pre_next2.isna() | np.isclose(pre_next2, close_next, rtol=0, atol=0.005)
    valid1 = (bars["next_trade_date"].eq(bars["trade_date"].map(next_session))
              & pre1_ok & close_now.gt(0))
    valid2 = (valid1
              & bars["next2_trade_date"].eq(bars["trade_date"].map(next2_session))
              & pre2_ok & close_next.gt(0))
    bars["session_ret_open_1"] = np.where(
        valid1, open_next / close_now - 1.0, np.nan)
    bars["session_ret_close_1"] = np.where(
        valid1, close_next / close_now - 1.0, np.nan)
    bars["session_ret_close_2"] = np.where(
        valid2, pd.to_numeric(bars["next2_close"], errors="coerce") / close_now - 1.0,
        np.nan)
    # T 收盘后才能生成信号，所以实际可执行评价从 T+1 开盘重新起算。
    open_valid = valid1 & open_next.gt(0)
    bars["session_ret_open_to_close_1"] = np.where(
        open_valid, close_next / open_next - 1.0, np.nan)
    bars["session_ret_open_to_close_2"] = np.where(
        valid2 & open_next.gt(0),
        pd.to_numeric(bars["next2_close"], errors="coerce") / open_next - 1.0,
        np.nan)
    for column in ("session_ret_open_1", "session_ret_close_1", "session_ret_close_2",
                   "session_ret_open_to_close_1", "session_ret_open_to_close_2"):
        market = bars.groupby("trade_date")[column].mean()
        bars["market_" + column.removeprefix("session_")] = bars["trade_date"].map(market)
    return bars


def _panel(desk, day: str) -> pd.DataFrame | None:
    """涨停事件面板：T 日结构 + T 日收盘 + 下一交易日收盘。

    只用同花顺的 ``ths_limit_up``，因为它有 1,434 个交易日（2020-12 起）而东财
    的涨停池只有约 20 天。分布要的是历史，历史只有这一份。

    ``seal_ratio`` 用 ``seal_money / circ_mv`` 而不是绝对封单额：2026 年的
    198 个交易日里，绝对额的前五与强度的前五只有 34% 的重合，从来没完全一致过。
    1.15 亿的封单压在 246 亿流通盘上是全市场最弱的一档，6.10 亿压在 54.6 亿上
    是最强的一档——绝对额回答的是市值问题，不是强度问题。
    """
    key = day
    hit = _PANEL.get(key)
    if hit is None:
        hit = _read_cached(day)
        if hit is not None:
            _PANEL[key] = hit
    if hit is not None:
        return hit if len(hit) else None

    start = (datetime.strptime(day, "%Y%m%d")
             - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    try:
        up = desk.data.read("ths_limit_up", start, day,
                            columns=["ts_code", "trade_date", "continue_cnt",
                                     "seal_money", "limit_up_time", "reason"])
        basic = desk.data.read("daily_basic", start, day,
                               columns=["ts_code", "trade_date", "circ_mv"])
        bars = desk.data.read("daily", start, day,
                              columns=["ts_code", "trade_date", "close", "open", "low",
                                       "pre_close"])
    except (KeyError, ValueError, OSError):
        return None
    if up is None or up.empty or bars is None or bars.empty:
        return None

    up = up.copy()
    up["trade_date"] = up["trade_date"].astype(str)
    bars = bars.copy()
    bars["trade_date"] = bars["trade_date"].astype(str)

    # 收益标签与样本外结算共用一份实现；ret / ret_open 保持历史兼容。
    bars = session_returns(bars)

    frame = up.merge(bars[["ts_code", "trade_date", "close", "open", "low",
                           "next_close", "next_open", "next2_close",
                           "session_ret_open_1", "session_ret_close_1", "session_ret_close_2",
                           "market_ret_open_1", "market_ret_close_1", "market_ret_close_2"]],
                     on=["ts_code", "trade_date"], how="left")
    if basic is not None and not basic.empty:
        basic = basic.copy()
        basic["trade_date"] = basic["trade_date"].astype(str)
        frame = frame.merge(basic, on=["ts_code", "trade_date"], how="left")
    else:
        frame["circ_mv"] = np.nan

    frame["circ_mv"] = pd.to_numeric(frame["circ_mv"], errors="coerce")
    frame["seal_money"] = pd.to_numeric(frame["seal_money"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["next_close"] = pd.to_numeric(frame["next_close"], errors="coerce")
    for leg in ("open_1", "close_1", "close_2"):
        frame[f"excess_{leg}"] = (pd.to_numeric(frame[f"session_ret_{leg}"], errors="coerce")
                                  - pd.to_numeric(frame[f"market_ret_{leg}"], errors="coerce"))

    # circ_mv 的单位是万元（Tushare 口径），乘 10,000 换元。这个换算只允许在
    # ``MarketRepository.circ_mv`` 之外出现这一次，且必须写出来——它曾经在两个
    # 模块里各写一遍，其中一份漂了之后数字差四个数量级而两边都不报错。
    frame["seal_ratio"] = frame["seal_money"] / (frame["circ_mv"] * 10_000.0)
    frame.loc[frame["circ_mv"] <= 0, "seal_ratio"] = np.nan

    frame["minute"] = frame["limit_up_time"].map(_to_minute)
    frame["seal_window"] = frame["minute"].map(
        lambda m: _seal_bucket(int(m)) if pd.notna(m) else None)
    frame["boards"] = pd.to_numeric(frame["continue_cnt"], errors="coerce")
    frame["boards_bucket"] = frame["boards"].clip(lower=1, upper=5)

    # 可成交性三分法：只看开盘价与最低价相对涨停价的位置。
    #
    # 这是日线**唯一**能严格说清的一维，而且它比东财的 ``open_times`` 好三点：
    # 覆盖 2010 年至今（不是 20 天）、不依赖第二家数据源、分出来的三档含义不同。
    #
    #   一字        open = low = close = 涨停价   开盘即封、全天没离开 → 买不到
    #   开盘封后开板  open = 涨停价，low < 涨停价  开盘封住但被砸开过 → 开板瞬间可买
    #   盘中封板    open < 涨停价                 涨停是盘中封上的 → **封板之前就能买**
    #
    # 2021-2026 实测占比与次日开盘卖：一字 8.9%（+6.74%，p10 +0.61%）、
    # 开盘封后开板 5.3%（+1.80%，p10 -4.58%）、盘中封板 85.8%（+1.87%，p10 -2.29%）。
    #
    # 两个反直觉的结论都在里面：最好的那批（一字，p10 都是正的）恰好买不到；
    # 而八成半的涨停票根本不需要排队——它们在封板之前是可以买的，"打板必须
    # 排队"只对 A/B 两类成立。所以"能不能买到"这个问题，对 85.8% 的样本问错了。
    frame["fill_kind"] = np.where(
        frame["open"] >= frame["close"] * 0.999,
        np.where(frame["low"] >= frame["close"] * 0.999,
                 "one_word", "opened_after_seal"),
        "sealed_intraday")

    # 题材簇：同一天里，同一标签下有几只涨停（含自己）。
    #
    # 这是「日 × 题材」级别的变量，对同一天同题材的票是共享的——所以它不是
    # 独立特征，不能按 n 算显著性。它的作用是把"孤立涨停"和"板块共振"分开：
    # 2021-2026 年，控制板数与封单强度之后，4 只以上同题材仍比独苗高 0.5-0.8pp
    # （1 板 × 弱封单：独苗 +0.68% / 4只+ +1.20%）。
    #
    # 标签来自同花顺的涨停原因，``NOISE_TAGS`` 剔除身份类与市场状态类，
    # 否则"超500只个股涨停"会被算成一个 500 只成员的题材。
    if "reason" in frame.columns:
        exploded = frame.assign(
            tag=frame["reason"].astype(str).str.split("+")).explode("tag")
        exploded["tag"] = exploded["tag"].astype(str).str.strip()
        exploded = exploded[(exploded["tag"].str.len() > 0)
                            & (~exploded["tag"].isin(NOISE_TAGS))]
        if len(exploded):
            size = (exploded.groupby(["trade_date", "tag"]).size()
                    .rename("cluster_size").reset_index())
            exploded = exploded.merge(size, on=["trade_date", "tag"], how="left")
            best = (exploded.groupby(["ts_code", "trade_date"])["cluster_size"]
                    .max().rename("theme_cluster").reset_index())
            frame = frame.merge(best, on=["ts_code", "trade_date"], how="left")
        else:
            frame["theme_cluster"] = np.nan
    else:
        frame["theme_cluster"] = np.nan
    frame["cluster_bucket"] = pd.cut(
        frame["theme_cluster"],
        [0] + [edge for edge, _ in CLUSTER_BUCKETS[:2]] + [10**6],
        labels=[name for _, name in CLUSTER_BUCKETS])

    # 收益口径写死在这里：T 日收盘买入。卖出有两条腿，两条都给——
    # ``ret_close`` 是 T+1 收盘卖，``ret_open`` 是 T+1 开盘卖。
    #
    # 只报收盘卖会把这个结构读得比它实际差：3 板 × 强封单 × 早盘封 的 2,070 个
    # 样本里，开盘卖的 p10 是 -0.64%、收盘卖是 -8.38%，均值差 1.18pp。尾部风险
    # 几乎全部来自"高开之后又跌回去"的那一段，而开盘卖不吃那一段。
    frame["ret"] = frame["next_close"] / frame["close"] - 1.0
    frame["ret_open"] = frame["next_open"] / frame["close"] - 1.0

    # 不丢掉没有次日收益的行：当日涨停股本来就还没有 T+1 收盘价，而它恰恰是
    # 「今天这一只」要拿来和历史比的对象。此前这里 dropna，结果模型问当天这只
    # 票时，它在面板里根本不存在，封单额只能回退到另一个源、当日分位也消失了。
    # 统计由 ``_stats`` 按 ret 非空过滤，所以这些行不会污染分布。
    frame = frame[frame["close"].notna()].copy()
    frame = frame[frame["seal_ratio"].notna() | frame["boards"].notna()]

    _PANEL.clear()
    _PANEL[key] = frame
    _write_cached(day, frame)
    return frame if len(frame) else None


def realized_forward(desk, day: str, symbol: str, *, upto: str | None = None) -> dict:
    """``day`` 那天这只票走到三个出口时的**实际超额**（毛，未扣成本）。

    ``_panel(day)`` 只把K线读到 ``day`` 为止，所以 ``day`` 自己的 T+1/T+2 列永远是
    NaN——要结算一天前的信号，面板必须用更晚的 ``upto`` 去建。这一点写在这里，
    而不是让每个调用方各自去猜该传哪一天。

    返回的三个数是 ``session_ret - market_ret``：与 ``short_alpha`` 候选证据用的
    是同一条腿、同一个全市场等权基准、同一套除权与连续交易日守卫。缺一个出口就是
    ``None``——数据没到，不是 0。
    """
    panel = _panel(desk, upto or desk.market.latest_bar_date())
    if panel is None or panel.empty:
        return {"ok": False, "error": "board_panel_unavailable", "day": day}
    row = panel[(panel["ts_code"] == symbol) & (panel["trade_date"] == day)]
    if row.empty:
        return {"ok": False, "error": "event_not_in_panel", "day": day,
                "symbol": symbol}
    row = row.iloc[-1]
    legs = {leg: _f(row.get(f"excess_{leg}")) for leg in ("open_1", "close_1", "close_2")}
    return {
        "ok": True,
        "day": day,
        "symbol": symbol,
        "upto": upto or desk.market.latest_bar_date(),
        "close": _f(row.get("close")),
        "next_open": _f(row.get("next_open")),
        "next_close": _f(row.get("next_close")),
        "next2_close": _f(row.get("next2_close")),
        "gross_excess": legs,
        "complete": all(v is not None for v in legs.values()),
    }


def _day_block_interval(values: pd.Series, days: pd.Series | None,
                        *, reps: int = 500, block: int = 2) -> dict:
    """按连续交易日块 bootstrap；保留事件权重并显式给块长敏感性。

    独立抽日会切断 T+2 收益的跨日重叠。环形相邻日块保留这段相关结构，
    同时让每个交易日都有相同的区块起点概率；`sum / count` 继续维持事件权重。
    """
    if days is None:
        return {"ci95": [None, None], "n_days": None,
                "method": "day_block_bootstrap_event_weighted_mean",
                "status": "trade_date_unavailable", "block_scheme": "circular",
                "block_days": None, "sensitivity": {}}
    aligned_days = days if isinstance(days, pd.Series) else pd.Series(days, index=values.index)
    clean = pd.to_numeric(values, errors="coerce")
    day_values = pd.to_numeric(aligned_days.reindex(values.index), errors="coerce")
    valid = clean.notna() & day_values.notna()
    grouped = (pd.DataFrame({"value": clean[valid].astype(float),
                             "day": day_values[valid]})
               .groupby("day", sort=True)["value"].agg(["sum", "count"]))
    n_days = len(grouped)
    block = max(1, min(int(block), n_days)) if n_days else 1
    result = {"n_days": n_days, "method": "day_block_bootstrap_event_weighted_mean",
              "reps": reps, "block_scheme": "circular",
              "block_days": block if n_days >= 8 else None}
    if n_days < 8:
        return {**result, "ci95": [None, None], "status": "insufficient_days_min_8",
                "sensitivity": {}}

    day_sums = grouped["sum"].to_numpy(dtype=float)
    day_counts = grouped["count"].to_numpy(dtype=float)

    def estimate(block_days: int) -> np.ndarray:
        n_blocks = (n_days + block_days - 1) // block_days
        starts = np.random.default_rng(20261115).integers(
            0, n_days, size=(reps, n_blocks))
        indices = (starts[..., None] + np.arange(block_days)) % n_days
        sums = day_sums[indices].sum(axis=(1, 2))
        counts = day_counts[indices].sum(axis=(1, 2))
        return sums / counts

    estimates = estimate(block)
    lengths = sorted({1, 2, 5, 10, block} & set(range(1, n_days + 1)))
    sensitivity = {}
    for length in lengths:
        draws = estimates if length == block else estimate(length)
        sensitivity[str(length)] = [float(np.quantile(draws, 0.025)),
                                    float(np.quantile(draws, 0.975))]
    return {**result, "ci95": [float(np.quantile(estimates, 0.025)),
                               float(np.quantile(estimates, 0.975))],
            "status": "ok", "sensitivity": sensitivity}


def _stats(values: pd.Series, cost_bps: float, days: pd.Series | None = None) -> dict:
    """一个桶的分布；均值区间按交易日整块重采样。"""
    clean = pd.to_numeric(values, errors="coerce").dropna()
    n = len(clean)
    if n < MIN_BUCKET_N:
        return {"n": n, "insufficient": True,
                "why": f"样本 {n} < {MIN_BUCKET_N}，统计量不可靠，不给数"}
    mean = float(clean.mean())
    interval = _day_block_interval(clean, days)
    primary_ci = interval["ci95"]
    sensitivity = interval.get("sensitivity", {})
    if sensitivity:
        # Model-choice uncertainty must not promote a candidate merely because
        # one block length is favourable (alternating day returns make a 2-day
        # block artificially narrow). The legacy gate reads this conservative
        # envelope; the primary adjacent-block estimate remains separately visible.
        ci = [min(values[0] for values in sensitivity.values()),
              max(values[1] for values in sensitivity.values())]
        interval_rule = "minmax_block_sensitivity_envelope"
    else:
        ci = primary_ci
        interval_rule = "primary_circular_day_block"
    cost = cost_bps / 10_000.0
    return {
        "n": n,
        "insufficient": False,
        "mean": _f(mean),
        "median": _f(clean.median()),
        "p10": _f(clean.quantile(0.10)),
        "p90": _f(clean.quantile(0.90)),
        "win_rate": _f((clean > 0).mean(), 4),
        # 成本按本账户这一笔的实算值扣。net 为负不代表这个结构一定亏——
        # 它只说明在这个票面上，均值吃不掉往返费用。
        "net_mean": _f(mean - cost),
        "mean_ci95": [None if ci[0] is None else _f(ci[0]),
                      None if ci[1] is None else _f(ci[1])],
        "net_mean_ci95": [None if ci[0] is None else _f(ci[0] - cost),
                          None if ci[1] is None else _f(ci[1] - cost)],
        "mean_ci95_primary_block": [
            None if primary_ci[0] is None else _f(primary_ci[0]),
            None if primary_ci[1] is None else _f(primary_ci[1])],
        "net_mean_ci95_primary_block": [
            None if primary_ci[0] is None else _f(primary_ci[0] - cost),
            None if primary_ci[1] is None else _f(primary_ci[1] - cost)],
        "ci_interval_rule": interval_rule,
        "n_days": interval["n_days"],
        "ci_method": interval["method"],
        "ci_status": interval["status"],
        "ci_block_scheme": interval.get("block_scheme"),
        "ci_block_days": interval.get("block_days"),
        "ci95_block_sensitivity": interval.get("sensitivity", {}),
        "cost_bps": _f(cost_bps, 2),
    }


def _tier(seal_ratio: float, cuts: tuple[float, float]) -> str:
    low, high = cuts
    if seal_ratio <= low:
        return "弱"
    if seal_ratio <= high:
        return "中"
    return "强"


def _derived_row(desk, symbol: str, day: str) -> dict | None:
    """派生池里的这一行。是否涨停以它为准：``close >= up_limit``，逐只可核对。"""
    try:
        frame = desk.data.read("limit_pool_t0", day, day,
                               columns=["code", "pool", "boards", "close",
                                        "open", "low", "limit_price"])
    except (KeyError, ValueError, OSError):
        return None
    if frame is None or frame.empty or "pool" not in frame.columns:
        return None
    hit = frame[(frame["code"].astype(str) == symbol)
                & (frame["pool"].astype(str) == "limit_up")]
    return None if hit.empty else hit.iloc[0].to_dict()


def _vendor_row(desk, symbol: str, day: str) -> dict | None:
    """东财涨停池里的这一行（只有约 20 个交易日的历史）。"""
    try:
        frame = desk.data.read("limit_up_pool", day, day)
    except (KeyError, ValueError, OSError):
        return None
    if frame is None or frame.empty or "code" not in frame.columns:
        return None
    code = symbol.split(".")[0]
    hit = frame[frame["code"].astype(str) == code]
    return None if hit.empty else hit.iloc[0].to_dict()


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _today(desk, symbol: str, day: str, panel: pd.DataFrame) -> dict:
    """今天的封板事实。三级来源各取所长，绝不混算，并把用了哪一级写出来。

    「不在 ``ths_limit_up``」**不等于**「没涨停」：同花顺的名单会漏——38 个
    交易日里只有 16 天与交易所限价逐只一致，平均每天少 1.6 只。所以涨停与否
    以派生池裁决（``close == up_limit``），同花顺只提供与历史分布同源的
    封单额与首封时间。
    """
    out: dict = {"symbol": symbol, "as_of": day}
    mine = panel[(panel["ts_code"] == symbol) & (panel["trade_date"] == day)]
    derived = _derived_row(desk, symbol, day)
    vendor = _vendor_row(desk, symbol, day)

    if mine.empty and derived is None:
        # 今天没涨停不代表没数据：它可能只是涨了 5%。这句话要说清楚，
        # 否则调用方会把"不是涨停股"读成"这只票不存在"。
        snap = desk.market.snapshot(symbol, day)
        out["is_limit_up"] = False
        out["note"] = (f"{symbol} 在 {day} 不在涨停名单里（派生池与同花顺都没有）；"
                       "下面的条件分布只对涨停股有意义，因此不提供")
        if snap is not None and getattr(snap, "last", None):
            out["last"] = _f(snap.last, 3)
        return out

    out["is_limit_up"] = True

    # 板数：派生池优先（它逐只可核对），其次同花顺的 continue_cnt。
    boards = _num((derived or {}).get("boards"))
    if boards is None and not mine.empty:
        boards = _num(mine.iloc[0]["boards"])
    out["boards"] = int(boards) if boards is not None else None

    # 封单与首封时间优先同花顺：条件分布就是用这两个字段分桶的，换成东财会让
    # 「今天这一档」与「历史那些档」不是同一个尺子。同花顺没有时才退回东财。
    seal = window = stamp = None
    sources: list[str] = []
    if not mine.empty:
        row = mine.iloc[0]
        seal = _num(row["seal_money"])
        stamp = str(row["limit_up_time"]) if pd.notna(row["limit_up_time"]) else None
        window = row["seal_window"] if isinstance(row["seal_window"], str) else None
        if seal is not None:
            sources.append("ths:seal_money")
    if seal is None and vendor is not None:
        seal = _num(vendor.get("seal_amount"))
        stamp = stamp or (str(vendor.get("first_seal_time"))
                          if pd.notna(vendor.get("first_seal_time")) else None)
        if seal is not None:
            sources.append("eastmoney:seal_amount")
    if window is None and stamp:
        minute = _to_minute(stamp)
        window = _seal_bucket(int(minute)) if minute is not None else None

    out["seal_money"] = _f(seal, 2) if seal is not None else None
    out["first_seal_time"] = stamp
    out["seal_window"] = window
    out["structure_source"] = "+".join(sources) if sources else "derived_only"
    if derived is not None:
        out["close"] = _f(derived.get("close"), 3)
    elif not mine.empty:
        out["close"] = _f(mine.iloc[0]["close"], 3)

    # 封单/流通市值：分子用上面择定的封单额，分母只从 daily_basic 取。
    circ = None
    if not mine.empty and pd.notna(mine.iloc[0].get("circ_mv")):
        circ = _num(mine.iloc[0]["circ_mv"])
    if circ is None:
        try:
            basic = desk.data.read("daily_basic", day, day,
                                   columns=["ts_code", "trade_date", "circ_mv"])
            if basic is not None and not basic.empty:
                hit = basic[basic["ts_code"].astype(str) == symbol]
                if not hit.empty:
                    circ = _num(hit.iloc[0]["circ_mv"])
        except (KeyError, ValueError, OSError):
            circ = None
    if seal is not None and circ is not None and circ > 0:
        out["seal_ratio"] = _f(seal / (circ * 10_000.0), 6)
        out["seal_ratio_basis"] = "封单额 ÷ (circ_mv 万元 × 10,000)"
    else:
        out["seal_ratio"] = None

    # 强度分位：今天这一天在当日所有涨停股里排第几，以及在全历史同板数的
    # 样本里排第几。两个分位回答不同问题，都给。
    today = panel[panel["trade_date"] == day]
    if out["seal_ratio"] is not None and len(today) > 1:
        out["seal_ratio_pctile_today"] = _f(
            (today["seal_ratio"] <= out["seal_ratio"]).mean(), 3)
        out["seal_ratio_pctile_today_n"] = int(today["seal_ratio"].notna().sum())
    peers = panel[panel["boards_bucket"] == out["boards"]] if out["boards"] else panel.iloc[0:0]
    if out["seal_ratio"] is not None and len(peers) > MIN_BUCKET_N:
        out["seal_ratio_pctile_hist"] = _f(
            (peers["seal_ratio"] <= out["seal_ratio"]).mean(), 3)
        out["seal_ratio_pctile_hist_n"] = len(peers)
    return out


def _fillable(panel: pd.DataFrame, symbol: str, day: str) -> dict:
    """能不能买到。三档，只用日线，覆盖 2010 年至今。

    ``low < 涨停价`` 单独看**不能**判"开过板"——它包含封板之前的成交。
    但``open`` 把它救回来了：

    * ``open == 涨停价`` 且 ``low == 涨停价`` → 一字，买不到；
    * ``open == 涨停价`` 且 ``low < 涨停价`` → 开盘封住又被砸开，开板时可买；
    * ``open < 涨停价`` → 涨停是盘中封的，**封板之前就能买**，不用排队。

    第三类占 2021-2026 全部涨停的 85.8%，所以「打板必须排队」对绝大多数样本
    根本不成立——它只对前两类（合计 14.2%）成立。
    """
    mine = panel[(panel["ts_code"] == symbol) & (panel["trade_date"] == day)]
    if mine.empty:
        return {"fill_shape": "unknown", "basis": f"{symbol} 不在 {day} 的涨停面板里"}
    kind = str(mine.iloc[0].get("fill_kind") or "")
    mapping = {
        "one_word": ("sealed_all_day",
                     "开盘即封、全天没离开涨停价：没有任何可成交时刻"),
        "opened_after_seal": ("opened_intraday",
                              "开盘封住但被砸开过：开板瞬间可以挂涨停价买"),
        "sealed_intraday": ("sealed_before_limit",
                            "涨停是盘中封上的：封板之前就能买，不需要排队"),
    }
    shape, why = mapping.get(kind, ("unknown", f"无法分类（fill_kind={kind!r}）"))
    # 单只票的 fill_kind 说的是"今天这一只怎么封的"，回答不了"这类票一般能不能
    # 买到"。同板数的历史分布才是可交易的那个数：一字买不到通常只占一成，
    # 而"排队买不到"的说法让人以为八成买不到——方向刚好相反。
    # 单只票的 fill_kind 说的是"今天这一只怎么封的"，回答不了"这类票一般能不能
    # 买到"。同板数的历史分布才是可交易的那个数：一字买不到通常只占一成，
    # 而"排队买不到"的说法让人以为八成买不到——方向刚好相反。
    bucket = mine.iloc[0].get("boards_bucket")
    by_board: dict = {"available": False,
                      "why": "面板里没有 boards_bucket，给不了同板数的分布"}
    if "boards_bucket" in panel.columns and not pd.isna(bucket):
        peers = panel[panel["boards_bucket"] == bucket]
        if len(peers):
            counts = peers["fill_kind"].value_counts()
            total = len(peers)
            by_board = {
                "available": True,
                "boards": int(bucket),
                "n": total,
                "shares": [
                    {"kind": label, "n": int(counts.get(key, 0)),
                     "share": _f(int(counts.get(key, 0)) / total)}
                    for key, label in (("one_word", "一字·买不到"),
                                       ("opened_after_seal", "开盘封后开板·可买"),
                                       ("sealed_intraday", "盘中封板·封前可买"))
                ],
                "reading": ("同板数历史上封板形态的构成。'盘中封板'意味着封板之前"
                            "就能买，不需要排队；它给的是可成交机会的**上界**，"
                            "不是成交概率——真正排到队没有，日线看不出来"),
            }
    return {"fill_shape": shape, "basis": why, "by_board": by_board}


def _ladder(panel: pd.DataFrame, boards: int, tier: str, window: str,
            cost_bps: float, *, brief: bool = False) -> list[dict]:
    """从最精确的桶逐级放宽。每一级都给完整的两条离场腿，不做取舍。

    内核是信息源：它给「每一层长什么样」，不给「该看哪一层」。所以这里没有
    「选中」的那一档、没有默认值、没有推荐——调用方从四级里自己挑，或者
    自己去组合别的条件。

    每级带 ``days``（有信号的交易日数）。``n`` 是笔数不是独立样本数：同一天
    封板的票同涨同跌，2,070 笔只落在 1,042 个交易日里，名义标准误被低估
    1.2-1.8 倍。把两个数并排放，读者才知道那个区间该乘多少。

    最后一层仍不足 ``MIN_BUCKET_N`` 时只给 ``n`` 和 ``insufficient``：那说明这个
    板数在历史上本来就罕见，任何"平均值"都是编的。
    """
    steps = [
        ("板+封单+时段", (panel["boards_bucket"] == boards)
         & (panel["seal_tier"] == tier) & (panel["seal_window"] == window)),
        ("板+封单", (panel["boards_bucket"] == boards)
         & (panel["seal_tier"] == tier)),
        ("板+时段", (panel["boards_bucket"] == boards)
         & (panel["seal_window"] == window)),
        ("仅板", panel["boards_bucket"] == boards),
    ]
    out: list[dict] = []
    for rule, mask in steps:
        sub = panel.loc[mask]
        close_ = _stats(sub["ret"], cost_bps, sub["trade_date"])
        row: dict = {"rule": rule, "n": close_["n"]}
        if close_["insufficient"]:
            row["insufficient"] = True
            out.append(row)
            continue
        row["days"] = int(sub["trade_date"].nunique())
        if brief:
            # 只给「近一年」与全历史的对比所需的最小字段：条件维度已经由上面
            # 四级展开，这里只回答"结构有没有衰减"，不需要再铺一遍两条腿。
            row["close_mean"] = close_["mean"]
            out.append(row)
            continue
        open_ = _stats(sub["ret_open"], cost_bps, sub["trade_date"])
        row["close"] = {"mean": close_["mean"], "median": close_["median"],
                        "p10": close_["p10"], "win_rate": close_["win_rate"],
                        "net": close_["net_mean"],
                        "mean_ci95": close_["mean_ci95"],
                        "net_mean_ci95": close_["net_mean_ci95"],
                        "n_days": close_["n_days"], "ci_method": close_["ci_method"],
                        "ci_status": close_["ci_status"]}
        if not open_["insufficient"]:
            row["open"] = {"mean": open_["mean"], "p10": open_["p10"],
                           "net": open_["net_mean"],
                           "mean_ci95": open_["mean_ci95"],
                           "net_mean_ci95": open_["net_mean_ci95"],
                           "n_days": open_["n_days"], "ci_method": open_["ci_method"],
                           "ci_status": open_["ci_status"]}
        out.append(row)
    return out


def _fill_kind_block(panel: pd.DataFrame, cost_bps: float) -> list[dict]:
    """三档可成交性各自的前向收益，统一用**开盘卖**那条腿。

    为什么单独给这一块：一字板看着最漂亮，但那一档买不到；而占 85.8% 的盘中
    封板，才是真的能吃到的那部分。只给一个混合平均，等于把"最好的买不到"和
    "能买到的没那么好"加在一起，读者看不出差别。

    用开盘卖而不是收盘卖：一字板开盘卖 p10 还是正的，收盘卖会掉到 -4.35%——
    这两条腿差多少，恰恰是"该不该拿着过夜"的答案。
    """
    out: list[dict] = []
    for kind, label in (("one_word", "一字·买不到"),
                        ("opened_after_seal", "开盘封后开板·可买"),
                        ("sealed_intraday", "盘中封板·封前可买")):
        block = _stats(panel.loc[panel["fill_kind"] == kind, "ret_open"], cost_bps,
                       panel.loc[panel["fill_kind"] == kind, "trade_date"])
        row: dict = {"kind": label, "n": block["n"]}
        if not block["insufficient"]:
            row.update({"mean": block["mean"], "p10": block["p10"],
                        "mean_ci95": block["mean_ci95"],
                        "net_mean_ci95": block["net_mean_ci95"],
                        "n_days": block["n_days"], "ci_method": block["ci_method"],
                        "ci_status": block["ci_status"]})
        out.append(row)
    return out


def _theme_block(panel: pd.DataFrame, symbol: str, day: str, cost_bps: float) -> dict:
    """这只票挂在哪些题材上，同题材今天还有谁，以及"簇大小"值多少。

    第三部分才是重点。前两部分是当日事实（可以在盘面里自己数），第三部分是
    这只票回答不了的：**同样板数、同样封单强度，独苗和 4 只以上同题材的差别
    有多大**。2021-2026 的样本里这个差是 0.5-0.8pp，且不随封单强度消失。
    """
    empty = {"available": False}
    if panel.empty or "reason" not in panel.columns:
        return {**empty, "why": "面板里没有涨停原因字段"}
    today = panel[panel["trade_date"] == day]
    if today.empty:
        return {**empty, "why": f"{day} 面板为空"}
    mine = today[today["ts_code"] == symbol]
    if mine.empty:
        return {**empty, "why": f"{symbol} 不在当日涨停面板里"}

    reason = str(mine.iloc[0]["reason"] or "")
    tags = [t.strip() for t in reason.split("+")
            if t.strip() and t.strip() not in NOISE_TAGS]
    if not tags:
        return {"available": True, "reason": reason, "tags": [],
                "note": "只带属性标签（ST/新股/次新/小盘等），没有题材"}

    exploded = today.assign(tag=today["reason"].astype(str).str.split("+")).explode("tag")
    exploded["tag"] = exploded["tag"].astype(str).str.strip()
    exploded = exploded[exploded["tag"].isin(tags)]
    sizes = exploded.groupby("tag").size().sort_values(ascending=False)
    lead_tag = str(sizes.index[0])
    lead_size = int(sizes.iloc[0])
    peers = exploded[(exploded["tag"] == lead_tag)
                     & (exploded["ts_code"].astype(str) != symbol)]

    boards = int(mine.iloc[0]["boards"]) if pd.notna(mine.iloc[0]["boards"]) else None
    by_cluster: list[dict] = []
    if boards is not None:
        peers_panel = panel[(panel["boards_bucket"] == boards)
                            & panel["cluster_bucket"].notna()]
        for name in (name for _, name in CLUSTER_BUCKETS):
            block = _stats(peers_panel.loc[peers_panel["cluster_bucket"] == name, "ret"],
                           cost_bps,
                           peers_panel.loc[peers_panel["cluster_bucket"] == name, "trade_date"])
            row = {"cluster": name, "n": block["n"]}
            if not block["insufficient"]:
                row.update({"mean": block["mean"], "net_mean": block["net_mean"]})
            by_cluster.append(row)

    # tags 用对象而不是对象数组：``[{"tag":"商业航天","size_today":2}]`` 比
    # ``{"商业航天":2}`` 多一倍字节，而读的人两种都能读。
    #
    # 不回 ``reason`` 原文：它带着已被剔除的财报季与属性标签，读的人会拿
    # "三季报增长 22 只"当共振，而那个 22 恰好是这里刻意不算的东西。
    return {
        "available": True,
        "tags": {str(tag): int(size) for tag, size in sizes.items()},
        "largest_cluster": {"tag": lead_tag, "size": lead_size,
                 "peers": [str(c) for c in peers["ts_code"].head(3)]},
        "boards_controlled": boards,
        "by_cluster": by_cluster,
    }


def validate(desk, day: str | None = None, split: str = "20250101") -> dict:
    """样本外检验：分位点钉在 ``split`` 之前，再拿去切 ``split`` 之后的样本。

    为什么必须有这一层：``conditional`` 里的分布是用**全部**历史算的，包括用
    2026 年的数据描述 2026 年。那不叫预测，叫描述。这里把封单强度分位的切点
    固定在 ``split`` 之前，之后的数据一点都没参与拟合——只有这一段是真正的
    样本外，也只有这一段能回答"这套结构还能不能用"。

    2026-11-02 实测（split=20250101）：9 个「板数 × 封单档 × 早盘」桶的样本外
    均值全部为正，衰减在 -0.74pp 到 +0.48pp 之间，排序保持；题材簇 4只+ 从
    +2.47% 降到 +2.10%，仍高于 1只 的 +1.56%。相对衰减 10-15%，是 alpha 缓慢
    衰减的正常形态，不是样本外归零。

    这不是一次性的：每天收盘后跑一遍就能看出衰减走到哪了。它是"内核能不能自我
    修正"的入口——没有它，``conditional`` 里的数字只会越来越像一段历史记录。
    """
    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "没有日线，无法锚定日期"}
    panel = _panel(desk, day)
    if panel is None:
        return {"ok": False, "as_of": day, "error": "面板读不到"}

    inside = panel[panel["trade_date"] < split]
    outside = panel[panel["trade_date"] >= split]
    if len(inside) < 500 or len(outside) < 200:
        return {"ok": False, "as_of": day, "split": split,
                "error": (f"split 两侧样本不足：样本内 {len(inside)} 笔、"
                          f"样本外 {len(outside)} 笔")}

    # 切点只用样本内定。用全样本定切点等于偷看未来——而这一层存在的全部意义
    # 就是"不许偷看"。
    ratios = inside["seal_ratio"].dropna()
    if len(ratios) < MIN_BUCKET_N:
        return {"ok": False, "as_of": day, "split": split,
                "error": "样本内没有足够的封单强度数据来定分位"}
    cuts = (float(ratios.quantile(1 / 3)), float(ratios.quantile(2 / 3)))

    def tagged(frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["tier"] = out["seal_ratio"].map(
            lambda v: _tier(float(v), cuts) if pd.notna(v) else None)
        out["early"] = out["minute"] <= 600
        return out

    a, b = tagged(inside), tagged(outside)

    buckets: list[dict] = []
    for boards in (1, 2, 3):
        for tier in ("弱", "中", "强"):
            left = a[(a["boards_bucket"] == boards) & (a["tier"] == tier)
                     & a["early"]]["ret_open"].dropna()
            right = b[(b["boards_bucket"] == boards) & (b["tier"] == tier)
                      & b["early"]]["ret_open"].dropna()
            if len(left) < 100 or len(right) < 50:
                continue
            buckets.append({
                "bucket": f"{boards}板·{tier}封单·早盘",
                "n_in": len(left), "in_mean": _f(left.mean()),
                "n_out": len(right), "out_mean": _f(right.mean()),
                "decay_pp": _f((right.mean() - left.mean()) * 100, 2),
            })

    themes: list[dict] = []
    for name, low, high in (("1只", 0, 1), ("2-3只", 1, 3), ("4只+", 3, 1e9)):
        left = a[(a["boards_bucket"] == 1) & (a["theme_cluster"] > low)
                 & (a["theme_cluster"] <= high)]["ret_open"].dropna()
        right = b[(b["boards_bucket"] == 1) & (b["theme_cluster"] > low)
                  & (b["theme_cluster"] <= high)]["ret_open"].dropna()
        if len(left) < 100 or len(right) < 50:
            continue
        themes.append({
            "cluster": name,
            "n_in": len(left), "in_mean": _f(left.mean()),
            "n_out": len(right), "out_mean": _f(right.mean()),
            "decay_pp": _f((right.mean() - left.mean()) * 100, 2),
        })

    decays = [row["decay_pp"] for row in buckets if row["decay_pp"] is not None]
    positives = sum(1 for row in buckets if (row["out_mean"] or 0) > 0)
    summary: dict = {
        "buckets": len(buckets),
        "out_positive": positives,
        "decay_min_pp": min(decays) if decays else None,
        "decay_max_pp": max(decays) if decays else None,
        "decay_mean_pp": _f(np.mean(decays), 2) if decays else None,
    }
    if buckets:
        # 排序是否保持：样本外里"强封单"是否仍高于"弱封单"。结构有效性的
        # 最低要求不是均值正，是**排序不乱**——均值可以随市场整体漂移，
        # 但只要强的还比弱的强，这个维度就还能用来做取舍。
        pairs = 0
        held = 0
        for boards in (1, 2, 3):
            strong = next((r["out_mean"] for r in buckets
                           if r["bucket"] == f"{boards}板·强封单·早盘"), None)
            weak = next((r["out_mean"] for r in buckets
                         if r["bucket"] == f"{boards}板·弱封单·早盘"), None)
            if strong is None or weak is None:
                continue
            pairs += 1
            held += int(strong > weak)
        summary["order_pairs"] = pairs
        summary["order_held"] = held

    return {
        "ok": True,
        "as_of": day,
        "split": split,
        "exit_leg": "T+1 开盘卖（尾部风险最小的那条，见 conditional.exit）",
        "in_sample": {"rows": len(inside),
                      "days": int(inside["trade_date"].nunique())},
        "out_of_sample": {"rows": len(outside),
                          "days": int(outside["trade_date"].nunique())},
        "tier_cuts_from_in_sample": {"弱≤": _f(cuts[0]), "中≤": _f(cuts[1])},
        "buckets": buckets,
        "theme_clusters": themes,
        "summary": summary,
        "basis": ("切点只用样本内定；样本外一段没有参与任何拟合。"
                  "衰减是样本外均值减样本内均值，单位 pp。"),
        "caveat": ("样本外只有一段。它证明「没崩」，不证明「未来还是这个数」；"
                   "市场结构变化时最先失效的通常是弱封单组，因为它的毛边最薄。"),
    }


def _base_events(desk, day: str) -> pd.DataFrame | None:
    """T 日及以前所有触板事件的前向收益（含炸板），按日缓存。

    与 ``_panel`` 的区别只有一条：面板来自 ``ths_limit_up``（收盘仍封），这张表
    来自 ``limit_pool_t0``（封住 + 炸板）。前者答"封住之后怎么走"，后者答
    "封不封得住"，两者不能互相替代。
    """
    hit = _BASE_EVENTS.get(day)
    if hit is not None:
        return hit if len(hit) else None

    cached = _read_base_cached(day)
    if cached is not None:
        _BASE_EVENTS[day] = cached
        return cached

    start = "20201201"
    # `low` 是这一块的分水岭，不是可选列：限价买单在价格**跌破**涨停价时才成交，
    # 全天没跌破（一字）的票只能靠排队优先权成交。少了 low，就只能给一个把
    # 买不到的票算在内的回封率——那个数会把"打高位板"说成更好的选择。
    cols = ["trade_date", "code", "pool", "boards", "prev_boards", "limit_price", "low"]
    try:
        pools = desk.data.read("limit_pool_t0", start, day, columns=cols)
        bars = desk.data.read("daily", start, day,
                              columns=["ts_code", "trade_date", "open", "close"])
    except (KeyError, ValueError, OSError):
        return None
    if pools is None or pools.empty or bars is None or bars.empty:
        return None
    if "prev_boards" not in pools.columns:
        # 老分区没有这一列（2026-11-03 之前派生的）。不猜身位：用 boards 顶替会
        # 让炸板行全落在"首板"，把 3 进 4 的失败算成首板失败——错的身位比没有更糟。
        return None

    pools = pools[pools["pool"].isin(["limit_up", "broken_board"])].copy()
    price = pd.to_numeric(pools["limit_price"], errors="coerce")
    pools = pools[price.gt(0)].copy()
    if pools.empty:
        return None
    bars = bars.copy()
    bars["trade_date"] = bars["trade_date"].astype(str)
    bars = bars.sort_values(["ts_code", "trade_date"])
    bars["next_open"] = bars.groupby("ts_code")["open"].shift(-1)
    bars["next_close"] = bars.groupby("ts_code")["close"].shift(-1)

    m = pools.merge(bars, left_on=["code", "trade_date"],
                    right_on=["ts_code", "trade_date"], how="left")
    m = m.dropna(subset=["next_close"])
    if m.empty:
        _BASE_EVENTS[day] = m
        return None
    entry = pd.to_numeric(m["limit_price"], errors="coerce")
    m["resealed"] = m["pool"].eq("limit_up")
    # 身位用"尝试高度"：封住的行 boards 就是它，炸板行是上一日连板 + 1
    # （3 进 4 失败记在 4 板上，不是 3 板也不是 0 板）。
    m["attempt"] = np.where(m["resealed"], m["boards"], m["prev_boards"] + 1)
    m["ret_close"] = pd.to_numeric(m["next_close"], errors="coerce") / entry - 1
    m["ret_open"] = pd.to_numeric(m["next_open"], errors="coerce") / entry - 1
    # 能不能买到：限价买单在价格跌破涨停价的那一刻成交。全天没跌破的（一字）
    # 只能靠排队优先权，散户没有。
    low = pd.to_numeric(m.get("low"), errors="coerce")
    m["buyable"] = (low < entry - 1e-6).fillna(False)
    m = m[m["ret_close"].notna()]
    _BASE_EVENTS[day] = m
    if len(m):
        _write_base_cached(day, m)
    return m if len(m) else None


def _base_rate(desk, boards: int, day: str, cost_bps: float) -> dict | None:
    """同一身位的**全部**触板样本，含收盘没封住的那些。

    这一块存在的理由：``conditional`` 的样本来自 ``ths_limit_up``，那是收盘仍封
    的票——它给的是"封住之后怎么走"，读者很容易当成"打板怎么走"。而打板的期望
    是两道门相乘：先看封不封得住，再看封住之后值多少。缺了前一道，分布只剩赢家。

    2020-12~2026-11 的全市场实测（首板，n=66,895）：回封 67.1%、封住后 +1.75%、
    炸板后 −4.58%。所以打板不是方向赌注，是一道分类题。

    **但全样本的回封率不能直接用来下单。** 按"限价单能不能成交"劈开：
一字（全天
    没跌破涨停价）的回封率按构造是 100%，隔夜 +7.01%，而限价单买不到它们；
    盘中跌破过的那组才成交，首板隔夜 −0.34%。而一字占比随身位上升
    （首板 2.1% → 7 板 28.6%），于是全样本回封率从 67% 抬到 76%（7 板）——
    **那 9 个点的涨幅全部来自买不到的那一组**。买得到的那组回封率是平的（61–66%），
    且未封时的亏损随身位加深（−4.63% → −12.07%）。

    ``required_reseal_rate`` 是这道题的门槛：把本组的两个条件均值与成本代进
    ``P·EV_封 + (1−P)·EV_炸 − 成本 = 0``，解出 P。它回答"要赚钱，封板率得多高"，
    而不是"这只票会不会封"。**判断用 ``buyable`` 块。**
    """
    m = _base_events(desk, day)
    if m is None:
        return {"available": False,
                "note": "limit_pool_t0 缺 prev_boards 列或读不到；先跑 "
                        "scripts/derive_pools.py --replace 重算历史分区"}

    hit = m[m["attempt"] == boards]
    if len(hit) < MIN_BUCKET_N:
        return {"available": False, "boards": boards, "n": len(hit),
                "note": f"这个身位的样本只有 {len(hit)} 笔，不到 {MIN_BUCKET_N}，不给统计量"}
    sealed = hit[hit["resealed"]]
    broken = hit[~hit["resealed"]]
    n = len(hit)
    p = len(sealed) / n
    interval = _day_block_interval(hit["resealed"].astype(float), hit["trade_date"])
    cost = cost_bps / 10_000.0
    ev_seal = float(sealed["ret_close"].mean()) if len(sealed) else None
    ev_break = float(broken["ret_close"].mean()) if len(broken) else None
    need = None
    if ev_seal is not None and ev_break is not None and ev_seal > ev_break:
        need = (cost - ev_break) / (ev_seal - ev_break)

    def _leg(frame: pd.DataFrame, col: str) -> dict:
        values = frame[col].dropna()
        if values.empty:
            return {"n": 0, "mean": None, "win_rate": None}
        return {"n": len(values), "mean": _f(float(values.mean())),
                "win_rate": _f(float((values > 0).mean()))}

    def _block(frame: pd.DataFrame) -> dict:
        """一组的完整算题：回封率、两条腿、门槛、差多少个 pp。"""
        if frame.empty:
            return {"n": 0, "available": False}
        s = frame[frame["resealed"]]
        b = frame[~frame["resealed"]]
        pp = len(s) / len(frame)
        interval = _day_block_interval(frame["resealed"].astype(float), frame["trade_date"])
        rs = float(s["ret_close"].mean()) if len(s) else None
        bk = float(b["ret_close"].mean()) if len(b) else None
        nd = None
        if rs is not None and bk is not None and rs > bk:
            nd = (cost - bk) / (rs - bk)
        return {"n": len(frame), "available": True,
                "reseal_rate": _f(pp),
                "reseal_rate_ci95": [None if interval["ci95"][0] is None else _f(interval["ci95"][0]),
                                     None if interval["ci95"][1] is None else _f(interval["ci95"][1])],
                "n_days": interval["n_days"],
                "ci_method": interval["method"],
                "if_reseal": _leg(s, "ret_close"),
                "if_break": _leg(b, "ret_close"),
                "required_reseal_rate": _f(nd),
                "gap_pp": None if nd is None else _f((nd - pp) * 100, 1),
                "feasible": None if nd is None else bool(nd <= 1.0)}

    # 能不能买到，把同一个身位劈成两组，而这两组的结论相反：
    #
    #   一字（全天没跌破涨停价）：回封率**按构造恒为 100%**（没跌破就意味着收在
    #   涨停价），隔夜 +7.01%。但限价单买不到——只在有排队优先权时成立。
    #   盘中跌破过：限价单在这里成交，隔夜 −0.34%（首板）到 −3.39%（5 板）。
    #
    # 全样本回封率随身位上升（2.1% → 28.6% 一字占比从首板 2.1% 升到 7 板 28.6%），
    # 那部分涨幅**全部来自买不到的那一组**。买得到的那组回封率是平的（61–66%）。
    # 所以"打高位板回封率更高"这句话，对散户的下单能力不成立。
    buyable = hit[hit["buyable"]]
    one_word = hit[~hit["buyable"]]

    return {
        "available": True,
        "boards": boards,
        "definition": "所有当日触碰过涨停价的样本（含收盘没封住的），按尝试高度分桶",
        "n": n,
        "resealed": len(sealed),
        "made_up": "attempt = 封住取 boards；炸板取 prev_boards + 1（3 进 4 失败记在 4 板）",
        "reseal_rate": _f(p),
        "reseal_rate_ci95": [None if interval["ci95"][0] is None else _f(interval["ci95"][0]),
                             None if interval["ci95"][1] is None else _f(interval["ci95"][1])],
        "reseal_rate_ci95_method": interval["method"],
        "reseal_rate_ci95_status": interval["status"],
        "n_days": interval["n_days"],
        "if_reseal": _leg(sealed, "ret_close"),
        "if_break": _leg(broken, "ret_close"),
        "unconditional": _leg(hit, "ret_close"),
        "overnight_only": {"if_reseal": _leg(sealed, "ret_open"),
                           "if_break": _leg(broken, "ret_open")},
        "required_reseal_rate": _f(need),
        "gap_pp": (None if need is None else _f((need - p) * 100, 1)),
        # 门槛 >100% 时，这不再是"封板率得多高"，而是"这个票面根本做不了"：
        # 一手 342 元的往返成本约 320bps（¥5 最低佣金占大头），即使每笔都封住
        # 也盖不住。把它写成 102% 不够——读者会以为是"再准一点就行"。
        "feasible": (None if need is None else bool(need <= 1.0)),
        "cost_bps": _f(cost_bps, 2),
        # 上面那些是全样本口径（含买不到的一字）。**下单决策看 buyable 那一块。**
        "buyable": _block(buyable),
        "one_word": {**_block(one_word),
                     "note": ("全天没跌破涨停价，限价单只能靠排队优先权成交——散户拿不到。"
                              "它们的回封率按构造就是 100%（没跌破即收在涨停价），"
                              "所以把这一组算进回封率会系统性高估。")},
        "buyable_share": _f(len(buyable) / n) if n else None,
        "one_word_share": _f(len(one_word) / n) if n else None,
        "reading": (
            "required_reseal_rate 是这道题的门槛，不是预测：把本组两个条件均值与"
            "成本代进 P·EV封+(1−P)·EV炸−成本=0 解出 P。gap_pp 为正表示本组的"
            "实际回封率低于门槛——按这个身位打板，在扣完成本后是负期望。"
            "**判断请用 buyable 块**：全样本那组含买不到的一字，"
            "它的回封率按构造是 100%，会把身位抬得比实际能做的更好看。"
            if need is not None and need <= 1.0 else
            f"门槛超过 100%：这一笔的往返成本是 {cost_bps:.0f}bps，"
            "即使每一笔都封住也覆盖不了。问题不在选股，在票面太小、"
            "最低佣金吃掉了全部边际"),
        "caveat": ("回封率是身位的历史基线，不含当日封单强度与首封时段；"
                   "要更细的条件，用 conditional（但它只含已封住的样本）。"
                   "回封率与均值区间按交易日整块 bootstrap，n_days 与笔数并列；"
                   "不足 8 个交易日期明确不给区间。"
                   f"一字占 {_f(len(one_word) / n * 100, 1) if n else None}% 且随"
                   "身位上升（首板 2.1% → 7 板 28.6%）——这是全样本回封率"
                   "随身位上升的唯一来源。"),
    }


def setup_table(desk, day: str, heights: tuple[int, ...] = (1, 2, 3)) -> dict:
    """各身位的期望与门槛——一屏回答"今天哪个身位还有边际"。

    这是把 ``base_rate`` 从"单票体检"升成"战法表"：模型不必先选票再问分布，
    可以直接问"首板/二板/三板，哪个在扣完成本后还是正期望"。

    成本按**本账户一个典型仓位**算一次（min(权益 × 单票上限, 权益 / 持仓只数)），
    不用常数，也不按一手——342 元的票一手只占 6.8%，按一手算会把门槛抬到
    102%，方向刚好相反。这里不对某个价位折整手：表是跨身位的，没有单一价格。

    表的结论可能是"全都没有边际"，那不是失败：它正是"今天不做"的依据，
    比让模型从 20 行涨幅里挑一只强。
    """
    from ..kernel.models import Instrument
    from .impact import round_trip_cost_bps

    equity, cap, max_pos, cap_key = 0.0, 0.0, 3, "max_core_single"
    try:
        equity = float(desk.book.summary()["equity"])
        limits = desk.limits()
        from .analog import _sizing_cap_key
        cap_key = _sizing_cap_key(desk)
        cap = float(limits.get(cap_key) or 0)
        max_pos = int(limits.get("max_positions") or 3)
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    ticket = min(equity * cap, equity / max_pos) if equity > 0 and cap > 0 else 0.0
    if ticket > 0:
        # 参考票只用主板股票的默认费率；这一块问的是"这个账户的票面成本，
        # 各身位的门槛在哪"，不是某一只票的成本——单票成本在 profile 里给。
        ref = Instrument(symbol="600000.SH", name="参考")
        try:
            cost_bps = float(round_trip_cost_bps(ticket, ref)["total_bps"])
        except (KeyError, ValueError, TypeError):
            cost_bps = 0.0
    else:
        cost_bps = 0.0

    rows: list[dict] = []
    for height in heights:
        block = _base_rate(desk, height, day, cost_bps)
        if not block or not block.get("available"):
            rows.append({"boards": height, "available": False,
                         "note": (block or {}).get("note") or "样本不足"})
            continue
        # 用 buyable：全样本含买不到的一字，它的回封率按构造是 100%，
        # 会把身位抬得比实际能做的更好看。下单能力只覆盖得到 buyable 那一组。
        tradeable = block.get("buyable") or {}
        gap = tradeable.get("gap_pp")
        rows.append({
            "boards": height,
            "available": True,
            "n": block["n"],
            "n_buyable": tradeable.get("n"),
            "one_word_share": block.get("one_word_share"),
            "reseal_rate": tradeable.get("reseal_rate"),
            "reseal_rate_all": block.get("reseal_rate"),
            "need_reseal_rate": tradeable.get("required_reseal_rate"),
            "gap_pp": gap,
            "feasible": tradeable.get("feasible"),
            "if_reseal_mean": (tradeable.get("if_reseal") or {}).get("mean"),
            "if_break_mean": (tradeable.get("if_break") or {}).get("mean"),
            # 原来这里有一个 `verdict: has_edge/no_edge`。删掉的理由不是"它算错了"，
            # 是"它替读者做完了判断"：`gap_pp` 已经把"实际回封率比需要的低多少"
            # 给了出来，要不要据此打这个身位，取决于你对自己的预测有多自信、
            # 愿不愿意承担这个 gap。代码不该替这个决定签字。
            # 判据本身还在（`gap_pp` 的符号），只是不再包装成一个词。
        })

    return {
        "as_of": day,
        "ticket_cny": _f(ticket, 0),
        "cost_bps": _f(cost_bps, 2),
        "sizing": {
            "cap_key": cap_key,
            "cap": _f(cap, 4),
            "scenario_only": True,
            "note": ("这是跨身位比较用的典型票面，不是实际委托数量；实际可买数量仍由 "
                     "proposal/ticket 按活袖、现金、当前持仓、目标数量与整手约束裁定。"),
        },
        "rows": rows,
        "basis": ("口径用 T 日收盘价（涨停价）买入、T+1 收盘卖出；按尝试高度分桶，"
                  "样本含当日炸板。**reseal_rate / gap_pp 按 buyable 组算**——"
                  "限价单只在价格跌破涨停价时成交，全天没跌破的一字只能靠排队"
                  "优先权，那组回封率按构造是 100%，计入会把身位说好。"
                  "reseal_rate_all 是全样本值，留着对账，不用来决策。"),
        "still_missing": ("成交概率。日线只能给'封板前有没有可买时刻'的上界，"
                          "给不了排队成交的概率——那需要分帧封单轨迹。"),
    }


def profile(desk, symbol: str, day: str | None = None) -> dict:
    """This name's board structure today, and what that structure paid historically.

    ``conditional`` 回答的是「同结构的样本之后发生了什么」，不是「这只票会涨
    还是会跌」。前者可以被样本量检验，后者不能。所以这里没有评分、没有建议、
    没有 ``action``。
    """
    requested = str(symbol or "").upper().strip()
    if not requested:
        return {"ok": False, "error": "symbol 不能为空"}
    parts = requested.split(".")
    digits = parts[0]
    if (len(digits) != 6 or not digits.isdigit() or len(parts) > 2
            or (len(parts) == 2 and parts[1] not in {"SH", "SZ", "BJ"})):
        return {"ok": False, "status": "bad_symbol", "symbol": requested,
                "error": "symbol 必须是六位代码或 <六位代码>.SH/.SZ/.BJ",
                "note": "代码形状不明确时显式拒绝，不把它误报成『不在涨停名单』。"}
    # pool 返回六位 code，board 的历史面板使用 ts_code。两条工具链共享同一个
    # canonical symbol，避免 ``ok=true, is_limit_up=false`` 的代码格式假阴性。
    symbol = canonical_ts_code(digits)
    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "没有日线，无法锚定日期"}

    panel = _panel(desk, day)
    if panel is None:
        return {"ok": False, "as_of": day,
                "error": ("ths_limit_up 或 daily 读不到；涨停结构分布依赖"
                          "2020-12 以来的同花顺涨停表")}

    today = _today(desk, symbol, day, panel)
    if not today.get("is_limit_up"):
        return {"ok": True, **today,
                "provenance": {"datasets": ["ths_limit_up", "daily", "daily_basic"],
                               "window": "2020-12 起"}}

    # 成本按本账户当前活袖生成一个可复算的整手票面情景，不用 CORE 常数，
    # 也不冒充一张已经知道现金、持仓和目标数量的实际委托。
    # 一手 342 元的票只占 5,000 元账户的 6.8%，票面情景会显著改变最低佣金 bps，
    # 进而改变 required_reseal_rate；最终数量仍由 proposal/ticket 裁定。
    price = float(today.get("close") or 0)
    one_lot = price * 100
    equity, cap, max_pos, cap_key = 0.0, 0.0, 3, "max_core_single"
    try:
        equity = float(desk.book.summary()["equity"])
        limits = desk.limits()
        from .analog import _sizing_cap_key
        cap_key = _sizing_cap_key(desk)
        cap = float(limits.get(cap_key) or 0)
        max_pos = int(limits.get("max_positions") or 3)
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    budget = (min(equity * cap, equity / max_pos)
              if equity > 0 and cap > 0 else one_lot)
    lots = max(1, int(budget // one_lot)) if one_lot > 0 else 1
    notional = lots * one_lot
    if equity > 0 and cap > 0 and one_lot > equity * cap:
        cost_basis = (f"一手 {one_lot:.0f} 元已超过单票上限 "
                      f"{equity * cap:.0f} 元——这个票面买不进，成本按一手计")
    else:
        cost_basis = (f"{lots} 手 {notional:.0f} 元（单票上限与持仓只数决定的整手数）"
                      "的往返实算，kernel CostModel + 滑点")
    cost_basis += (f"；sizing cap={cap_key}。这是成本情景，不是实际委托；"
                   "现金、当前持仓与最终数量由 proposal/ticket 重算")
    try:
        instrument = desk.market.instrument(symbol, day)
        cost_bps = float(round_trip_cost_bps(notional, instrument)["total_bps"])
    except (KeyError, ValueError, AttributeError, TypeError):
        cost_bps = 0.0

    # 封单档用全样本三分位切，不用固定阈值：seal_ratio 的绝对水平随年份漂移，
    # 一个写死的 0.05 在 2021 年是强档、在 2025 年可能只是中档。
    ratios = panel["seal_ratio"].dropna()
    cuts = ((float(ratios.quantile(1 / 3)), float(ratios.quantile(2 / 3)))
            if len(ratios) > MIN_BUCKET_N else (np.nan, np.nan))
    panel = panel.copy()
    panel["seal_tier"] = panel["seal_ratio"].map(
        lambda v: _tier(float(v), cuts) if pd.notna(v) else None)

    boards = int(today.get("boards") or 1)
    # 分档必须用**未四舍五入**的值。``_today`` 里的 seal_ratio 已经 round 到 6 位，
    # 而分位切点是原始精度：0.0166666666 舍入成 0.016667 之后刚好大于同一个切点，
    # 今天这一档会被判成"强"，而面板里那条是"弱"——mask 直接匹配不到，四级阶梯
    # 全空。这不是测试数据特有的，任何落在切点附近的票都会撞上。
    raw_row = panel[(panel["ts_code"] == symbol) & (panel["trade_date"] == day)]
    ratio = (float(raw_row.iloc[0]["seal_ratio"])
             if not raw_row.empty and pd.notna(raw_row.iloc[0]["seal_ratio"]) else None)
    window = today.get("seal_window")
    tier = _tier(ratio, cuts) if ratio is not None and np.isfinite(cuts[0]) else None

    ladder = _ladder(panel, boards, tier, window, cost_bps)
    caveats = [
        ("买入锚点是 T 日收盘价（涨停价）。卖出给两条腿：T+1 开盘、T+1 收盘。"
         "换成「次日开盘买入」得到的是另一个动作的期望（约 -0.14%），与这里不可比。"),
        "封单额是收盘终值。盘中封单在衰减，用这里的分布回答盘中问题有前视偏差。",
        ("可成交性只看开盘价与最低价相对涨停价的位置，覆盖 2010 年至今；"
         "85.8% 的涨停是盘中封的，封板前就能买，不需要排队。"),
        ("题材簇是「日 × 题材」级变量，同一天同题材的票共享它——它用来分开"
         "孤立涨停与板块共振，不提供新的独立样本量。"),
        ("conditional 只含收盘仍封的样本（这是它的定义）；base_rate 才含当日炸板。"
         "引用「打板赚不赚钱」时必须两个一起读：打板 = P(封住) × 封住之后的值。"),
        (f"样本区间 {panel['trade_date'].min()}–{panel['trade_date'].max()}。"
         "ladder 的 n 是笔数、n_days 是交易日数；均值与回封率区间按交易日"
         "整块 bootstrap，同日股票不冒充独立观测。"),
    ]
    if tier is None:
        caveats.append("这只票的封单/流通市值为空，封单档用不了，条件分布只按板数分。")

    recent = panel[panel["trade_date"] >= (
        datetime.strptime(day, "%Y%m%d") - timedelta(days=365)).strftime("%Y%m%d")]
    recent_stats = (_ladder(recent, boards, tier, window, cost_bps, brief=True)
                    if len(recent) else [])

    return {
        "ok": True,
        "symbol": symbol,
        "as_of": day,
        "data_as_of": day,
        "time_basis": "trade_date_daily_close",
        "pit_safe": True,
        "pit_scope": "event rows and forward returns only through the requested as_of panel",
        "execution_scope": {
            "basis": "daily_ohlc_fillability_proxy_not_queue_replay",
            "fill_probability_available": False,
            "order_created": False,
        },
        **today,
        "fillable": _fillable(panel, symbol, day),
        "theme": _theme_block(panel, symbol, day, cost_bps),
        "conditional": {
            "buy_at": "T 日收盘价（= 涨停价）",
            "sell_at": "T+1 收盘价",
            # 这一块的全部样本都来自 ths_limit_up —— 收盘仍封的票。不写这句，
            # 读者会把"封住之后怎么走"当成"打板怎么走"，而打板的期望先要过
            # 回封这道门。base_rate 就是那道门。
            "conditioned_on": "收盘仍封（resealed）——不含当日炸板的样本",
            "cost_bps": _f(cost_bps, 2),
            "cost_basis": cost_basis,
            "tier_cuts": {"弱≤": _f(cuts[0]), "中≤": _f(cuts[1])},
                # 同一入场点、两条离场腿 + 独立交易日数。只给"收盘卖"会把结构读差：
            # 3 板强封单组开盘卖 p10 -0.64%、收盘卖 -8.38%，均值差 1.18pp。
            "by_fill_kind": _fill_kind_block(panel, cost_bps),
            "ladder": ladder,
            "recent_1y": recent_stats,
            "recent_1y_n": len(recent),
        },
        # 先看这道门，再看 conditioning：打板的期望 = P(封住) × 封住之后的值。
        # 只有 conditional 时，"3 板强封单 +2.91%"读起来像结论，其实它是条件
        # 期望；乘上回封率才是这只票这个身位的无条件期望。
        "base_rate": _base_rate(desk, boards, day, cost_bps),
        "caveats": caveats,
        "provenance": {"datasets": ["ths_limit_up", "daily", "daily_basic",
                                    "limit_up_pool"],
                       "window": f"{panel['trade_date'].min()}–{panel['trade_date'].max()}",
                       "rows": len(panel),
                       "rows_with_forward_ret": int(panel["ret"].notna().sum()),
                       "trust_tier": "T1"},
    }
