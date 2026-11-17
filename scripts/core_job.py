"""Daily core-sleeve job: mark the ETF book and print what the rule implies.

    python scripts/core_job.py                 # mark today, print the reference
    python scripts/core_job.py --date 20261023
    python scripts/core_job.py --force         # print even if the rule is not due

Runs after the close, is idempotent for a given date, and never touches the
aggressive sleeve. It writes one NAV row - that is bookkeeping, not a decision -
and prints the orders an inverse-volatility rule would place.

There is no ``--apply`` here any more. A job that can move money on a schedule is
a trading bot, whatever the flag is called, and the whole point of this desk is
that the trading is done by the model. To act on this output, POST the weights
to /core/preview first, then to /core/execute with a thesis; the decision log
will then show who decided and why.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.trading.core import CoreBook, describe
from service.trading.state import PaperStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="")
    parser.add_argument("--force", action="store_true", help="print even if not due")
    args = parser.parse_args(argv)
    if args.date:
        try:
            if datetime.strptime(args.date, "%Y%m%d").strftime("%Y%m%d") != args.date:
                raise ValueError
        except ValueError:
            print(f"[core_job] invalid --date {args.date!r}; expected YYYYMMDD", file=sys.stderr)
            return 2

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    paper = PaperStore(Path(os.getenv("PAPER_DB", ROOT / "data" / "paper.sqlite")))
    book = CoreBook(store, paper)

    day = args.date or book.last_day()
    need, why = book.should_rebalance(day)
    marked = book.mark(day)
    weights = book.target_weights(day)
    meta = weights.pop("_meta", {})
    # Bookkeeping only: one NAV row per session so the shadow record exists even
    # on days nobody trades. No order is created here.
    paper.core_record_nav(day, f"{marked['nav']:.2f}", f"{marked['cash']:.2f}",
                          f"{marked['invested']:.2f}", weights)

    if not (need or args.force):
        print(json.dumps({"trade_date": day, "nav": marked["nav"], "action": "none",
                          "reason": why, "targets": weights, "regime": meta},
                         ensure_ascii=False, indent=1))
        print(f"[rationale] {describe(book.target_weights(day))}")
        return 0

    plan = book.plan(day)
    fees = sum(float(o["fee"]) for o in plan["orders"])
    print(json.dumps({
        "trade_date": day,
        "action": "reference",
        "trigger": why,
        "orders": plan["orders"],
        "fees_cny": round(fees, 2),
        "turnover_cny": round(sum(float(o["qty"]) * float(o["price"])
                                  for o in plan["orders"]), 2),
        "targets": plan["targets"],
        "weights_after": plan["weights"],
        "cash_weight_after": plan["cash_weight"],
        "skipped_unaffordable": plan["skipped_unaffordable"],
        "rounding_gaps": plan["rounding_gaps"],
        "effective_positions": plan["effective_positions"],
        "regime": meta,
        "note": ("这是规则会做的分配，不会被执行。要执行请 POST /core/execute "
                 "并自己给出 targets + thesis。"),
    }, ensure_ascii=False, indent=1))
    print(f"[rationale] {describe(book.target_weights(day))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
