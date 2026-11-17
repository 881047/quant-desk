"""Data health surface for the trading agent.

The agent must be able to answer "is the data I am about to trade on fresh and
trustworthy?" with a tool call, not with a guess. These endpoints expose the
registry, the quarantine state and the snapshot fingerprint used in decisions.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from service import paths
from service.adapters.brief import build_brief
from service.adapters.catalog import SPECS, coverage_boundary, dataset_kind
from service.adapters.lineage import collect as collect_lineage
from service.adapters.parquet_store import DatasetStore
from service.adapters.pipeline_state import status as pipeline_status
from service.adapters.pit import TradingCalendarStore
from service.adapters.sqlite_store import Registry

from .params import day_param
from .security import require_token

# One validator for every router; see `service/api/params.py`.
_day = day_param


router = APIRouter(prefix="/data", tags=["data"], dependencies=[Depends(require_token)])

_registry: Registry | None = None
_store: DatasetStore | None = None
_data_init_lock = threading.RLock()


def _data_root() -> Path:
    # 唯一解析处：``service/paths.py``。在这里再写一遍 ``os.getenv`` 就是第二个
    # 答案，而两个答案只在环境变量没设时相等——生产环境恰好没设，所以没人发现。
    return paths.data_dir()


def get_registry() -> Registry:
    """Build the registry on first use, after the environment is configured."""
    global _registry
    if _registry is None:
        with _data_init_lock:
            if _registry is None:
                override = os.getenv("QUANT_REGISTRY_DB")
                _registry = Registry(
                    Path(override) if override else _data_root() / "registry.sqlite")
    return _registry


def get_store() -> DatasetStore:
    """Build the dataset store lazily; importing the router must not create files."""
    global _store
    if _store is None:
        with _data_init_lock:
            if _store is None:
                _store = DatasetStore(_data_root(), get_registry())
    return _store


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d")


def _latest_rt(day: str | None = None) -> tuple[int, str | None, float | None]:
    """(rows, latest_snapshot_ts, age_seconds) for one day's quote partition.

    Age is measured from ``quote_ts`` — the vendor's own tick time — and only
    falls back to ``snapshot_ts`` when the batch carries none. ``snapshot_ts``
    is the moment *we* wrote the row, and using it made every batch look fresh:
    the 2026-10-30 14:45 snapshot holds rows whose ``quote_ts`` is 08:00 (halted
    names served at the previous close) stamped ``snapshot_ts=14:45:00``, so a
    six-hour-old price reported an age of zero and ``/data/brief`` handed a live
    trading pass to it. ``quality.check_quotes`` already used ``quote_ts``; the
    same pipeline should not hold two definitions of "fresh".

    For a historical day, age is measured against that day's 15:05 close, not
    against now — otherwise every audit of "could we have traded on the 23rd?"
    would incorrectly report stale quotes.
    """
    target = day or _today()
    part = get_store().read_partition("rt_quote", target)
    if part.empty or "snapshot_ts" not in part.columns:
        return 0, None, None
    latest = str(part["snapshot_ts"].max())
    now = datetime.now().astimezone()
    stamp = None
    if "quote_ts" in part.columns:
        raw = part["quote_ts"].max()
        try:
            epoch = float(raw)
        except (TypeError, ValueError):
            epoch = 0.0
        if math.isfinite(epoch) and epoch > 0:
            try:
                stamp = datetime.fromtimestamp(epoch, tz=now.tzinfo)
            except (ValueError, OSError, OverflowError):
                # A millisecond epoch (1.7e12) or vendor garbage raises here, and
                # it used to take `/data/brief` down with it.
                stamp = None
    if stamp is None:
        try:
            stamp = datetime.fromisoformat(latest)
        except ValueError:
            return len(part), latest, None
    try:
        if target != now.strftime("%Y%m%d"):
            close = datetime.strptime(f"{target}150500", "%Y%m%d%H%M%S").replace(tzinfo=now.tzinfo)
            age = max(0.0, (close - stamp).total_seconds())
        else:
            age = (now - stamp).total_seconds()
    except ValueError:
        age = None
    return len(part), latest, age


def _sessions_between(start: str, end: str) -> int:
    """Trading sessions in ``(start, end]``; -1 when the calendar cannot answer.

    -1 rather than 0 on purpose: "the calendar is missing" and "no session has
    elapsed" are different, and a reader that shows ``sessions_behind: 0`` for
    both would make a week-old snapshot look current.
    """
    if not start or not end or start >= end:
        return 0
    try:
        cal = get_store().read("trade_cal", start, end,
                               columns=["cal_date", "is_open"],
                               filters=[("is_open", "==", 1)])
    except (KeyError, ValueError, OSError):
        return -1
    if cal is None or cal.empty:
        return -1
    return sum(1 for day in cal["cal_date"].astype(str) if start < day <= end)


def _sentiment_snapshot(day: str | None = None) -> dict | None:
    """The freshest sentiment block plus the date it is actually for.

    This used to be ``_sentiment``, which accepted today's file and nothing else.
    On a Saturday, before the evening job, or after a missed snapshot run it
    therefore returned ``None`` — and ``/context`` reported ``stage: unknown``
    while the previous session's complete block sat on disk. That block is the
    only sentiment the model can see; going blank throws away the one input, and
    pretending it is today's would be worse.

    The file walk itself lives in ``trading.sentiment.read_snapshot``: this module
    and ``context_pack`` each had a copy, and only one of them had the fallback.
    """
    from service.trading.sentiment import read_snapshot

    target = day or _today()
    payload, trade_date = read_snapshot(_data_root() / "sentiment", target)
    if payload is None:
        return None
    behind = 0 if trade_date == target else _sessions_between(trade_date, target)
    out = dict(payload["sentiment"])
    out["trade_date"] = trade_date
    out["as_of"] = payload.get("as_of")
    out["sessions_behind"] = behind
    if behind:
        out["note"] = (f"这是 {trade_date} 的快照，距 {target} 已过 {behind} 个交易日"
                       if behind > 0 else
                       f"这是 {trade_date} 的快照，日历读不到，落后几个交易日未知")
    # 文件里的 `quality` 是**实时报价**的判定，不是这段情绪的判定：池的家数来自
    # 厂商池，跟这一帧 tick 无关。所以它不往这里放——报价质量有自己的出口
    # （/data/brief、/readiness、quality_report），挂在情绪块旁边只会让
    # "quarantine" 被读成"情绪被判为隔离"。两个写入者的口径也不同：盘中快照判
    # 自己那一帧，日更判合并后的分区。
    return out


@router.get("/health")
def health_alias() -> dict:
    """`/data/status` 的别名。

    文档和 runbook 让人先跑 `GET /data/health` 做数据自查，但真实路径一直
    只有 `/data/status`——2026-11-03 按文档自查时拿到的是 404，于是把
    「路径写错」误读成「这个能力不存在」。两条路径都通，免得下次再猜。
    """
    return status()


@router.get("/status")
def status() -> dict:
    summary = get_registry().summary()
    # 以前 `datasets` 里只有一个裸 `cover_end`，日历类数据集的 2027-12-31 会被
    # 任何按覆盖日倒序的通用排序顶到第一，看起来像"抓到了未来数据"。分类只在
    # catalog 里定义一次；`coverage_boundary` 是给"要不要拿 cover_end 判滞后"用的
    # 直接答案，消费方不必再背一遍 kind 的含义。
    datasets = [
        dict(row,
             kind=dataset_kind(row["dataset"]),
             coverage_boundary=coverage_boundary(row["dataset"]))
        for row in summary
    ]
    known = {d["dataset"] for d in summary}
    missing = [name for name in SPECS if name in
               ("trade_cal", "stock_basic", "daily", "adj_factor", "stk_limit") and name not in known]

    rt_summary = next((d for d in summary if d["dataset"] == "rt_quote"), {})
    rt_rows = int(rt_summary.get("rows") or 0)
    latest_rt = None
    if rt_rows:
        latest_day = rt_summary.get("cover_end")
        rt = get_store().read("rt_quote", latest_day, latest_day, columns=["snapshot_ts"])
        if not rt.empty and "snapshot_ts" in rt.columns:
            latest_rt = str(rt["snapshot_ts"].max())
    age = None
    if latest_rt:
        try:
            age = (datetime.now().astimezone() - datetime.fromisoformat(latest_rt)).total_seconds()
        except ValueError:
            age = None

    backfill = None
    path = _data_root() / "backfill_last_run.json"
    try:
        backfill = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        backfill = None

    return {
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "datasets": datasets,
        "dataset_kinds": {
            "calendar": "日程/日历：cover_end 排到未来是业务语义，不是未来数据",
            "reference": "参考表：cover_end 是最新实体自己的日期，滞后它不说明缺数据",
            "snapshot": "快照：新鲜度由 rt_quote.age_seconds 判定，不看 cover_end",
            "on_demand": "按需抓取：没有按日抓取的义务，覆盖落后不是告警",
            "data": "逐日面板：cover_end 是覆盖边界，落后一个交易日就是缺一天",
        },
        "dataset_kind_note": (
            "按 cover_end 排序或判新鲜度前先看 kind / coverage_boundary；"
            "只有 coverage_boundary=true 的数据集适合按覆盖日比较。值域见 dataset_kinds。"
        ),
        "missing_required": missing,
        "rt_quote": {"rows": rt_rows, "latest_snapshot": latest_rt,
                     "age_seconds": None if age is None else round(age, 1),
                     "stale": age is not None and age > 120,
                     # age 为 null 时 `stale: false` 会被读成"很新"，而真实含义是
                     # "今天一行实时快照都没有"——价格全部来自日线。
                     **({} if age is not None else {"note":
                        "今天没有实时快照行：快照任务没跑过（或已过 15:05 归档），"
                        "此刻的价格全部来自日线"})},
        "derived_pools": _derived_health(summary),
        "quarantined": [d["dataset"] for d in summary if d["quarantine_partitions"]],
        "last_backfill": None if backfill is None else {
            "ran_at": backfill.get("ran_at"),
            "range": backfill.get("range"),
            "failures": backfill.get("failures"),
        },
    }


def _derived_health(summary: list[dict]) -> dict:
    """派生池建了没有、建得对不对。

    家数、连板、晋级率现在都读它。它没建或没预热时不会报错——只会安静地退回厂商口径
    （同花顺日均漏 1.6 只），或者把连板高度算成全场首板。这种"数字照样出来但含义变了"
    的事，只能靠在状态页上显式写一行来防。口径见 ``tape.pool_health``。
    """
    from service.trading import tape

    daily = next((d for d in summary if d["dataset"] == "daily"), {})
    day = daily.get("cover_end")
    if not day:
        return {"status": "unknown", "reason": "daily 没有覆盖区间，无法确定要检查哪一天"}
    try:
        return tape.pool_health(get_store(), str(day))
    except Exception as exc:  # noqa: BLE001 - a status page must still render
        return {"status": "error", "day": str(day),
                "reason": f"{type(exc).__name__}: {exc}"}


@router.get("/brief")
def brief(day: str | None = None) -> dict:
    """One-call answer to 'may I trade on this data today?'."""
    target = _day(day) or _today()
    store = get_store()
    cal = store.read("trade_cal", None, target, columns=["cal_date"],
                     filters=[("is_open", "==", 1)])
    if cal.empty:
        # A day before the first stored session still needs the weekday fallback.
        cal = store.read("trade_cal", columns=["cal_date"],
                         filters=[("is_open", "==", 1)])
    calendar = TradingCalendarStore.from_frame(cal)
    _, _, age = _latest_rt(target)
    # One read of the manifest for the whole answer. It used to be two —
    # `build_brief` and `_derived_health` each asked the registry — which cost
    # ~5 ms twice (the query hits `dataset_manifest` and `quality_report` with a
    # correlated subquery per dataset) and, worse, let a 17:30 refresh land
    # between them: the brief would say `daily` covers 20261105 while
    # `derived_pools` was computed against 20261104's `cover_end`. Same response,
    # two different days, no way for the reader to tell.
    summary = get_registry().summary()
    payload = build_brief(
        summary=summary,
        calendar=calendar,
        today=target,
        rt_age_seconds=age,
        # 回退到最近一份快照，但把它的日期一起给出来：报空会丢掉唯一能看到的
        # 情绪，报成"今天"则是撒谎。`sessions_behind` 是两者之间的交易日数。
        sentiment=_sentiment_snapshot(target),
    )
    # 派生池的状态单独附在 brief 上：`core`/`aggressive` 只回答"覆盖到没有"，
    # 而这个块回答"覆盖到的那个池子建得对不对"。它没建时家数会安静地退回厂商口径，
    # 一个"可以交易"的回答里应该带着这件事。
    payload["derived_pools"] = _derived_health(summary)
    # 最后一块回答的是另一个问题：不是"数据覆盖到没有"，而是"17:30 那次跑完了没有"。
    # 两者会分叉——日线先落库、池子和情绪后落库，崩在中间时每个数据集的 cover_end
    # 都可能是对的，而当天的结构面板还停在昨天。
    # 只在对当天问时给：历史某天要的不是"今晚能不能用"，而且标记只留三周，翻旧账
    # 会把"跑过但已过期"读成"那天没跑"。
    today = _today()
    if target == today:
        payload["pipeline"] = pipeline_status(
            _data_root(), today=today,
            trading_day=calendar.is_trading_day(today))
    return payload


@router.get("/quality")
def quality() -> dict:
    items = get_registry().latest_quality()
    counts = {"ok": 0, "warn": 0, "quarantine": 0}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
            "counts": counts, "items": items}


@router.get("/fingerprint")
def fingerprint() -> dict:
    return {"as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
            "snapshots": get_store().fingerprint()}


@router.get("/information")
def information(day: str | None = None, force: bool = False) -> dict:
    """How much of the market this desk can actually see, with the holes named.

    Not a row count. Each dimension is scored on sessions actually present in the
    last trading year, discounted by staleness and by how young the stream is,
    and facts that more than one source claims are compared on the spot. The
    ``blind_spots`` list is the part a model has to obey: it says which claims
    the data cannot support today.
    """
    from service.research.information import cached_scorecard
    from service.trading import get_desk

    # The scorecard's live-quote dimension is a *capability probe*, not a data
    # read: it asks the same path the quote tool asks. The desk is handed in
    # here so `research` never imports `trading` — see the note on
    # `information.probe_quotes`.
    return cached_scorecard(get_store(), get_registry(), _day(day),
                            root=_data_root().parent, force=force,
                            desk=get_desk())


@router.get("/lineage/{as_of}")
def lineage(as_of: str) -> dict:
    """Exact data versions that would serve a decision made on ``as_of``."""
    return collect_lineage(get_store(), _day(as_of, "as_of")).as_dict()


@router.get("/calendar/{day}")
def calendar_day(day: str) -> dict:
    day = _day(day)
    cal = TradingCalendarStore.from_frame(get_store().read("trade_cal"))
    return {
        "day": day,
        "is_trading_day": cal.is_trading_day(day),
        "prev": cal.prev(day),
        "next": cal.next(day),
        "last_5": cal.last_n(day, 5),
    }


@router.get("/manifest/{dataset}")
def manifest(dataset: str) -> dict:
    recs = get_store().partitions(dataset)
    return {"dataset": dataset,
            "partitions": [r.as_dict() for r in recs]}


@router.get("/sentiment")
def sentiment() -> dict:
    folder = _data_root() / "sentiment"
    if not folder.exists():
        return {"as_of": None, "sentiment": None}
    files = sorted(folder.glob("*.json"))
    if not files:
        return {"as_of": None, "sentiment": None}
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            503, detail={"code": "SENTIMENT_UNREADABLE",
                         "message": f"{files[-1].name}: {type(exc).__name__}"}) from exc
