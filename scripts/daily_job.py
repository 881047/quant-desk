"""Unattended daily pipeline (P1 acceptance: run 30 days without a human).

    python scripts/daily_job.py                 # 17:30 after close
    python scripts/daily_job.py --days 10       # after a holiday break
    python scripts/daily_job.py --snapshot      # force an intraday snapshot
    python scripts/daily_job.py --pass late     # 21:40, late-published data
    python scripts/daily_job.py --pass morning  # 08:50, T+1 data

Three passes, because the vendor publishes in three waves
--------------------------------------------------------
`close` (17:30) is the whole pipeline described in Steps below.
`late` and `morning` are four lines of backfill each: they exist only because
`moneyflow` (19:00), `moneyflow_hsgt` (18:00–20:00) and `margin_detail` (T+1
08:30) cannot exist at 17:30. Asking for them in the close pass did not fail —
it returned an empty frame, the ledger recorded "done", and the block sat one
session behind with no error anywhere. See ``GAP_PASSES``.

Steps (close pass)
------------------
1. incremental backfill of the last N calendar days (idempotent, resumable):
   p1 = bars/factors/limits, panel = 东财三池 + 同花顺盘面
2. quality verification over the same window
3. intraday snapshot if we are inside a trading session
4. one JSON summary per day under ``reports/daily/``

Exit code is non-zero if backfill or verification failed, so Task Scheduler can
flag the run. The only thing here that touches the paper account is one NAV row
for the core sleeve — a measurement, never an order.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import backfill, verify_data
from service.adapters.brief import build_brief
from service.adapters.eastmoney_adapter import EastMoneyAdapter
from service.adapters.jsonio import write_json_atomic
from service.adapters.parquet_store import DatasetStore
from service.adapters.pipeline_state import PipelineBusy, hold, write_marker
from service.adapters.pit import TradingCalendarStore
from service.adapters.sqlite_store import Registry
from service.kernel.clock import SHANGHAI

BACKUP_KEEP = 14
DERIVE_POOL_WARMUP_DAYS = 45

#: Exit code for "another run already holds the lock". Distinct from 1 (the gate
#: is not ready) and 2 (bad arguments), because the reader's next move is
#: different: wait for the run in flight, do not re-run and do not repair.
EXIT_BUSY = 3

# Tushare does not publish everything at 17:00, and a run that asks too early
# does not fail — it comes back with nothing and the ledger still says "done".
# That is how `moneyflow` (19:00) and `moneyflow_hsgt` (18:00–20:00) sat one
# session behind every close pass, and why `margin_detail` could never be
# current: the exchanges publish it next morning around 08:30, so a 17:30 run on
# day T is asking for a file that does not exist yet. Two extra passes, each
# reading only what is actually published by then:
#
#   late    21:40  moneyflow, moneyflow_hsgt
#   morning 08:50  margin_detail (T+1) + a retry of the two above, so one failed
#                  evening does not leave a permanent hole — nothing else in the
#                  close pass asks for them again.
#
# `morning` is deliberately not free of the late pair: a gap that only one
# unscheduled window can close is a gap that comes back.
GAP_PASSES: dict[str, tuple[str, ...]] = {
    "late": ("moneyflow", "moneyflow_hsgt"),
    "morning": ("margin_detail", "moneyflow", "moneyflow_hsgt"),
}

# What the close pass refreshes. `moneyflow`, `moneyflow_hsgt` and
# `margin_detail` used to be in this list; they were three API calls per run that
# could not return today's rows at 17:30, so they now belong to the passes above.
CLOSE_DATASETS = "p1,panel,dividend"


def data_root() -> Path:
    """Where the API will look for this run's lock and completion marker.

    Not always ``ROOT/data``: ``service/api/data.py`` resolves its data directory
    through ``QUANT_DATA_ROOT``, and an artifact written in one place but looked
    for in another is worse than no artifact — it reports "never ran" for a run
    that finished. The dataset paths below still use ``ROOT/data``; that split
    pre-dates this function and is not widened here. What matters for the lock is
    that both ends agree, and this is the end the API reads.
    """
    return Path(os.getenv("QUANT_DATA_ROOT") or (ROOT / "data"))


def backup_databases(root: Path, day: str) -> list[str]:
    """Consistent copies of the two SQLite files that hold the whole record.

    Uses the SQLite backup API rather than a file copy so a WAL write in flight
    cannot produce a half-written snapshot. Without this the 20-trade sample and
    every audit row live on a single 152 KB file with no history.
    """
    target = root / "data" / "backups" / day
    target.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for name in ("paper.sqlite", "registry.sqlite"):
        src = root / "data" / name
        if not src.exists():
            continue
        dst = target / name
        with sqlite3.connect(src) as s_conn, sqlite3.connect(dst) as d_conn:
            s_conn.backup(d_conn)
        saved.append(str(dst))

    backups = sorted(p for p in (root / "data" / "backups").iterdir() if p.is_dir())
    for old in backups[:-BACKUP_KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    return saved


def in_session(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 5


def _days_before(day: str, n: int) -> str:
    """Calendar subtraction on a YYYYMMDD string, done on a Timestamp."""
    from datetime import datetime as _dt
    from datetime import timedelta

    return (_dt.strptime(day, "%Y%m%d") - timedelta(days=n)).strftime("%Y%m%d")


def _held_codes(root: Path) -> set[str]:
    """Symbols in the paper book. An unreadable book holds nothing, by definition."""
    try:
        from service.trading.state import PaperStore as _PaperStore

        state = _PaperStore(root / "data" / "paper.sqlite").load_state() or {}
        positions = (state.get("account") or {}).get("positions") or {}
        return {str(sym) for sym in positions}
    except Exception:  # noqa: BLE001
        return set()


def _pool_codes(store, dataset: str, day: str) -> set[str]:
    """Bare six-digit codes in one pool partition; empty when it was not fetched."""
    try:
        frame = store.read_partition(dataset, day)
    except Exception:  # noqa: BLE001
        return set()
    if frame is None or frame.empty or "code" not in frame.columns:
        return set()
    return {str(c) for c in frame["code"].astype(str)}


def _turnover_leaders(store, day: str, limit: int) -> list[str]:
    """Today's biggest turnover names, as ts_codes.

    The minute table used to be filled only for holdings and today's pools — the
    desk's own book. But the question that gets asked is about a *candidate* the
    screen just surfaced, which is almost always a liquid name near the top of
    turnover. This is the cheapest set that covers "what the market is actually
    trading today"; a long tail of 4,000 untraded names is 40 minutes of requests
    for bars nobody will read.
    """
    try:
        # `daily` is stored by year, not by day: read_partition(day) returns
        # nothing at all. The two-sided read pushes the date filter down.
        frame = store.read("daily", day, day)
    except Exception:  # noqa: BLE001 - no bars yet is not an error here
        return []
    if frame is None or frame.empty or "amount" not in frame.columns:
        return []
    frame = frame.copy()
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    frame = frame.dropna(subset=["amount"])
    frame = frame[~frame["ts_code"].astype(str).str.endswith(".BJ")]
    return [str(c) for c in frame.nlargest(limit, "amount")["ts_code"].astype(str)]


def _minute_universe(root: Path, day: str, store, top: int = 400) -> list[str]:
    """What the 5-minute table is refreshed for: held + pools + turnover leaders.

    Separate from the announcement universe on purpose. Announcements are fetched
    30 days deep per symbol, so widening that set costs 30× per name; minute bars
    cost one session per name. Same reasoning, different price, so different sets.
    """
    base = set(_announcement_universe(root, day, store))
    return sorted(base | set(_turnover_leaders(store, day, top)))


def _announcement_universe(root: Path, day: str, store) -> list[str]:
    """Held names plus today's limit-up and broken-board names, with suffixes.

    The pools key on a bare six-digit code while everything else uses a ts_code,
    which has already caused one silent mismatch in this repo; the mapping goes
    through `stock_basic` rather than guessing from the prefix.
    """
    codes = _held_codes(root)
    bare = _pool_codes(store, "limit_up_pool", day) | _pool_codes(
        store, "broken_board_pool", day)
    if bare:
        basic = store.read("stock_basic")
        if not basic.empty:
            table = dict(zip(basic["symbol"].astype(str), basic["ts_code"].astype(str)))
            codes |= {table[c] for c in bare if c in table}
    return sorted(codes)


#: Steps whose failure changes what the desk may conclude about a session.
#: `derived_pools` is the only per-step entry: it is the sole source of limit-up
#: counts, streaks and promotion rates, and a silent fallback to the vendor
#: undercounts by ~1.6 names a day (see the `_gate` docstring). Everything else
#: is visible but supplementary — `intraday_features` is 0.094 on the scorecard
#: and on 2026-11-12 it turned the whole desk red while the close facts were
#: READY with `in_cap=26`. Backfill/verify cover `daily`/`stk_limit` directly.
CRITICAL_STEPS = frozenset({"derived_pools"})


def _gate(rc_backfill: int, rc_verify: int,
          step_rcs: dict[str, int]) -> tuple[str, list[str], list[str]]:
    """日更的放行判定：只有判定必需的步骤失败，这一天才算 ``not_ready``。

    ``derived_pools`` 在这张表里是刻意的：派生池是涨停家数、连板、晋级率的唯一来源，
    它没重建成功时数据不会报错——只会安静地退回厂商口径（同花顺日均漏 1.6 只），
    或者把连板算成全场首板。把它算进 gate，才会有人在那天看到红。

    其余步骤（``intraday_features``、``text``、``cb_daily``、``ths``……）失败时
    写进 ``supplementary_failed``：它们不该被藏起来，但也不该把整台桌子判红——
    一个 0.094 分的可选维度否决一份 READY 的收盘事实，是另一种假权威。
    """
    failed = sorted(name for name, rc in step_rcs.items() if rc)
    critical = [name for name in failed if name in CRITICAL_STEPS]
    supplementary = [name for name in failed if name not in CRITICAL_STEPS]
    gate = "not_ready" if (rc_backfill or rc_verify or critical) else "ready"
    return gate, critical, supplementary


def write_alert(day: str, window: tuple[str, str], gate: str, *,
                rc_backfill: int, rc_verify: int, failed_steps: list[str],
                root: Path | None = None) -> Path | None:
    """Leave the pipeline verdict where a session will actually see it.

    The non-zero exit code is for Task Scheduler; nobody inside a session reads
    it. On 2026-10-29 the job failed at 18:39 and the desk went on to write a
    close report off a snapshot. `context_pack` surfaces this file as
    `pipeline_alert`, so the first call of the next session states the failure
    instead of leaving it to be inferred from missing rows.
    """
    base = root or ROOT
    alert = base / "data" / "ALERT.txt"
    if gate != "not_ready":
        alert.unlink(missing_ok=True)
        return None
    reasons = []
    if rc_backfill:
        reasons.append(f"backfill rc={rc_backfill}")
    if rc_verify:
        reasons.append(f"verify rc={rc_verify}")
    if failed_steps:
        reasons.append("failed steps: " + ", ".join(failed_steps))
    alert.parent.mkdir(parents=True, exist_ok=True)
    alert.write_text(
        f"{day} daily_job gate=not_ready\n"
        + "\n".join(f"- {r}" for r in reasons)
        + f"\n窗口 {window[0]}..{window[1]}。在此之前写下的判定不可复现，"
          f"先跑 GET /readiness 看缺什么。\n",
        encoding="utf-8")
    return alert


def resolve_alert_step(step: str, day: str | None = None,
                       root: Path | None = None) -> bool:
    """Remove one step from ``ALERT.txt`` after its partition was rebuilt.

    ``write_alert`` clears the file only on a clean ``daily_job`` run. A manual
    rebuild (``scripts/derive_intraday.py --day ...``) never runs that job, so
    on 2026-11-12 the 18:35 partition coexisted with a 17:56 ``not_ready`` and
    the next session read the alert as current. The script that did the rebuild
    calls this: when no reason is left the alert is deleted, otherwise it is
    rewritten with the remaining reasons (a ``backfill rc`` cannot be resolved
    by a step rebuild and stays).
    """
    base = root or ROOT
    alert = base / "data" / "ALERT.txt"
    if not alert.exists():
        return False
    try:
        lines = alert.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if not lines or "gate=not_ready" not in lines[0]:
        return False
    if day and lines[0].split()[0] != str(day):
        return False
    marker = "- failed steps: "
    idx = next((i for i, line in enumerate(lines) if line.startswith(marker)), None)
    if idx is None:
        return False
    steps = [s.strip() for s in lines[idx][len(marker):].split(",") if s.strip()]
    if step not in steps:
        return False
    steps.remove(step)
    if steps:
        lines[idx] = marker + ", ".join(steps)
    else:
        del lines[idx]
    if not any(line.startswith("- ") for line in lines):
        alert.unlink(missing_ok=True)
        return True
    alert.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def run_gap_pass(pass_name: str, start: str, end: str, root: Path | None = None) -> int:
    """One of the off-hours passes: fetch what is published now, nothing else.

    No verify, no pools, no announcements, no NAV row, no dashboard. Those steps
    describe a finished session and a session is already finished by the time
    these run. Keeping them out means a late failure cannot overwrite the close
    pass's verdict with a second, thinner one.
    """
    base = root or ROOT
    datasets = GAP_PASSES[pass_name]
    t0 = datetime.now(SHANGHAI)
    print(f"[daily_job:{pass_name}] start {t0.isoformat(timespec='seconds')} "
          f"window={start}..{end} datasets={','.join(datasets)}", flush=True)
    rc = 1
    error = None
    try:
        rc = backfill.main(["--datasets", ",".join(datasets),
                            "--start", start, "--end", end, "--resume"])
    except Exception as exc:  # noqa: BLE001 - the pass must report, not raise
        error = f"{type(exc).__name__}: {exc}"
        print(f"[daily_job:{pass_name}] crashed: {error}", flush=True)

    out = base / "reports" / "daily" / f"{end}_{pass_name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "as_of": t0.isoformat(timespec="seconds"),
        "pass": pass_name,
        "window": [start, end],
        "datasets": list(datasets),
        "backfill_rc": rc,
        "ok": rc == 0 and error is None,
        "error": error,
    }
    try:
        write_json_atomic(out, payload)
    except OSError as exc:  # pragma: no cover - disk full / permissions
        print(f"[daily_job:{pass_name}] could not write {out}: {exc}", flush=True)
    # 显式传了 root（测试）就写那个根；生产上没有 root，必须和 API 读的同一个地方。
    try:
        write_marker(base / "data" if root else data_root(), end, pass_name, {
            "gate": "ready" if payload["ok"] else "not_ready",
            "failed_steps": [] if payload["ok"] else [f"backfill:{pass_name}"],
            "datasets": list(datasets),
            "backfill_rc": rc,
            "error": error,
            "report": f"reports/daily/{end}_{pass_name}.json",
        })
    except OSError as exc:  # pragma: no cover - disk full / permissions
        print(f"[daily_job:{pass_name}] marker NOT written: {exc}", flush=True)
    print(f"[daily_job:{pass_name}] rc={rc} -> {out}")
    return 0 if payload["ok"] else 1


def _settle_short_alpha_oos(end: str, is_open: bool, *, root: Path | None = None) -> dict:
    """Settle frozen WorkBuddy judgments only; never create a model judgment."""
    base = root or ROOT
    if not is_open:
        return {"settled": False, "reason": "not a trading day",
                "judgment_capture": "external_workbuddy_only"}
    try:
        from service.trading import build_desk, short_alpha_tracking

        alpha_desk = build_desk(root=base)
        alpha_settled = short_alpha_tracking.settle(alpha_desk, root=base)
        alpha_report = short_alpha_tracking.report(
            alpha_desk, root=base, scope="all", limit=30)
        alpha_path = base / "reports" / "daily" / f"{end}_short_alpha_oos.json"
        write_json_atomic(alpha_path, {
            "ok": True, "settlement": alpha_settled, "tracking": alpha_report,
            "judgment_capture": "external_workbuddy_only",
            "order_created": False, "paper_ledger_modified": False,
        })
        return {
            "settled": True, "records": alpha_settled["checked"],
            "changed": alpha_settled["changed"], "report": str(alpha_path),
            "judgment_capture": "external_workbuddy_only",
        }
    except Exception as exc:  # noqa: BLE001 - report, never interrupt the job
        return {"settled": False, "reason": f"{type(exc).__name__}: {exc}",
                "judgment_capture": "external_workbuddy_only"}


def main(argv: list[str] | None = None) -> int:
    # PowerShell's `*>>` wraps every child stderr line into an ErrorRecord, and
    # Task Scheduler then records a successful run as failed: on 2026-10-29 the
    # gate was `ready` while LastTaskResult stayed 1, purely because pandas wrote
    # a FutureWarning to stderr. The log already merges both streams (`*>>`), so
    # routing stderr into stdout loses nothing and makes the exit code mean what
    # it says. A real crash still exits non-zero — the traceback just lands in
    # the log instead of being swallowed by the wrapper.
    sys.stderr = sys.stdout
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="daily unattended data job")
    parser.add_argument("--days", type=int, default=3, help="calendar days to refresh")
    parser.add_argument("--pass", dest="pass_name", default="close",
                        choices=("close", "late", "morning"),
                        help="close = the 17:30 job; late / morning = the two "
                             "off-hours passes that fetch datasets Tushare "
                             "publishes after the close (see GAP_PASSES)")
    parser.add_argument("--datasets", default=CLOSE_DATASETS,
                        help="p1 | panel | all | comma separated names. "
                             "dividend is in the default set: the ledger applies "
                             "dividends on the ex-date. moneyflow / moneyflow_hsgt "
                             "/ margin_detail are NOT here — they are published at "
                             "19:00 / 18-20 点 / T+1 08:30 and belong to --pass late "
                             "and --pass morning")
    parser.add_argument("--snapshot", action="store_true", help="force a snapshot now")
    parser.add_argument("--skip-verify", action="store_true")
    args = parser.parse_args(argv)
    if args.days <= 0:
        print("[daily_job] --days must be positive", file=sys.stderr)
        return 2

    now = datetime.now(SHANGHAI)
    end = now.strftime("%Y%m%d")
    start = (now - timedelta(days=args.days)).strftime("%Y%m%d")

    # Single writer, or nothing. The documented fallback for a late session is
    # "再跑一次 daily_job --days 3", so two runs *will* overlap one day — and two
    # runs merging the same partition lose one writer's rows, because the
    # manifest guard orders them by `ingested_at` instead of serialising them.
    # See service/adapters/pipeline_state.py.
    try:
        with hold(data_root(), args.pass_name, now=now):
            return _run(args, now, end, start)
    except PipelineBusy as exc:
        print(f"[daily_job] refused: {exc}", flush=True)
        print("[daily_job] 先看 GET /data/brief 的 pipeline.state；"
              "正在跑的那次结束前不要补跑，也不要动同一批分区。", flush=True)
        return EXIT_BUSY


def _run(args: argparse.Namespace, now: datetime, end: str, start: str) -> int:
    if args.pass_name != "close":
        return run_gap_pass(args.pass_name, start, end)

    # A run that dies halfway used to leave no trace at all: the closing line is
    # printed only after every step finishes, so "crashed at 17:35" and "never
    # started" looked identical in the log. This marker makes the difference
    # visible, and pairs with the closing line to bound each attempt.
    print(f"[daily_job] start {now.isoformat(timespec='seconds')} "
          f"window={start}..{end} days={args.days}", flush=True)

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)

    # Back up before touching anything: a failed backfill must not cost history.
    backups = backup_databases(ROOT, end)

    # Wrapped like every other step below. These two ran bare, so a network blip
    # or one unreadable partition raised out of `main()` and the process exited
    # before writing `data/ALERT.txt` and `reports/daily/<end>.json` — which made
    # "the job never ran" and "the job died halfway" look identical again, the
    # exact ambiguity the alert files exist to remove.
    try:
        rc_backfill = backfill.main(
            ["--datasets", args.datasets, "--start", start, "--end", end, "--resume"]
        )
    except Exception as exc:  # noqa: BLE001 - the job must finish and report
        rc_backfill = 1
        print(f"[daily_job] backfill crashed: {type(exc).__name__}: {exc}", flush=True)
    rc_verify = 0
    if not args.skip_verify:
        try:
            rc_verify = verify_data.main([
                "--start", start, "--end", end,
                "--out", str(ROOT / "reports" / "daily" / f"{end}_quality.json"),
            ])
        except Exception as exc:  # noqa: BLE001
            rc_verify = 1
            print(f"[daily_job] verify crashed: {type(exc).__name__}: {exc}", flush=True)

    snap = None
    snapshot_rc = 0
    cal = TradingCalendarStore.from_frame(store.read("trade_cal"))
    is_open = cal.is_trading_day(end)

    # 同花顺 panels: a different vendor with its own history floors and its own
    # script (the ladder ignores its date parameter, 龙虎榜 only reaches back a
    # year). Kept out of the backfiller on purpose; run here so the tape and the
    # bars describe the same session. A failure is reported, not fatal.
    ths: dict = {"rc": 0, "note": "not a trading day"}
    if is_open:
        try:
            from scripts import fetch_ths_special

            rc_ths = fetch_ths_special.main(["--days", "1"])
            ths = {"rc": rc_ths, "note": "ok" if rc_ths == 0 else "fetch failed"}
        except Exception as exc:  # noqa: BLE001 - the tape must not fail the job
            ths = {"rc": 1, "note": f"{type(exc).__name__}: {exc}"}

    # Derived pool history (bars + limit prices → the three pools). The research
    # layer's backtest universe reads this table, and it was never scheduled: on
    # 2026-10-26 it was still on the 23rd while the pools it derives from were
    # current. Four seconds per session, so it belongs here rather than in a
    # person's memory.
    derived: dict = {"rc": 0, "note": "not a trading day"}
    if is_open:
        try:
            from scripts import derive_pools

            rc_derive = derive_pools.main([
                "--start", end, "--end", end,
                "--warmup-days", str(DERIVE_POOL_WARMUP_DAYS),
            ])
            derived = {"rc": rc_derive, "note": "ok" if rc_derive == 0 else "derive failed"}
        except Exception as exc:  # noqa: BLE001 - derived data must not fail the job
            derived = {"rc": 1, "note": f"{type(exc).__name__}: {exc}"}

    # The sentiment panel is a daily-frequency fact — pool counts, highest
    # streak, sealed amount — so it can and must be computed here. It used to be
    # produced only by the in-session snapshot, which meant that from 15:00
    # onward "no sentiment panel for today" was the answer the hot-money sleeve
    # got while deciding what to do tomorrow.
    #
    # It runs **after** ``derive_pools`` on purpose: ``close_panel`` prefers the
    # derived breadth (close >= up_limit, checkable name by name) and falls back
    # to the vendor pools only when today's derived partition is absent. Built
    # before that step, every close-of-day file stayed on the vendor basis while
    # ``facts`` and ``limit_structure`` used the derived one — 2026-11-13
    # published 0.3621 beside 0.387 for the same session.
    sentiment: dict | None = None
    if is_open:
        try:
            from service.trading.sentiment import close_panel

            sentiment = close_panel(store, end)
            folder = ROOT / "data" / "sentiment"
            folder.mkdir(parents=True, exist_ok=True)
            # 文件里的 `quality` 是**实时报价**的判定，跟上面的池家数无关（家数优先取
            # 派生池）。但它得有个真值：17:30 之后不再有快照，不写就等于让读者拿
            # 上午那一帧的结论当收盘结论。这里判合并后的分区——这个钟点最好的答案。
            # 判不出来就写 None：没评过就说没评过，不写一个没做过的 "ok"。
            quotes_quality: str | None = None
            try:
                from service.adapters.quality import evaluate as evaluate_quality

                quotes_quality = evaluate_quality(
                    "rt_quote", end, store.read("rt_quote", end, end)).status
            except Exception:  # noqa: BLE001 - a label must not fail the job
                quotes_quality = None
            write_json_atomic(folder / f"{end}.json", {
                "as_of": now.isoformat(timespec="seconds"),
                "trade_date": end,
                "pools": {"limit_up_pool": sentiment["limit_up"],
                          "broken_board_pool": sentiment["broken_board"],
                          "limit_down_pool": sentiment["limit_down"]},
                "sentiment": sentiment,
                "quality": quotes_quality,
            })
        except Exception as exc:  # noqa: BLE001 - a label must not fail the job
            sentiment = None
            ths = {**ths, "sentiment_error": f"{type(exc).__name__}: {exc}"}

    # 逐帧日内特征。旧 rt_quote 分区没有 frame_id 时 derive_intraday 会显式报
    # legacy_partition_no_frame_id 并返回 0——那是「这一天不可回补」的历史边界，
    # 不是日更失败，不能把它当成坏晚上去染红 gate。
    features: dict = {"rc": 0, "note": "not a trading day"}
    if is_open:
        try:
            from scripts import derive_intraday

            rc_features = derive_intraday.main(["--day", end])
            features = {"rc": rc_features,
                        "note": "ok" if rc_features == 0 else "derive failed"}
        except Exception as exc:  # noqa: BLE001 - 派生数据不能拖垮日更主链路
            features = {"rc": 1, "note": f"{type(exc).__name__}: {exc}"}

    # 5-minute bars for the whole market, once a day. baostock allows 50,000
    # requests per day and no concurrent connections, so the shape is fixed:
    # one range query per symbol, which is 5,571 requests for one session and
    # the same 5,571 for twenty — history costs time, not requests, and that is
    # what `--days N` is for (a person decides, not this job).
    #
    # It stays out of `step_rcs`: minute bars are research fuel. A bad evening at
    # the source must not hold tomorrow's daily bars.
    minutes_sweep: dict = {"rc": 0, "note": "not a trading day"}
    if is_open:
        try:
            from scripts import intraday_sweep

            rc_sweep = intraday_sweep.main(["--day", end, "--days", "1"])
            minutes_sweep = {
                "rc": rc_sweep,
                "note": {0: "ok", 3: "另一个 sweep 在跑，跳过"}.get(rc_sweep, f"rc={rc_sweep}"),
            }
        except Exception as exc:  # noqa: BLE001 - 分钟线不是日线的前置
            minutes_sweep = {"rc": 1, "note": f"{type(exc).__name__}: {exc}"}

    # Two datasets the backfiller cannot serve and nobody scheduled:
    # `cb_daily` needs its own Tushare endpoint, the text channel goes through
    # akshare. On 2026-10-26 both sat on the 23rd while every bar was current.
    #   * cb_daily has no live consumer since P21 retired cb_link_down. It is
    #     kept warm so a research replay does not silently compute over a window
    #     that ends three sessions early.
    #   * the text channel does have one: knowledge(news) is the only place the
    #     desk reads the world outside prices, and a stale window there answers
    #     with three-day-old headlines without saying that they are old.
    cb: dict = {"rc": 0, "note": "not a trading day"}
    text: dict = {"rc": 0, "note": "not a trading day"}
    anns: dict = {"rc": 0, "note": "not a trading day"}
    if is_open:
        try:
            from scripts import backfill_cb

            rc_cb = backfill_cb.main(["--start", end, "--end", end])
            cb = {"rc": rc_cb, "note": "ok" if rc_cb == 0 else "cb fetch failed"}
        except Exception as exc:  # noqa: BLE001 - research input, never fatal
            cb = {"rc": 1, "note": f"{type(exc).__name__}: {exc}"}
        try:
            from scripts import fetch_text

            rc_text = fetch_text.main(["--top", "10", "--per", "6"])
            text = {"rc": rc_text, "note": "ok" if rc_text == 0 else "text fetch failed"}
        except Exception as exc:  # noqa: BLE001 - the text channel, never fatal
            text = {"rc": 1, "note": f"{type(exc).__name__}: {exc}"}
        # A filing published after the close is the most common reason a thesis
        # is dead before the next open, and cninfo is free. Refreshing the whole
        # market costs ~45 minutes, so the job refreshes what it can act on:
        # what it holds, and what was limit-up or broken today.
        try:
            codes = _announcement_universe(ROOT, end, store)
            if codes:
                from scripts import backfill as backfill_mod

                rc_anns = backfill_mod.main([
                    "--datasets", "anns", "--symbols", ",".join(codes),
                    "--start", _days_before(end, 30), "--end", end,
                ])
                # The minute table gets a wider set than announcements: one
                # session per name is cheap, and the names that matter tomorrow
                # are the ones the screen will surface — liquid, high-turnover,
                # not the six the book happens to hold.
                minute_codes = _minute_universe(ROOT, end, store)
                rc_mins = backfill_mod.main([
                    "--datasets", "stk_mins", "--symbols", ",".join(minute_codes),
                    "--start", end, "--end", end,
                ])
                # Titles say a filing exists; only the body says whether it
                # matters. Two days of recent filings is ~40 PDFs, and it is the
                # only way the excerpt is ever populated without being asked.
                from scripts import fetch_anns_text as text_mod

                text_rc = text_mod.main(["--recent-days", "2", "--limit", "40"])
                anns = {"rc": rc_anns, "symbols": len(codes), "minutes_rc": rc_mins,
                        "minute_symbols": len(minute_codes),
                        "text_rc": text_rc,
                        "note": "ok" if max(rc_anns, rc_mins, text_rc) == 0
                                else "text/intraday fetch failed"}
            else:
                anns = {"rc": 0, "symbols": 0, "note": "no position and no pool"}
        except Exception as exc:  # noqa: BLE001 - never fatal
            anns = {"rc": 1, "note": f"{type(exc).__name__}: {exc}"}

    if (args.snapshot or in_session(now)) and is_open:
        try:
            from service.trading.snapshot import snapshot as take_snapshot

            basic = store.read("stock_basic")
            codes = [str(c) for c in basic["ts_code"] if not str(c).endswith(".BJ")]
            snap = take_snapshot(store, registry, EastMoneyAdapter(), codes)
        except Exception as exc:  # noqa: BLE001 - record the failed critical step
            snapshot_rc = 1
            snap = {"error": f"{type(exc).__name__}: {exc}"}

    quality_path = ROOT / "reports" / "daily" / f"{end}_quality.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.exists() else {}

    # One NAV row per trading day for the core sleeve, cash days included.
    # Without this the decay monitor can only see the days the book traded, which
    # is exactly the sample that looks good. A failure here must not fail the
    # data job: the row is a measurement, not a dependency.
    core_nav: dict = {"recorded": False, "reason": "not a trading day"}
    if is_open:
        try:
            from service.trading.core import CoreBook
            from service.trading.state import PaperStore

            paper = PaperStore(ROOT / "data" / "paper.sqlite")
            core_book = CoreBook(store, paper)
            # A retired sleeve writes no NAV row. Six rows of 100% cash were
            # indistinguishable from a rule choosing cash, and they were not a
            # choice — see `_retired_note` in configs/core.json.
            core_nav = ({"recorded": False, "reason": "core sleeve retired"}
                        if core_book.retired else core_book.record_nav(end))
        except Exception as exc:  # noqa: BLE001 - report, never interrupt the job
            core_nav = {"recorded": False,
                        "reason": f"{type(exc).__name__}: {exc}"}

    # Short-alpha OOS: settle only judgments WorkBuddy already froze. Never call
    # an analyzer to create candidates on the model's behalf.
    short_alpha_oos = _settle_short_alpha_oos(end, is_open)

    brief = build_brief(
        summary=registry.summary(),
        calendar=cal,
        today=end,
        rt_age_seconds=0.0 if snap else None,
        sentiment=(snap or {}).get("sentiment") or sentiment,
    )
    step_rcs = {
        "ths": int(ths.get("rc") or 0),
        "sentiment": int(bool(ths.get("sentiment_error"))),
        "derived_pools": int(derived.get("rc") or 0),
        "intraday_features": int(features.get("rc") or 0),
        "cb_daily": int(cb.get("rc") or 0),
        "text": int(text.get("rc") or 0),
        "snapshot": snapshot_rc,
    }
    for name in ("rc", "minutes_rc", "text_rc"):
        step_rcs[f"announcements.{name}"] = int(anns.get(name) or 0)
    gate, failed_steps, supplementary_failed = _gate(rc_backfill, rc_verify, step_rcs)

    # The facts sheet is what a close report is checked against
    # (`scripts/verify_report.py` refuses numbers that are not in it), and it is
    # also the only per-day snapshot of the account. It must be written **before**
    # that check: the check reads the file, and on 2026-11-12 it read a noon
    # snapshot whose readiness was SESSION_OPEN and whose in_cap was None, then
    # recorded two problems against a close report the evening facts already
    # supported (READY, in_cap=26). A verifier that runs against yesterday's
    # understanding of today is not a verifier.
    facts_path = "skipped"
    try:
        from service.trading import build_desk
        from service.trading.facts import write_facts

        facts_path = str(write_facts(build_desk(root=ROOT), end))
    except Exception as exc:  # noqa: BLE001 - a snapshot must not fail the job
        facts_path = f"skipped: {type(exc).__name__}: {exc}"

    # A report is not a pipeline input, so its verdict must not move `gate`: a
    # model writing a bad close brief should not stop tomorrow's bars from
    # loading. It is reported here instead, next to the row counts, because the
    # alternative — finding out weeks later that a number never had a source — is
    # how 2026-10-29's "6 affordable names" survived.
    report_check: dict = {"checked": False}
    try:
        if str(ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "scripts"))
        import verify_report

        problems = verify_report.check_day(end, strict=False)
        report_check = {
            "checked": True,
            "ok": not any(level == "FAIL" for level, _ in problems),
            "problems": [f"{level}: {message}" for level, message in problems],
        }
    except Exception as exc:  # noqa: BLE001 - a report check must never fail the job
        report_check = {"checked": False, "error": f"{type(exc).__name__}: {exc}"}

    # The API contract document is generated, and a generated document drifts
    # the moment someone changes a route and forgets to regenerate it. On
    # 2026-11-02 it was 13 routes behind (including the one the model uses
    # most). It is reported here rather than gated: a stale document must not
    # stop tomorrow's bars from loading, but it must be visible every evening
    # so the gap is one day old, not one month old.
    contract_check: dict = {"checked": False}
    try:
        if str(ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(ROOT / "scripts"))
        import api_contract

        current = api_contract.render(api_contract.collect())
        stored = api_contract.DOCS.read_text(encoding="utf-8") \
            if api_contract.DOCS.exists() else ""
        contract_check = {
            "checked": True,
            "ok": current == stored,
            "routes": len(api_contract.collect()),
            "note": "" if current == stored else
                    "契约文档已过期：python scripts/api_contract.py --write",
        }
    except Exception as exc:  # noqa: BLE001 - a doc check must never fail the job
        contract_check = {"checked": False, "error": f"{type(exc).__name__}: {exc}"}

    payload = {
        "as_of": now.isoformat(timespec="seconds"),
        "window": [start, end],
        "is_trading_day": is_open,
        "backfill_rc": rc_backfill,
        "verify_rc": rc_verify,
        "gate": gate,
        "failed_steps": failed_steps,
        "supplementary_failed": supplementary_failed,
        "quality_counts": quality.get("counts"),
        "ths": ths,
        "sentiment": sentiment,
        "derived_pools": derived,
        "intraday_features": features,
        "minutes_sweep": minutes_sweep,
        "cb_daily": cb,
        "text": text,
        "announcements": anns,
        "core_nav": core_nav,
        "short_alpha_oos": short_alpha_oos,
        "report_check": report_check,
        "contract_check": contract_check,
        "brief": brief,
        "snapshot": snap,
        "datasets": registry.summary(),
        "backups": backups,
    }
    # A frozen copy of the day's desk. The window is always current; this file is
    # what the evening looked like when the pipeline ran, so a later reader can
    # tell "nothing happened" from "the row changed".
    dashboard = "skipped"
    try:
        from service.api.dashboard import collect as collect_dashboard
        from service.trading import build_desk

        dash_path = ROOT / "reports" / "daily" / f"{end}.dashboard.json"
        write_json_atomic(dash_path, collect_dashboard(build_desk(root=ROOT)))
        dashboard = str(dash_path)
    except Exception as exc:  # noqa: BLE001 - a snapshot must not fail the job
        dashboard = f"skipped: {type(exc).__name__}: {exc}"
    payload["dashboard"] = dashboard

    # Written before `report_check` above, so the check reads the close facts
    # rather than whatever intraday snapshot happened to exist. Recorded here
    # because the path belongs beside the rest of the evening's artefacts.
    payload["facts"] = facts_path

    # Rebuilding the analog index costs ~47s against 3.4M rows, while the host
    # request timeout is 60s (``plugin/lib/index.js`` LIMITS.timeoutMs) — so the
    # first caller after a trading day always timed out. Build it here, once the
    # data has landed, and the next session starts against a warm cache.
    analog_warm = "skipped"
    try:
        from service.trading import analog

        warm_desk = build_desk(root=ROOT)
        warm_day = warm_desk.market.latest_bar_date()
        index = analog.get_index(warm_desk.market.store, ROOT, end=warm_day)
        analog_warm = f"{len(index.dates)} days / {len(index.day_of_row)} rows"
    except Exception as exc:  # noqa: BLE001 - a warm-up must not fail the job
        analog_warm = f"skipped: {type(exc).__name__}: {exc}"
    payload["analog_warm"] = analog_warm

    out = ROOT / "reports" / "daily" / f"{end}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out, payload)

    # A non-zero exit code is a signal, but only if something reads it. The
    # scheduled task's LastResult is invisible to the desk, so the same fact is
    # written where the next reader actually looks.
    alert = write_alert(end, (start, end), gate, rc_backfill=rc_backfill,
                        rc_verify=rc_verify, failed_steps=failed_steps)
    if alert:
        print(f"[daily_job] ALERT written: {alert}")

    # Last thing on the path, so its existence means everything above happened:
    # the report is on disk and the alert has been written or cleared. `ALERT.txt`
    # alone cannot carry this — it is absent both when the run is clean and when
    # the run never started, and 2026-10-29 was the second.
    # Guarded like every other tail step: bookkeeping must not overturn the
    # verdict it is bookkeeping *about*. A missing marker reads as "never ran",
    # which is wrong here — but so is turning a written report into a crash.
    try:
        write_marker(data_root(), end, "close", {
            "gate": gate,
            "failed_steps": failed_steps,
            "supplementary_failed": supplementary_failed,
            "window": [start, end],
            "trading_day": is_open,
            "backfill_rc": rc_backfill,
            "verify_rc": rc_verify,
            "steps": step_rcs,
            "report": f"reports/daily/{end}.json",
        })
    except OSError as exc:  # pragma: no cover - disk full / permissions
        print(f"[daily_job] completion marker NOT written: {exc}", flush=True)

    print(f"[daily_job] window={start}..{end} trading={is_open} "
          f"backfill={rc_backfill} verify={rc_verify} gate={gate} -> {out}")
    return 1 if gate == "not_ready" else 0


if __name__ == "__main__":
    raise SystemExit(main())
