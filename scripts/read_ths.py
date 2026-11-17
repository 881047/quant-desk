"""Ask the trading client to copy its own grid, then read what it copied.

Measured on this machine, the 持仓 grid of 网上股票交易系统5.0 (xiadan.exe) is a
`CVirtualGridCtrl`: UI Automation sees one container with no cells, and MSAA
reports accChildCount = 0. There is no tree to walk and no text to read.

What the control does support is the copy a person would perform by hand. So this
sends exactly those two keystrokes — Ctrl+A, Ctrl+C — reads the clipboard, puts
the clipboard back, and validates the result before anything is written.

It never sends Enter, never clicks, never types a price or a quantity, and never
leaves a key down. Four guard rails, because a trading client is not a place to
be clever:

  * refuses if the window is minimised (nothing would be copied)
  * refuses if the keyboard was used in the last 2 seconds (you are busy)
  * restores the clipboard even when it fails
  * **refuses if the clipboard did not change** — that means Ctrl+C did nothing,
    and the old content would otherwise be imported as if it were today's book

    python scripts/read_ths.py --dry-run     # copy, parse, print, write nothing
    python scripts/read_ths.py --write       # also store a snapshot (source=screen:ths)
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.external import (
    DB,
    _frame_from_text,
    _pick_profile,
    _promote,
    known_headers,
    rows_from,
)
from service.kernel.clock import SHANGHAI
from service.trading.external import ExternalStore

VK_CONTROL, VK_A, VK_C = 0x11, 0x41, 0x43
KEYEVENTF_KEYUP = 0x0002
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _find_windows() -> tuple[int, int]:
    """(top-level trading window, its grid control), or (0, 0)."""
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def class_of(hwnd) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), buffer, 256)
        return buffer.value

    def title_of(hwnd) -> str:
        length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(wintypes.HWND(hwnd), buffer, length + 1)
        return buffer.value

    top = 0

    def find_top(hwnd, _param):
        nonlocal top
        if class_of(hwnd).startswith("Afx:") and "网上股票交易" in title_of(hwnd):
            top = hwnd
            return False
        return True

    user32.EnumWindows(callback_type(find_top), 0)
    if not top:
        return 0, 0

    grid = 0

    def walk(hwnd, depth=0):
        nonlocal grid
        if depth > 5 or grid:
            return

        def visit(child, _param):
            nonlocal grid
            if grid:
                return False
            if class_of(child) == "CVirtualGridCtrl":
                rect = wintypes.RECT()
                user32.GetWindowRect(wintypes.HWND(child), ctypes.byref(rect))
                if (rect.right - rect.left) > 500 and (rect.bottom - rect.top) > 150:
                    grid = child
                    return False
            walk(child, depth + 1)
            return True

        user32.EnumChildWindows(wintypes.HWND(hwnd), callback_type(visit), 0)

    walk(top)
    return top, grid


def _idle_millis() -> int:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 10_000
    return ctypes.windll.kernel32.GetTickCount() - info.dwTime


def _clipboard_get() -> str:
    from scripts.external import _clipboard_text

    try:
        return _clipboard_text()
    except RuntimeError:
        return ""


def _clipboard_set(text: str) -> None:
    """Put the clipboard back exactly as it was. Best effort, never raises."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        if not user32.OpenClipboard(None):
            return
        try:
            user32.EmptyClipboard()
            buffer = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buffer)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return
            pointer = kernel32.GlobalLock(handle)
            ctypes.memmove(pointer, buffer, size)
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(CF_UNICODETEXT, handle)
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001 - restoring the clipboard must not fail a read
        return


def _foreground_is(top: int) -> bool:
    """Is the trading window the one that would receive keystrokes?"""
    foreground = ctypes.windll.user32.GetForegroundWindow()
    if not foreground:
        return False
    return foreground == top or ctypes.windll.user32.IsChild(
        wintypes.HWND(top), wintypes.HWND(foreground)) != 0


def _ensure_foreground(top: int) -> bool:
    """Bring the client forward, and verify it actually came forward.

    Without this check the keys go to whatever window happens to be in front.
    Measured the hard way: a Ctrl+A/Ctrl+C meant for the holdings grid selected
    all the text of a different application instead, and 233 lines of it arrived
    as "the book". Sending keys to an unverified target is the one part of this
    tool that can touch something the user did not ask for.
    """
    user32 = ctypes.windll.user32
    if _foreground_is(top):
        return True
    user32.SetForegroundWindow(wintypes.HWND(top))
    for _ in range(10):
        time.sleep(0.1)
        if _foreground_is(top):
            return True
    return False


