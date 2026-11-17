"""Ask whether the desk may conclude about today — before someone concludes anyway.

`daily_job` writes `data/ALERT.txt` when it runs and fails. It cannot write
anything on a day it never starts: the machine was off, the task was disabled, the
user was logged out. That is the case nobody notices until a close report claims
six affordable names where the session had 23 — which is what happened on
2026-10-29, when the bar for the day had never reached the store.

This check is read-only and cheap, so it is safe to schedule twice a day:

    09:00  before the open   — is yesterday settled?
    17:45  after the close   — did today's pipeline deliver?

Exit code carries the answer for Task Scheduler; `data/READINESS_ALERT.txt`
carries it to the next session, where `context_pack` surfaces it as
`pipeline_alert`.

    python scripts/check_readiness.py            # today
    python scripts/check_readiness.py --as-of 20261029
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.kernel.clock import SHANGHAI
from service.trading import build_desk
from service.trading.readiness import check

ALERT = ROOT / "data" / "READINESS_ALERT.txt"


def wipe() -> None:
    ALERT.unlink(missing_ok=True)


def write(state: dict, when: datetime) -> Path:
    lines = [
        f"{state.get('target')} not ready: {state.get('code')}",
        f"- {state.get('detail', '')}",
        f"- last_bar={state.get('last_bar')} bars_behind={state.get('bars_behind')}",
        f"- blocks: {', '.join(state.get('blocks') or [])}",
        f"checked {when:%Y-%m-%d %H:%M} (scripts/check_readiness.py)",
    ]
    ALERT.parent.mkdir(parents=True, exist_ok=True)
    ALERT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ALERT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", help="YYYYMMDD; defaults to today")
    parser.add_argument("--quiet", action="store_true", help="exit code only")
    args = parser.parse_args(argv)

    now = datetime.now(SHANGHAI)
    desk = build_desk(root=ROOT)
    state = check(desk.market, day=args.as_of, now=now).as_dict()

    if state["ok"]:
        # Only this script's own alert is cleared. `daily_job`'s ALERT.txt is a
        # different file and a different failure; wiping it here would hide a
        # pipeline error behind a data-readiness pass.
        had = ALERT.exists()
        wipe()
        if args.quiet:
            # Still one line. An empty log cannot distinguish "ran and was fine"
            # from "never ran", which is the whole question this task exists to
            # answer — and it is what the last line of `daily_job` gets wrong.
            print(f"{now:%Y-%m-%d %H:%M} {state['target']} {state['code']}"
                  + (" alert=cleared" if had else ""))
        else:
            print(f"[ok] {state['target']} {state['code']}"
                  + (" (alert cleared)" if had else ""))
        return 0

    path = write(state, now)
    if args.quiet:
        print(f"{now:%Y-%m-%d %H:%M} {state['target']} {state['code']}")
    else:
        print(f"[{state['code']}] {state['target']}: {state['detail']}")
        print(f"  alert -> {path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
