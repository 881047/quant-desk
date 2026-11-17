"""Text as data: announcements, news and research notes, with a PIT clock.

The desk has no internet. WorkBuddy does. So the flow is:

    WorkBuddy (search / read the web)
        -> data/inbox/text/<YYYYMMDD>.json      (raw, human-inspectable)
        -> scripts/ingest_text.py               (validate, dedupe, partition)
        -> data/parquet/news/<YYYYMMDD>.parquet (queryable, PIT-filterable)
        -> /knowledge/news                      (what the trader actually sees)

Why not let the model paste headlines into a memo? Because a headline without a
timestamp is indistinguishable from a rumour, and a memo cannot be re-queried next
week. Text gets the same treatment as prices: an id, a source, a trust tier, and a
clock.

The clock is the point. A decision made on the close of ``D`` may use text
published up to ``D 15:00``. Anything published later belongs to ``D+1``. Without
that rule, an 18:00 session would happily read the 20:00 announcement and call it
foresight.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

import pandas as pd

KINDS = ("announcement", "news", "research", "policy")
TIERS = ("official", "media", "web")
DEFAULT_CUTOFF = "15:00"

_TS_ALIASES = ("ts_code", "symbol", "code", "股票代码")
_TITLE_ALIASES = ("title", "新闻标题", "标题")
_SUMMARY_ALIASES = ("summary", "content", "新闻内容", "摘要")
_TIME_ALIASES = ("pub_time", "发布时间", "时间", "datetime", "publish_time")
_SOURCE_ALIASES = ("source", "文章来源", "来源")
_URL_ALIASES = ("url", "新闻链接", "链接", "link")

_TIME_PATTERNS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M",
    "%Y-%m-%d",
    "%Y%m%d",
)


def _pick(record: dict, names: tuple[str, ...], default=None):
    for name in names:
        value = record.get(name)
        if value not in (None, "", "-"):
            return value
    return default


def parse_time(value) -> tuple[str, str] | None:
    """Return (YYYYMMDD, HH:MM). Date-only values become 00:00."""
    if value is None:
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        stamp = value.to_pydatetime() if isinstance(value, pd.Timestamp) else value
        return stamp.strftime("%Y%m%d"), stamp.strftime("%H:%M")
    text = str(value).strip()
    if not text:
        return None
    for pattern in _TIME_PATTERNS:
        try:
            stamp = datetime.strptime(text[:len(pattern) + 4], pattern)
        except ValueError:
            continue
        return stamp.strftime("%Y%m%d"), stamp.strftime("%H:%M")
    digits = re.sub(r"\D", "", text)
    for size, pattern in ((14, "%Y%m%d%H%M%S"), (12, "%Y%m%d%H%M"), (8, "%Y%m%d")):
        if len(digits) >= size:
            try:
                stamp = datetime.strptime(digits[:size], pattern)
            except ValueError:
                continue
            return stamp.strftime("%Y%m%d"), stamp.strftime("%H:%M")
    return None


def to_ts_code(value) -> str:
    """'002966' -> '002966.SZ'. Unknown shapes are kept as-is so nothing is lost."""
    text = str(value or "").strip().upper()
    if not text or text in ("-", "ALL", "全部"):
        return ""
    if text.endswith((".SH", ".SZ", ".BJ")):
        return text
    digits = re.sub(r"\D", "", text)
    if len(digits) != 6:
        return text
    if digits.startswith(("60", "68", "51", "56", "58", "11")):
        return f"{digits}.SH"
    if digits.startswith(("00", "30", "15", "16", "12", "18")):
        return f"{digits}.SZ"
    if digits.startswith(("4", "8", "92")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


def normalize(record: dict, *, default_kind: str = "news") -> dict | None:
    """One raw record -> one row, or None when it cannot be trusted."""
    if not isinstance(record, dict):
        return None
    title = _clip(_pick(record, _TITLE_ALIASES, ""), 200)
    stamp = parse_time(_pick(record, _TIME_ALIASES))
    if not title or stamp is None:
        return None
    pub_date, pub_time = stamp

    source = _clip(_pick(record, _SOURCE_ALIASES, "unknown"), 60) or "unknown"
    url = _clip(_pick(record, _URL_ALIASES, ""), 400)
    kind = str(record.get("kind") or default_kind).lower()
    if kind not in KINDS:
        kind = default_kind if default_kind in KINDS else "news"
    tier = str(record.get("tier") or "web").lower()
    if tier not in TIERS:
        tier = "web"

    sentiment = record.get("sentiment")
    try:
        sentiment = None if sentiment in (None, "") else max(-1.0, min(1.0, float(sentiment)))
    except (TypeError, ValueError):
        sentiment = None

    code = to_ts_code(_pick(record, _TS_ALIASES, ""))
    news_id = hashlib.sha1(
        f"{source}|{url}|{code}|{title}".encode()).hexdigest()[:16]

    return {
        "news_id": news_id,
        "pub_date": pub_date,
        "pub_time": pub_time,
        "ts_code": code,
        "title": title,
        "summary": _clip(_pick(record, _SUMMARY_ALIASES, ""), 800),
        "source": source,
        "url": url,
        "kind": kind,
        "tier": tier,
        "sentiment": sentiment,
        "tags": _clip(record.get("tags") or "", 120),
    }


def ingest(store, records: list[dict], *, as_of: str,
           source: str = "workbuddy", trust_tier: str = "T3",
           default_kind: str = "news") -> dict:
    """Normalise, dedupe and write one partition per publication date."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    for record in records:
        row = normalize(record, default_kind=default_kind)
        if row is None:
            rejected.append({"record": str(record)[:200], "why": "缺标题或时间不可解析"})
        else:
            accepted.append(row)

    if not accepted:
        return {"ok": True, "written": 0, "rejected": len(rejected), "details": rejected,
                "note": "没有可入库的记录（每条都需要 title 与可解析的 pub_time）"}

    frame = pd.DataFrame(accepted).drop_duplicates(subset=["news_id"], keep="last")
    by_date: dict[str, int] = {}
    for pub_date, chunk in frame.groupby("pub_date"):
        store.write("news", str(pub_date), chunk, as_of=as_of, mode="merge",
                    source=source, trust_tier=trust_tier,
                    note=f"{len(chunk)} items")
        by_date[str(pub_date)] = len(chunk)
    return {
        "ok": True,
        "written": len(frame),
        "by_date": by_date,
        "rejected": len(rejected),
        "details": rejected[:5],
        "kinds": frame["kind"].value_counts().to_dict(),
        "codes": int(frame["ts_code"].replace("", pd.NA).nunique()),
    }


