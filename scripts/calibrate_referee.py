"""Can the referee say yes?

    python scripts/calibrate_referee.py
    python scripts/calibrate_referee.py --symbols 240 --days 260 --seeds 5
    python scripts/calibrate_referee.py --json reports/P20_calibration.json

Every `rejected` in `reports/P5-P8` is a statement about the gate as much as
about the structure. This script separates the two: it builds panels whose edge
is known, runs the *real* gate on them, and reports

    TPR(alpha)   how often a real edge is admitted
    FDR(0)       how often noise is admitted
    lift         TPR minus the rate a random gate at the same adoption gets

plus two controls that fail loudly if the research layer ever learns to see the
future: `null` (no drift) and `placebo` (drift on stock-days the pool did not
select). Read-only with respect to data/ — it touches no parquet, no sqlite.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.research.controls import DEFAULT_WORLDS, PanelConfig, calibrate


def _table(report: dict) -> str:
    lines = [
        (f"{'world':<10}{'alpha':>8}{'runs':>6}{'admitted':>10}{'rate':>8}"
         f"{'trades':>8}{'excess':>10}{'verdicts':>26}"),
        "-" * 86,
    ]
    for name, block in report["worlds"].items():
        verdicts = ", ".join(f"{k}:{v}" for k, v in sorted(block["verdicts"].items()))
        lines.append(
            f"{name:<10}{block['alpha']:>8.3%}{block['runs']:>6}{block['admitted']:>10}"
            f"{block['rate']:>8.0%}{block['median_trades']:>8.0f}"
            f"{block['median_excess']:>10.3%}   {verdicts:<24}"
        )
    pooled = report["pooled"]
    cfg = report["config"]
    lines += [
        "-" * 86,
        (f"cost {cfg['cost_floor_bps']:.0f}bps/round-trip   "
         f"admission bar {cfg['admission_floor_bps']:.0f}bps "
         f"({cfg['stress_mult']:g}x cost)"),
        (f"adoption {pooled['adoption_rate']:.0%}   TPR {pooled['tpr']:.0%}   "
         f"random TPR {pooled['random_tpr']:.0%}   lift {pooled['lift']:+.0%}   "
         f"carries information: {pooled['carries_information']}"),
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", type=int, default=240)
    ap.add_argument("--days", type=int, default=260)
    ap.add_argument("--seeds", type=int, default=5, help="how many independent worlds per cell")
    ap.add_argument("--json", type=str, default="", help="write the full report here")
    args = ap.parse_args()

    cfg = PanelConfig(symbols=args.symbols, days=args.days)
    started = time.time()
    report = calibrate(cfg, worlds=DEFAULT_WORLDS, seeds=tuple(range(args.seeds)))
    elapsed = time.time() - started

    print(f"synthetic market: {cfg.symbols} symbols x {cfg.days} sessions, "
          f"sigma {cfg.sigma:.1%}, winner cut {cfg.threshold:.0%}")
    print(_table(report))
    print(f"\n{report['pooled']['runs']} gate runs in {elapsed:.1f}s")

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"report -> {path}")

    # The only hard failure: a gate that admits noise, or one that cannot admit
    # a real edge. Both make every verdict in the repo uninterpretable.
    null_rate = report["worlds"].get("null", {}).get("rate", 0.0)
    placebo_rate = report["worlds"].get("placebo", {}).get("rate", 0.0)
    strong = report["worlds"].get("strong", {}).get("rate", 0.0)
    if null_rate > 0.10 or placebo_rate > 0.10:
        print("FAIL: the gate admits noise (null/placebo rate > 10%)")
        return 1
    if strong < 0.80:
        print("FAIL: the gate cannot admit a +1.0%/trade edge (TPR < 80%)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
