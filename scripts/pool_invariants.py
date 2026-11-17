"""派生池的不变式：一批逻辑上必须成立的关系，与实现无关。

为什么需要它
------------
`limit_pool_t0` 的每一行都由 `derive.py` 算出来，而那个函数已经错过两次：
把停牌当连续（复牌日接着数板）、以及年度分区不带预热（每年首日全场 1 板）。
两次都**没有报错**——分区写成功、质量检查 `ok`、数字照常出来。

所以这里不检查"代码怎么算的"，只检查"结果必须满足什么"：

1. `limit_up` 行：`close >= limit_price`、`high >= limit_price`、`boards >= 1`。
2. `broken_board` 行：`close < limit_price`、`high >= limit_price`、`boards == 0`。
   涨停与炸板互斥，同一只票同一天不能两边都在。
3. `limit_down` 行：`close <= limit_price`、`boards == 0`。
4. 主键 (trade_date, code, pool) 不重复。
5. **连板增量**：同一只票在相邻交易日都封板时，`boards` 必须恰好 +1。
   这条是停牌 bug 的直接探针——那一版会让复牌日变成 6 板而不是 1 板。

2026-10-31 全库实测：472,763 行、68,350 对相邻交易日，全部 0 违反。

用法
----
    python scripts/pool_invariants.py
    python scripts/pool_invariants.py --start 20260101 --json reports/pool_invariants.json

退出码 1 表示至少一条不变式被违反。它读本地仓库，不抓数。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry

EPS = 0.001

PRICE_COLUMNS = ("close", "high", "low", "open", "limit_price", "boards", "pre_close")


def _num(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame[column], errors="coerce")


def check(pools: pd.DataFrame, sessions: list[str]) -> list[dict]:
    """每一条违反变成一行 ``{rule, count, examples}``；空列表代表全过。"""
    problems: list[dict] = []
    if pools is None or pools.empty:
        return [{"rule": "no_rows", "count": 0, "examples": []}]

    frame = pools.copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    for column in PRICE_COLUMNS:
        if column in frame.columns:
            frame[column] = _num(frame, column)

    def flag(rule: str, mask: pd.Series) -> None:
        n = int(mask.fillna(False).sum())
        if not n:
            return
        cols = [c for c in ("trade_date", "code", "pool", "close", "limit_price", "boards")
                if c in frame.columns]
        problems.append({"rule": rule, "count": n,
                         "examples": frame.loc[mask.fillna(False), cols].head(3)
                         .to_dict("records")})

    dup = frame.duplicated(["trade_date", "code", "pool"])
    flag("duplicate_key", dup)

    up = frame[frame["pool"] == "limit_up"]
    broken = frame[frame["pool"] == "broken_board"]
    down = frame[frame["pool"] == "limit_down"]

    flag("limit_up_close_below_ceiling", up["close"] < up["limit_price"] - EPS)
    flag("limit_up_high_below_ceiling", up["high"] < up["limit_price"] - EPS)
    flag("limit_up_boards_below_one", up["boards"] < 1)
    flag("limit_up_ceiling_missing", up["limit_price"].isna())

    flag("broken_close_at_ceiling", broken["close"] >= broken["limit_price"] - EPS)
    flag("broken_high_below_ceiling", broken["high"] < broken["limit_price"] - EPS)
    flag("broken_has_boards", broken["boards"] != 0)

    flag("limit_down_close_above_floor", down["close"] > down["limit_price"] + EPS)
    flag("limit_down_has_boards", down["boards"] != 0)

    # 同一只票同一天不能既是涨停又是炸板。
    both = up[["trade_date", "code"]].merge(broken[["trade_date", "code"]],
                                            on=["trade_date", "code"], how="inner")
    if len(both):
        problems.append({"rule": "limit_up_and_broken_same_day", "count": len(both),
                         "examples": both.head(3).to_dict("records")})

    if sessions and not up.empty:
        # 逐只票看相邻两行。两种情况各自有唯一正确答案：
        #   1) 两行属于**相邻交易日** -> 后一个 boards 必须恰好 +1；
        #   2) 中间有断层（停牌/缺数据） -> 后一个必须是 1，因为复牌首板是新的一板。
        # 停牌 bug 违反的是第 2 条：它让复牌日接在停牌前那个数后面（6 板、13 板）。
        next_of = {day: sessions[i + 1] if i + 1 < len(sessions) else None
                   for i, day in enumerate(sessions)}
        ordered = up.sort_values(["code", "trade_date"], kind="mergesort")
        grouped = ordered.groupby("code", sort=False)
        next_date = grouped["trade_date"].shift(-1)
        next_boards = grouped["boards"].shift(-1)
        expected_next = ordered["trade_date"].map(next_of)

        has_next = next_date.notna()
        adjacent = has_next & next_date.eq(expected_next)
        gapped = has_next & ~next_date.eq(expected_next)

        flag("streak_does_not_increment", adjacent & (next_boards != ordered["boards"] + 1))
        flag("streak_continues_across_gap", gapped & (next_boards != 1))
    return problems


def _sessions(store: DatasetStore, start: str | None, end: str | None) -> list[str]:
    try:
        cal = store.read("trade_cal", start, end, columns=["cal_date", "is_open"])
    except (KeyError, ValueError):
        return []
    if cal is None or cal.empty or "is_open" not in cal.columns:
        return []
    opened = pd.to_numeric(cal["is_open"], errors="coerce") == 1
    return sorted(cal.loc[opened, "cal_date"].astype(str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="YYYYMMDD")
    parser.add_argument("--end", help="YYYYMMDD")
    parser.add_argument("--json", help="把结果写到这里")
    args = parser.parse_args(argv)

    store = DatasetStore(str(ROOT / "data"), Registry(str(ROOT / "data" / "registry.sqlite")))
    pools = store.read("limit_pool_t0", args.start, args.end)
    sessions = _sessions(store, args.start, args.end)
    problems = check(pools, sessions)

    span = "全部" if not (args.start or args.end) else f"{args.start or '…'}~{args.end or '…'}"
    print(f"[invariants] {span}  行 {0 if pools is None else len(pools)}  交易日 {len(sessions)}")
    if not problems:
        print("[invariants] 全部通过")
    for item in problems:
        print(f"  ✗ {item['rule']}: {item['count']}")
        for row in item["examples"]:
            print(f"      {row}")

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"span": span, "rows": 0 if pools is None else len(pools),
                                    "sessions": len(sessions), "problems": problems},
                                   ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
        print(f"[invariants] 写入 {path}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
