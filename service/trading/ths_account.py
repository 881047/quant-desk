"""Account-level numbers from the running trading client — read, never touched.

The holdings grid of 网上股票交易系统5.0 is a `CVirtualGridCtrl`: UIA sees one
container, MSAA reports accChildCount = 0. Its cells cannot be read, and the
workaround (send Ctrl+A / Ctrl+C) is not worth it — it has to steal focus, and
when the focus check loses a race the keys land in whatever application happens
to be in front.

The 资金 panel is different: it is a set of plain `Static` controls, and
GetWindowTextW reads them without focus, without keystrokes and without touching
the window. That is enough for实时 monitoring, because the numbers that move
during a session are 市值 and 盈亏, not the share counts.

Two overlapping panels render into the same HWND tree (A股 and 港股), so every
label appears twice with different values. Rather than guessing which is which,
this module solves the identity that must hold:

    总资产 ≈ 可用金额 + 股票市值

and returns nothing when no combination satisfies it. A wrong account total is
worse than a missing one: it silently rescales every percentage downstream.
"""

from __future__ import annotations

import ctypes
import math
from ctypes import wintypes
from typing import Any

from service import settings

TITLE_HINT = "网上股票交易"
IDENTITY_TOLERANCE = settings.num("ths_account.identity_tolerance")   # of 总资产

LABEL_TOTAL = ("总 资 产", "总资产")
LABEL_CASH = ("可用金额", "可用资金")
LABEL_MARKET = ("股票市值", "证券市值", "参考市值")


def _user32():
    return ctypes.windll.user32


def _class_of(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(wintypes.HWND(hwnd), buffer, 256)
    return buffer.value


def _text_of(hwnd) -> str:
    length = _user32().GetWindowTextLengthW(wintypes.HWND(hwnd))
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32().GetWindowTextW(wintypes.HWND(hwnd), buffer, length + 1)
    return buffer.value


def find_windows() -> list[int]:
    """Every top-level window of the trading client, in enumeration order."""
    user32 = _user32()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[int] = []

    def visit(hwnd, _param):
        if _class_of(hwnd).startswith("Afx:") and TITLE_HINT in _text_of(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return found


def _statics(top: int) -> list[tuple[str, int, int]]:
    """(text, left, top) for every labelled Static inside the window."""
    user32 = _user32()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    items: list[tuple[str, int, int]] = []

    def walk(hwnd, depth: int = 0):
        if depth > 6:
            return

        def visit(child, _param):
            if _class_of(child) == "Static":
                text = _text_of(child).strip()
                if text:
                    rect = wintypes.RECT()
                    user32.GetWindowRect(wintypes.HWND(child), ctypes.byref(rect))
                    items.append((text, rect.left, rect.top))
            walk(child, depth + 1)
            return True

        user32.EnumChildWindows(wintypes.HWND(hwnd), callback_type(visit), 0)

    walk(top)
    return items


def _candidates(items: list[tuple[str, int, int]], labels: tuple[str, ...]) -> list[float]:
    """Values that sit immediately to the right of a label on the same row."""
    out = []
    for text, left, top in items:
        if text not in labels:
            continue
        best = None
        for other, other_left, other_top in items:
            if abs(other_top - top) > 3 or not 0 < other_left - left <= 200:
                continue
            if other in labels or not any(ch.isdigit() for ch in other):
                continue
            if best is None or other_left < best[0]:
                best = (other_left, other)
        if best:
            try:
                value = float(best[1].replace(",", ""))
            except ValueError:
                continue
            if math.isfinite(value):
                out.append(value)
    return sorted(set(out))


def disambiguate(items: list[tuple[str, int, int]]) -> tuple[dict[str, Any] | None, str]:
    """Pick the A股 totals out of the overlapping panels, using the identity.

    Pure function of the label/value positions, so it can be tested without a
    running client — which matters, because the layout is the part most likely
    to change after a client update.
    """
    totals = _candidates(items, LABEL_TOTAL)
    cash = _candidates(items, LABEL_CASH)
    market = _candidates(items, LABEL_MARKET)
    if not (totals and cash and market):
        return None, (f"缺字段：总资产{len(totals)} 可用{len(cash)} 市值{len(market)}")

    best = None
    for total in totals:
        for one_cash in cash:
            for one_market in market:
                residual = abs(one_cash + one_market - total)
                if best is None or residual < best[0]:
                    best = (residual, total, one_cash, one_market)

    residual, total, one_cash, one_market = best
    if total <= 0:
        return None, f"总资产为 {total}"
    if residual > max(total * IDENTITY_TOLERANCE, 100):
        return None, (f"恒等式不成立：可用 {one_cash:,.2f} + 市值 {one_market:,.2f} "
                      f"≠ 总资产 {total:,.2f}（差 {residual:,.2f}）")
    return {"total": round(total, 2), "cash": round(one_cash, 2),
            "market_value": round(one_market, 2), "residual": round(residual, 2),
            "cash_ratio": round(one_cash / total, 4) if total else None}, ""


def read_account() -> dict[str, Any]:
    """The A股 totals, or a reason why they could not be disambiguated."""
    windows = find_windows()
    if not windows:
        return {"ok": False, "reason": "没找到「网上股票交易系统5.0」窗口（客户端没开或停在登录框）"}

    attempts: list[str] = []
    for top in windows:
        items = _statics(top)
        if not items:
            continue
        solved, reason = disambiguate(items)
        if solved is None:
            attempts.append(f"hwnd={top} {reason}")
            continue
        required = ("total", "cash", "market_value", "residual", "cash_ratio")
        missing = [name for name in required if name not in solved or solved[name] is None]
        if missing:
            attempts.append(f"hwnd={top} 缺输出字段：{','.join(missing)}")
            continue
        try:
            finite = all(math.isfinite(float(solved[name])) for name in required)
        except (TypeError, ValueError):
            finite = False
        if not finite:
            attempts.append(f"hwnd={top} 输出字段不是有限数字")
            continue
        return {"ok": True, **solved, "source": "screen:ths", "hwnd": top,
                "note": "只读资金面板；持仓明细读不到，用行情×持仓另算"}

    return {"ok": False, "reason": "；".join(attempts) or "窗口里没有可用的资金面板"}


if __name__ == "__main__":
    import json

    print(json.dumps(read_account(), ensure_ascii=False, indent=1))
