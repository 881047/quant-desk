"""Does the book still describe the account?

The holdings detail is refreshed by hand — an export, or a paste — because the
client's grid cannot be read. That is acceptable: share counts only change when
you trade. But it has one silent failure mode. Forget to update after a trade and
every number downstream is computed on shares you no longer hold, with no symptom
at all: the P&L looks plausible, the stop sits at a price that belongs to a
different position, and the alert that should have fired does not.

The 资金 panel gives the total live and read-only (`ths_account`). Comparing it
against holdings × live prices turns "you forgot" into a sentence.

This is a measurement, not a verdict: it says the two disagree, not which one is
right. Nothing here writes to the ledger, and nothing here can trigger a trade.
"""

from __future__ import annotations

import math
from typing import Any

from service import settings

TOLERANCE_PCT = settings.num("account_check.tolerance_pct")   # of the panel's value...
MIN_ABS = settings.num("account_check.min_abs")               # ...but never less than this
DRIFT_FLOOR = settings.num("account_check.drift_floor")       # below this, drift is unmeasurable


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _invalid(panel_value: float | None, book_value: float | None,
             reason: str) -> dict[str, Any]:
    return {"ok": False, "verdict": "invalid_input", "reason": reason,
            "panel_value": panel_value, "book_value": book_value}


def compare(*, panel: dict[str, Any], book_value: float, positions: int,
            quotes_fresh: bool) -> dict[str, Any]:
    """Compare the client's market value with ours. Never raises."""
    book = _number(book_value)
    panel = panel if isinstance(panel, dict) else {}
    panel_number = _number(panel.get("market_value"))
    if book is None:
        return _invalid(panel_number, None, "账本市值不是有限数值")
    book = round(book, 2)
    if not panel or not panel.get("ok"):
        return {"ok": False, "verdict": "unavailable",
                "reason": str(panel.get("reason") or "账户面板读不到"),
                "panel_value": None, "book_value": book}

    if not quotes_fresh:
        return {"ok": False, "verdict": "stale_quotes",
                "reason": "行情不是实时的，比较会变成两个时点相减",
                "panel_value": panel_number,
                "book_value": book}

    panel_value = _number(panel.get("market_value") or 0.0)
    if panel_value is None:
        return _invalid(None, book, "账户面板市值不是有限数值")
    diff = panel_value - book
    if not math.isfinite(diff):
        return _invalid(panel_value, book, "市值差不是有限数值")
    tolerance = max(MIN_ABS, abs(panel_value) * TOLERANCE_PCT)
    diff_pct = (diff / panel_value) if panel_value else None
    if diff_pct is not None and not math.isfinite(diff_pct):
        return _invalid(panel_value, book, "市值差比例不是有限数值")

    if abs(diff) <= tolerance:
        verdict = "match"
    elif diff > 0:
        verdict = "book_missing"      # 面板比账本多：有买入没记进来
    else:
        verdict = "book_extra"        # 账本比面板多：有卖出没记进来

    if verdict == "match":
        message = (f"与账户一致：市值 {panel_value:,.2f}，"
                   f"按持仓算是 {book:,.2f}（差 {diff:+,.2f}）")
    elif verdict == "book_missing":
        message = (f"账户市值 {panel_value:,.2f}，按持仓算只有 {book:,.2f}，"
                   f"差 {diff:+,.2f}"
                   + (f"（{diff_pct * 100:+.1f}%）" if diff_pct is not None else "")
                   + "：可能有买入（或送股）没有记进账本")
    else:
        message = (f"按持仓算 {book:,.2f}，账户只有 {panel_value:,.2f}，"
                   f"差 {diff:+,.2f}"
                   + (f"（{diff_pct * 100:+.1f}%）" if diff_pct is not None else "")
                   + "：可能有卖出（或转股/非交易过户）没有记进账本")

    return {"ok": True, "verdict": verdict, "panel_value": round(panel_value, 2),
            "book_value": book, "diff": round(diff, 2),
            "diff_pct": round(diff_pct, 4) if diff_pct is not None else None,
            "tolerance": round(tolerance, 2), "positions": positions,
            "message": message}


def is_empty_book_drift(result: dict[str, Any]) -> bool:
    """The book has no holdings at all, but the account is worth something."""
    if not result.get("ok") or result.get("verdict") != "book_missing":
        return False
    if result.get("positions"):
        return False
    panel_value = _number(result.get("panel_value") or 0.0)
    return panel_value is not None and panel_value > DRIFT_FLOOR
