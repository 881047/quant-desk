"""Read-only synchronisation with the real brokerage account.

    python scripts/external.py import                    # everything in data/external/drop
    python scripts/external.py import --dry-run
    python scripts/external.py add --symbol 000700.SZ --side BUY \
        --qty 100 --price 13.07 --date 20261026          # when there is no export yet
    python scripts/external.py report                    # 收盘后：盈亏 + 纪律位
    python scripts/external.py report --preopen          # 09:26：竞价线是否触发
    python scripts/external.py status

No subcommand sends an order, and none of them reads a password. That is the
whole design: since the 2026-08 接入规范, a personal program that places orders is
程序化交易 (institutional-only channel); a program that reads a statement is
bookkeeping. The line is crossed by the first `POST /order`, not by an import.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import pandas as pd

from service.adapters.jsonio import write_json_atomic, write_text_atomic
from service.kernel.clock import SHANGHAI
from service.trading.external import (
    ExternalStore,
    levels,
    normalize_date,
    normalize_side,
    normalize_symbol,
    reconcile,
    trade_key,
)

DROP = ROOT / "data" / "external" / "drop"
DB = Path(os.getenv("EXTERNAL_DB") or (ROOT / "data" / "external.sqlite"))
MAP = ROOT / "configs" / "broker_map.json"
REPORTS = ROOT / "reports" / "external"
EXTS = (".csv", ".xlsx", ".xls", ".txt")


# ------------------------------------------------------------------ reading
def _read_table(path: Path) -> pd.DataFrame:
    """Any broker export → a frame with the real header row promoted.

    Three shapes are handled, because brokers ship all three under the same
    `.xls` extension:
      * a real workbook (openpyxl / xlrd),
      * a delimited text file with a title line above the header,
      * an **HTML table renamed to .xls** — very common for web 交割单 exports,
        and `read_excel` refuses it with "Excel file format cannot be determined".
    """
    known = known_headers()
    suffix = path.suffix.lower()
    raw = None
    if suffix in (".xlsx", ".xls"):
        try:
            raw = pd.read_excel(path, header=None, dtype=str)
        except Exception:  # noqa: BLE001 - most likely an HTML/SpeadsheetML export
            raw = None
        if raw is None:
            raw = _read_html_table(path, known)
    else:
        raw = _read_delimited(path, known)
    return _promote(raw, known, path.name)


def _promote(raw: pd.DataFrame, known: set[str], label: str) -> pd.DataFrame:
    """Find the header row and make it the column names.

    A title line above the header ("成交明细", "资金股份") is standard in Chinese
    broker exports, so the header is located by counting how many known column
    names a row contains rather than assuming row 0.
    """
    best_row, best_hits = None, 0
    for idx in range(min(len(raw), 12)):
        cells = {str(v).strip() for v in raw.iloc[idx].tolist() if str(v).strip()}
        hits = len(cells & known)
        if hits > best_hits:
            best_row, best_hits = idx, hits
    if best_row is None or best_hits < 3:
        raise RuntimeError(
            f"{label} 找不到表头（匹配到的已知列名 {best_hits} 个）。"
            f"第一行内容：{[str(v) for v in raw.iloc[0].tolist() if str(v) != 'nan'][:8]}")

    frame = raw.iloc[best_row + 1:].copy()
    frame.columns = [str(v).strip() for v in raw.iloc[best_row].tolist()]
    return frame.dropna(how="all")


def _clipboard_text() -> str:
    """Windows clipboard → str, without a third-party dependency.

    `Ctrl+C` on a broker's 持仓 grid is the shortest path from "what the client
    shows" to "what the ledger knows": no window automation, no OCR, no reading
    of the client's memory. The user copies, this reads.

    Both CF_UNICODETEXT and CF_TEXT are tried. Older Delphi grids — and 同花顺's
    trading module is one — still put GBK bytes on the clipboard, and reading
    those as UTF-16 yields mojibake that parses as a table with no known header.
    """
    import ctypes
    from ctypes import wintypes

    CF_TEXT, CF_UNICODETEXT = 1, 13
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    if not user32.OpenClipboard(None):
        raise RuntimeError("打不开剪贴板：另一个程序正在占用它，稍等一秒再试")

    def _read(fmt: int) -> bytes | None:
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            if fmt == CF_UNICODETEXT:
                return ctypes.wstring_at(pointer).encode("utf-8")
            raw = ctypes.string_at(pointer)
            for encoding in ("gbk", "utf-8"):
                try:
                    return raw.decode(encoding).encode("utf-8")
                except UnicodeDecodeError:
                    continue
            return raw
        finally:
            kernel32.GlobalUnlock(handle)

    try:
        raw = _read(CF_UNICODETEXT) or _read(CF_TEXT)
    finally:
        user32.CloseClipboard()
    if not raw:
        raise RuntimeError("剪贴板里没有文本（先在表格里 Ctrl+A、Ctrl+C）")
    return raw.decode("utf-8", "replace")


def _profiles() -> dict:
    return json.loads(MAP.read_text(encoding="utf-8"))["profiles"]


def known_headers() -> set[str]:
    """Every alias the broker map knows, or an empty set when it cannot be read.

    ``_profiles`` parses ``configs/broker_map.json`` with a hard index into
    ``["profiles"]``; a missing or edited file raised ``JSONDecodeError`` /
    ``KeyError`` straight out of `paste`, which is the one command a user reaches
    for when an import went wrong.
    """
    try:
        profiles = _profiles()
    except (OSError, ValueError, KeyError, TypeError):
        return set()
    return {alias for profile in profiles.values()
            for aliases in (profile.get("columns") or {}).values()
            for alias in aliases}


def _read_html_table(path: Path, known: set[str]) -> pd.DataFrame:
    """Pick the table that looks like a statement, from an HTML export.

    A web 交割单 saved as `.xls` is an HTML document: several <table>s (title,
    totals, the data), and `read_excel` cannot open it at all. The one worth
    keeping is the one whose first rows contain the most known column names.
    """
    last_error: Exception | None = None
    tables = None
    raw_bytes = path.read_bytes()
    # Brokers export GBK pages that declare no charset; read_html needs to be told.
    for encoding in ("utf-8", "gbk"):
        text = None
        try:
            text = raw_bytes.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        try:
            tables = pd.read_html(io.StringIO(text), header=None)
            break
        except ValueError as exc:      # "No tables found"
            last_error = exc
            tables = None
    if not tables:
        raise RuntimeError(f"{path.name} 既不是 Excel 也不是含表格的 HTML：{last_error}")

    best, best_hits = None, 0
    for table in tables:
        head = table.head(3).to_numpy().ravel()
        cells = {str(v).strip() for v in head if str(v).strip() not in ("", "nan")}
        hits = len(cells & known)
        if hits > best_hits:
            best, best_hits = table, hits
    if best is None or best_hits < 3:
        raise RuntimeError(
            f"{path.name} 是 HTML，但没有一张表含已知列名（最多命中 {best_hits} 个）；"
            f"表头可能是：{[str(v) for v in tables[0].head(2).to_numpy().ravel() if str(v) != 'nan'][:8]}")
    return best.astype(str).reset_index(drop=True)


def _read_delimited(path: Path, known: set[str]) -> pd.DataFrame:
    """Read a text export whose first line is often a title, not a header.

    pandas infers the column count from line 1. A title line ("成交明细") has one
    field, so the whole frame collapses to a single column and every later row
    arrives as one long string — which then fails the header check for a reason
    that has nothing to do with the file being wrong. Delimiter and header line
    are therefore found here first, and pandas is handed a file that begins at
    the header.
    """
    text = None
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    if text is None:
        raise RuntimeError(f"无法解码 {path.name}: {last_error}")

    return _frame_from_text(text, known, path.name)


def _frame_from_text(text: str, known: set[str], label: str) -> pd.DataFrame:
    """Delimited text → a frame whose header row has already been promoted.

    Shared by the file reader and the clipboard reader, because a copy out of a
    broker grid and an export of the same grid are the same bytes with a
    different delivery method. Two parsers would drift, and then "it worked from
    the file but not from the clipboard" becomes a bug nobody can reproduce.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise RuntimeError(f"{label} 是空的")

    best_sep, best_score = None, 0
    for sep in ("\t", ",", ";", "|"):
        score = sum(line.count(sep) for line in lines[:20])
        if score > best_score:
            best_sep, best_score = sep, score
    if not best_sep or best_score < 3:
        raise RuntimeError(
            f"{label} 找不到分隔符（制表符/逗号/分号/竖线）——"
            f"多半是只选中了一列，或者复制的是整块带空格的版面。"
            f"前两行：{[line[:60] for line in lines[:2]]}")

    start, best_hits = 0, -1
    for idx, line in enumerate(lines[:20]):
        cells = {cell.strip().strip('"') for cell in line.split(best_sep) if cell.strip()}
        if len(cells) < 3:
            continue
        hits = len(cells & known)
        if hits > best_hits:
            start, best_hits = idx, hits
    if best_hits <= 0:
        raise RuntimeError(
            f"{label} 里没有一行像表头。表头前两行是："
            f"{[line[:80] for line in lines[:2]]}；"
            f"请把这两行发出来，我在 configs/broker_map.json 里补别名。")

    return pd.read_csv(io.StringIO("\n".join(lines[start:])), header=None, dtype=str,
                       sep=best_sep, on_bad_lines="skip")


