"""Record that one trial was overturned by another — or that it was not.

    python scripts/supersede_trial.py list
    python scripts/supersede_trial.py supersede 13 --by 14 --reason "同一份数据上..."
    python scripts/supersede_trial.py unsupersede 13 --reason "..."

Why this is a separate command and not an automatic rule: deciding that evidence
B overturns claim A is a *reading*, and this desk keeps readings out of code
(P29). The kernel's job is to make the two rows and their numbers visible at the
moment of the decision, and to write down what was decided and why.

Nothing is deleted. A superseded trial still counts in the Deflated Sharpe
denominator, because the look still happened — the only thing that changes is
whether it is still a candidate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.research.registry import DEFAULT_FAMILY, TrialRegistry


def _row(registry: TrialRegistry, trial_id: int) -> dict | None:
    for row in registry.list(limit=10_000):
        if row["id"] == trial_id:
            return row
    return None


def _show(registry: TrialRegistry, trial_id: int, header: str) -> None:
    row = _row(registry, trial_id)
    if row is None:
        print(f"  [{header}] trial {trial_id} 不存在")
        return
    print(f"  [{header} #{trial_id}] {row['source']}  pool={row['pool']}  "
          f"{row['window_start']}..{row['window_end']}")
    print(f"           p={row['p_value']}  effect={row['effect']}  "
          f"n_trades={row['n_trades']}  verdict={row['verdict'] or '-'}")


def _status(registry: TrialRegistry, family: str) -> None:
    st = registry.program_status(family)
    print(f"\n[family {st['family']}] {st['trials_total']} 条 look / "
          f"{st['variants_total']} 次搜索（分母，含全部已回收的）")
    print(f"  仍在场上 {st['live']} 条，已回收 {st['superseded']} 条"
          f"（{st['superseded_share']:.0%}）")
    if st["superseded_effect_mean"] is not None:
        print(f"  被回收者的平均 effect {st['superseded_effect_mean']:+.4%}"
              "（正数说明被推翻的曾是看起来有希望的那些）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", default=DEFAULT_FAMILY)
    parser.add_argument("--db", default=str(ROOT / "data" / "research.sqlite"),
                        help="账本路径；默认是本机的那一本")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出当前状态")

    sup = sub.add_parser("supersede", help="标记一条 trial 被另一条推翻")
    sup.add_argument("trial_id", type=int)
    sup.add_argument("--by", type=int, required=True)
    sup.add_argument("--reason", required=True)

    undo = sub.add_parser("unsupersede", help="撤销一条回收")
    undo.add_argument("trial_id", type=int)
    undo.add_argument("--reason", required=True)

    args = parser.parse_args(argv)
    registry = TrialRegistry(args.db)

    if args.cmd == "list":
        labels = registry.supersessions(args.family)
        for row in reversed(registry.list(limit=10_000, family=args.family)):
            label = labels.get(row["id"]) or {}
            mark = (f"  <- 被 #{label['superseded_by']} 推翻"
                    if label.get("superseded_by") else "")
            print(f"  #{row['id']:<3} {row['source']:<14} {row['pool'] or ''!s:<20} "
                  f"p={row['p_value']!s:<8} effect={row['effect']!s:<11}{mark}")
            if label.get("reason"):
                print(f"        理由：{label['reason']}")
        _status(registry, args.family)
        return 0

    if args.cmd == "supersede":
        print("[before]")
        _show(registry, args.trial_id, "被推翻")
        _show(registry, args.by, "推翻者")
        try:
            result = registry.supersede(args.trial_id, by=args.by,
                                        reason=args.reason, family=args.family)
        except ValueError as exc:
            print(f"[fail] {exc}")
            return 2
        print(f"\n[{'recorded' if result['changed'] else 'already'}] "
              f"#{args.trial_id} <- #{args.by}")
        _status(registry, args.family)
        print("\n  分母没有变小：被推翻的那次 look 仍然算在 DSR 里。")
        return 0

    try:
        result = registry.unsupersede(args.trial_id, reason=args.reason,
                                      family=args.family)
    except ValueError as exc:
        print(f"[fail] {exc}")
        return 2
    print(f"[{'recorded' if result['changed'] else 'already'}] "
          f"#{args.trial_id} 恢复在场")
    _status(registry, args.family)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
