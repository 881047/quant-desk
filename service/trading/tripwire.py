"""The tripwire: should the model be woken right now?

A 24-hour desk wakes several times a session. Each wake is a full agent turn -
dozens of tool calls, tens of thousands of tokens - and most of the time nothing
has changed. Paying that on a timer is not "always on", it is an expensive poll.

So the scheduler runs this first. It is plain arithmetic over the book and the
tape: milliseconds, no model, no writes. It returns a list of *reasons*, and if
the list is empty the turn is skipped and a single line goes to the log.

Two design rules, both learned the hard way elsewhere in this codebase:

* **A check that cannot run is reported as unknown, never as "fine".** If the
  quote service is down, a stop at 4% is not "safe", it is unmeasured, and the
  reason it is unmeasured is printed.
* **The threshold is a reason to look, not a reason to trade.** Every output line
  says what changed, not what to do. The model reads it and decides.

Cost: one quote call for the holdings, plus two small parquet reads for the tape.
That is roughly 200 ms warm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from service import settings
from service.kernel.clock import SHANGHAI

# A stop 2% away is close enough that an ordinary session can reach it.
STOP_PROXIMITY = settings.num("tripwire.stop_proximity")
# A single holding moving this much has changed the book's risk without anyone
# deciding anything.
POSITION_MOVE = settings.num("tripwire.position_move")
# A queued order within this distance of the last price may fill at the open.
PENDING_DISTANCE = settings.num("tripwire.pending_distance")


@dataclass
class Finding:
    code: str
    severity: str            # critical / high / normal
    detail: str
    subject: str | None = None
    numbers: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "detail": self.detail,
                "subject": self.subject, "numbers": self.numbers}


def _stops(desk, day: str, quotes: dict) -> list[Finding]:
    from .stops import open_stops

    out: list[Finding] = []
    try:
        views = open_stops(desk, day, quotes=quotes)
    except (settings.MissingParameter, settings.BadParameter):
        # ``open_stops`` prices the next-open exit, so it reads the cost model.
        # A missing rate must surface as a configuration error, not as
        # "止损距离无法计算" (which blames the market data).
        raise
    except (ValueError, KeyError, RuntimeError) as exc:
        return [Finding("stop_unmeasured", "normal",
                        f"止损距离无法计算：{type(exc).__name__}: {exc}")]
    for view in views:
        if view.breached:
            out.append(Finding(
                "stop_breach", "critical",
                f"{view.name or view.symbol} 收盘/现价 {view.last} 已跌破止损 {view.stop}",
                subject=view.symbol,
                numbers={"last": str(view.last), "stop": str(view.stop),
                         "price_source": getattr(view, "price_source", None)},
            ))
            continue
        distance = float(view.last / view.stop - 1) if view.stop > 0 else None
        if distance is not None and distance <= STOP_PROXIMITY:
            out.append(Finding(
                "stop_proximity", "high",
                f"{view.name or view.symbol} 距止损仅 {distance:.2%}",
                subject=view.symbol,
                numbers={"distance_pct": round(distance, 4),
                         "stop": str(view.stop), "last": str(view.last)},
            ))
    return out


def _moves(desk, day: str, quotes: dict) -> list[Finding]:
    out: list[Finding] = []
    for pos in desk.book.summary()["positions"]:
        symbol = pos["symbol"]
        quote = quotes.get(symbol) if quotes else None
        pct = quote.pct_chg if quote is not None else None
        if pct is None:
            continue
        if abs(pct) >= POSITION_MOVE:
            out.append(Finding(
                "position_move", "high",
                f"{pos.get('name') or symbol} 今日 {pct:+.1%}，持仓 {pos['qty']} 股",
                subject=symbol,
                numbers={"pct_chg": round(pct, 4), "qty": pos["qty"],
                         "weight": round(float(pos.get("weight") or 0), 4)},
            ))
    return out


def _pending(desk, quotes: dict) -> list[Finding]:
    out: list[Finding] = []
    pending = desk.book.state.get("pending") or []
    for entry in pending:
        symbol = entry.get("symbol")
        quote = quotes.get(symbol) if quotes else None
        if quote is None or quote.last <= 0:
            continue
        limit = float(entry.get("limit_price") or 0)
        if limit <= 0:
            continue
        gap = abs(float(quote.last) / limit - 1)
        if gap <= PENDING_DISTANCE:
            out.append(Finding(
                "pending_near_fill", "normal",
                f"{symbol} 挂单限价 {limit} 距现价 {gap:.2%}，{quote.source} 报价",
                subject=symbol,
                numbers={"limit": limit, "last": str(quote.last),
                         "gap_pct": round(gap, 4)},
            ))
    return out


def _tape(desk, day: str) -> list[Finding]:
    """A regime change in the limit-up tape is a reason to look at the book."""
    try:
        from .tape import limit_history
    except ImportError:
        return []
    try:
        history = limit_history(desk, day)
    except (ValueError, KeyError, OSError):
        return []
    if not history.get("ok"):
        return []

    out: list[Finding] = []
    # 这一块的分位数取自同花顺序列，而 `counts.limit_up` 是派生池——同一天可能
    # 差一两只（2026-11-02：131 对 132）。叫醒理由里必须写明是哪一份，否则同一
    # 次 wake 里 findings 说 131、changes 说 132，读的人无法判断谁错了。
    basis = "同花顺口径" if history.get("basis") == "ths" else "厂商口径"
    today = history.get("limit_up") or {}
    pct = today.get("percentile")
    if pct is not None and (pct >= 0.90 or pct <= 0.10):
        out.append(Finding(
            "tape_extreme", "normal",
            f"涨停家数 {today.get('today')}（{basis}）处于 {pct:.0%} 分位"
            f"（{today.get('sessions')} 个交易日的样本）",
            numbers={"percentile": round(pct, 3), "today": today.get("today"),
                     "basis": history.get("basis")},
        ))
    seal = history.get("seal_rate") or {}
    seal_pct = seal.get("percentile")
    if seal_pct is not None and (seal_pct >= 0.90 or seal_pct <= 0.10):
        out.append(Finding(
            "seal_rate_extreme", "normal",
            f"封板率 {seal.get('today')}（{basis}）处于 {seal_pct:.0%} 分位"
            f"（{seal.get('sessions')} 个交易日的样本）",
            numbers={"percentile": round(seal_pct, 3), "today": seal.get("today"),
                     "basis": history.get("basis")},
        ))
    return out


def evaluate(desk, *, day: str | None = None, now: datetime | None = None,
             include_tape: bool = True, frame=None) -> dict:
    """Run every check and return the findings. Pure: no writes, no orders.

    ``frame`` is a captured market frame (the DataFrame in ``rt_snapshots``). When
    supplied, the book is priced from **those rows** and no vendor request is
    made — the snapshot loop judges a stop against the same bytes ``quant_live``
    will hand the model, instead of a second request three seconds later that
    makes one instant into two numbers. The frame is filtered to the held names
    inside, after they are known: a two-name book has no business converting
    5,217 rows.

    ``frame=None`` means "go and ask the vendor", which is what ``/watch`` does.
    """
    from .realtime import STALE_AFTER, quote_freshness, service_for, session_state

    now = now or datetime.now(SHANGHAI)
    day = day or desk.market.last_trading_day()
    session = session_state(now, calendar=getattr(desk.market, "calendar", None))

    measured: dict = {}
    quote_error = None
    held = [p["symbol"] for p in desk.book.summary()["positions"]]
    pending = [e.get("symbol") for e in desk.book.state.get("pending") or []]
    wanted = sorted({s for s in held + pending if s})
    if frame is not None:
        measured = service_for(desk.market).from_frame(frame, symbols=wanted, now=now)
    elif wanted:
        try:
            fetched = desk.quotes(wanted) or {}
            measured = {symbol: quote for symbol, quote in fetched.items() if symbol in wanted}
        except (ValueError, KeyError, RuntimeError) as exc:
            quote_error = f"{type(exc).__name__}: {exc}"

    # A daily fallback is useful for displaying an estimate, but it is not a
    # live measurement. Partial answers need the same warning as a total outage:
    # otherwise one fresh symbol makes the whole book look measured.
    def is_fresh(symbol: str) -> bool:
        quote = measured.get(symbol)
        age = getattr(quote, "age_seconds", None) if quote is not None else None
        return bool(quote is not None and getattr(quote, "is_realtime", False)
                    and isinstance(age, (int, float)) and 0 <= age <= STALE_AFTER)

    unmeasured = [symbol for symbol in wanted if not is_fresh(symbol)]
    live_quotes = {symbol: measured[symbol] for symbol in wanted if is_fresh(symbol)}

    # B2: ``is_fresh`` above deliberately keeps the 120 s state-judgement window
    # (STALE_AFTER); live_fill refuses a fill past 90 s. Expose both thresholds
    # on this answer so a quote that is "fresh here / STALE_QUOTE there" reads
    # as an audited layering, not a silent contradiction. Nothing above changed:
    # the boundary is measurable but not tradable, by design.
    boundary = quote_freshness(STALE_AFTER)
    state_basis = (f"{boundary['basis']}；本层按 measurable="
                   f"{boundary['measurable']}（状态判定窗口），成交侧按 tradable="
                   f"{boundary['tradable']}（{boundary['max_quote_age']:.0f}s）")

    findings: list[Finding] = []
    findings.extend(_stops(desk, day, live_quotes))
    findings.extend(_moves(desk, day, live_quotes))
    findings.extend(_pending(desk, live_quotes))
    if include_tape:
        findings.extend(_tape(desk, day))

    if unmeasured:
        detail = (f"{', '.join(unmeasured)} 没有实时报价，止损距离按收盘价估算"
                  f"{('：' + quote_error) if quote_error else ''}")
        findings.append(Finding(
            "quotes_unavailable", "normal", detail,
            numbers={"symbols": unmeasured},
        ))

    order = {"critical": 0, "high": 1, "normal": 2}
    findings.sort(key=lambda f: order.get(f.severity, 3))
    return {
        "ok": True,
        "as_of": day,
        "at": now.isoformat(timespec="seconds"),
        "session": session,
        "triggered": bool(findings),
        "verdict": "wake" if findings else "skip",
        "findings": [f.as_dict() for f in findings],
        "counts": {sev: sum(1 for f in findings if f.severity == sev)
                   for sev in ("critical", "high", "normal")},
        "checked": {"positions": len(held), "pending": len(pending),
                    "quotes": len(measured), "live_quotes": len(live_quotes),
                    "tape": include_tape,
                    "quotes_basis": "frame" if frame is not None else "request"},
        "freshness_basis": state_basis,
        "threshold_seconds": boundary["stale_after"],
        "note": ("这是叫醒理由，不是交易理由。要不要动、动多少，仍由你判断。"
                 if findings else "没有越过阈值的事实。可以不唤醒模型。"),
    }
