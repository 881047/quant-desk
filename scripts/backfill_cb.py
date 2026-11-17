"""Backfill convertible-bond bars — the T+0 instrument the P5 result points to.

    python scripts/backfill_cb.py --start 20230101 --end 20261023
    python scripts/backfill_cb.py --basic-only

Convertible bonds trade T+0 and have no stamp duty, so the intraday reversal
that a stock account cannot harvest is legally available here. This script only
loads the data; the referee decides whether the effect is real.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import tushare as ts

from service.adapters.parquet_store import DatasetStore
from service.adapters.pit import TradingCalendarStore
from service.adapters.sqlite_store import Registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="20230101")
    parser.add_argument("--end", default="20261023")
    parser.add_argument("--basic-only", action="store_true")
    parser.add_argument("--rate", type=int, default=200, help="calls per minute")
    args = parser.parse_args(argv)
    try:
        start = datetime.strptime(args.start, "%Y%m%d").strftime("%Y%m%d")
        end = datetime.strptime(args.end, "%Y%m%d").strftime("%Y%m%d")
        if start != args.start or end != args.end or start > end:
            raise ValueError
    except ValueError:
        print("[fail] --start/--end must be YYYYMMDD with start <= end")
        return 2
    if args.rate <= 0:
        print("[fail] --rate must be positive")
        return 2

    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        print("[fail] TUSHARE_TOKEN missing")
        return 2
    pro = ts.pro_api(token)
    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)

    try:
        basic = pro.cb_basic()
        if basic is None or basic.empty:
            print("[fail] cb_basic empty (permission?)")
            return 2
        store.write("cb_basic", "all", basic, as_of=args.end, mode="replace")
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"[fail] cb_basic: {type(exc).__name__}: {exc}")
        return 2
    print(f"[basic] {len(basic)} convertible bonds")
    if args.basic_only:
        return 0

    calendar = TradingCalendarStore.from_frame(store.read("trade_cal"))
    days = [d for d in calendar.days if args.start <= d <= args.end]
    if not days:
        print("[fail] no trading days in the requested range")
        return 2
    interval = 60.0 / args.rate
    written = 0
    failures = 0
    failed_years: set[str] = set()
    last = 0.0
    by_year: dict[str, list] = {}
    for day in days:
        wait = interval - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()
        year = day[:4]
        try:
            df = pro.cb_daily(trade_date=day)
        except Exception as exc:  # noqa: BLE001 - vendor throttle
            failures += 1
            failed_years.add(year)
            print(f"[warn] {day}: {type(exc).__name__}: {str(exc)[:70]}")
            try:
                registry.mark_ingest("backfill_cb", "cb_daily", year, "failed",
                                     cursor=day, rows=written, error=str(exc)[:500])
            except Exception as checkpoint_exc:  # noqa: BLE001
                print(f"[fail] checkpoint {day}: {checkpoint_exc}")
                return 2
            continue
        if df is not None and not df.empty:
            by_year.setdefault(year, []).append(df)
            written += len(df)
        try:
            registry.mark_ingest("backfill_cb", "cb_daily", year, "running",
                                 cursor=day, rows=written)
        except Exception as exc:  # noqa: BLE001 - an uncheckpointed run is not resumable
            print(f"[fail] checkpoint {day}: {type(exc).__name__}: {exc}")
            return 2
        if df is not None and not df.empty and written % 20000 < len(df):
            print(f"[progress] {day} rows={written}")
    import pandas as pd

    for year, frames in sorted(by_year.items()):
        # A failed *day* used to discard every row fetched for that whole year,
        # so one network blip threw away 200+ successful calls and the retry had
        # to start over. Write what was fetched and let the checkpoint carry the
        # failure: the partition is stamped warn/failed below, which is the
        # honest report, and `mode="merge"` makes the retry additive.
        partial = year in failed_years
        frame = pd.concat(frames, ignore_index=True)
        try:
            store.write("cb_daily", year, frame, as_of=days[-1], mode="merge",
                        note=f"cb_daily {year}")
            registry.mark_ingest(
                "backfill_cb", "cb_daily", year,
                "warn" if partial else "done",
                cursor=max(d for d in days if d.startswith(year)), rows=len(frame))
        except Exception as exc:  # noqa: BLE001 - partial partitions must not report success
            failures += 1
            print(f"[fail] write {year}: {type(exc).__name__}: {exc}")
            try:
                registry.mark_ingest("backfill_cb", "cb_daily", year, "failed",
                                     error=str(exc)[:500])
            except Exception as checkpoint_exc:  # noqa: BLE001
                print(f"[fail] checkpoint {year}: {checkpoint_exc}")
            continue
        print(f"[write] {year}: {len(frame)} rows")
    print(f"[done] rows={written} failures={failures} days={len(days)}")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
