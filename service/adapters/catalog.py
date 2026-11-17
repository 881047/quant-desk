"""Dataset catalog: one place that declares what we store and how.

Conventions
-----------
- Dates are kept as ``YYYYMMDD`` strings, exactly as the exchanges publish them.
  This avoids timezone/frequency bugs and makes filters lexicographic.
- ``partition`` is "all" (small reference tables), "year" (daily panels) or
  "day" (intraday snapshots / limit-up pools).
- ``pk`` is the natural key used for de-duplication and deterministic hashing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The T0–T3 vocabulary that ``DatasetSpec.trust_tier`` values come from. Nothing
# imports this dict: the specs carry the bare strings, and readers compare them.
# It is kept because this is the only place the tiers are defined in words — a
# new dataset's tier is a judgement call, and the scale has to be readable.
TRUST_TIERS = {
    "T0": "exchange / Tushare official",
    "T1": "EastMoney / akshare",
    "T2": "media / CLS",
    "T3": "web search / unverified",
}


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    source: str
    trust_tier: str
    partition: str  # all | year | day
    pk: tuple[str, ...]
    date_column: str
    required: tuple[str, ...] = ()
    description: str = ""
    on_demand: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


_DAILY_PANEL = ("ts_code", "trade_date")

SPECS: dict[str, DatasetSpec] = {
    s.name: s
    for s in [
        DatasetSpec(
            "trade_cal", "tushare", "T0", "all", ("cal_date",), "cal_date",
            ("cal_date", "is_open"), "交易日历（含 pre_trade_date）", tags=("reference",),
        ),
        DatasetSpec(
            "stock_basic", "tushare", "T0", "all", ("ts_code",), "list_date",
            ("ts_code", "name", "list_date"), "股票列表，含退市与上市状态", tags=("reference",),
        ),
        DatasetSpec(
            "index_member", "tushare", "T0", "all", ("ts_code",), "in_date",
            ("ts_code", "name", "in_date"), "申万三级行业归属（当前快照，无历史）",
            tags=("reference",),
        ),
        DatasetSpec(
            "namechange", "tushare", "T0", "all", ("ts_code", "start_date"), "start_date",
            ("ts_code", "name", "start_date"), "曾用名，用于 ST 判定", tags=("reference",),
        ),
        DatasetSpec(
            "suspend_d", "tushare", "T0", "year", ("ts_code", "trade_date"), "trade_date",
            ("ts_code", "trade_date"), "每日停复牌",
        ),
        DatasetSpec(
            "daily", "tushare", "T0", "year", _DAILY_PANEL, "trade_date",
            ("ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"),
            "A 股未复权日线（撮合与质量基准）",
        ),
        DatasetSpec(
            "adj_factor", "tushare", "T0", "year", _DAILY_PANEL, "trade_date",
            ("ts_code", "trade_date", "adj_factor"), "复权因子",
        ),
        DatasetSpec(
            "daily_basic", "tushare", "T0", "year", _DAILY_PANEL, "trade_date",
            ("ts_code", "trade_date", "close"), "每日指标：换手/市值/估值",
        ),
        DatasetSpec(
            "stk_limit", "tushare", "T0", "year", _DAILY_PANEL, "trade_date",
            ("ts_code", "trade_date", "up_limit", "down_limit"),
            "每日涨跌停价（交易所口径；0 或 99999.99 表示无涨跌幅，不是缺失）",
        ),
        DatasetSpec(
            "dividend", "tushare", "T0", "all",
            ("ts_code", "end_date", "div_proc", "ann_date"), "ex_date",
            ("ts_code", "ann_date", "end_date"), "分红送转（按公告日取，PIT）", on_demand=True,
        ),
        DatasetSpec(
            "fund_basic", "tushare", "T0", "all", ("ts_code",), "list_date",
            ("ts_code", "name"), "ETF/基金列表", tags=("reference",),
        ),
        DatasetSpec(
            "fund_daily", "tushare", "T0", "year", _DAILY_PANEL, "trade_date",
            ("ts_code", "trade_date", "open", "high", "low", "close", "vol"),
            "ETF 日线（未复权）",
        ),
        DatasetSpec(
            "fund_adj", "tushare", "T0", "year", _DAILY_PANEL, "trade_date",
            ("ts_code", "trade_date", "adj_factor"), "ETF 复权因子",
        ),
        DatasetSpec(
            "fund_nav", "tushare", "T0", "year", ("ts_code", "nav_date"), "nav_date",
            ("ts_code", "nav_date", "unit_nav"), "ETF 净值（用于溢价率）",
            # Opt-in like moneyflow/dividend: in FULL_DATASETS, read by no service
            # path. The premium-rate question it answers has not been asked yet.
            on_demand=True,
        ),
        DatasetSpec(
            "cb_basic", "tushare", "T0", "all", ("ts_code",), "list_date",
            ("ts_code", "bond_short_name", "stk_code", "list_date", "delist_date"),
            "可转债列表（T+0 品种）", tags=("reference",),
        ),
        DatasetSpec(
            "cb_daily", "tushare", "T0", "year", _DAILY_PANEL, "trade_date",
            ("ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"),
            "可转债日线（未复权，T+0）",
        ),
        DatasetSpec(
            "index_daily", "tushare", "T0", "year", _DAILY_PANEL, "trade_date",
            ("ts_code", "trade_date", "close"), "指数日线（regime/基准）",
        ),
        DatasetSpec(
            "fina_indicator", "tushare", "T0", "all",
            ("ts_code", "end_date", "ann_date"), "end_date",
            ("ts_code", "ann_date", "end_date"), "财务指标（必须用 ann_date 做 PIT）",
            # This token refuses a period-only query (`必填参数, ts_code`), so the
            # only shape that works is one call per symbol covering the whole
            # range. That makes it a single table rather than a year partition —
            # otherwise the same symbol is fetched once per year for the same
            # answer. Quarterly by nature, so it stays out of the daily job.
            on_demand=True,
        ),
        DatasetSpec(
            "moneyflow", "tushare", "T0", "year", _DAILY_PANEL, "trade_date",
            ("ts_code", "trade_date"), "个股资金流", on_demand=True,
        ),
        DatasetSpec(
            "margin_detail", "tushare", "T0", "year", _DAILY_PANEL, "trade_date",
            ("ts_code", "trade_date"), "融资融券明细（融资余额/买入额/偿还额）",
            on_demand=True,
        ),
        DatasetSpec(
            "moneyflow_hsgt", "tushare", "T0", "year", ("trade_date",), "trade_date",
            ("trade_date",), "沪深港通资金流向（北向净流入）", on_demand=True,
        ),
        DatasetSpec(
            "share_float", "tushare", "T0", "all",
            ("ts_code", "float_date", "holder_name", "ann_date"), "float_date",
            ("ts_code", "ann_date", "float_date"), "限售解禁（按解禁日，不是公告日）",
            # The vendor's `ann_date` mode returns *announcements*, which include
            # unlocks scheduled for 2030. A calendar needs `float_date`, so the
            # range filter runs on that column.
            on_demand=True,
        ),
        DatasetSpec(
            "stk_holdernumber", "tushare", "T0", "all",
            ("ts_code", "end_date", "ann_date"), "end_date",
            ("ts_code", "ann_date", "end_date"), "股东户数（季度披露，看筹码集中还是分散）",
            on_demand=True,
        ),
        DatasetSpec(
            "top10_floatholders", "tushare", "T0", "all",
            ("ts_code", "end_date", "holder_name", "ann_date"), "end_date",
            ("ts_code", "ann_date", "end_date"), "十大流通股东（集中度与机构占比）",
            on_demand=True,
        ),
        DatasetSpec(
            "pledge_stat", "tushare", "T0", "all", ("ts_code", "end_date"), "end_date",
            ("ts_code", "end_date"), "股权质押比例（季末快照，全市场一次可取）",
            on_demand=True,
        ),
        DatasetSpec(
            "stk_holdertrade", "tushare", "T0", "all",
            ("ts_code", "ann_date", "holder_name", "change_vol"), "ann_date",
            ("ts_code", "ann_date", "holder_name"), "股东增减持（区间可取，不必逐票）",
            on_demand=True,
        ),
        DatasetSpec(
            "limit_up_pool", "eastmoney", "T1", "day", ("trade_date", "code"), "trade_date",
            ("trade_date", "code", "name"), "涨停池（含封单/首封时间/连板数）",
        ),
        DatasetSpec(
            "broken_board_pool", "eastmoney", "T1", "day", ("trade_date", "code"), "trade_date",
            ("trade_date", "code", "name"), "炸板池",
        ),
        DatasetSpec(
            "limit_down_pool", "eastmoney", "T1", "day", ("trade_date", "code"), "trade_date",
            ("trade_date", "code", "name"), "跌停池",
        ),
        DatasetSpec(
            "stk_mins", "eastmoney", "T1", "day", ("ts_code", "trade_time"),
            "trade_date", ("ts_code", "trade_time", "close"),
            "5 分钟线（东财/腾讯，免费）：未复权，用于盘中止损与成交校准",
            # EastMoney/Tencent are shallow, but baostock can backfill a date range
            # with one query per symbol. `intraday_sweep.py` owns that budgeted,
            # single-connection path; the free HTTP adapters remain forward-only.
            on_demand=True, tags=("intraday",),
        ),
        DatasetSpec(
            "rt_quote", "eastmoney", "T1", "day", ("snapshot_ts", "code"), "trade_date",
            ("snapshot_ts", "code", "last"), "实时快照（盘中落盘，供滑点校准）",
            # Snapshot-only by definition: a quote panel can only be written while
            # the session is open, so no evening job can keep it current. Its
            # freshness is reported by the brief's `quotes` block instead, which
            # separates "the feed answers" from "an order could fill here".
            tags=("snapshot",),
        ),
        DatasetSpec(
            # 封单轨迹。东财批量行情不带五档，腾讯 `qt.gtimg.cn` 带，而且一次
            # 能给 50 个代码——所以只补池子里那几十只，不是全市场 5,500 只。
            # 主键带 `snapshot_ts`，`mode="merge"` 才会逐帧追加而不是覆盖：
            # 池子本身（`limit_up_pool`）的主键是 `(trade_date, code)`，一天只
            # 留最后一帧，所以"封单从 8 万手掉到 2 万手"这件事在那里永远看不
            # 到。这一张表就是为了留住那个过程。
            "pool_tape", "tencent", "T1", "day", ("snapshot_ts", "code"), "trade_date",
            ("snapshot_ts", "code", "pool"), "涨停/炸板池的盘口时序（封单量轨迹）",
            tags=("snapshot", "aggressive"),
        ),
        DatasetSpec(
            "limit_pool_t0", "derived:tushare", "T0", "year",
            ("trade_date", "code", "pool"), "trade_date",
            ("trade_date", "code", "pool"),
            "由日线+涨跌停价派生的涨停/炸板/跌停池（含连板数），覆盖 2023+",
            tags=("derived", "aggressive"),
        ),
        # 逐帧日内特征。主键带 frame_id：分析层的最小粒度从「交易日」降到
        # 「某一批全市场截面」。旧 rt_quote 分区没有 frame_id，派生脚本会显式
        # 报 legacy_partition_no_frame_id，绝不拿 snapshot_ts 假装帧。
        DatasetSpec(
            "intraday_features", "derived:rt_quote", "T1", "day",
            ("day", "frame_id", "code"), "day",
            ("day", "frame_id", "code", "frame_at", "bucket_5min", "shape"),
            "逐帧全市场日内特征（由带 frame_id 的 rt_quote 派生，零新数据源）",
            tags=("derived", "intraday"),
        ),
        # 免费源离线校准集。T1 不是 T3：catalog 的 T1 定义本就含 akshare，
        # 标 T3 会让 information.py 把免费行情误判为未核实。
        # 用途隔离靠 on_demand + tags + OFFLINE_CALIBRATION_ONLY 常量表达，
        # 不给 DatasetSpec 新增 usage 字段（会牵动全部 spec 构造）。
        DatasetSpec(
            "akshare_calibration", "akshare", "T1", "day",
            ("trade_date", "symbol", "quote_ts"), "trade_date",
            ("trade_date", "symbol", "quote_ts", "last",
             "bid1_volume", "ask1_volume", "schema_version"),
            "AKShare 免费源逐笔/五档离线校准集（只做盘后校准，不进撮合定价与实时决策链）",
            on_demand=True,
            tags=("offline_calibration", "intraday"),
        ),
        # Tonghuashun special data: the tape's own bookkeeping for short horizons.
        DatasetSpec(
            "ths_limit_up", "ths", "T1", "day", ("trade_date", "ts_code"), "trade_date",
            ("trade_date", "ts_code", "name", "pct"), "涨停/连板池（含封单、首封时间、原因）",
            tags=("special", "short"),
        ),
        DatasetSpec(
            "ths_limit_down", "ths", "T1", "day", ("trade_date", "ts_code"), "trade_date",
            ("trade_date", "ts_code", "name", "pct"), "跌停池",
            tags=("special", "short"),
        ),
        DatasetSpec(
            "ths_limit_break", "ths", "T1", "day", ("trade_date", "ts_code"), "trade_date",
            ("trade_date", "ts_code", "name", "pct"), "炸板池（含开板次数）",
            tags=("special", "short"),
        ),
        DatasetSpec(
            "ths_ladder", "ths", "T1", "day", ("trade_date", "board", "ts_code"),
            "trade_date", ("trade_date", "board", "ts_code"),
            "连板天梯（长表，只落当日那一层）", tags=("special", "short"),
        ),
        DatasetSpec(
            "ths_limit_stat", "ths", "T1", "day", ("trade_date", "level"), "trade_date",
            ("trade_date", "level", "stock_count"), "封板率与连板晋级率",
            tags=("special", "short"),
        ),
        DatasetSpec(
            "ths_lhb", "ths", "T1", "day", ("trade_date", "ts_code", "range_days"),
            "trade_date", ("trade_date", "ts_code", "name"),
            "龙虎榜个股维度（净买额/机构/游资/概念/原因）。主键含 range_days（1/3/10/30 日榜）："
            "同一只票同一天最多四行，net_value 是各自窗口的累计值（实测同日可差 50 倍）。"
            "混用几种口径做截面排名会造出假信号——2026-11-12 实测 t=−3.18 在只留一日榜后"
            "降到 −1.27。读这张表必须先选 range_days",
            tags=("special", "short", "multi_caliber"),
        ),
        DatasetSpec(
            "ths_lhb_seat", "ths", "T1", "day", ("trade_date", "seat", "ts_code"),
            "trade_date", ("trade_date", "seat", "ts_code"),
            "龙虎榜游资席位维度", tags=("special", "short"),
        ),
        DatasetSpec(
            "ths_auction", "ths", "T1", "day", ("trade_date", "ts_code", "warn_name"),
            "trade_date", ("trade_date", "ts_code", "warn_name"),
            "集合竞价异动（抢筹/砸盘/高低开），首次/末次告警与原始事件",
            tags=("special", "short"),
        ),
        DatasetSpec(
            "ths_auction_freeze", "ths", "T1", "day",
            ("trade_date", "ts_code", "warn_name"), "trade_date",
            ("trade_date", "ts_code", "warn_name"),
            "09:26 竞价冻结快照；只写一次，盘后刷新不得覆盖",
            on_demand=True, tags=("special", "short", "auction_freeze"),
        ),
        DatasetSpec(
            "ths_anomaly", "ths", "T2", "day", ("trade_date", "ts_code"), "trade_date",
            ("trade_date", "ts_code", "tag"),
            "个股异动原因（上游 AI 归纳的文本线索，非公告原文）",
            tags=("special", "short", "text"),
        ),
        # Text is fetched outside the market-data pipeline (WorkBuddy reads the
        # web; the desk has no internet), then normalised and stored here so it
        # is queryable, PIT-filterable and auditable like everything else.
        DatasetSpec(
            "news", "workbuddy", "T3", "day", ("news_id",), "pub_date",
            ("news_id", "pub_date", "pub_time", "title", "source", "kind", "tier"),
            "公告/新闻/研报文本；PIT 以 pub_date+pub_time 为准，收盘后发布的不进当日",
            tags=("text",),
        ),
        DatasetSpec(
            "anns", "cninfo", "T2", "all", ("announcement_id",), "pub_date",
            ("announcement_id", "pub_date", "ts_code", "title"),
            "公告元数据（巨潮资讯网全文检索）：标题、类别、PDF 链接；日期为日级精度",
            # One call per symbol, so the backfill unit is a code, not a date: a
            # year-partitioned table would have to refetch every symbol per year
            # for the same answer. `on_demand` because nothing live blocks on it —
            # the daily step covers the watchlist, the rest is fetched when asked.
            on_demand=True, tags=("text",),
        ),
        # Forward-looking fundamentals. Unlike a filing, a forecast is a
        # *prediction*, not a reported fact: it must always be labelled as such
        # and cut on ann_date (PIT). Both APIs accept ann_date without ts_code,
        # so one call per day covers the whole market. Maintained by
        # scripts/sync_forecast.py, deliberately outside the default backfill
        # groups: a missing forecast is not a broken daily pipeline.
        DatasetSpec(
            "forecast", "tushare", "T0", "all",
            ("ts_code", "ann_date", "end_date"), "ann_date",
            ("ts_code", "ann_date", "end_date", "type"),
            "业绩预告（预测，不是已实现业绩）：类型、净利变动区间、摘要与变动原因",
            on_demand=True, tags=("fundamental", "forward"),
        ),
        DatasetSpec(
            "express", "tushare", "T0", "all",
            ("ts_code", "ann_date", "end_date"), "ann_date",
            ("ts_code", "ann_date", "end_date"),
            "业绩快报（早于正式财报的初步数字，仍非审计数）",
            on_demand=True, tags=("fundamental", "forward"),
        ),
    ]
}

# Default backfill set: everything needed for a daily-bar strategy.
P1_DATASETS = [
    "trade_cal", "stock_basic", "index_member", "namechange", "suspend_d",
    "daily", "adj_factor", "daily_basic", "stk_limit",
    "fund_basic", "fund_daily", "fund_adj", "index_daily",
]

# Additional research datasets, opt-in (slower / heavier).
FULL_DATASETS = P1_DATASETS + ["fund_nav", "fina_indicator", "moneyflow",
                               "margin_detail", "moneyflow_hsgt", "dividend",
                               "share_float", "stk_holdernumber", "top10_floatholders",
                               "pledge_stat", "stk_holdertrade"]

POOL_DATASETS = ["limit_up_pool", "broken_board_pool", "limit_down_pool", "rt_quote"]

# The tape, part one: the three 东财 pools, which this backfiller can fetch.
# Separate from P1 because they come from a different vendor and have their own
# history floors (limit-up pool from 2020-12, the other two rolling one year).
# They were missing from the daily job until 2026-10-26, which is why the
# dashboard could show 涨停 82 家 read from a three-day-old partition.
PANEL_DATASETS = ["limit_up_pool", "broken_board_pool", "limit_down_pool"]

# The tape, part two: 同花顺 panels. They have no fetcher in this backfiller —
# `scripts/fetch_ths_special.py` owns them, because every endpoint has its own
# history limit and the ladder ignores its date parameter entirely. Listing them
# here keeps one place that names the whole tape, and the daily job runs both.
THS_DATASETS = [
    "ths_limit_up", "ths_limit_down", "ths_limit_break",
    "ths_ladder", "ths_limit_stat", "ths_lhb", "ths_lhb_seat",
    "ths_auction", "ths_anomaly",
]

# What the 17:30 job refreshes, step by step. Declared here instead of inferred
# from the script because this is a contract, and the contract has a test:
# every dataset in SPECS must be scheduled, reference, opt-in or snapshot-only.
# A dataset that is none of the four is a dataset nobody owns — which is how
# cb_daily, news, limit_pool_t0 and stk_mins each sat behind on 2026-10-26 while
# the bars they describe were current. Reference tables ride along inside the
# steps that fetch them (cb_basic inside cb_bars, stock_basic inside backfill_p1)
# and are not listed separately, because their `cover_end` is not a boundary.
DAILY_JOB_STEPS: dict[str, tuple[str, ...]] = {
    "backfill_p1": tuple(P1_DATASETS),
    "backfill_panel": tuple(PANEL_DATASETS),
    "ths_panels": tuple(THS_DATASETS),
    "derived_pools": ("limit_pool_t0",),
    "intraday_features": ("intraday_features",),
    "cb_bars": ("cb_daily",),
    "text": ("news",),
}

SCHEDULED_DATASETS = frozenset().union(*DAILY_JOB_STEPS.values())

#: 用途隔离清单：这些数据集**只许盘后离线校准**，不进 ``realtime.service_for``
#: / ``from_frame``、不进撮合定价、不进实时决策链。AKShare 抓取第三方网页，
#: 接口会随上游改版失效；把它接进实时链路等于让一次网页改版改动成交价。
#: 有测试断言 realtime.py 不引用这些名字，改名时两边一起改。
OFFLINE_CALIBRATION_ONLY = frozenset({"akshare_calibration"})

# `cover_end` 有两种互不相容的语义，接口以前只给一个裸日期：
#   * 逐日面板（daily / adj_factor）：cover_end 是"数据抓到哪天"，落后一天就是缺一天；
#   * 日历与日程（trade_cal / share_float）：cover_end 是"表里排到未来的最后一个日期"。
#     2027-12-31 不是抓到了未来数据，是交易日历本来就排到 2027 年、解禁表本来就
#     排到 2030 年。按 cover_end 做通用新鲜度排序会把日历顶到第一名。
# `/data/status` 以前不带任何类型标志，前端只能放弃按覆盖日排序（见
# docs/14-backend-debt-from-frontend-20261108.md A3）。分类放在这里，是因为
# `/data/brief` 的滞后判定早就在用同一套 `tags` / `on_demand` 规则——两个消费方
# 各判一次迟早分叉。
CALENDAR_DATASETS = frozenset({"trade_cal", "share_float"})
SNAPSHOT_DATASETS = frozenset({"rt_quote"})


def dataset_kind(name: str) -> str:
    """这个数据集的 ``cover_end`` 该怎么读；值域见 :data:`DATASET_KINDS`。

    未知数据集按 ``"data"`` 处理：宁可误报一次滞后，也不要静默放过一天缺失。
    """
    if name in CALENDAR_DATASETS:
        return "calendar"
    if name in SNAPSHOT_DATASETS:
        return "snapshot"
    spec = SPECS.get(name)
    if spec is None:
        return "data"
    if "reference" in spec.tags:
        return "reference"
    if spec.on_demand:
        return "on_demand"
    return "data"


def coverage_boundary(name: str) -> bool:
    """``cover_end`` 是不是逐日面板的覆盖边界（只有它是，滞后才有意义）。"""
    return dataset_kind(name) == "data"


def get_spec(name: str) -> DatasetSpec:
    try:
        return SPECS[name]
    except KeyError as exc:  # pragma: no cover - programmer error
        raise KeyError(f"unknown dataset {name!r}; known: {sorted(SPECS)}") from exc


def resolve(names: list[str] | None) -> list[str]:
    """Expand 'all' / 'p1' / 'pools' shortcuts into concrete dataset names."""
    if not names:
        return list(P1_DATASETS)
    # A refresh may replace catalog entries in another thread. Iterate one stable
    # snapshot instead of mutating or walking the process-global dictionary.
    specs = SPECS.copy()
    out: list[str] = []
    for raw in names:
        key = raw.strip()
        if not key:
            continue
        if key == "all":
            out.extend(specs)
        elif key == "p1":
            out.extend(P1_DATASETS)
        elif key == "full":
            out.extend(FULL_DATASETS)
        elif key == "pools":
            out.extend(POOL_DATASETS)
        elif key == "panel":
            out.extend(PANEL_DATASETS)
        elif key in specs:
            out.append(key)
        else:
            raise KeyError(f"unknown dataset {key!r}")
    # de-duplicate, keep order
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]
