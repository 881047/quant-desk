"""今天发生了什么：把三张表里的「动作」按时间合成一条线。

存在三个地方是因为它们记的不是同一种东西——决策与成交在 decisions，人格切换
在 persona_switches（它带 readiness 和预期 edge，决策行装不下），复盘与计划在
journal。要回答"今天它到底做了什么"，只能把三者合起来按时间排。

摘要在 Python 里生成，不交给前端：同一个事件在桌面窗口和 ``/dashboard.json``
里必须说同一句话，两份模板一定会漂移。
"""

from __future__ import annotations

from datetime import datetime

from service import settings

MAX_READ_LIMIT = settings.integer("timeline.max_read_limit")

# kind -> (中文标签, 语气). 语气决定颜色，只有五个值，前端按类名映射。
KIND_LABEL: dict[str, tuple[str, str]] = {
    "propose": ("提案", "act"),
    "fill": ("成交", "good"),
    "proposal_blocked": ("被拒", "bad"),
    "memo": ("备忘录", "note"),
    "hypothesis": ("假设", "note"),
    "persona_switch": ("换人格", "persona"),
    "persona_refused": ("换人格被拒", "bad"),
    "journal": ("记录", "note"),
    "review": ("复盘", "note"),
    "plan": ("计划", "note"),
    "note": ("笔记", "note"),
    "playbook": ("规则变更", "note"),
}


def belongs_to(ts: str, day: str) -> bool:
    """Does this act belong on the page for ``day``?

    ``day`` is the latest bar date, which is *yesterday* for most of the trading
    day: today's daily data only lands at 17:30. Filtering on the calendar date
    therefore hid every act of the current session — at 12:30 the page said
    "今天没有任何记录" to a desk that had spent the morning proposing. The page is
    a live view, so acts after the reported session are shown too, up to three
    days ahead (a weekend plus a holiday) and no further, so a broken pipeline
    cannot silently accumulate a month of history here.
    """
    date = str(ts)[:10].replace("-", "")
    if date == day:
        return True
    if date < day:
        return False
    try:
        gap = (datetime.strptime(date, "%Y%m%d") - datetime.strptime(day, "%Y%m%d")).days
    except ValueError:
        return False
    return 0 < gap <= 3


def _text(value, limit: int = 0) -> str:
    out = " ".join(str(value or "").split())
    return out[:limit] if limit else out


def describe(kind: str, payload: dict) -> dict:
    """One event → ``{title, detail}``, both markup-free.

    ``title`` is the fact, ``detail`` is the reason. The two are kept apart so a
    narrow window can drop the detail instead of truncating the fact.
    """
    payload = payload or {}
    if kind == "propose":
        req = payload.get("request") or {}
        ticket = payload.get("ticket") or {}
        title = " ".join(str(x) for x in (
            req.get("symbol"), req.get("side"), req.get("sleeve"),
            req.get("persona")) if x)
        size = ""
        if ticket:
            size = (f"{ticket.get('qty')} 股 @ {ticket.get('limit_price')}"
                    f"，止损 {ticket.get('stop_price')}")
        thesis = _text(req.get("thesis"), 120)
        return {"title": title, "detail": "；".join(x for x in (size, thesis) if x)}
    if kind == "fill":
        title = (f"{payload.get('symbol')} {payload.get('side')} "
                 f"{payload.get('qty')} 股 @ {payload.get('price')}")
        fees = payload.get("fees")
        return {"title": title,
                "detail": f"费用 {float(fees):,.2f}" if fees not in (None, "") else ""}
    if kind in ("proposal_blocked", "persona_refused"):
        return {"title": _text(payload.get("code")),
                "detail": _text(payload.get("message"), 160)}
    if kind == "persona_switch":
        title = f"{payload.get('from_key')} → {payload.get('to_key')}"
        edge = payload.get("expected_edge_bps")
        head = f"预期 {edge} bps" if edge not in (None, "") else ""
        reason = _text(payload.get("reason"), 130)
        return {"title": title, "detail": "；".join(x for x in (head, reason) if x)}
    if kind == "memo":
        return {"title": f"#{payload.get('id')} {_text(payload.get('topic'), 90)}",
                "detail": ""}
    if kind == "hypothesis":
        text = payload.get("statement") or payload.get("text")
        if not text:
            # Older rows are a metrics dict, not a sentence. Flattening it beats
            # rendering Python's repr with its quotes and braces.
            parts = [f"{key}={value}" for key, value in payload.items()
                     if not isinstance(value, (dict, list))]
            text = " · ".join(parts[:5])
        return {"title": "", "detail": _text(text, 160)}
    if kind in ("journal", "review", "plan", "note", "playbook"):
        return {"title": "", "detail": _text(payload.get("text"), 180)}
    return {"title": "", "detail": _text(payload, 160)}


def _read_all(read, initial_limit: int) -> list[dict]:
    """Read through the store's real history instead of truncating at a page cap.

    The store exposes newest-first limits but no offset or date predicate. Grow
    the limit until a short page proves the table is exhausted, then let the
    caller apply its actual session range before trimming the rendered output.
    """
    read_limit = initial_limit
    while read_limit < MAX_READ_LIMIT:
        rows = read(limit=read_limit)
        if len(rows) < read_limit:
            return rows
        read_limit *= 2
    # Bounded on purpose. A store that ignores ``limit`` never returns a short
    # page, and the loop above would then double forever — a page render that
    # hangs instead of showing the acts it has.
    return read(limit=MAX_READ_LIMIT)


def collect(desk, day: str, limit: int = 60) -> list[dict]:
    """Every recorded act for one session, from three tables that all record acts."""
    prefix = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    events: list[dict] = []

    for row in _read_all(desk.paper.list_decisions, 400):
        ts = str(row.get("ts") or "")
        if not belongs_to(ts, day):
            continue
        events.append({"ts": ts, "kind": str(row.get("kind")),
                       "payload": row.get("payload") or {}})

    for row in _read_all(desk.paper.persona_switches, 100):
        if str(row.get("day")) != day:
            continue
        events.append({"ts": str(row.get("ts") or prefix), "kind": "persona_switch",
                       "payload": row})

    for row in _read_all(desk.paper.search_journal, 100):
        ts = str(row.get("ts") or "")
        if not belongs_to(ts, day):
            continue
        # Journal rows carry their own kind (review / plan / note); keeping it
        # lets the timeline distinguish "wrote a plan" from "wrote a review".
        events.append({"ts": ts, "kind": str(row.get("kind") or "journal"),
                       "payload": row})

    events.sort(key=lambda e: e["ts"])
    out = []
    for event in events[-limit:]:
        label, tone = KIND_LABEL.get(event["kind"], (event["kind"], "note"))
        described = describe(event["kind"], event["payload"])
        out.append({"ts": event["ts"], "kind": event["kind"], "label": label,
                    "tone": tone, **described})
    return out
