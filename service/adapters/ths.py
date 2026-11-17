"""Tonghuashun special data: limit pools, the ladder, the dragon-tiger board.

These four datasets are what a short-horizon trader reads before the open:

* which names are limit-up, why, at what time, with how much sealed money;
* how many boards deep the strongest names are, and how often a board promotes;
* which seats (institutions, hot money) actually bought the moves.

None of it is a signal. All of it is the tape's own bookkeeping, and it is
structured so a model can ask "was the seal rate high yesterday" instead of
inferring sentiment from a price chart.

Contract notes that cost time to discover and are worth writing down:

* ``date_ms`` is the **Asia/Shanghai midnight** of the trading day, in
  milliseconds, not a ``YYYYMMDD`` string.
* ``dragon-tiger-list`` is the exception: it takes ``date=yyyy-MM-dd``.
* ``limit-up-ladder`` takes no parameters and always returns the last 30
  sessions; each call is therefore 30x redundant, so only the requested day is
  written.
* The ladder's own dates come back as ``YYYY-MM-DD`` even though the docs show
  ``YYYYMMDD`` for ``window.date_list``.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from service import settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
PAGE_SIZE = settings.integer("source.ths.page_size")
TIMEOUT = settings.num("source.ths.timeout")
BOARD_KEYS = ("two_board", "three_board", "four_board", "five_board",
              "six_board", "seven_over")


class ThsError(RuntimeError):
    def __init__(self, code, message: str, path: str):
        super().__init__(f"{path}: code={code} {message}")
        self.code = code


def available() -> bool:
    return bool(os.getenv("THS_API_KEY", "").strip())


def _base() -> str:
    return os.getenv("THS_BASE_URL", "https://fuyao.aicubes.cn").rstrip("/")


def _get(path: str, params: dict | None = None) -> dict:
    import httpx

    key = os.getenv("THS_API_KEY", "").strip()
    if not key:
        raise ThsError(2001, "THS_API_KEY is empty", path)
    response = httpx.get(f"{_base()}{path}", params=params or {},
                         headers={"X-api-key": key}, timeout=TIMEOUT)
    payload = response.json()
    if payload.get("code") != 0:
        raise ThsError(payload.get("code"), str(payload.get("message")), path)
    return payload.get("data") or {}


def date_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y%m%d")
               .replace(tzinfo=SHANGHAI).timestamp() * 1000)


def iso_date(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def _dash(day: str) -> str:
    return day.replace("-", "")


def _response_day(value, requested: str, path: str, *, required: bool = False) -> str:
    """Validate an upstream session instead of relabelling it as the request."""
    text = str(value or "").strip()
    if not text:
        if required:
            raise ThsError("missing_trade_date", "upstream trade_date is missing", path)
        return requested
    day = _dash(text)
    try:
        datetime.strptime(day, "%Y%m%d")
    except ValueError as exc:
        raise ThsError(
            "invalid_trade_date", f"invalid upstream trade_date: {value!r}", path
        ) from exc
    return day


# --------------------------------------------------------------------- pools
# A pool is a few hundred rows in a handful of pages. The cap is not a guess at
# the real size: it is the difference between "the vendor reports 400 pages" and
# "we send 400 requests", which is what an unbounded `while True` does when
# ``pagination.pages`` is wrong. `fetch_auction` already had this guard.
MAX_POOL_PAGES = settings.integer("source.ths.max_pool_pages")


def _pool(path: str, day: str, columns: dict[str, str]) -> pd.DataFrame:
    """Walk the pages of a pool endpoint and rename to our column vocabulary."""
    rows: list[dict] = []
    page = 1
    seen_first: set[str] = set()
    while page <= MAX_POOL_PAGES:
        data = _get(path, {"date_ms": date_ms(day), "page": page, "size": PAGE_SIZE})
        items = data.get("item") or []
        first = {str(item.get("thscode")) for item in items[:5]}
        if first and first <= seen_first:
            # The same rows came back for a new page: the endpoint is ignoring
            # ``page``, and continuing would append the pool to itself N times.
            break
        seen_first |= first
        rows.extend(items)
        pages = int((data.get("pagination") or {}).get("pages") or 1)
        if page >= pages or not items:
            break
        page += 1
    if not rows:
        return pd.DataFrame(columns=["trade_date", "ts_code", *columns.values()])
    frame = pd.DataFrame(rows).rename(columns={"thscode": "ts_code", **columns})
    # If the upstream row carries its own date, trust it over the request — and
    # refuse a mismatch. Assigning `day` unconditionally meant a pool endpoint
    # that ignores `date_ms` (three sibling endpoints already do, one of them
    # documented) would file *today's* boards under a 2026-01-05 partition,
    # stamped with a checksum and a snapshot id, indistinguishable afterwards.
    #
    # A date we cannot parse is a different matter: refusing it would turn a
    # vendor format change into a total outage for every pool. In that case the
    # requested day stands and no comparison is claimed.
    if "trade_date" in frame.columns:
        parsed: list[str] = []
        for value in frame["trade_date"]:
            try:
                parsed.append(_response_day(value, day, path))
            except ThsError:
                parsed = []
                break
        if parsed:
            conflicts = sorted(set(parsed) - {day})
            if conflicts:
                raise ThsError(
                    "trade_date_conflict",
                    f"requested {day}, upstream returned {', '.join(conflicts)}",
                    path,
                )
            frame["trade_date"] = parsed
        else:
            frame["trade_date"] = day
    else:
        frame["trade_date"] = day
    keep = ["trade_date", "ts_code", *columns.values()]
    for column in keep:
        if column not in frame.columns:
            frame[column] = None
    return frame[keep]


def fetch_limit_up(day: str) -> pd.DataFrame:
    return _pool("/api/a-share/special-data/limit-up-pool", day, {
        "name": "name", "is_st": "is_st", "is_new": "is_new",
        "last_price": "last_price", "price_change_ratio_pct": "pct",
        "limit_up_time": "limit_up_time", "limit_up_reason": "reason",
        "continue_day_text": "continue_text", "continue_day_cnt": "continue_cnt",
        "seal_money": "seal_money", "max_seal_money": "max_seal_money",
    })


def fetch_limit_down(day: str) -> pd.DataFrame:
    return _pool("/api/a-share/special-data/limit-down-pool", day, {
        "name": "name", "last_price": "last_price",
        "price_change_ratio_pct": "pct",
        "first_limit_time": "first_limit_time", "last_limit_time": "last_limit_time",
        "turnover_ratio_pct": "turnover_ratio",
    })


def fetch_limit_break(day: str) -> pd.DataFrame:
    return _pool("/api/a-share/special-data/limit-break-pool", day, {
        "name": "name", "last_price": "last_price",
        "price_change_ratio_pct": "pct", "open_times": "open_times",
        "turnover_ratio_pct": "turnover_ratio", "turnover": "turnover",
    })


# -------------------------------------------------------------------- ladder
def fetch_ladder() -> pd.DataFrame:
    """Long format: one row per (date, board, symbol) for the whole 30-day window."""
    data = _get("/api/a-share/special-data/limit-up-ladder")
    rows: list[dict] = []
    for day_block in data.get("item") or []:
        day = _dash(str(day_block.get("date") or ""))
        if len(day) != 8:
            continue
        for board in BOARD_KEYS:
            for item in (day_block.get("boards") or {}).get(board) or []:
                rows.append({
                    "trade_date": day,
                    "board": board,
                    "ts_code": item.get("thscode"),
                    "name": item.get("name"),
                    "board_num": item.get("board_num"),
                    "seal_nextday": item.get("seal_nextday"),
                    "sign_level": item.get("sign_level"),
                })
    return pd.DataFrame(rows, columns=["trade_date", "board", "ts_code", "name",
                                       "board_num", "seal_nextday", "sign_level"])


def fetch_limit_stat(day: str) -> dict:
    """Seal rate and promotion ladder for **today**.

    The endpoint accepts ``date_ms`` and ignores it: every date returns the
    current session's numbers (verified against three different dates). So the
    upstream's own ``trade_date`` is authoritative and the caller must partition
    on it, not on the requested date. Historical seal rates are therefore not
    recoverable from this vendor and ``limit_structure`` has to derive them.
    """
    path = "/api/a-share/special-data/limit-up-statistics"
    data = _get(path, {"date_ms": date_ms(day)})
    return {
        "trade_date": _response_day(data.get("trade_date"), day, path, required=True),
        "requested_date": day,
        "seal_rate": data.get("seal_rate"),
        "limit_up_stock_count": data.get("limit_up_stock_count"),
        "touched_limit_up_stock_count": data.get("touched_limit_up_stock_count"),
        "open_stock_count": data.get("open_stock_count"),
        "page_boards": data.get("page_boards") or [],
        "exact_boards": data.get("exact_boards") or [],
    }


def stat_frame(stat: dict) -> pd.DataFrame:
    """Flatten the promotion ladder into one row per board level."""
    day = stat["trade_date"]
    rows = [{"trade_date": day, "level": str(item.get("board_level")),
             "stock_count": item.get("stock_count"),
             "promotion_rate": item.get("promotion_rate")}
            for item in stat.get("page_boards") or []]
    summary = {"seal_rate": stat.get("seal_rate"),
               "limit_up_stock_count": stat.get("limit_up_stock_count"),
               "touched_limit_up_stock_count": stat.get("touched_limit_up_stock_count"),
               "open_stock_count": stat.get("open_stock_count")}
    if rows:
        rows[0].update(summary)
    elif any(value is not None for value in summary.values()):
        rows.append({"trade_date": day, "level": None, "stock_count": None,
                     "promotion_rate": None, **summary})
    return pd.DataFrame(rows, columns=["trade_date", "level", "stock_count",
                                       "promotion_rate", "seal_rate",
                                       "limit_up_stock_count",
                                       "touched_limit_up_stock_count",
                                       "open_stock_count"])


# --------------------------------------------------------------- dragon tiger
def fetch_lhb(day: str, board_type: str = "all") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stock-level board rows and hot-money seat rows, normalised separately.

    The board is published after the close, so asking for session D before the
    publication returns D-1's rows — that is normal and the returned date is
    kept. What is *not* normal is a payload with rows and no date at all: those
    rows would be stamped with the requested day and written into that
    partition with a checksum, and no later reader could tell. ``required=True``
    makes that case an error instead of a silent mis-stamp.
    """
    path = "/api/a-share/special-data/dragon-tiger-list"
    data = _get(path, {"date": iso_date(day), "board_type": board_type})
    trade_date = _response_day(data.get("trade_date"), day, path,
                               required=bool(data.get("stock_items")))
    if trade_date > day:
        raise ThsError(
            "trade_date_conflict",
            f"board dated {trade_date} is later than the requested {day}", path)

    stocks: list[dict] = []
    for item in data.get("stock_items") or []:
        concepts = item.get("concept_list") or []
        stocks.append({
            "trade_date": trade_date,
            "ts_code": item.get("thscode"),
            "name": item.get("name"),
            "pct": None if item.get("change") is None else float(item["change"]) * 100,
            "net_value": item.get("net_value"),
            "net_rate": None if item.get("net_rate") is None else float(item["net_rate"]) * 100,
            "buy_value": item.get("buy_value"),
            "sell_value": item.get("sell_value"),
            "amount": item.get("amount"),
            "hot_rank": item.get("hot_rank"),
            "range_days": item.get("range_days"),
            "reason": item.get("limit_reason"),
            "org_net_value": item.get("org_net_value"),
            "org_net_rate": (None if item.get("org_net_rate") is None
                             else float(item["org_net_rate"]) * 100),
            "org_buy_num": item.get("org_buy_num"),
            "org_sell_num": item.get("org_sell_num"),
            "hot_money_net_value": item.get("hot_money_net_value"),
            "concepts": "|".join(str(c.get("name")) for c in concepts if c.get("name")),
        })

    seats: list[dict] = []
    for seat in data.get("hot_money_items") or []:
        for row in seat.get("rows") or []:
            seats.append({
                "trade_date": trade_date,
                "seat": seat.get("name"),
                "ts_code": row.get("thscode"),
                "name": row.get("name"),
                "net_value": row.get("hot_money_item_net_value"),
                "net_rate": (None if row.get("hot_money_item_net_rate") is None
                             else float(row["hot_money_item_net_rate"]) * 100),
            })

    stock_cols = ["trade_date", "ts_code", "name", "pct", "net_value", "net_rate",
                  "buy_value", "sell_value", "amount", "hot_rank", "range_days",
                  "reason", "org_net_value", "org_net_rate", "org_buy_num",
                  "org_sell_num", "hot_money_net_value", "concepts"]
    seat_cols = ["trade_date", "seat", "ts_code", "name", "net_value", "net_rate"]
    return (pd.DataFrame(stocks, columns=stock_cols),
            pd.DataFrame(seats, columns=seat_cols))


