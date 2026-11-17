"""Tencent quote adapter — the fallback when EastMoney throttles us.

EastMoney starts dropping connections after a few thousand rapid requests
(observed after a 2,034-request pool backfill). Tencent's ``qt.gtimg.cn``
endpoint is lighter, returns a compact GBK text line per stock, and includes
the exchange timestamp we need to decide whether a quote is stale.

It is a *fallback*, not a second source of truth: fields are mapped into the
same schema as EastMoney so the rest of the system does not care which one
answered.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pandas as pd

from service import settings
from service.adapters.eastmoney_adapter import (
    RetryBudget,
    _close_response,
    _finite_timeout,
    _retryable_http_error,
)
from service.kernel.units import CNY_PER_WAN, CNY_PER_YI

BASE = "https://qt.gtimg.cn/q="
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}
SHANGHAI = "Asia/Shanghai"


class TencentError(RuntimeError):
    pass


def _prefix(code: str) -> str:
    """``sh`` / ``sz`` / ``bj`` prefix for qt.gtimg.cn. See ``symbols.tencent_symbol``."""
    from .symbols import tencent_symbol

    return tencent_symbol(code)[:2]


class TencentQuoteAdapter:
    def __init__(self, *, timeout: float | None = None, retries: int | None = None,
                 chunk: int | None = None, client: Any | None = None,
                 max_seconds: float | None = None):
        """Defaults come from configs/params.yaml (source.tencent.*).

        Passing an argument still wins: the tests and the backfill scripts pin
        these on purpose. Leaving them as literals here meant a rate-limit
        incident needed a code edit to slow the crawler down.
        """
        if getattr(client, "trust_env", False):
            raise ValueError("client must use trust_env=False; ambient proxies are not supported")
        self.timeout = _finite_timeout(
            settings.pick(timeout, "source.tencent.timeout", settings.num))
        self.retries = settings.pick(retries, "source.tencent.retries", settings.integer)
        self.chunk = settings.pick(chunk, "source.tencent.chunk", settings.integer)
        self._client = client
        self.max_seconds = settings.pick(
            max_seconds, "source.tencent.max_seconds", settings.num)
        self.calls = 0

    def _request(self, url: str, decode) -> Any:
        last: Exception | None = None
        budget = RetryBudget(self.max_seconds)
        for attempt in range(self.retries + 1):
            response = None
            try:
                self.calls += 1
                if self._client is not None:
                    response = self._client.get(url, headers=HEADERS, timeout=self.timeout)
                else:
                    with httpx.Client(trust_env=False, timeout=self.timeout) as client:
                        response = client.get(url, headers=HEADERS, timeout=self.timeout)
                response.raise_for_status()
                return decode(response)
            except Exception as exc:  # noqa: BLE001 - transport boundary
                last = exc
                retry_source = response if response is not None else exc
                if (attempt >= self.retries
                        or not _retryable_http_error(exc, response)
                        or not budget.wait(attempt, retry_source)):
                    break
            finally:
                _close_response(response)
        raise TencentError(f"GET {url} failed: {last}") from last

    def get_json(self, url: str) -> Any:
        """A JSON endpoint on the same host family, with the same retry policy.

        `ifzq.gtimg.cn` serves the kline endpoints; `qt.gtimg.cn` serves quotes
        as GBK text. Same provider, two shapes, so the transport is shared and
        only the decoder differs.
        """
        return self._request(url, lambda response: response.json())

    def _get_text(self, url: str) -> str:
        return self._request(
            url, lambda response: response.content.decode("gbk", errors="ignore"))

    def fetch_quotes(self, codes: list[str]) -> pd.DataFrame:
        if not codes:
            return pd.DataFrame()
        clean = list(dict.fromkeys(c.split(".")[0].zfill(6) for c in codes))
        rows: list[dict] = []
        for i in range(0, len(clean), self.chunk):
            batch = clean[i : i + self.chunk]
            text = self._get_text(BASE + ",".join(f"{_prefix(c)}{c}" for c in batch))
            for line in text.split(";"):
                parsed = self._parse_line(line)
                if parsed:
                    rows.append(parsed)
        if not rows:
            return pd.DataFrame()
        out = pd.DataFrame(rows)
        now = datetime.now().astimezone()
        out["snapshot_ts"] = now.isoformat(timespec="seconds")
        out["trade_date"] = out["quote_dt"].dt.strftime("%Y%m%d")
        out["source"] = "tencent"
        return out.drop(columns=["quote_dt"])

    @staticmethod
    def _parse_line(line: str) -> dict | None:
        if "=" not in line:
            return None
        body = line.split("=", 1)[1].strip().strip('"')
        parts = body.split("~")
        if len(parts) < 40 or not parts[2]:
            return None

        def num(idx: int) -> float | None:
            try:
                return float(parts[idx])
            except (ValueError, IndexError):
                return None

        stamp = parts[30].strip()
        if not stamp:
            return None
        try:
            quote_dt = pd.Timestamp(
                datetime.strptime(stamp, "%Y%m%d%H%M%S")
            ).tz_localize(SHANGHAI)
        except (ValueError, TypeError):
            return None

        return {
            "code": parts[2],
            "name": parts[1],
            "last": num(3),
            "pre_close": num(4),
            "open": num(5),
            "volume": num(6),
            "quote_ts": quote_dt.timestamp() if pd.notna(quote_dt) else None,
            "change": num(31),
            "pct_chg": num(32),
            "high": num(33),
            "low": num(34),
            "amount": (num(37) or 0) * CNY_PER_WAN,  # Tencent reports 万元
            "turnover": num(38),
            "pe": num(39),
            "amplitude": num(43),
            "float_mv": (num(44) or 0) * CNY_PER_YI,
            "total_mv": (num(45) or 0) * 1e8,
            "pb": num(46),
            # 五档盘口：9/10 买一价/量，19/20 卖一价/量，其余每两格一档。
            "bid1": num(9), "bid1_volume": num(10),
            "ask1": num(19), "ask1_volume": num(20),
            "bid_volume_5": sum(filter(None, (num(10), num(12), num(14),
                                             num(16), num(18)))),
            "ask_volume_5": sum(filter(None, (num(20), num(22), num(24),
                                             num(26), num(28)))),
            # 47/48 是交易所给的涨停/跌停价，用它比自己算 10% 更可靠（ST、科创板不同）。
            "up_limit": num(47), "down_limit": num(48),
            "quote_dt": quote_dt,
        }
