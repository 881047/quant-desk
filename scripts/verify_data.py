"""Run the full quality suite over stored partitions and publish a report.

    python scripts/verify_data.py --start 20240101 --reconcile

Outputs
-------
- ``reports/data_quality.json`` + ``reports/P1_data_quality.md`` — only when
  ``--out`` is omitted, i.e. a **manual** full run. No scheduled job writes
  these two paths; the 17:30 chain always passes
  ``--out reports/daily/<day>_quality.json``. So a date on ``data_quality.json``
  is the date of the last manual run, not "yesterday" — it sat at 2026-11-02
  for two weeks while every trading day was being verified into ``daily/``.
- ``reports/daily/<day>_quality.json`` — what the scheduled chain publishes.
  This is the file to read for "what did the scan say on day X".
- registry rows in ``quality_report`` + manifest status updates (quarantine
  gate). ``GET /data/quality`` serves **these rows**, not any of the files
  above, so it is the only view that is current by construction.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.adapters import catalog
from service.adapters.parquet_store import DatasetStore
from service.adapters.pit import TradingCalendarStore
from service.adapters.quality import evaluate, reconcile_with_baostock
from service.adapters.sqlite_store import Registry

# Only the authoritative price panel gets a gap check. daily_basic, stk_limit
# and adj_factor are naturally sparse on suspended days.
DAILY_LIKE = {"daily"}


def load_calendar(store: DatasetStore) -> TradingCalendarStore:
    cal = store.read("trade_cal")
    return TradingCalendarStore.from_frame(cal)


def load_suspensions(store: DatasetStore) -> dict[str, set[str]]:
    df = store.read("suspend_d")
    if df.empty:
        return {}
    out: dict[str, set[str]] = {}
    for code, group in df.groupby("ts_code"):
        out[str(code)] = set(group["trade_date"].astype(str))
    return out


def limit_up_counts(store: DatasetStore, start: str, end: str) -> dict[str, int]:
    daily = store.read("daily", start, end, columns=["ts_code", "trade_date", "close"])
    limits = store.read("stk_limit", start, end, columns=["ts_code", "trade_date", "up_limit"])
    if daily.empty or limits.empty:
        return {}
    merged = daily.merge(limits, on=["ts_code", "trade_date"], how="inner")
    hit = merged[pd.to_numeric(merged["close"], errors="coerce")
                 >= pd.to_numeric(merged["up_limit"], errors="coerce") * 0.999]
    return {str(k): int(v) for k, v in hit.groupby("trade_date").size().items()}


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="verify stored data quality")
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--start", default="20240101")
    parser.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--reconcile", action="store_true", help="sample-check vs baostock")
    parser.add_argument("--repair", action="store_true",
                        help="drop manifest rows whose file no longer exists")
    parser.add_argument("--out", default=None,
                        help="report path (default reports/data_quality.json); "
                             "daily jobs use reports/daily/<date>_quality.json")
    parser.add_argument("--allow-quarantine", action="store_true",
                        help="report quarantined partitions but exit successfully")
    args = parser.parse_args(argv)

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    calendar = load_calendar(store)
    suspensions = load_suspensions(store)
    expected = limit_up_counts(store, args.start, args.end) if calendar.days else {}
    cal_days = set(calendar.days)

    names = [args.dataset] if args.dataset else [r.dataset for r in registry.list_manifest()]
    names = [n for n in dict.fromkeys(names) if n in catalog.SPECS]

    results = []
    for name in names:
        recs = store.partitions(name)
        for rec in recs:
            path = Path(rec.path)
            if not path.exists():
                # A manifest row without a file is lineage that describes nothing.
                # Left in place it quarantines the partition on every nightly scan
                # for ever (ths_limit_stat 20261023 did exactly that), and a
                # standing quarantine is indistinguishable from a new one.
                # `--repair` drops the row so the dataset's coverage is recomputed
                # from partitions that actually exist.
                issue = {"code": "missing_file", "severity": "error",
                         "count": 0, "ratio": 0.0, "detail": str(path)}
                if args.repair:
                    registry.drop_partition(name, rec.partition)
                    issue = {"code": "manifest_row_dropped", "severity": "warning",
                             "count": 1, "ratio": 0.0,
                             "detail": f"{rec.partition}: {path} missing, row removed"}
                results.append({"dataset": name, "partition": rec.partition,
                                "status": "quarantine" if not args.repair else "warn",
                                "rows": 0, "issues": [issue]})
                continue
            try:
                df = pd.read_parquet(path)
            except (OSError, ValueError) as exc:
                # A truncated file, a locked handle, an Arrow decode error: any of
                # these used to propagate out of `main()` and take the whole
                # verification with it, so one bad partition reported nothing about
                # the other 2,600.
                results.append({"dataset": name, "partition": rec.partition,
                                "status": "quarantine", "rows": 0,
                                "issues": [{"code": "unreadable_file", "severity": "error",
                                            "count": 1, "ratio": 1.0,
                                            "detail": f"{type(exc).__name__}: {exc}"}]})
                continue
            use_cal = cal_days if name in DAILY_LIKE else None
            use_susp = suspensions if name == "daily" else None
            adj = pd.DataFrame()
            if name == "daily":
                adj_rec = registry.get_manifest("adj_factor", rec.partition)
                if adj_rec is not None and Path(adj_rec.path).exists():
                    try:
                        adj = pd.read_parquet(adj_rec.path)
                    except (OSError, ValueError):
                        adj = pd.DataFrame()
            q = evaluate(name, rec.partition, df, calendar_days=use_cal,
                         suspensions=use_susp,
                         expected_limit_ups=expected.get(rec.partition)
                         if name == "limit_up_pool" else None,
                         adj_frame=adj if name == "daily" else None)
            payload = q.as_dict()
            registry.record_quality(name, rec.partition, q.status, q.rows,
                                    [i.as_dict() for i in q.issues])
            if q.status != rec.status:
                registry.set_manifest_status(
                    name, rec.partition, q.status,
                    note=",".join(i.code for i in q.issues)[:200],
                )
            results.append(payload)
            flag = {"ok": "OK ", "warn": "WRN", "quarantine": "QAR"}[q.status]
            print(f"[{flag}] {name}[{rec.partition}] rows={q.rows} issues={len(q.issues)}")

    reconcile = None
    if args.reconcile:
        print("[reconcile] sampling Tushare vs baostock ...")
        reconcile = reconcile_with_baostock(store, start=args.start, end=args.end)
        print(json.dumps(reconcile, ensure_ascii=False)[:400])

    summary = registry.summary()
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "range": [args.start, args.end],
        "calendar_days": len(cal_days),
        "datasets": summary,
        "reconcile": reconcile,
        "results": results,
        "counts": {
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "warn": sum(1 for r in results if r["status"] == "warn"),
            "quarantine": sum(1 for r in results if r["status"] == "quarantine"),
        },
    }
    # A single-dataset run must not overwrite the whole-market report. It did
    # exactly that on 2026-10-28: `--dataset ths_limit_stat --repair` left
    # `reports/data_quality.json` describing one dataset, and the file kept its
    # name, so the next reader would have taken it for the market-wide scan.
    suffix = f"_{args.dataset}" if args.dataset else ""
    out_json = (Path(args.out) if args.out
                else ROOT / "reports" / f"data_quality{suffix}.json")
    md_path = (out_json.with_suffix(".md") if args.out
               else ROOT / "reports" / f"P1_data_quality{suffix}.md")
    try:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        write_markdown(report, md_path)
    except Exception as exc:  # noqa: BLE001 - CLI must report a distinct write failure
        print(f"error: could not write quality report: {exc}", file=sys.stderr)
        return 2
    print(f"[done] {report['counts']} -> {out_json}")
    if report["counts"]["quarantine"] and not args.allow_quarantine:
        return 1
    return 0


def write_markdown(report: dict, path: Path | None = None) -> None:
    lines = [
        "# P1 数据质量报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 覆盖区间：{report['range'][0]} – {report['range'][1]}",
        f"- 分区结论：ok {report['counts']['ok']} / warn {report['counts']['warn']} / quarantine {report['counts']['quarantine']}",
        "",
        "## 数据集",
        "",
        "| 数据集 | 分区 | 行数 | 覆盖 | 最近入库 | ok/warn/quarantine |",
        "|---|---:|---:|---|---|---|",
    ]
    for d in report["datasets"]:
        lines.append(
            f"| {d['dataset']} | {d['partitions']} | {d['rows'] or 0:,} | "
            f"{d['cover_start'] or '-'} ~ {d['cover_end'] or '-'} | "
            f"{str(d['last_ingested'])[:19]} | {d['ok_partitions']}/{d['warn_partitions']}/"
            f"{d['quarantine_partitions']} |"
        )

    findings = [r for r in report["results"] if r.get("issues")]
    lines += ["", "## 问题明细", ""]
    if not findings:
        lines.append("无。")
    else:
        lines += ["| 数据集 | 分区 | 级别 | 检查 | 数量 | 占比 | 说明 |",
                  "|---|---|---|---|---:|---:|---|"]
        for r in findings:
            for i in r["issues"]:
                # A missing file carries no row count; nothing was read. Using
                # .get here is what keeps one sparse issue from taking the whole
                # daily job down after the backfill already succeeded.
                count = i.get("count", 0)
                ratio = i.get("ratio", 0.0)
                lines.append(
                    f"| {r['dataset']} | {r['partition']} | {i['severity']} | {i['code']} | "
                    f"{count} | {ratio:.4%} | {i.get('detail', '')} |"
                )

    if report.get("reconcile"):
        rec = report["reconcile"]
        lines += ["", "## 跨源对账（Tushare vs baostock）", ""]
        if rec.get("ok"):
            lines.append(
                f"- 抽样 {rec['codes']} 只 / 比对 {rec['compared']} 行，"
                f"超差 {rec['mismatch']} 行，最大偏差 {rec['max_diff']:.4%}，"
                f"P95 偏差 {rec['p95_diff']:.4%}（阈值 {rec['tolerance']:.2%}）"
            )
        else:
            lines.append(f"- 跳过：{rec.get('skipped')}")

    (path or (ROOT / "reports" / "P1_data_quality.md")).write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