def _resolve(frame: pd.DataFrame, profile: dict) -> dict[str, str] | None:
    """field -> actual column name, or None when a required field is missing."""
    headers = {h.strip(): h for h in frame.columns}
    mapping: dict[str, str] = {}
    for key, aliases in profile["columns"].items():
        for alias in aliases:
            if alias in headers:
                mapping[key] = headers[alias]
                break
    missing = [f for f in profile["required"] if f not in mapping]
    return None if missing else mapping


def _pick_profile(frame: pd.DataFrame) -> tuple[str, dict, dict] | None:
    best = None
    for name, profile in _profiles().items():
        mapping = _resolve(frame, profile)
        if mapping is None:
            continue
        score = len(mapping)
        if best is None or score > best[3]:
            best = (name, profile, mapping, score)
    return (best[0], best[1], best[2]) if best else None


def _to_float(value, default: float = 0.0) -> float:
    try:
        text = str(value).replace(",", "").replace("¥", "").strip()
        if not text or text.lower() == "nan":
            return default
        return float(text)
    except (TypeError, ValueError):
        return default


def _fee(row: dict, mapping: dict) -> float:
    """Total cost of the fill: commission plus stamp duty plus transfer fee.

    `row` is keyed by *field* (`fee`, `stamp`, `transfer`), not by the broker's
    column name — looking it up by column name silently returned 0 for every
    fill, which is the kind of error that only shows up as "why is my cost basis
    5 yuan lower than the broker's".
    """
    total = _to_float(row.get("fee"))
    for key in ("stamp", "transfer"):
        total += _to_float(row.get(key))
    return round(abs(total), 2)


