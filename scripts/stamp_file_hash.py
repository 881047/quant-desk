"""Stamp the byte hash of an already-published parquet into its metadata.

Why this exists
---------------
`DatasetStore._validate_record` binds three things together: the manifest row, the
`.meta.json` beside the file, and the bytes on disk. Partitions written before
`file_sha256` existed can only be checked the expensive way — read the parquet,
re-hash every column, compare against the manifest. That costs 2-4 seconds per
partition on a cold process, and a short-lived script pays it on *every* run.

Stamping records the hash once. After that the check is "hash the file, compare",
which never parses a column.

What it does **not** do
-----------------------
It does not take the manifest's word for it. Before writing a hash it re-derives
`content_checksum` and the row count from the file and refuses to stamp anything
that disagrees with its manifest. A stamp is a statement that *these bytes* are
the bytes the manifest describes, and that statement is only made after checking.

    python scripts/stamp_file_hash.py            # report only
    python scripts/stamp_file_hash.py --apply     # write the hashes

Idempotent: partitions that already carry a hash are skipped, so a second run
does nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import pandas as pd

from service.adapters.parquet_store import (
    _atomic_json,
    _file_sha256,
    content_checksum,
    schema_hash,
)
from service.adapters.sqlite_store import Registry


def _meta_path(path: Path) -> Path:
    return path.with_suffix(".meta.json")


def _plan(registry: Registry) -> tuple[list[dict], list[dict], int]:
    """Split manifests into (to_stamp, already, errors) without touching anything."""
    todo: list[dict] = []
    done: list[dict] = []
    broken: list[dict] = []
    for rec in registry.list_manifest():
        path = Path(rec.path)
        meta_file = _meta_path(path)
        if not path.exists() or not meta_file.exists():
            broken.append({"dataset": rec.dataset, "partition": rec.partition,
                           "reason": "missing parquet or metadata", "path": str(path)})
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            broken.append({"dataset": rec.dataset, "partition": rec.partition,
                           "reason": f"unreadable metadata: {exc}", "path": str(path)})
            continue
        if meta.get("file_sha256"):
            done.append({"dataset": rec.dataset, "partition": rec.partition})
            continue
        # The expensive part, and the whole reason a stamp is trustworthy: read
        # the file and prove it still is what the manifest says.
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001 - any parse failure is a finding
            broken.append({"dataset": rec.dataset, "partition": rec.partition,
                           "reason": f"unreadable parquet: {type(exc).__name__}", "path": str(path)})
            continue
        if len(frame) != rec.rows or content_checksum(frame) != rec.checksum:
            broken.append({"dataset": rec.dataset, "partition": rec.partition,
                           "reason": "content does not match manifest", "path": str(path)})
            continue
        if schema_hash(frame) != rec.schema_hash:
            broken.append({"dataset": rec.dataset, "partition": rec.partition,
                           "reason": "schema hash does not match manifest", "path": str(path)})
            continue
        todo.append({"dataset": rec.dataset, "partition": rec.partition,
                     "path": path, "meta": meta, "meta_path": meta_file})
    return todo, done, broken


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the hashes")
    parser.add_argument("--dataset", action="append", default=None,
                        help="limit to one dataset (repeatable)")
    parser.add_argument("--limit", type=int, default=0, help="stop after N partitions")
    args = parser.parse_args(argv)

    registry_path = Path(__import__("os").getenv("QUANT_REGISTRY_DB")
                         or (ROOT / "data" / "registry.sqlite"))
    registry = Registry(registry_path)
    todo, done, broken = _plan(registry)
    if args.dataset:
        wanted = set(args.dataset)
        todo = [row for row in todo if row["dataset"] in wanted]
    if args.limit:
        todo = todo[:args.limit]

    print(f"already stamped : {len(done)}")
    print(f"to stamp        : {len(todo)}")
    print(f"cannot stamp    : {len(broken)}")
    for row in broken[:10]:
        print(f"  ! {row['dataset']}[{row['partition']}]: {row['reason']}")
    if len(broken) > 10:
        print(f"  ... and {len(broken) - 10} more")

    if not args.apply:
        print("\ndry run; pass --apply to write")
        return 0 if not broken else 1

    written = 0
    failed = 0
    for row in todo:
        path = row["path"]
        try:
            digest = _file_sha256(path)
            meta = dict(row["meta"])
            meta["file_sha256"] = digest
            _atomic_json(row["meta_path"], meta)
            written += 1
        except OSError as exc:
            failed += 1
            print(f"  ! {row['dataset']}[{row['partition']}]: {exc}")
    print(f"\nstamped {written}, failed {failed}")
    print("Run `python scripts/verify_data.py` to confirm the store still reads.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