# ------------------------------------------------------------------- auction
AUCTION_PAGE = settings.integer("source.ths.auction_page")   # >20 -> code=1003


def _auction_warn_at(value) -> datetime | None:
    """Epoch seconds/ms in Asia/Shanghai; tiny test sentinels are not dates."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 1_000_000_000:
        return None
    seconds = number / 1000 if number >= 100_000_000_000 else number
    try:
        return datetime.fromtimestamp(seconds, SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None


def fetch_auction(day: str, max_pages: int = 80) -> pd.DataFrame:
    """Pre-open alerts with first/last occurrences and lossless raw events.

    The public row shape stays one row per ``(symbol, alert type)`` and keeps the
    first trigger in the legacy columns. Repeated alerts are no longer discarded:
    ``event_count``, ``last_*`` and ``events_json`` preserve their process.
    Pagination completeness is explicit in both columns and ``frame.attrs`` so a
    capped crawl is stored as a warning rather than masquerading as full coverage.
    """
    path = "/api/a-share/special-data/auction-stock-opportunities"
    rows: list[dict] = []
    page = 1
    pages_fetched = 0
    upstream_total = None
    has_next = False
    signatures = set()
    while page <= max_pages:
        data = _get(path, {"date": iso_date(day), "page": page, "size": AUCTION_PAGE})
        items = data.get("item") or []
        pagination = data.get("pagination") or {}
        raw_total = pagination.get("total")
        try:
            upstream_total = int(raw_total) if raw_total is not None else upstream_total
        except (TypeError, ValueError):
            pass
        if not items:
            break
        signature = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
        if signature in signatures:
            raise ThsError("page_repeat", f"page {page} repeated an earlier payload", path)
        signatures.add(signature)
        rows.extend(items)
        pages_fetched += 1
        has_next = bool(pagination.get("has_next"))
        if not has_next:
            break
        page += 1

    truncated = bool(has_next and pages_fetched >= max_pages)
    complete = bool(not truncated and (upstream_total is None or len(rows) == upstream_total))
    capture = {"pages_fetched": pages_fetched, "upstream_total": upstream_total,
               "capture_rows": len(rows), "truncated": truncated, "complete": complete}
    captured_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    columns = ["trade_date", "ts_code", "name", "warn_type", "warn_name", "warn_ts",
               "bid_price", "bid_pct", "bid_amount", "previous_amount", "related_block",
               "event_count", "first_warn_at", "last_warn_ts", "last_warn_at",
               "last_bid_price", "last_bid_pct", "last_bid_amount", "events_json",
               "pages_fetched", "upstream_total", "capture_rows", "capture_truncated",
               "capture_complete", "captured_at", "date_basis"]
    if not rows:
        empty = pd.DataFrame(columns=columns)
        empty.attrs["capture"] = capture
        return empty

    frame = pd.DataFrame(rows)
    warn_dates = [_auction_warn_at(value) for value in frame.get("warn_timestamp")]
    response_days = []
    date_basis = []
    for index, value in enumerate(frame.get("trade_date", [None] * len(frame))):
        stamp = warn_dates[index]
        if value is not None and not (isinstance(value, float) and math.isnan(value)):
            response_days.append(_response_day(value, day, path))
            date_basis.append("trade_date")
        elif stamp is not None:
            response_days.append(stamp.strftime("%Y%m%d"))
            date_basis.append("warn_timestamp")
        else:
            response_days.append(day)
            date_basis.append("request_date")
    conflicts = sorted(set(response_days) - {day})
    if conflicts:
        raise ThsError("trade_date_conflict",
                       f"requested {day}, upstream returned {', '.join(conflicts)}", path)

    out = pd.DataFrame({
        "trade_date": response_days,
        "ts_code": frame.get("thscode"),
        "name": frame.get("name"),
        "warn_type": frame.get("warn_type"),
        "warn_name": frame.get("warn_name"),
        "warn_ts": pd.to_numeric(frame.get("warn_timestamp"), errors="coerce"),
        "bid_price": pd.to_numeric(frame.get("bid_price"), errors="coerce"),
        "bid_pct": pd.to_numeric(frame.get("bid_change_ratio"), errors="coerce") * 100,
        "bid_amount": pd.to_numeric(frame.get("bid_amount"), errors="coerce"),
        "previous_amount": pd.to_numeric(frame.get("previous_amount"), errors="coerce"),
        "related_block": frame.get("related_block"),
        "first_warn_at": [stamp.isoformat() if stamp else None for stamp in warn_dates],
        "_raw": rows,
        "_date_basis": date_basis,
    })
    out = out.sort_values("warn_ts", kind="mergesort", na_position="last")
    first_rows = []
    for _, group in out.groupby(["trade_date", "ts_code", "warn_name"],
                                sort=False, dropna=False):
        first = group.iloc[0].copy()
        last = group.iloc[-1]
        first["event_count"] = len(group)
        first["last_warn_ts"] = last["warn_ts"]
        first["last_warn_at"] = last["first_warn_at"]
        first["last_bid_price"] = last["bid_price"]
        first["last_bid_pct"] = last["bid_pct"]
        first["last_bid_amount"] = last["bid_amount"]
        first["events_json"] = json.dumps(group["_raw"].tolist(), ensure_ascii=False,
                                          default=str, separators=(",", ":"))
        first["pages_fetched"] = pages_fetched
        first["upstream_total"] = upstream_total
        first["capture_rows"] = len(rows)
        first["capture_truncated"] = truncated
        first["capture_complete"] = complete
        first["captured_at"] = captured_at
        first["date_basis"] = first["_date_basis"]
        first_rows.append(first)
    result = pd.DataFrame(first_rows).drop(columns=["_raw", "_date_basis"])
    result = result[columns].reset_index(drop=True)
    result.attrs["capture"] = capture
    return result


# ------------------------------------------------------------------- anomaly
def fetch_anomaly(day: str | None = None) -> pd.DataFrame:
    """Per-stock explanations for today's big moves.

    The endpoint ignores ``date_ms`` (four different dates returned identical
    rows), so this is a today-only capture: the desk accumulates its own history
    one session at a time. ``analysis_content`` is machine-written prose, not a
    filing — it is stored as a lead to go and verify, never as a fact.
    """
    data = _get("/api/a-share/special-data/anomaly-analysis-list")
    items = data.get("item") or []
    if not items:
        return pd.DataFrame(columns=["trade_date", "ts_code", "tag", "analysis"])
    frame = pd.DataFrame(items)
    out = pd.DataFrame({
        "trade_date": day or "",
        "ts_code": frame.get("thscode"),
        "name": frame.get("stock_name"),
        "tag": frame.get("tag_name"),
        "keywords": frame.get("keyword_list").apply(
            lambda v: "|".join(v) if isinstance(v, list) else ""),
        "analysis": frame.get("analysis_content").astype(str).str.slice(0, 800),
    })
    return out.drop_duplicates(subset=["trade_date", "ts_code"])
