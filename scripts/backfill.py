"""Resumable backfill CLI.

    python scripts/backfill.py --datasets p1 --start 20240101 --end 20261024
    python scripts/backfill.py --datasets daily,adj_factor --resume
    python scripts/backfill.py --datasets pools --start 20240101 --end 20261024

Guarantees
----------
- Idempotent: re-running a finished unit is a no-op; identical data produces the
  same ``snapshot_id``.
- Crash-safe: progress is flushed every ``--flush-every`` keys and the last
  completed key is stored in ``ingest_ledger.cursor``; a rerun continues there.
- Honest: a dataset the token cannot access is marked ``skipped_permission``,
  never silently empty.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.adapters import catalog
from service.adapters.cninfo_adapter import CninfoAdapter
from service.adapters.eastmoney_adapter import EastMoneyAdapter
from service.adapters.parquet_store import DatasetStore
from service.adapters.pit import TradingCalendarStore
from service.adapters.quality import evaluate, repair
from service.adapters.sqlite_store import Registry
from service.adapters.tushare_adapter import (
    TushareAdapter,
    TusharePermissionError,
)

JOB = "backfill"
DATA_CFG = json.loads((ROOT / "configs" / "data.json").read_text(encoding="utf-8"))

KEY_MODE = {
    "trade_cal": "single",
    "stock_basic": "single",
    "index_member": "single",
    "namechange": "single",
    "fund_basic": "single",
    "suspend_d": "range",
    "daily": "date",
    "adj_factor": "date",
    "daily_basic": "date",
    "stk_limit": "date",
    "moneyflow": "date",
    "margin_detail": "date",
    "moneyflow_hsgt": "date",
    "stk_holdernumber": "date",
    "share_float": "range",
    "top10_floatholders": "range",
    "pledge_stat": "range",
    "stk_holdertrade": "range",
    "dividend": "date",
    "fina_indicator": "code",
    "anns": "code",
    "stk_mins": "code",
    "fund_daily": "code",
    "fund_adj": "code",
    "fund_nav": "code",
    "index_daily": "code",
    "limit_up_pool": "pool",
    "broken_board_pool": "pool",
    "limit_down_pool": "pool",
}

DATE_API = {
    "daily": "fetch_daily",
    "adj_factor": "fetch_adj_factor",
    "daily_basic": "fetch_daily_basic",
    "stk_limit": "fetch_stk_limit",
    "moneyflow": "fetch_moneyflow",
    "margin_detail": "fetch_margin_detail",
    "moneyflow_hsgt": "fetch_moneyflow_hsgt",
    "stk_holdernumber": "fetch_stk_holdernumber",
    "dividend": "fetch_dividend",
}

CODE_API = {
    "fund_daily": "fetch_fund_daily",
    "fund_adj": "fetch_fund_adj",
    "fund_nav": "fetch_fund_nav",
    "fina_indicator": "fetch_fina_indicator",
    "index_daily": "fetch_index_daily",
}


@dataclass
class WorkUnit:
    dataset: str
    partition: str
    keys: list[str]
    start: str
    end: str

    @property
    def label(self) -> str:
        return f"{self.dataset}[{self.partition}]"


def quarter_ends(start: str, end: str) -> list[str]:
    out = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        for md in ("0331", "0630", "0930", "1231"):
            d = f"{year}{md}"
            if start <= d <= end:
                out.append(d)
    return out


def build_units(dataset: str, start: str, end: str,
                calendar: TradingCalendarStore,
                codes: list[str] | None = None) -> list[WorkUnit]:
    spec = catalog.get_spec(dataset)
    mode = KEY_MODE[dataset]
    days = calendar.between(start, end)

    if spec.partition == "all":
        keys: list[str] = []
        if mode == "date":
            keys = days
        elif mode == "code":
            keys = list(codes or [])
        return [WorkUnit(dataset, "all", keys, start, end)]

    if spec.partition == "day":
        if mode == "code":
            # One file per session, keyed by symbol: the fetch is per-symbol, the
            # storage is per-day, because a reader always asks "today's shape".
            return [WorkUnit(dataset, d, list(codes or []), d, d) for d in days]
        return [WorkUnit(dataset, d, [d], d, d) for d in days]

    units: list[WorkUnit] = []
    for year in sorted({d[:4] for d in days}):
        year_days = [d for d in days if d[:4] == year]
        y_start, y_end = year_days[0], year_days[-1]
        if mode == "date":
            keys = year_days
        elif mode == "range":
            keys = [y_end]
        elif mode == "period":
            keys = [p for p in quarter_ends(start, end) if p[:4] == year]
        elif mode == "code":
            keys = DATA_CFG["index_codes"] if dataset == "index_daily" else DATA_CFG["etf_codes"]
        else:
            keys = []
        units.append(WorkUnit(dataset, year, keys, y_start, y_end))
    return units


# --------------------------------------------------------------------- fetch
_CNINFO: CninfoAdapter | None = None


def cninfo() -> CninfoAdapter:
    """One client per process: the throttle is a courtesy to a public service."""
    global _CNINFO
    if _CNINFO is None:
        _CNINFO = CninfoAdapter()
    return _CNINFO


def fetch_unit(unit: WorkUnit, ts: TushareAdapter, em: EastMoneyAdapter) -> pd.DataFrame:
    ds = unit.dataset
    mode = KEY_MODE[ds]

    if ds == "trade_cal":
        return ts.fetch_trade_cal(unit.start, unit.end)
    if ds == "stock_basic":
        return ts.fetch_stock_basic()
    if ds == "index_member":
        return ts.fetch_index_member()
    if ds == "stk_mins":
        from service.adapters.minutes import fetch_minutes as _fetch_minutes
        from service.adapters.tencent_adapter import TencentQuoteAdapter

        frames = []
        tg = TencentQuoteAdapter()
        for code in unit.keys:
            # Two free providers: 东财 is deeper but goes quiet under load, and a
            # desk that reads only one source reads nothing on that day.
            frame, _ = _fetch_minutes(code, em=em, tg=tg)
            if frame is None or frame.empty:
                continue
            frames.append(frame[frame["trade_date"] == unit.partition])
        frames = [f for f in frames if not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if ds == "anns":
        client = cninfo()
        frames = [client.search(code, unit.start, unit.end) for code in unit.keys]
        frames = [f for f in frames if f is not None and not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if ds == "namechange":
        return ts.fetch_namechange()
    if ds == "fund_basic":
        return ts.fetch_fund_basic()
    if ds == "suspend_d":
        return ts.fetch_suspend(unit.start, unit.end)
    if ds == "share_float":
        return ts.fetch_share_float(unit.start, unit.end)
    if ds == "pledge_stat":
        from service.adapters.tushare_adapter import quarter_windows as _qw

        frames = [ts.fetch_pledge_stat(p) for p in
                  [w[0] for w in _qw(unit.start, unit.end)]]
        frames = [f for f in frames if f is not None and not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if ds == "stk_holdertrade":
        from service.adapters.tushare_adapter import quarter_windows as _qw

        frames = [ts.fetch_stk_holdertrade(w[1], w[2]) for w in _qw(unit.start, unit.end)]
        frames = [f for f in frames if f is not None and not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if ds == "top10_floatholders":
        frames = [ts.fetch_top10_floatholders(p) for p in quarter_ends(unit.start, unit.end)]
        frames = [f for f in frames if f is not None and not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    if mode == "date":
        api = DATE_API[ds]
        frames = [getattr(ts, api)(d) for d in unit.keys]
        return pd.concat([f for f in frames if f is not None and not f.empty], ignore_index=True) \
            if frames else pd.DataFrame()

    if mode == "period":
        frames = [ts.fetch_fina_indicator(p) for p in unit.keys]
        return pd.concat([f for f in frames if f is not None and not f.empty], ignore_index=True) \
            if frames else pd.DataFrame()

    if mode == "code":
        api = CODE_API[ds]
        frames = [getattr(ts, api)(code, unit.start, unit.end) for code in unit.keys]
        return pd.concat([f for f in frames if f is not None and not f.empty], ignore_index=True) \
            if frames else pd.DataFrame()

    if mode == "pool":
        frames = [em.fetch_pool(ds, d) for d in unit.keys]
        frames = [f for f in frames if f is not None and not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    raise ValueError(f"no fetcher for {ds}")


# ----------------------------------------------------------------------- run
def _write_rejects(store: DatasetStore, dataset: str, partition: str, rejected: pd.DataFrame) -> None:
    if rejected.empty:
        return
    path = store.root / "rejects" / dataset / f"{partition}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        rejected = pd.concat([pd.read_parquet(path), rejected], ignore_index=True)
        # ``--force`` and a resumed run both re-offer the same rows, and the data
        # path is idempotent while this one was not: every retry of a partition
        # appended another full copy of its rejects, so the directory grew without
        # bound and the same bad row appeared three times in one report.
        rejected = rejected.drop_duplicates(ignore_index=True)
    rejected.to_parquet(path, index=False, compression="zstd")


def run_unit(unit: WorkUnit, store: DatasetStore, registry: Registry,
             ts: TushareAdapter, em: EastMoneyAdapter, *, resume: bool = True,
             force: bool = False, flush_every: int = 20,
             last_session: str | None = None) -> dict:
    spec = catalog.get_spec(unit.dataset)
    state = registry.ingest_state(JOB, unit.dataset, unit.partition)
    existing = store.registry.get_manifest(unit.dataset, unit.partition)

    # "done" only means the keys requested *at that time* were fetched. A later
    # run may ask for a wider range (including backwards), so compare the
    # requested range with the actual coverage instead of trusting the status.
    requested_first = unit.keys[0] if unit.keys else None
    requested_last = unit.keys[-1] if unit.keys else None
    mode = KEY_MODE.get(unit.dataset, "single")
    covered = bool(state and state["status"] == "done" and existing)
    stale_reference = False
    if covered and existing is not None:
        if mode == "date" and requested_first and requested_last:
            covered = (
                bool(existing.cover_start)
                and bool(existing.cover_end)
                and existing.cover_start <= requested_first
                and existing.cover_end >= requested_last
            )
        elif mode in ("code", "range"):
            # Keys are codes / a single range end, not dates: coverage is judged
            # by the last trading day present in the partition.
            covered = bool(existing.cover_end) and existing.cover_end >= unit.end
            if covered and mode == "code":
                # `cover_end` is the newest date in the data, so three symbols
                # with a filing dated today satisfy it — and the ledger then
                # reports the whole partition done. A complete code-mode run
                # leaves its cursor on the last requested key; anything earlier
                # means only a prefix was ever fetched. On 2026-10-28 a three
                # symbol trial made `--datasets anns` skip all 5,919.
                covered = bool(state.get("cursor")) and state["cursor"] == requested_last
        # "single" and "period" tables are small; a done unit is enough —
        # except that "enough" had no expiry. Being reference tables they have no
        # date boundary to compare against, so a finished run was final: on
        # 2026-10-31 `stock_basic` and `fund_basic` were still the 2026-10-25
        # pull, which means a name listed, renamed to ST or added to an index
        # after that date was invisible to screen and universe while every bar
        # around it was current. Freshness for these can only be judged by when
        # we fetched them, so the check uses `ingested_at` against the last
        # session in the requested window. `last_session` is passed by the CLI;
        # a unit test that calls this directly keeps the old "done is done".
        if mode == "single" and last_session and existing.ingested_at:
            fetched = str(existing.ingested_at)[:10].replace("-", "")
            covered = fetched >= last_session
            # A reference table has no keys, so the empty-key branch further down
            # would answer "skipped" no matter what `covered` says. That branch
            # is what made these files permanent in the first place; this flag is
            # the one thing allowed to step past it.
            stale_reference = not covered

    if not force and covered and existing is not None and existing.status != "quarantine":
        return {"unit": unit.label, "status": "skipped", "rows": existing.rows}

    # Only a crashed run has a trustworthy contiguous prefix. A finished run
    # whose range is being extended must refetch; the merge is idempotent.
    # A failed unit is a resumable unit. `failed` is written by the CLI's handler
    # after any exception — a file lock, a dropped socket — and while only
    # "running" counted as resumable, the next invocation re-fetched from key 1.
    # On 2026-10-28 a Windows `PermissionError` from a reader holding
    # `fina_indicator/all.parquet` threw away 2,500 completed calls and started
    # over. The cursor is a fact about what has been fetched, not about why the
    # last attempt stopped.
    cursor = None
    if not force and resume and state and state["status"] in ("running", "failed"):
        cursor = state.get("cursor")
    keys = [k for k in unit.keys if cursor is None or k > cursor]
    total = 0 if existing is None else existing.rows
    t0 = time.monotonic()
    written = 0

    if not keys and existing is not None and not force and not stale_reference:
        registry.mark_ingest(JOB, unit.dataset, unit.partition, "done",
                             cursor=cursor, rows=existing.rows)
        return {"unit": unit.label, "status": "skipped", "rows": existing.rows}

    # A unit with no keys (reference tables) is fetched in one shot.
    batches = [keys[i : i + flush_every] for i in range(0, len(keys), flush_every)] or [keys]
    for bi, batch in enumerate(batches, start=1):
        sub = WorkUnit(unit.dataset, unit.partition, batch, unit.start, unit.end)
        raw = fetch_unit(sub, ts, em)
        if raw is None or raw.empty:
            if batch:
                registry.mark_ingest(JOB, unit.dataset, unit.partition, "running",
                                     cursor=batch[-1], rows=total)
            continue
        clean, rejected = repair(raw, spec)
        _write_rejects(store, unit.dataset, unit.partition, rejected)
        rec = store.write(
            unit.dataset, unit.partition, clean,
            as_of=batch[-1] if batch else unit.end,
            mode="merge" if existing is not None or written > 0 else "replace",
            note=f"backfill {unit.start}-{unit.end}",
        )
        written += 1
        if rec:
            total = rec.rows
        if batch:
            registry.mark_ingest(JOB, unit.dataset, unit.partition, "running",
                                 cursor=batch[-1], rows=total)
        elapsed = time.monotonic() - t0
        print(f"  {unit.label} {bi}/{len(batches)} "
              f"({batch[-1] if batch else 'ref'}) rows={total} {elapsed:.0f}s", flush=True)

    final = store.registry.get_manifest(unit.dataset, unit.partition)
    if final is None:
        registry.mark_ingest(JOB, unit.dataset, unit.partition, "empty", rows=0)
        return {"unit": unit.label, "status": "empty", "rows": 0}

    frame = store.read_partition(unit.dataset, unit.partition)
    q = evaluate(unit.dataset, unit.partition, frame,
                 calendar_days=None)  # full gap check runs in verify_data
    registry.record_quality(unit.dataset, unit.partition, q.status, q.rows,
                            [i.as_dict() for i in q.issues])
    if q.status == "quarantine":
        store.registry.set_manifest_status(unit.dataset, unit.partition, "quarantine",
                                           note="auto: " + ",".join(i.code for i in q.issues))
    elif q.status == "warn" and final.status == "ok":
        store.registry.set_manifest_status(unit.dataset, unit.partition, "warn",
                                           note=",".join(i.code for i in q.issues))
    elif q.status == "ok" and final.status in ("warn", "quarantine"):
        # A partition that was incomplete during an earlier partial ingest keeps
        # its warning forever otherwise. daily[2023] still said
        # "missing_trading_days" long after it had all 242 sessions, and a stale
        # warning is worse than none: it teaches the reader to ignore the column.
        store.registry.set_manifest_status(unit.dataset, unit.partition, "ok", note="")
    registry.mark_ingest(JOB, unit.dataset, unit.partition, "done",
                         cursor=unit.keys[-1] if unit.keys else unit.end, rows=q.rows)
    return {"unit": unit.label, "status": q.status, "rows": q.rows}


def ensure_calendar(store: DatasetStore, registry: Registry, ts: TushareAdapter,
                    start: str, end: str) -> TradingCalendarStore:
    cal = store.read("trade_cal", start, end)
    if cal.empty or str(cal["cal_date"].min()) > start or str(cal["cal_date"].max()) < end:
        print("[setup] fetching trade_cal", flush=True)
        df = ts.fetch_trade_cal(start, end)
        store.write("trade_cal", "all", df, as_of=end, mode="merge")
        cal = store.read("trade_cal", start, end)
    return TradingCalendarStore.from_frame(cal, start, end)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="resumable data backfill")
    parser.add_argument("--datasets", default="p1",
                        help="p1|full|pools|all|comma separated names")
    parser.add_argument("--start", default="20240101")
    parser.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument("--force", action="store_true", help="refetch even if done")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--symbols", default=None,
                        help="comma separated codes; narrows a code-mode run "
                             "(the daily job refreshes only what it holds and saw)")
    parser.add_argument("--flush-every", type=int, default=int(DATA_CFG["flush_every"]))
    parser.add_argument("--rate", type=int, default=180, help="Tushare calls per minute")
    args = parser.parse_args(argv)

    try:
        start = datetime.strptime(args.start, "%Y%m%d").strftime("%Y%m%d")
        end = datetime.strptime(args.end, "%Y%m%d").strftime("%Y%m%d")
        if start != args.start or end != args.end or start > end:
            raise ValueError
    except ValueError:
        print("error: --start/--end must be YYYYMMDD with start <= end", file=sys.stderr)
        return 2
    if args.flush_every <= 0 or args.rate <= 0:
        print("error: --flush-every and --rate must be positive", file=sys.stderr)
        return 2

    try:
        datasets = catalog.resolve(args.datasets.split(","))
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    ts = TushareAdapter(token, per_minute=args.rate)
    em = EastMoneyAdapter()

    calendar = ensure_calendar(store, registry, ts, args.start, args.end)
    if not calendar.days:
        print("error: empty trading calendar", file=sys.stderr)
        return 2

    print(f"[plan] {args.start}..{args.end}  trading_days={len(calendar.days)}  datasets={datasets}")
    results: list[dict] = []
    failures = 0
    # The window's own last session, not the wall clock: a run on a Saturday
    # asking for 20261029..20261031 should still count Friday's reference pull
    # as fresh, and this is the value that makes that true.
    last_session = calendar.days[-1]

    for dataset in datasets:
        if dataset not in KEY_MODE:
            print(f"[skip] {dataset}: no fetcher (on-demand dataset)")
            continue
        codes = None
        # A code-mode run needs its universe whether it lands in one `all`
        # partition or in one file per session: `stk_mins` stores by day but
        # fetches by symbol, and the first version of this only built the list
        # for `all`, which left the minute job with zero keys and a silent skip.
        if KEY_MODE.get(dataset) == "code":
            basic = store.read("stock_basic")
            codes = sorted(basic["ts_code"].astype(str).unique()) if not basic.empty else []
            if args.symbols:
                wanted = {c.strip().upper() for c in args.symbols.split(",") if c.strip()}
                codes = [c for c in codes if c.upper() in wanted]
                if not codes:
                    print(f"[plan] --symbols matched none of {len(wanted)} codes", flush=True)
            elif catalog.get_spec(dataset).partition != "all":
                # Nothing sane to default to: a per-day minute table times the
                # whole market is 5,577 calls per session.
                print(f"[plan] {dataset} needs --symbols (or it would fetch the "
                      f"whole market every session)", flush=True)
                codes = []
        units = build_units(dataset, args.start, args.end, calendar, codes=codes)
        if args.dry_run:
            for u in units:
                print(f"  {u.label} keys={len(u.keys)}")
            continue
        print(f"[{dataset}] {len(units)} unit(s)")
        for unit in units:
            try:
                res = run_unit(unit, store, registry, ts, em,
                               resume=args.resume, force=args.force,
                               flush_every=args.flush_every,
                               last_session=last_session)
                results.append(res)
                if res.get("status") == "quarantine":
                    failures += 1
            except TusharePermissionError as exc:
                failures += 1
                print(f"  [permission] {unit.label}: {exc}")
                registry.mark_ingest(JOB, dataset, unit.partition, "skipped_permission",
                                     error=str(exc)[:500])
            except Exception as exc:  # noqa: BLE001 - CLI boundary
                failures += 1
                print(f"  [error] {unit.label}: {type(exc).__name__}: {exc}", file=sys.stderr)
                registry.mark_ingest(JOB, dataset, unit.partition, "failed", error=str(exc)[:500])

    if not args.dry_run:
        summary = {
            "ran_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "range": [args.start, args.end],
            "datasets": datasets,
            "results": results,
            "failures": failures,
            "tushare_calls": ts.calls,
            "eastmoney_calls": em.calls,
        }
        out = ROOT / "data" / "backfill_last_run.json"
        # Written via a temp file: `/data/status` reads this directly, and a
        # truncate-then-write left it parsing an empty file whenever the two
        # overlapped.
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(out)
        print(f"[done] units={len(results)} failures={failures} "
              f"tushare_calls={ts.calls} eastmoney_calls={em.calls}")
        print(f"[done] wrote {out}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
