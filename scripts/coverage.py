"""What the store actually holds, and where the holes are.

    python scripts/coverage.py
    python scripts/coverage.py --json
    python scripts/coverage.py --gaps          # only datasets with missing sessions

Three questions this answers that a row count cannot:

* **How far back does it go?** Per dataset, not per project. The endpoints have
  wildly different history limits, so "we have data since 2021" is false for the
  broken-board pool and true for the limit-up pool.
* **Which sessions are missing?** For day-partitioned datasets, every trading day
  inside the covered range must have a partition. A missing one is not "a quiet
  day" - it is a failed fetch that every later query reads as zero.
* **Is it shallow enough to be misleading?** A dataset covering 20 sessions and
  one covering 1,400 look identical in a row count.

This is deliberately read-only and cheap: it reads the manifest, never the parquet.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.adapters.parquet_store import DatasetStore
from service.adapters.pit import TradingCalendarStore
from service.adapters.sqlite_store import Registry

# Datasets whose partition key is a trading day **and** whose contract is "one
# partition per session". Gap checking only means something for these.
#
# Deliberately absent:
#   news / stk_mins / rt_quote  - fetched on demand for a handful of symbols, so
#     a missing day means "nobody asked", not "the fetch failed";
#   limit_up_pool and friends   - eastmoney pools, written only when non-empty.
DAY_PARTITIONED = {
    "ths_limit_up", "ths_limit_down", "ths_limit_break", "ths_ladder",
    "ths_lhb", "ths_lhb_seat", "ths_limit_stat", "ths_auction", "ths_anomaly",
}


def _is_day(partition: str) -> bool:
    return len(partition) == 8 and partition.isdigit()


def collect(store: DatasetStore, registry: Registry) -> list[dict]:
    rows: dict[str, list] = defaultdict(list)
    for rec in registry.list_manifest():
        rows[rec.dataset].append(rec)

    try:
        calendar = TradingCalendarStore.from_frame(store.read("trade_cal"))
        open_days = set(calendar.days)
    except (KeyError, ValueError):
        open_days = set()

    # Days upstream answered with zero rows. They have no parquet partition
    # because there is nothing to write, but they are not holes either.
    answered_empty: dict[str, set[str]] = defaultdict(set)
    try:
        for row in registry.list_ingest("ths_fetch"):
            if row.get("status") == "empty":
                answered_empty[row["dataset"]].add(row["partition"])
    except Exception:  # noqa: BLE001 - ledger is a nicety, not a requirement
        answered_empty = defaultdict(set)

    out: list[dict] = []
    for name in sorted(rows):
        records = rows[name]
        ok = [r for r in records if r.status in ("ok", "warn")]
        if not ok:
            continue
        starts = [str(r.cover_start) for r in ok if r.cover_start]
        ends = [str(r.cover_end) for r in ok if r.cover_end]
        entry = {
            "dataset": name,
            "partitions": len(ok),
            "rows": sum(int(r.rows or 0) for r in ok),
            "cover_start": min(starts) if starts else None,
            "cover_end": max(ends) if ends else None,
            "source": ok[0].source,
            "trust_tier": ok[0].trust_tier,
            "statuses": sorted({r.status for r in records}),
        }
        if name in DAY_PARTITIONED:
            have = {r.partition for r in ok if _is_day(r.partition)}
            span = {d for d in open_days if entry["cover_start"] <= d <= entry["cover_end"]} \
                if (entry["cover_start"] and entry["cover_end"]) else set()
            empty = answered_empty.get(name, set())
            checked = bool(span)
            missing = sorted(span - have - empty) if checked else []
            entry["sessions"] = len(span & have) if checked else len(have)
            entry["sample_partitions"] = len(have)
            entry["coverage_scope"] = (
                "open_sessions_inside_cover_range" if checked else "partitions_only"
            )
            entry["empty_sessions"] = len(span & empty)
            entry["expected_sessions"] = len(span) if checked else None
            entry["missing_sessions"] = missing if checked else None
            entry["missing_count"] = len(missing) if checked else None
        out.append(entry)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--gaps", action="store_true", help="only show missing sessions")
    args = parser.parse_args(argv)

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    report = collect(store, registry)
    if args.gaps:
        report = [r for r in report if r.get("missing_count")]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    print(f"{'dataset':16s} {'rows':>9s} {'parts':>7s}  {'cover':<20s} {'src':10s} tier  gaps")
    for r in report:
        gaps = r.get("missing_count")
        gap_text = "-" if gaps is None else (f"{gaps}" if gaps else "0")
        if gaps:
            gap_text = f"{gaps}  {','.join(r['missing_sessions'][:3])}" + \
                       (" ..." if gaps > 3 else "")
        print(f"{r['dataset']:16s} {r['rows']:9d} {r['partitions']:7d}  "
              f"{r['cover_start']!s}..{r['cover_end']:<8s} "
              f"{str(r['source'])[:10]:10s} {r['trust_tier']:4s}  {gap_text}")

    broken = [r for r in report if r.get("missing_count")]
    unchecked = [r for r in report
                 if "missing_count" in r and r["missing_count"] is None]
    print()
    if unchecked:
        print(f"{len(unchecked)} dataset(s) were not gap-checked: trade_cal has no "
              "open sessions inside their covered range")
    if broken:
        print(f"{len(broken)} dataset(s) have missing sessions inside their own range:")
        for r in broken:
            print(f"  {r['dataset']}: {r['missing_count']} of {r['expected_sessions']} "
                  f"({','.join(r['missing_sessions'][:6])})")
        print("Re-run scripts/fetch_ths_special.py --backfill to fill them; the script")
        print("skips partitions that already exist, so it only fetches the holes.")
    elif any(r.get("missing_count") is not None for r in report):
        print("no gaps inside any checked covered range")
    else:
        print("no session coverage could be checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
