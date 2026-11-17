"""Post-close reconciliation: one fixed checklist, no framework.

A close is not "the report looked fine". The desk has to answer three separate
questions in the same order every day (completeness -> consistency -> freshness),
plus the one that is unique to a point-in-time system: is the reference date
still behind today's disclosures?

    python scripts/postclose_check.py            # JSON
    python scripts/postclose_check.py --human    # readable
    python scripts/postclose_check.py --strict   # exit 1 on any warning

Nothing here writes, orders, or changes configuration. A pre-17:30 run is
expected to report "waiting for the daily job"; that is `info`, not a failure.
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8811"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import promotion_gate  # noqa: E402  (needs ROOT on sys.path)
REPORT_MARKS = (
    ("reports/002185-20261110.html", "盘后状态补记"),
    ("reports/kernel-capability-audit-20261109.html", "15:50 盘后再补记"),
)


def _token() -> str:
    cfg = Path.home() / ".workbuddy" / "mcp.json"
    return json.loads(cfg.read_text(encoding="utf-8"))[
        "mcpServers"]["quant-desk"]["env"]["QUANT_TOKEN"]


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"X-Quant-Token": token})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def _row(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def collect() -> dict:
    today = datetime.now().strftime("%Y%m%d")
    token = _token()
    checks: list[dict] = []

    # 1. Completeness — is the machinery even there?
    try:
        health = _get("/health", token)
        build = health.get("build") or {}
        status = "ok" if health.get("ok") and not build.get("code_changed_since_start") else "warn"
        checks.append(_row("service", status,
                           f"commit={build.get('commit')} code_changed="
                           f"{build.get('code_changed_since_start')} dirty={build.get('dirty')}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_row("service", "warn", f"{type(exc).__name__}: {exc}"))

    done = ROOT / "data" / "daily_done" / f"{today}.json"
    if done.exists():
        payload = json.loads(done.read_text(encoding="utf-8"))
        failed = payload.get("failed_steps") or []
        supplementary = payload.get("supplementary_failed") or []
        note = f"gate={payload.get('gate')} failed={failed}"
        if supplementary:
            note += f" supplementary={supplementary}（不影响判定）"
        checks.append(_row("daily_job", "ok" if payload.get("gate") == "ready" and not failed else "warn",
                           note))
    else:
        checks.append(_row("daily_job", "info",
                           f"{today} 主日更尚未落标记（17:30 之后才有）"))

    for rel, mark in REPORT_MARKS:
        path = ROOT / rel
        ok = path.exists() and mark in path.read_text(encoding="utf-8")
        checks.append(_row(f"report:{path.name}", "ok" if ok else "warn",
                           "含修复后状态注记" if ok else "缺状态注记，正文数字可能已过时"))

    # 2. Consistency — do the declared and observed states agree?
    cursor = ROOT / "data" / "state" / "quarterly_fundamentals.json"
    cursor_period = None
    if cursor.exists():
        cursor_period = json.loads(cursor.read_text(encoding="utf-8")).get("cycle_period")
    checks.append(_row("sync_cursor", "ok" if cursor_period else "warn",
                       f"cycle_period={cursor_period}；新报告期才重置游标"))

    # 3. Freshness — has the reference date caught up with disclosures?
    info = _get(f"/data/information?day={today}", token)
    pit = next((r for r in info.get("dimensions", [])
                if r.get("key") == "point_in_time"), {})
    pit_score = pit.get("score")
    checks.append(_row("point_in_time", "ok" if pit_score == 1.0 else "warn",
                       f"as_of={info.get('as_of')} pit={pit_score} score={info.get('score')}"))
    default = _get("/data/information", token)
    if default.get("as_of") != info.get("as_of"):
        checks.append(_row("as_of_lag", "info",
                           f"默认口径 {default.get('as_of')} 落后于 {today}；"
                           "今晚日更后自动对齐，零前视检查会先报警"))
    sla = info.get("fundamental_sla") or {}
    checks.append(_row("fundamental_sla",
                       "ok" if sla.get("available") and not sla.get("sla_breached") else "warn",
                       f"period={sla.get('latest_period')} coverage={sla.get('coverage')} "
                       f"breached={sla.get('sla_breached')}"))
    text = info.get("announcement_text_coverage") or {}
    checks.append(_row("text_coverage", "info",
                       f"{text.get('with_text')}/{text.get('announcements')} 公告有正文 "
                       f"({text.get('coverage')})；按需抓取，低覆盖不是故障"))
    weak = [(w.get("key"), w.get("score")) for w in info.get("weak_dimensions", [])]
    checks.append(_row("weak_dimensions", "info", f"{weak}"))

    partitions = len(glob.glob(str(ROOT / "data/parquet/intraday_features/*.parquet")))
    checks.append(_row("intraday_partitions", "info",
                       f"{partitions} 天；MIN_BUCKET_N=30，攒够多日才能引用择时分布"))

    handoff = _get("/monitor/handoff", token)
    checks.append(_row("handoff", "ok" if handoff.get("pending") == 0 else "warn",
                       f"pending={handoff.get('pending')} readable={handoff.get('readable')}"))

    arena = _get("/arena", token)
    edge = arena.get("edge") or {}
    checks.append(_row("arena_kpi", "info",
                       f"edge.n={edge.get('n')} / {arena.get('min_decisions_for_an_interval')} "
                       f"平仓；ci={arena.get('ci_position')}"))
    try:
        con = sqlite3.connect(ROOT / "data" / "samples.sqlite")
        cases = con.execute("SELECT count(*) FROM cases").fetchone()[0]
        con.close()
        checks.append(_row("sample_ledger", "info", f"样本账本已平仓 {cases} 笔（与主账本 edge 分开）"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_row("sample_ledger", "info", f"不可读：{type(exc).__name__}"))

    # 晋级门禁是资金开关唯一的日常监督点。关闭时门禁 blocked 是合规状态
    # （info，不是 warning）；只有「已打开却不满足前置」才按机械降级规则报警。
    try:
        gate = promotion_gate.evaluate(promotion_gate.collect())
        if gate["compliance"] == "violation":
            checks.append(_row(
                "promotion_gate", "warn",
                f"trigger.enabled=true 但 {gate['eligible']}/{gate['total']} 条资格满足；"
                f"未过：{'/'.join(gate['blockers'])} —— 按降级规则关回"))
        elif gate["flags"]["enabled"]:
            checks.append(_row(
                "promotion_gate", "ok",
                f"资金开关已开且门禁通过（{gate['eligible']}/{gate['total']}）"))
        else:
            checks.append(_row(
                "promotion_gate", "info",
                f"资金开关关闭，符合门禁（{gate['eligible']}/{gate['total']} 条资格满足；"
                f"未过：{'/'.join(gate['blockers']) or '无'}）"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_row("promotion_gate", "warn",
                           f"门禁不可读，按 fail-closed 报警：{type(exc).__name__}: {exc}"))

    warnings = [c for c in checks if c["status"] == "warn"]
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "today": today, "status": "warn" if warnings else "ok",
            "warning_count": len(warnings), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="收盘后固定清单（只读）")
    ap.add_argument("--human", action="store_true")
    ap.add_argument("--strict", action="store_true", help="有 warn 时退出码 1")
    args = ap.parse_args(argv)
    try:
        out = collect()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    if args.human:
        print(f"收盘后清单 {out['generated_at']}  status={out['status']} "
              f"warnings={out['warning_count']}")
        for c in out["checks"]:
            print(f"  [{c['status']:4s}] {c['check']:24s} {c['detail']}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if args.strict and out["status"] != "ok" else 0


if __name__ == "__main__":
    raise SystemExit(main())