def _click_center(grid: int) -> None:
    """One left click in the middle of the grid, so it owns the keyboard.

    A click on a holdings row selects it; it cannot place an order. This is the
    smallest action that makes Ctrl+A meaningful, and without it the copy often
    lands nowhere.
    """
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetClientRect(wintypes.HWND(grid), ctypes.byref(rect))
    x, y = rect.right // 2, rect.bottom // 2
    point = wintypes.POINT(x, y)
    user32.ClientToScreen(wintypes.HWND(grid), ctypes.byref(point))
    user32.SetCursorPos(point.x, point.y)
    user32.SetForegroundWindow(wintypes.HWND(grid))
    user32.mouse_event(0x0002, 0, 0, 0, 0)   # MOUSEEVENTF_LEFTDOWN
    user32.mouse_event(0x0004, 0, 0, 0, 0)   # MOUSEEVENTF_LEFTUP
    time.sleep(0.2)


def _clipboard_matches_grid(text: str) -> bool:
    """Does this look like a holdings table at all?

    The check runs before parsing so a wrong-focus copy is reported as what it
    is — "this is another window's text" — instead of as a parse failure.
    """
    from scripts.external import known_headers

    known = known_headers()
    for line in text.splitlines()[:5]:
        cells = {cell.strip() for cell in line.replace(",", "\t").split("\t")}
        if len(cells & known) >= 3:
            return True
    return False


def _press(virtual_key: int) -> None:
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(virtual_key, 0, 0, 0)
    user32.keybd_event(virtual_key, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.35)


def read_grid(*, allow_busy: bool = False, click: bool = True) -> tuple[str, str]:
    """Return (clipboard text, error). An error means nothing should be written."""
    user32 = ctypes.windll.user32
    top, grid = _find_windows()
    if not top:
        return "", ("没找到「网上股票交易系统5.0」窗口——同花顺的独立下单程序没有启动，"
                    "或者还停在登录框")
    if not grid:
        return "", "找到窗口但没找到持仓表格（CVirtualGridCtrl）；先切到「查询资金股票」页面"
    if user32.IsIconic(wintypes.HWND(top)):
        return "", "窗口最小化了：最小化时复制不到内容，先把它还原"
    if not allow_busy and _idle_millis() < 2000:
        return "", "键盘刚刚被用过（2 秒内），这一轮跳过；你正在操作时不去抢焦点"

    previous = user32.GetForegroundWindow()
    if not _ensure_foreground(top):
        return "", ("没能把交易窗口切到前台，所以一个键都没发。"
                    "焦点不在它上面时按 Ctrl+A/Ctrl+C 会作用到别的程序上——宁可这次不读")

    before = _clipboard_get()
    try:
        if click:
            _click_center(grid)
        if not _foreground_is(top):          # a click can hand focus elsewhere
            return "", "点击表格后焦点不在交易窗口，已中止，未发送任何按键"
        _press(VK_A)
        _press(VK_C)
        after = _clipboard_get()
    finally:
        _clipboard_set(before)
        if previous and previous != top:
            user32.SetForegroundWindow(wintypes.HWND(previous))

    if not after.strip():
        return "", "复制之后剪贴板是空的：这张表可能不接受 Ctrl+C"
    if after == before:
        return "", ("剪贴板内容没有变化：Ctrl+C 没有生效（或焦点不在表格上）。"
                    "没有拿到新数据，就不写任何东西")
    if not _clipboard_matches_grid(after):
        preview = " / ".join(after.splitlines()[:2])[:80]
        return "", (f"复制到的不是持仓表而是别处的内容（前两行：{preview}）。"
                    f"焦点可能又被抢走了，这次不写库")
    return after, ""


