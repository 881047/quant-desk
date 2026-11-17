"""Turn WorkBuddy's text captures into a queryable dataset.

    python scripts/ingest_text.py                 # all files in data/inbox/text
    python scripts/ingest_text.py --date 20261023 # one day
    python scripts/ingest_text.py --dry-run

WorkBuddy reads the web (announcements, news, research notes) and drops a JSON
file per session into ``data/inbox/text/``. This script is the only thing that
writes the ``news`` dataset, so every piece of text the trader sees has gone
through one validation path: a title, a parseable publication time, a source, and
a content hash for de-duplication.

Accepted shapes:

    [ {...}, {...} ]                       # a bare list of records
    {"as_of": "20261023", "items": [...]}  # with a stamp

Each record needs at least ``title`` and a publication time (``pub_time`` or
``发布时间``). Optional: ``ts_code`` (6 digits or full code), ``summary``,
``source``, ``url``, ``kind`` (announcement/news/research/policy), ``tier``
(official/media/web), ``sentiment`` (-1..1), ``tags``.

Processed files move to ``data/inbox/text/done/`` so a re-run cannot double-count.
Empty captures are accepted and logged: "WorkBuddy found nothing today" is a fact
worth recording, not an error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.adapters.text import ingest
from service.kernel.clock import SHANGHAI

INBOX = ROOT / "data" / "inbox" / "text"
DONE = INBOX / "done"


def _load(path: Path) -> tuple[list[dict], str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw, datetime.now(SHANGHAI).strftime("%Y%m%d")
    if isinstance(raw, dict):
        items = raw.get("items") or raw.get("records") or []
        stamp = str(raw.get("as_of") or datetime.now(SHANGHAI).strftime("%Y%m%d"))
        return list(items), stamp
    raise ValueError("inbox file must be a list or an object with 'items'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="", help="only files named <date>.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep", action="store_true", help="do not move files to done/")
    args = parser.parse_args(argv)
    if args.date:
        try:
            if datetime.strptime(args.date, "%Y%m%d").strftime("%Y%m%d") != args.date:
                raise ValueError
        except ValueError:
            print(f"[ingest_text] invalid --date {args.date!r}; expected YYYYMMDD", file=sys.stderr)
            return 2

    INBOX.mkdir(parents=True, exist_ok=True)
    pattern = f"{args.date}.json" if args.date else "*.json"
    files = sorted(INBOX.glob(pattern))
    if not files:
        print(f"[ingest_text] no inbox files under {INBOX}")
        return 0

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    total = 0
    failures = 0

    for path in files:
        try:
            records, as_of = _load(path)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[ingest_text] {path.name}: unreadable ({exc})")
            failures += 1
            continue
        if args.dry_run:
            print(f"[ingest_text] {path.name}: {len(records)} records, as_of={as_of} (dry run)")
            continue
        try:
            result = ingest(store, records, as_of=as_of,
                            source="workbuddy", trust_tier="T3")
            total += result["written"]
            print(f"[ingest_text] {path.name}: written={result['written']} "
                  f"rejected={result['rejected']} dates={result.get('by_date')}")
            if result["rejected"]:
                failures += 1
                for detail in result.get("details", []):
                    print(f"    rejected: {detail['why']} | {detail['record'][:120]}")
            if not args.keep and not result["rejected"]:
                DONE.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(DONE / path.name))
        except Exception as exc:  # noqa: BLE001 - keep the file available for a retry
            failures += 1
            print(f"[ingest_text] {path.name}: ingest failed "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)

    print(f"[ingest_text] files={len(files)} rows={total} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
