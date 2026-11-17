"""The desk as data — one payload, every reader.

There used to be an HTML page here as well. It is gone: the desktop window
(``scripts/panel.pyw``) is the one human interface, and a second front end that
renders the same numbers in a browser tab is a thing that drifts. The first time
the two disagreed, nobody could tell which one was right.

What stays is the assembly: ``/dashboard.json`` for a model that wants to quote
today, and ``reports/daily/*.json`` as the frozen copy of an evening.

Every block degrades on its own. A page that disappears exactly when the account
is in trouble is worse than useless, and the same is true of a JSON body.
"""

from __future__ import annotations

from copy import copy
from datetime import datetime
from pathlib import Path

from service.kernel.clock import SHANGHAI
from service.trading import timeline as timeline_mod
from service.trading.timeline import KIND_LABEL  # noqa: F401 - re-exported for tools


def _belongs_to(desk, ts: str, day: str) -> bool:
    """Kept as a thin alias: this name was imported by tests and tools."""
    return timeline_mod.belongs_to(ts, day)


def collect_timeline(desk, day: str, limit: int = 60) -> list[dict]:
    """Every recorded act for one session, from three tables that all record acts."""
    return timeline_mod.collect(desk, day, limit=limit)


_SLIM_BRIEF_KEYS = ("as_of", "today", "last_trading_day", "is_trading_day",
                    "core", "aggressive", "quality", "session", "sentiment")

#: Slim mode caps the timeline, and says so.
#:
#: The MCP host drops any tool body over 16 KB **whole** — it does not truncate.
#: The timeline was the only unbounded block on the page: each entry carries the
#: note's full text (~450 B measured) and the collector asks for 60 of them, so
#: a busy afternoon would have taken the page past the ceiling — and the
#: model's most-used tool would have returned nothing at all. Measured on
#: 2026-11-02: 11.6 KB with three entries, 15.0 KB with twenty.
#:
#: So slim mode keeps the **last** few acts (the most recent ones are what a
#: reader asks the page for) and truncates their text. Both cuts are declared
#: in the body — ``events_total`` says how many there were, and the detail
#: ends with an ellipsis — because a silently shortened page reads like a
#: complete one.
_SLIM_EVENTS = 10
_SLIM_DETAIL_CHARS = 240


def _slim_events(events: object) -> tuple[object, int]:
    """Trim the timeline for a host with a hard payload ceiling."""
    if not isinstance(events, list):
        return events, 0
    total = len(events)
    kept = events[-_SLIM_EVENTS:]
    trimmed: list[dict] = []
    for item in kept:
        if not isinstance(item, dict):
            trimmed.append(item)
            continue
        detail = item.get("detail")
        if isinstance(detail, str) and len(detail) > _SLIM_DETAIL_CHARS:
            item = {**item, "detail": detail[:_SLIM_DETAIL_CHARS] + "…"}
        trimmed.append(item)
    return trimmed, total


