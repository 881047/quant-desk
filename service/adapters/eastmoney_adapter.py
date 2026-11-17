"""EastMoney adapters: limit-up / broken-board / limit-down pools + live quotes.

These are T1 sources (vendor aggregation, not exchange files). They are used for
the aggressive sleeve and for intraday context, never as the sole basis for
price/limit calculations — those come from Tushare T0 data.

Historical pool coverage on EastMoney starts around 2020 and some early days are
incomplete, so a partition is marked ``partial`` when the pool is empty on a day
that clearly had limit-ups. The quality layer records this instead of silently
pretending the day had zero limit-ups.
"""

from __future__ import annotations

import email.utils
import logging
import math
import random
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pandas as pd

from service import settings

PUSH2EX = "https://push2ex.eastmoney.com"
PUSH2HIS = "https://push2his.eastmoney.com"
PUSH2 = "https://push2.eastmoney.com"
UT = "7eea3edcaed734bea9cbfc24409ed989"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}


class EastMoneyError(RuntimeError):
    pass


def _finite_timeout(value: Any) -> float:
    seconds = float(value)
    if isinstance(value, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("timeout must be a finite positive number")
    return seconds


def _retry_after_seconds(response_or_error: Any) -> float | None:
    """Parse Retry-After as seconds or an HTTP date, never as milliseconds."""
    response = getattr(response_or_error, "response", None)
    source = response if response is not None else response_or_error
    headers = getattr(source, "headers", None) or {}
    value = headers.get("Retry-After")
    if value is None:
        value = headers.get("retry-after")
    if value is None:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001, S110 - close must not hide the result
            pass


def _retryable_http_error(exc: Exception, response: Any = None) -> bool:
    if isinstance(exc, (httpx.RequestError, ConnectionError, TimeoutError)):
        return True
    source = response if response is not None else getattr(exc, "response", None)
    if source is None and hasattr(exc, "code"):
        source = exc
    status = getattr(source, "status_code", None)
    if status is None:
        status = getattr(source, "status", getattr(source, "code", None))
    if status is not None:
        return int(status) == 429 or int(status) >= 500
    return isinstance(exc, OSError)


class RetryBudget:
    """Shared bounded exponential backoff with Retry-After and small jitter."""

    def __init__(self, max_seconds: float = 30.0):
        value = float(max_seconds)
        if isinstance(max_seconds, bool) or not math.isfinite(value) or value < 0:
            raise ValueError("max_seconds must be a finite non-negative number")
        self.max_seconds = value
        self.started = time.monotonic()

    def wait(self, attempt: int, response_or_error: Any = None) -> bool:
        remaining = self.max_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            return False
        retry_after = _retry_after_seconds(response_or_error)
        base = retry_after if retry_after is not None else min(0.25 * (2 ** attempt), 4.0)
        delay = base + random.uniform(0.0, min(0.25, 0.05 + base * 0.1))
        if delay > remaining:
            return False
        time.sleep(delay)
        return True


def _hhmmss(value: Any) -> str | None:
    """Normalise HHMMSS / HH:MM:SS and reject impossible vendor times."""
    if value is None or isinstance(value, bool) or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    if ":" in text:
        parts = text.split(":")
        if len(parts) == 2:
            parts.append("00")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            return None
        hour, minute, second = (int(p) for p in parts)
    elif text.isdigit() and len(text) <= 6:
        digits = text.zfill(6)
        hour, minute, second = int(digits[:2]), int(digits[2:4]), int(digits[4:])
    else:
        return None
    if hour > 23 or minute > 59 or second > 59:
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


POOL_ENDPOINTS = {
    "limit_up_pool": "getTopicZTPool",
    "broken_board_pool": "getTopicZBPool",
    "limit_down_pool": "getTopicDTPool",
}

# The DT pool has no first-seal-time field; sorting by it returns an empty page.
POOL_SORT = {
    "limit_up_pool": "fbt:asc",
    "broken_board_pool": "fbt:asc",
    "limit_down_pool": "fund:asc",
}

POOL_FIELDS: dict[str, dict[str, str]] = {
    "limit_up_pool": {
        "c": "code", "n": "name", "m": "market", "p": "close", "zdp": "pct_chg",
        "amount": "amount", "ltsz": "float_mv", "tshare": "total_mv", "hs": "turnover",
        "lbc": "boards", "fbt": "first_seal_time", "lbt": "last_seal_time",
        "fund": "seal_amount", "zbc": "open_times", "hybk": "industry",
    },
    "broken_board_pool": {
        "c": "code", "n": "name", "m": "market", "p": "close", "ztp": "limit_price",
        "zdp": "pct_chg", "amount": "amount", "ltsz": "float_mv", "tshare": "total_mv",
        "hs": "turnover", "fbt": "first_seal_time", "zbc": "open_times",
        "zf": "amplitude", "zs": "speed", "hybk": "industry",
    },
    "limit_down_pool": {
        "c": "code", "n": "name", "m": "market", "p": "close", "zdp": "pct_chg",
        "amount": "amount", "ltsz": "float_mv", "tshare": "total_mv", "hs": "turnover",
        "fund": "seal_amount", "lbt": "last_seal_time", "fba": "board_amount",
        "days": "consecutive_down", "oc": "open_times", "hybk": "industry",
    },
}

TIME_FIELDS = {"first_seal_time", "last_seal_time"}


def _secid(symbol: str) -> str:
    """`600519.SH` → `1.600519`；`000001.SH` → `1.000001`（指数也是 1.）；无后缀时按数字猜。"""
    code, _, suffix = str(symbol).partition(".")
    code = code.zfill(6)
    suffix = suffix.upper()
    if suffix == "SH":
        return f"1.{code}"
    if suffix in ("SZ", "BJ"):
        return f"0.{code}"
    return f"{_market_of(code)}.{code}"


def _market_of(code: str) -> int:
    """1 = Shanghai, 0 = Shenzhen/Beijing. See ``service.adapters.symbols``.

    This used to be ``code[0] in "659"``, which sent an SSE convertible
    (111012) to Shenzhen and returned nothing at all — a silent gap that read
    as "the vendor has no data for that bond".
    """
    from .symbols import eastmoney_market

    return eastmoney_market(code)


class EastMoneyAdapter:
    def __init__(self, *, timeout: float | None = None, retries: int | None = None,
                 client: httpx.Client | None = None, min_interval: float | None = None,
                 max_seconds: float | None = None):
        """~3 calls/s between requests: faster than that and EastMoney drops the
        connection. The rate and the timeout live in configs/params.yaml
        (source.eastmoney.*), not here."""
        if getattr(client, "trust_env", False):
            raise ValueError("client must use trust_env=False; ambient proxies are not supported")
        self.timeout = _finite_timeout(
            settings.pick(timeout, "source.eastmoney.timeout", settings.num))
        self.retries = settings.pick(retries, "source.eastmoney.retries", settings.integer)
        self._client = client
        self.min_interval = settings.pick(min_interval, "source.eastmoney.min_interval", settings.num)
        self.max_seconds = settings.pick(max_seconds, "source.eastmoney.max_seconds", settings.num)
        self._last_call = 0.0
        self.calls = 0

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        import time

        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        budget = RetryBudget(self.max_seconds)
        for attempt in range(self.retries + 1):
            response = None
            try:
                self._throttle()
                self.calls += 1
                if self._client is not None:
                    response = self._client.get(
                        url, params=params, headers=HEADERS, timeout=self.timeout)
                else:
                    with httpx.Client(trust_env=False, timeout=self.timeout) as client:
                        response = client.get(
                            url, params=params, headers=HEADERS, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001 - transport boundary
                last = exc
                retry_source = response if response is not None else exc
                if (attempt >= self.retries
                        or not _retryable_http_error(exc, response)
                        or not budget.wait(attempt, retry_source)):
                    break
            finally:
                _close_response(response)
        raise EastMoneyError(f"GET {url} failed: {last}") from last

    # ----------------------------------------------------------------- minutes
    @staticmethod
    def _secid(symbol: str) -> str:
        """EastMoney's id: 1.600519 for Shanghai, 0.000001 for Shenzhen/BJ."""
        code, _, market = symbol.partition(".")
        prefix = "1" if market.upper() == "SH" else "0"
        return f"{prefix}.{code}"

    def fetch_minutes(self, symbol: str, klt: int = 5, limit: int = 2000) -> pd.DataFrame:
        """Intraday bars from EastMoney. Free, no key, and shallow.

        Depth is the whole story and it is not in any parameter: 5-minute bars go
        back about 30 sessions, 15-minute about 60, 1-minute only the current
        day. There is no way to ask for 2024 — `beg=0` returns what the endpoint
        keeps, not what you requested. That makes this a **forward-accumulating**
        table: the daily job appends today, and history is whatever has been
        collected since the job started. Anything that needs a long minute history
        must be built by waiting, not by backfilling.
        """
        payload = self._get(
            f"{PUSH2HIS}/api/qt/stock/kline/get",
            {
                "secid": self._secid(symbol),
                "klt": str(klt),
                "fqt": "0",                     # 未复权: same basis as Tencent fallback
                "beg": "0",
                "end": "20500101",
                "lmt": str(limit),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            },
        )
        data = (payload or {}).get("data") or {}
        klines = data.get("klines") or []
        if not klines:
            return pd.DataFrame(columns=["ts_code", "trade_time", "open", "close",
                                         "high", "low", "vol", "amount"])
        # "2026-10-28 09:35,1324.00,1324.07,1333.60,1322.58,2964,393600621.00,0.83"
        rows = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 7:
                continue
            stamp = parts[0].strip()
            try:
                datetime.strptime(stamp, "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            rows.append({
                "ts_code": symbol,
                "trade_time": stamp,
                "trade_date": stamp[:10].replace("-", ""),
                "open": float(parts[1]), "close": float(parts[2]),
                "high": float(parts[3]), "low": float(parts[4]),
                "vol": float(parts[5]),         # 手
                "amount": float(parts[6]),      # 元
            })
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------ pools
    def fetch_pool(self, dataset: str, trade_date: str) -> pd.DataFrame:
        endpoint = POOL_ENDPOINTS[dataset]
        payload = self._get(
            f"{PUSH2EX}/{endpoint}",
            {
                "ut": UT,
                "dpt": "wz.ztzt",
                "Pageindex": "0",
                "pagesize": "10000",
                "sort": POOL_SORT[dataset],
                "date": trade_date,
            },
        )
        data = (payload or {}).get("data") or {}
        # push2ex echoes the session it answered for in ``qdate``. Reading it is
        # the difference between "this is the board for 20240603" and "this is
        # whatever it returned when we asked". The endpoint ignored ``date`` for
        # early periods once already, and a backfill that writes today's board
        # into a 2024 partition is indistinguishable from real history afterwards.
        echoed = data.get("qdate") or data.get("date") or data.get("trade_date")
        if echoed:
            echoed_day = str(echoed).replace("-", "")[:8]
            if echoed_day.isdigit() and echoed_day != str(trade_date):
                raise EastMoneyError(
                    f"pool {dataset}: upstream answered for {echoed_day}, "
                    f"asked for {trade_date}")
        else:
            logging.getLogger(__name__).warning(
                "pool %s: upstream returned no session date; stamping %s",
                dataset, trade_date)
        rows = data.get("pool") or []
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=["trade_date", "code", "name", "source"])

        mapping = POOL_FIELDS[dataset]
        keep = [c for c in mapping if c in df.columns]
        out = df[keep].rename(columns={k: mapping[k] for k in keep})

        if "close" in out.columns:
            out["close"] = pd.to_numeric(out["close"], errors="coerce") / 1000.0
        if "limit_price" in out.columns:
            out["limit_price"] = pd.to_numeric(out["limit_price"], errors="coerce") / 1000.0
        for col in TIME_FIELDS:
            if col in out.columns:
                out[col] = out[col].map(_hhmmss)
        if "code" in out.columns:
            out["code"] = out["code"].astype(str).str.zfill(6)
        if "zttj" in df.columns:
            stat = df["zttj"].map(lambda x: x if isinstance(x, dict) else {})
            out["stat_days"] = stat.map(lambda x: x.get("days"))
            out["stat_boards"] = stat.map(lambda x: x.get("ct"))

        out.insert(0, "trade_date", trade_date)
        out["source"] = "eastmoney"
        out["ingested_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        return out

    def fetch_all_pools(self, trade_date: str) -> dict[str, pd.DataFrame]:
        return {name: self.fetch_pool(name, trade_date) for name in POOL_ENDPOINTS}

    # ---------------------------------------------------------------- quotes
    def fetch_quotes(self, codes: list[str], *, chunk: int = 200) -> pd.DataFrame:
        if not codes:
            return pd.DataFrame()
        # Callers pass "600000.SH" (Tushare style); EastMoney wants "1.600000".
        # 后缀不能丢：`000001.SH` 是上证指数，`000001.SZ` 是平安银行，同一组六位数
        # 在两个市场是两只东西。只按数字判市场会把指数的请求发成银行，回包再被
        # `sym in remaining` 丢掉，表现成"这个指数没有实时价"（2026-11-02 实测）。
        pairs = list(dict.fromkeys(_secid(c) for c in codes))
        frames = []
        for i in range(0, len(pairs), chunk):
            batch = pairs[i : i + chunk]
            secids = ",".join(batch)
            payload = self._get(
                f"{PUSH2}/api/qt/ulist.np/get",
                {
                    "fltt": "2",
                    "invt": "2",
                    "secids": secids,
                    "fields": "f12,f13,f14,f2,f3,f4,f5,f6,f7,f8,f10,f15,f16,f17,f18,f20,f21,f124",
                },
            )
            data = (payload or {}).get("data") or {}
            rows = data.get("diff") or []
            if rows:
                frames.append(pd.DataFrame(rows))
        if not frames:
            return pd.DataFrame()

        raw = pd.concat(frames, ignore_index=True)
        out = pd.DataFrame(
            {
                "code": raw["f12"].astype(str).str.zfill(6),
                "market": raw["f13"],
                "name": raw["f14"],
                "last": raw["f2"],
                "pct_chg": raw["f3"],
                "change": raw["f4"],
                "volume": raw["f5"],
                "amount": raw["f6"],
                "amplitude": raw["f7"],
                "turnover": raw["f8"],
                "volume_ratio": raw["f10"],
                "high": raw["f15"],
                "low": raw["f16"],
                "open": raw["f17"],
                "pre_close": raw["f18"],
                "total_mv": raw["f20"],
                "float_mv": raw["f21"],
                "quote_ts": pd.to_numeric(raw["f124"], errors="coerce"),
            }
        )
        # f2..f21 use "-" for halted names; force numeric.
        for col in out.columns:
            if col not in {"code", "name"}:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        now = datetime.now().astimezone()
        out["snapshot_ts"] = now.isoformat(timespec="seconds")
        out["trade_date"] = now.strftime("%Y%m%d")
        out["source"] = "eastmoney"
        return out
