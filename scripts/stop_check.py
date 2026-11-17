"""Report stop breaches as a state, with the arithmetic of an exit.

    python scripts/stop_check.py
    python scripts/stop_check.py --json

This prints information, never an instruction: the close, the stop, how many
sessions it has been below, what an exit at the next open would cost, and how
this symbol behaved after similar down days. Whether to sell is not this
script's call, and there is no flag that makes it one. The automatic stop lives
elsewhere: once a breach has persisted for ``autostop.confirm_seconds``, the
capture loop (``service/trading/autostop.py``) executes the stop written on the
entry ticket through ``/decision/propose + approve``. This script only reports;
``configs/params.yaml``'s ``autostop.enabled`` can turn that execution off.

There used to be an ``--enforce`` switch that created and approved the sell
itself. It is gone: a kernel that measures the breach, prices the exit and then
also pulls the trigger has stopped being an instrument and become a strategy.

Exit code 1 is reserved for a newly observed, confirmed close below a stop.
Repeating the same breach on later runs still prints the state but does not raise
another scheduler alert; an intraday-only pierce is not a confirmed close breach.
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

from service.trading import build_desk
from service.trading.observation import stop_constraints


def _confirmed(records: list[dict]) -> dict[str, dict]:
    out = {}
    for record in records:
        fact = record.get("fact") or {}
        if record.get("kind") != "stop_breach" or fact.get("intraday_only"):
            continue
        try:
            close, stop = float(fact["close"]), float(fact["stop"])
        except (KeyError, TypeError, ValueError):
            continue
        if stop <= 0 or close > stop:
            continue
        identity = fact.get("ticket_id") or record.get("subject") or record.get("id")
        out[f"{identity}:{fact['stop']}"] = record
    return out


def _new_breaches(records: list[dict], state_path: Path) -> list[dict]:
    current = _confirmed(records)
    try:
        previous = set(json.loads(state_path.read_text(encoding="utf-8")).get("active", []))
    except (OSError, ValueError, AttributeError):
        previous = set()
    fresh = [record for key, record in current.items() if key not in previous]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"active": sorted(current)}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(state_path)
    return fresh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    desk = build_desk(ROOT)
    day = desk.market.latest_bar_date()
    records = stop_constraints(desk, day)
    fresh = _new_breaches(records, ROOT / "data" / "stop_check_state.json")
    payload = {"mode": "observe", "as_of": day, "breached": len(records),
               "new_breaches": len(fresh), "constraints": records}

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(f"[stops] as_of={day} breached={len(records)}")
        for record in records:
            fact, measures = record["fact"], record["measures"]
            exit_ = measures.get("exit_at_next_open") or {}
            print(f"  {record['subject']:11s} 收盘 {fact['close']:>7} / 止损 {fact['stop']:>7}"
                  f"  ({fact['breach_pct']:+.2%})  连续 {fact['sessions_below']} 日")
            print(f"    若要次日开盘退出: 手续费 ¥{exit_.get('fee_cny', 0)}"
                  f" + 冲击 {exit_.get('impact_bps')}bps ≈ ¥{exit_.get('impact_cny', 0)}"
                  f"，净得 ¥{exit_.get('net_if_filled_at_ref')}"
                  f"；T+1: {record['constraints'][0]}")
            hist = measures.get("history") or {}
            if hist.get("n"):
                print(f"    这只票历史上 {hist['condition']} 的日子 {hist['n']} 次，"
                      f"次日开盘买持 {hist['horizon']} 日: 中位 {hist['median']:+.2%}，"
                      f"胜率 {hist['win_rate']:.0%}，最差 {hist['worst']:+.2%}")
        if records:
            print("  要不要卖不在这里决定：用 POST /decision/propose 带上你自己的理由。")
            print("  本脚本不卖；破线持续 autostop.confirm_seconds 后，抓取循环会经"
                  " /decision/propose + approve 代执行建仓票上写好的止损"
                  "（configs/params.yaml 的 autostop.enabled 可关）。")

    return 1 if fresh else 0


if __name__ == "__main__":
    raise SystemExit(main())
