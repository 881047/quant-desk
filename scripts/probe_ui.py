"""What can actually be read out of a running client — measured, not assumed.

The question this answers: if you log into 同花顺 on this machine, can a program
read the holdings grid, and does it need OCR?

Two very different outcomes are possible and they look identical from outside:

    the grid is a standard control or an embedded browser table
        -> every cell is a `name` in the accessibility tree: exact, free, live
    the grid is owner-drawn (DuiLib and friends)
        -> the tree stops at the container, and only a screenshot carries the text

This script reads the UI Automation tree of a window and reports which of the two
you are looking at. It never clicks, types, focuses or sends anything: opening a
window's tree is a read of data Windows already publishes.

    python scripts/probe_ui.py --list                 # what is open right now
    python scripts/probe_ui.py --match 同花顺
    python scripts/probe_ui.py --match 同花顺 --depth 8 --json out.json

Exit code is 0 when text was found, 1 when only containers were exposed, 2 on a
setup problem — so it can be used as a check, not only as a report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STOCK_CODE = re.compile(r"^\d{6}$")
PRICE = re.compile(r"^\d{1,6}\.\d{2,3}$")
QUANTITY = re.compile(r"^\d{2,7}$")


def _backend():
    import warnings

    # pywinauto ships a docstring with an invalid escape; harmless, but it prints
    # a SyntaxWarning into the middle of this report.
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    try:
        from pywinauto import Desktop
    except ImportError:  # pragma: no cover - the message is the point
        print("缺少 pywinauto。安装：uv pip install --python .venv/Scripts/python.exe pywinauto")
        raise SystemExit(2)
    return Desktop(backend="uia")


def wake_accessibility(handle: int) -> int:
    """Turn Chromium's accessibility tree on, and say how many widgets were poked.

    Chromium only builds its accessibility tree once an assistive client asks. On
    a quiet desktop it answers UIA with the window frame and nothing else — which
    reads exactly like an owner-drawn grid, and is why "UIA cannot see it" is
    often a wrong diagnosis. Sending WM_GETOBJECT with OBJID_CLIENT to the
    renderer widget is the documented way to ask; measured on this machine it took
    a Chromium window from 7 nodes / 1 name to 278 nodes / 231 names.

    同花顺 ships cef84 and a jsinject folder, so its trading page may well be one
    of these. That is exactly what this probe is for: find out instead of guessing.
    """
    import ctypes
    from ctypes import wintypes

    WM_GETOBJECT = 0x003D
    OBJID_CLIENT = -4
    user32 = ctypes.windll.user32

    def class_name(hwnd) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), buffer, 256)
        return buffer.value

    children: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _param):
        children.append(hwnd)
        return True

    user32.EnumChildWindows(wintypes.HWND(handle), callback_type(collect), 0)
    renderers = [h for h in children if "RenderWidgetHost" in class_name(h)]
    for hwnd in renderers:
        for _ in range(2):
            user32.SendMessageW(wintypes.HWND(hwnd), WM_GETOBJECT, 0,
                                ctypes.c_long(OBJID_CLIENT))
    if renderers:
        import time

        time.sleep(0.8)
    return len(renderers)


def _process_name(pid: int) -> str:
    """Executable name for a pid, via ctypes — no extra dependency, no COM."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


def list_windows(desktop) -> list[dict]:
    rows = []
    for window in desktop.windows():
        try:
            info = window.element_info
            rows.append({"title": info.name, "class": info.class_name,
                         "pid": info.process_id, "handle": info.handle,
                         "process": _process_name(info.process_id)})
        except Exception:  # noqa: BLE001, S112 - a window can vanish mid-scan
            continue
    return rows


def walk(window, depth: int, limit: int) -> dict:
    """Breadth-first read of the element tree, with a hard node cap.

    The cap matters: a broker client's main window can carry tens of thousands of
    nodes, and an unbounded walk looks exactly like a hang.
    """
    root = window
    stack: list[tuple[object, int]] = [(root, 0)]
    nodes: list[dict] = []
    control_types: dict[str, int] = {}
    truncated = False

    while stack:
        element, level = stack.pop(0)
        if len(nodes) >= limit:
            truncated = True
            break
        try:
            info = element.element_info
            name = str(info.name or "")
            control_type = str(info.control_type or "")
        except Exception:  # noqa: BLE001, S112  # pragma: no cover - detached node
            continue
        nodes.append({"depth": level, "type": control_type, "name": name,
                      "class": str(getattr(info, "class_name", "") or "")})
        control_types[control_type] = control_types.get(control_type, 0) + 1
        if level >= depth:
            continue
        try:
            children = element.children()
        except Exception:  # noqa: BLE001  # pragma: no cover - some refuse
            children = []
        stack.extend((child, level + 1) for child in children)

    names = [n["name"] for n in nodes if n["name"].strip()]
    codes = [n for n in names if STOCK_CODE.match(n)]
    prices = [n for n in names if PRICE.match(n)]
    quantities = [n for n in names if QUANTITY.match(n)]
    return {
        "nodes": len(nodes),
        "truncated": truncated,
        "control_types": dict(sorted(control_types.items(), key=lambda kv: -kv[1])[:12]),
        "names": len(names),
        "codes": codes[:12],
        "prices": prices[:12],
        "quantities": quantities[:12],
        "sample": names[:25],
        "detail": nodes,
    }


