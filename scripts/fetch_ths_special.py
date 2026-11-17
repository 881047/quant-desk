"""Pull Tonghuashun special data into the store, one trading day per partition.

    python scripts/fetch_ths_special.py                     # latest trading day
    python scripts/fetch_ths_special.py --days 10           # last 10 sessions
    python scripts/fetch_ths_special.py --dates 20261020,20261021
    python scripts/fetch_ths_special.py --only pools        # skip ladder/lhb/stat
    python scripts/fetch_ths_special.py --backfill          # every dataset to its own floor
    python scripts/fetch_ths_special.py --start 20201201 --end 20261026 \\
        --datasets ths_limit_up --force
    python scripts/fetch_ths_special.py --freeze-auction  # actual today, 09:26 only
    python scripts/fetch_ths_special.py --backfill --dry-run

Datasets written (all partitioned by trade_date):

    ths_limit_up     limit-up pool: seal money, first seal time, reason, streak
    ths_limit_down   limit-down pool
    ths_limit_break  failed boards, with how many times they opened
    ths_ladder       the 30-day streak ladder, trimmed to the requested day
    ths_limit_stat   seal rate and per-board promotion rate
    ths_lhb          dragon-tiger rows, stock level
    ths_lhb_seat     dragon-tiger rows, hot-money seat level
    ths_auction      pre-open call-auction alerts
    ths_anomaly      machine-written explanations of the day's big moves

Every write goes through DatasetStore.write with mode="merge", so re-running a
day is idempotent and a partial failure can be retried without duplicating rows.

**Backfill is per dataset, not per day.** Each endpoint has its own history
limit, and they are nowhere near the same. These were measured against the
vendor, not read from docs, because the docs do not state them:

    limit-up       from 2020-12-01   (2020-07-01 returns an empty page, code=0)
    limit-down     rolling 1 year    (beyond: code=1002 Invalid parameter format)
    limit-break    rolling 1 year    (same)
    lhb            rolling 1 year    ("date must be within one year")
    ladder         last 30 sessions, date parameter silently ignored
    limit-stat     today only, date parameter silently ignored
    anomaly        today only, date parameter silently ignored
    auction        rolling 1 year    (2025-11-14: code=1003; 2025-11-17 works)

So a "backfill everything to 2021" command cannot exist. `--backfill` clamps
each dataset to its own floor and says what it could not reach.

Re-measure before trusting a longer window: the vendor changes these without
notice, and a silently short window looks exactly like a quiet market.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.adapters import ths
from service.adapters.parquet_store import DatasetStore
from service.adapters.pit import TradingCalendarStore
from service.adapters.sqlite_store import Registry
from service.kernel.clock import SHANGHAI

# name -> (group, verified floor). Floor is an absolute date, "today",
# "30d" (the ladder window) or "-1y" (a rolling one-year limit).
DATASETS: dict[str, tuple[str, str]] = {
    "ths_limit_up": ("pools", "20201201"),
    "ths_limit_down": ("pools", "-1y"),
    "ths_limit_break": ("pools", "-1y"),
    "ths_lhb": ("lhb", "-1y"),
    "ths_lhb_seat": ("lhb", "-1y"),
    "ths_ladder": ("ladder", "30d"),
    "ths_limit_stat": ("stat", "today"),
    "ths_auction": ("auction", "-1y"),
    "ths_anomaly": ("anomaly", "today"),
}

GROUPS = ("pools", "ladder", "stat", "lhb", "auction", "anomaly")

# Today-only datasets are skipped during a backfill: the endpoint ignores the
# date, so writing the same rows under 200 historical dates would fabricate a
# history that never existed.
TODAY_ONLY = {"ths_limit_stat", "ths_anomaly"}


def _today() -> str:
    return datetime.now(SHANGHAI).strftime("%Y%m%d")


def _shift_days(day: str, days: int) -> str:
    return (datetime.strptime(day, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")


def _calendar(store) -> TradingCalendarStore:
    return TradingCalendarStore.from_frame(store.read("trade_cal"))


def _window(cal: TradingCalendarStore, floor: str, end: str) -> list[str]:
    """Resolve a floor token into a list of trading days."""
    if floor == "today":
        return [end] if cal.is_trading_day(end) else []
    if floor == "30d":
        return list(reversed(cal.last_n(end, 30)))
    if floor == "-1y":
        start = _shift_days(end, 365)
    else:
        start = floor
    return cal.between(start, end)


def _done(registry: Registry, dataset: str) -> set[str]:
    """Partitions already written. Only 'ok' counts: a quarantined partition is
    re-fetched, because its rows were deliberately withheld from readers."""
    return {r.partition for r in registry.list_manifest(dataset=dataset, status=("ok",))}


def _response_matches_day(frame, day: str) -> bool:
    """Today-only rows must carry one unambiguous, matching upstream session."""
    if frame is None or frame.empty:
        return True
    if "trade_date" not in frame.columns:
        return False
    dates = frame["trade_date"].astype(str).str.strip().str.replace("-", "", regex=False)
    return bool(frame["trade_date"].notna().all()) and set(dates) == {day}


def _put(store, day: str, written: dict, dataset: str, frame, *,
         dry_run: bool, mode: str = "merge") -> None:
    capture = getattr(frame, "attrs", {}).get("capture") or {}
    incomplete = bool(capture) and not capture.get("complete", False)
    capture_note = (f"; pages={capture.get('pages_fetched')} "
                    f"rows={capture.get('capture_rows')}/{capture.get('upstream_total')} "
                    f"truncated={capture.get('truncated')}" if capture else "")
    if frame is None or frame.empty:
        written[dataset] = 0
        if not dry_run:
            # Record that upstream answered with zero rows. Without this an empty
            # day and a failed day look identical in the manifest - a partition is
            # simply absent - and the coverage report calls a genuinely quiet
            # session a hole, or worse, a hole passes as a quiet session.
            store.registry.mark_ingest("ths_fetch", dataset, day,
                                       "warn" if incomplete else "empty", rows=0)
        return
    written[dataset] = len(frame)
    if dry_run:
        return
    kwargs = {"status": "warn"} if incomplete else {}
    store.write(dataset, day, frame,
                as_of=datetime.now(SHANGHAI).strftime("%Y%m%d"),
                mode=mode, source="ths", trust_tier="T1",
                note=f"{len(frame)} rows{capture_note}", **kwargs)
    store.registry.mark_ingest("ths_fetch", dataset, day,
                               "warn" if incomplete else "ok", rows=len(frame))


class _Fetcher:
    """Fetch one dataset for one day. Caches the dragon-tiger call so the stock
    and seat datasets do not each pay for it, and absorbs the vendor's rate
    limiter.

    A 429 is not a data problem: the page is there, we asked too fast. Treating
    it as a failure means a backfill quietly ends with holes, and a hole reads
    later as "that day had no limit-downs".
    """

    RETRIES = 4

    def __init__(self, args) -> None:
        self.args = args
        self._lhb: dict[str, tuple] = {}

    def _retry(self, fn, *a):
        delay = 1.5
        for attempt in range(self.RETRIES):
            try:
                return fn(*a)
            except ths.ThsError as exc:
                code = str(getattr(exc, "code", ""))
                if code not in {"429", "5002"} or attempt == self.RETRIES - 1:
                    raise
                time.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    def lhb(self, day: str) -> tuple:
        if day not in self._lhb:
            self._lhb[day] = self._retry(ths.fetch_lhb, day, "all")
        return self._lhb[day]

    def seats(self, day: str):
        if day not in self._lhb:
            self._lhb[day] = self._retry(ths.fetch_lhb, day, "all")
        return self._retry(ths.fetch_lhb, day, "hot_money")[1]

    def get(self, dataset: str, day: str):
        if dataset == "ths_limit_up":
            return self._retry(ths.fetch_limit_up, day)
        if dataset == "ths_limit_down":
            return self._retry(ths.fetch_limit_down, day)
        if dataset == "ths_limit_break":
            return self._retry(ths.fetch_limit_break, day)
        if dataset == "ths_lhb":
            return self.lhb(day)[0]
        if dataset == "ths_lhb_seat":
            return self.seats(day)
        if dataset == "ths_ladder":
            ladder = self._retry(ths.fetch_ladder)
            return ladder[ladder["trade_date"].astype(str) == day] if not ladder.empty else ladder
        if dataset == "ths_limit_stat":
            stat = self._retry(ths.fetch_limit_stat, day)
            return ths.stat_frame(stat)
        if dataset == "ths_auction":
            return self._retry(ths.fetch_auction, day, self.args.auction_pages)
        if dataset == "ths_anomaly":
            return self._retry(ths.fetch_anomaly, day)
        raise ValueError(dataset)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dates", default="", help="comma separated YYYYMMDD")
    parser.add_argument("--days", type=int, default=1, help="how many sessions back")
    parser.add_argument("--start", default="", help="inclusive YYYYMMDD")
    parser.add_argument("--end", default="", help="inclusive YYYYMMDD, default today")
    parser.add_argument("--backfill", action="store_true",
                        help="every dataset to its own verified floor")
    parser.add_argument("--datasets", default="", help="comma separated explicit dataset names")
    parser.add_argument("--only", default="", help="comma separated groups: " + ",".join(GROUPS))
    parser.add_argument("--force", action="store_true", help="re-fetch partitions that exist")
    parser.add_argument("--sleep", type=float, default=0.15, help="seconds between days")
    parser.add_argument("--auction-pages", type=int, default=200,
                        help="page cap for the auction list (20 rows per page; overflow is warned, never called complete)")
    parser.add_argument("--freeze-auction", action="store_true",
                        help="capture actual today at 09:26 and preserve an immutable freeze")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not ths.available():
        print("[ths] THS_API_KEY is empty; nothing to do")
        return 2

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    cal = _calendar(store)
    today = _today()
    end = args.end or today

    # ---- which datasets ---------------------------------------------------
    if args.freeze_auction:
        if any((args.dates, args.start, args.end, args.backfill,
                args.datasets, args.only)):
            print("[ths] --freeze-auction accepts only actual today; explicit windows are refused")
            return 2
        wanted = ["ths_auction"]
    elif args.datasets:
        wanted = [d.strip() for d in args.datasets.split(",") if d.strip()]
        unknown = [d for d in wanted if d not in DATASETS]
        if unknown:
            print(f"[ths] unknown dataset(s): {unknown}; known: {list(DATASETS)}")
            return 2
    elif args.only:
        groups = {g.strip() for g in args.only.split(",") if g.strip()}
        wanted = [d for d, (g, _) in DATASETS.items() if g in groups]
    else:
        wanted = list(DATASETS)

    # ---- which days per dataset ------------------------------------------
    plan: dict[str, list[str]] = {}
    for name in wanted:
        floor = DATASETS[name][1]
        if args.freeze_auction:
            days = [today] if cal.is_trading_day(today) else []
        elif args.dates:
            days = [d.strip() for d in args.dates.split(",") if d.strip()]
        elif args.backfill:
            days = _window(cal, floor, end)
        elif args.start:
            days = _window(cal, args.start, end)
        else:
            days = _window(cal, "today", end) if floor == "today" else \
                list(reversed(cal.last_n(end, max(1, args.days))))
        if args.backfill and name in TODAY_ONLY:
            # Not an error: the vendor cannot serve history for these at all.
            print(f"[ths] {name}: today-only upstream, skipped in backfill")
            continue
        if name in TODAY_ONLY and days and (days != [today] or not cal.is_trading_day(today)):
            print(f"[ths] {name}: today-only upstream accepts only actual today ({today})")
            return 2
        if not days:
            print(f"[ths] {name}: no trading days in window, skipped")
            continue
        plan[name] = days

    if not plan:
        print("[ths] nothing to do")
        return 0

    total = sum(len(v) for v in plan.values())
    print(f"[ths] end={end} datasets={len(plan)} day-fetches={total} dry_run={args.dry_run}")
    for name, days in plan.items():
        print(f"[ths]   {name:16s} {len(days):5d} days  {days[0]}..{days[-1]}")

    if args.dry_run:
        # A dry run answers "what would happen", and the two things that matter
        # are how many partitions are missing and where each range starts. It
        # must not touch the network: a 1,400-day preview that fetches is just a
        # slow backfill.
        for name, days in plan.items():
            have = set() if args.force else _done(registry, name)
            missing = [d for d in days if d not in have]
            print(f"[ths]   {name:16s} missing={len(missing):5d} present={len(days) - len(missing):5d}"
                  + (f"  next={missing[0]}" if missing else ""))
        return 0

    fetcher = _Fetcher(args)
    failures = 0
    skipped = 0
    started = time.time()
    done_count = 0

    for name, days in plan.items():
        have = set() if args.force else _done(registry, name)
        todo = [d for d in days if d not in have]
        skipped += len(days) - len(todo)
        if not todo:
            print(f"[ths] {name}: all {len(days)} partitions present")
            continue
        print(f"[ths] {name}: {len(todo)} to fetch ({len(days) - len(todo)} present)")

        for day in todo:
            written: dict[str, int] = {}
            try:
                frame = fetcher.get(name, day)
            except ths.ThsError as exc:
                # A boundary error is expected when a rolling window moves; a
                # single bad day must not abort the rest of the range.
                print(f"[ths] {name} {day} FAILED: {exc}")
                failures += 1
                continue
            except Exception as exc:  # noqa: BLE001 - network flakiness is reported
                print(f"[ths] {name} {day} FAILED: {type(exc).__name__}: {exc}")
                failures += 1
                continue

            if name in TODAY_ONLY and not _response_matches_day(frame, day):
                print(f"[ths] {name} {day} FAILED: response trade_date does not match")
                failures += 1
                continue
            if args.freeze_auction and frame is not None and not frame.empty:
                existing = store.registry.get_manifest("ths_auction_freeze", day)
                if existing is None:
                    frozen = frame.copy()
                    frozen["capture_kind"] = "0926_freeze"
                    _put(store, day, written, "ths_auction_freeze", frozen,
                         dry_run=args.dry_run, mode="replace")
                    print(f"[ths] ths_auction_freeze {day} frozen")
                else:
                    print(f"[ths] ths_auction_freeze {day} preserved; no overwrite")
            _put(store, day, written, name, frame, dry_run=args.dry_run,
                 mode="replace" if name == "ths_auction" else "merge")

            done_count += 1
            if done_count % 50 == 0:
                elapsed = time.time() - started
                rate = done_count / elapsed if elapsed else 0
                left = total - skipped - done_count
                eta = left / rate / 60 if rate else 0
                print(f"[ths]   ... {done_count}/{total - skipped} fetched, "
                      f"{rate:.1f}/s, eta {eta:.1f} min", flush=True)
            if args.sleep:
                time.sleep(args.sleep)

    elapsed = time.time() - started
    print(f"[ths] done in {elapsed / 60:.1f} min; fetched={done_count} "
          f"present={skipped} failed={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
