"""Give the journal entries written before the check a verdict.

    python scripts/backfill_journal_verify.py --dry
    python scripts/backfill_journal_verify.py
    python scripts/backfill_journal_verify.py --json reports/journal_verify.json

The check landed with P83: `POST /journal` compares every number in the text
with that day's `facts` and stores the verdict next to the entry. The twenty
entries already in the book have no verdict, and they are the ones a later
session reads — "was this note checked" is exactly what that reader cannot see.

So this walks the existing entries and applies the same `journal_check.verify`
the write route uses. Same function, not a copy: a backfill that reimplements
the rule measures the reimplementation.

Four answers, and the last two are the honest ones:

* **filled** — a facts snapshot for that day exists; both halves are checked.
* **labels_only** — no snapshot on disk. The account half is *not* rebuilt:
  `equity`, `cash`, `positions` are read from the book as it stands **now**, so
  computing 20261023 today would put today's balance under that date, and
  `write_facts` persists it — poisoning the file a close report for that day is
  later verified against. But counts are not like that: 涨停 49 家 came from
  bars that never changed, so the label check runs against `market_facts` and
  the result is stored with `scope: market_only`. The numbers stay unchecked,
  and the read path says so.
* **no_market** — not even bars for that day; nothing to check against. The
  entry keeps `verified: null`.
* **already** — the entry carried a verdict; untouched.

Writes only the snapshot; the prose is never touched.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.trading import journal_check
from service.trading.facts import facts_for


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry", action="store_true", help="report only, write nothing")
    ap.add_argument("--limit", type=int, default=0, help="newest N entries; 0 = all")
    ap.add_argument("--json", default="", help="write the per-entry report here")
    ap.add_argument("--refresh-claims", action="store_true",
                    help="also re-run the label check on rows that already have numbers "
                         "(for entries written before `claims` existed)")
    args = ap.parse_args(argv)

    from service.trading import get_desk

    desk = get_desk()
    rows = desk.paper.search_journal("", args.limit or 1000)
    rows = list(reversed(rows))  # oldest first: a run that dies leaves a contiguous prefix

    tally: Counter[str] = Counter()
    facts_seen: dict[str, dict | None] = {}
    report: list[dict] = []

    for row in rows:
        snapshot = row.get("snapshot") or {}
        if snapshot.get("numbers") and not args.refresh_claims:
            tally["already"] += 1
            continue
        day = snapshot.get("day")
        if not day:
            tally["no_day"] += 1
            report.append({"id": row["id"], "state": "no_day"})
            continue
        if day not in facts_seen:
            facts_seen[day] = facts_for(desk, day, root=ROOT, compute=False)
        facts = facts_seen[day]
        if facts is None:
            # 没有当天快照：账户那一半补不回来（读的是今天的账本），但**计数补得回来**。
            # 只核标签那半，并写明 scope，读的人才知道数字仍然没核。
            labels = journal_check.verify_labels(desk, row.get("text") or "", day)
            if not labels:
                tally["no_facts"] += 1
                report.append({"id": row["id"], "day": day, "state": "no_market"})
                continue
            if not args.dry:
                desk.paper.set_journal_snapshot(
                    row["id"], {**snapshot,
                                **journal_check.stored({"claims": labels}, day,
                                                       scope="market_only")})
            tally["labels_only"] += 1
            report.append({"id": row["id"], "day": day, "state": "labels_only",
                           "labels": labels.get("labelled"), "ok": labels.get("ok")})
            continue

        verdict = journal_check.verify(desk, row.get("text") or "", day,
                                       facts=facts, compute=False)
        if not args.dry:
            # 合并而不是替换：这条记录的 snapshot 可能还带着别的东西（今天只有 day，
            # 但"只有"是此刻的观察，不是约束）。回填只该加一条结论。
            desk.paper.set_journal_snapshot(
                row["id"], {**snapshot, **journal_check.stored(verdict, day)})
        tally["filled"] += 1
        report.append({"id": row["id"], "day": day, "state": "filled",
                       "checked": verdict.get("checked"),
                       "matched": verdict.get("matched"),
                       "unmatched_count": verdict.get("unmatched_count"),
                       "unmatched": verdict.get("unmatched")[:6]})

    print(f"{'id':>5}  {'day':<10}{'state':<11}checked  matched  unmatched")
    print("-" * 57)
    for item in report:
        print(f"{item['id']:>5}  {item.get('day', '-'):<10}{item['state']:<11}"
              f"{item.get('checked', item.get('labels', '-')):>7}"
              f"{item.get('matched', item.get('ok', '-')):>9}"
              f"{item.get('unmatched_count', '-'):>11}")
    print("-" * 57)
    print("  ".join(f"{key}={tally[key]}" for key in
                    ("filled", "labels_only", "already", "no_market", "no_day")))
    if tally["labels_only"]:
        print(f"  {tally['labels_only']} 条没有当天快照：只核了标签那半"
              "（scope=market_only），数字仍然没核", file=sys.stderr)
    if tally["no_market"]:
        print(f"  {tally['no_market']} 条连行情都没有，保持 verified: null", file=sys.stderr)
    if args.dry and tally["filled"]:
        print("[dry] 什么都没写", file=sys.stderr)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"tally": dict(tally), "entries": report},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