def verdict(result: dict) -> tuple[str, int]:
    if result["codes"] and result["prices"]:
        return ("可读：单元格文字以元素 name 暴露。可以做一个实时读取器，不需要 OCR。", 0)
    if result["codes"] or result["prices"]:
        return ("部分可读：代码或价格只出现了一类，可能只暴露了部分列。值得再看一次树。", 0)
    if result["names"] >= 30:
        return ("树已打开（{} 个命名元素），但没有 6 位代码或价格格式的文字。"
                "如果这就是持仓页，说明数值不在文本层——多半是自绘表。".format(result["names"]), 1)
    return ("几乎读不到文字（只有 {} 个命名元素）。"
            "要么窗口没登录/没切到持仓页，要么整块是自绘的。".format(result["names"]), 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读出某个窗口的可访问性树（只读，不操作）")
    parser.add_argument("--list", action="store_true", help="列出当前所有顶层窗口")
    parser.add_argument("--match", default="", help="按标题关键词筛选，例如 同花顺 / 持仓")
    parser.add_argument("--class-name", dest="class_name", default="",
                        help="按窗口类名筛选，例如 Hexin")
    parser.add_argument("--depth", type=int, default=6, help="遍历深度，默认 6")
    parser.add_argument("--limit", type=int, default=4000, help="节点上限，默认 4000")
    parser.add_argument("--no-wake", action="store_true",
                        help="不发送 WM_GETOBJECT（Chromium 界面会读不到文字）")
    parser.add_argument("--tree", action="store_true", help="打印缩进的元素树，便于设计提取规则")
    parser.add_argument("--json", default="", help="把结果另存为 JSON")
    args = parser.parse_args(argv)

    desktop = _backend()
    windows = list_windows(desktop)

    if args.list or not args.match:
        print(f"{'标题':<40} {'类名':<28} {'pid':>7}  进程")
        for row in windows:
            title = (row["title"] or "")[:38]
            print(f"{title:<40} {(row['class'] or '')[:26]:<28} {row['pid']:>7}  {row['process']}")
        if not args.match:
            print("\n加 --match 关键词（例如 --match 同花顺）再看那个窗口的树。")
        return 0

    targets = [w for w in windows
               if (args.match and args.match.lower() in (w["title"] or "").lower())
               or (args.class_name and args.class_name.lower() in (w["class"] or "").lower())]
    if not targets:
        print(f"没有标题含 “{args.match}” 的窗口。当前打开的有：")
        for row in windows[:20]:
            print(f"  {row['title']!r}  class={row['class']!r}")
        return 2

    worst = 0
    payload = []
    for target in targets:
        print(f"\n=== {target['title']!r}  class={target['class']!r}  pid={target['pid']}")
        try:
            if not args.no_wake:
                woke = wake_accessibility(target["handle"])
                if woke:
                    print(f"  已唤醒可访问性（{woke} 个渲染控件）")
            window = desktop.window(handle=target["handle"])
            result = walk(window, args.depth, args.limit)
        except Exception as exc:  # noqa: BLE001 - report, do not crash the scan
            print(f"  读取失败：{type(exc).__name__}: {exc}")
            worst = max(worst, 2)
            continue
        text, code = verdict(result)
        worst = max(worst, code)
        print(f"  节点 {result['nodes']}（截断={result['truncated']}，命名元素 {result['names']}）")
        print("  控件类型：" + ", ".join(f"{k}×{v}" for k, v in result["control_types"].items()))
        if result["codes"]:
            print("  代码样例：" + ", ".join(result["codes"]))
        if result["prices"]:
            print("  价格样例：" + ", ".join(result["prices"]))
        print("  结论：" + text)
        print("  名字样例：" + " | ".join(result["sample"][:12]))
        if args.tree:
            print("  --- 元素树 ---")
            for node in result["detail"][:400]:
                label = node["name"][:48].replace("\n", " ")
                print(f"  {'  ' * node['depth']}{node['type']:<18} {label}")
        payload.append({"window": target, "nodes": result["nodes"],
                        "truncated": result["truncated"],
                        "control_types": result["control_types"],
                        "names": result["names"], "codes": result["codes"],
                        "prices": result["prices"], "sample": result["sample"],
                        "verdict": text})

    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\n已写入 {args.json}")
    return worst if payload else 2


if __name__ == "__main__":
    sys.exit(main())
