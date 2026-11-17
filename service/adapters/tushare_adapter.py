"""Tushare Pro adapter with rate limiting, retries and permission classification.

Tushare at 2,000 points allows roughly 200 calls/minute on the interfaces we
use, and some interfaces (``limit_list_d``, ``anns_d``) are simply not granted.
The adapter therefore has three jobs beyond HTTP:

1. stay inside the quota (token bucket, default 180/min);
2. retry transient failures with exponential backoff;
3. turn "no permission" into a typed exception so the backfill can mark a
   dataset *skipped* instead of retrying forever.
"""

from __future__ import annotations

import operator
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import pandas as pd


class TushareError(RuntimeError):
    pass


class TusharePermissionError(TushareError):
    """Interface not granted for this token's points."""


class TushareQuotaError(TushareError):
    """Per-minute or per-day quota exhausted after retries."""


def _integer(value: Any, name: str, *, minimum: int) -> int:
    """Reject bool-as-int while preserving valid zero for retry counts."""
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer, not bool")
    try:
        result = operator.index(value)
    except TypeError as exc:
        raise TypeError(f"{name} must be an integer") from exc
    if result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


class RateLimiter:
    def __init__(self, per_minute: int = 180, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.per_minute = _integer(per_minute, "per_minute", minimum=1)
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= 60.0:
            self._calls.popleft()
        if len(self._calls) >= self.per_minute:
            wait = 60.0 - (now - self._calls[0]) + 0.05
            if wait > 0:
                self._sleep(wait)
            now = self._clock()
            while self._calls and now - self._calls[0] >= 60.0:
                self._calls.popleft()
        self._calls.append(self._clock())


def _classify(message: str) -> type[TushareError]:
    text = str(message)
    if "每分钟" in text or "每天最多" in text or "频率" in text:
        return TushareQuotaError
    if "权限" in text or "积分" in text:
        return TusharePermissionError
    return TushareError




def quarter_windows(start: str, end: str) -> list[tuple[str, str, str]]:
    """Calendar quarters in range as (period_end, window_start, window_end)."""
    out: list[tuple[str, str, str]] = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        for month_end in ("0331", "0630", "0930", "1231"):
            period = f"{year}{month_end}"
            if period < start or period > end:
                continue
            month = int(month_end[:2])
            out.append((period, f"{year}{month - 2:02d}01", period))
    return out


def _window_chunks(start: str, end: str, days: int = 7) -> list[tuple[str, str]]:
    """Split ``[start, end]`` into windows of at most ``days`` calendar days."""
    from datetime import date, timedelta

    def parse(text: str) -> date:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))

    first, last = parse(start), parse(end)
    out: list[tuple[str, str]] = []
    cursor = first
    while cursor <= last:
        stop = min(cursor + timedelta(days=days - 1), last)
        out.append((cursor.strftime("%Y%m%d"), stop.strftime("%Y%m%d")))
        cursor = stop + timedelta(days=1)
    return out