def collect(desk, slim: bool = False) -> dict:
    """Assemble the page.

    ``slim`` is for hosts with a hard payload ceiling. The default body is
    ~24.5KB, and the MCP adapter does not truncate an oversized answer — it
    replaces the whole body with ``{"error": "output too large"}``, so a
    default-shaped call returns nothing at all. Slimming drops the two blocks
    that already have their own endpoints (``usage`` -> ``/usage``,
    ``brief.optional``/``aux``/``quotes``/``notes`` -> ``/data/brief``), trims
    the timeline to the last ten acts, and keeps every field a trading
    decision reads, including the data-readiness verdict in
    ``brief.core``/``brief.aggressive``. It sets ``slim: true`` in the
    body so a reader can tell a trimmed page from a short one, and it says
    how many acts there were in ``events_total`` so a truncated timeline does
    not read like a complete one.
    """
    day = desk.market.latest_bar_date() or datetime.now(SHANGHAI).strftime("%Y%m%d")
    # Value a private view instead of the live book: the page is a *read*, and
    # it used to be the one account surface that neither rolled over nor marked
    # prices, so `/dashboard.json` could show yesterday's T+1 lock and
    # yesterday's equity next to `/portfolio`'s today. ``read_view`` settles and
    # marks on a copy, writes nothing and cannot turn a queued ticket into a
    # fill, which is exactly the contract a read endpoint needs.
    book = desk.book.read_view(day, live=True)
    desk = copy(desk)
    desk.book = book
    summary = book.summary()
    block_errors: dict[str, str] = {}
    try:
        from service.trading.arena import scoreboard
        arena = scoreboard(desk)
    except Exception as exc:  # noqa: BLE001 - the page must render even when one block fails
        arena = {}
        block_errors["arena"] = f"{type(exc).__name__}: {exc}"
    try:
        from service.trading.duty import build as build_duty
        duty = build_duty(desk, day)
    except Exception as exc:  # noqa: BLE001 - one broken block must not blank the page
        duty = {"items": []}
        block_errors["duty"] = f"{type(exc).__name__}: {exc}"
    regime = ""
    try:
        from service.trading.regime import compute as compute_regime
        regime = compute_regime(desk.market, day).label
    except Exception as exc:  # noqa: BLE001
        regime = ""
        block_errors["regime"] = f"{type(exc).__name__}: {exc}"
    persona = None
    probe_map: dict = {}
    try:
        from service.trading import persona as persona_mod
        probe_map = persona_mod.probe(desk.market, desk.registry)
        persona = {"current": persona_mod.current(desk.paper, regime, probe_map),
                   "available": persona_mod.describe(regime, probe_map)}
    except Exception as exc:  # noqa: BLE001
        persona = None
        block_errors["persona"] = f"{type(exc).__name__}: {exc}"
    # Read it through the same function `/data/brief` uses, so the page and the
    # brief cannot disagree about the tone. They *did*: this block read
    # `{day}.json` and nothing else, so on a weekend or before the 17:30 job the
    # page showed no sentiment while the brief showed the previous session's —
    # which is exactly the disagreement the original comment was guarding against.
    # The shared version falls back to the newest file and carries `trade_date`.
    from service.api import data as data_api

    sentiment = data_api._sentiment_snapshot(day)
    brief = {}
    try:
        from service.adapters.brief import build_brief
        cal = desk.market.calendar
        # Same trap as the window: None means "no quote", which marks the
        # aggressive sleeve blocked even when the feed is answering.
        rt = (probe_map.get("rt_quote") or {}) if isinstance(probe_map, dict) else {}
        brief = build_brief(summary=desk.registry.summary(), calendar=cal, today=day,
                            rt_age_seconds=0.0 if rt.get("tradable") else None,
                            sentiment=sentiment)
    except Exception as exc:  # noqa: BLE001
        brief = {}
        block_errors["brief"] = f"{type(exc).__name__}: {exc}"
    # The real account, read-only. It is deliberately not merged into `summary`:
    # the paper book is a measurement and the real book is reality, and a page
    # that adds them together would report one number that means nothing.
    from service.trading.panel import account_panel

    # One builder, two front ends (this page and the desktop window). Two copies
    # would drift, and "the window says 200, the page says 100" is a bug nobody
    # can diagnose from a screenshot.
    #
    # Degraded per block, like the six above it: this module's contract says
    # "every block degrades on its own", and these three were the exception. A
    # locked `external.sqlite`, or one malformed timestamp in the timeline,
    # then took down the only data source the desktop window and the evening
    # report have — precisely when the ledger is already misbehaving.
    try:
        external = account_panel(desk, root=Path(__file__).resolve().parents[2])
    except Exception as exc:  # noqa: BLE001
        external = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        block_errors["external"] = external["error"]
    try:
        usage = desk.paper.usage_summary(days=1)
    except Exception as exc:  # noqa: BLE001
        usage = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        block_errors["usage"] = usage["error"]
    try:
        events = collect_timeline(desk, day)
    except Exception as exc:  # noqa: BLE001
        events = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        block_errors["events"] = events["error"]

    body = {"day": day, "summary": summary, "arena": arena, "duty": duty,
            "regime": regime, "persona": persona, "brief": brief,
            "sentiment": sentiment, "probe": probe_map, "external": external,
            "usage": usage, "events": events}
    block_values = {"arena": arena, "duty": duty, "regime": regime,
                    "persona": persona, "brief": brief, "external": external,
                    "usage": usage, "events": events}
    body["status"] = "degraded" if block_errors else "ok"
    body["block_status"] = {
        name: ("error" if name in block_errors else
               "empty" if (value is None or value == "" or value == {} or value == []
                           or (name == "duty" and isinstance(value, dict)
                               and not value.get("items"))) else "ok")
        for name, value in block_values.items()
    }
    if block_errors:
        body["errors"] = block_errors
    if slim:
        body.pop("usage", None)
        if isinstance(body.get("brief"), dict):
            body["brief"] = {k: body["brief"][k] for k in _SLIM_BRIEF_KEYS
                             if k in body["brief"]}
        events, total = _slim_events(body.get("events"))
        body["events"] = events
        body["events_total"] = total
        if isinstance(events, list) and total > len(events):
            body["events_truncated"] = True
        body["slim"] = True
    return body
