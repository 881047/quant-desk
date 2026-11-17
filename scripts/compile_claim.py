"""Compile a sentence into a testable claim, and answer the cheap one now.

    python scripts/compile_claim.py "跌多了会反弹"
    python scripts/compile_claim.py "加 5% 止损会更好" --trades limit_up_first
    python scripts/compile_claim.py "炸板股次日开盘买入，持有一天" --json
    python scripts/compile_claim.py --batch claims.txt      # refusal rate

`--batch` reads one claim per line and reports how many could not be compiled.
That ratio is the point: it is the share of the model's beliefs that were never
testable to begin with.

`--trades` names a preset to build a base trade table from. An overlay claim is a
statement about trades that already happened, so it needs a book to sit on; the
table is built with the preset's own spec and is not tuned to make the rule look
good.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.research import claim as claim_mod
from service.research import runner as runner_mod
from service.research import state as state_mod
from service.research.presets import PRESETS
from service.research.registry import TrialRegistry
from service.research.spec import build_trades, parse_spec


def _state(store) -> object:
    return state_mod.cached_state(store)


def _trades(store, preset: str):
    if preset not in PRESETS:
        raise SystemExit(f"未知预设 {preset}；可选：{', '.join(sorted(PRESETS))}")
    spec = parse_spec(dict(PRESETS[preset]))
    panel, pools, market = runner_mod.load_inputs(store, spec)
    trades = build_trades(panel, pools, spec, market, equity=runner_mod._equity())
    return trades, spec


def _book(trials: TrialRegistry, claim, evaluation: dict, *,
          base_spec, state) -> dict | None:
    """Append this look to the family. Cheap looks count as much as expensive ones."""
    look = claim_mod.as_look(claim, evaluation)
    if look is None:
        return None
    if look["kind"] == "bucket":
        window = (str(state.index.min()), str(state.index.max()))
        return trials.record_bucket(
            bucket=look["bucket"], horizon=look["horizon"], window=window,
            p_value=look["p_value"], effect=look["effect"],
            n_episodes=look["n_episodes"], source="claim:bucket")
    return trials.record_overlay(
        base_spec, rule=look["rule"], params=look["params"],
        p_value=look["p_value"], effect=look["effect"],
        n_trades=look["n_trades"], n_fired=look["n_fired"],
        p_one_sided=look["p_one_sided"], source="claim:overlay")


def _print_registry(result: dict) -> None:
    rec = result.get("registry")
    if not rec:
        return
    print(f"\n[registry] family={rec['family']}  这次是第 {rec['n_looks']} 次跑同一个假设")
    print(f"  本族累计 {rec['trials_total']} 条 look / {rec['variants_total']} 次搜索"
          f"（来源 {rec['by_source']}）")
    if rec.get("superseded"):
        print(f"  其中已被后续证据推翻 {rec['superseded']} 条"
              f"（占 {rec['superseded_share']:.0%}，仍然算在分母里）")
    print(f"  本族最小 p {rec['min_p']}  BH 调整后 {rec['program_fdr']}  "
          f"通过 q=0.10 的 {rec['survivors_q10']} 条")
    print(f"  这一条的 p={rec['this_p']}  调整后 p={rec['p_adjusted']}")
    print("  BH 的 q=0.10 意思是「值得再看一次」，不是「可以上」；"
          "上不上由裁判（DSR/PBO/CPCV/账户）决定。")
    print("  记账不影响结论，只影响「这个族一共试了多少次」——而那正是 DSR 的分母。")


def _print_overlay(ev: dict) -> None:
    e = ev["evidence"]
    print(f"\n[evidence] {e['rule']} 参数={json.dumps(e['params'], ensure_ascii=False)}  "
          f"触发={e['trigger']}")
    print(f"  样本      {e['n_trades']} 笔，规则改变出场 {e['n_fired']} 笔"
          f"（{e['fired_share']:.1%}）"
          + (f"，另有 {e['crossed_on_last_leg']} 笔在出场日触发但出场未变"
             if e.get("crossed_on_last_leg") else ""))
    for arm in ("without", "with"):
        d = e["arms"][arm]
        print(f"  {arm:8s}  n={d['n']:5d}  均值 {d['mean']:+.3%}  中位 {d['median']:+.3%}  "
              f"p25 {d['p25']:+.3%}  p75 {d['p75']:+.3%}  胜率 {d['win']:.1%}")
    p = e["paired_diff"]
    print(f"  配对差     {p['mean']:+.3%}/笔   95%CI [{p['ci_low']:+.3%}, {p['ci_high']:+.3%}]"
          f"   p(≤0) {p['p_value']:.3f}   口径 {p['p_basis']}")
    a = e["affected_diff"]
    if a.get("n"):
        print(f"  仅受影响   n={a['n']}  均值 {a['mean']:+.3%}  中位 {a['median']:+.3%}  "
              f"变好占比 {a['better_share']:.1%}  区间 [{a['ci_low']:+.3%}, {a['ci_high']:+.3%}]")
    if e.get("days_saved"):
        print(f"  提前天数   均值 {e['days_saved']['mean']:.1f}  中位 {e['days_saved']['median']:.0f}")
    print(f"  重建误差   {e['reconstruction_error']:.2e}（日腿复利还原 gross 的误差）")
    print(f"  方向       点估计与主张{'一致' if e.get('sign') == 'agrees' else '相反'}")
    print("\n[读法]")
    for line in e["basis"]:
        print(f"  口径      {line}")
    for line in e["caveats"]:
        print(f"  注意      {line}")
    print("\n  这不是判决。要不要采用这条规则，由你判断。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="*", help="一句主张")
    parser.add_argument("--batch", default="", help="每行一条主张")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-eval", action="store_true", help="只编译，不查证据")
    parser.add_argument("--trades", default="",
                        help="overlay 用的基准交易表，取一个 preset 名")
    parser.add_argument("--no-record", action="store_true",
                        help="不要把这次 look 记进试验账本")
    args = parser.parse_args(argv)

    store = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))

    if args.batch:
        lines = [ln.strip() for ln in Path(args.batch).read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        report = claim_mod.refusal_rate(lines)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    text = " ".join(args.text).strip()
    if not text:
        print("用法: python scripts/compile_claim.py \"跌多了会反弹\"")
        return 2

    compiled = claim_mod.compile_claim(text)
    if isinstance(compiled, claim_mod.Refusal):
        payload = compiled.as_dict()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        else:
            print(f"[refused] {payload['text']}")
            print(f"  原因  {payload['reason']}")
            for need in payload["needs"]:
                print(f"  需要  {need}")
        return 1

    result = {"claim": compiled.as_dict()}
    base_spec = None
    if not args.no_eval:
        state = _state(store) if compiled.kind == "bucket" else None
        trades = None
        if compiled.kind == "overlay" and args.trades:
            trades, base_spec = _trades(store, args.trades)
        result["evaluation"] = claim_mod.evaluate(compiled, state, trades=trades)
        if not args.no_record:
            trials = TrialRegistry(ROOT / "data" / "research.sqlite")
            result["registry"] = _book(trials, compiled, result["evaluation"],
                                       base_spec=base_spec, state=state)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    c = result["claim"]
    print(f"[compiled] {c['text']}")
    print(f"  form      {c['kind']}")
    print(f"  spec      {json.dumps(c['spec'], ensure_ascii=False)}")
    print(f"  horizon   {c['horizon']} 日   direction {c['direction']}")
    for a in c["assumptions"]:
        print(f"  assumes   {a}")

    ev = result.get("evaluation") or {}
    if not ev:
        return 0
    if not ev.get("ok"):
        print(f"  [cannot judge] {ev.get('error')}")
        return 0
    if ev.get("kind") == "overlay" and ev.get("evidence"):
        _print_overlay(ev)
        _print_registry(result)
        return 0
    if ev.get("kind") != "bucket":
        print(f"  status    {ev.get('status')}")
        print(f"  next      {ev.get('next')}")
        return 0

    e = ev["evidence"]
    print(f"\n[evidence] bucket={e['bucket']} horizon={e['horizon']}  "
          f"days={e['days']} episodes={e['episodes']}")
    print(f"  条件均值   {e['conditional_mean']:+.2%}   中位 {e['conditional_median']:+.2%}")
    print(f"  无条件均值 {e['unconditional_mean']:+.2%}")
    print(f"  差         {e['diff']:+.2%}   95%CI [{e['ci_low']:+.2%}, {e['ci_high']:+.2%}]")
    print(f"  p(双尾)    {e['p_two_sided']:.3f}   p(优于平均) {e['p_better_than_average']:.3f}")
    print(f"  口径       {e['basis']}")
    if e.get("sign") == "contrary":
        print(f"  方向       点估计与主张相反（{e['diff']:+.2%}）")

    pol = ev["policy"]
    print(f"\n[policy] {pol['name']}：p ≤ {pol['p_max']} 才记 consistent / contradicted")
    reading = claim_mod.threshold_reading(e, pol)
    print(f"  本台记账会记为 {reading['reading']}"
          f"（p={reading['p_two_sided']:.4f}，线 {reading['p_max']:g}）")
    print("  这是记账约定，不是结论。上面的数字已经足够，你可以不同意这个阈值。")
    _print_registry(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
