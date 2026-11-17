"""The one order this desk may place while the model is asleep.

Why this is not the ``enforce()`` that ``stops.py`` removed
----------------------------------------------------------
``stops.py`` opens with "stop *state*, never stop *orders*", and it is right
about what it deleted: an ``enforce()`` that opened its own ticket, approved it,
and wrote **its own thesis** ("triggered by stops.enforce, independent of model
judgement"). A rule that decides the breach matters *and* writes the reason *and*
pulls the trigger has three jobs, and the measurement becomes the pretext.

The stop price is not that. It is a number the **model wrote when it opened the
position** — ``ticket.stop_price``, with the thesis that came with it — and the
question here is only "is that sentence true yet". This module does not pick a
side, a size, a symbol, or a reason. What the record shows afterwards is two
authors: the model decided at 09:26 that 14.20 was where its own thesis would be
wrong, and the loop executed that decision at 10:16 because nobody else was awake.

Why the confirmation is measured in seconds
-------------------------------------------
A stop usually sits on a round number, which is where the tape flutters. Prices
cross 14.20 and come back all morning; selling the first frame that prints below
it would turn a stop into a noise detector, which is worse than the four-hour
wait it replaces. So the finding must have been open for
``autostop.confirm_seconds`` — read from ``first_at`` in ``_watch.json``, which
``alert_log`` already keeps — before anything is sold.

Seconds, not frames, because the capture cadence changes four times a day: four
frames is two minutes in the 30-second windows and one frame in the 3-minute
ones. One number must mean one thing.

Only *trading-session* seconds count. A break across the lunch hour or a weekend
has not been open for that wall-clock time: 11:29 -> 13:00 is one minute of
exposure, not ninety-one. Gaps contribute nothing and never reset what has
already accumulated, so a breach still needs a full ``confirm_seconds`` of
session time before it is real.

Who prices the order
--------------------
Not this module. The frame decided the breach, and its price is what the alert
says; the order goes through ``Decisions.propose``/``approve`` exactly like the
model's own sell, so the guard, T+1, the price bands, the fees and the live-fill
staleness check all apply unchanged. That also means one extra vendor call per
*event* — not per frame — which is the price of trading at a price that exists.
The judgement never fetches; the execution does, once.

One consequence worth naming: if no fillable quote exists at that moment (the last
minutes before 15:00, or a vendor outage), `FILL_MODE=intraday` queues the order
for the next open, and it settles there even if the price recovers overnight. That
is the book's existing fallback, not a new behaviour — but it means a stop broken
at 14:58 is a decision about tomorrow's open, and the ticket says `queued` so the
next reader can see that.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from service import settings
from service.kernel.clock import SHANGHAI
from service.notify import alert_log

from .stops import stop_map


def actor_for(ticket_id: str) -> str:
    """代下的单在决策日志里的身份，指回建仓那张 ticket。

    读者要能一眼看出"这是 09:26 那笔的止损被兑现了"，而不是凭空多出来一笔卖出。
    """
    return f"stop:{ticket_id}"


def _age_seconds(stamp: str | None, now: datetime, calendar=None) -> float | None:
    """Trading-session seconds between a finding's ``first_at`` and ``now``.

    A plain wall-clock difference counts the 90-minute lunch break and whole
    weekends as "still broken", so a one-frame dip at 11:29 would look
    confirmed at 13:00. Only moments inside a session on a trading day count;
    lunch, after-hours and non-trading days contribute nothing and never clear
    what has already accumulated. 10:30 -> 10:33 is 180 s, 11:29 -> 13:00 is
    60 s. Pure function: no I/O, no new state.
    """
    if not stamp:
        return None
    try:
        start = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    # Delayed: realtime pulls in pandas. SESSIONS is the single source for the
    # two daily windows, the same one tripwire/session_state read.
    from .realtime import SESSIONS

    def wall(moment: datetime) -> datetime:
        # Session boundaries are naive Shanghai wall-clock times; normalise
        # both ends to that frame so aware and naive stamps compare alike.
        if moment.tzinfo is not None:
            return moment.astimezone(SHANGHAI).replace(tzinfo=None)
        return moment

    lo, hi = wall(start), wall(now)
    if hi <= lo:
        return 0.0
    total = 0.0
    day = lo.date()
    while day <= hi.date():
        trading = day.weekday() < 5
        if trading and calendar is not None:
            try:
                trading = bool(calendar.is_trading_day(day.strftime("%Y%m%d")))
            except TypeError:
                trading = bool(calendar.is_trading_day(day))
        if trading:
            for begin, finish in SESSIONS:
                sess_lo = datetime.combine(day, begin)
                sess_hi = datetime.combine(day, finish)
                a, b = max(lo, sess_lo), min(hi, sess_hi)
                if b > a:
                    total += (b - a).total_seconds()
        day += timedelta(days=1)
    return total


def run(
    desk,
    *,
    day: str,
    scope: str,
    now: datetime | None = None,
    base=None,
    findings=None,
) -> list[dict]:
    """Sell what has been below its stop long enough to be real.

    Returns one row per symbol this frame *acted on* (or refused). An empty list
    is the normal case: no breach, not confirmed yet, or the feature is off.
    ``findings`` is the current frame's tripwire output: a stale open mark is
    not enough, the breach must still be present in this frame. ``None`` keeps
    the old behaviour for callers that have no frame to hand us.
    """
    if not settings.flag("autostop.enabled"):
        return []
    now = now or datetime.now(SHANGHAI)
    # Read per run, not at import: a config change mid-session takes effect.
    confirm_seconds = settings.num("autostop.confirm_seconds")
    calendar = getattr(getattr(desk, "market", None), "calendar", None)
    # Second confirmation from the frame itself. ``reconcile`` keeps a finding
    # open through the grace window after it disappears, so without this a
    # price that has recovered would still be sold on a stale ``first_at``.
    breached_now = {
        f.get("subject") for f in (findings or [])
        if f.get("code") == "stop_breach"
    }
    marks = alert_log.open_marks(day, base, scope=scope)
    ready = {
        row.get("subject"): row
        for row in marks.values()
        if row.get("code") == "stop_breach" and row.get("subject")
        and (findings is None or row.get("subject") in breached_now)
        and (_age_seconds(row.get("first_at"), now, calendar) or 0.0) >= confirm_seconds
    }
    if not ready:
        return []

    positions = {row["symbol"]: row for row in desk.book.summary()["positions"]}
    stops = stop_map(desk, sorted(ready))

    acted: list[dict] = []
    for symbol, mark in ready.items():
        position = positions.get(symbol)
        if position is None:
            # 破了线但仓位已经不在了：刚被卖掉（多半就是上一帧的自己），或者
            # 今早平掉了。这不是一次失败，是"没什么可做的"，不占返回值的位。
            continue
        ticket = stops.get(symbol)
        if ticket is None:
            # 有仓位、破了线、却找不到那张带止损的票：仓是这个台子建的，
            # 这条不该发生，说出来。
            acted.append({"symbol": symbol, "status": "no_stop", "qty": 0,
                          "ticket_id": None,
                          "reasons": ["持有但没有带止损的建仓票"]})
            continue
        available = int(position.get("available_qty") or 0)
        if available <= 0:
            acted.append({"symbol": symbol, "status": "blocked", "qty": 0,
                          "ticket_id": ticket.get("ticket_id"),
                          "reasons": ["available=0（T+1 冻结或全部挂单中）"]})
            continue
        acted.append(_sell(desk, symbol, ticket, available, mark, day=day,
                           confirm_seconds=confirm_seconds))
    return acted


def _sell(desk, symbol: str, ticket: dict, qty: int, mark: dict, *, day: str,
          confirm_seconds: float) -> dict:
    """One sell, through the decision path — guard, T+1, bands and fees included."""
    entry = str(ticket.get("ticket_id") or "")
    stop = Decimal(str(ticket.get("stop_price_final") or ticket.get("stop_price")))
    first_at = str(mark.get("first_at") or "")
    seen = first_at[11:19] or "?"
    req = {
        "symbol": symbol,
        "side": "SELL",
        "qty": qty,
        "sleeve": str(ticket.get("sleeve") or "CORE"),
        # 理由不是这里编的：引用建仓时那张 ticket 上写下的止损与它自己的编号。
        # 这个台子允许代码执行一句话，不允许代码写一句话。
        "thesis": (f"止损 {stop}：{entry} 建仓时写下的线，{seen} 起持续跌在它下面，"
                   "无人值守代执行。"),
        "invalidation": ("成交前若价格回到止损上方并保持，破线会被写成 cleared；"
                         "那时单已经发出，不撤回。"),
        "evidence": [f"stop_breach first_at={first_at}",
                     f"confirm={confirm_seconds:g}s",
                     f"source={ticket.get('ticket_id')}"],
        # 一张建仓票、一个交易日只应有一笔自动止损：重试拿回同一张单。数量不进
        # 键——available_qty 会随 T+1 解冻/部分成交变化，含 qty 会让同一个
        # 意图生成第二个句柄，再卖一次。day 保留：跨日再破线是新的一笔。
        "client_request_id": f"autostop-{day}-{symbol}-{entry}",
    }
    try:
        order = desk.decisions.propose(req, actor="autostop")
        result = desk.decisions.approve(order["ticket_id"], actor=actor_for(entry))
    except Exception as exc:  # noqa: BLE001 - 代下单失败不该弄死抓取循环
        return {"symbol": symbol, "status": "error", "qty": qty, "ticket_id": entry,
                "reasons": [f"{type(exc).__name__}: {exc}"]}
    return {
        "symbol": symbol,
        "status": str(result.get("status") or "unknown"),
        "qty": qty,
        "ticket_id": entry,
        "order": order.get("ticket_id"),
        "reasons": [str(r) for r in (result.get("reasons") or [])][:3],
    }
