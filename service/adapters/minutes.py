"""Intraday bars from two free providers, with the differences kept visible.

Free minute data is not one thing, and the difference is not cosmetic:

| provider | 5-minute depth | 成交额 | 复权 |
|---|---|---|---|
| 东财 `push2his` | ~32 sessions | yes | 不复权 (`fqt=0`) |
| 腾讯 `ifzq` | 320 bars ≈ 7 sessions | no | 不复权 |

Neither can be asked for 2024. EastMoney returns what it keeps; Tencent caps at
320 bars whatever you request. So a long minute history can only be **collected**,
one session at a time — this module is the front door for that, not a backfill.

The fallback exists because the primary falls over. On 2026-10-28 `push2his`
answered a handful of requests and then began closing connections without a
response, which is what a free endpoint does under load; Tencent answered
throughout. A desk that can only read one free source reads nothing on the day
that source is unhappy.

Rows carry `source`, so a mixing of the two is visible rather than silent, and
`amount` is null on Tencent rows — a VWAP computed from it would be an invention,
so the reader checks instead of assuming.
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from service.adapters.eastmoney_adapter import EastMoneyAdapter
from service.adapters.tencent_adapter import TencentQuoteAdapter

COLUMNS = ["ts_code", "trade_time", "trade_date", "open", "close", "high", "low",
           "vol", "amount", "source"]


def _from_tencent(adapter: TencentQuoteAdapter, symbol: str, *, klt: int = 5) -> pd.DataFrame:
    """320 bars at the requested interval, no turnover value, unadjusted."""
    # The prefix and the code are concatenated, not comma separated:
    # `param=sh600519,m5,,320`. The comma form returns an empty `data` object,
    # which reads like "no bars" rather than "wrong request".
    interval = f"m{klt}"
    code = f"{_prefix(symbol)}{symbol.split('.')[0]}"
    payload = adapter.get_json(
        f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},{interval},,320")
    node = ((payload or {}).get("data") or {}).get(code) or {}
    bars = node.get(interval) or []
    rows = []
    for bar in bars:
        if len(bar) < 6:
            continue
        stamp = str(bar[0])                       # 202610281455
        try:
            datetime.strptime(stamp, "%Y%m%d%H%M")
        except ValueError:
            continue
        rows.append({
            "ts_code": symbol,
            "trade_time": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}",
            "trade_date": stamp[:8],
            "open": float(bar[1]), "close": float(bar[2]),
            "high": float(bar[3]), "low": float(bar[4]),
            "vol": float(bar[5]),
            "amount": None,                       # the field is turnover, not 成交额
            "source": "tencent",
        })
    return pd.DataFrame(rows, columns=COLUMNS)


def _prefix(symbol: str) -> str:
    market = symbol.split(".")[-1].upper()
    if market == "SH":
        return "sh"
    if market == "BJ":
        return "bj"
    return "sz"


#: When the primary feed is down, stop asking it for a while. `push2his` does not
#: answer 500; it accepts connections and closes them, so each attempt costs the
#: full retry budget (~3 s) before the fallback gets a turn. Measured 2026-11-01:
#: four symbols in a row timed out at 3.0–3.5 s each and Tencent served all four.
#: A shared cooldown turns that from per-symbol to once per ten minutes, which is
#: the difference between an on-demand fetch costing 1 s and costing 4 s.
EM_COOLDOWN_S = 600.0
_em_down_until = 0.0


def reset_breaker() -> None:
    """Forget the cooldown. Tests and any explicit re-probe need this."""
    global _em_down_until
    _em_down_until = 0.0


def em_available() -> bool:
    """Whether the primary feed is inside its cooldown window."""
    return time.monotonic() >= _em_down_until


def fetch_minutes(symbol: str, *, em: EastMoneyAdapter | None = None,
                  tg: TencentQuoteAdapter | None = None,
                  klt: int = 5) -> tuple[pd.DataFrame, str]:
    """Bars and the provider that produced them. Never raises for a dead source."""
    global _em_down_until
    if klt not in {1, 5, 15, 30, 60}:
        raise ValueError("minute klt must be one of 1, 5, 15, 30, 60")
    errors: list[str] = []
    if em is not None and em_available():
        try:
            frame = em.fetch_minutes(symbol, klt=klt)
        except Exception as exc:  # noqa: BLE001 - contract: never raise for a feed
            errors.append(f"eastmoney: {type(exc).__name__}")
            _em_down_until = time.monotonic() + EM_COOLDOWN_S
            frame = pd.DataFrame()
        if frame is not None and not frame.empty:
            frame = frame.copy()
            frame["source"] = "eastmoney"
            _em_down_until = 0.0
            return frame, "eastmoney"
    elif em is not None:
        errors.append("eastmoney: cooling down")
    if tg is not None:
        try:
            frame = _from_tencent(tg, symbol, klt=klt)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"tencent: {type(exc).__name__}")
            frame = pd.DataFrame()
        if frame is not None and not frame.empty:
            return frame, "tencent"
    return pd.DataFrame(columns=COLUMNS), "none" + (f" ({'; '.join(errors)})" if errors else "")
