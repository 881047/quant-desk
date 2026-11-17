"""Build the market-state history and print what today looks like inside it.

    python scripts/state_history.py --start 20230101
    python scripts/state_history.py --start 20050101 --day 20261023 --json

Run this before wiring the block into `discover`: if most buckets come back with
``n < 30``, the bucketing is too fine and any "conditional distribution" built on
it is a story with error bars drawn on.
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
from service.research import state as state_mod


def table(rows: list[dict]) -> str:
    head = (f"{'bucket':14s} {'n':>5s} {'segs':>5s} {'recent':>6s} {'ok':>3s} "
            f"{'fwd5 p50':>9s} {'fwd20 p50':>10s} {'win20':>6s}")
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['bucket']:14s} {r['n']:5d} {r['episodes']:5d} {r['n_recent']:6d} "
            f"{'yes' if r['usable'] else 'NO':>3s} "
            f"{(r['fwd_5d_p50'] if r['fwd_5d_p50'] is not None else float('nan')):9.2%} "
            f"{(r['fwd_20d_p50'] if r['fwd_20d_p50'] is not None else float('nan')):10.2%} "
            f"{(r['fwd_20d_win'] if r['fwd_20d_win'] is not None else float('nan')):6.1%}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="20230101")
    parser.add_argument("--day", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    store = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
    daily = store.read("daily", args.start, "20991231")
    if daily.empty:
        print("[fail] daily is empty")
        return 2

    state = state_mod.build_state(daily, start=args.start)
    if state.empty:
        print("[fail] state build produced nothing")
        return 2

    reading = state_mod.reading(state, args.day or None)
    rows = state_mod.bucket_table(state)

    if args.json or args.json_out:
        payload = {"reading": reading, "buckets": rows,
                   "coverage": reading.get("coverage") if reading.get("ok") else {
                       "sessions": len(state)}}
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        if args.json_out:
            Path(args.json_out).write_text(text, encoding="utf-8")
            print(f"[saved] {args.json_out}")
        else:
            print(text)
        return 0

    print(f"[state] {args.start} → {state.index.max()}, sessions={len(state)}")
    print(table(rows))
    usable = sum(1 for r in rows if r["usable"])
    print(f"\nbuckets: {len(rows)}, usable (n>={state_mod.MIN_BUCKET_N}): {usable}")
    print()
    if not reading.get("ok"):
        print(f"[reading] {reading.get('error')}")
        return 0
    s = reading["state"]
    print(f"[today] {reading['as_of']}  bucket={reading['bucket']}")
    for name, cell in s.items():
        print(f"  {name:12s} {cell['value']!s:>8s}  pct={cell['pct']!s:>5s}  {cell['tercile']}")
    h = reading["history"]
    print(f"  history      n={h.get('n')}  段数={h.get('episodes')}  "
          f"(last year {h.get('n_recent')})  usable={h.get('usable')}")
    def dist(label: str, d: dict) -> None:
        if d.get("n"):
            print(f"  {label:12s} p25 {d['p25']:+.2%}  p50 {d['p50']:+.2%}  "
                  f"p75 {d['p75']:+.2%}  win {d['win']:.0%}  n={d['n']}")

    for hz in state_mod.HORIZONS:
        dist(f"fwd_{hz}d", h.get(f"fwd_{hz}d") or {})
        dist(f"fwd_{hz}d 3y", h.get(f"fwd_{hz}d_3y") or {})
    if h.get("recent_share") is not None:
        print(f"  recent share {h['recent_share']:.0%} of the sample is the last year")
    if h.get("note"):
        print(f"  note         {h['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
