"""AKShare 免费源逐笔/五档**离线校准集**采集器。

    python scripts/fetch_akshare_calibration.py --day 20261109 --symbols 600000,000001
    python scripts/fetch_akshare_calibration.py --self-test --day 20261106

用途隔离（裁定 D-10）
--------------------
本数据集标 **T1**（catalog 的 T1 定义本就含 akshare），但只做盘后离线校准：
不进 ``realtime.service_for`` / ``from_frame``、不进撮合定价、不进实时决策链。
``OFFLINE_CALIBRATION_ONLY`` 常量与契约测试一起钉死这条边界。

三道闸（复用 intraday_sweep.py 的形态）
--------------------------------------
1. 进程锁：免费源禁不起并发，两次撞在一起比跑得慢糟得多。
2. 按日请求预算：按**运行日**计数、留两成余量；不够就不开始，不跑到一半被限流。
3. 非盘中判据：目标日的**日线已在库**才采。判据是数据到没到，不是今天收没收盘。

质量闸（裁定 D-11）
------------------
落库前逐列断言 + ``schema_version`` 比对；任何一项不过**整批 quarantine、
不落库、不静默 cast**。单位（手↔股、分↔元）由 schema_version 钉死。
AKShare 失败/限流 → ``calibration_degraded: true``、兜底 ``stk_mins``、
**10bps 闸门不放宽且不允许 Promotion**。

本模块可注入 ``fetcher``，默认才 lazy import akshare；测试全程不联网。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry

SCHEMA_VERSION = "akshare_calibration_v1"
REQUIRED_COLUMNS = ("trade_date", "symbol", "quote_ts", "last",
                    "bid1_volume", "ask1_volume", "schema_version")
#: 09:15–11:30 / 13:00–15:00，上海墙钟。竞价与收盘集合竞价都算在窗口内。
SESSIONS = ((9 * 60 + 15, 11 * 60 + 30), (13 * 60, 15 * 60))
LOCK = ROOT / "data" / "akshare_calibration.lock"
BUDGET = ROOT / "data" / "akshare_budget.json"
LOCK_TTL_SECONDS = 7200
DAILY_LIMIT = 2000
BUDGET_RESERVE = 0.8


def acquire_lock(path: Path = LOCK, *, now: datetime | None = None) -> bool:
    """锁文件写时间戳；超 TTL 视为上次崩溃留下的 stale 锁。"""
    now = now or datetime.now()
    if path.exists():
        try:
            stamp = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
            if (now - stamp).total_seconds() < LOCK_TTL_SECONDS:
                return False
        except (ValueError, OSError):
            pass
        path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
    return True


def release_lock(path: Path = LOCK) -> None:
    path.unlink(missing_ok=True)


def allow_budget(path: Path, count: int, *, now: datetime | None = None) -> tuple[bool, dict]:
    """按运行日计数，留两成余量。返回 (是否放行, 账本快照)。"""
    now = now or datetime.now()
    today = now.strftime("%Y%m%d")
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    used = int(data.get(today, 0))
    cap = int(DAILY_LIMIT * BUDGET_RESERVE)
    if used + count > cap:
        return False, {"used": used, "requested": count, "cap": cap}
    data[today] = used + count
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return True, {"used": data[today], "requested": count, "cap": cap}


def session_ready(store, day: str) -> bool:
    """目标日的日线已落库才允许采；盘中/数据未到一律跳过。"""
    try:
        bars = store.read("daily", day, day, columns=["trade_date"])
    except Exception:  # noqa: BLE001 - 读不出就是没准备好
        return False
    return bars is not None and not bars.empty


def _minute(ts: Any) -> int | None:
    text = str(ts or "")
    if "T" in text:
        text = text.split("T", 1)[1]
    parts = text.split(":")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]) * 60 + int(parts[1])


def validate_frame(frame: pd.DataFrame | None, day: str) -> tuple[bool, list[str]]:
    """逐列断言。返回 (可落库, 原因列表)；任何原因都导致整批 quarantine。"""
    if frame is None or frame.empty:
        return False, ["empty_frame"]
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        # 绝不静默补列或 cast：上游改版时补出来的列会把单位错误写进分区。
        return False, [f"missing_columns:{missing}"]
    versions = frame["schema_version"].astype(str).unique().tolist()
    if versions != [SCHEMA_VERSION]:
        return False, [f"schema_version_mismatch:{versions}"]

    reasons: list[str] = []
    last = pd.to_numeric(frame["last"], errors="coerce")
    if last.isna().any() or (last <= 0).any():
        reasons.append("last_must_be_positive")
    for column in ("bid1_volume", "ask1_volume"):
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or (values < 0).any():
            reasons.append(f"{column}_must_be_nonnegative")
    bad_ts = 0
    for ts in frame["quote_ts"]:
        minute = _minute(ts)
        if minute is None or not any(lo <= minute <= hi for lo, hi in SESSIONS):
            bad_ts += 1
    if bad_ts:
        reasons.append(f"quote_ts_outside_session:{bad_ts}")
    if "trade_date" in frame.columns:
        bad_day = frame["trade_date"].astype(str) != str(day)
        if bad_day.any():
            reasons.append(f"trade_date_mismatch:{int(bad_day.sum())}")
    return not reasons, reasons


#: 真实字段映射**尚未在交易日实采验证**（2026-11-07 是周六）。免费源接口随
#: 上游改版，未实测的列名/单位映射不许进生产校准分区。首个交易日先校对
#: `stock_bid_ask_em` 的返回形态与单位（手↔股、分↔元），再置 True 并写台账。
VERIFIED_REAL_FETCHER = False


def _default_fetcher(symbol: str, day: str) -> pd.DataFrame:
    """真实源。lazy import；未实采校准前显式拒绝，绝不静默写错单位的数。"""
    if not VERIFIED_REAL_FETCHER:
        raise RuntimeError(
            "real AKShare fetcher is not field-verified yet; "
            "use --self-test or inject a verified fetcher")
    import akshare as ak

    raw = ak.stock_bid_ask_em(symbol=symbol)
    # 归一化在这里显式做完并打 schema_version；上游字段漂移由 validate 拦下。
    out = pd.DataFrame({
        "trade_date": [str(day)], "symbol": [str(symbol)],
        "quote_ts": [datetime.now().strftime("%H:%M:%S")],
        "last": [raw.get("last")], "bid1_volume": [raw.get("bid1_volume")],
        "ask1_volume": [raw.get("ask1_volume")],
        "schema_version": [SCHEMA_VERSION],
    })
    return out


def collect(
    day: str,
    symbols: list[str],
    *,
    store,
    fetcher: Callable[[str, str], pd.DataFrame] | None = None,
    lock_path: Path = LOCK,
    budget_path: Path = BUDGET,
    now: datetime | None = None,
    skip_session_gate: bool = False,
) -> dict:
    """采一批并落库。只读账本/写校准分区；绝不写 paper 账本或产生委托。"""
    now = now or datetime.now()
    base = {"day": str(day), "schema_version": SCHEMA_VERSION,
            "calibration_degraded": False, "fallback": None,
            "promotion_allowed": True, "reasons": [], "rows": 0}
    if not acquire_lock(lock_path, now=now):
        return {**base, "ok": False, "status": "locked",
                "reasons": ["另一个采集在跑，或 stale 锁未过期"]}
    try:
        if not skip_session_gate and not session_ready(store, str(day)):
            return {**base, "ok": False, "status": "not_ready",
                    "reasons": ["目标日日线未落库（盘中或数据未到）"]}
        allowed, ledger = allow_budget(budget_path, len(symbols), now=now)
        if not allowed:
            return {**base, "ok": False, "status": "budget_exceeded",
                    "budget": ledger, "reasons": ["当日请求预算不足"]}

        pull = fetcher or _default_fetcher
        pieces: list[pd.DataFrame] = []
        try:
            for symbol in symbols:
                pieces.append(pull(str(symbol), str(day)))
        except Exception as exc:  # noqa: BLE001 - 限流/改版是状态，不是崩溃
            return {**base, "ok": False, "status": "degraded",
                    "calibration_degraded": True, "fallback": "stk_mins",
                    "promotion_allowed": False,
                    "reasons": [f"vendor:{type(exc).__name__}: {exc}"],
                    "note": "10bps 闸门不放宽；本轮不允许 Promotion。"}
        frame = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
        ok, reasons = validate_frame(frame, str(day))
        if not ok:
            # 整批 quarantine：不落库、不补列、不 cast。
            return {**base, "ok": False, "status": "quarantine",
                    "calibration_degraded": True, "fallback": "stk_mins",
                    "promotion_allowed": False, "reasons": reasons,
                    "rows": len(frame)}
        store.write("akshare_calibration", str(day), frame, as_of=str(day),
                    mode="replace", status="ok",
                    note=f"offline calibration {day}; schema={SCHEMA_VERSION}")
        return {**base, "ok": True, "status": "ok", "rows": len(frame)}
    finally:
        release_lock(lock_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AKShare offline calibration collector")
    parser.add_argument("--day", required=True, help="YYYYMMDD")
    parser.add_argument("--symbols", default="", help="comma separated 6-digit codes")
    parser.add_argument("--self-test", action="store_true",
                        help="use a synthetic fetcher; never touches the network")
    args = parser.parse_args(argv)
    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.self_test:
        def fake(symbol, day):
            return pd.DataFrame({
                "trade_date": [day], "symbol": [symbol], "quote_ts": ["10:00:00"],
                "last": [10.5], "bid1_volume": [100.0], "ask1_volume": [50.0],
                "schema_version": [SCHEMA_VERSION],
            })
        out = collect(args.day, symbols, store=store, fetcher=fake,
                      skip_session_gate=True)
    else:
        out = collect(args.day, symbols, store=store)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["ok"] else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
