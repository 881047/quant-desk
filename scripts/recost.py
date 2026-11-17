"""Re-judge every canonical preset under the kernel's cost model.

    python scripts/recost.py
    python scripts/recost.py --record      # also append each trial to the registry
    python scripts/recost.py --json reports/P21_recost.json

Why presets and not the saved verdicts: `reports/research_*.json` from P5-P8
carried a **lossy** spec — no `link_pool`, no `rank_dir` — so replaying one
silently tested a different hypothesis (正股跌停→转债 became 正股涨停→转债).
The preset in `service/research/presets.py` is the definition; the old verdict is
only the "before" answer in the comparison.

A preset with no saved verdict is still run; it just has no before/after row.
Read-only with respect to data/: it reads the parquet panel and writes a report.
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

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.research import runner
from service.research.presets import PRESETS
from service.research.registry import TrialRegistry
from service.research.spec import SpecError, parse_spec


def _old(name: str) -> dict | None:
    path = ROOT / "reports" / f"research_{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _reading(payload) -> str | None:
    """Read the gate label from either the new or the legacy key."""
    payload = payload or {}
    return payload.get("gate_reading") or payload.get("verdict")


def _excess(payload: dict | None) -> float | None:
    if not payload:
        return None
    value = ((payload.get("best") or {}).get("trades") or {}).get("mean_excess")
    return None if value is None else float(value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", default="reports/P21_recost.json")
    ap.add_argument("--record", action="store_true",
                    help="append each re-judged trial to the program registry")
    args = ap.parse_args(argv)

    store = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
    trials = TrialRegistry(ROOT / "data" / "research.sqlite") if args.record else None
    rows: list[dict] = []

    for name, raw in PRESETS.items():
        try:
            spec = parse_spec(raw)
        except SpecError as exc:
            rows.append({"preset": name, "status": "skipped", "why": str(exc)})
            continue
        old = _old(name)
        # Recosting an alias or an already-seen economic spec is another read of
        # the same evidence, not a new trial in the research denominator.
        record = bool(trials is not None and name.lower() != "cf" and trials.looks(spec) == 0)
        try:
            new = runner.run(store, spec, registry=trials, source="p21_recost",
                             record=record)
        except (SpecError, ValueError) as exc:
            rows.append({"preset": name, "status": "error", "why": str(exc)})
            continue

        before, after = _excess(old), _excess(new)
        rows.append({
            "preset": name,
            "status": "ok",
            # 机械读数的对照组：同一个假设在两套成本下门槛过没过。
            # 2026-11-03 之前这个键叫 verdict，读起来像"改的是结论"，改的是读数。
            "old_gate_reading": _reading(old),
            "new_gate_reading": _reading(new),
            "moved": bool(old) and _reading(old) != _reading(new),
            "old_excess": before,
            "new_excess": after,
            "delta_bps": (round((after - before) * 10000, 1)
                          if after is not None and before is not None else None),
            "cost_bps": new.get("cost_bps_effective"),
            "ticket_cny": (new.get("cost") or {}).get("ticket_cny"),
            "trades": ((new.get("best") or {}).get("trades") or {}).get("n"),
            "p": ((new.get("best") or {}).get("trades") or {}).get("p_value"),
            "trials_effective": new.get("trials_effective"),
            "blockers": new.get("shadow_blockers") or [],
        })

    head = (f"{'preset':<22}{'old':<10}{'new':<10}{'old xs':>9}{'new xs':>9}"
            f"{'Δbps':>8}{'cost':>7}{'trades':>8}{'p':>8}")
    print(head)
    print("-" * len(head))
    for row in rows:
        if row["status"] != "ok":
            print(f"{row['preset']:<22}{row['status']:<10}{row.get('why', '')[:44]}")
            continue
        print(f"{row['preset']:<22}{row['old_gate_reading'] or '-'!s:<10}"
              f"{row['new_gate_reading']!s:<10}"
              f"{(row['old_excess'] or 0) * 100:>8.3f}%"
              f"{(row['new_excess'] or 0) * 100:>8.3f}%"
              f"{(row['delta_bps'] or 0):>8.1f}{(row['cost_bps'] or 0):>7.1f}"
              f"{(row['trades'] or 0):>8}{(row['p'] or 0):>8.3f}")

    moved = [r for r in rows if r.get("moved")]
    print(f"\n{len(rows)} presets · {len(moved)} moved · "
          f"{sum(1 for r in rows if r['status'] != 'ok')} skipped")
    if moved:
        print("moved: " + ", ".join(
            f"{r['preset']} {r['old_gate_reading']}->{r['new_gate_reading']}"
            for r in moved))
    if trials is not None:
        status = trials.program_status()
        print(f"program: family={status['family']} trials={status['trials_total']} "
              f"min_p={status['min_p']} fdr={status['program_fdr']} "
              f"survivors(q10)={status['survivors_q10']}")

    out = ROOT / args.json
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"rows": rows, "moved": len(moved)}, indent=2,
                              ensure_ascii=False), encoding="utf-8")
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
