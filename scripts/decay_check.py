"""Is a live strategy still doing what it promised?

    python scripts/decay_check.py
    python scripts/decay_check.py --strategy llm --window 30

Reads closed cases from the paper book and compares them with the baseline each
strategy was promoted on. Prints a state per strategy; changes nothing.

A strategy whose evidence carries no `mean_excess` / `std` shows `no_baseline`
— that is the honest answer, not a zero. Backfilling a sigma from the live
trades would make the monitor measure itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.trading import decay as decay_mod
from service.trading import strategy as strategy_mod
from service.trading.state import PaperStore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default="")
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    if args.window <= 0:
        print("[decay_check] --window must be positive", file=sys.stderr)
        return 2

    paper = PaperStore(ROOT / "data" / "paper.sqlite")
    cases = paper.list_cases(limit=2000)
    keys = [args.strategy] if args.strategy else list(strategy_mod.REGISTRY)

    rows = []
    missing_metrics = 0
    head = f"{'strategy':<18}{'status':<11}{'state':<14}{'n':>4}{'mean':>9}{'z':>7}{'sprt':>14}"
    print(head)
    print("-" * len(head))
    for key in keys:
        meta = strategy_mod.get(key)
        if meta is None:
            print(f"{key:<18}{'?':<11}{'unknown':<14}")
            rows.append({"key": key, "state": "unknown", "n": 0})
            missing_metrics += 1
            continue
        baseline = decay_mod.baseline_from_evidence(meta.evidence)
        if baseline is None:
            print(f"{key:<18}{meta.status:<11}{'no_baseline':<14}{0:>4}{'-':>9}{'-':>7}{'-':>14}")
            rows.append({"key": key, "status": meta.status, "state": "no_baseline"})
            missing_metrics += 1
            continue
        result = (decay_mod.monitor_nav(paper.core_nav_history(limit=2000), baseline,
                                        strategy=key, window=args.window)
                  if meta.book == "core"
                  else decay_mod.monitor(cases, baseline, strategy=key, window=args.window))
        live = result.get("live") or {}
        rolling = result.get("rolling") or {}
        sprt = result.get("sprt") or {}
        print(f"{key:<18}{meta.status:<11}{result['state']:<14}{result['n']:>4}"
              f"{(live.get('mean') or 0) * 100:>8.2f}%"
              f"{(rolling.get('z') or 0):>7.2f}"
              f"  {sprt.get('decision', '-'):<12}")
        rows.append({"key": key, "status": meta.status, **result})
        if int(result.get("n") or 0) == 0:
            missing_metrics += 1

    if args.json:
        out = ROOT / args.json
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, indent=2, ensure_ascii=False, default=str),
                       encoding="utf-8")
        print(f"\nreport -> {out}")

    missing = [r["key"] for r in rows if r.get("state") == "no_baseline"]
    if missing:
        print(f"\nno baseline (evidence lacks mean_excess/std or cagr/sharpe): "
              f"{', '.join(missing)}")
    return 1 if missing_metrics else 0


if __name__ == "__main__":
    raise SystemExit(main())
