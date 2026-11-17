"""Run a research spec end to end and (optionally) write the verdict back.

    python scripts/run_research.py --preset limit_up_first
    python scripts/run_research.py --hypothesis 3
    python scripts/run_research.py --spec '{"pool":"broken_board","hold":1}'

Research runs out of process on purpose: a full pass loads years of daily bars,
and the trading service must never block behind a pandas job.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.research import runner
from service.research.presets import PRESETS
from service.research.registry import TrialRegistry
from service.research.spec import SpecError, parse_spec
from service.trading.state import PaperStore


def table(payload: dict) -> str:
    best = payload["best"]
    m = best["metrics"]
    t = best["trades"]
    rows = [
        ("gate reading", payload["gate_reading"]),
        ("shadow candidate", ("YES" if payload["shadow_candidate"] else
                              "no (" + ",".join(payload["shadow_blockers"]) + ")")),
        ("best spec", best["spec"]),
        ("trades", f"{t['n']}"),
        ("mean net / trade", f"{t['mean']:.3%}"),
        ("mean excess / trade", f"{t['mean_excess']:.3%}"),
        ("excess 95% CI", f"[{t['ci_low']:.3%}, {t['ci_high']:.3%}]"),
        ("bootstrap p", f"{t['p_value']:.3f}"),
        ("CAGR", f"{m['cagr']:.2%}"),
        ("Sharpe", f"{m['sharpe']:.2f}"),
        ("MaxDD", f"{m['max_drawdown']:.2%}"),
        ("Deflated Sharpe", (f"{payload['deflated_sharpe']['dsr']:.3f} "
                             f"({payload['deflated_sharpe']['trials']} trials, "
                             f"{payload['deflated_sharpe'].get('trials_local', '?')} local)")),
        ("cost", (f"{payload.get('cost_bps_effective', 0):.1f}bps/trade "
                  f"(ticket CNY {payload.get('cost', {}).get('ticket_cny', 0):,.0f}, "
                  f"{payload.get('cost', {}).get('model', '?')})")),
        ("trials", f"{payload['trials']} local / {payload['trials_effective']} effective"),
        ("power", (f"MDE {payload['power']['mde_bps']:.0f}bps vs bar "
                   f"{payload['power']['floor_bps']:.0f}bps over {payload['power']['days']} days"
                   + ("  UNDERPOWERED" if payload['power'].get('underpowered') else ""))),
        ("attribution", "not attached" if not (payload.get("attribution") or {}).get("n")
                        else (f"residual {payload['attribution']['mean_matched_excess']:+.3%}/trade "
                              f"(t {payload['attribution']['t_stat']}, p {payload['attribution']['p_value']}, "
                              f"n {payload['attribution']['n']}, coverage {payload['attribution']['coverage']:.0%}, "
                              f"k {payload['attribution']['k']}, {payload['attribution']['size_proxy']})")),
        ("PBO", f"{payload['pbo']['pbo']:.3f} ({payload['pbo']['splits']} splits)"),
        ("CPCV OOS p05", (f"{payload['cpcv']['oos_sharpe_p05']:.2f} "
                          f"(mean {payload['cpcv']['oos_sharpe_mean']:.2f}, "
                          f"{payload['cpcv']['positive']:.0%} positive)")),
        ("2x cost Sharpe", f"{payload['stress']['2x']['metrics']['sharpe']:.2f}"),
        ("3x cost Sharpe", f"{payload['stress']['3x']['metrics']['sharpe']:.2f}"),
        ("capacity", (f"{payload['capacity']['median_signals_per_day']:.0f} signals/day, "
                      f"median CNY {payload['capacity']['median_price']:.2f}, "
                      f"needs CNY {payload['capacity']['capital_one_lot_each']:,.0f}, "
                      f"affordable {payload['capacity']['max_names_affordable']} names")),
    ]
    basis = payload.get("metric_basis") or {}
    if basis:
        rows.append(("sample window",
                     (f"{basis.get('calendar_days', 0)} sessions, "
                      f"exposure {(basis.get('exposure') or 0):.0%}")))
    gate = payload.get("sell_gate") or {}
    if gate.get("blocked"):
        rows.append(("sell gate", (
            f"{gate['blocked']} exits blocked at the lower limit, "
            f"{gate.get('rolled', 0)} rolled to the next session, "
            f"{gate.get('stuck', 0)} still had no bid")))
    account = payload.get("account") or {}
    if account.get("metrics"):
        am = account["metrics"]
        rows.append(("account (CNY 5k)", (
            f"{account.get('trades_taken', 0)} taken / {account.get('trades_skipped', 0)} skipped, "
            f"total {am.get('total_return', 0):.2%}, CAGR {am.get('cagr', 0):.2%}, "
            f"Sharpe {am.get('sharpe', 0):.2f}, avg {account.get('avg_positions', 0):.2f} pos")))
    if account.get("skip_reasons"):
        rows.append(("account skips", str(account["skip_reasons"])))

    pol = payload.get("policy") or {}
    if pol.get("gates"):
        failed = [g for g in pol["gates"] if not g["passed"]]
        rows.append(("policy", pol["policy"]["name"]))
        if failed:
            for g in failed:
                rows.append((f"  x {g['gate']}",
                             f"{g['value']} {g['op']} {g['threshold']} -> {g['fails_as']}"
                             + (f"  ({g['detail']})" if g.get("detail") else "")))
        else:
            rows.append(("  all gates", "passed"))
        promo = pol.get("promotion") or {}
        if promo.get("gates") and not promo["all_passed"]:
            blocked = [g["gate"] for g in promo["gates"] if not g["passed"]]
            rows.append(("  promotion blocked by", ", ".join(blocked)))
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"  {k.ljust(width)}  {v}" for k, v in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", default="", help="JSON spec")
    parser.add_argument("--preset", default="", choices=sorted(PRESETS))
    parser.add_argument("--hypothesis", type=int, default=0, help="run a stored hypothesis")
    parser.add_argument("--start", default="20230101")
    parser.add_argument("--end", default="20261023")
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--json-out", default="")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--no-record", action="store_true",
                        help="do not append this run to the trial registry")
    # 2026-11-12: a real promotion path must always attach the
    # characteristic-matched benchmark ("a real promotion path should always
    # turn it on", runner._run_impl docstring). Default ON for the CLI;
    # --no-attribution is an explicit escape hatch for a cheap diagnostic run,
    # and the payload says which one happened.
    parser.add_argument("--no-attribution", action="store_true",
                        help="skip the characteristic-matched benchmark (diagnostic only; "
                             "promotion paths must not use this)")
    parser.add_argument("--include-t-close", action="store_true",
                        help="add entry=t_close as a competing grid variant. Widens the "
                             "search family and changes the DSR denominator, so this "
                             "verdict is NOT comparable with the default family's "
                             "on-book verdicts")
    args = parser.parse_args(argv)
    args.attribution = not args.no_attribution
    try:
        start = datetime.strptime(args.start, "%Y%m%d").strftime("%Y%m%d")
        end = datetime.strptime(args.end, "%Y%m%d").strftime("%Y%m%d")
        if start != args.start or end != args.end or start > end:
            raise ValueError("invalid date range")
        if not math.isfinite(args.cost_bps) or args.cost_bps < 0 or args.hypothesis < 0:
            raise ValueError("invalid numeric argument")
    except ValueError as exc:
        print(f"[fail] {exc}: check dates, --cost-bps and --hypothesis")
        return 2

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    paper = PaperStore(ROOT / "data" / "paper.sqlite")
    trials = TrialRegistry(ROOT / "data" / "research.sqlite")

    if args.hypothesis:
        hyp = paper.get_hypothesis(args.hypothesis)
        if hyp is None:
            print(f"[fail] hypothesis {args.hypothesis} not found")
            return 2
        raw = hyp.get("test_spec") or "{}"
        print(f"[hypothesis {args.hypothesis}] {hyp['statement']}")
    elif args.preset:
        raw = dict(PRESETS[args.preset])
        print(f"[preset] {args.preset}")
    else:
        raw = args.spec or "{}"

    try:
        spec = parse_spec(raw)
    except SpecError as exc:
        print(f"[fail] {exc}")
        return 2

    spec = spec.with_variant(start=args.start, end=args.end, cost_bps=args.cost_bps)
    print(f"[spec] {json.dumps({k: v for k, v in spec.__dict__.items() if k != 'label'}, ensure_ascii=False)}")

    try:
        payload = runner.run(store, spec, registry=trials,
                             record=not args.no_record,
                             attach_attribution=args.attribution,
                             include_t_close=args.include_t_close)
    except (SpecError, ValueError) as exc:
        print(f"[fail] {exc}")
        return 2
    print(f"\n[result] {spec.name()}\n")
    print(table(payload))
    for reason in payload["reasons"]:
        print(f"  -> {reason}")
    program = payload.get("program") or {}
    if program.get("recorded") is False:
        print(f"  program: 未记账；DSR 分母仍按本族 {program.get('family_variants')} 次"
              f" + 本次 {program.get('local_variants')} 次 = {program.get('dsr_trials')} 次搜索")
    else:
        print(f"  program: family={program.get('family')} "
              f"trials={program.get('trials_total')} looks={program.get('n_looks')} "
              f"this_p={program.get('this_p')} p_adj={program.get('p_adjusted')} "
              f"fdr={program.get('program_fdr')} "
              f"survivors(q10)={program.get('survivors_q10')}")

    # `--no-persist` used to guard only the hypothesis write-back and still
    # overwrote `reports/research_<id>.json`. A flag that lies about what it
    # writes is worse than no flag: the desk's claim to reproducibility rests on
    # a verdict file not changing under it. Now the name means what it says —
    # nothing is written unless `--json-out` names a destination.
    out = Path(args.json_out) if args.json_out else (
        None if args.no_persist
        else ROOT / "reports" / f"research_{args.hypothesis or args.preset or 'custom'}.json")
    if out is None:
        print()
        print("[not saved] --no-persist: report not written "
              "(pass --json-out=<path> to keep a copy)")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        print()
        print(f"[saved] {out}")

    if args.hypothesis and not args.no_persist:
        result = {
            # 机械读数。判断不在这里，也不该在这里。
            "gate_reading": payload["gate_reading"],
            "decided_by": payload["decided_by"],
            "shadow_candidate": payload["shadow_candidate"],
            "shadow_blockers": payload["shadow_blockers"],
            "reasons": payload["reasons"],
            "spec": payload["spec"],
            "best": payload["best"],
            "capacity": payload["capacity"],
            "dsr": payload["deflated_sharpe"],
            "pbo": payload["pbo"],
            "cpcv": payload["cpcv"],
            "stress": {k: v["metrics"] for k, v in payload["stress"].items()},
            "trials": payload["trials"],
            "report": str(out) if out else None,
        }
        # status 是这本账簿的登记状态（跑过没有、门槛过没过），不是市场判断。
        paper.update_hypothesis(args.hypothesis, payload["gate_reading"], result)
        print(f"[persisted] hypothesis {args.hypothesis} -> {payload['gate_reading']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
