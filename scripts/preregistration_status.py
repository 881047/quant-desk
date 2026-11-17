"""Read-only progress check for the D-23 pre-registration.

Counts *partition filenames* only. It never opens a parquet file, never reads a
bucket statistic, and therefore cannot peek at the locked-out holdout even once
the data exists. "How many eligible sessions have arrived" is a question about
the calendar; "what did the early-session buckets do" is the study itself, and
this script is forbidden from answering it.

Usage::

    python scripts/preregistration_status.py --json
    python scripts/preregistration_status.py --root /path/to/data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.trading.preregistration import V1, readiness


def status(root: Path) -> dict:
    """Pure read of directory names; no parquet is ever loaded."""
    tape_dir = root / "parquet" / "pool_tape"
    days: list[str] = []
    if tape_dir.is_dir():
        for path in tape_dir.glob("*.parquet"):
            day = path.stem
            if (len(day) == 8 and day.isdigit()
                    and day >= V1.first_eligible_day
                    and day not in V1.contaminated_days):
                days.append(day)
    days.sort()

    eligible_days = len(days)
    # This is the number that **must be reserved**, not a claim that the tail
    # was left unread. Whether anyone peeked is an audit question, not a file
    # count; the pre-registration log answers it.
    holdout_reserved = int(eligible_days * V1.holdout_fraction)
    # No regime labels are published in the data layer yet; do not invent them.
    regime_labels = 0
    gate = readiness(eligible_days, holdout_reserved, regime_labels)
    if eligible_days == 0:
        state = "insufficient_confirmatory_samples"
    elif not gate["allowed_to_test"]:
        state = "below_gate"
    else:
        state = "sample_gate_met_awaiting_human_decision"
    return {
        "ok": True,
        "version": V1.version,
        "root": str(root),
        "first_eligible_day": V1.first_eligible_day,
        "contaminated_days": list(V1.contaminated_days),
        "eligible_days": eligible_days,
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "holdout_reserved_days": holdout_reserved,
        "regime_labels": regime_labels,
        "status": state,
        "readiness": gate,
        "note": ("只读分区文件名；不打开任何 parquet、不读任何分桶统计、"
                 "不产生研究结论、不改配置、不激活意图层。"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D-23 pre-registration sample progress")
    ap.add_argument("--root", default=str(ROOT / "data"), help="data root")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    out = status(Path(args.root))
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"[preregistration] {out['version']} status={out['status']} "
              f"eligible_days={out['eligible_days']}/"
              f"{V1.min_confirmatory_sessions} "
              f"holdout_reserved={out['holdout_reserved_days']} "
              f"regimes={out['regime_labels']}/{V1.min_regimes}")
        for check in out["readiness"]["checks"]:
            mark = "OK" if check["ok"] else "GAP"
            print(f"  {mark:3s} {check['name']:26s} "
                  f"{check['actual']} / {check['required']}")
        print(f"  allowed_to_test={out['readiness']['allowed_to_test']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
