"""WorkBuddy 短线 α 的复评冻结与模拟票据关联。

本模块不创建订单：入场和退出仍由 WorkBuddy 显式走 decisions.propose/approve。
这里只把不可变复评、固定请求号、模拟票据和当前持仓放在同一事实视图里。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from service import settings
from service.adapters.jsonio import write_json_atomic
from service.kernel.clock import SHANGHAI

from .short_alpha_tracking import _path as judgment_path
from .short_alpha_tracking import _read as read_judgment
from .short_alpha_tracking import _records as judgment_records

VERSION = "short-alpha-execution-v1"
DECISIONS = frozenset({"hold", "exit_next_open", "exit_now"})
REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


class ExecutionConflict(RuntimeError):
    """An immutable review or its signal binding conflicts with an existing record."""


def _root(root: str | Path | None) -> Path:
    return Path(root) if root is not None else Path(settings.ROOT)


def _day(value: object) -> str:
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        raise ValueError("day must be YYYYMMDD")
    datetime.strptime(text, "%Y%m%d")
    return text


def _now(now: datetime | None = None) -> datetime:
    return now.astimezone(SHANGHAI) if now is not None else datetime.now(SHANGHAI)


def _digest(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _review_path(root: str | Path | None, review_id: str) -> Path:
    # Request ids deliberately allow ':' for readable MCP handles. Hash the id for
    # a Windows-safe filename instead of substituting characters and losing identity.
    safe = hashlib.sha256(review_id.encode("utf-8")).hexdigest()[:24]
    return _root(root) / "data" / "short_alpha_reviews" / f"{safe}.json"


def _verify_review(record: dict) -> dict:
    frozen = record.get("frozen")
    if not isinstance(frozen, dict) or _digest(frozen) != record.get("frozen_sha256"):
        raise ExecutionConflict("frozen short-alpha review hash mismatch")
    return record


def _reviews(root: str | Path | None, signal_day: str | None = None) -> list[dict]:
    directory = _root(root) / "data" / "short_alpha_reviews"
    if not directory.exists():
        return []
    rows = [_verify_review(json.loads(path.read_text(encoding="utf-8")))
            for path in directory.glob("*.json")]
    if signal_day is not None:
        rows = [row for row in rows if row.get("signal_day") == signal_day]
    return sorted(rows, key=lambda row: str(row.get("captured_at") or ""), reverse=True)


def _validate_review(review: dict, signal: dict) -> None:
    items = review.get("items") or []
    if not items or len(items) > 5:
        raise ValueError("a short-alpha review needs 1..5 items")
    weighted = {str(item.get("symbol")) for item in signal["frozen"].get("items") or []
                if item.get("target_weight") is not None}
    if not weighted:
        raise ValueError("this signal has no frozen target_weight; legacy tracking stays order-free")
    seen: set[str] = set()
    for item in items:
        symbol = str(item.get("symbol") or "")
        if symbol in seen:
            raise ValueError("the same symbol cannot be reviewed twice in one request")
        seen.add(symbol)
        if symbol not in weighted:
            raise ValueError(f"{symbol} is not a weighted item in the frozen signal")
        if item.get("decision") not in DECISIONS:
            raise ValueError("review decision must be hold, exit_next_open or exit_now")
        if len(str(item.get("reason") or "").strip()) < 20:
            raise ValueError("each review needs a reason of at least 20 characters")
        evidence = item.get("evidence") or []
        if not evidence or len(evidence) > 8 or any(not str(ref).strip() for ref in evidence):
            raise ValueError("each review needs 1..8 non-empty evidence ids")


def record_review(desk, review: dict, *, root: str | Path | None = None,
                  now: datetime | None = None) -> dict:
    """Freeze one WorkBuddy review; no proposal, approval, order or ledger write."""
    desired = copy.deepcopy(review)
    review_id = str(desired.get("client_request_id") or "")
    if not REQUEST_ID.fullmatch(review_id):
        raise ValueError("client_request_id must be 8..64 safe characters")
    signal_day = _day(desired.get("day"))
    desired["day"] = signal_day
    path = _review_path(root, review_id)
    if path.exists():
        existing = _verify_review(json.loads(path.read_text(encoding="utf-8")))
        if existing.get("request") != desired:
            raise ExecutionConflict("the same review request id already froze different content")
        return {"ok": True, "created": False, "record": existing,
                "order_created": False, "paper_ledger_modified": False}
    signal_file = judgment_path(root, signal_day)
    if not signal_file.exists():
        raise ValueError(f"signal day {signal_day} has no frozen WorkBuddy judgment")
    signal = read_judgment(signal_file)
    if signal["frozen"].get("capture_kind") != "prospective":
        raise ValueError("replay judgments cannot enter paper execution reviews")
    _validate_review(desired, signal)
    stamp = _now(now).isoformat(timespec="seconds")
    frozen = {
        "version": VERSION,
        "review_id": review_id,
        "signal_day": signal_day,
        "judgment_sha256": signal["frozen_sha256"],
        "captured_at": stamp,
        "judgment_owner": "WorkBuddy",
        "request": desired,
    }
    record = {
        "version": VERSION,
        "review_id": review_id,
        "signal_day": signal_day,
        "captured_at": stamp,
        "request": desired,
        "frozen": frozen,
        "frozen_sha256": _digest(frozen),
    }
    write_json_atomic(path, record)
    return {"ok": True, "created": True, "record": record,
            "order_created": False, "paper_ledger_modified": False}


def _ticket_view(ticket: dict | None) -> dict | None:
    if ticket is None:
        return None
    keys = ("ticket_id", "client_request_id", "status", "symbol", "side", "strategy",
            "trade_date", "qty", "target_weight", "limit_price", "fill_basis",
            "created_at", "thesis", "invalidation", "reasons")
    return {key: ticket[key] for key in keys if key in ticket}


def _calendar_day(desk, signal_day: str, offset: int) -> str | None:
    try:
        return desk.market.calendar.shift(signal_day, offset)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def execution_status(desk, *, root: str | Path | None = None, day: str | None = None,
                     limit: int = 12) -> dict:
    """Read-only join of frozen intents, reviews, paper tickets and current positions."""
    wanted = _day(day) if day is not None else None
    records = [row for row in judgment_records(root)
               if row["frozen"].get("capture_kind") == "prospective"
               and (wanted is None or row["signal_day"] == wanted)]
    records.sort(key=lambda row: row["signal_day"], reverse=True)
    records = records[:max(0, int(limit))]
    tickets = desk.paper.list_tickets(limit=500)
    alpha_tickets = [ticket for ticket in tickets
                     if str(ticket.get("strategy") or "") == "short_alpha"
                     or str(ticket.get("client_request_id") or "").startswith("short-alpha:")]
    positions = {str(row.get("symbol")): row for row in desk.book.summary().get("positions", [])}
    days = []
    state_counts: dict[str, int] = {}
    for signal in records:
        signal_day = signal["signal_day"]
        reviews = _reviews(root, signal_day)
        review_by_symbol: dict[str, dict] = {}
        for review in reviews:
            for item in review["frozen"]["request"].get("items") or []:
                review_by_symbol.setdefault(str(item["symbol"]), {
                    "review_id": review["review_id"], "captured_at": review["captured_at"],
                    "frozen_sha256": review["frozen_sha256"], **item,
                })
        items = []
        for frozen_item in signal["frozen"].get("items") or []:
            symbol = frozen_item["symbol"]
            weight = frozen_item.get("target_weight")
            entry_id = f"short-alpha:{signal_day}:entry:{symbol}"
            exit_id = f"short-alpha:{signal_day}:exit:{symbol}"
            entry = next((t for t in alpha_tickets if t.get("client_request_id") == entry_id), None)
            exit_ticket = next((t for t in alpha_tickets if t.get("client_request_id") == exit_id), None)
            position = positions.get(symbol) if entry and entry.get("status") == "filled" else None
            if weight is None:
                state = "tracking_only"
            elif exit_ticket and exit_ticket.get("status") == "filled":
                state = "closed"
            elif position and int(position.get("qty") or 0) > 0:
                state = "open"
            elif entry is not None:
                state = str(entry.get("status") or "unknown")
            else:
                state = "awaiting_entry"
            state_counts[state] = state_counts.get(state, 0) + 1
            items.append({
                "item_id": frozen_item["item_id"], "symbol": symbol,
                "name": frozen_item.get("name") or "", "role": frozen_item["role"],
                "target_weight": weight,
                "thesis": frozen_item.get("thesis") or "",
                "evidence": list(frozen_item.get("evidence") or []),
                "invalidation": frozen_item.get("invalidation") or "",
                "entry_window_day": _calendar_day(desk, signal_day, 1),
                "exit_deadline_day": _calendar_day(desk, signal_day, 2),
                "state": state,
                "entry_ticket": _ticket_view(entry),
                "position": position,
                "latest_review": review_by_symbol.get(symbol),
                "exit_ticket": _ticket_view(exit_ticket),
            })
        days.append({
            "signal_day": signal_day,
            "judgment_sha256": signal["frozen_sha256"],
            "entry_window_day": _calendar_day(desk, signal_day, 1),
            "exit_deadline_day": _calendar_day(desk, signal_day, 2),
            "execution_intent": any(item.get("target_weight") is not None
                                    for item in signal["frozen"].get("items") or []),
            "items": items,
            "reviews": [{"review_id": row["review_id"], "captured_at": row["captured_at"],
                         "frozen_sha256": row["frozen_sha256"],
                         "items": row["frozen"]["request"].get("items") or []}
                        for row in reviews],
        })
    return {
        "ok": True,
        "version": VERSION,
        "as_of": _now().isoformat(timespec="seconds"),
        "account": "paper",
        "external_account_touched": False,
        "write_side_effects": False,
        "contract": {
            "judgment_owner": "WorkBuddy",
            "entry": "T+1 open window via explicit quant_propose/quant_approve",
            "review": "dynamic immutable WorkBuddy reviews; review itself never orders",
            "exit": "dynamic exit or T+2 close boundary via explicit SELL propose/approve",
            "queued_is_fill": False,
        },
        "summary": {"signal_days": len(days), "states": state_counts},
        "days": days,
    }
