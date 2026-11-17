"""Re-publish partitions whose manifest no longer matches their own bytes.

A manifest is a promise: "the file at this path hashes to this value". When the
promise is broken the store refuses to read — correctly — but the repair is not
to weaken the check. It is to re-read the values and re-publish them through the
same writer, which re-normalises dtypes, writes a new immutable version and
records a byte hash. Values are not touched; only the encoding and the manifest
are.

Why this is needed at all: `content_checksum` hashes ``column|dtype|values``, and
an object column of True/False is stored by parquet as BOOLEAN and read back as
``bool``. A session where such a column was fully populated therefore produced a
manifest that its own file could never reproduce, while an all-null session did.
29 of 32 ths_ladder partitions were in that state and made
``quant_limit_structure`` raise on every call.

    python scripts/repair_store.py --dataset ths_ladder            # report only
    python scripts/repair_store.py --dataset ths_ladder --apply    # rewrite
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import pandas as pd

from service.adapters.parquet_store import DatasetStore, StoreCorruptionError
from service.adapters.sqlite_store import Registry


def explore(store: DatasetStore, dataset: str) -> tuple[list[dict], list[dict]]:
    good: list[dict] = []
    bad: list[dict] = []
    for rec in store.registry.list_manifest(dataset):
        try:
            store._validate_record(rec)
        except StoreCorruptionError as exc:
            bad.append({"record": rec, "error": str(exc)})
        else:
            good.append({"record": rec})
    return good, bad


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="re-publish the broken partitions (default: report only)")
    args = parser.parse_args()

    store = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
    good, bad = explore(store, args.dataset)
    print(f"{args.dataset}: {len(good)} ok, {len(bad)} mismatch")
    for row in bad[:10]:
        print(f"  {row['record'].partition}  {row['error'][:90]}")
    if not args.apply or not bad:
        return 0

    repaired = 0
    failed: list[str] = []
    for row in bad:
        rec = row["record"]
        try:
            frame = pd.read_parquet(rec.path)
            note = (rec.note + " | republished: manifest did not match its bytes"
                    if rec.note else "republished: manifest did not match its bytes")
            store.write(rec.dataset, rec.partition, frame, as_of=rec.as_of,
                        note=note[:200], source=rec.source, trust_tier=rec.trust_tier,
                        allow_repair=True)
            repaired += 1
        except Exception as exc:  # noqa: BLE001 - a repair pass must reach the end
            # One unreadable or locked partition used to abort the whole loop, so
            # the ones already fixed stayed fixed and the rest were never tried,
            # with no "how many of how many" line printed at all.
            failed.append(f"{rec.partition}: {type(exc).__name__}: {exc}")
            print(f"[fail] {rec.partition}: {type(exc).__name__}: {exc}")
    print(f"republished {repaired} partitions, {len(failed)} failed")
    for line in failed[:10]:
        print(f"  {line}")
    _, remaining = explore(store, args.dataset)
    print(f"remaining mismatches: {len(remaining)}")
    return 0 if not remaining else 1


if __name__ == "__main__":
    raise SystemExit(main())
