"""Diagnose a paper book that refuses to load — read-only.

    python scripts/repair_book.py            # human-readable
    python scripts/repair_book.py --json     # for a report or a tool call

``PaperBook.__init__`` deliberately refuses a snapshot whose reservations do
not reconcile with its live orders: trading a book whose cash is unfrozen but
whose orders still claim it is worse than not starting. The cost of that
correctness was that the refusal surfaced as a 500 on every route, with
``/health`` still green and no sentence saying *which* invariant broke.

``/health`` now carries ``book_error``. This script is the next step: it loads
the same payload, runs the same ``Ledger.from_snapshot``, and prints the exact
message plus the counts a repair decision needs. It writes nothing — the
release paths in ``PaperBook`` (``orphan_reservation_release``) are the only
code allowed to change a book, and they do it through a committed transaction.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service import paths
from service.kernel.costs import CostModel
from service.kernel.ledger import Ledger
from service.trading.state import PaperStore


def inspect(db: Path | None = None) -> dict:
    store = PaperStore(db or paths.paper_db())
    payload, revision = store.load_state_with_revision()
    out: dict = {
        "db": str(store.db_path),
        "revision": revision,
        "loaded": payload is not None,
        "ok": False,
        "error": None,
        "counts": {},
    }
    if payload is None:
        out["error"] = "no paper_state row: the book has never been written"
        return out

    account = payload.get("account") or {}
    positions = account.get("positions") or {}
    records = account.get("order_records") or []
    out["counts"] = {
        "positions": len(positions),
        "order_records": len(records),
        "open_orders": sum(
            1 for row in records
            if row.get("status") in {"PENDING_NEW", "NEW", "PARTIALLY_FILLED",
                                     "PENDING_CANCEL"}),
        "cash_available": account.get("cash_available"),
        "cash_frozen": account.get("cash_frozen"),
        "frozen_shares": sum(int(row.get("frozen_qty") or 0)
                             for row in positions.values()),
        "pending": len(payload.get("pending") or []),
        "day": payload.get("day"),
        "durable_open_orders": len(store.open_order_records()),
    }
    try:
        Ledger.from_snapshot(account, CostModel())
    except (ValueError, KeyError, TypeError) as exc:
        # The message is the whole point: it names the invariant, not a stack.
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["ok"] = True
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None,
                        help="paper sqlite (default: PAPER_DB or <data>/paper.sqlite)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    result = inspect(args.db)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"db       {result['db']}")
        print(f"revision {result['revision']}")
        for key, value in result["counts"].items():
            print(f"{key:22} {value}")
        print("ok       " + ("yes" if result["ok"] else "NO"))
        if result["error"]:
            print(f"error    {result['error']}")
            print("\n只读诊断：本脚本不会改动账本。修法见 "
                  "docs/03-runbook.md 的『账本拒绝加载』一节。")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
