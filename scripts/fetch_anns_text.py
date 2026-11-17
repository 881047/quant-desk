"""Download cninfo PDFs and cache their text — the "公告原文" half of the gap.

Titles tell you a filing exists; the text tells you whether it matters. The
difference is concrete: 「减持计划」 and 「减持计划实施完毕」 are the same category
and opposite facts, and only the body separates them.

Why a cache and not a column: an annual report is 200+ pages and ~500 KB of prose.
Rewriting the announcement parquet — 230k rows — to append 12,000 characters per
filing would cost far more than the text is worth. Text lives in
``data/cache/anns_text/<announcement_id>.json``, is written once, and the profile
quotes from it when present.

Usage::

    python scripts/fetch_anns_text.py --symbol 600519.SH --limit 5
    python scripts/fetch_anns_text.py --recent-days 7 --limit 40
    python scripts/fetch_anns_text.py --symbol 000001.SZ --limit 3 --force

``--recent-days`` walks the market: every filing dated in the window, newest
first, capped by ``--limit``. That is the shape a morning scan wants — the
question is not "what did this company file" but "what was filed last night".
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

# The cache layout, the reader and the downloader live in the service layer:
# `service/adapters/anns_text.py`. They used to live here, with
# `market.announcement_text()` composing the same path a second time — two
# implementations of one fact, which is how the read and the write end up
# disagreeing without either one raising.
from service.adapters.anns_text import (
    MAX_PAGES,
    fetch_one,
)
from service.adapters.cninfo_adapter import CninfoAdapter
from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry


def load_announcements(store: DatasetStore, *, symbols: list[str] | None,
                       recent_days: int | None, limit: int) -> list[dict]:
    """Rows to fetch, newest first, never more than ``limit`` per symbol."""
    try:
        frame = store.read("anns")
    except (KeyError, FileNotFoundError, ValueError):
        print("[fatal] anns 尚未回填，先跑 scripts/backfill.py --datasets anns", flush=True)
        return []
    if frame is None or frame.empty:
        return []
    frame = frame.copy()
    frame["pub_date"] = frame["pub_date"].astype(str)

    if recent_days is not None:
        # Calendar arithmetic on a YYYYMMDD string is wrong at month boundaries;
        # a Timestamp is not.
        cutoff = pd.Timestamp(datetime.now().strftime("%Y%m%d"))
        start = (cutoff - pd.Timedelta(days=recent_days)).strftime("%Y%m%d")
        frame = frame[frame["pub_date"] >= start]

    if symbols:
        upper = {s.upper() for s in symbols}
        frame = frame[frame["ts_code"].astype(str).isin(upper)]

    frame = frame.sort_values(["pub_date", "announcement_id"], ascending=[False, False])
    if symbols:
        frame = frame.groupby("ts_code", sort=False).head(limit)
    else:
        frame = frame.head(limit)
    return frame.to_dict("records")


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="cache cninfo announcement text")
    parser.add_argument("--symbol", action="append", default=None,
                        help="repeatable, e.g. --symbol 600519.SH")
    parser.add_argument("--recent-days", type=int, default=None,
                        help="every filing dated within N days (market-wide)")
    parser.add_argument("--limit", type=int, default=5,
                        help="per symbol with --symbol, otherwise total (default 5)")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--force", action="store_true", help="refetch cached text")
    args = parser.parse_args(argv)

    if not args.symbol and args.recent_days is None:
        parser.error("give --symbol or --recent-days")

    store = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
    rows = load_announcements(store, symbols=args.symbol,
                              recent_days=args.recent_days, limit=args.limit)
    if not rows:
        print("[done] nothing to fetch")
        return 0

    client = CninfoAdapter()
    counts: dict[str, int] = {}
    chars = 0
    for i, row in enumerate(rows, 1):
        res = fetch_one(client, row, ROOT / "data", force=args.force,
                        max_pages=args.max_pages)
        key = res["status"].split(":")[0]
        counts[key] = counts.get(key, 0) + 1
        chars += res["chars"] or 0
        if key not in ("cached", "ok"):
            print(f"  {res['status']:24s} {row['ts_code']} {str(row['title'])[:40]}",
                  flush=True)
        if i % 20 == 0:
            print(f"  ... {i}/{len(rows)}", flush=True)

    print(f"[done] {counts} | chars={chars:,} | pdf_calls={client.calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
