"""Stop *state*, never stop *orders*.

A stop written on a ticket is a number the model chose when it opened the
position. This module reports whether that number still holds: distance,
sessions below, and the arithmetic of exiting at the next open.

It does not sell. There used to be an ``enforce()`` here that created a ticket
with ``actor="stops"`` and approved it, writing its own thesis ("triggered by
stops.enforce, independent of model judgement"). That is a code-authored trade,
which is exactly what this desk is not allowed to have: the kernel measured the
breach, collected a fee estimate, and then also pulled the trigger. Those two
roles have to be separate or the measurement becomes a pretext.

This module only reports state; execution lives in ``autostop.py``. Once a
breach has persisted for ``autostop.confirm_seconds``, the capture loop puts the
stop written on the entry ticket through ``/decision/propose`` + ``approve``
(``autostop.enabled`` in ``configs/params.yaml`` turns it off). The model can
also place its own sell through ``/decision/propose`` with its own thesis; the
audit always shows who decided.

Conventions, stated because they are limits rather than choices:

* The trigger is close-based (``last <= stop``). A daily-bar system cannot know
  whether an intraday touch would have filled, so it does not pretend to. When
  the low pierces the stop but the close recovers, that is reported as
  ``intraday_pierce`` instead of being traded.
* A position bought today has ``available == 0`` under T+1. The stop then reports
  ``blocked_t1`` and retries on the next session rather than silently dropping.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from service.kernel.units import SHARES_PER_LOT


def stop_risk_context(*, last: float, stop: float, qty: int,
                      equity: float, max_trade_risk: float,
                      atr20_fraction: float | None = None,
                      lot: int = SHARES_PER_LOT,
                      atr_multiple: float = 2.5) -> dict:
    """Arithmetic around one stop: volatility width and risk-budget usage.

    Facts and measurements only — no action, score, ranking or verdict. This
    module must never decide whether a stop is "good"; it reports the numbers
    the caller needs to make that judgment themselves.

    Two口径 rules that matter and are easy to get wrong:

    * ``stop_width`` is measured in *traded* prices (``last`` and ``stop`` come
      from the same book/quote space), so subtracting them is legitimate.
    * ATR20 is computed on **adjusted** prices (see ``profile._technicals``).
      It is therefore passed in as a *fraction of price*, never as a price to
      subtract. ``stop_width_in_atr`` is a dimensionless ratio of two fractions,
      which is why it stays correct across ex-dividend gaps.

    ``atr_rule`` answers a what-if, not a recommendation: if a stop were placed
    ``atr_multiple`` ATRs away, what would one lot risk against the per-ticket
    budget? On a small book this is where an ATR-based stop and a fixed risk
    budget can be arithmetically incompatible — a fact worth seeing before a
    ticket is written, not after.
    """
    def _finite(value):
        try:
            out = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return out if math.isfinite(out) else None

    last_f, stop_f = _finite(last), _finite(stop)
    equity_f = _finite(equity)
    risk_pct = _finite(max_trade_risk)
    atr_f = _finite(atr20_fraction)
    qty_f = _finite(qty)
    qty_i = int(qty_f) if qty_f is not None else 0

    bad = (last_f is None or last_f <= 0 or stop_f is None or stop_f < 0
           or qty_i <= 0 or equity_f is None or equity_f <= 0
           or risk_pct is None or risk_pct <= 0)
    if bad:
        return {"available": False,
                "reason": ("需要 last>0、stop>=0、qty>0、equity>0、"
                           "max_trade_risk>0；缺一项就不编数")}

    width_cny = last_f - stop_f
    width_pct = width_cny / last_f
    risk_to_stop = width_cny * qty_i
    budget = equity_f * risk_pct
    out = {
        "available": True,
        "stop_width_cny": round(width_cny, 4),
        "stop_width_pct": round(width_pct, 6),
        "risk_to_stop_cny": round(risk_to_stop, 2),
        "risk_budget_cny": round(budget, 2),
        "risk_budget_used_pct": round(risk_to_stop / budget, 4) if budget else None,
        "atr20_pct": round(atr_f * 100, 2) if atr_f is not None else None,
        "stop_width_in_atr": (round(width_pct / atr_f, 4)
                              if atr_f and atr_f > 0 else None),
        "atr_rule": None,
        "units": {"stop_width_pct": "fraction (0..1)",
                  "risk_budget_used_pct": "fraction, >1 表示超预算",
                  "atr20_pct": "percent", "stop_width_in_atr": "倍数，无量纲"},
        "note": ("纯测算，不是建议。stop_width_in_atr<1 表示止损宽度不足一个日均波幅；"
                 "risk_budget_used_pct>1 表示这笔按止损计的风险超过单票预算。"
                 "ATR 用价格百分比口径，未把前复权价与真实成交价相减。"),
    }
    if atr_f and atr_f > 0:
        rule_width_pct = atr_multiple * atr_f
        one_lot_risk = rule_width_pct * last_f * lot
        out["atr_rule"] = {
            "multiple": atr_multiple,
            "width_pct": round(rule_width_pct, 6),
            "one_lot_risk_cny": round(one_lot_risk, 2),
            "budget_used_pct": round(one_lot_risk / budget, 4) if budget else None,
            "note": ("假设按该倍数的 ATR 设止损，一手的风险与预算占比；"
                     "是 what-if，不是应设的止损价"),
        }
    return out


@dataclass(frozen=True)
class StopView:
    symbol: str
    name: str
    qty: int
    available: int
    stop: Decimal
    last: Decimal
    low: Decimal | None
    sleeve: str
    ticket_id: str
    breached: bool
    pierce_only: bool
    blocked: bool
    breach_at: str | None = None    # first 5-minute close through the stop
    price_source: str = "daily"     # daily | eastmoney | tencent
    price_ts: str | None = None
    age_seconds: float | None = None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "qty": self.qty,
            "available": self.available,
            "stop": str(self.stop),
            "last": str(self.last),
            "low": None if self.low is None else str(self.low),
            "distance_pct": round(float(self.last / self.stop - 1), 4) if self.stop > 0 else None,
            "sleeve": self.sleeve,
            "ticket_id": self.ticket_id,
            "breached": self.breached,
            "pierce_only": self.pierce_only,
            "blocked": self.blocked,
            "breach_at": self.breach_at,
            "price_source": self.price_source,
            "price_ts": self.price_ts,
            "age_seconds": None if self.age_seconds is None else round(self.age_seconds, 1),
        }


def _held_symbols(desk) -> list[str]:
    """Symbols with a non-zero position, straight from the ledger.

    ``getattr`` all the way down on purpose: the unit tests hand this module a
    two-field desk double, and a stop report that raises because the double has
    no ledger is worse than one that falls back to a wider query.
    """
    book = getattr(desk, "book", None)
    account = getattr(getattr(book, "ledger", None), "account", None)
    positions = getattr(account, "positions", None) or {}
    # Snapshot before scanning: this is called from the watch/stop paths, which
    # do not hold the book lock, while a fill can insert into `positions`.
    return [symbol for symbol, pos in tuple(positions.items()) if pos.total_qty > 0]


def stop_map(desk, symbols: list[str] | None = None) -> dict[str, dict]:
    """The stop written on the *current* entry, per held symbol.

    The list is newest-first, so the natural reading — "first BUY that has a
    stop" — keeps the most recent stopped ticket, not the most recent ticket.
    Sell out, buy back in without naming a stop, and the level nobody chose for
    this position is still reported as its stop: ``duty`` asks for an exit every
    morning and ``propose`` 409s on a number the model never wrote.

    The newest BUY wins whether or not it carries a stop. When it does not, the
    position has no stop, which is a different and honest answer.

    Filtered to held symbols and to those symbols only. The unfiltered version
    read the newest 500 filled tickets, which is both slow (4.1 ms per call, and
    ``stop_gate`` called it twice) and wrong past that horizon: a position older
    than the window silently lost its stop.
    """
    symbols = _held_symbols(desk) if symbols is None else list(symbols)
    lookup = getattr(getattr(desk, "paper", None), "stop_tickets", None)
    if lookup is not None:
        # Empty symbols means a flat book, and the answer is "no stops" — not
        # "scan the history and report the ones you sold".
        tickets = lookup(symbols)
    else:
        # A desk double that does not own a store: fall back to the window so
        # the helper still answers something instead of raising.
        tickets = desk.paper.list_tickets(limit=500, status="filled")

    out: dict[str, dict] = {}
    seen: set[str] = set()
    for ticket in tickets:
        if str(ticket.get("side", "")).upper() != "BUY":
            continue
        symbol = str(ticket.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        stop = ticket.get("stop_price")
        if stop in (None, "", "None"):
            continue
        out[symbol] = ticket
    return out


def stopped_symbols(desk, stops: dict[str, dict] | None = None) -> set[str]:
    """Held names that have a stop written on a buy ticket.

    Public because "this position has no stop" and "this position's stop cannot
    be priced" are two different states, and a caller that only sees
    ``open_stops`` cannot tell them apart: a position with no ticket at all is
    silently absent from that list.
    """
    return set(stop_map(desk) if stops is None else stops)


def _intraday_breach(desk, symbol: str, day: str, stop: Decimal) -> str | None:
    """First 5-minute close at or below the stop, when minute data exists."""
    try:
        from .intraday import describe, load

        frame = load(desk.data, symbol, day)
        if frame.empty:
            return None
        info = describe(frame, symbol, day, stop)
        return info.breach_at if info else None
    except (KeyError, ValueError):
        return None


def open_stops(desk, date: str | None = None, quotes=None, *,
               stops: dict[str, dict] | None = None) -> list[StopView]:
    """Every stopped position, priced from a live quote when one is supplied.

    ``quotes`` is a mapping of symbol -> Quote (see ``realtime.py``). Without it
    the view falls back to the stored daily close, and says so in
    ``price_source``: a caller must be able to tell whether "breached" means
    "right now" or "at yesterday's close".
    """
    day = date or desk.market.latest_bar_date()
    if not day:
        return []
    summary = desk.book.summary()
    stops = stop_map(desk) if stops is None else stops
    views: list[StopView] = []
    for pos in summary["positions"]:
        ticket = stops.get(pos["symbol"])
        if ticket is None:
            continue
        snap = desk.market.snapshot(pos["symbol"], day)
        quote = (quotes or {}).get(pos["symbol"])
        if snap is None and quote is None:
            continue
        # A queued fill re-anchors the stop to the price actually paid.
        stop = Decimal(str(ticket.get("stop_price_final") or ticket["stop_price"]))
        if quote is not None:
            last, low = quote.last, quote.low
            source, stamp, age = quote.source, quote.quote_ts, quote.age_seconds
        else:
            last, low = snap.last, getattr(snap, "low", None)
            source, stamp, age = "daily", snap.trade_date, None
        breach_at = _intraday_breach(desk, pos["symbol"], day, stop)
        views.append(StopView(
            symbol=pos["symbol"],
            name=pos.get("name") or "",
            qty=int(pos["qty"]),
            available=int(pos["available_qty"]),
            stop=stop,
            last=last,
            low=low,
            sleeve=str(ticket.get("sleeve") or "CORE"),
            ticket_id=str(ticket.get("ticket_id") or ""),
            breached=last <= stop,
            pierce_only=bool(low is not None and low <= stop < last),
            blocked=int(pos["available_qty"]) < int(pos["qty"]),
            breach_at=breach_at,
            price_source=source,
            price_ts=stamp,
            age_seconds=age,
        ))
    return views

