"""G1 离线 parity 的**可复算**数据就绪度。只读，不触发 Promotion。

    python scripts/parity_readiness.py
    python scripts/parity_readiness.py --root data --json

2026-11-07 的一次性探针得出了 `no_parity`；把它做成可复算查询，是为了
11-09 首个带 `frame_id` 的交易日之后能一键重跑，而不是凭记忆说「应该够了」。

口径
----
* G1 要的是 **≥100 条 terminal shadow 比较**、跨 ≥10 交易日、≥3 regime 且
  每 regime ≥20；100 是**比较数**不是成交数。
* G1' 真仓护栏：≥20 笔真实平仓才可谈「这套系统行不行」，但不足**不阻止**
  离线 parity 运行，只是单独标注。
* 测试用临时 PAPER_DB，生产账本为空属正常；**不许拿测试成交冒充真仓样本**。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _schema_names(path: Path) -> list[str]:
    try:
        return list(pq.read_schema(path).names)
    except Exception:  # noqa: BLE001 - 损坏分区视为没有 frame_id
        return []


def _tape_frames(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        frame = pd.read_parquet(path, columns=["snapshot_ts"])
        return int(frame["snapshot_ts"].nunique())
    except Exception:  # noqa: BLE001
        return 0


def _ledger(root: Path) -> dict:
    db = root / "paper.sqlite"
    out = {"paper_fills": 0, "tickets": 0, "decisions": 0,
           "terminal_comparisons": 0}
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        for table in ("paper_fills", "tickets", "decisions"):
            try:
                out[table] = int(con.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                pass
        if out["tickets"]:
            for (payload,) in con.execute("SELECT payload FROM tickets"):
                try:
                    row = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                if (row.get("decision_ts")
                        and row.get("status") in ("armed", "filled", "expired")):
                    out["terminal_comparisons"] += 1
    finally:
        con.close()
    return out


def readiness(root: Path | str, *, min_comparisons: int = 100,
              min_days: int = 10, min_regimes: int = 3,
              min_per_regime: int = 20, real_fill_min: int = 20) -> dict:
    """统计现状并给出 G1 差距。纯只读。"""
    root = Path(root)
    days: list[dict[str, Any]] = []
    for directory in sorted((root / "rt_snapshots").glob("*/")):
        if not directory.is_dir():
            continue
        cold_dir = root / "rt_archive" / directory.name
        quote = root / "parquet" / "rt_quote" / f"{directory.name}.parquet"
        tape = root / "parquet" / "pool_tape" / f"{directory.name}.parquet"
        days.append({
            "day": directory.name,
            "hot_frames": len(list(directory.glob("*.parquet"))),
            "cold_frames": (len(list(cold_dir.glob("*.parquet")))
                            if cold_dir.is_dir() else 0),
            "tape_frames": _tape_frames(tape),
            "rt_quote_has_frame_id": ("frame_id" in _schema_names(quote)
                                      if quote.exists() else False),
        })

    features_dir = root / "parquet" / "intraday_features"
    features = (sorted(p.name for p in features_dir.glob("*.parquet"))
                if features_dir.is_dir() else [])
    ledger = _ledger(root)
    comparable_days = sum(1 for d in days if d["rt_quote_has_frame_id"])
    terminal = ledger["terminal_comparisons"]

    checks = [
        {"name": "terminal_shadow_comparisons", "actual": terminal,
         "required": min_comparisons, "ok": terminal >= min_comparisons},
        {"name": "days_with_frame_id", "actual": comparable_days,
         "required": min_days, "ok": comparable_days >= min_days},
        {"name": "regimes", "actual": 0, "required": min_regimes,
         "ok": False, "note": "当前无 regime 标注，需按 params.yaml:92-101 分档"},
        {"name": "real_fills", "actual": ledger["paper_fills"],
         "required": real_fill_min, "ok": ledger["paper_fills"] >= real_fill_min,
         "blocks_parity": False},
    ]
    if terminal < 60:
        status = "no_parity"
    elif terminal < min_comparisons:
        status = "insufficient_shadow_samples"
    elif not all(c["ok"] for c in checks if c["name"] != "real_fills"):
        status = "blocked"
    else:
        status = "ready_for_gate"

    return {
        "ok": True,
        "status": status,
        "promotion_allowed": False,
        "checks": checks,
        "days": days,
        "totals": {
            "frame_days": len(days),
            "hot_frames": sum(d["hot_frames"] for d in days),
            "cold_frames": sum(d["cold_frames"] for d in days),
            "comparable_days": comparable_days,
            "intraday_features_partitions": len(features),
        },
        "intraday_features": features,
        "ledger": ledger,
        "note": ("只读盘点，不触发 Promotion。测试用临时 PAPER_DB，生产账本为空属正常；"
                 "不许拿测试成交冒充真仓样本。20 笔平仓 KPI 之前不下系统有效性结论。"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="G1 parity data readiness")
    parser.add_argument("--root", default=str(ROOT / "data"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    out = readiness(args.root)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"status = {out['status']}  (promotion_allowed=False)")
        for check in out["checks"]:
            print(f"  {check['name']:<32} {check['actual']:>6} / {check['required']:<6} "
                  f"{'OK' if check['ok'] else 'GAP'}")
        print(f"  frame_days={out['totals']['frame_days']} "
              f"hot_frames={out['totals']['hot_frames']} "
              f"cold_frames={out['totals']['cold_frames']} "
              f"comparable_days={out['totals']['comparable_days']}")
        print(out["note"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
