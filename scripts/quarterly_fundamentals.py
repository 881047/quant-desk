"""按公告日增量同步 ``fina_indicator``（季度型数据，不进 17:30 日更）。

Why this exists
---------------
``catalog.py`` deliberately keeps ``fina_indicator`` out of the daily job: the
token refuses a period-only query, so the only shape that works is one call per
``ts_code``. That is fine for a full backfill but means a freshly disclosed
quarter never arrives on its own. On 2026-11-10 the manifest was still stamped
``2026-10-28`` while the market had already published 2026Q3 on 10-31, so
``profile.fundamentals.latest_period`` sat at 20260630 (79 days stale) even
though the ``fundamentals`` coverage dimension scored 1.0.

Candidate source is ``stock_basic``, not announcement metadata: on 2026-11-10
``anns`` had no 002185.SZ announcement after 10-20 even though its Q3 report
was published on 10-31. Filtering candidates by announcement titles silently
dropped the very name we were trying to fix. A cursor walks the full listed
universe in bounded batches, so every symbol is eventually reached.

    python scripts/quarterly_fundamentals.py --since 20261029 --dry-run
    python scripts/quarterly_fundamentals.py --since 20261029 --symbols 002185.SZ
    python scripts/quarterly_fundamentals.py --since 20261029 --limit 400
    python scripts/quarterly_fundamentals.py --since 20261029 --limit 0  # full sweep

Point-in-time is preserved: rows are keyed by (ts_code, end_date, ann_date) and
readers still filter on ``ann_date <= as_of``. A later restatement of the same
period keeps its own ann_date instead of overwriting the original.
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

# Never override: this .env keeps an empty TUSHARE_TOKEN placeholder while the
# real credential arrives from the process environment.
load_dotenv(ROOT / ".env")

from service.adapters.parquet_store import DatasetStore  # noqa: E402
from service.adapters.sqlite_store import Registry  # noqa: E402
from service.adapters.tushare_adapter import TushareAdapter  # noqa: E402

CURSOR = ROOT / "data" / "state" / "quarterly_fundamentals.json"
PK = ["ts_code", "end_date", "ann_date"]


def _cursor() -> dict:
    if CURSOR.exists():
        try:
            return json.loads(CURSOR.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def _default_since() -> str:
    return str(_cursor().get("since") or
               (datetime.now() - timedelta(days=30)).strftime("%Y%m%d"))


def next_batch(store: DatasetStore, limit: int, last_code: str = "") -> list[str]:
    """The next slice of listed symbols, in stable ts_code order."""
    try:
        basic = store.read("stock_basic")
    except (KeyError, FileNotFoundError, ValueError):
        return []
    if basic is None or basic.empty:
        return []
    live = basic[basic["list_status"].astype(str) == "L"] if "list_status" in basic.columns else basic
    codes = sorted(live["ts_code"].astype(str).unique())
    todo = [c for c in codes if c > last_code]
    return todo[:limit] if limit else todo


def fetch_new(adapter: TushareAdapter, codes: list[str], since: str) -> pd.DataFrame:
    """Fetch recent reporting periods per symbol, then keep only new announcements.

    ``start_date``/``end_date`` on this API are *reporting periods*, not
    announcement dates, so the window covers two years and the ann_date filter
    does the incremental part. One failed symbol never aborts the run.
    """
    year = int(since[:4])
    start, end = f"{year - 1}0101", f"{year}1231"
    frames: list[pd.DataFrame] = []
    for i, code in enumerate(codes, 1):
        try:
            frame = adapter.fetch_fina_indicator(code, start, end)
        except Exception as exc:  # noqa: BLE001 - one bad name must not kill the run
            print(f"[warn] {code}: {type(exc).__name__}: {exc}", flush=True)
            continue
        if frame is None or frame.empty or "ann_date" not in frame.columns:
            continue
        fresh = frame[frame["ann_date"].astype(str) >= since]
        if not fresh.empty:
            frames.append(fresh)
        if i % 100 == 0:
            print(f"[progress] {i}/{len(codes)}", flush=True)
    # Drop all-NA columns before concat: a vendor frame with a fully empty
    # optional column otherwise changes the concatenated dtype (FutureWarning).
    frames = [f.dropna(axis=1, how="all") for f in frames]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def merge(store: DatasetStore, new: pd.DataFrame) -> dict:
    """Merge into the single ``all`` partition without changing its schema."""
    old = store.read("fina_indicator")
    if old is None or old.empty:
        raise RuntimeError("fina_indicator 为空，先跑 backfill.py --datasets fina_indicator")
    # The vendor may return a narrower column set. Reindex onto the stored
    # columns so the schema hash (and therefore every reader) stays unchanged.
    added = new.reindex(columns=old.columns)
    merged = pd.concat([old, added], ignore_index=True)
    merged = merged.sort_values(PK, kind="mergesort")
    # A restatement carries its own ann_date; keep the latest known version of
    # each (symbol, period, announcement) key. Readers still cut on ann_date.
    merged = merged.drop_duplicates(subset=PK, keep="last").reset_index(drop=True)
    as_of = str(merged["ann_date"].astype(str).max())
    store.write("fina_indicator", "all", merged, as_of=as_of)
    return {"before": int(len(old)), "after": int(len(merged)),
            "new_rows": int(len(added)), "as_of": as_of}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按公告日增量同步 fina_indicator")
    ap.add_argument("--since", default=None, help="只处理 ann_date >= YYYYMMDD 的新披露")
    ap.add_argument("--symbols", default=None, help="逗号分隔，定向修复，跳过全市场游标")
    ap.add_argument("--limit", type=int, default=400, help="本批最多处理多少只；0 = 全部")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    since = args.since or _default_since()
    state = _cursor()
    store = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
    if args.symbols:
        codes = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        last_code = str(state.get("last_code") or "")
    else:
        # A completed cycle with an unchanged since means there is nothing new.
        if state.get("since") == since and state.get("cycle_done"):
            print(f"[done] since={since} 的全市场扫描已完成，无新工作", flush=True)
            return 0
        last_code = str(state.get("last_code") or "")
        codes = next_batch(store, args.limit, last_code)
        if not codes:
            state.update({"since": since, "cycle_done": True,
                          "last_run": datetime.now().isoformat(timespec="seconds")})
            CURSOR.parent.mkdir(parents=True, exist_ok=True)
            CURSOR.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[done] 全市场游标已走完一轮", flush=True)
            return 0

    print(f"[start] since={since} batch={len(codes)} from={last_code or 'BEGIN'} "
          f"dry_run={args.dry_run}", flush=True)
    adapter = TushareAdapter(token=os.getenv("TUSHARE_TOKEN"))
    fresh = fetch_new(adapter, codes, since)
    periods = sorted(fresh["end_date"].astype(str).unique()) if not fresh.empty else []
    print(f"[fetch] 新披露行数={len(fresh)} | 期间={periods}", flush=True)
    if args.dry_run:
        if not fresh.empty:
            print(fresh[["ts_code", "ann_date", "end_date"]].head(8).to_string(index=False))
        return 0

    out = None
    if not fresh.empty:
        out = merge(store, fresh)
        print(f"[write] rows {out['before']} -> {out['after']} | as_of={out['as_of']}", flush=True)

    CURSOR.parent.mkdir(parents=True, exist_ok=True)
    state.update({
        "since": since,
        "last_code": codes[-1] if not args.symbols and codes else last_code,
        "cycle_done": bool(not args.symbols and not codes),
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "rows_fetched": int(len(fresh)),
        "rows_before": out["before"] if out else None,
        "rows_after": out["after"] if out else None,
        "symbols_processed": len(codes),
    })
    CURSOR.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
