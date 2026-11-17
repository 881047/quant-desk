"""Recompute the announcement labels from the stored titles — no refetch.

The labels (`category`, `kind`, `stage`, `paperwork`) are derived from the title,
so improving the rule table must not cost 5,919 requests. This rewrites the
218k-row table in place and prints what moved, which is the only way a rule
change is ever going to be made twice.

Usage::

    python scripts/reclassify_anns.py --dry-run
    python scripts/reclassify_anns.py

A dry run reads and prints; the real run backs the parquet up first, then writes
through `DatasetStore` so the manifest, checksum and column list stay in step
with the file.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from service.adapters.cninfo_adapter import reclassify
from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry

DATASET = "anns"
PARTITION = "all"


def _shares(series: pd.Series, top: int = 12) -> str:
    counts = series.fillna("（无）").value_counts(normalize=True).mul(100).round(1)
    return "  ".join(f"{name} {pct}%" for name, pct in counts.head(top).items())


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="recompute announcement labels")
    parser.add_argument("--dry-run", action="store_true", help="print, do not write")
    parser.add_argument("--dataset", default=DATASET)
    parser.add_argument("--partition", default=PARTITION)
    args = parser.parse_args(argv)

    if (args.dataset, args.partition) != (DATASET, PARTITION):
        print(f"[fatal] 仅支持 {DATASET}/{PARTITION}，拒绝全量读取后覆盖其他分区", flush=True)
        return 2

    store = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
    try:
        frame = store.read(args.dataset)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        print(f"[fatal] 读不到 {args.dataset}: {exc}", flush=True)
        return 2
    if frame is None or frame.empty:
        print(f"[fatal] {args.dataset} 是空表", flush=True)
        return 2

    before = frame.get("category", pd.Series([""] * len(frame)))
    before_stage = frame.get("stage", pd.Series([None] * len(frame)))
    out = reclassify(frame)
    if out is None or out.empty:
        print("[fatal] 没有可重算的 title 列", flush=True)
        return 2

    print(f"{len(out):,} 行 · {out['ts_code'].nunique():,} 只")
    print(f"\ncategory 之前  {_shares(before)}")
    print(f"category 之后  {_shares(out['category'])}")
    print(f"\nkind      {_shares(out['kind'])}")
    print(f"stage     {_shares(out['stage'])}")
    print(f"paperwork {out['paperwork'].mean() * 100:.1f}% 的文件不带来新事实")

    moved = int((before.astype(str) != out["category"].astype(str)).sum())
    moved_stage = int((before_stage.fillna("").astype(str)
                       != out["stage"].fillna("").astype(str)).sum())
    print(f"\n改判 {moved:,} 行（{moved / len(out) * 100:.1f}%），"
          f"stage 变化 {moved_stage:,} 行")
    if moved:
        changed = out[before.astype(str) != out["category"].astype(str)]
        pairs = (before[changed.index].astype(str) + " → " + changed["category"])
        print("主要迁移：" + "；".join(f"{k} {v:,}"
                                    for k, v in pairs.value_counts().head(8).items()))

    if args.dry_run:
        print("\n[dry-run] 没有写入")
        return 0

    path = store.path_for(args.dataset, args.partition)
    if path.exists():
        backup = path.with_suffix(f".parquet.bak-{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(path, backup)
        print(f"\n备份 → {backup}")

    today = datetime.now().strftime("%Y%m%d")
    rec = store.write(args.dataset, args.partition, out, as_of=today,
                      mode="replace", note="labels recomputed from stored titles")
    print(f"写入 → {path}  rows={rec.rows if rec else '?'} snapshot={rec.snapshot_id if rec else '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