def rows_from(frame: pd.DataFrame, mapping: dict, profile: dict) -> tuple[list[dict], list[str]]:
    kind = profile["kind"]
    side_values = {k.upper(): v for k, v in (profile.get("side_values") or {}).items()}
    rows: list[dict] = []
    skipped: list[str] = []
    for _, raw in frame.iterrows():
        row = {k: raw.get(col) for k, col in mapping.items()}
        raw_symbol = str(row.get("symbol") or "").strip()
        if raw_symbol in {"合计", "总计", "汇总"}:
            # A statement footer is not an unparsed holding. Treating it as a
            # skipped data row would make every otherwise complete export fail
            # the full-snapshot guard.
            continue
        symbol = normalize_symbol(raw_symbol)
        if not symbol:
            skipped.append(f"代码无法识别: {row.get('symbol')}")
            continue
        if kind == "positions":
            qty = int(_to_float(row.get("qty")))
            if qty == 0:
                continue
            rows.append({"symbol": symbol, "qty": qty,
                         "available": int(_to_float(row.get("available"), qty)),
                         "cost": _to_float(row.get("cost")),
                         "market_value": _to_float(row.get("market_value")),
                         "name": str(row.get("name") or "").strip()})
            continue

        side_raw = str(row.get("side") or "").strip().upper()
        side = side_values.get(side_raw) or normalize_side(side_raw)
        if not side:
            skipped.append(f"{symbol} 方向无法识别: {row.get('side')}")
            continue
        qty = abs(int(_to_float(row.get("qty"))))
        price = _to_float(row.get("price"))
        if qty <= 0 or price <= 0:
            skipped.append(f"{symbol} 数量/价格异常: {row.get('qty')} × {row.get('price')}")
            continue
        record = {
            "trade_date": normalize_date(row.get("trade_date")),
            "trade_time": str(row.get("trade_time") or "").strip(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "amount": round(abs(_to_float(row.get("amount"), qty * price)) or qty * price, 2),
            "fee": _fee(row, mapping),
            "broker_id": str(row.get("broker_id") or "").strip(),
        }
        record["trade_key"] = trade_key(record)
        rows.append(record)
    return rows, skipped


# ------------------------------------------------------------------ commands
def parse_text(text: str) -> dict:
    """A pasted block → rows, without touching the database.

    Split out of :func:`cmd_paste` so the desktop window can show "这里认出了 3
    只，市值 4,795.56，要写吗" before anything is committed. The window and the
    CLI must agree on what a paste means, so both go through this function.
    """
    known = known_headers()
    try:
        frame = _promote(_frame_from_text(text, known, "粘贴内容"), known, "粘贴内容")
    except Exception as exc:  # noqa: BLE001 - tell the user what the header was
        return {"ok": False, "message": str(exc)}

    picked = _pick_profile(frame)
    if picked is None:
        return {"ok": False, "message": "认不出这份表。表头是：" + str(list(frame.columns)[:12])}
    profile_name, profile, mapping = picked
    rows, skipped = rows_from(frame, mapping, profile)
    if not rows:
        return {"ok": False, "message": f"有表头但没有数据行；跳过 {len(skipped)} 行"}

    if profile["kind"] == "positions":
        rows = [{"symbol": row["symbol"], "name": row.get("name") or "",
                 "qty": int(row["qty"]), "cost": float(row.get("cost") or 0),
                 "market_value": float(row.get("market_value") or 0)} for row in rows]
    return {"ok": True, "kind": profile["kind"], "profile": profile_name,
            "rows": rows, "skipped": skipped[:5]}


def apply_parsed(store: ExternalStore, parsed: dict, *, source: str,
                 as_of: str | None = None) -> dict:
    """Write a :func:`parse_text` result. Positions replace the day, trades append."""
    rows = parsed.get("rows") or []
    if not rows:
        return {"ok": False, "message": "没有可写的行"}
    if parsed.get("kind") == "positions":
        skipped = parsed.get("skipped") or []
        if skipped:
            return {"ok": False, "saved": 0,
                    "message": "持仓表有未解析行，不能用部分快照替换完整账本：" +
                               "；".join(skipped)}
        day = normalize_date(as_of) or datetime.now(SHANGHAI).strftime("%Y%m%d")
        count = store.replace_snapshot(day, rows, source=source)
        store.log_import(file_name=f"{source}-{day}", file_sha="",
                         profile=str(parsed.get("profile") or "positions"),
                         rows=len(rows), inserted=count, duplicates=0, status="ok")
        return {"ok": True, "kind": "positions", "as_of": day, "saved": count,
                "message": f"持仓快照 {day}：{count} 只"}
    result = store.add_trades(rows, source=source)
    store.log_import(file_name=f"{source}-trades", file_sha="",
                     profile=str(parsed.get("profile") or "trades"),
                     rows=len(rows), inserted=result["inserted"],
                     duplicates=result["duplicates"], status="ok")
    return {"ok": True, "kind": "trades", **result,
            "message": f"成交 {len(rows)} 条：新增 {result['inserted']}，"
                       f"重复 {result['duplicates']}"}


def cmd_import(args) -> int:
    store = ExternalStore(DB)
    DROP.mkdir(parents=True, exist_ok=True)
    paths = [Path(p) for p in args.paths] if args.paths else sorted(
        p for p in DROP.glob("*") if p.suffix.lower() in EXTS)
    if not paths:
        print(f"[import] {DROP} 里没有新文件（支持 {', '.join(EXTS)}）")
        return 0

    total = {"inserted": 0, "duplicates": 0, "files": 0}
    for path in paths:
        try:
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            # A name ending in .csv that is actually a folder, a file still open in
            # Excel, a permission error: none of these used to be caught, and one
            # of them aborted the whole import batch with the remaining files
            # never even looked at.
            print(f"[skip] {path.name}: {type(exc).__name__}: {exc}")
            continue
        if store.seen_sha(sha) and not args.force:
            print(f"[skip] {path.name} 已经导入过（内容哈希相同）")
            continue
        try:
            frame = _read_table(path)
            picked = _pick_profile(frame)
            if picked is None:
                raise RuntimeError(
                    "没有匹配的列名配置。表头是：" + str(list(frame.columns)[:12]) +
                    "；请在 configs/broker_map.json 里补别名。")
            profile_name, profile, mapping = picked
            rows, skipped = rows_from(frame, mapping, profile)
        except Exception as exc:  # noqa: BLE001 - a bad file must not kill the batch
            store.log_import(file_name=path.name, file_sha=sha, profile="",
                             rows=0, inserted=0, duplicates=0,
                             status="error", note=str(exc)[:300])
            print(f"[error] {path.name}: {exc}")
            continue

        if args.dry_run:
            print(f"[dry] {path.name} profile={profile_name} rows={len(rows)} "
                  f"skipped={len(skipped)}")
            for row in rows[:3]:
                print("   ", {k: row[k] for k in list(row)[:7]})
            continue

        if profile["kind"] == "positions":
            if skipped:
                note = "持仓表有未解析行，拒绝用部分快照替换完整账本：" + "；".join(skipped)
                store.log_import(file_name=path.name, file_sha=sha, profile=profile_name,
                                 rows=len(rows), inserted=0, duplicates=0,
                                 status="error", note=note[:300])
                print(f"[error] {path.name}: {note}")
                continue
            as_of = normalize_date(args.as_of) or datetime.now(SHANGHAI).strftime("%Y%m%d")
            count = store.replace_snapshot(as_of, rows, source=path.name)
            store.log_import(file_name=path.name, file_sha=sha, profile=profile_name,
                             rows=len(rows), inserted=count, duplicates=0, status="ok")
            print(f"[ok] {path.name} → 持仓快照 {as_of}：{count} 只")
        else:
            result = store.add_trades(rows, source=path.name)
            store.log_import(file_name=path.name, file_sha=sha, profile=profile_name,
                             rows=len(rows), inserted=result["inserted"],
                             duplicates=result["duplicates"], status="ok")
            total["inserted"] += result["inserted"]
            total["duplicates"] += result["duplicates"]
            print(f"[ok] {path.name} → 成交 {len(rows)} 条：新增 {result['inserted']}，"
                  f"重复 {result['duplicates']}，跳过 {len(skipped)}")
        total["files"] += 1
        for note in skipped[:3]:
            print(f"   [skip] {note}")

    if total["files"]:
        print(f"[import] files={total['files']} inserted={total['inserted']} "
              f"duplicates={total['duplicates']}")
    return 0


def cmd_paste(args) -> int:
    """Import whatever is on the clipboard — the 同花顺 path.

    同花顺 keeps the account inside its own window: there is no file to watch and
    no API for a personal account. What it does have is a grid that copies as
    TSV, so the shortest reliable bridge is the clipboard. The user selects the
    rows once; everything after that is the same importer as a broker export.
    """
    store = ExternalStore(DB)
    text = args.text if args.text else _clipboard_text()
    known = known_headers()
    frame = _promote(_frame_from_text(text, known, "剪贴板"), known, "剪贴板")

    if args.profile:
        profile = _profiles().get(args.profile)
        if profile is None:
            print(f"[error] 没有名为 {args.profile} 的 profile；可选：{list(_profiles())}")
            return 2
        mapping = _resolve(frame, profile)
        if mapping is None:
            missing = [f for f in profile["required"] if f not in mapping]
            print(f"[error] profile={args.profile} 缺少字段 {missing}；"
                  f"实际表头：{list(frame.columns)[:12]}")
            return 2
        picked = (args.profile, profile, mapping)
    else:
        picked = _pick_profile(frame)
        if picked is None:
            print("[error] 认不出这份表。表头是：" + str(list(frame.columns)[:12]))
            print("        把这一行发出来就能加别名，不用改代码。")
            return 2

    profile_name, profile, mapping = picked
    rows, skipped = rows_from(frame, mapping, profile)
    if not rows:
        print(f"[error] profile={profile_name} 有表头但没有数据行；"
              f"跳过 {len(skipped)} 行：{skipped[:3]}")
        return 2

    head = "  ".join(f"{r['symbol']}×{r['qty']}" for r in rows[:4])
    print(f"[paste] profile={profile_name} rows={len(rows)} skipped={len(skipped)}")
    print(f"        {head}{' …' if len(rows) > 4 else ''}")

    if args.dry_run:
        print("[dry] 未写库。去掉 --dry-run 才真正导入。")
        return 0

    if profile["kind"] == "positions":
        if skipped:
            print("[error] 持仓表有未解析行，不能用部分快照替换完整账本："
                  + "；".join(skipped))
            return 2
        as_of = normalize_date(args.as_of) or datetime.now(SHANGHAI).strftime("%Y%m%d")
        count = store.replace_snapshot(as_of, rows, source=f"clipboard:{profile_name}")
        store.log_import(file_name=f"clipboard-{as_of}", file_sha="",
                         profile=profile_name, rows=len(rows), inserted=count,
                         duplicates=0, status="ok")
        print(f"[ok] 持仓快照 {as_of}：{count} 只（source=clipboard）")
        gap = reconcile(store, as_of=as_of)
        if gap.get("ok"):
            print("[ok] 对账一致：流水算出的持仓 = 券商快照")
        else:
            print(f"[warn] 对账有 {len(gap.get('mismatches') or [])} 处差异"
                  f"（分红/转股/漏导？以快照为准）：")
            for item in (gap.get("mismatches") or [])[:5]:
                print(f"   {item['symbol']} 流水 {item['ledger_qty']} vs 快照 "
                      f"{item['broker_qty']}（差 {item['delta']:+d}）")
        return 0

    result = store.add_trades(rows, source=f"clipboard:{profile_name}")
    store.log_import(file_name="clipboard-trades", file_sha="", profile=profile_name,
                     rows=len(rows), inserted=result["inserted"],
                     duplicates=result["duplicates"], status="ok")
    print(f"[ok] 成交 {len(rows)} 条：新增 {result['inserted']}，"
          f"重复 {result['duplicates']}（按内容哈希去重，重复粘贴不会翻倍）")
    return 0


def _instrument_for(symbol: str, day: str):
    """The security's own fee/trading rules, or None when the store cannot say.

    Resolving this is what makes "bought an ETF" pay no stamp duty. It is allowed
    to fail: recording a fill must never depend on the market-data store being
    readable, and the fallback (a stock) is stated in the output rather than
    silently assumed.
    """
    try:
        from service.trading import build_desk

        return build_desk(ROOT).market.instrument(symbol, day)
    except Exception:  # noqa: BLE001 - the ledger outranks the fee refinement
        return None


def cmd_add(args) -> int:
    symbol = normalize_symbol(args.symbol)
    if not symbol:
        print(f"[fail] 代码无法识别：{args.symbol}")
        return 2
    side = normalize_side(args.side)
    if not side:
        print(f"[fail] 方向无法识别：{args.side}（用 BUY/SELL 或 买入/卖出）")
        return 2
    qty = int(args.qty)
    price = float(args.price)
    if qty <= 0:
        print(f"[fail] 数量要大于 0：{args.qty}")
        return 2
    if not math.isfinite(price) or price <= 0:
        print(f"[fail] 价格要大于 0：{args.price}")
        return 2
    if args.fee is not None and (not math.isfinite(float(args.fee)) or float(args.fee) < 0):
        print(f"[fail] 费用不能小于 0：{args.fee}")
        return 2

    day = normalize_date(args.date) or datetime.now(SHANGHAI).strftime("%Y%m%d")
    # One code path for "record a fill": `manual.apply_trade` computes the fee,
    # updates the holding and the cash line, and rolls all of it back together.
    # This subcommand used to insert a trade row and nothing else — so the
    # position stayed where it was, and AGENTS.md's "佣金/印花税/过户费自动算、
    # 持仓与可用资金同时更新" described a module no entry point called.
    from service.trading.manual import apply_trade

    result = apply_trade(
        ExternalStore(DB),
        {"trade_date": day, "trade_time": args.time or "", "symbol": symbol,
         "side": side, "qty": qty, "price": price,
         "fee": args.fee, "broker_id": args.broker_id or ""},
        source="manual",
        instrument=_instrument_for(symbol, day),
    )
    if not result.get("ok"):
        print(f"[fail] {result.get('message')}")
        return 2
    if result.get("duplicate"):
        print(f"[dup] {result.get('message')}")
        return 0
    print(f"[ok] 已记：{result['message']}")
    return 0


def _quotes(symbols: list[str]) -> dict[str, dict]:
    from service.trading import build_desk

    desk = build_desk(ROOT)
    out: dict[str, dict] = {}
    for symbol in symbols:
        try:
            snap = desk.market.snapshot(symbol)
        except Exception:  # noqa: BLE001 - one bad symbol must not blank the report
            snap = None
        if snap is None:
            out[symbol] = {}
            continue
        # The band depends on the short name (未股改股恒为 5%) and on the ST flag
        # (主板 ST 自 2026-07-06 起为 10%). The quote dict used to hard-code
        # ``st: False``, so an ST holding in the real account was monitored with
        # the wrong ceiling in both directions.
        inst = desk.market.instrument(symbol, snap.trade_date)
        out[symbol] = {"last": float(snap.last), "prev_close": float(snap.pre_close),
                       "up_limit": float(snap.up_limit), "down_limit": float(snap.down_limit),
                       "trade_date": snap.trade_date, "st": bool(inst.is_st),
                       "name": str(inst.name or "")}
    return out


def cmd_report(args) -> int:
    store = ExternalStore(DB)
    held = store.holdings()
    if not held:
        print("[report] 还没有任何成交记录。先 `python scripts/external.py import`，"
              "或用 `add` 手工记一笔。")
        return 0

    quotes = _quotes(sorted(held))
    first_buy = {}
    for trade in store.trades():
        if trade["side"] == "BUY":
            seen = first_buy.get(trade["symbol"])
            if seen is None or trade["trade_date"] < seen:
                first_buy[trade["symbol"]] = trade["trade_date"]

    rows = []
    unpriced: list[str] = []
    unpriced_holdings: list[dict] = []
    realized_total = 0.0
    for symbol, position in sorted(held.items()):
        realized_total += position.realized
        if position.qty <= 0:
            # 已清仓的行不再算持仓：0 股却出现在持仓表里会触发"破止损"，
            # 而那个告警指向的是一笔已经不存在的仓位（P0-4，2026-11-02）。
            continue
        quote = quotes.get(symbol) or {}
        last = quote.get("last")
        if not last:
            # **不再回落到成本价**。成本价和现价在 13.617 vs 13.62 这种量级上
            # 几乎重合，读的人分不出"行情没取到"和"今天没动"，而止损距离会
            # 锚到一个不存在的价位上。取不到就说取不到。
            #
            # 但**行不能消失**：之前这里直接 continue，持仓整行从表里掉出去，
            # 面板只剩「市值 0 成本 0」，而行情在 15:00–17:30 这段必然取不到
            # （日线要等 17:30 落库）。那 2.5 小时里，一个真实持仓读起来和空仓
            # 一模一样。把股数与成本单独列出来，市值留空，比让行消失安全。
            unpriced.append(symbol)
            unpriced_holdings.append({
                "symbol": symbol,
                "qty": position.qty,
                "cost": round(position.cost, 3),
                "cost_value": round(position.cost * position.qty, 2),
            })
            continue
        prev_close = quote.get("prev_close") or last
        opened_today = (first_buy.get(symbol) == (quote.get("trade_date") or ""))
        level = levels(symbol=symbol, qty=position.qty, cost=position.cost,
                       prev_close=prev_close, last=last, entry=position.avg_price,
                       opened_today=opened_today, st=bool(quote.get("st")),
                       auction_anchor=last, name=quote.get("name"))
        if quote.get("up_limit"):
            level["up_limit"] = round(quote["up_limit"], 2)
            level["down_limit"] = round(quote["down_limit"], 2)
        level["name"] = quote.get("name") or ""
        level["quote_date"] = quote.get("trade_date")
        level["price_source"] = "quote"
        level["realized"] = round(position.realized, 2)
        level["fees"] = round(position.fees, 2)
        rows.append(level)

    day = datetime.now(SHANGHAI).strftime("%Y%m%d")
    marketed = sum(r["market_value"] for r in rows)
    cost_value = sum(r["cost"] * r["qty"] for r in rows)
    pnl_today = sum(r["pnl_today"] for r in rows)
    pnl_total = sum(r["pnl_total"] for r in rows)
    breaches = [r for r in rows if r["breach_stop"] or r["breach_auction"]]

    payload = {
        "as_of": day,
        "mode": "preopen" if args.preopen else "close",
        "positions": rows,
        "market_value": round(marketed, 2),
        "cost_value": round(cost_value, 2),
        "pnl_today": round(pnl_today, 2),
        "pnl_total": round(pnl_total, 2),
        "realized_total": round(realized_total, 2),
        "pnl_total_pct": round(pnl_total / cost_value * 100, 2) if cost_value else None,
        "breaches": [r["symbol"] for r in breaches],
        # 市值/盈亏是**已定价部分**的合计。有取不到价的票时这两个数不是全账户的，
        # 而"部分合计"和"全账户"在纸上长得一样，所以必须说出来。
        "priced": len(rows),
        "unpriced": unpriced,
        "unpriced_holdings": unpriced_holdings,
        "price_source": ("unavailable" if rows == [] and unpriced
                         else "partial" if unpriced else "quote"),
        "totals_partial": bool(unpriced),
    }

    REPORTS.mkdir(parents=True, exist_ok=True)
    # Both files or neither: a reader that finds the .md but not the .json (or a
    # half-written pair) has no way to tell an interrupted run from a quiet day.
    write_json_atomic(REPORTS / f"{day}.json", payload)
    write_text_atomic(REPORTS / f"{day}.md", _render_markdown(payload))

    print(_render_console(payload))
    if args.reconcile:
        gap = reconcile(store)
        if not gap["ok"]:
            print("\n⚠️  成交流水与持仓快照不一致：")
            for row in gap["mismatches"]:
                print(f"   {row['symbol']} 流水 {row['ledger_qty']} vs 快照 {row['broker_qty']}"
                      f"（差 {row['delta']:+d}）")
            print(f"   {gap['note']}")
    return 0


def _num(value, spec: str, dash: str = "—") -> str:
    """Render a number that may be missing.

    A None here is not a zero: printing "0.00" for an unknown stop would read as
    a real price level, and the desk would then argue about a level that does not
    exist. A dash says "we don't know", which is the actual state.
    """
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return dash


def _render_console(payload: dict) -> str:
    held_unpriced = payload.get("unpriced_holdings") or []
    if held_unpriced and not payload["positions"]:
        # 全账户未定价时，绝不打印「市值 0 成本 0」：那和空仓长得一样。
        totals = (f"市价不可用：{len(held_unpriced)} 只持仓全部未定价，"
                  f"市值 / 盈亏 / 当日都算不出来（不是 0）")
    else:
        totals = (f"市值 {payload['market_value']:,.0f}  成本 {payload['cost_value']:,.0f}  "
                  f"当日 {payload['pnl_today']:+,.0f}  累计 {payload['pnl_total']:+,.0f}"
                  + (f" ({payload['pnl_total_pct']:+.2f}%)" if payload['pnl_total_pct'] is not None else ""))
    lines = [f"实盘（只读同步） {payload['as_of']} · {payload['mode']}", totals]
    for row in payload["positions"]:
        flag = ""
        if row["breach_stop"]:
            flag = "  ⚠️ 破止损"
        elif row["breach_auction"]:
            flag = "  ⚠️ 破竞价线"
        lines.append(
            f"  {row['symbol']} {row['qty']}股 现价 {_num(row['last'], '.2f')} "
            f"成本 {_num(row['cost'], '.3f')} 盈亏 {_num(row['pnl_total'], '+.0f')}"
            f"（{_num(row['pnl_total_pct'], '+.2f')}%） 止损 {_num(row['stop'], '.2f')} "
            f"竞价线 {_num(row['auction_line'], '.2f')}{flag}")
    for row in payload.get("unpriced_holdings") or []:
        lines.append(
            f"  {row['symbol']} {row['qty']}股 现价 未取到 成本 {row['cost']:.3f} "
            f"成本额 {row['cost_value']:,.0f}  未计入市值 · 无止损距离可算")
    if payload["breaches"]:
        lines.append(f"⚠️  触发纪律：{', '.join(payload['breaches'])}")
    if payload.get("unpriced"):
        lines.append(
            f"⚠️  {len(payload['unpriced'])} 只取不到现价（{', '.join(payload['unpriced'])}）："
            f"上面的市值/盈亏只含已定价的 {payload['priced']} 只，**不是全账户**。"
            f"不用成本价顶替——那会把止损锚到一个不存在的价位。")
    return "\n".join(lines)


def _render_markdown(payload: dict) -> str:
    head = (f"# 实盘同步 {payload['as_of']}\n\n"
            f"市值 **{payload['market_value']:,.0f}** · 成本 {payload['cost_value']:,.0f} · "
            f"当日 {payload['pnl_today']:+,.0f} · 累计 {payload['pnl_total']:+,.0f}"
            + (f"（{payload['pnl_total_pct']:+.2f}%）" if payload['pnl_total_pct'] is not None else "")
            + "\n\n")
    table = ("| 代码 | 股数 | 现价 | 成本 | 今日 | 累计 | 止损 | 竞价线 | 涨停 | 跌停 |\n"
             "|---|---|---|---|---|---|---|---|---|---|\n")
    for row in payload["positions"]:
        table += (f"| {row['symbol']} | {row['qty']} | {_num(row['last'], '.2f')} | "
                  f"{_num(row['cost'], '.3f')} | "
                  f"{_num(row['pnl_today'], '+.0f')} | {_num(row['pnl_total'], '+.0f')} | "
                  f"{_num(row['stop'], '.2f')} | "
                  f"{_num(row['auction_line'], '.2f')} | {_num(row['up_limit'], '.2f')} | "
                  f"{_num(row['down_limit'], '.2f')} |\n")
    for row in payload.get("unpriced_holdings") or []:
        table += (f"| {row['symbol']} | {row['qty']} | — | {row['cost']:.3f} | "
                  f"— | — | — | — | — | — |\n")
    tail = ""
    if payload.get("unpriced"):
        tail += (f"\n**取不到现价**：{', '.join(payload['unpriced'])}。"
                 f"上面的合计只含已定价的 {payload['priced']} 只，不是全账户；"
                 f"这两只没有止损距离可算。\n")
    if payload["breaches"]:
        tail += f"\n**纪律触发**：{', '.join(payload['breaches'])}\n"
    return head + table + tail + \
        "\n> 止损以买入成本为锚，竞价线以前收盘为锚；两者用不同基准是有意的。\n" \
        "> 本文件由 `scripts/external.py report` 生成，只读账户，不下单。\n"


def cmd_forget(args) -> int:
    store = ExternalStore(DB)
    keep = [row for row in store.trades() if str(row.get("source") or "") != args.source]
    drop = len(store.trades()) - len(keep)
    if drop == 0:
        print(f"[ok] 没有 source={args.source} 的成交")
        return 0
    if not args.yes:
        print(f"[dry] 会删除 {drop} 条 source={args.source} 的成交；加 --yes 执行")
        for row in store.trades():
            if str(row.get("source") or "") == args.source:
                print(f"   {row['trade_date']} {row['symbol']} {row['side']} "
                      f"{row['qty']} × {row['price']}")
        return 0
    removed = store.forget(source=args.source)
    print(f"[ok] 已删除 {removed} 条 source={args.source} 的成交")
    return 0


def cmd_status(args) -> int:
    store = ExternalStore(DB)
    held = store.holdings()
    snap = store.snapshot()
    print(f"数据库 {DB}")
    print(f"成交 {len(store.trades())} 条 · 持仓 {len(held)} 只 · "
          f"快照 {len(snap)} 只 · 导入 {len(store.imports(50))} 次")
    for symbol, position in sorted(held.items()):
        print(f"  {symbol} {position.qty} 股 成本 {position.cost:.4f} "
              f"已实现 {position.realized:+.2f} 费用 {position.fees:.2f}")
    if snap:
        day = max(r["as_of"] for r in snap.values())
        print(f"  最近快照 {day}：{len(snap)} 只")
    gap = reconcile(store)
    print("  对账：" + ("一致" if gap["ok"] else f"{len(gap['mismatches'])} 处不一致"))
    for row in store.imports(5):
        print(f"  [{row['status']}] {row['imported_at']} {row['file_name']} "
              f"rows={row['rows']} +{row['inserted']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实盘账户只读同步")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser("import", help="导入券商导出的成交/持仓文件")
    p_import.add_argument("paths", nargs="*")
    p_import.add_argument("--dry-run", action="store_true")
    p_import.add_argument("--force", action="store_true", help="同一个文件重复导入")
    p_import.add_argument("--as-of", dest="as_of", default="")
    p_import.set_defaults(func=cmd_import)

    p_paste = sub.add_parser("paste", help="直接从剪贴板导入（同花顺/券商客户端里 Ctrl+A Ctrl+C）")
    p_paste.add_argument("--dry-run", action="store_true", help="只解析，不写库")
    p_paste.add_argument("--profile", default="", help="trades / positions，默认自动识别")
    p_paste.add_argument("--as-of", dest="as_of", default="", help="持仓快照日期，默认今天")
    p_paste.add_argument("--text", default="", help="直接给文本，不读剪贴板（测试用）")
    p_paste.set_defaults(func=cmd_paste)

    p_add = sub.add_parser("add", help="手工记一笔成交（还没有导出文件时用）")
    p_add.add_argument("--symbol", required=True)
    p_add.add_argument("--side", required=True)
    p_add.add_argument("--qty", type=int, required=True)
    p_add.add_argument("--price", type=float, required=True)
    p_add.add_argument("--date", default="")
    p_add.add_argument("--time", default="")
    # Omit it and the fee is computed from the broker schedule and the
    # security's own rules; pass it to match a statement to the fen.
    p_add.add_argument("--fee", type=float, default=None)
    p_add.add_argument("--broker-id", dest="broker_id", default="")
    p_add.set_defaults(func=cmd_add)

    p_report = sub.add_parser("report", help="盈亏与纪律位")
    p_report.add_argument("--preopen", action="store_true", help="盘前：只看竞价线")
    p_report.add_argument("--reconcile", action="store_true", help="同时核对持仓快照")
    p_report.set_defaults(func=cmd_report)

    p_forget = sub.add_parser("forget", help="删除某个来源的成交（导真实交割单前清手工记录）")
    p_forget.add_argument("--source", default="manual")
    p_forget.add_argument("--yes", action="store_true")
    p_forget.set_defaults(func=cmd_forget)

    p_status = sub.add_parser("status", help="账本与导入历史")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
