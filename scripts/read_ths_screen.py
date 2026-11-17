"""Read the holdings grid from pixels — the only zero-touch path.

The grid (`CVirtualGridCtrl`) exposes nothing to UIA or MSAA, and the clipboard
route needs the window focused. What does work is `PrintWindow`: it asks the window
to draw itself into a bitmap, so a covered, background window renders completely.
Measured on this machine: an occluded grid came back as a clean 905x360 table.

That leaves OCR. Windows' built-in engine recognises every digit but splits on the
decimal point (`9` + `035`) and drops the minus sign in front of 盈亏. So raw OCR
text is not a table — it is fragments that need column positions to mean anything.

This module turns fragments into rows, then makes the numbers prove themselves:

  * a cell's digits are only a number once a decimal position is chosen;
  * the choice is not guessed — for each row it is the placement that satisfies
    盈亏 ≈ 实际数量 × (市价 − 成本价);
  * the surviving rows must also sum to the account panel's 股票市值.

Any row that cannot prove itself is dropped with a reason, and if nothing survives
the read fails. A wrong digit here has no symptom: it just makes every later
number slightly wrong, so refusing is the correct behaviour.

    python scripts/read_ths_screen.py --dry-run
    python scripts/read_ths_screen.py --write
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.kernel.clock import SHANGHAI
from service.trading.external import ExternalStore, normalize_symbol

# The client's column order. OCR mangles some headers (成本价 came back as 成/亻介/价),
# so the names are not read from the image — the *count* and the order are fixed by
# the client, and positions come from where the header fragments sit.
COLUMNS = ("操作", "证券代码", "证券名称", "股票余额", "实际数量", "可用余额",
           "冻结数量", "成本价", "市价", "盈亏", "市值", "当日买入", "当日卖出")

WANTED = ("证券代码", "证券名称", "实际数量", "股票余额", "可用余额", "成本价", "市价", "盈亏")


@dataclass
class Token:
    x: int
    y: int
    text: str


def _group_header(tokens: list[Token], *, y_max: int, gap: int = 46) -> list[int]:
    """Column centres, from the header row.

    Header cells are split into individual characters (`证`,`券`,`代`,`码`), so the
    centres are found by clustering on x rather than by reading the words.
    """
    header = sorted((t for t in tokens if t.y <= y_max), key=lambda t: t.x)
    if not header:
        return []
    centres: list[int] = []
    start = header[0].x
    last = header[0].x
    for token in header[1:]:
        if token.x - last > gap:
            centres.append((start + last) // 2)
            start = token.x
        last = token.x
    centres.append((start + last) // 2)
    return centres


def _assign(column_centres: list[int], x: int) -> int:
    return min(range(len(column_centres)), key=lambda i: abs(column_centres[i] - x))


def build_rows(tokens: list[Token], *, row_gap: int = 24) -> tuple[list[list[str]], list[str]]:
    """Fragments in, table out. Pure: the fixture in the tests is real OCR output."""
    if not tokens:
        return [], ["没有任何 OCR 结果"]

    header_y = min(t.y for t in tokens)
    centres = _group_header(tokens, y_max=header_y + 20)
    problems: list[str] = []
    if len(centres) < len(WANTED):
        return [], [f"表头只找到 {len(centres)} 列，少于预期的 {len(WANTED)} 列"]

    body = sorted((t for t in tokens if t.y > header_y + 20), key=lambda t: (t.y, t.x))
    if not body:
        return [], ["表头之下没有数据行"]

    rows: list[list[list[str]]] = []       # row -> column -> fragments
    anchors: list[int] = []
    for token in body:
        if not anchors or token.y - anchors[-1] > row_gap:
            anchors.append(token.y)
            rows.append([[] for _ in centres])
        rows[-1][_assign(centres, token.x)].append(token.text)

    names = COLUMNS[:len(centres)]
    out = []
    for index, cells in enumerate(rows):
        joined = ["".join(fragments) for fragments in cells]
        if not any(joined):
            continue
        record = dict(zip(names, joined, strict=False))
        if not record.get("证券代码"):
            problems.append(f"第 {index + 1} 行没有代码，跳过")
            continue
        out.append(record)
    return out, problems


def _num(digits: str, places: int) -> float | None:
    """`9035` with places=3 is 9.035; with places=1 it is 903.5."""
    if not digits or not digits.isdigit():
        return None
    if places <= 0:
        return float(digits)
    if len(digits) <= places:
        digits = digits.rjust(places + 1, "0")
    return float(f"{digits[:-places]}.{digits[-places:]}")


def interpret(rows: list[dict[str, str]], *,
              panel_value: float | None = None) -> tuple[list[dict], list[str], str]:
    """Turn digit soup into positions, and let the numbers choose the decimals.

    OCR drops the decimal point, so `9.035` arrives as `9035` and `903.5` fits the
    same digits. Per-row arithmetic cannot decide between them: 盈亏 ≈ 数量 ×
    (市价 − 成本价) is scale invariant — 9.035/8.190 and 903.5/819.0 both satisfy it.
    The scale has to come from outside: the account panel's 股票市值 is an absolute
    number, and only one placement makes Σ 数量 × 市价 land on it.

    So the placement is a single global convention (the client formats a column one
    way), chosen by two votes: how many rows satisfy their own 盈亏 equation, and
    how close the total gets to the panel. Rows that still fail are dropped.
    """
    prepared = []
    problems: list[str] = []
    for row in rows:
        code = re.sub(r"\D", "", row.get("证券代码", ""))
        if len(code) != 6:
            problems.append(f"代码不完整：{row.get('证券代码')!r}")
            continue
        symbol = normalize_symbol(code)
        qty_digits = re.sub(r"\D", "", row.get("实际数量") or row.get("股票余额") or "")
        if not qty_digits:
            problems.append(f"{symbol} 没有股数")
            continue
        qty = int(qty_digits)
        if qty == 0:
            # Sold today: the row lingers until settlement and is not a position.
            continue
        prepared.append({
            "symbol": symbol,
            "name": row.get("证券名称", "").strip(),
            "qty": qty,
            "available": int(re.sub(r"\D", "", row.get("可用余额") or "0") or 0),
            "cost_digits": re.sub(r"\D", "", row.get("成本价", "")),
            "price_digits": re.sub(r"\D", "", row.get("市价", "")),
            "pnl_digits": re.sub(r"\D", "", row.get("盈亏", "")),
        })

    if not prepared:
        return [], problems, ""

    best = None
    for cost_places in (3, 2, 1, 0):
        for price_places in (3, 2, 1, 0):
            for pnl_places in (3, 2, 1, 0):
                passed, total = 0, 0.0
                errors = []
                for item in prepared:
                    cost = _num(item["cost_digits"], cost_places)
                    price = _num(item["price_digits"], price_places)
                    pnl = _num(item["pnl_digits"], pnl_places)
                    if cost is None or price is None:
                        errors.append(float("inf"))
                        continue
                    implied = item["qty"] * (price - cost)
                    error = abs(abs(implied) - abs(pnl)) if pnl is not None else 0.0
                    if error <= max(1.0, abs(implied) * 0.02):
                        passed += 1
                    else:
                        errors.append(error)
                    total += item["qty"] * price
                panel_error = abs(total - panel_value) if panel_value else 0.0
                # Most rows proving themselves first, then the closest total,
                # then the finer convention (the client prints three decimals).
                score = (-passed, round(panel_error, 2), -(cost_places + price_places + pnl_places))
                if best is None or score < best[0]:
                    best = (score, cost_places, price_places, pnl_places, passed, total)

    _, cost_places, price_places, pnl_places, passed, total = best
    convention = (f"成本{cost_places}位/市价{price_places}位/盈亏{pnl_places}位 小数；"
                  f"{passed}/{len(prepared)} 行自证通过；合计 {total:,.2f}")

    positions = []
    for item in prepared:
        cost = _num(item["cost_digits"], cost_places)
        price = _num(item["price_digits"], price_places)
        pnl = _num(item["pnl_digits"], pnl_places)
        if cost is None or price is None:
            problems.append(f"{item['symbol']} 成本/市价无法解析")
            continue
        implied = item["qty"] * (price - cost)
        if pnl is not None and abs(abs(implied) - abs(pnl)) > max(1.0, abs(implied) * 0.02):
            problems.append(
                f"{item['symbol']} 自校验不过：{item['qty']} × ({price} − {cost}) = "
                f"{implied:.2f}，表上盈亏 {pnl:.2f}")
            continue
        positions.append({
            "symbol": item["symbol"], "name": item["name"], "qty": item["qty"],
            "available": item["available"], "cost": round(cost, 3),
            "market_value": round(item["qty"] * price, 2),
            "pnl": round(implied, 2), "source": "screen:ths-ocr",
        })
    return positions, problems, convention


# --------------------------------------------------------------------- capture
def _windows() -> tuple[int, int]:
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

    tops: list[int] = []

    def find(hwnd, _param):
        if class_of(hwnd).startswith("Afx:") and "网上股票交易" in title_of(hwnd):
            tops.append(hwnd)
        return True

    user32.EnumWindows(callback_type(find), 0)

    for top in tops:
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
        if grid:
            return top, grid
    return (tops[0] if tops else 0), 0


def capture(grid: int, *, scale: int = 2):
    """Ask the window to draw itself. Works covered and in the background."""
    import win32gui
    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(grid)
    width, height = right - left, bottom - top
    window_dc = win32gui.GetWindowDC(grid)
    source = win32ui.CreateDCFromHandle(window_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, width, height)
    memory.SelectObject(bitmap)
    try:
        ctypes.windll.user32.PrintWindow(wintypes.HWND(grid), memory.GetSafeHdc(), 2)
        info = bitmap.GetInfo()
        raw = bitmap.GetBitmapBits(True)
        image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), raw,
                                 "raw", "BGRX", 0, 1)
        return image.resize((image.width * scale, image.height * scale), 1)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(grid, window_dc)


def ocr(image) -> list[Token]:
    from winsdk.windows.globalization import Language
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage import FileAccessMode, StorageFile

    path = ROOT / "data" / "external" / "_ths_grid.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)

    async def run() -> list[Token]:
        engine = (OcrEngine.try_create_from_language(Language("zh-Hans"))
                  or OcrEngine.try_create_from_user_profile_languages())
        if engine is None:
            raise RuntimeError("这台机器没有中文 OCR 引擎")
        file = await StorageFile.get_file_from_path_async(str(path))
        stream = await file.open_async(FileAccessMode.READ)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await engine.recognize_async(bitmap)
        tokens: list[Token] = []
        for line in result.lines:
            for word in line.words:
                rect = word.bounding_rect
                tokens.append(Token(x=round(rect.x), y=round(rect.y), text=word.text))
        return tokens

    return asyncio.run(run())


def fix_names(positions: list[dict]) -> None:
    """Replace OCR names with the official ones, keyed by the code.

    OCR reads 古汉医药 as 古汉药医 — character order inside a name is the one thing
    it gets wrong that costs nothing to fix, because the code is exact.
    """
    try:
        from service.adapters.parquet_store import DatasetStore
        from service.adapters.sqlite_store import Registry

        store_data = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
        basic = store_data.read("stock_basic")
        official = dict(zip(basic["ts_code"].astype(str), basic["name"].astype(str)))
    except Exception as exc:  # noqa: BLE001 - a wrong name is cosmetic
        print(f"[warn] 没能用代码校正名称：{type(exc).__name__}")
        return
    for position in positions:
        position["name"] = official.get(position["symbol"], position["name"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从屏幕像素读持仓表（零操作，需客户端打开）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--as-of", dest="as_of", default="")
    parser.add_argument("--save-tokens", default="", help="把 OCR token 存成 JSON（做夹具用）")
    args = parser.parse_args(argv)

    _, grid = _windows()
    if not grid:
        print("[refuse] 没找到持仓表格：客户端没开，或没停在「查询资金股票」页")
        return 1

    tokens = ocr(capture(grid))
    print(f"[ocr] 识别到 {len(tokens)} 个片段")
    if args.save_tokens:
        import json

        Path(args.save_tokens).write_text(
            json.dumps([[t.x, t.y, t.text] for t in tokens], ensure_ascii=False),
            encoding="utf-8")
        print(f"[ocr] token 已存到 {args.save_tokens}")

    rows, row_problems = build_rows(tokens)

    from service.trading.ths_account import read_account

    panel = read_account()
    panel_value = float(panel["market_value"]) if panel.get("ok") else None
    positions, number_problems, convention = interpret(rows, panel_value=panel_value)
    for problem in row_problems + number_problems:
        print("   [skip] " + problem)
    if convention:
        print(f"[scale] {convention}")

    if not positions:
        print("[refuse] 没有一行能通过自校验，不写库")
        return 1

    fix_names(positions)

    total = sum(p["market_value"] for p in positions)
    print(f"[read] {len(positions)} 只，市值合计 {total:,.2f}")
    for p in positions:
        print(f"   {p['symbol']} {p['name']} {p['qty']:>6} 股 成本 {p['cost']:.3f} "
              f"市值 {p['market_value']:>10,.2f} 盈亏 {p['pnl']:+,.2f}")

    if panel.get("ok"):
        diff = abs(panel["market_value"] - total)
        tolerance = max(30.0, panel["market_value"] * 0.01)
        print(f"[check] 面板市值 {panel['market_value']:,.2f} vs 读出 {total:,.2f}"
              f"（差 {diff:,.2f}，容差 {tolerance:,.0f}）")
        if diff > tolerance:
            print("[refuse] 与账户面板对不上，说明有列读错了；不写库")
            return 1
    else:
        print(f"[warn] 无法与账户面板交叉校验：{panel.get('reason')}")

    if not args.write:
        print("[dry] 未写库。加 --write 才写入快照。")
        return 0

    if row_problems or number_problems:
        print("[refuse] OCR 有未通过校验的行；部分持仓不能替换完整快照，不写库")
        return 1

    store = ExternalStore(ROOT / "data" / "external.sqlite")
    as_of = args.as_of or datetime.now(SHANGHAI).strftime("%Y%m%d")
    count = store.replace_snapshot(as_of, positions, source="screen:ths-ocr")
    store.log_import(file_name=f"screen-ocr-{as_of}", file_sha="", profile="positions",
                     rows=len(positions), inserted=count, duplicates=0, status="ok")
    print(f"[ok] 持仓快照 {as_of}：{count} 只（source=screen:ths-ocr）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