def _usable(row, day: str, cutoff: str) -> bool:
    """A piece of text is usable at decision time only if it was public by then."""
    pub_date = str(row["pub_date"])
    if pub_date < day:
        return True
    if pub_date > day:
        return False
    return str(row["pub_time"]) <= cutoff


def query(store, *, day: str, symbol: str | None = None, days: int = 7,
          limit: int = 20, cutoff: str = DEFAULT_CUTOFF,
          include_market: bool = True) -> dict:
    """PIT-filtered text around a date, for one symbol or the whole market."""
    start = (datetime.strptime(day, "%Y%m%d") - timedelta(days=max(days, 1))).strftime("%Y%m%d")
    frame = store.read("news", start=start, end=day)
    empty = {"ok": False, "as_of": day, "symbol": symbol,
             "error": ("文本库为空或窗口内没有记录。先让 WorkBuddy 抓取并运行 "
                       "scripts/ingest_text.py（或 scripts/fetch_text.py 兜底）。"),
             "coverage": {"items": 0, "days_with_text": 0}}
    if frame.empty:
        return empty

    mask = frame.apply(lambda r: _usable(r, day, cutoff), axis=1)
    frame = frame[mask]
    if frame.empty:
        return empty

    total = len(frame)
    dates = sorted(frame["pub_date"].astype(str).unique())
    coverage = {
        "items": int(total),
        "days_with_text": len(dates),
        "window_start": start,
        "latest": dates[-1] if dates else None,
        "stale_days": ((datetime.strptime(day, "%Y%m%d")
                        - datetime.strptime(dates[-1], "%Y%m%d")).days if dates else None),
        "pit_cutoff": cutoff,
        "note": (f"{day} 的决策只能用到 {cutoff} 之前发布的文本；之后的算作次日信息。"),
    }

    # The vendor serves one market-wide headline once per symbol, and `news_id`
    # includes the code, so three symbols mean three rows with three ids. On a
    # single name that is harmless; on a market query it reads as three
    # independent reports of one fact, and repetition is the cheapest thing to
    # mistake for corroboration. `items` stays the raw window count; the caller
    # receives the deduplicated set.
    raw = frame
    distinct = raw.drop_duplicates(subset=["title", "pub_time", "kind"], keep="first")
    coverage["distinct_items"] = len(distinct)
    coverage["duplicate_rows"] = len(raw) - len(distinct)

    scoped = raw
    if symbol:
        code = to_ts_code(symbol)
        keep = scoped["ts_code"].astype(str).eq(code)
        if include_market:
            keep = keep | scoped["ts_code"].astype(str).eq("")
        scoped = scoped[keep]
        if scoped.empty:
            return {"ok": True, "as_of": day, "symbol": code,
                    "coverage": coverage, "items": [],
                    "note": f"{code} 在窗口内没有文本；coverage 是市场整体覆盖，不是这只票的。"}

    # Scope first: another symbol's copy of a shared headline must not suppress
    # the requested symbol's own row. Market coverage above remains global.
    scoped = scoped.drop_duplicates(subset=["title", "pub_time", "kind"], keep="first")
    scoped = scoped.sort_values(["pub_date", "pub_time"], ascending=False).head(limit)
    items = []
    for _, row in scoped.iterrows():
        raw_code = row["ts_code"]
        # ``NaN or "市场"`` returns NaN: a float NaN is truthy, so the market-level
        # fallback never fired. The NaN then reached the response body, where
        # Starlette's ``allow_nan=False`` turned a news read into a 500.
        blank = (raw_code is None
                 or (isinstance(raw_code, float) and pd.isna(raw_code))
                 or str(raw_code).strip().lower() in ("", "nan", "none", "nat"))
        items.append({
            "ts_code": "市场" if blank else str(raw_code).strip(),
            "pub_date": str(row["pub_date"]),
            "pub_time": str(row["pub_time"]),
            "kind": row["kind"],
            "tier": row["tier"],
            "title": row["title"],
            "summary": row["summary"],
            "source": row["source"],
            "url": row["url"],
            "sentiment": None if pd.isna(row["sentiment"]) else round(float(row["sentiment"]), 2),
            "tags": row["tags"],
        })

    counts = scoped["kind"].value_counts().to_dict()
    return {
        "ok": True,
        "as_of": day,
        "symbol": to_ts_code(symbol) if symbol else None,
        "coverage": coverage,
        "by_kind": {k: int(v) for k, v in counts.items()},
        "items": items,
        "caveats": [
            "文本来自网页/公告转写，tier=web 的条目未经核实，可能与原文不符。",
            "标题不等于事实：公告要在原文里确认口径（金额、主体、生效条件）。",
            "检索是按代码做的，结果可能包含同行业或板块级新闻；标题里的主体不一定是你查的这只票。",
            f"只回溯 {days} 天；更早的文本在 parquet 里，可把 days 调大。",
        ],
        "provenance": {"dataset": "news", "trust_tier": "T3",
                       "ingested_via": "workbuddy -> inbox -> ingest_text"},
    }
