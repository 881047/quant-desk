"""时段阈值**描述性**复核（v4 P2 的「按时段复核现有阈值，只出报告、不自动改」）。

    python scripts/session_threshold_audit.py
    python scripts/session_threshold_audit.py --json
    python scripts/session_threshold_audit.py --out reports/session_threshold_audit_20261107.md

裁定 D-12
---------
* 数据源是 ``pool_tape`` 的**单表内**时序；``snapshot_ts`` 在这里只用来给自己
  分桶，合法。D-9 禁止的是拿它做 ``rt_quote`` ↔ ``pool_tape`` 的**跨表连接键**，
  两件事不冲突。
* 样本是 3 个交易日 / 445 帧，远低于稳定季节性估计所需的 ≥60 交易日；且
  日历时间聚合会高估开盘与收盘附近的波动（U 型只是背景，不是信号）。
  因此本报告 **inference_allowed=False**、**threshold_recommendations=[]**，
  只给每桶的 ``n`` 与 p25→p75 经验区间，不推断、不推荐、不自动改任何配置。
* ``break_rate_proxy`` 是**行级**「炸板池行数 /(涨停池行数+炸板池行数)」，
  与日级 ``sentiment.break_rate_gate=0.38`` 不是同一个口径，禁止机械比较。
* 本脚本只读行情与配置，唯一写入是用户指定的报告文件；不产生委托、
  不给买卖建议、不改 ``configs/``。
* **D-23**：本报告看过的 20261104/05/06 已被永久标记为「污染的探索期」，
  排除出确认性样本。任何后续季节性推断受
  ``docs/session_seasonality_preregistration_20261107.md``（v1 冻结）约束，
  本脚本本身不会因样本变多而升级为推断工具。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DAYS = ("20261104", "20261105", "20261106")
MIN_FRAMES_FOR_INFERENCE = 20
MIN_SESSIONS_FOR_SEASONALITY = 60
#: 09:15 起的竞价帧单列；连续竞价之外的时间不进入分桶。
SESSION_START_MIN = 9 * 60 + 15
SESSION_END_MIN = 15 * 60


def bucket_of(stamp: Any) -> str | None:
    """``snapshot_ts`` → 5 分钟桶（向下取整），按实际时刻不按序号。"""
    text = str(stamp or "")
    if "T" in text:
        text = text.split("T", 1)[1]
    parts = text.split(":")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    minute = int(parts[0]) * 60 + int(parts[1])
    if not (SESSION_START_MIN <= minute <= SESSION_END_MIN):
        return None
    return f"{int(parts[0]):02d}{int(parts[1]) // 5 * 5:02d}"


def _interval(series: pd.Series) -> list[float] | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return [round(float(values.quantile(0.25)), 4),
            round(float(values.quantile(0.75)), 4)]


def audit(frames: list[pd.DataFrame], *, days: list[str] | None = None,
          thresholds: dict | None = None) -> dict:
    """纯计算：把若干日的 pool_tape 帧合成时段描述。"""
    days = days or []
    if not frames:
        return {"ok": False, "status": "no_data", "buckets": [],
                "inference_allowed": False, "threshold_recommendations": []}
    df = pd.concat(frames, ignore_index=True)
    df["bucket"] = df["snapshot_ts"].map(bucket_of)
    df = df[df["bucket"].notna()].copy()
    if df.empty:
        return {"ok": False, "status": "no_session_rows", "buckets": [],
                "inference_allowed": False, "threshold_recommendations": []}

    for column in ("bid1_volume", "bid_volume_5", "ask_volume_5", "open_times"):
        if column not in df.columns:
            df[column] = None
    denom = (pd.to_numeric(df["bid_volume_5"], errors="coerce")
             + pd.to_numeric(df["ask_volume_5"], errors="coerce"))
    df["obi_5"] = ((pd.to_numeric(df["bid_volume_5"], errors="coerce")
                    - pd.to_numeric(df["ask_volume_5"], errors="coerce"))
                   / denom).where(denom > 0)

    rows: list[dict] = []
    for bucket, group in df.groupby("bucket", sort=True):
        up = int((group["pool"] == "limit_up").sum())
        broken = int((group["pool"] == "broken_board").sum())
        down = int((group["pool"] == "limit_down").sum())
        n_frames = int(group["snapshot_ts"].nunique())
        opened = pd.to_numeric(group["open_times"], errors="coerce")
        rows.append({
            "bucket": str(bucket),
            "n_frames": n_frames,
            "n_rows": len(group),
            "limit_up_rows": up, "broken_board_rows": broken,
            "limit_down_rows": down,
            "break_rate_proxy": (round(broken / (up + broken), 4)
                                 if up + broken else None),
            "bid1_volume_median": (round(float(pd.to_numeric(
                group["bid1_volume"], errors="coerce").median()), 2)
                if pd.to_numeric(group["bid1_volume"], errors="coerce").notna().any()
                else None),
            "bid1_volume_p25_p75": _interval(group["bid1_volume"]),
            "obi_5_median": (round(float(group["obi_5"].median()), 4)
                             if group["obi_5"].notna().any() else None),
            "obi_5_p25_p75": _interval(group["obi_5"]),
            "open_board_rate": (round(float((opened > 0).mean()), 4)
                                if opened.notna().any() else None),
            "status": ("descriptive_only" if n_frames < MIN_FRAMES_FOR_INFERENCE
                       else "descriptive"),
        })

    return {
        "ok": True,
        "status": "ok",
        "days": days,
        "sessions": len({str(d)[:8] for d in days}) or len(days),
        "totals": {
            "rows": len(df),
            "frames": int(df["snapshot_ts"].nunique()),
            "buckets": len(rows),
            "up_rows": int((df["pool"] == "limit_up").sum()),
            "broken_rows": int((df["pool"] == "broken_board").sum()),
            "down_rows": int((df["pool"] == "limit_down").sum()),
        },
        "thresholds": thresholds or {},
        "inference_allowed": False,
        "inference_note": (
            f"样本 {len({str(d)[:8] for d in days}) or len(days)} 个交易日，"
            f"低于稳定季节性估计所需的 ≥{MIN_SESSIONS_FOR_SEASONALITY} 交易日；"
            "且日历时间聚合会高估开盘/收盘附近波动。U 型只作背景条件，"
            "不是信号，不得据此调整阈值。"),
        "threshold_recommendations": [],
        "break_rate_caveat": (
            "break_rate_proxy 是行级代理量，与日级 sentiment.break_rate_gate "
            "口径不同，禁止机械比较；本报告不判断阈值是否该改。"),
        "obi_caveat": (
            "涨停封板时卖档常为 0，OBI₅ 会结构性等于 1.0；那是「没有卖盘」"
            "不是「买盘极强」，不得当成入场信号或强度排序。"),
        "buckets": rows,
    }


def _thresholds() -> dict:
    params = yaml.safe_load((ROOT / "configs" / "params.yaml").read_text(encoding="utf-8"))
    costs = yaml.safe_load((ROOT / "configs" / "costs.yaml").read_text(encoding="utf-8"))
    return {
        "sentiment.break_rate_gate": params.get("sentiment", {}).get("break_rate_gate"),
        "sentiment.break_rate_gate_vendor": params.get("sentiment", {}).get("break_rate_gate_vendor"),
        "tripwire.stop_proximity": params.get("tripwire", {}).get("stop_proximity"),
        "monitor.hard_stop_pct": params.get("monitor", {}).get("hard_stop_pct"),
        "costs.slippage.stock_bps": costs.get("slippage", {}).get("stock_bps"),
    }


def render_md(report: dict) -> str:
    lines = [
        "# 时段阈值描述性复核（D-12）",
        "",
        f"- 生成时刻：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        (f"- 样本：{report['days']}，共 **{report['totals']['rows']} 行 / "
        f"{report['totals']['frames']} 帧 / {report['totals']['buckets']} 个 5 分钟桶**"),
        "- 数据源：`pool_tape`（单表内按 `snapshot_ts` 分桶，不做跨表连接）",
        ("- 结论：**inference_allowed = false；threshold_recommendations = []**。"
        "本报告只描述，不推断、不推荐、不改配置。"),
        "",
        "## 口径与限制",
        "",
        f"- {report['inference_note']}",
        f"- {report['break_rate_caveat']}",
        f"- {report['obi_caveat']}",
        ("- 每桶同时给 `n_frames` 与 `n_rows`；`n_frames < 20` 标 `descriptive_only`，"
        "≥20 标 `descriptive`。**两者都只是描述，不代表可推断**；全局 "
        "`inference_allowed=false`。p25→p75 是经验区间，不是置信区间。"),
        "- U 型日内波动率是背景条件，绝不作为入场信号或自动阈值。",
        ("- **D-23 预注册**：本报告看过的 20261104/05/06 已被永久标记为「污染的探索期」，"
        "排除出确认性样本。后续任何季节性推断必须遵守 "
        "`docs/session_seasonality_preregistration_20261107.md`（v1 冻结）："
        "确认性样本自 2026-11-09 起 ≥60 交易日、按时间顺序最后 20% 为 lockbox、"
        "单一主假设、样本外同号且幅度衰减 ≤50%。进度查 "
        "`python scripts/preregistration_status.py --json`；本脚本不会因样本变多"
        "而升级为推断工具。"),
        "",
        "## 现有阈值（读自配置，未改动）",
        "",
        "| 键 | 值 |",
        "|---|---|",
    ]
    for key, value in report["thresholds"].items():
        lines.append(f"| `{key}` | {value} |")
    lines += [
        "",
        "## 逐桶描述",
        "",
        ("| 桶 | 帧数 n | 行数 n | 涨停行 | 炸板行 | 跌停行 | break 代理 | "
        "bid1 中位 [p25,p75] | OBI₅ 中位 | 开板率 | 状态 |"),
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["buckets"]:
        interval = row["bid1_volume_p25_p75"]
        interval_text = f"{interval[0]}–{interval[1]}" if interval else "—"
        lines.append(
            f"| {row['bucket']} | {row['n_frames']} | {row['n_rows']} | "
            f"{row['limit_up_rows']} | {row['broken_board_rows']} | "
            f"{row['limit_down_rows']} | "
            f"{'—' if row['break_rate_proxy'] is None else row['break_rate_proxy']} | "
            f"{'—' if row['bid1_volume_median'] is None else row['bid1_volume_median']} "
            f"[{interval_text}] | "
            f"{'—' if row['obi_5_median'] is None else row['obi_5_median']} | "
            f"{'—' if row['open_board_rate'] is None else row['open_board_rate']} | "
            f"{row['status']} |")
    lines += [
        "",
        "## 不做的事",
        "",
        ("- 不因某桶 `break_rate_proxy` 偏离 0.38 就建议改 `sentiment.break_rate_gate`；"
        "两者口径不同，且样本只有 3 日。"),
        ("- 不因开盘/收盘桶数值更高就把它写成「时段信号」；日历时间聚合的过度聚合效应"
        "足以解释大部分 U 型外观。"),
        ("- 不改 `tripwire.stop_proximity=0.02`、`monitor.hard_stop_pct=0.05`、"
        "股票滑点 10bps 等全天单一口径；本报告没有足够证据支持任何分时段参数。"),
        "- 不给买卖建议、不产生委托、不写 `configs/`。",
        "",
        "## 复跑",
        "",
        "```bash",
        "python scripts/session_threshold_audit.py --json",
        "```",
        "",
        ("样本积累到 ≥60 个交易日后，才可考虑季节性估计与样本外验证；届时另立预注册"
        "假设，不在本报告里追加结论。"),
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="descriptive session-threshold audit")
    parser.add_argument("--root", default=str(ROOT / "data"))
    parser.add_argument("--days", default=",".join(DEFAULT_DAYS))
    parser.add_argument("--out", default=str(ROOT / "reports" / "session_threshold_audit_20261107.md"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    frames = []
    for day in days:
        path = Path(args.root) / "parquet" / "pool_tape" / f"{day}.parquet"
        if path.exists():
            frames.append(pd.read_parquet(path))
    report = audit(frames, days=days, thresholds=_thresholds())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    elif report["ok"]:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_md(report), encoding="utf-8")
        print(f"[session_threshold_audit] {out} "
              f"({report['totals']['rows']} rows / {report['totals']['buckets']} buckets)")
    else:
        print(f"[session_threshold_audit] {report['status']}", file=sys.stderr)
    return 0 if report["ok"] else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
