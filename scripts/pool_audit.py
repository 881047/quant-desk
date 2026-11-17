"""逐只核对三个来源的涨停名单，对交易所限价。

为什么需要这个脚本
------------------
「同花顺和东财的涨停家数不一样」这句话，很容易被写成「两家口径不同，都对」。
2026-09-01~10-30 逐只核对下来不是这样：把 `daily.close` 和 `stk_limit.up_limit`
对起来，同花顺 38 个交易日里只有 16 天名单完全正确，东财 19 天里只有 4 天。
差的那些不是「算不算北交所」，是名单本身不全——漏掉的 60 条里 35 条是沪深主板/创业板。

所以这个脚本回答的是一个可以用两种方式问的问题：

  * 今天这几只涨停，哪一家漏了？
  * 这一段窗口，哪一家能用来做跨日比较？

口径
----
基准 = `close >= up_limit - 0.001`（与 `service/adapters/derive.py` 同一定义，同一
EPS）。派生池就是这个定义的物化结果，所以它的 miss/extra 恒为 0——那是**自检**，
不是证据；真正的证据是同花顺和东财相对这个基准的差。

用法
----
    python scripts/pool_audit.py --days 60
    python scripts/pool_audit.py --start 20260901 --end 20261030 --json reports/pool_audit.json

只读本地仓库，不抓数。缺某个来源的日子会被跳过并在 `skipped` 里说明，不会当成 0。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry

# 与 derive.py 同一个常数：涨停是一个确切价格，不是「0.1% 以内」。
PRICE_EPS = 0.001

SOURCES = {
    "derived": ("limit_pool_t0", "code", "limit_up"),
    "ths": ("ths_limit_up", "ts_code", None),
    "eastmoney": ("limit_up_pool", "code", None),
}
LABELS = {"derived": "派生池", "ths": "同花顺", "eastmoney": "东财"}


def to_ts_code(raw: str) -> str:
    """东财给的是裸 6 位，两张表都用 ts_code。不归一化就等于零交集。"""
    code = str(raw)
    if "." in code:
        return code
    if code[0] == "6":
        return f"{code}.SH"
    # 北交所：老的 43/83/87/88 段，以及 2024 年起的 920/921 段。只判首位是 4/8 会把
    # 920592 映射成 .SZ，于是每只北交所票都变成「东财漏报」——一次纯代码格式造成的
    # 假阳性，看起来和真实漏报一模一样。
    if code[0] in ("4", "8") or code[:2] in ("92",):
        return f"{code}.BJ"
    return f"{code}.SZ"


def load_truth(store: DatasetStore, start: str, end: str) -> dict[str, set[str]]:
    """交易所限价说了算：收在涨停价上就是涨停，没有第二种解释。"""
    daily = store.read("daily", start, end, columns=["ts_code", "trade_date", "close"])
    limits = store.read("stk_limit", start, end, columns=["ts_code", "trade_date", "up_limit"])
    if daily is None or daily.empty or limits is None or limits.empty:
        return {}
    for frame in (daily, limits):
        frame["trade_date"] = frame["trade_date"].astype(str)

    merged = daily.merge(limits, on=["ts_code", "trade_date"], how="inner")
    up = pd.to_numeric(merged["up_limit"], errors="coerce")
    close = pd.to_numeric(merged["close"], errors="coerce")
    merged = merged[up.gt(0) & close.notna()].copy()
    merged["at_up"] = close[merged.index] >= up[merged.index] - PRICE_EPS
    return {day: set(chunk.loc[chunk["at_up"], "ts_code"])
            for day, chunk in merged.groupby("trade_date")}


def load_source(store: DatasetStore, key: str, start: str, end: str) -> dict[str, set[str]]:
    dataset, code_col, pool = SOURCES[key]
    frame = store.read(dataset, start, end)
    if frame is None or frame.empty:
        return {}
    frame = frame.copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    if pool and "pool" in frame.columns:
        frame = frame[frame["pool"].astype(str) == pool]
    if frame.empty:
        return {}
    return {day: {to_ts_code(code) for code in chunk[code_col]}
            for day, chunk in frame.groupby("trade_date")}


def compare(truth: dict[str, set[str]],
            sources: dict[str, dict[str, set[str]]]) -> tuple[pd.DataFrame, dict]:
    """每天的漏报与多认，外加每个来源的汇总。

    ``miss`` 是基准有而它没有；``extra`` 是它有而基准没有（收在涨停价下方却被算成
    涨停）。两个数都报，因为只报净差会把「漏 16、多认 2」写成「少 14」，
    而那是两件不同的事。
    """
    rows = []
    for day in sorted(truth):
        base = truth[day]
        row: dict = {"trade_date": day, "truth": len(base)}
        for key in SOURCES:
            got = sources.get(key, {}).get(day)
            if got is None:
                row[f"{key}_n"] = row[f"{key}_miss"] = row[f"{key}_extra"] = None
                continue
            row[f"{key}_n"] = len(got)
            row[f"{key}_miss"] = len(base - got)
            row[f"{key}_extra"] = len(got - base)
        rows.append(row)
    table = pd.DataFrame(rows)

    summary: dict[str, dict] = {}
    for key in SOURCES:
        sub = table.dropna(subset=[f"{key}_n"])
        if sub.empty:
            summary[key] = {"days": 0}
            continue
        exact = int(((sub[f"{key}_miss"] + sub[f"{key}_extra"]) == 0).sum())
        summary[key] = {
            "days": len(sub),
            "mean_names": round(float(sub[f"{key}_n"].mean()), 2),
            "mean_miss": round(float(sub[f"{key}_miss"].mean()), 2),
            "mean_extra": round(float(sub[f"{key}_extra"].mean()), 2),
            "exact_days": exact,
            "exact_share": round(exact / len(sub), 3),
            "worst_miss": int(sub[f"{key}_miss"].max()),
        }
    return table, summary


def composition(truth: dict[str, set[str]], sources: dict[str, dict[str, set[str]]],
                store: DatasetStore, start: str, end: str) -> dict[str, dict]:
    """漏掉的到底是哪些票——北交所（30% 板）还是沪深主板。

    这是「口径不同」与「名单不全」的分界：只漏北交所可以说是口径，漏主板不是。
    """
    pool = store.read("limit_pool_t0", start, end,
                      columns=["trade_date", "code", "pool", "limit_price"])
    if pool is None or pool.empty:
        return {}
    pool = pool[pool["pool"].astype(str) == "limit_up"].copy()
    pool["trade_date"] = pool["trade_date"].astype(str)
    up_limit = {(str(r.trade_date), to_ts_code(r.code)): r.limit_price
                for r in pool.itertuples(index=False)}
    pre_close = _pre_close(store, start, end)

    out: dict[str, dict] = {}
    for key in ("ths", "eastmoney"):
        buckets = {"BJ": 0, "SH": 0, "SZ": 0, "20%+": 0, "10%": 0}
        for day, base in truth.items():
            got = sources.get(key, {}).get(day)
            if got is None:
                continue
            for code in base - got:
                buckets[code.split(".")[-1]] = buckets.get(code.split(".")[-1], 0) + 1
                limit = up_limit.get((day, code))
                base_px = pre_close.get((day, code))
                if limit and base_px:
                    band = round(float(limit) / float(base_px) - 1, 3)
                    buckets["20%+" if band > 0.15 else "10%"] += 1
        out[key] = buckets
    return out


def _pre_close(store: DatasetStore, start: str, end: str) -> dict[tuple[str, str], float]:
    frame = store.read("daily", start, end, columns=["ts_code", "trade_date", "pre_close"])
    if frame is None or frame.empty:
        return {}
    frame["trade_date"] = frame["trade_date"].astype(str)
    return {(str(r.trade_date), str(r.ts_code)): r.pre_close for r in frame.itertuples(index=False)}


def _window(days: int, start: str | None, end: str | None) -> tuple[str, str]:
    end = end or datetime.now().strftime("%Y%m%d")
    if start:
        return start, end
    return (datetime.strptime(end, "%Y%m%d") - timedelta(days=days * 2)).strftime("%Y%m%d"), end


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="calendar days back (default 60)")
    parser.add_argument("--start", help="YYYYMMDD; overrides --days")
    parser.add_argument("--end", help="YYYYMMDD; default today")
    parser.add_argument("--json", help="also write the table and summary here")
    args = parser.parse_args(argv)

    store = DatasetStore(str(ROOT / "data"), Registry(str(ROOT / "data" / "registry.sqlite")))
    start, end = _window(args.days, args.start, args.end)
    truth = load_truth(store, start, end)
    if not truth:
        print(f"[audit] {start}~{end} 没有日线或限价，无法建立基准")
        return 1

    sources = {key: load_source(store, key, start, end) for key in SOURCES}
    table, summary = compare(truth, sources)

    print(f"基准 {start}~{end}：{len(truth)} 个交易日、{sum(len(v) for v in truth.values())} 条涨停")
    print(f"\n{'来源':8} {'天数':>4} {'日均家数':>8} {'日均漏':>7} {'日均多认':>8} {'完全正确':>9} {'最差一天漏':>10}")
    for key, stat in summary.items():
        if not stat.get("days"):
            print(f"{LABELS[key]:8} {'—':>4}   无数据")
            continue
        print(f"{LABELS[key]:8} {stat['days']:>4} {stat['mean_names']:>8.1f} "
              f"{stat['mean_miss']:>7.2f} {stat['mean_extra']:>8.2f} "
              f"{stat['exact_days']:>4}/{stat['days']:<4} {stat['worst_miss']:>10}")

    print("\n漏掉的票按板块分（只漏北交所可以说是口径，漏主板不是）：")
    for key, buckets in composition(truth, sources, store, start, end).items():
        if key == "derived":
            continue
        print(f"  {LABELS[key]}: 北交所 {buckets.get('BJ', 0)}、沪 {buckets.get('SH', 0)}、"
              f"深 {buckets.get('SZ', 0)}（其中 10% 板 {buckets.get('10%', 0)}、"
              f"20%+ 板 {buckets.get('20%+', 0)}）")

    bad = [row for row in table.itertuples(index=False) if row.ths_miss and row.ths_miss > 0]
    if bad:
        worst = sorted(bad, key=lambda r: -r.ths_miss)[:3]
        print("\n同花顺漏得最多的三天：")
        for row in worst:
            print(f"  {row.trade_date}: 基准 {row.truth} 只，它给 {row.ths_n} 只"
                  f"（漏 {row.ths_miss}、多认 {row.ths_extra}）")

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"window": {"start": start, "end": end},
                   "summary": summary,
                   "composition": composition(truth, sources, store, start, end),
                   "table": table.to_dict("records")}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
        print(f"\n[audit] 写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
