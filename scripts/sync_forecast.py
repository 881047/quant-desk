"""按公告日同步业绩预告 / 业绩快报（全市场，一次调用一天）。

Both Tushare APIs accept ``ann_date`` without ``ts_code``, so this is cheap:
~30 calls for a month of disclosure history. The two datasets stay out of the
default backfill groups (see ``catalog.py``) because a day with no forecast is
normal, not a failed ingest.

    python scripts/sync_forecast.py --since 20261001 --dry-run
    python scripts/sync_forecast.py --since 20261001

Point-in-time: rows are keyed by (ts_code, ann_date, end_date); readers cut on
``ann_date <= as_of``. A forecast is a *prediction*, never a reported result —
``profile.events.guidance`` labels it as such.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from service.adapters.parquet_store import DatasetStore  # noqa: E402
from service.adapters.sqlite_store import Registry  # noqa: E402
from service.adapters.tushare_adapter import TushareAdapter  # noqa: E402

CURSOR = ROOT / "data" / "state" / "sync_forecast.json"
PK = ["ts_code", "ann_date", "end_date"]
APIS = ("forecast", "express")


def _days(since: str, until: str) -> list[str]:
    start, end = pd.Timestamp(since), pd.Timestamp(until)
    return [d.strftime("%Y%m%d") for d in pd.date_range(start, end, freq="D")]


def fetch(adapter: TushareAdapter, api: str, days: list[str]) -> pd.DataFrame:
    frames = []
    for day in days:
        try:
            frame = adapter.call_paged(api, ann_date=day)
        except Exception as exc:  # noqa: BLE001 - a quiet day must not abort the sweep
            print(f"[warn] {api} {day}: {type(exc).__name__}: {exc}", flush=True)
            continue
        if frame is not None and not frame.empty:
            frames.append(frame)
    frames = [f.dropna(axis=1, how="all") for f in frames]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def merge(store: DatasetStore, api: str, new: pd.DataFrame) -> dict:
    try:
        old = store.read(api)
    except (KeyError, FileNotFoundError, ValueError):
        old = None
    if old is not None and not old.empty:
        frame = pd.concat([old, new.reindex(columns=old.columns)], ignore_index=True)
    else:
        frame = new
    frame = frame.drop_duplicates(subset=PK, keep="last").reset_index(drop=True)
    as_of = str(frame["ann_date"].astype(str).max())
    store.write(api, "all", frame, as_of=as_of)
    return {"rows": int(len(frame)), "as_of": as_of}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按公告日同步业绩预告/快报")
    ap.add_argument("--since", default=(datetime.now() - timedelta(days=45)).strftime("%Y%m%d"))
    ap.add_argument("--until", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    days = _days(args.since, args.until)
    print(f"[start] {len(days)} days, apis={APIS}, dry_run={args.dry_run}", flush=True)

    adapter = TushareAdapter(token=os.getenv("TUSHARE_TOKEN"))
    store = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
    summary: dict = {"since": args.since, "until": args.until,
                     "last_run": datetime.now().isoformat(timespec="seconds")}
    for api in APIS:
        fresh = fetch(adapter, api, days)
        print(f"[{api}] rows={len(fresh)}", flush=True)
        if fresh.empty or args.dry_run:
            if args.dry_run and not fresh.empty:
                print(fresh[["ts_code", "ann_date", "end_date"]].head(5).to_string(index=False))
            continue
        out = merge(store, api, fresh)
        summary[api] = out
        print(f"[{api}] written rows={out['rows']} as_of={out['as_of']}", flush=True)

    CURSOR.parent.mkdir(parents=True, exist_ok=True)
    CURSOR.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
