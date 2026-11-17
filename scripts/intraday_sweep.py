"""Collect 5-minute bars for the whole market — a range at a time, not a day at a time.

    python scripts/intraday_sweep.py --dry                     # 要抓多少、还差多少、几次请求
    python scripts/intraday_sweep.py --days 5                  # 最近 5 个交易日，全市场
    python scripts/intraday_sweep.py --day 20261030 --refresh  # 用 baostock 覆盖那天的旧行
    python scripts/intraday_sweep.py --source http --limit 50  # 兜底：老的东财/腾讯路径

为什么是「区间」而不是「一天一天」：baostock 的规则是**每日 5 万次请求**、**不许并发**。
一次 `query_history_k_data_plus` 拉一个代码的一整段区间，所以全市场 1 天 = 5,571 次，
全市场 20 天还是 5,571 次。行数是流式读的：时间随行数涨，请求数不涨。一天一天拉会把
请求数乘以天数，20 天就是 11 万次——直接进黑名单。

时间预算（实测 ~520 行/秒，单连接）：一只票一个交易日 48 行 ≈ 0.09 s。
5,571 只 × N 天 × 48 行 ÷ 520 ≈ N × 8.5 分钟。所以：

| 窗口 | 请求数 | 时长 | 体积 |
|---|---|---|---|
| 1 天 | 5,571 | ~9 分钟 | ~3.3 MB |
| 5 天 | 5,571 | ~43 分钟 | ~17 MB |
| 20 天 | 5,571 | ~2.8 小时 | ~66 MB |

三道闸：

1. **单连接 + 进程锁**（`data/baostock.lock`）。提供方明确不许并发，两次运行撞在一起
   比跑得慢糟得多。
2. **每日请求预算**（`data/baostock_budget.json`，按日期计数、留两成余量）。开始前先算
   `需要多少 vs 还剩多少`，不够就不开始，而不是跑到一半被拉黑。
3. **不在盘中跑**：判据是"那天的日线在不在"，不是"今天收没收盘"——扫的往往是过去某天。

抓不动的票单独列出来（`no_bars` 停牌 / `unsupported` 北交所 / `error:*`），不并进成功。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.adapters.baostock_minutes import BS_USABLE, Budget, fetch_range
from service.trading import get_desk
from service.trading.facts import check_readiness

LOCK = ROOT / "data" / "baostock.lock"
BUDGET = ROOT / "data" / "baostock_budget.json"


def target_days(desk, end: str, count: int) -> list[str]:
    """The ``count`` sessions up to and including ``end``, from the daily table.

    ``MarketData.daily(day)`` 是单日横截面，旧实现误把它当历史日期表，导致
    任意 ``--days N`` 都退化为一天。直接读取日线日期列，并只载入足够覆盖
    N 个交易日的日历窗口；节假日仍以真实 daily 行为准。
    """
    if count < 1:
        return []
    end_day = datetime.strptime(str(end), "%Y%m%d")
    start = (end_day - timedelta(days=max(14, int(count * 1.7) + 14))).strftime("%Y%m%d")
    frame = desk.market.store.read("daily", start, str(end), columns=["trade_date"])
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return []
    days = sorted({str(day).replace("-", "") for day in frame["trade_date"].unique()})
    days = [day for day in days if day <= str(end)]
    return days[-count:]


def coverage(desk, days: list[str]) -> dict[str, dict[str, frozenset[str]]]:
    """Per day, which symbols already have rows — and from which sources.

    带上来源是为了让 `--refresh` 能续跑：一次全市场换源要二十分钟，中间断了重跑时，
    已经换过的那些不该再抓一遍。只看"有没有行"做不到这件事——换到一半的分区里
    两种源都在。
    """
    out: dict[str, dict[str, frozenset[str]]] = {}
    for day in days:
        try:
            frame = desk.market.store.read_partition(
                "stk_mins", day, columns=["ts_code", "source"])
        except (KeyError, FileNotFoundError, ValueError):
            out[day] = {}
            continue
        if frame is None or frame.empty:
            out[day] = {}
            continue
        grouped = (frame.assign(ts_code=frame["ts_code"].astype(str),
                                source=frame["source"].astype(str))
                   .groupby("ts_code")["source"].agg(lambda s: frozenset(s)))
        out[day] = grouped.to_dict()
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock() -> bool:
    """One connection at a time. A stale lock from a killed run is cleaned by hand."""
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and _alive(pid):
            return False
        LOCK.unlink(missing_ok=True)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _dash(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}"


def _http_pass(desk, day: str, todo: list[str], *, workers: int, chunk: int) -> dict:
    """The old path: one request per symbol per day, amount missing on tencent rows."""
    from concurrent.futures import ThreadPoolExecutor

    from service.adapters.eastmoney_adapter import EastMoneyAdapter
    from service.adapters.minutes import fetch_minutes
    from service.adapters.tencent_adapter import TencentQuoteAdapter

    em, tg = EastMoneyAdapter(), TencentQuoteAdapter()
    failed: dict[str, str] = {}
    sources: dict[str, int] = {}
    pending: list[pd.DataFrame] = []
    rows_written = 0

    def flush():
        nonlocal pending, rows_written
        if pending:
            frame = pd.concat(pending, ignore_index=True)
            desk.market.store.write("stk_mins", day, frame, as_of=day, mode="merge",
                                    note=f"http sweep {len(frame)} rows")
            rows_written += len(frame)
            pending = []

    def one(symbol: str):
        bars, source = fetch_minutes(symbol, em=em, tg=tg)
        if bars is None or bars.empty:
            return symbol, None, source, "无数据"
        part = bars[bars["trade_date"].astype(str) == str(day)]
        if part.empty:
            return symbol, None, source, "源没有这一天"
        return symbol, part, source, ""

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for symbol, part, source, why in pool.map(one, todo):
            if part is None:
                failed[symbol] = why
            else:
                pending.append(part)
                sources[source] = sources.get(source, 0) + 1
            if len(pending) >= chunk:
                flush()
    flush()
    return {"failed": failed, "sources": sources, "rows": rows_written}


def needs_fetch(have: dict, days: list[str], symbol: str, *,
                refresh: bool, want: str) -> bool:
    """This symbol needs a request: missing on some day, or not yet the wanted source.

    换源那一遍要能续跑——一次全市场二十分钟，中间断了，已经换过的四千只不该重抓。
    "有没有行"判不了这件事：换到一半的分区里两种源都在，所以比的是**来源集合**。
    """
    for day in days:
        sources = have.get(day, {}).get(symbol)
        if sources is None:
            return True
        if refresh and sources != {want}:
            return True
    return False


def sweep(desk, days: list[str], *, refresh: bool, limit: int, chunk: int,
          dry: bool, force: bool, source: str, heartbeat: float = 30.0,
          request_timeout: float = 120.0, progress_path: Path | None = None) -> dict:
    end = days[-1]
    frame = desk.market.daily(end, columns=["ts_code", "trade_date"])
    every = sorted({str(code) for code in frame["ts_code"]}) if frame is not None else []
    if not every:
        return {"days": days, "error": f"{end} 没有日线，先确认交易日"}

    have = coverage(desk, days)
    todo = [symbol for symbol in every
            if needs_fetch(have, days, symbol, refresh=refresh, want=source)]
    if limit:
        todo = todo[:limit]

    budget = Budget(BUDGET)
    plan = {
        "days": days, "end": end, "universe": len(every),
        "days_covered": {day: len(have[day]) for day in days},
        "todo": len(todo), "refresh": refresh, "source": source,
        "requests": len(todo), "budget_used_today": budget.used(),
        "budget_remaining": budget.remaining(),
        "est_rows": len(todo) * len(days) * 48,
    }
    plan["est_minutes"] = round(plan["est_rows"] / 520 / 60, 1)
    plan["heartbeat_seconds"] = heartbeat
    plan["request_timeout_seconds"] = request_timeout

    def checkpoint(payload: dict) -> None:
        if progress_path is None:
            return
        path = Path(progress_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    if dry or not todo:
        return plan

    if plan["requests"] > budget.remaining():
        return {**plan, "error": (f"需要 {plan['requests']} 次请求，今天只剩 "
                                  f"{budget.remaining()} 次（上限 {BS_USABLE}）。"
                                  f"减少 --days 或 --limit，或明天再跑")}

    if not force:
        ready = check_readiness(desk.market)
        if not ready.last_bar or str(end) > str(ready.last_bar):
            return {**plan, "error": (f"{end} 的日线还没落库（最新 {ready.last_bar or '无'}），"
                                      f"会话没结束；收盘后跑，或 --force")}

    started = time.time()
    if source == "http":
        outcome = _http_pass(desk, end, todo, workers=6, chunk=chunk * 48)
        return {**plan, **outcome, "seconds": round(time.time() - started, 1)}

    failed: dict[str, str] = {}
    sources: dict[str, int] = {}
    per_day: dict[str, int] = {}
    rows_written = 0
    writes = 0
    for start_at in range(0, len(todo), chunk):
        block = todo[start_at:start_at + chunk]
        block_end = start_at + len(block)
        print(f"  块 {start_at + 1}..{block_end}/{len(todo)} 开始；"
              f"预算已用 {budget.used()} 次", flush=True)
        last_beat = [time.monotonic()]

        def on_symbol(index: int, total: int, record: dict) -> None:
            if heartbeat <= 0:
                return
            now = time.monotonic()
            if index != total and now - last_beat[0] < heartbeat:
                return
            elapsed = now - last_beat[0]
            last_beat[0] = now
            print(f"    心跳 全局 {start_at + index}/{len(todo)}，块内 {index}/{total}，"
                  f"最近 {record['symbol']}={record['why']}，预算 {budget.used()}，"
                  f"间隔 {elapsed:.1f}s", flush=True)

        results = fetch_range(block, _dash(days[0]), _dash(days[-1]), budget=budget,
                              on_symbol=on_symbol, request_timeout=request_timeout)
        frames = []
        for record in results:
            if record["frame"] is None:
                failed[record["symbol"]] = record["why"]
            else:
                frames.append(record["frame"])
                sources["baostock"] = sources.get("baostock", 0) + 1
        if frames:
            merged = pd.concat(frames, ignore_index=True)
            for day, part in merged.groupby("trade_date"):
                desk.market.store.write("stk_mins", str(day), part, as_of=str(day),
                                        mode="merge",
                                        note=f"baostock sweep {len(part)} rows")
                per_day[str(day)] = per_day.get(str(day), 0) + len(part)
                rows_written += len(part)
                writes += 1
        done = min(start_at + chunk, len(todo))
        print(f"  {done}/{len(todo)}  写入 {rows_written} 行  "
              f"失败 {len(failed)}  请求 {budget.used()} 次", flush=True)
        checkpoint({
            **plan, "complete": False, "processed": done,
            "fetched": sources.get("baostock", 0), "failed_n": len(failed),
            "failed": dict(sorted(failed.items())[:40]), "rows": rows_written,
            "writes": writes, "requests_spent": budget.used() - plan["budget_used_today"],
            "seconds": round(time.time() - started, 1),
        })

    result = {
        **plan, "fetched": len(todo) - len(failed), "failed_n": len(failed),
        "failed": dict(sorted(failed.items())[:40]),
        "sources": sources, "rows": rows_written, "rows_per_day": per_day,
        "writes": writes, "requests_spent": budget.used() - plan["budget_used_today"],
        "seconds": round(time.time() - started, 1),
    }
    checkpoint({**result, "complete": True, "processed": len(todo)})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default="", help="结束日 YYYYMMDD；默认最新交易日")
    parser.add_argument("--days", type=int, default=1, help="从结束日往前共几个交易日")
    parser.add_argument("--source", choices=("baostock", "http"), default="baostock")
    parser.add_argument("--refresh", action="store_true", help="已有的也重抓（换源时用）")
    parser.add_argument("--limit", type=int, default=0, help="只抓前 N 只，用于试")
    parser.add_argument("--chunk", type=int, default=400, help="每多少只写一次分区")
    parser.add_argument("--heartbeat", type=float, default=30.0,
                        help="块内心跳秒数；0 关闭")
    parser.add_argument("--request-timeout", type=float, default=120.0,
                        help="baostock 阻塞 socket 超时秒数；0 关闭")
    parser.add_argument("--progress", default="",
                        help="块级断点 JSON；指定 --json 时默认是其 .progress.json")
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--force", action="store_true", help="盘中也要跑")
    parser.add_argument("--json", default="", help="清单写到哪")
    args = parser.parse_args(argv)

    desk = get_desk()
    end = args.day or desk.market.latest_bar_date() or ""
    if not end:
        print("没有可用的交易日", file=sys.stderr)
        return 2
    days = target_days(desk, end, args.days)
    if not days:
        print(f"{end} 取不到交易日列表", file=sys.stderr)
        return 2

    locked = False
    if not args.dry:
        if not _lock():
            print("已经有一个 sweep 在跑（data/baostock.lock）。baostock 不许并发，等它结束。",
                  file=sys.stderr)
            return 3
        locked = True
    try:
        progress_path = (Path(args.progress) if args.progress else
                         (Path(args.json).with_suffix(".progress.json") if args.json else None))
        result = sweep(desk, days, refresh=args.refresh, limit=args.limit,
                       chunk=args.chunk, dry=args.dry, force=args.force,
                       source=args.source, heartbeat=args.heartbeat,
                       request_timeout=args.request_timeout, progress_path=progress_path)
    finally:
        if locked:
            LOCK.unlink(missing_ok=True)

    if result.get("error"):
        print(result["error"], file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 1

    span = f"{days[0]}..{days[-1]}（{len(days)} 个交易日）"
    if args.dry or not result.get("todo"):
        print(f"{span}：全市场 {result['universe']} 只，待抓 {result['todo']}，"
              f"{result['requests']} 次请求（今天已用 {result['budget_used_today']}，"
              f"上限 {BS_USABLE}），约 {result['est_minutes']} 分钟 / {result['est_rows']} 行")
        print(f"  已有：{result['days_covered']}")
    else:
        print(f"{span}：抓 {result['fetched']} 只 / 计划 {result['todo']}，失败 "
              f"{result['failed_n']}，写 {result['rows']} 行（{result['writes']} 次分区写），"
              f"{result['seconds']} 秒，用掉 {result['requests_spent']} 次请求")
        if result["failed_n"]:
            print(f"  失败的（前 5）：{list(result['failed'].items())[:5]}", file=sys.stderr)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
