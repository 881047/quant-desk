"""把当日带 frame_id 的 rt_quote 派生成 intraday_features 分区。

    python scripts/derive_intraday.py --day 20261109

由 17:30 的日更主链路调用（见 ``DAILY_JOB_STEPS``）。**旧分区没有 frame_id**
是已知边界，不是失败：打印状态并返回 0，绝不拿 snapshot_ts / 行号假装帧。
冷存上线（2026-11-07）之前的日子不可回补。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.trading.intraday_features import build_features


def main(argv: list[str] | None = None, *, store: DatasetStore | None = None) -> int:
    parser = argparse.ArgumentParser(description="derive intraday_features from rt_quote")
    parser.add_argument("--day", required=True, help="YYYYMMDD")
    args = parser.parse_args(argv)
    day = str(args.day)

    if store is None:
        registry = Registry(ROOT / "data" / "registry.sqlite")
        store = DatasetStore(ROOT / "data", registry)

    try:
        quotes = store.read("rt_quote", day, day)
    except Exception as exc:  # noqa: BLE001 - 源表读不出是状态，不是崩溃
        print(f"[intraday_features] {day} rt_quote 读不出：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 0
    # pool_tape 是加分项：读不到、或旧分区缺 frame_id，特征表仍要产出，
    # 只是 depth_available=False 并如实标注，绝不静默补 0。
    try:
        tape = store.read("pool_tape", day, day)
    except Exception:  # noqa: BLE001
        tape = None

    out = build_features(quotes, day, tape=tape)
    if not out["ok"]:
        # legacy_partition_no_frame_id / empty / no_source 都是「这一天没有特征」，
        # 不是日更失败。写进日志，别把 gate 染红。
        print(f"[intraday_features] {day} {out['status']}: {out['note']}")
        return 0

    store.write("intraday_features", day, out["frame"], as_of=day,
                mode="replace", status="ok",
                note=f"derived from rt_quote {day}; {out['frames']} frames")
    # A manual rebuild is the only thing that happens after a failed close run
    # on 2026-11-12: the partition was rebuilt at 18:35 but ALERT.txt still said
    # not_ready. Resolve the step where the rebuild happens.
    try:
        import daily_job

        if daily_job.resolve_alert_step("intraday_features", day, ROOT):
            print(f"[intraday_features] {day} ALERT step resolved")
    except Exception as exc:  # noqa: BLE001 - an alert update must not fail the build
        print(f"[intraday_features] ALERT not updated: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    print(f"[intraday_features] {day} ok: {out['rows']} rows / {out['frames']} frames"
          f" / tape={out['tape_status']} depth_rows={out['depth_rows']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
