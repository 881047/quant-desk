"""Weekly entry point for the two opt-in research data syncs.

Neither sync belongs in the 17:30 daily job (see docs/12-automation-chain.md
§12). This wrapper decides *whether* there is anything to do, opens a new
reporting cycle when the disclosure window moves, and runs both syncs with
bounded batches so a weekly task cannot hammer the vendor or sit in the
17:30-18:00 window.

    python scripts/sync_research_data.py --dry-run
    python scripts/sync_research_data.py --batch 2000
    python scripts/sync_research_data.py --force --batch 0
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.adapters.parquet_store import DatasetStore  # noqa: E402
from service.adapters.sqlite_store import Registry  # noqa: E402
from service.research.information import _fundamental_sla  # noqa: E402
from scripts import quarterly_fundamentals as quarterly  # noqa: E402
from scripts import sync_forecast  # noqa: E402

LOOKBACK_DAYS = 45


def _due(sla: dict) -> bool:
    """A new disclosure season, or an ingest that has not caught up with it."""
    if not sla.get("available"):
        return True  # unknown is a reason to check, never a reason to skip
    if sla.get("sla_breached"):
        return True
    return (sla.get("days_since_announcement") or 10**6) <= LOOKBACK_DAYS


def _new_cycle(state: dict, period: str) -> bool:
    """Only a NEW reporting period restarts the full-universe sweep.

    Deriving `since` from "today minus 45 days" makes it change on every run;
    resetting the cursor each week would forever rescan the first batch and
    never reach the rest of the market. Within one period the cursor keeps
    advancing until cycle_done, then the sync idles.
    """
    return bool(period) and str(state.get("cycle_period") or "") != period


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="每周研究数据增量同步（不进日更）")
    ap.add_argument("--batch", type=int, default=400,
                    help="本轮最多扫描多少只；0 = 剩余全部")
    ap.add_argument("--force", action="store_true", help="忽略 due 判断")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    today = datetime.now().strftime("%Y%m%d")
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    store = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
    sla = _fundamental_sla(store, today)
    due = _due(sla)
    print(f"[due] day={today} since={since} breached={sla.get('sla_breached')} "
          f"days_since={sla.get('days_since_announcement')} due={due}", flush=True)
    if not due and not args.force:
        print("[skip] 没有新披露且同步已追平；用 --force 强制", flush=True)
        return 0
    if args.dry_run:
        print("[dry-run] 不写游标、不调用接口", flush=True)
        return 0

    # A new reporting period starts a fresh full-universe sweep. Within the same
    # period the cursor must keep advancing; never rewind it on a rolling date.
    state = quarterly._cursor()
    period = str(sla.get("latest_period") or "")
    if _new_cycle(state, period):
        cycle_since = str(sla.get("latest_announced_on") or since)
        state.update({"since": cycle_since, "last_code": "", "cycle_done": False,
                      "cycle_period": period})
        quarterly.CURSOR.parent.mkdir(parents=True, exist_ok=True)
        quarterly.CURSOR.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        print(f"[cycle] 新报告期 {period}（公告 {cycle_since}），游标重置", flush=True)
    else:
        since = str(state.get("since") or since)
        print(f"[cycle] 沿用报告期 {period} 的游标 last_code="
              f"{state.get('last_code') or 'BEGIN'}", flush=True)

    rc1 = quarterly.main(["--since", since, "--limit", str(args.batch)])
    rc2 = sync_forecast.main(["--since", since])
    print(f"[done] quarterly_rc={rc1} forecast_rc={rc2}", flush=True)
    return 0 if rc1 == 0 and rc2 == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
