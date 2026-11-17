"""Prepare the desk before the model's session.

    python scripts/prepare_session.py

Two things happen, both idempotent, neither of which decides anything:

1. warm the historical analog index (cold build is tens of seconds; warm is 0.2 s)
   so the first ``quant_analog`` in the session does not pay the cold-start cost;
2. compute the observation snapshot (constraint state, rule drift, budget) and
   print it, so the session opens with the facts already on screen.

Why a separate script: the daily data job must not touch the paper account, and
the model must not pay the index build inside its own turn.
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

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.trading import analog, build_desk
from service.trading.observation import snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-analog", action="store_true", help="skip the index rebuild")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    index_info: dict = {"skipped": True}
    desk = build_desk(ROOT)
    if not args.no_analog:
        try:
            index = analog.get_index(desk.data, ROOT)
            index_info = {"rows": index.meta["rows"], "days": index.meta["days"],
                          "start": index.meta.get("start"), "end": index.meta["end"],
                          "built_at": index.meta["built_at"]}
        except Exception as exc:  # noqa: BLE001 - a cold index must not block the session
            index_info = {"error": f"{type(exc).__name__}: {exc}"}

    day = desk.market.latest_bar_date()
    obs = snapshot(desk, day)
    payload = {
        "as_of": day,
        "analog_index": index_info,
        "counts": obs.get("counts"),
        "constraints": obs.get("constraints"),
        "deviations": [
            {"id": d.get("id"), "kind": d.get("kind"),
             "attention": d.get("attention"),
             "max_drift": (d.get("fact") or {}).get("max_drift"),
             "orders": len((d.get("measures") or {}).get("orders") or [])}
            for d in obs.get("deviations", [])
        ],
        "budget": obs.get("budget"),
        "seconds": round(time.time() - started, 1),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        counts = payload.get("counts") or {}
        print(f"[prepare] {day} | analog {index_info.get('rows', 'skip')} rows "
              f"{index_info.get('start', '?')}~{index_info.get('end', '-')} | "
              f"约束 {counts.get('constraints', 0)} "
              f"（高关注 {counts.get('high_attention', 0)}）"
              f" 偏离 {counts.get('deviations', 0)} | {payload['seconds']}s")
        for record in payload.get("constraints") or []:
            fact = record.get("fact") or {}
            print(f"  {record.get('id')}: {fact}")
        for record in payload.get("deviations") or []:
            print(f"  {record['id']}: 最大偏离 {record.get('max_drift')}，"
                  f"{record.get('orders')} 笔订单待定")
        print("  只有状态与测算，没有建议。")
    return 1 if index_info.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
