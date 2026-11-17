"""Move the pre-versioning parquet files out of the live dataset directories.

Before the manifest layout, `daily` data lived at `data/parquet/daily/2026.parquet`
— one file per year, overwritten in place. The versioned layout put the real data
under `.versions/2026/` and left those files behind. They stopped being written
and stopped being read: `store.read()` resolves through the manifest.

The problem is not the wasted bytes, it is that they are a **second answer**.
`data/parquet/daily/2026.parquet` still contained rows up to 2026-10-28 on a day
the store had 10-29, and reading it directly — which is the obvious thing to do
when you just want a dataframe — silently returns a stale session. This happened
during the 2026-10-29 investigation and produced a wrong conclusion about whether
the daily job had run.

So the files are moved, not deleted: `data/parquet/.legacy/<dataset>/`. Nothing
reads from there, but the bytes are still recoverable, and the live directory now
has exactly one answer.

    python scripts/retire_legacy_files.py            # report only
    python scripts/retire_legacy_files.py --apply    # move them
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry

PARQUET = ROOT / "data" / "parquet"
BACKUP = PARQUET / ".legacy"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="move the files")
    parser.add_argument("--dataset", help="only this dataset")
    args = parser.parse_args(argv)

    store = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
    moved = kept = 0
    for folder in sorted(p for p in PARQUET.iterdir() if p.is_dir()):
        if folder.name.startswith("."):
            continue
        if args.dataset and folder.name != args.dataset:
            continue
        flat = sorted(folder.glob("*.parquet"))
        if not flat:
            continue
        for path in flat:
            published = store.registry.get_manifest(folder.name, path.stem)
            if published is None:
                # A flat file with no manifest is the only copy. Moving it would
                # turn "not yet published" into "gone".
                print(f"[keep] {path.relative_to(ROOT)} — no manifest, sole copy")
                kept += 1
                continue
            if Path(published.path) == path:
                print(f"[keep] {path.relative_to(ROOT)} — manifest points here")
                kept += 1
                continue
            target = BACKUP / folder.name / path.name
            print(f"[move] {path.relative_to(ROOT)} -> {target.relative_to(ROOT)}")
            if args.apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
                # The sidecar holds the checksum and the previous-version pointer.
                # Leaving it behind meant the live directory still answered "what
                # is this partition" with a stale stamp for a file that had moved,
                # so the claim above — one answer per dataset — was not true.
                sidecar = path.with_suffix(".meta.json")
                if sidecar.exists():
                    shutil.move(str(sidecar),
                                str(target.with_suffix(".meta.json")))
            moved += 1

    verb = "moved" if args.apply else "would move"
    print(f"\n{verb}: {moved} | kept: {kept}")
    if not args.apply and moved:
        print("re-run with --apply to move them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
