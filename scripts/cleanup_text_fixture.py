"""Remove only the synthetic text fixture and its SQLite sidecar files.

The default is a dry run. Pass ``--apply`` to delete the explicit allowlist;
this script never scans, rewrites, or deletes market-data partitions.
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_DB = ROOT / "data" / "text"
ALLOWED_NAMES = (
    "fixture.json",
    "text_index.sqlite",
    "text_index.sqlite-wal",
    "text_index.sqlite-shm",
)


def fixture_targets(root: Path | None = None) -> list[Path]:
    """Resolve and validate the deletion plan without discovering other files."""
    base = (TEXT_DB if root is None else root).resolve(strict=False)
    if base.exists() and not base.is_dir():
        raise ValueError(f"text fixture root is not a directory: {base}")
    if not base.exists():
        return []

    targets = []
    for name in ALLOWED_NAMES:
        candidate = base / name
        if not candidate.exists() and not candidate.is_symlink():
            continue
        resolved = candidate.resolve(strict=True)
        if (not resolved.is_relative_to(base) or resolved.parent != base
                or resolved.name not in ALLOWED_NAMES or not resolved.is_file()):
            raise ValueError(f"refusing non-fixture path: {candidate}")
        targets.append(resolved)
    return targets


def cleanup(root: Path | None = None, *, dry_run: bool = True) -> list[Path]:
    """Use the same validated plan for preview and deletion."""
    targets = fixture_targets(root)
    for path in targets:
        print(f"[cleanup] {'would remove' if dry_run else 'remove'} {path}")
        if not dry_run:
            path.unlink()
    print(f"[cleanup] {'dry-run' if dry_run else 'removed'} files={len(targets)}")
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="preview/remove only text fixture files")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview deletions (default)")
    mode.add_argument("--apply", action="store_true", help="delete the validated fixture files")
    args = parser.parse_args(argv)
    cleanup(dry_run=not args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
