"""Export the trial ledger to a versioned JSON snapshot.

The book of record lives in ``data/research.sqlite``, which is gitignored — it is
a database, and committing a mutable binary once per session is worse than not
committing it. But a ledger that can be deleted is a ledger whose denominator
resets to zero, and zero is the flattering direction: with no history every new
idea deflates by its own grid alone.

So the counts get exported. The snapshot is append-only in spirit: it is
rewritten wholesale, but the family totals can only grow, and a diff that shows
them shrinking is the one thing worth reading.

    python scripts/export_trials.py
    python scripts/export_trials.py --check   # 失败退出，用于 CI / 提交前
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.research.registry import DEFAULT_FAMILY, TrialRegistry

OUT = ROOT / "reports" / "trial_ledger.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="只比较，不写；账本与快照不一致时退出码 1")
    parser.add_argument("--family", default=DEFAULT_FAMILY)
    args = parser.parse_args(argv)

    registry = TrialRegistry(ROOT / "data" / "research.sqlite")
    current = registry.snapshot(args.family)

    if args.check:
        if not OUT.exists():
            print(f"[fail] 没有快照：{OUT}")
            return 1
        previous = json.loads(OUT.read_text(encoding="utf-8"))
        problems = []
        if current["trials_total"] != previous.get("trials_total"):
            problems.append(f"trials_total {previous.get('trials_total')} -> "
                            f"{current['trials_total']}")
        if current["variants_total"] != previous.get("variants_total"):
            problems.append(f"variants_total {previous.get('variants_total')} -> "
                            f"{current['variants_total']}")
        if current["variants_total"] < previous.get("variants_total", 0):
            problems.append("分母变小了——它只应该增长")
        if problems:
            print("[stale] 快照与账本不一致：")
            for p in problems:
                print(f"  {p}")
            print("  跑一次 python scripts/export_trials.py 并一起提交。")
            return 1
        print(f"[ok] family={current['family']} "
              f"{current['trials_total']} 条 look / {current['variants_total']} 次搜索"
              f"（已回收 {current['superseded']} 条，仍在分母里）")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[saved] {OUT}")
    print(f"  family={current['family']} "
          f"{current['trials_total']} 条 look / {current['variants_total']} 次搜索 "
          f"来源 {current['by_source']}")
    print(f"  min_p {current['min_p']}  BH {current['program_fdr']}  "
          f"q=0.10 存活 {current['survivors_q10']} 条"
          f"（其中仍在场上 {current['survivors_live']} 条）")
    print(f"  仍在场上 {current['live']} 条，已回收 {current['superseded']} 条"
          f"（分母 {current['variants_total']} 含全部已回收的）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
