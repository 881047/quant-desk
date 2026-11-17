"""Read-only answer to "what data could produce short-horizon alpha, and what
actually enters the alpha estimate".

The 2026-11-12 diagnosis found 46 ingested datasets but only a handful on the
alpha-estimation path. The honest first fix is to make that gap *measurable*,
not to quietly add factors: every new factor x horizon is a new hypothesis test,
and adding one without registering it in ``data/research.sqlite`` would shrink
the Deflated-Sharpe denominator dishonestly (the P22 incident). This module
therefore reports the gap and the integration rule; it runs no test, writes no
trial, produces no signal, verdict, ranking or action.

The dataset lists below mirror the actual ``store.read(...)`` call sites in
``service/research/spec.py``, ``runner.py``, ``factors.py`` and
``service/trading/analog.py``. ``tests/research/test_breadth.py`` asserts those
names still appear in the source so the list cannot drift away from the code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_DAY_PARTITION_RE = re.compile(r"^\d{8}\.parquet$")


def _usable_days(store, name: str) -> dict[str, Any]:
    """How many *days* this dataset actually has — not how many rows.

    ``rows`` is the number that misleads: ``intraday_features`` reported
    3,010,268 rows and had **4** daily partitions. On 2026-11-12 that gap cost a
    wrong turn ("301 万行，可以跑了"). Row count answers "how big", day count
    answers "how many independent observations", and only the second one decides
    whether an IC can be measured at all.

    Counted from the parquet tree rather than from ``registry.summary()``:
    that returns *partition* count, and annual-partition datasets
    (``margin_detail``, ``moneyflow``) hold 449 days inside 2 files. A count of
    2 would be worse than no answer.
    """
    root = getattr(store, "root", None)
    if root is None:
        return {"usable_days": None, "partition_kind": "unknown"}
    directory = Path(root) / "parquet" / name
    if not directory.is_dir():
        return {"usable_days": None, "partition_kind": "missing"}
    try:
        names = list(directory.iterdir())
    except OSError:
        return {"usable_days": None, "partition_kind": "unreadable"}
    day_files = sum(1 for f in names if _DAY_PARTITION_RE.match(f.name))
    if day_files:
        return {"usable_days": day_files, "partition_kind": "day"}
    annual = sum(1 for f in names if f.suffix == ".parquet")
    return {"usable_days": None,
            "partition_kind": "annual" if annual else "unknown",
            "files": annual}

#: Datasets actually read on the alpha-estimation path (deduplicated).
#: ``index_member`` supplies industry labels only; ``trade_cal`` / ``stock_basic``
#: are metadata; ``cb_basic`` / ``cb_daily`` serve the convertible-book pools.
ALPHA_INPUT_DATASETS: tuple[str, ...] = (
    "daily", "daily_basic", "moneyflow", "stk_limit", "limit_pool_t0",
    "cb_basic", "stock_basic", "trade_cal", "index_member",
)

#: Of those, the ones that actually carry a price / flow / valuation signal.
SIGNAL_CARRYING_DATASETS: tuple[str, ...] = (
    "daily", "daily_basic", "moneyflow", "stk_limit", "limit_pool_t0", "cb_basic",
)

#: Ingested, short-horizon-relevant, but NOT on the alpha-estimation path.
#: ``why`` is the concrete blocker, not a generic "not wired up yet".
SHORT_HORIZON_CANDIDATES: dict[str, str] = {
    "intraday_features": "日内条件分布；研究 spec 最细粒度是日线，需要新的事件形态网格才能入 α",
    "stk_mins": "分钟线；同上，且分钟级成交假设需单独验证",
    "rt_quote": "实时快照；只用于盘中读数与覆盖度自评，未做 PIT 回放",
    "pool_tape": "涨停池分帧过程；只进 seal_tape 知识层，不进策略级 α",
    "ths_auction": "集合竞价；只在 auction / limit_structure 读数里用",
    "ths_lhb": "龙虎榜；只在 lhb 知识层用，未做席位行为的横截面因子",
    "ths_lhb_seat": "龙虎榜席位明细；同上",
    "ths_limit_up": "同花顺涨停口径；只用于与自算池对账（vendor 差异已记录）",
    "ths_limit_break": "炸板厂商口径；未进池子构造",
    "ths_limit_down": "跌停厂商口径；未进池子构造",
    "ths_limit_stat": "涨停统计；只做覆盖率自评",
    "ths_ladder": "连板梯队；只在 ladder 读数里用",
    "ths_anomaly": "异动；只在 anomaly 知识层用",
    "anns": "公告；PIT 键是 ann_date，与收盘面板不同源，正文覆盖率也低",
    "forecast": "业绩预告；未做 PIT 对齐",
    "express": "业绩快报；样本仅十几条，未接入",
    "fina_indicator": "财务指标；PIT 键 ann_date，窗口内可用期数太少",
    "margin_detail": "两融明细；未做横截面因子",
    "moneyflow_hsgt": "北向资金；未做横截面因子",
    "limit_up_pool": "涨停池快照；策略层用的是 limit_pool_t0，此表只做对账",
    "broken_board_pool": "炸板池快照；同上",
    "limit_down_pool": "跌停池快照；同上",
}

INTEGRATION_RULE = (
    "把这些数据集接进 α 估计 = 新增 (因子 × 期限) 检验。必须先经 "
    "quant_hypothesis(add) 预注册、把新增变体登记进 TrialRegistry"
    "（data/research.sqlite 的 trials 表），再让统计裁判判；"
    "否则 DSR 分母失真，等于回到 P22 事故。"
    "改默认因子族还会破坏已交付报告的 36 检验口径，须作为一次独立迁移处理。"
)


def _days_gap(rows: list[dict[str, Any]], store) -> dict[str, Any]:
    """Which datasets are too short to measure anything on, stated up front.

    ``MIN_IC_DAYS`` is 20 and the factor layer's practical bar is 120 sessions.
    A dataset with 4 daily partitions cannot answer a 1/3/5/10-horizon IC no
    matter how many rows it holds — that is the single most expensive misreading
    this layer is meant to prevent.
    """
    short: list[dict[str, Any]] = []
    for r in rows:
        name = str(r.get("dataset"))
        info = _usable_days(store, name)
        days = info.get("usable_days")
        if days is not None and days < 120:
            short.append({"dataset": name, "usable_days": days,
                          "rows": int(r.get("rows") or 0),
                          "note": ("20 天以下无法算 IC" if days < 20
                                   else "过 20 天门槛但低于 120 天的稳健线")})
    short.sort(key=lambda x: x["usable_days"])
    return {
        "threshold_sessions": 120,
        "below_threshold_count": len(short),
        "datasets": short,
        "note": ("usable_days < 120 的数据集不适合单独做因子研究。"
                 "它们可以进知识层读数，但不能进 α 估计——所有实验都是"
                 "（因子 × 期限）的一次检验，样本不足时得到的是噪声。"),
    }


def breadth_read(desk) -> dict[str, Any]:
    """One read-only snapshot of the data-breadth gap."""
    try:
        rows = desk.market.store.registry.summary()
    except Exception as exc:  # noqa: BLE001 - a missing registry is an answer
        return {"ok": False, "topic": "breadth", "status": "registry_unavailable",
                "error": f"{type(exc).__name__}: {exc}", "read_only": True}

    by_name = {str(r.get("dataset")): r for r in rows}

    def brief(name: str) -> dict[str, Any]:
        r = by_name.get(name)
        if not r:
            return {"dataset": name, "available": False, **_usable_days(desk.market.store, name)}
        return {
            "dataset": name, "available": True,
            "rows": int(r.get("rows") or 0),
            **_usable_days(desk.market.store, name),
            "cover_start": str(r.get("cover_start") or ""),
            "cover_end": str(r.get("cover_end") or ""),
            "trust_tier": r.get("trust_tier"),
        }

    in_alpha = []
    for name in ALPHA_INPUT_DATASETS:
        item = brief(name)
        item["role"] = ("signal" if name in SIGNAL_CARRYING_DATASETS
                        else "metadata_or_label")
        in_alpha.append(item)

    unused = []
    for name, why in SHORT_HORIZON_CANDIDATES.items():
        item = brief(name)
        item["why_not_in_alpha"] = why
        unused.append(item)

    present_unused = [u for u in unused if u.get("available")]
    return {
        "ok": True,
        "topic": "breadth",
        "read_only": True,
        "dataset_count": len(rows),
        "in_alpha_estimate_count": len(in_alpha),
        "signal_carrying_count": len(SIGNAL_CARRYING_DATASETS),
        "unused_short_horizon_count": len(present_unused),
        "not_in_alpha_count": len(rows) - len(in_alpha),
        "in_alpha_estimate": in_alpha,
        "short_horizon_unused": unused,
        "integration_rule": INTEGRATION_RULE,
        "days_gap": _days_gap(rows, desk.market.store),
        "caveats": [
            "这是数据覆盖度读数，不是 α 估计，也没有新增任何检验",
            "没有 action / verdict / signal / score / ranking / 目标价",
            "in_alpha_estimate 依据源码里实际的 store.read 调用点，不是主观分类",
            "已入库 ≠ 可成交、≠ PIT 干净；anns / fina_indicator 的 ann_date 键与收盘面板不同源",
            ("rows 是行数，不是可用样本天数：intraday_features 报了 301 万行，"
             "实际只有 4 个日分区。判断能不能做 IC 看 usable_days，不要看 rows"),
            ("usable_days 只对日分区数据集可数；年分区（daily / moneyflow / margin_detail 等）"
             "报 null 并给 cover_start..cover_end，天数要按那个跨度加交易日历算，不要用文件数当天数"),
        ],
        "note": ("本层只读：不写 store、不登记假设、不改 trial 分母、不进 arena.edge。"
                 "它回答『α 还能从哪些数据里来』，不回答『买什么』"),
        "source": ["store.registry.summary()",
                   "service/research/spec.py", "service/research/runner.py",
                   "service/research/factors.py", "service/trading/analog.py"],
    }
