"""How much of the market can this desk actually see — as a number with reasons.

The question this answers is not "how many rows do we have". It is: *for each
decision the desk is allowed to make, is every input that could change that
decision present, fresh, point-in-time, and agreeing with the other sources?*

Three failure modes make a naive row count useless, and all three have already
happened here:

* **Present but hollow.** ``limit_pool_t0`` held 444k rows spanning 2010-2026
  while the last 200 sessions of 2026 were missing. Any coverage check that
  looks at ``cover_start``/``cover_end`` calls that complete, because both ends
  are populated.
* **Present but wrong.** The one 2026 session that did exist was derived without
  warm-up, so every stock's streak read 1. The dataset was fresh, complete for
  its single day, and false.
* **Present twice, disagreeing.** On 2026-10-28 the derived pool said the ladder
  topped at one board; the THS ladder said five. The model saw both.

So each dimension is scored on **recent sessions actually present**, not span;
each score is discounted by staleness; and facts that more than one source
claims are read from every source and compared on the spot.

Scoring is ``min`` across a dimension's datasets, not a mean: a chain of evidence
is as strong as its weakest link, and averaging lets one good dataset hide one
missing one. Missing a dimension sets it to zero rather than skipping it — the
headline number should fall when something disappears, not stay flat.

Nothing here decides anything. It reports coverage, conflicts and the exact
command that closes each hole.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from service import settings
from service.adapters.catalog import SPECS, get_spec
from service.adapters.jsonio import write_json_atomic
from service.research.pools import promotion_series

# How many sessions back "recent" reaches. One trading year is the horizon over
# which a regime prior is still the same regime; a dataset that stops before it
# cannot describe today.
LOOKBACK = settings.integer("research.lookback")

# Reading 250 sessions of the four backbone datasets costs about two seconds
# each. Everything else is small. The score is cached against the manifest, so
# this is paid on the first call after an ingest, not on every call.
CACHE = Path("data") / "cache" / "information.json"

# Masters are keyed by their own date (list_date, cal_date, ann_date), not by
# session, so "how many of the last 250 sessions are here" is not a question they
# can answer — asking it returns zero and would score a complete master as empty.
# They are judged on presence and on how long ago they were refreshed instead.
MASTER_GRACE = settings.integer("research.master_grace")


@dataclass(frozen=True)
class Requirement:
    """One dimension of information, and what it is for."""

    key: str
    label: str
    question: str
    datasets: tuple[str, ...]
    weight: float
    max_lag: int = 0
    # Sessions of history this dimension needs before it can support a prior.
    # A stream that has been captured perfectly for three days is not a prior,
    # and reporting it as 100% would be the most expensive kind of lie.
    min_history: int = 60
    blind: str = ""
    fix: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        key="price_volume",
        label="个股价量",
        question="这只票过去怎么走的、量能如何",
        datasets=("daily", "adj_factor"),
        weight=0.12,
        min_history=250,
        blind="不能谈任何个股的走势、均线、区间位置或量比",
        fix="python scripts/backfill.py --datasets daily,adj_factor",
    ),
    Requirement(
        key="tradability",
        label="可交易性",
        question="今天能不能买、能买多少、涨跌停在哪",
        datasets=("stk_limit", "suspend_d", "stock_basic", "namechange"),
        weight=0.10,
        min_history=250,
        blind="不能判断一笔委托是否可成交，也不能给涨跌停价或一手金额",
        fix="python scripts/backfill.py --datasets stk_limit,suspend_d,stock_basic,namechange",
    ),
    Requirement(
        key="limit_structure",
        label="涨停结构",
        question="封板率、连板高度、晋级率、炸板",
        # 四项里三项由派生池和同花顺池直接支撑：封板率/炸板来自 ``ths_limit_break``
        # （246 天，之前一直没计分），连板高度来自 ``limit_pool_t0.boards``（250 天），
        # 晋级率由 ``promotion_series`` 从同一个派生池算（250 天）。
        #
        # ``ths_limit_stat``（5 天，实测 0.042）与 ``ths_ladder``（34 天，0.283）
        # 都不在这张清单里，因为它们**不再是输入**：晋级率已经自己算，天梯只用来
        # 交叉验证最高连板。这不是"把短板排除掉"，是那条链换了一条有历史的边——
        # 两个口径的分歧由 ``_reconcile`` 摆出来，不藏在分数里。
        datasets=("limit_pool_t0", "ths_limit_up", "ths_limit_break"),
        weight=0.10,
        min_history=120,
        blind="不能谈情绪周期、连板高度、能不能追强",
        fix="python scripts/derive_pools.py --start 20260101 --warmup-days 400",
    ),
    Requirement(
        key="flow",
        label="资金与杠杆",
        question="谁在买、融资盘在加还是在减",
        datasets=("moneyflow", "margin_detail"),
        weight=0.08,
        max_lag=1,
        min_history=120,
        blind="不能声称主力资金流入流出，也不能谈两融余额变化",
        fix="python scripts/backfill.py --datasets moneyflow,margin_detail",
    ),
    Requirement(
        key="fundamentals",
        label="基本面",
        question="已公告的财报与估值分位",
        datasets=("fina_indicator", "daily_basic", "dividend"),
        weight=0.09,
        max_lag=250,
        blind="不能谈业绩、估值高低、分红，任何 PE/PB 分位都不可引用",
        fix="python scripts/backfill.py --datasets fina_indicator,daily_basic,dividend",
    ),
    Requirement(
        key="events",
        label="公告与事件",
        question="有没有事、是什么事",
        datasets=("anns",),
        weight=0.11,
        max_lag=3,
        min_history=60,
        blind="不能说'没有公告'或'没有消息'——那只是没抓到",
        fix="python scripts/fetch_anns_text.py && python scripts/ingest_text.py",
    ),
    Requirement(
        key="etf_cb",
        label="ETF 与可转债",
        question="核心盘的 ETF 溢价、转债价格与转股价值",
        datasets=("fund_daily", "fund_adj", "cb_daily"),
        weight=0.08,
        min_history=250,
        blind="不能谈核心 ETF 的净值与折溢价，也不能给可转债价格——核心盘的全部输入都在这里",
        fix="python scripts/backfill.py --datasets fund_daily,fund_adj && python scripts/backfill_cb.py",
    ),
    Requirement(
        key="quotes",
        label="实时报价",
        question="现在这一刻多少钱",
        datasets=(),
        weight=0.08,
    ),
    Requirement(
        key="intraday_series",
        label="盘中序列",
        question="这半小时怎么走的、封板后有没有开过",
        datasets=("rt_quote", "stk_mins"),
        weight=0.06,
        min_history=120,
        blind=("5 分钟线可由 baostock 按区间回补，北交所与停牌无行单独记账；"
               "盘口回放不能由分钟线冒充。"
               "盘中全市场截面自 2026-11-07 起在裁剪前冷存到 rt_archive/<day>/，"
               "可用 /market/frame?as_of= 或 ?frame_id= 按帧回放"
               "（见 service/trading/replay.py，零前视）；**冷存上线之前的日子不可回补**，"
               "要回答\"那只票 20 天前下午怎么走的\"得先确认那天在库里"),
        fix=("每天盘后跑一次全市场（5,571 只 × 48 根 ≈ 3.3 MB）；补历史用 "
             "`intraday_sweep.py --days N`——请求数不变，时间按 N 线性涨。"
             "冷存由抓取循环 snapshot.prune_frames 自动转存，不需额外任务"),
    ),
    Requirement(
        key="lhb",
        label="龙虎榜",
        question="谁在买",
        datasets=("ths_lhb",),
        weight=0.04,
        max_lag=1,
        min_history=120,
        blind="不能说谁在买、机构还是游资，也不能引用席位名字",
        fix="python scripts/fetch_ths_special.py --only lhb",
    ),
    Requirement(
        key="auction",
        label="竞价与异动",
        question="开盘前发生了什么、为什么涨",
        datasets=("ths_auction", "ths_anomaly"),
        weight=0.04,
        max_lag=1,
        blind="竞价告警与机器生成异动线索不足时，不能据此解释开盘强弱或上涨原因",
        fix=("ths_auction 上游实测支持滚动一年：`fetch_ths_special.py --datasets "
             "ths_auction --days 60 --force`；ths_anomaly 仍是 TODAY_ONLY，"
             "漏日不能伪造。15:20 与 17:30 双跑继续积累"),
    ),
    Requirement(
        key="point_in_time",
        label="时点正确",
        question="用的是不是当天真能看到的信息",
        datasets=(),
        weight=0.10,
    ),
)

TOTAL_WEIGHT = sum(r.weight for r in REQUIREMENTS)

# Raw, pre-prune frame replay exists only from the day the archive was enabled.
# Older rt_quote manifests are useful snapshots, but they are not a replayable
# tape. Keep the public `intraday_series` key and its 0.06 weight, while scoring
# that tape from the real archive boundary and exposing both inputs separately.
TAPE_ARCHIVE_START = "20261107"

# Reading aid, NOT a gate. The weighted headline can sit at 89 while one
# dimension is at 0.061; a reader who only sees the total will assume every
# input is roughly complete. This threshold only decides which dimensions are
# *listed* in `weak_dimensions` — it changes no score, no weight, no permission.
# DQS practice calls this the per-dimension drill-down / minimum-threshold view:
# report the components, let the consumer decide, never let the mean hide one.
# It lives in params.yaml (like LOOKBACK / MASTER_GRACE / QUOTE_STALE_SECONDS)
# because "how low counts as worth naming" is a口径 someone may want to tune;
# a literal here would only be changeable by editing code.
WEAK_DIMENSION_CEILING = settings.num("research.weak_dimension_ceiling")

# One instrument per asset class the desk can be asked to trade. The classes do
# not share a quote path: stocks ride THS, ETFs ride EastMoney, Shenzhen
# convertibles ride Tencent, and Shanghai convertibles ride nothing — the lookup
# comes back empty. Scoring this from stored snapshots would have missed all of
# it, because a capability is not a file.
PROBE_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("stock", "600519.SH"),
    ("etf", "510300.SH"),
    ("cb_sh", "111012.SH"),
    ("cb_sz", "123122.SZ"),
)

# In session, a quote older than this is not a price you can trade on. Outside
# session every quote is hours old and that is normal, so the age is reported but
# not scored.
QUOTE_STALE_SECONDS = settings.num("research.quote_stale_seconds")

# Semantic freshness for quarterly fundamentals. A trading-day lag cannot tell
# "the next report is not due yet" from "the report was published and our ingest
# missed it", so this SLA measures coverage of the newest *stored* period.
FUNDAMENTAL_MIN_COVERAGE = settings.num("research.fundamental_min_coverage")
FUNDAMENTAL_MAX_STALE_DAYS = settings.integer("research.fundamental_max_stale_days")


def probe_quotes(*, desk: Any | None = None,
                 reader: Callable[[str], Any] | None = None,
                 in_session: bool | None = None,
                 session_label: str | None = None) -> dict:
    """Ask the live path for one instrument of each class and record the answer.

    ``reader`` is injectable so the scorecard can be tested without a running
    desk or a network. The default reader is the same one the quote tool uses —
    probing something else would measure a capability the model does not have.

    ``desk`` is passed in, not imported. This module is the research layer and
    ``trading`` sits above it: the import used to live inside this function to
    dodge a cycle, which meant any refactor that hoisted it to file scope broke
    the service at startup. A probe with no desk reports itself blind — which is
    exactly what this function is for.
    """
    if reader is None:
        if desk is None:
            return {"ok": False, "error": "no desk injected for the live-quote probe",
                    "classes": {}, "score": 0.0, "answered": 0,
                    "required": len(PROBE_SYMBOLS)}
        try:
            # One batched call, not one per class. Each miss walks THS ->
            # EastMoney -> Tencent with its own timeout, so four separate calls
            # cost four times the wall clock for the same four numbers: the probe
            # was taking 22s on a cached scorecard and made the tool look broken.
            fetched = desk.quotes([code for _, code in PROBE_SYMBOLS]) or {}

            def reader(code: str) -> Any:
                return fetched.get(code)
        except Exception as exc:  # noqa: BLE001 - a probe must not take the page down
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "classes": {}, "score": 0.0, "answered": 0,
                    "required": len(PROBE_SYMBOLS)}

    if in_session is None or session_label is None:
        try:
            state = desk.session_state() if desk else {}
            if in_session is None:
                in_session = bool(state.get("in_session"))
            if session_label is None:
                session_label = state.get("label")
        except Exception:  # noqa: BLE001
            if in_session is None:
                in_session = False
    if session_label is None:
        session_label = "连续竞价" if in_session else "盘前/盘后"

    classes: dict[str, dict] = {}
    for kind, code in PROBE_SYMBOLS:
        try:
            quote = reader(code)
        except Exception as exc:  # noqa: BLE001
            quote = None
            classes[kind] = {"symbol": code, "got": False,
                             "error": f"{type(exc).__name__}: {exc}"}
            continue
        if quote is None:
            classes[kind] = {"symbol": code, "got": False}
            continue
        row = quote.as_dict() if hasattr(quote, "as_dict") else dict(quote)
        raw_age = row.get("age_seconds")
        try:
            price = float(row.get("last"))
        except (TypeError, ValueError, OverflowError):
            price = float("nan")
        try:
            age = float(raw_age)
        except (TypeError, ValueError, OverflowError):
            age = float("nan")
        stamp = pd.to_datetime(row.get("quote_ts"), errors="coerce", utc=True)
        price_ok = bool(math.isfinite(price) and price > 0 and pd.notna(stamp))
        age_usable = bool(math.isfinite(age) and age >= 0)
        # Outside the session a stored close carries NO age on purpose: the
        # quote tool documents that "4 seconds old" would be a lie there. The
        # old check demanded a finite age unconditionally, so after every close
        # the stock channel (THS) was reported dead and the scorecard emitted a
        # false blind spot. Found on 2026-11-09: 600519.SH last=1444.99, a valid
        # quote_ts, age_seconds=None -> "invalid quote". In session the age is
        # still required, because that is the only way to detect a stale feed.
        age_missing_by_design = raw_age is None and not in_session
        valid = bool(price_ok and (age_usable or age_missing_by_design))
        classes[kind] = {
            "symbol": code,
            "got": valid,
            "name": row.get("name"),
            "last": row.get("last"),
            "source": row.get("source"),
            "quote_ts": row.get("quote_ts"),
            "age_seconds": age if math.isfinite(age) else row.get("age_seconds"),
            "age_basis": ("stored_close" if age_missing_by_design
                          else "reported" if age_usable else "missing"),
            "is_realtime": row.get("is_realtime"),
            "stale_in_session": bool(valid and in_session and age_usable
                                     and age > QUOTE_STALE_SECONDS),
        }
        if not valid:
            classes[kind]["error"] = (
                "invalid quote: positive finite price and quote_ts required"
                if not price_ok else
                "invalid quote: age_seconds required while in session")

    answered = sum(1 for c in classes.values() if c.get("got"))
    stale = [k for k, c in classes.items() if c.get("stale_in_session")]
    score = answered / len(PROBE_SYMBOLS)
    if stale:
        # Half credit per stale class: the path answers, but not with a price you
        # could act on. Reporting 100% here is how a closed feed looks healthy.
        score -= 0.5 * len(stale) / len(PROBE_SYMBOLS)

    missing = [k for k, c in classes.items() if not c.get("got")]
    return {
        "ok": answered == len(PROBE_SYMBOLS),
        "in_session": bool(in_session),
        "session_label": session_label,
        "classes": classes,
        "answered": answered,
        "required": len(PROBE_SYMBOLS),
        "missing": missing,
        "score": max(0.0, round(score, 3)),
        "note": ("每个品种走的是不同的报价通道，所以必须按品种探测；"
                 "拿不到不等于没有这只票，可能只是这条通道不支持它"),
    }


def _session_list(store, day: str) -> list[str]:
    """The reference calendar: sessions the daily dataset actually carries.

    Only the tail is read. The full 16M-row history costs seconds and this needs
    the last year of it — the module already knows how deep each dataset is from
    the manifest, so re-reading twenty years to answer "what is the last session"
    would be the expensive way to learn nothing new.
    """
    start = _shift(day, -520)
    daily = store.read("daily", start, day, columns=["trade_date"])
    if daily.empty:
        daily = store.read("daily", None, day, columns=["trade_date"])
    if daily.empty:
        return []
    return sorted(daily["trade_date"].astype(str).unique())


def _shift(yyyymmdd: str, days: int) -> str:
    stamp = datetime.strptime(yyyymmdd, "%Y%m%d") + timedelta(days=days)
    return stamp.strftime("%Y%m%d")


def _manifest_lag(records, day: str) -> int | None:
    """Calendar-day age of the latest ingest, independent of business rows."""
    stamps = [str(getattr(r, "ingested_at", "") or "") for r in records]
    stamps = [s for s in stamps if s]
    if not stamps:
        return None
    try:
        ingest_day = datetime.fromisoformat(max(stamps)).date()
        as_of = datetime.strptime(str(day), "%Y%m%d").date()
    except ValueError:
        return None
    return max(0, (as_of - ingest_day).days)


def _minute_universe_breadth(store, sessions: list[str], day: str,
                             min_history: int) -> dict:
    """Mean per-session symbol breadth for 5-minute bars.

    A partition containing a two-symbol canary is not a full-market session.  The
    ordinary session metric still reports calendar depth; this second axis stops
    that depth from being presented as complete market history while a sweep is
    only part way through.  Missing minute partitions are handled by ``coverage``
    and are not silently added to this average as zero-breadth sessions.
    """
    ratios: list[float] = []
    days = [s for s in sessions if s <= day]
    if min_history:
        days = days[-min_history:]
    for session in days:
        minute = store.read("stk_mins", session, session, columns=["ts_code"])
        if minute.empty or "ts_code" not in minute.columns:
            continue
        daily = store.read("daily", session, session, columns=["ts_code"])
        if daily.empty or "ts_code" not in daily.columns:
            continue
        expected = int(daily["ts_code"].astype(str).nunique())
        actual = int(minute["ts_code"].astype(str).nunique())
        if expected:
            ratios.append(min(1.0, actual / expected))
    if not ratios:
        return {}
    return {
        "universe_coverage": round(sum(ratios) / len(ratios), 4),
        "universe_min": round(min(ratios), 4),
        "universe_days": len(ratios),
        "universe_basis": "各分钟分区唯一 ts_code / 当日 daily 唯一 ts_code 的均值",
    }


def _dataset_state(store, registry, name: str, sessions: list[str], day: str,
                   max_lag: int = 0, min_history: int = 60) -> dict:
    """Presence, capture rate, history depth and staleness for one dataset.

    Two different questions, and conflating them is how a young stream gets
    reported as a hole and an old hole gets reported as complete:

    * **capture** — of the sessions this dataset *could* have covered, how many
      are actually here. A stream that only exists from October and delivered
      every day since is at 100%, not 1%.
    * **depth** — how many sessions of it exist at all. Three days is a complete
      stream and still not a prior.
    """
    spec = get_spec(name)
    records = [r for r in registry.list_manifest(name) if r.status in ("ok", "warn")]
    if not records:
        return {"dataset": name, "present": False, "recent": 0, "coverage": 0.0,
                "depth_factor": 0.0, "lag": None, "cover_end": None,
                "score": 0.0, "basis": "absent"}

    cover_end = max((r.cover_end or "") for r in records)
    cover_start = min((r.cover_start or "") for r in records) or None
    lag = None
    if cover_end and sessions:
        later = [s for s in sessions if s > cover_end]
        lag = len(later)

    window = sessions[-LOOKBACK:] if len(sessions) > LOOKBACK else sessions
    window_start = window[0] if window else None
    if window_start is None:
        return {"dataset": name, "present": True, "recent": 0, "coverage": 0.0,
                "depth_factor": 0.0, "lag": lag, "cover_end": cover_end,
                "score": 0.0, "basis": "no-calendar"}

    col = spec.date_column
    frame = store.read(name, window_start, day, columns=[col])
    recent = int(pd.Series(frame[col]).astype(str).nunique()) if not frame.empty else 0

    if col != "trade_date":
        # Masters and event tables do not owe one business row per session.
        # Freshness is the ingest manifest's age: an old list_date or a quiet
        # announcement week is not a stale collection job.
        lag = _manifest_lag(records, day)
        return {
            "dataset": name,
            "present": True,
            "recent": recent,
            "coverage": 1.0,
            "depth": recent,
            "depth_factor": 1.0,
            "lag": lag,
            "cover_end": cover_end,
            "basis": "manifest",
            "score": 1.0,
            "_grace": max(MASTER_GRACE, max_lag),
        }

    lo = max(window_start, cover_start or window_start)
    # The window ends *today*, not at the dataset's last row. Ending the window at
    # ``cover_end`` makes a hole at the tail invisible: a dataset that stops in
    # July would have its denominator stop in July too, and report 100% of a year
    # it only half covers. Staleness is then reported separately by ``lag``.
    expected = len([s for s in window if s >= lo])

    coverage = min(1.0, recent / expected) if expected else 0.0
    depth_factor = min(1.0, recent / min_history) if min_history else 1.0
    breadth = (_minute_universe_breadth(store, window, day, min_history)
               if name == "stk_mins" else {})
    universe_factor = breadth.get("universe_coverage", 1.0)
    return {
        "dataset": name,
        "present": True,
        "recent": recent,
        "expected": expected,
        "coverage": round(coverage, 3),
        "depth": recent,
        "depth_factor": round(depth_factor, 3),
        "since": cover_start,
        "lag": lag,
        "cover_end": cover_end,
        "basis": "sessions",
        **breadth,
        "score": coverage * depth_factor * universe_factor,
    }


def _quality_state(row: dict | None) -> dict:
    """Separate verified coverage from coverage with known correctness errors.

    Warning partitions remain visible but are not arbitrarily converted into a
    fractional penalty: some feeds legitimately carry a warning on every
    partition. An error issue or a current quarantine is different — those rows
    failed a correctness rule, so their coverage does not count as verified.
    """
    row = row or {}

    def codes(key: str) -> list[str]:
        return sorted({part for part in str(row.get(key) or "").split(",") if part})

    errors = int(row.get("error_issues") or 0)
    warned = int(row.get("warn_partitions") or 0)
    quarantined = int(row.get("quarantine_partitions") or 0)
    failed = errors > 0 or quarantined > 0
    return {
        "dataset": str(row.get("dataset") or ""),
        "factor": 0.0 if failed else 1.0,
        "status": "error" if failed else ("warn" if warned else "ok"),
        "error_issues": errors,
        "error_codes": codes("error_codes"),
        "warn_partitions": warned,
        "warn_codes": codes("warn_codes"),
        "quarantine_partitions": quarantined,
    }


def _freshness(lag: int | None, max_lag: int) -> float:
    if lag is None:
        return 0.0
    if lag <= max_lag:
        return 1.0
    # Five sessions late is worthless; decay rather than a cliff so the score
    # can show direction before it hits zero.
    return max(0.0, 1.0 - (lag - max_lag) / 10.0)


def _pillar_pit(store, day: str) -> dict:
    """Is the point-in-time contract actually enforced, or just documented?"""
    checks: list[dict] = []

    fina = store.read("fina_indicator", None, day)
    if fina.empty:
        checks.append({"check": "财报用 ann_date", "ok": False, "detail": "fina_indicator 为空"})
    else:
        has_ann = "ann_date" in fina.columns
        future = 0
        if has_ann:
            ann = fina["ann_date"].astype(str)
            future = int((ann > day).sum())
        checks.append({
            "check": "财报用 ann_date",
            "ok": bool(has_ann and future == 0),
            "detail": f"ann_date 在列={has_ann}，晚于 {day} 的行={future}",
        })

    basic = store.read("stock_basic")
    has_list = "list_date" in basic.columns if not basic.empty else False
    has_delist = "delist_date" in basic.columns if not basic.empty else False
    checks.append({
        "check": "退市与上市日期可用",
        "ok": bool(has_list and has_delist),
        "detail": f"list_date={has_list} delist_date={has_delist}",
    })

    calendars = store.read("trade_cal")
    ok_cal = not calendars.empty
    checks.append({"check": "交易日历", "ok": ok_cal,
                   "detail": f"{len(calendars)} 行" if ok_cal else "缺失"})

    passed = sum(1 for c in checks if c["ok"])
    return {"checks": checks, "score": passed / len(checks) if checks else 0.0}


def _reconcile(store, day: str) -> list[dict]:
    """Facts more than one source claims, read from all of them on the same day.

    A conflict is not an error to be averaged away: two sources disagreeing about
    how many stocks sealed today means neither number may be quoted as fact.
    """
    out: list[dict] = []

    def count(dataset: str, predicate: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
              session: str = day) -> int | None:
        frame = store.read(dataset, session, session)
        if frame.empty:
            return None
        if predicate is not None:
            frame = predicate(frame)
        return len(frame)

    derived = store.read("limit_pool_t0", day, day)
    # ``DataFrame.get`` returns None for a missing column, and ``None == "x"`` is
    # False, so the naive form indexes the frame with a scalar and raises. A
    # missing column has to mean "no rows", not "crash the whole scorecard".
    if not derived.empty and "pool" in derived.columns:
        derived_up = derived[derived["pool"] == "limit_up"]
    else:
        derived_up = derived.iloc[0:0]

    counts = {
        "ths_limit_up": count("ths_limit_up"),
        "eastmoney_pool": count("limit_up_pool"),
        "derived_t0": None if derived.empty else len(derived_up),
    }
    present = {k: v for k, v in counts.items() if v is not None}
    if len(present) >= 2:
        lo, hi = min(present.values()), max(present.values())
        spread = (hi - lo) / hi if hi else 0.0
        out.append({
            "fact": "当日涨停家数",
            "values": present,
            "spread": round(spread, 3),
            "agree": spread <= 0.10,
            "note": "来源口径不同（ST/北交所/是否含一字）会差几只；差超过 10% 就不是口径问题",
        })

    streaks: dict[str, int | None] = {}
    ths = store.read("ths_limit_up", day, day)
    if not ths.empty and "continue_cnt" in ths.columns:
        streaks["ths_limit_up"] = int(pd.to_numeric(ths["continue_cnt"], errors="coerce").max())
    ladder = store.read("ths_ladder", day, day)
    if not ladder.empty and "board_num" in ladder.columns:
        streaks["ths_ladder"] = int(pd.to_numeric(ladder["board_num"], errors="coerce").max())
    if not derived_up.empty and "boards" in derived_up.columns:
        streaks["derived_t0"] = int(pd.to_numeric(derived_up["boards"], errors="coerce").max())
    streak_vals = {k: v for k, v in streaks.items() if v is not None}
    if len(streak_vals) >= 2:
        lo, hi = min(streak_vals.values()), max(streak_vals.values())
        out.append({
            "fact": "最高连板",
            "values": streak_vals,
            "spread": hi - lo,
            "agree": hi == lo,
            "note": "连板高度必须精确一致；差 1 以上说明有一路没有预热或口径错",
        })

    # 晋级率：desk 现在自己从派生池算（250 天），上游那张表只留 5 天。两版都有的
    # 日子要摆在一起看——差 1 个百分点是口径，差 20 个百分点就有一版是错的。
    try:
        derived = (promotion_series(store, [day, day]) or {}).get(day) or []
        vendor_day = store.read("ths_limit_stat", day, day)
        if derived and vendor_day is not None and not vendor_day.empty:
            mine = {r["level"]: r["promotion_rate"] for r in derived
                    if r.get("promotion_rate") is not None}
            theirs: dict[str, float] = {}
            for _, row in vendor_day.iterrows():
                rate = pd.to_numeric(row.get("promotion_rate"), errors="coerce")
                if pd.notna(rate):
                    theirs[str(row.get("level"))] = float(rate)
            shared = mine.keys() & theirs.keys()
            if shared:
                spread = max(abs(mine[k] - theirs[k]) for k in shared)
                out.append({
                    "fact": "晋级率",
                    "values": {k: {"derived": mine[k], "vendor": theirs[k]} for k in shared},
                    "spread": round(spread, 4),
                    "agree": spread <= 0.02,
                    "note": ("派生池口径是「昨日 k 板 → 今日 k+1 板」。对不上时先看"
                             "「当日涨停家数」那一条：20261026 的 22 个百分点来自同花顺"
                             "漏报 16 只涨停，分子少了 5、分母没变。不取平均"),
                })
    except (ImportError, KeyError, ValueError, FileNotFoundError):
        pass

    return out


def _dimension_components(parts: list[dict], labels: dict) -> list[dict]:
    """Expose source-level recoverability without splitting the dimension weight."""
    out = []
    for part in parts:
        key, label, recoverable, fix, basis = labels.get(
            str(part.get("dataset")), (None, None, None, None, None))
        if key is None:
            continue
        score = float(part.get("score") or 0.0)
        if score >= 0.95:
            status = "ok"
        elif not part.get("present"):
            status = "missing"
        elif part.get("lag") is not None and part["lag"] > part.get("grace", 0):
            status = "stale"
        elif int(part.get("depth") or 0) < 120:
            status = "young"
        else:
            status = "thin"
        out.append({
            "key": key, "label": label, "dataset": part["dataset"],
            "score": round(score, 3),
            "quality_score": round(float(part.get("quality_score") or 0.0), 3),
            "status": status, "min_history": 120,
            "depth": part.get("depth"), "coverage": part.get("coverage"),
            **({"universe_coverage": part["universe_coverage"]}
               if part.get("universe_coverage") is not None else {}),
            "since": part.get("since"), "cover_end": part.get("cover_end"),
            "recoverable": recoverable, "fix": fix, "basis": basis,
        })
    return out


def _intraday_components(parts: list[dict]) -> list[dict]:
    return _dimension_components(parts, {
        "stk_mins": ("minute_history", "分钟历史", True,
                     "python scripts/intraday_sweep.py --days 120", "5-minute bars"),
        "rt_quote": ("tape_replay", "盘口回放", False,
                     "只能从 2026-11-07 起逐交易日积累真实冷存；更早日期不可回补",
                     "raw frame archive"),
    })


def _auction_components(parts: list[dict]) -> list[dict]:
    return _dimension_components(parts, {
        "ths_auction": ("auction_alerts", "竞价告警", True,
                        "python scripts/fetch_ths_special.py --datasets ths_auction "
                        "--days 60 --force", "rolling one-year upstream"),
        "ths_anomaly": ("anomaly_tape", "盘口异动", False,
                        "TODAY_ONLY：只能随真实交易日逐日冷存，不能伪造历史",
                        "same-day anomaly snapshot"),
    })


def _coverage(store, registry, day: str | None = None) -> dict:
    """Everything that changes only when the warehouse changes.

    Split out from the assembly step so the expensive part can be cached on the
    manifest digest while the live-quote probe — which changes every minute,
    without any ingest — is measured on every call.
    """
    sessions = _session_list(store, day or "20991231")
    if day is None:
        day = sessions[-1] if sessions else datetime.now().strftime("%Y%m%d")
        sessions = [s for s in sessions if s <= day]
    if not sessions:
        return {"ok": False, "error": "daily 为空，无法建立参考交易日历"}

    summary = list(registry.summary())
    quality_by_dataset = {str(row.get("dataset")): row for row in summary}
    rows: list[dict] = []
    blind: list[str] = []
    quality_datasets: list[dict] = []
    weighted = 0.0
    quality_weighted = 0.0

    for req in REQUIREMENTS:
        if req.key == "quotes":
            continue

        if req.key == "point_in_time":
            pit = _pillar_pit(store, day)
            weighted += req.weight * pit["score"]
            quality_weighted += req.weight * pit["score"]
            rows.append({
                "key": req.key, "label": req.label, "question": req.question,
                "score": round(pit["score"], 3), "quality_score": round(pit["score"], 3),
                "weight": req.weight, "status": "ok" if pit["score"] == 1 else "partial",
                "quality_status": "ok" if pit["score"] == 1 else "error",
                "checks": pit["checks"], "datasets": [],
            })
            if pit["score"] < 1:
                blind.append("部分时点校验未通过：" + "；".join(
                    c["check"] for c in pit["checks"] if not c["ok"]))
            continue

        parts = []
        for name in req.datasets:
            state_sessions = sessions
            if req.key == "intraday_series" and name == "rt_quote":
                state_sessions = [s for s in sessions if s >= TAPE_ARCHIVE_START]
            state = _dataset_state(store, registry, name, state_sessions, day,
                                   max_lag=req.max_lag, min_history=req.min_history)
            if req.key == "intraday_series" and name == "rt_quote":
                state["since"] = max(str(state.get("since") or ""), TAPE_ARCHIVE_START)
                state["archive_start"] = TAPE_ARCHIVE_START
            grace = state.pop("_grace", req.max_lag)
            state["grace"] = grace
            state["score"] = round(state["score"] * _freshness(state["lag"], grace), 3)
            quality = _quality_state(quality_by_dataset.get(name))
            quality["dataset"] = name
            # Full issue details live once in the top-level quality table. Nesting
            # the same codes and counts in every dataset row pushed the response
            # into a shrink tier that discarded half of the twelve dimensions.
            state["quality_status"] = quality["status"]
            state["quality_score"] = round(state["score"] * quality["factor"], 3)
            if quality["status"] != "ok":
                quality_datasets.append(quality)
            parts.append(state)

        dim = min((p["score"] for p in parts), default=0.0)
        quality_dim = min((p["quality_score"] for p in parts), default=0.0)
        weighted += req.weight * dim
        quality_weighted += req.weight * quality_dim

        worst = min(parts, key=lambda p: p["score"]) if parts else None
        quality_worst = min(parts, key=lambda p: p["quality_score"]) if parts else None
        if dim >= 0.95:
            status = "ok"
        elif not any(p["present"] for p in parts):
            status = "missing"
        elif any(p["lag"] is not None and p["lag"] > p["grace"] for p in parts):
            # The job that fills it stopped: fix by running it, not by backfilling.
            status = "stale"
        elif any(p.get("depth", 0) < req.min_history for p in parts):
            # Everything available has been captured; there is just not enough
            # of it yet to act as a prior. Only time fixes this.
            status = "young"
        else:
            status = "thin"

        if any(p["quality_status"] == "error" for p in parts):
            quality_status = "error"
        elif any(p["quality_status"] == "warn" for p in parts):
            quality_status = "warn"
        else:
            quality_status = "ok"
        entry = {
            "key": req.key, "label": req.label, "question": req.question,
            "score": round(dim, 3), "quality_score": round(quality_dim, 3),
            "weight": req.weight, "status": status, "quality_status": quality_status,
            "datasets": parts,
            "worst": None if worst is None else worst["dataset"],
            "quality_worst": None if quality_worst is None else quality_worst["dataset"],
        }
        if req.key == "intraday_series":
            entry["components"] = _intraday_components(parts)
        elif req.key == "auction":
            entry["components"] = _auction_components(parts)
        rows.append(entry)
        if status != "ok" and req.blind:
            blind.append(req.blind)

    declared = set(SPECS)
    held = {d["dataset"] for d in summary}
    return {
        "ok": True,
        "as_of": day,
        "sessions": len(sessions),
        "rows": rows,
        "blind": blind,
        "weighted": weighted,
        "quality_weighted": quality_weighted,
        "quality_datasets": quality_datasets,
        "conflicts": _reconcile(store, day),
        "declared_not_held": sorted(declared - held),
    }


def _quote_row(req: Requirement, probe: dict, blind: list[str]) -> dict:
    dim = float(probe.get("score") or 0.0)
    if dim >= 0.95:
        status = "ok"
    elif dim <= 0.01:
        status = "missing"
    else:
        status = "partial"

    gone = probe.get("missing") or []
    session_label = probe.get("session_label") or (
        "连续竞价" if probe.get("in_session") else "盘前/盘后")
    labels = {"stock": "股票", "etf": "ETF",
              "cb_sh": "沪市可转债", "cb_sz": "深市可转债"}
    if gone and probe.get("in_session"):
        blind.append("盘中实时报价取不到：" + "、".join(labels.get(g, g) for g in gone)
                     + "——这些品种此刻不能按「当前价」下单")
    elif gone:
        if session_label == "午休":
            blind.append("午休以下通道连上午最后静态价都没取到："
                         + "、".join(labels.get(g, g) for g in gone)
                         + "——午休不产新帧是时序，通道完全没回答才是故障")
        else:
            blind.append("盘后以下通道连收盘价都没取到："
                         + "、".join(labels.get(g, g) for g in gone)
                         + "——不是「实时价缺失」，是这条通道本身没回答")
    if not probe.get("in_session"):
        if session_label == "午休":
            blind.append("午休没有连续竞价新报价：现有报价是上午最后静态价，"
                         "报价年龄增长是正常停帧，不是数据源故障，也不能说「现在」")
        else:
            blind.append("盘后没有实时价：报价是当日收盘静态价（is_realtime=false），"
                         "不能说「现在」；quotes 维度得分只说明通道可达，不说明有实时价")
    if any(c.get("stale_in_session") for c in (probe.get("classes") or {}).values()):
        blind.append("盘中报价超过 5 分钟未更新，不能按「当前价」下单")

    return {
        "key": req.key, "label": req.label, "question": req.question,
        "score": round(dim, 3), "weight": req.weight, "status": status,
        "session_label": session_label,
        "classes": probe.get("classes", {}),
        "missing": gone,
        "datasets": [],
    }


def scorecard(store, registry, day: str | None = None, *,
              quotes: dict | None = None, desk: Any | None = None) -> dict:
    """The full picture: per-dimension score, conflicts, and what you cannot say.

    ``quotes`` is the result of ``probe_quotes``. It is passed in rather than
    called here so the whole scorecard — including the live-quote dimension —
    can be exercised in a test without a running desk or a network. ``desk`` is
    the fallback source for that probe when the caller did not run one itself.
    """
    base = _coverage(store, registry, day)
    if not base.get("ok"):
        return base
    return _assemble(base, quotes if quotes is not None else probe_quotes(desk=desk),
                     _fundamental_sla(store, base["as_of"]),
                     _text_coverage(store, base["as_of"]))


# 这里原来有一个 `_grade()`，把分数翻成四句结论：可下判断 / 可读大势，个股要谨慎 /
# 只能读价格与结构 / 不足以支持交易判断。
#
# 它删掉的理由不是"分档不合理"，而是**它回答的问题不归它回答**。同一个 72 分，
# 想做指数的人够了，想判断一只票的分时承接不够——"够不够"取决于你要说什么，
# 而那是读者正在做的事。分数本身是事实（最弱一环的覆盖率加权），`dimensions`
# 里逐项写着哪个维度低，`blind_spots` 写着今天不能下的断言。三样加起来，
# 读者能自己决定能说什么；再加上一个 grade，只是把这个决定预先做掉。


def _schema_digest(source: bytes | None = None) -> str:
    """A fingerprint of the scoring rules and their implementation.

    ``_digest`` identifies the *data*; this identifies the *question*. Without it,
    editing REQUIREMENTS (adding a dataset, changing a weight, redefining what a
    dimension covers) leaves the cache key untouched and the old verdict is served
    for as long as the warehouse does not change — a rule change nobody can see.
    The module source is included as well: a lunch/after-close wording or probe
    semantic fix must not be hidden behind the previous implementation's cache.
    """
    payload = json.dumps(
        [(r.key, r.label, r.datasets, r.weight, r.max_lag, r.min_history)
         for r in REQUIREMENTS],
        sort_keys=True, default=str)
    implementation = Path(__file__).read_bytes() if source is None else source
    return hashlib.sha256(payload.encode() + b"\0" + implementation).hexdigest()[:8]


def _digest(registry) -> str:
    # Row counts and cover_end do not identify content. Include every manifest
    # checksum/schema/snapshot so replacing a partition in place invalidates the
    # cached scorecard instead of preserving the old quality verdict.
    records = []
    if hasattr(registry, "list_manifest"):
        for row in registry.list_manifest():
            records.append({key: getattr(row, key, None) for key in (
                "dataset", "partition", "rows", "cover_start", "cover_end", "status",
                "checksum", "schema_hash", "snapshot_id", "as_of", "ingested_at")})
    summary = list(registry.summary()) if hasattr(registry, "summary") else []
    if not records:
        records = summary
    # Quality reports can change while a partition remains `warn` with the same
    # row count and checksum. Exclude them here and a cached 1.0 survives a newly
    # detected correctness error — precisely the stale verdict this score audits.
    quality = [{key: row.get(key) for key in (
        "dataset", "warn_partitions", "quarantine_partitions", "error_issues",
        "error_codes", "warn_codes")} for row in summary]
    payload = json.dumps({"manifest": records, "quality": quality},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def cached_scorecard(store, registry, day: str | None = None, *,
                     root: Path | None = None, force: bool = False,
                     quotes: dict | None = None, desk: Any | None = None) -> dict:
    """``scorecard`` with the warehouse half cached on the manifest digest.

    Two lifetimes, on purpose. Coverage only changes when data is ingested, so it
    is cached against the manifest and never against a clock — a TTL there would
    reintroduce exactly the staleness the module measures. The live-quote probe
    changes without any ingest at all, so it is *not* cached: reporting a quote
    path as healthy because it was healthy at cache-fill time is the failure this
    dimension exists to catch.
    """
    path = (root or Path(".")) / CACHE
    stamp = f"{_digest(registry)}:{_schema_digest()}"
    key = f"{day or 'latest'}:{stamp}"

    base = None
    if not force and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("_key") == key or key in (cached.get("_aliases") or []):
                base = cached["payload"]
        except (OSError, ValueError, json.JSONDecodeError):
            base = None

    if base is None:
        base = _coverage(store, registry, day)
        if not base.get("ok"):
            return base
        # A caller asking for "latest" and a caller asking for the date that
        # happens to be latest are the same question. Writing only the key they
        # used made the two alternate into a permanent cache miss: the tool took
        # 45s on every other call while producing identical bytes. Alias both.
        alias = f"{base['as_of']}:{stamp}"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(
                path,
                {"_key": key, "_aliases": sorted({key, alias}), "payload": base})
        except OSError:
            pass

    return _assemble(base, quotes if quotes is not None else probe_quotes(desk=desk),
                     _fundamental_sla(store, base["as_of"]),
                     _text_coverage(store, base["as_of"]))


def _fundamental_sla(store, day: str) -> dict:
    """Has the newest disclosed reporting period actually been ingested?

    This is deliberately NOT another staleness discount on ``score``. Quarterly
    data legitimately goes ~90 days without a new report, so a trading-day lag
    (``fundamentals.max_lag=250``) reports 1.0 even when the market published a
    quarter and our ingest missed it. What distinguishes those two states is
    *coverage*: how many listed names have a row for the newest stored period.
    A single stray 20260930 row among 36560 is a sync gap, not a fresh table.

    Purely observational: no score, no weight, no permission is changed, and a
    concurrent rewrite of the partition degrades to ``available=False`` instead
    of breaking the endpoint.
    """
    unavailable = {"available": False, "reason": None}
    try:
        frame = store.read("fina_indicator")
    except Exception as exc:  # noqa: BLE001 - a concurrent sweep must not 500 the endpoint
        return {**unavailable, "reason": f"fina_indicator 不可读：{type(exc).__name__}"}
    if frame is None or frame.empty:
        return {**unavailable, "reason": "fina_indicator 为空"}
    try:
        end = frame["end_date"].astype(str)
        ann = frame["ann_date"].astype(str)
        latest_period = str(end.max())
        latest_ann = str(ann[end == latest_period].max())
        covered = int(frame.loc[end == latest_period, "ts_code"].nunique())
        universe = 0
        try:
            basic = store.read("stock_basic")
            if basic is not None and not basic.empty:
                live = (basic[basic["list_status"].astype(str) == "L"]
                        if "list_status" in basic.columns else basic)
                universe = int(live["ts_code"].nunique())
        except Exception:  # noqa: BLE001 - universe is a denominator, not a fact to guess
            universe = 0
        coverage = round(covered / universe, 4) if universe else None
        try:
            stale_days = int((pd.Timestamp(str(day)) - pd.Timestamp(latest_ann)).days)
        except (ValueError, TypeError):
            stale_days = None
        breached = bool(
            (coverage is not None and coverage < FUNDAMENTAL_MIN_COVERAGE)
            or (stale_days is not None and stale_days > FUNDAMENTAL_MAX_STALE_DAYS))
        return {
            "available": True,
            "latest_period": latest_period,
            "latest_announced_on": latest_ann,
            "days_since_announcement": stale_days,
            "universe": universe,
            "symbols_with_latest_period": covered,
            "coverage": coverage,
            "min_coverage": FUNDAMENTAL_MIN_COVERAGE,
            "max_stale_days": FUNDAMENTAL_MAX_STALE_DAYS,
            "sla_breached": breached,
            "note": ("覆盖度衡量同步是否追平，不是距今天数：季度数据约 90 天不出新报告"
                     "属正常。breached 表示本机只入库了少数最新报告期，"
                     "补齐命令 python scripts/quarterly_fundamentals.py --since <公告日>。"),
        }
    except Exception as exc:  # noqa: BLE001
        return {**unavailable, "reason": f"{type(exc).__name__}: {exc}"}


def _text_coverage(store, day: str, window_days: int = 30) -> dict:
    """How much of the recent announcement metadata actually has downloaded text.

    ``quant_filing`` can page through a filing's PDF text, but only for ids that
    were fetched into the cache. A profile that shows five announcements with
    zero excerpts is not a broken tool — it is an unmeasured coverage gap. This
    makes the gap countable instead of silently looking like "no news".
    """
    try:
        anns = store.read("anns")
        root = store.root
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    if anns is None or anns.empty:
        return {"available": False, "reason": "anns 为空"}
    try:
        from service.adapters.anns_text import cache_dir

        pub = anns["pub_date"].astype(str)
        cutoff = (pd.Timestamp(str(day)) - pd.Timedelta(days=window_days)).strftime("%Y%m%d")
        recent = anns[(pub >= cutoff) & (pub <= str(day))]
        ids = recent["announcement_id"].astype(str).unique()
        if not len(ids):
            return {"available": True, "window_days": window_days,
                    "announcements": 0, "with_text": 0, "coverage": None,
                    "note": "窗口内没有公告，覆盖率不适用（不是 0）"}
        # One directory listing, not one resolve() per id: 23k path resolutions
        # made this endpoint take ~17s. Membership in a set is microseconds.
        folder = cache_dir(root)
        cached_ids = ({p.stem for p in folder.glob("*.json")}
                      if folder.is_dir() else set())
        with_text = sum(1 for ann_id in ids if str(ann_id) in cached_ids)
        return {
            "available": True,
            "window_days": window_days,
            "announcements": len(ids),
            "with_text": with_text,
            "coverage": round(with_text / len(ids), 4),
            "note": ("只统计缓存里已有正文的公告；缺正文时 quant_filing 仍可按需抓取，"
                     "所以覆盖率低不是'没有公告'。补齐：python scripts/fetch_anns_text.py"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def _assemble(base: dict, probe: dict, sla: dict | None = None,
              text_coverage: dict | None = None) -> dict:
    """Finish coverage and quality-adjusted coverage with the live-quote dimension."""
    req = next(r for r in REQUIREMENTS if r.key == "quotes")
    rows = list(base["rows"])
    for item in rows:
        # Keep compatibility with cached/mock payloads produced before the
        # quality dimension existed. The legacy coverage arithmetic is untouched.
        item.setdefault("quality_score", item["score"])
        item.setdefault("quality_status", "ok")
    blind = list(base["blind"])
    row = _quote_row(req, probe, blind)
    row["quality_score"] = row["score"]
    row["quality_status"] = "ok"
    order = [r.key for r in REQUIREMENTS]
    rows.append(row)
    rows.sort(key=lambda r: order.index(r["key"]))

    weighted = base["weighted"] + req.weight * float(row["score"])
    total = round(100 * weighted / TOTAL_WEIGHT, 1)
    quality_weighted = (float(base.get("quality_weighted", base["weighted"]))
                        + req.weight * float(row["quality_score"]))
    quality_total = round(100 * quality_weighted / TOTAL_WEIGHT, 1)

    by_key = {r.key: r for r in REQUIREMENTS}
    ranked = sorted(rows, key=lambda r: (float(r["score"]), r["key"]))
    quality_ranked = sorted(rows, key=lambda r: (float(r["quality_score"]), r["key"]))
    weak = [{
        "key": r["key"],
        "label": r["label"],
        "score": r["score"],
        "quality_score": r["quality_score"],
        "status": r["status"],
        "quality_status": r["quality_status"],
        "question": r["question"],
        "blind": by_key[r["key"]].blind or None,
        "fix": by_key[r["key"]].fix or None,
        **({"components": r["components"]} if r.get("components") else {}),
    } for r in ranked if float(r["score"]) < WEAK_DIMENSION_CEILING]

    quality_datasets = sorted(base.get("quality_datasets", []),
                              key=lambda item: item["dataset"])
    disagreements = [item for item in base["conflicts"] if not item.get("agree")]
    has_errors = (any(item["error_issues"] or item["quarantine_partitions"]
                      for item in quality_datasets)
                  or any(row["quality_status"] == "error" for row in rows))
    has_warnings = (any(item["warn_partitions"] for item in quality_datasets)
                    or bool(disagreements))
    quality = {
        "all_clear": not has_errors and not has_warnings,
        "has_errors": has_errors,
        "has_warnings": has_warnings,
        "error_issues": sum(item["error_issues"] for item in quality_datasets),
        "warn_partitions": sum(item["warn_partitions"] for item in quality_datasets),
        "quarantine_partitions": sum(item["quarantine_partitions"]
                                     for item in quality_datasets),
        "conflicts": len(disagreements),
        "datasets": quality_datasets,
    }

    return {
        "ok": True,
        "as_of": base["as_of"],
        "sessions": base["sessions"],
        "score": total,
        "quality_adjusted_score": quality_total,
        "quality_penalty": round(total - quality_total, 1),
        "quality": quality,
        # 没有 grade。分数是覆盖率，不是许可；能不能下某个判断由读者按
        # dimensions 与 blind_spots 自己定。
        "dimensions": rows,
        # Pure drill-down: the weighted mean cannot show that one dimension is
        # near zero. Listing the weak ones changes no arithmetic and grants no
        # permission — it only stops "89" from being read as "all 89".
        "weak_dimensions": weak,
        "weak_dimensions_ceiling": WEAK_DIMENSION_CEILING,
        "score_floor": ranked[0]["score"] if ranked else None,
        "score_floor_key": ranked[0]["key"] if ranked else None,
        "quality_score_floor": quality_ranked[0]["quality_score"] if quality_ranked else None,
        "quality_score_floor_key": quality_ranked[0]["key"] if quality_ranked else None,
        # Several dimensions can tie at the floor. Naming only one hides the
        # others — the same masking this drill-down exists to prevent.
        "score_floor_keys": ([r["key"] for r in ranked
                              if r["score"] == ranked[0]["score"]]
                             if ranked else []),
        "quotes": probe,
        # Per-dataset semantic SLA: a 1.0 coverage score cannot tell
        # "no new report is due" from "the report was published but not ingested".
        "fundamental_sla": sla or {"available": False, "reason": "未提供"},
        "announcement_text_coverage": text_coverage or {"available": False,
                                                       "reason": "未提供"},
        "conflicts": base["conflicts"],
        "blind_spots": blind,
        "declared_not_held": base["declared_not_held"],
        "rule": ("score 是最近 250 个交易日里、每个维度最弱一环的覆盖率加权；"
                 "不是数据量。quality_adjusted_score 保留同一口径，但把含 error issue "
                 "或当前 quarantine 的数据集可信覆盖计为 0；warning 只显式披露、不作任意比例扣分。"
                 "stk_mins 的 universe_coverage 是各日实际股票数 / daily 股票数的均值，"
                 "防止少量试写分区被当成全市场历史。"
                 "它不是收益分或许可开关，也不代表其余数据已被证明正确。"
                 "加权均值会被高分维度抬起来，所以必须同时读 "
                 "score_floor（最低那一维）与 weak_dimensions（低于阈值的维度及其"
                 "不能说的断言与补齐命令）：一个 89 分完全可能同时带着一个 0.061。"
                 "weak_dimensions_ceiling 只决定「列出哪些」，不改任何分数、权重或许可。"
                 "conflicts 非空时，那个事实不能作为依据引用；"
                 "blind_spots 里的能力不许假装具备——缺数据时正确的回答是「不知道」。"
                 "分数不构成「允许/禁止交易」的开关，它说的是你能引用什么。"),
    }