class TushareAdapter:
    def __init__(
        self,
        token: str | None = None,
        *,
        per_minute: int = 180,
        retries: int = 3,
        client: Any | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if client is None:
            if not token:
                raise TusharePermissionError("TUSHARE_TOKEN missing")
            import tushare as ts

            client = ts.pro_api(token)
        self._client = client
        self.per_minute = _integer(per_minute, "per_minute", minimum=1)
        self.retries = _integer(retries, "retries", minimum=0)
        self._sleep = sleep
        self._limiter = limiter or RateLimiter(self.per_minute, sleep=sleep)
        self.calls = 0

    # ------------------------------------------------------------------ core
    def call(self, api_name: str, **params: Any) -> pd.DataFrame:
        api = getattr(self._client, api_name)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            self._limiter.acquire()
            self.calls += 1
            try:
                df = api(**params)
            except Exception as exc:
                last = exc
                kind = _classify(exc)
                if kind is TusharePermissionError:
                    raise TusharePermissionError(f"{api_name}: {exc}") from exc
                if attempt >= self.retries:
                    raise kind(f"{api_name}: {exc}") from exc
                wait = 60.0 if kind is TushareQuotaError else 2.0 ** (attempt + 1)
                self._sleep(min(wait, 65.0))
                continue
            if df is None:
                return pd.DataFrame()
            return df

        raise TushareError(f"{api_name} failed: {last}")

    def call_paged(self, api_name: str, *, page_size: int = 5000, max_pages: int = 40,
                   **params: Any) -> pd.DataFrame:
        page_size = _integer(page_size, "page_size", minimum=1)
        max_pages = _integer(max_pages, "max_pages", minimum=1)
        frames: list[pd.DataFrame] = []
        offset = 0
        complete = False
        for _ in range(max_pages):
            page = self.call(api_name, limit=page_size, offset=offset, **params)
            if page is None or page.empty:
                complete = True
                break
            frames.append(page)
            if len(page) < page_size:
                complete = True
                break
            offset += page_size
        if not frames:
            return pd.DataFrame()
        if not complete and len(frames[-1]) == page_size:
            raise TushareError(
                f"{api_name}: incomplete after {max_pages} pages of {page_size}; "
                "narrow the date/symbol window before retrying"
            )
        return pd.concat(frames, ignore_index=True)

    # ------------------------------------------------------------- reference
    def fetch_trade_cal(self, start: str, end: str) -> pd.DataFrame:
        df = self.call("trade_cal", exchange="SSE", start_date=start, end_date=end)
        if df.empty:
            return df
        return df[["cal_date", "is_open", "pretrade_date"]].copy()

    def fetch_stock_basic(self) -> pd.DataFrame:
        frames = []
        for status in ("L", "D", "P"):
            part = self.call(
                "stock_basic",
                exchange="",
                list_status=status,
                fields="ts_code,symbol,name,area,industry,market,list_date,delist_date,is_hs",
            )
            if part is None or part.empty:
                continue
            part = part.copy()
            part["list_status"] = status
            frames.append(part)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def fetch_index_member(self) -> pd.DataFrame:
        """申万 L1/L2/L3 for every listed name, in one call.

        No arguments: the endpoint returns the whole market (5,922 rows), so the
        346 L3 buckets do not need 346 calls.

        It is a **snapshot, not a history**. `out_date` is empty on every row and
        `is_new` is `Y` on every row; the historical membership lives behind
        `index_member(is_new=N)`, which this token cannot reach. The table
        therefore answers "who is in this industry now" and never "who was in it
        in 2024" — stated here because a `in_date` column looks like it carries
        history and it does not.
        """
        df = self.call("index_member_all")
        if df is None or df.empty:
            return pd.DataFrame()
        return df

    def fetch_namechange(self) -> pd.DataFrame:
        return self.call_paged(
            "namechange",
            fields="ts_code,name,start_date,end_date,ann_date,change_reason",
        )

    # ------------------------------------------------------------- daily panel
    def fetch_daily(self, trade_date: str) -> pd.DataFrame:
        return self.call("daily", trade_date=trade_date)

    def fetch_adj_factor(self, trade_date: str) -> pd.DataFrame:
        return self.call("adj_factor", trade_date=trade_date)

    def fetch_daily_basic(self, trade_date: str) -> pd.DataFrame:
        return self.call("daily_basic", trade_date=trade_date)

    def fetch_stk_limit(self, trade_date: str) -> pd.DataFrame:
        return self.call("stk_limit", trade_date=trade_date)

    def fetch_moneyflow(self, trade_date: str) -> pd.DataFrame:
        return self.call("moneyflow", trade_date=trade_date)

    def fetch_margin_detail(self, trade_date: str) -> pd.DataFrame:
        return self.call("margin_detail", trade_date=trade_date)

    def fetch_share_float(self, start: str, end: str) -> pd.DataFrame:
        """Unlocks whose `float_date` falls in the range, not announcements.

        Chunked by month on purpose: a 14-month range returns more than the
        vendor's 100,000-row paging ceiling and the whole unit dies with
        `offset不能大于100000` after 25 calls of work.
        """
        frames = []
        # Seven-day windows, not months: 2026-12-31 alone is a year-end unlock
        # cliff, and one month's worth blew past the vendor's 100,000-row offset
        # ceiling even after the first round of chunking. `max_pages=18` keeps a
        # single window at or below 90,000 rows so the offset never reaches the cap.
        for first, last in _window_chunks(start, end, days=7):
            chunk = self.call_paged("share_float", start_date=first, end_date=last,
                                    max_pages=18)
            if chunk is not None and not chunk.empty:
                frames.append(chunk)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def fetch_stk_holdernumber(self, ann_date: str) -> pd.DataFrame:
        return self.call("stk_holdernumber", ann_date=ann_date)

    def fetch_top10_floatholders(self, period: str) -> pd.DataFrame:
        return self.call_paged("top10_floatholders", period=period)

    def fetch_pledge_stat(self, end_date: str) -> pd.DataFrame:
        """One quarter-end snapshot of the whole market (~2,200 rows).

        Per-symbol would be 5,577 calls for the same answer; the vendor exposes a
        market-wide snapshot keyed on the quarter end, and a pledge ratio only
        moves when a new quarter is filed.
        """
        return self.call_paged("pledge_stat", end_date=end_date, max_pages=18)

    def fetch_stk_holdertrade(self, start: str, end: str) -> pd.DataFrame:
        return self.call_paged("stk_holdertrade", start_date=start, end_date=end,
                               max_pages=18)

    def fetch_moneyflow_hsgt(self, trade_date: str) -> pd.DataFrame:
        # One row per session; `trade_date` also works as a start/end range, but
        # a single day keeps the backfill unit symmetric with the other panels.
        return self.call("moneyflow_hsgt", trade_date=trade_date)

    def fetch_suspend(self, start: str, end: str) -> pd.DataFrame:
        return self.call_paged(
            "suspend_d", start_date=start, end_date=end,
            fields="ts_code,trade_date,suspend_timing,suspend_type",
        )

    def fetch_dividend(self, ex_date: str) -> pd.DataFrame:
        """Dividend plans whose ex-date is ``ex_date``.

        Keyed by ex-date, not announcement date: the ledger applies the plan on
        the session the price drops, and the dataset is partitioned by
        ``ex_date`` (catalog.py). Pulling by ``ann_date`` used to scatter rows
        into partitions of a different date, which made the partition snapshot
        depend on which announcement days happened to be fetched.
        """
        return self.call(
            "dividend", ex_date=ex_date,
            fields="ts_code,ann_date,end_date,ex_date,div_proc,cash_div,"
                   "cash_div_tax,stk_div,stk_bo_rate,stk_co_rate,"
                   "record_date,pay_date",
        )

    # ------------------------------------------------------------------- ETF
    def fetch_fund_basic(self) -> pd.DataFrame:
        return self.call_paged(
            "fund_basic", market="E",
            fields="ts_code,name,management,custodian,fund_type,found_date,list_date,delist_date,status",
        )

    def fetch_fund_daily(self, ts_code: str, start: str, end: str) -> pd.DataFrame:
        return self.call("fund_daily", ts_code=ts_code, start_date=start, end_date=end)

    def fetch_fund_adj(self, ts_code: str, start: str, end: str) -> pd.DataFrame:
        return self.call("fund_adj", ts_code=ts_code, start_date=start, end_date=end)

    def fetch_fund_nav(self, ts_code: str, start: str, end: str) -> pd.DataFrame:
        return self.call("fund_nav", ts_code=ts_code, start_date=start, end_date=end)

    # --------------------------------------------------------------- index/Fin
    def fetch_index_daily(self, ts_code: str, start: str, end: str) -> pd.DataFrame:
        return self.call("index_daily", ts_code=ts_code, start_date=start, end_date=end)

    def fetch_fina_indicator(self, ts_code: str, start: str, end: str) -> pd.DataFrame:
        """One symbol, all reporting periods announced in the range.

        `period=` alone is rejected by this token (`必填参数, ts_code`), so the
        period-based fan-out the old signature assumed never ran.
        """
        return self.call_paged("fina_indicator", ts_code=ts_code,
                               start_date=start, end_date=end)
