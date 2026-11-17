"""Settle/read frozen WorkBuddy short-alpha judgments; never create the judgments here."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.trading import build_desk
from service.trading import short_alpha_tracking as tracking


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="report", choices=("settle", "report"))
    parser.add_argument("--day", help="signal day YYYYMMDD; default all / latest report")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scope", default="prospective",
                        choices=("prospective", "replay", "all"))
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args(argv)
    if not 0 <= args.limit <= 30:
        parser.error("--limit must be in 0..30")
    desk = build_desk(root=args.root)
    try:
        if args.action == "report":
            payload = tracking.report(desk, root=args.root, scope=args.scope,
                                      limit=args.limit, as_of=args.day)
        else:
            payload = tracking.settle(desk, root=args.root, day=args.day,
                                      dry_run=args.dry_run)
    except (KeyError, ValueError, OSError, tracking.TrackingConflict) as exc:
        print(f"short-alpha tracking failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
