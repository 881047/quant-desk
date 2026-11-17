"""Intraday bars: the only way a daily system can see a touch.

A 5-minute frame answers two questions a daily bar cannot:

* did the price trade through a stop during the session, and when;
* did our assumed next-open fill actually happen at a tradable price.

The stop policy still defaults to close-based. Switching ``STOP_TRIGGER`` to
``intraday`` makes a 5-minute close through the stop sufficient, which is closer
to a real stop order but noisier — a choice, not an improvement, so it stays
opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pandas as pd


@dataclass(frozen=True)
class Intraday:
    symbol: str
    day: str
    bars: int
    low: Decimal | None
    high: Decimal | None
    breach_at: str | None     # first 5-minute close at or below the stop
    worst_drawdown: float     # close vs the session's running high

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "day": self.day,
            "bars": self.bars,
            "low": None if self.low is None else str(self.low),
            "high": None if self.high is None else str(self.high),
            "breach_at": self.breach_at,
            "worst_drawdown": round(self.worst_drawdown, 4),
        }


def load(store, symbol: str, day: str) -> pd.DataFrame:
    try:
        frame = store.read("stk_mins", day, day)
    except (KeyError, ValueError):
        return pd.DataFrame()
    if frame.empty or "ts_code" not in frame.columns:
        return pd.DataFrame()
    frame = frame[frame["ts_code"].astype(str) == symbol].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["trade_time"] = frame["trade_time"].astype(str)
    return frame.sort_values("trade_time")


def collect(market, symbol: str, day: str, *, fetch: bool = True,
            refresh: bool = False) -> tuple[pd.DataFrame, str]:
    """One session's 5-minute bars, pulled and stored when the desk never had them.

    The table is filled forward for the whole market every evening
    (`scripts/intraday_sweep.py`, baostock, 5,571 names), so on a covered day this
    is a pure read. On a day before the sweep started, or for a name the sweep
    could not reach (北交所 has no baostock code), the read path fills the gap
    once from the two free HTTP endpoints and merges the row into the partition.

    ``refresh=True`` bypasses an already stored frame. It exists for the live
    single-symbol page: an intraday sweep row is a snapshot, not a reason to keep
    returning the same 11:30 bar for the rest of the session. The normal read
    path remains a pure read. A failed refresh returns the stored frame together
    with the failure receipt, so a transient vendor outage does not erase the
    last usable chart or mislabel it as freshly fetched.

    Returns ``(frame, how)`` where ``how`` is one of:

    ``stored``
        Already in the table — no network.
    ``fetched:<source>``
        Just pulled. ``source`` matters: 东财 carries 成交额, 腾讯 does not, so
        VWAP is exact for one and null for the other.
    ``not_collected``
        ``fetch=False`` and nothing stored — the caller asked not to touch the net.
    ``provider_has_no_<day>``
        The endpoint answered, but not for this session. Free minute data only
        goes back ~32 sessions, so a replay of an old date cannot be filled. This
        is the honest version of "we don't have it": the source does not either.
    ``unavailable`` / ``failed:<why>``
        Nothing came back.
    """
    frame = market.minutes(symbol, day)
    if not frame.empty and not refresh:
        return frame, "stored"
    if not fetch:
        return frame, "not_collected"

    from service.adapters.eastmoney_adapter import EastMoneyAdapter
    from service.adapters.minutes import fetch_minutes
    from service.adapters.tencent_adapter import TencentQuoteAdapter

    try:
        bars, source = fetch_minutes(symbol, em=EastMoneyAdapter(),
                                     tg=TencentQuoteAdapter())
    except Exception as exc:                       # noqa: BLE001 - a feed, not a bug
        return frame, f"failed:{type(exc).__name__}"
    if bars is None or bars.empty:
        return (frame if refresh else pd.DataFrame()), f"unavailable:{source}"
    part = bars[bars["trade_date"].astype(str) == str(day)]
    if part.empty:
        return (frame if refresh else pd.DataFrame()), f"provider_has_no_{day}"
    keep = [c for c in ("ts_code", "trade_time", "trade_date", "open", "close",
                        "high", "low", "vol", "amount", "source") if c in part.columns]
    # 当天的分时**不落库**。盘中的半天写进分区之后，晚间 sweep 的
    # `needs_fetch` 看到这只票"已有行"就跳过它，留下的半日数据看起来却像一整天
    # ——2026-11-02 差点就是这样：一次盘中查询足以让 17:30 的全市场补齐漏掉这只票。
    from service.kernel.clock import SHANGHAI

    if str(day) >= datetime.now(SHANGHAI).strftime("%Y%m%d"):
        return (part[keep].sort_values("trade_time").reset_index(drop=True),
                f"fetched:{source}（当日不落库：收盘后由 intraday_sweep 统一补齐）")
    try:
        # merge, not replace: other symbols already in this partition stay.
        market.store.write("stk_mins", str(day), part[keep].copy(), as_of=str(day),
                           mode="merge", note=f"on-demand {source} for {symbol}")
    except (OSError, ValueError) as exc:
        # A read still works; it just will not be there next time.
        return part[keep].sort_values("trade_time").reset_index(drop=True), \
            f"fetched:{source} (store failed: {type(exc).__name__})"
    return market.minutes(symbol, day), f"fetched:{source}"


def describe(frame: pd.DataFrame, symbol: str, day: str,
             stop: Decimal | None = None) -> Intraday | None:
    if frame.empty:
        return None
    close = pd.to_numeric(frame["close"], errors="coerce")
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    breach_at = None
    if stop is not None:
        hit = frame[close <= float(stop)]
        if not hit.empty:
            breach_at = str(hit.iloc[0]["trade_time"])
    running = high.cummax()
    drawdown = close / running - 1
    dd = float(drawdown.min()) if drawdown.notna().any() else 0.0
    return Intraday(
        symbol=symbol, day=day, bars=len(frame),
        low=Decimal(str(low.min())) if low.notna().any() else None,
        high=Decimal(str(high.max())) if len(high.dropna()) else None,
        breach_at=breach_at, worst_drawdown=dd,
    )


EXPECTED_SESSION_BARS = 48


def session_metrics(frame: pd.DataFrame) -> dict:
    """Reproducible 5-minute session shape; descriptive only, never a signal.

    All intervals use bar *end* times. A full A-share session has 48 bars. The
    completeness fields travel beside every derived value so a half-day capture
    cannot be read as a quiet full session. Missing volume, zero range and missing
    midday/last prices remain ``None`` rather than becoming fabricated zeros.
    """
    if frame is None or frame.empty:
        return {"bars": 0, "expected_bars": EXPECTED_SESSION_BARS,
                "bar_coverage": 0.0, "complete_session": False}
    data = frame.copy()
    data["trade_time"] = data["trade_time"].astype(str)
    data = data.sort_values("trade_time", kind="mergesort")
    times = data["trade_time"].str[11:16]
    close = pd.to_numeric(data["close"], errors="coerce")
    high = pd.to_numeric(data["high"], errors="coerce")
    low = pd.to_numeric(data["low"], errors="coerce")
    vol = pd.to_numeric(data["vol"], errors="coerce")
    total_vol = float(vol.sum()) if vol.notna().any() else 0.0

    def volume_share(mask) -> float | None:
        if total_vol <= 0:
            return None
        return float(vol[mask].sum()) / total_vol

    valid_close = close[close > 0]
    last = float(valid_close.iloc[-1]) if not valid_close.empty else None
    morning = (times <= "11:30") & (close > 0)
    afternoon = (times > "11:30") & (close > 0)
    morning_close = float(close[morning].iloc[-1]) if morning.any() else None
    afternoon_return = ((last / morning_close - 1) * 100
                        if last is not None and morning_close and afternoon.any() else None)
    session_high = float(high.max()) if high.notna().any() else None
    session_low = float(low.min()) if low.notna().any() else None
    close_position = ((last - session_low) / (session_high - session_low)
                      if last is not None and session_high is not None
                      and session_low is not None and session_high > session_low else None)
    unique_bars = int(data["trade_time"].nunique())
    last_time = str(times.iloc[-1]) if len(times) else ""
    return {
        "bars": len(data),
        "expected_bars": EXPECTED_SESSION_BARS,
        "bar_coverage": unique_bars / EXPECTED_SESSION_BARS,
        "complete_session": bool(unique_bars == EXPECTED_SESSION_BARS
                                 and last_time >= "15:00"),
        "volume_complete": bool(vol.notna().all()),
        "afternoon_volume_share": volume_share(times > "11:30"),
        "last_30min_volume_share": volume_share((times > "14:30") & (times <= "15:00")),
        "afternoon_return_pct": afternoon_return,
        "close_position_in_range": close_position,
        "close_vs_session_high_pct": ((last / session_high - 1) * 100
                                      if last is not None and session_high else None),
    }