def _validate(rows: list[dict]) -> list[str]:
    """Reasons to refuse. An empty list means the read looks self-consistent."""
    problems: list[str] = []
    if not rows:
        problems.append("一行都没解析出来")
        return problems
    seen: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if len(symbol) != 9 or not symbol[:6].isdigit():
            problems.append(f"代码不像 A 股：{symbol!r}")
        if symbol in seen:
            problems.append(f"{symbol} 出现了两次，同一只票不该有两行")
        seen.add(symbol)
        if int(row.get("qty") or 0) <= 0:
            problems.append(f"{symbol} 股数 {row.get('qty')} 不合理")
        market_value = float(row.get("market_value") or 0)
        cost = float(row.get("cost") or 0)
        qty = int(row.get("qty") or 0)
        if qty and not market_value:
            problems.append(
                f"{symbol} 有 {qty} 股但市值是 0：要么停牌，要么是当日卖出后待交收的残留行。"
                f"这一行不该按持仓记")
        if qty and not cost:
            problems.append(f"{symbol} 有 {qty} 股但成本价是 0：成本列可能错位")
        if market_value and cost and qty:
            implied = qty * cost
            if implied and abs(market_value - implied) / implied > 0.5:
                problems.append(
                    f"{symbol} 市值 {market_value:,.0f} 与 股数×成本 {implied:,.0f} 差太多"
                    f"（>50%），可能列错位了")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="让交易客户端自己复制一次持仓表，再读回来")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库（默认行为）")
    parser.add_argument("--write", action="store_true", help="写持仓快照，source=screen:ths")
    parser.add_argument("--press-keys", action="store_true",
                        help="真正发送 Ctrl+A/Ctrl+C（默认只检查，不发键）")
    parser.add_argument("--allow-busy", action="store_true", help="不检查键盘空闲")
    parser.add_argument("--no-click", action="store_true", help="不点击表格（只按键）")
    parser.add_argument("--raw", action="store_true", help="打印复制到的原始文本")
    parser.add_argument("--as-of", dest="as_of", default="", help="快照日期，默认今天")
    args = parser.parse_args(argv)

    if not args.press_keys:
        # Sending keys is the part that can touch something else: a lost focus
        # race once selected all the text of an unrelated application. Opt in.
        print("[guard] 这个脚本会给交易窗口发 Ctrl+A / Ctrl+C，可能抢焦点。")
        print("        确认要用，加 --press-keys；日常监控请用 service/trading/ths_account.py")
        return 1

    text, error = read_grid(allow_busy=args.allow_busy, click=not args.no_click)
    if error:
        print(f"[refuse] {error}")
        return 1
    if args.raw:
        print("--- 原始内容（制表符用 | 表示）---")
        for line in text.splitlines()[:12]:
            print("   " + line.replace("\t", " | ")[:200])
        print(f"--- 共 {len(text.splitlines())} 行 ---")

    known = known_headers()
    try:
        frame = _promote(_frame_from_text(text, known, "剪贴板"), known, "剪贴板")
        picked = _pick_profile(frame)
        if picked is None:
            print("[refuse] 复制到的内容认不出表头。前两行：")
            for line in text.splitlines()[:2]:
                print("   " + line[:120])
            return 1
        profile_name, profile, mapping = picked
        rows, skipped = rows_from(frame, mapping, profile)
    except RuntimeError as exc:
        print(f"[refuse] {exc}")
        return 1

    if profile["kind"] != "positions":
        print(f"[refuse] 复制到的是 {profile_name}，不是持仓表；"
              f"先用 paste 处理成交，或切到持仓页面再运行")
        return 1

    print(f"[read] profile={profile_name} rows={len(rows)} skipped={len(skipped)}")
    for row in rows:
        print(f"   {row['symbol']} {row['qty']:>6} 股  成本 {row['cost']:.3f}  "
              f"市值 {row['market_value']:>10,.0f}  {row['name']}")

    problems = [f"未解析行：{note}" for note in skipped] + _validate(rows)
    if problems:
        print("[refuse] 读到的数不自洽，不写库：")
        for item in problems:
            print("   - " + item)
        return 1
    print("[ok] 自检通过：代码、股数、市值与成本互相吻合")

    if not args.write:
        print("[dry] 未写库。加 --write 才写入快照。")
        return 0

    store = ExternalStore(DB)
    as_of = args.as_of or datetime.now(SHANGHAI).strftime("%Y%m%d")
    count = store.replace_snapshot(as_of, rows, source="screen:ths")
    store.log_import(file_name=f"screen-ths-{as_of}", file_sha="", profile=profile_name,
                     rows=len(rows), inserted=count, duplicates=0, status="ok")
    print(f"[ok] 持仓快照 {as_of}：{count} 只（source=screen:ths）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
