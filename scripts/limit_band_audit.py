"""Classify every `stk_limit` row that carries no band.

    python scripts/limit_band_audit.py --year 2026
    python scripts/limit_band_audit.py --all

Zero is three different facts: a first-day listing, a delisting-arrangement
session, or a row the vendor failed to publish. The first two are legitimate
and must not be "repaired"; the third must be visible. This script joins
`stock_basic` so the distinction is made from dated facts, not from the shape
of the number alone.

Exit code 1 when at least one row cannot be classified, so a scheduled check
can fail loudly instead of printing a reassuring count.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from service.adapters.limit_bands import DELISTING, FIRST_DAY, MISSING, classify_band
from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry


def audit(store: DatasetStore, year: str | None) -> dict:
    start = f"{year}0101" if year else None
    end = f"{year}1231" if year else None
    limits = store.read("stk_limit", start, end,
                        columns=["ts_code", "trade_date", "up_limit", "down_limit"])
    if limits.empty:
        return {"year": year or "all", "rows": 0, "zero_rows": 0,
                "counts": {}, "missing": [], "note": "stk_limit 为空"}
    limits["up_limit"] = pd.to_numeric(limits["up_limit"], errors="coerce")
    limits["down_limit"] = pd.to_numeric(limits["down_limit"], errors="coerce")
    zero = limits[(limits["up_limit"] <= 0) | (limits["down_limit"] <= 0)].copy()
    if zero.empty:
        return {"year": year or "all", "rows": int(len(limits)), "zero_rows": 0,
                "counts": {}, "missing": []}

    basic = store.read("stock_basic")
    cols = [c for c in ("ts_code", "list_date", "delist_date") if c in basic.columns]
    dates = basic[cols].drop_duplicates("ts_code") if cols else pd.DataFrame(columns=["ts_code"])
    zero = zero.merge(dates, on="ts_code", how="left")

    zero["reason"] = [
        classify_band(row.up_limit, row.down_limit,
                      list_date=getattr(row, "list_date", None),
                      trade_date=row.trade_date,
                      delist_date=getattr(row, "delist_date", None))
        for row in zero.itertuples(index=False)
    ]
    counts = {k: int(v) for k, v in zero["reason"].value_counts().items()}
    missing = zero[zero["reason"] == MISSING]
    return {
        "year": year or "all",
        "rows": int(len(limits)),
        "zero_rows": int(len(zero)),
        "counts": {
            "first_day": counts.get(FIRST_DAY, 0),
            "delisting": counts.get(DELISTING, 0),
            "missing": counts.get(MISSING, 0),
        },
        "missing": missing[["ts_code", "trade_date", "up_limit", "down_limit"]]
        .head(20).to_dict("records"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--year", help="YYYY, defaults to 2026")
    group.add_argument("--all", action="store_true", help="every stored partition")
    args = parser.parse_args(argv)
    year = None if args.all else (args.year or "2026")

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    result = audit(store, year)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if result["counts"].get("missing") else 0


if __name__ == "__main__":
    raise SystemExit(main())
