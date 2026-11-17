"""Fallback text collector: EastMoney headlines via akshare.

    python scripts/fetch_text.py                       # holdings + liquid names
    python scripts/fetch_text.py --symbols 002966,600000
    python scripts/fetch_text.py --top 30 --per 10

WorkBuddy is the primary source: it can read announcements and research notes, and
it can summarise. This script exists so a day where WorkBuddy did not run is not a
day with zero text. It only sees headline news, never the filing itself, and it is
marked ``tier=media`` so the model knows the difference.

Writes through ``service.adapters.text.ingest`` — the same validation path as the
WorkBuddy inbox, so nothing bypasses the clock.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.adapters.text import ingest, to_ts_code
from service.kernel.clock import SHANGHAI
from service.trading import build_desk


def _holdings(desk) -> list[str]:
    return [p["symbol"] for p in desk.book.summary()["positions"]]


def _liquid(desk, day: str, top: int) -> list[str]:
    adv = desk.market.adv_map(day)
    if adv.empty:
        return []
    codes = [c for c in adv.sort_values(ascending=False).head(top * 3).index
             if not str(c).endswith(".BJ")]
    return codes[:top]


def fetch(symbols: list[str], per: int) -> list[dict]:
    import akshare as ak

    records: list[dict] = []
    failures = 0
    for symbol in symbols:
        digits = symbol.split(".")[0]
        try:
            frame = ak.stock_news_em(symbol=digits)
        except Exception as exc:  # noqa: BLE001 - report every bad symbol, then fail the run
            failures += 1
            print(f"  {symbol}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if frame is None or frame.empty:
            continue
        for _, row in frame.head(per).iterrows():
            records.append({
                "ts_code": symbol,
                "title": row.get("新闻标题"),
                "summary": row.get("新闻内容"),
                "pub_time": row.get("发布时间"),
                "source": row.get("文章来源") or "东财",
                "url": row.get("新闻链接"),
                "kind": "news",
                "tier": "media",
            })
    if failures:
        raise RuntimeError(f"{failures} symbol fetches failed")
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="", help="comma separated ts_codes")
    parser.add_argument("--top", type=int, default=0, help="also take top-N by turnover")
    parser.add_argument("--per", type=int, default=10, help="headlines per symbol")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.top < 0 or args.per <= 0:
        print("[fetch_text] --top must be >= 0 and --per must be positive", file=sys.stderr)
        return 2

    try:
        desk = build_desk(ROOT)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"[fetch_text] desk setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    day = desk.market.latest_bar_date()
    if not day:
        print("[fetch_text] no daily bars; nothing to anchor text to")
        return 1

    symbols = [to_ts_code(s) for s in args.symbols.split(",") if s.strip()]
    symbols += [s for s in _holdings(desk) if s not in symbols]
    if args.top > 0:
        symbols += [s for s in _liquid(desk, day, args.top) if s not in symbols]
    if not symbols:
        print("[fetch_text] no symbols: pass --symbols or --top")
        return 1

    print(f"[fetch_text] symbols={len(symbols)} per={args.per}")
    try:
        records = fetch(symbols, args.per)
    except Exception as exc:  # noqa: BLE001 - vendor failure must fail the scheduler
        print(f"[fetch_text] fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"[fetch_text] raw records={len(records)}")
    if args.dry_run or not records:
        for record in records[:3]:
            print("  ", record.get("pub_time"), record.get("title"))
        return 0

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    try:
        result = ingest(store, records, as_of=datetime.now(SHANGHAI).strftime("%Y%m%d"),
                        source="akshare", trust_tier="T2", default_kind="news")
    except Exception as exc:  # noqa: BLE001 - a failed write is not a successful collection
        print(f"[fetch_text] ingest failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"[fetch_text] written={result['written']} rejected={result['rejected']} "
          f"dates={result.get('by_date')} kinds={result.get('kinds')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
