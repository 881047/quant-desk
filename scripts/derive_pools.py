"""Build the T0 pool history from stored daily bars + limit prices.

    python scripts/derive_pools.py --start 20230101 --end 20261024

Writes ``limit_pool_t0`` (partition = year), then prints a per-year summary and
the raw next-day premium by board height. The premium is research output, not a
trading signal: it ignores fills, costs and the fact that a sealed limit-up
cannot be bought.

**Warm-up matters.** ``boards`` is a running streak: it counts how many
consecutive sessions the close sat at the up-limit. Computing a partition from
its own rows alone therefore starts every stock at 1 — which is correct only if
nothing was sealed the day before. A one-day run on 2026-10-28 produced
``max_boards=1, first_board_share=1.0`` for a session whose ladder actually
reached five, and every downstream reader treated "no streak" as a fact.

So the streak is computed once over the *whole* read window and only then split
into years for writing. ``--warmup-days`` adds read-only history before
``--start`` so a mid-year rebuild still sees the streak it inherited.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service.adapters.derive import derive_pools, pool_counts, zt_next_day_stats
from service.adapters.parquet_store import DatasetStore
from service.adapters.quality import evaluate
from service.adapters.sqlite_store import Registry

COLS = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"]

# Upper bound on warm-up extensions. Each round doubles the read window, so eight
# rounds reach back roughly twenty years — past the start of the archive. The cap
# exists so a streak that never resets cannot turn a rebuild into a full-history
# scan repeated N times.
MAX_WARMUP_ROUNDS = 8

# Sessions of look-back that are always enough to resolve a streak. The longest
# streak in the stored archive is 22 sessions; 30 leaves room for a longer one
# without buying the extra reads a larger window costs.
MAX_STREAK_LOOKBACK = 30


def _shift(yyyymmdd: str, days: int) -> str:
    stamp = datetime.strptime(yyyymmdd, "%Y%m%d") + timedelta(days=days)
    return stamp.strftime("%Y%m%d")


def _missing_sessions(store, start: str, end: str, have: set[str]) -> list[str]:
    """区间内开盘、但数据里没有的交易日。日历读不到时返回空（不猜）。"""
    try:
        cal = store.read("trade_cal", start, end, columns=["cal_date", "is_open"])
    except (KeyError, ValueError):
        return []
    if cal is None or cal.empty or "is_open" not in cal.columns:
        return []
    opened = pd.to_numeric(cal["is_open"], errors="coerce") == 1
    days = sorted(cal.loc[opened, "cal_date"].astype(str))
    return [day for day in days if day not in have]


def _warmup_incomplete(daily: pd.DataFrame, limits: pd.DataFrame, target_start: str) -> bool:
    """Whether a streak still has no reset inside the warm-up window.

    Only the names sealed *on the first target session* matter: they are the ones
    whose count would restart at 1 if the window began there. For each of them the
    question is whether every prior session in the window was also sealed — if so
    the window boundary is cutting a streak in half, and reading further back is
    the only way to count it. If even one session before it was a normal day, the
    streak started inside the window and more history cannot change the answer.

    Two things the earlier version got wrong. It collected candidates from *every*
    sealed session at or after the target date, so a short-lived listing that was
    sealed throughout — a five-day history of five limit-ups — asked for more
    history on every round, and the answer never changed; a rebuild walked back to
    the start of the archive one doubling at a time. And it did that per symbol
    with a full-frame boolean mask, which on a twenty-year read is 15 M rows times
    a few hundred names, on every call. Both are one ``groupby`` on one session's
    worth of candidates.
    """
    columns = ["ts_code", "trade_date", "close"]
    frame = daily[columns].merge(
        limits[["ts_code", "trade_date", "up_limit"]],
        on=["ts_code", "trade_date"],
        how="inner",
    )
    if frame.empty:
        return False
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["at_up"] = (
        pd.to_numeric(frame["close"], errors="coerce")
        >= pd.to_numeric(frame["up_limit"], errors="coerce") - 0.001
    ).fillna(False)

    days = sorted(frame["trade_date"].unique())
    prior_days = [d for d in days if d < target_start]
    # Enough history already. A streak is bounded in practice — the longest in
    # the stored archive is 22 sessions — so 30 sessions of look-back resolves
    # any of them, and asking for more cannot change a single `boards` value.
    # Without this the loop below doubled until it hit the round cap.
    if len(prior_days) >= MAX_STREAK_LOOKBACK:
        return False
    first = next((d for d in days if d >= target_start), None)
    if first is None or not prior_days:
        return False

    sealed = frame[frame["at_up"]]
    candidates = sealed.loc[sealed["trade_date"] == first, "ts_code"].drop_duplicates()
    if candidates.empty:
        return False
    prior = frame[frame["trade_date"] < target_start]
    never_reset = prior.groupby("ts_code")["at_up"].all()
    # A code absent from ``prior`` listed on or after the target date: its first
    # session is a first board, not a cut streak.
    return bool(never_reset.reindex(candidates).fillna(False).any())


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="derive T0 pools")
    parser.add_argument("--start", default="20230101", help="first date to write")
    parser.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--warmup-days", type=int, default=0,
                        help="calendar days read before --start so streak counts "
                             "inherit the prior sessions (read-only, never written)")
    parser.add_argument("--stats", action="store_true", help="print next-day premium stats")
    parser.add_argument(
        "--replace", action="store_true",
        help="rewrite the dates you cover instead of merging into what is stored. "
             "Needed after a code or source fix: `merge` can only add rows, so a "
             "row that is no longer a limit-up stays in the partition forever. "
             "Dates outside --start..--end are still carried over.")
    parser.add_argument(
        "--verify", action="store_true",
        help="compare the stored partitions with a fresh derivation and report "
             "drift; writes nothing. Exit code 1 means at least one partition "
             "no longer matches its inputs.")
    args = parser.parse_args(argv)

    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)

    read_start = _shift(args.start, -abs(args.warmup_days)) if args.warmup_days else args.start
    daily_all = store.read("daily", read_start, args.end, columns=COLS)
    limits_all = store.read("stk_limit", read_start, args.end,
                            columns=["ts_code", "trade_date", "up_limit", "down_limit"])
    if daily_all.empty or limits_all.empty:
        print("error: daily/stk_limit missing; run backfill first", file=sys.stderr)
        return 2
    daily_all["trade_date"] = daily_all["trade_date"].astype(str)
    limits_all["trade_date"] = limits_all["trade_date"].astype(str)

    # A fixed warmup is only an optimization, never a correctness boundary. If
    # a limit-up chain reaches the requested start without a normal-day reset,
    # extend just until that chain closes (or the available history begins).
    step = max(1, abs(args.warmup_days))
    rounds = 0
    while args.warmup_days and _warmup_incomplete(daily_all, limits_all, args.start):
        rounds += 1
        if rounds > MAX_WARMUP_ROUNDS:
            # Bounded for the same reason the read is: a suspect streak (a name
            # whose close sits on its limit every session, which does happen in
            # vendor data) must not send a rebuild through the whole archive one
            # doubling at a time. Whatever we have is enough to start a streak
            # count; the alternative is a job that never returns.
            print(f"[warmup] {MAX_WARMUP_ROUNDS} extensions without a reset; "
                  f"starting from {read_start} anyway")
            break
        previous_earliest = str(daily_all["trade_date"].min())
        read_start = _shift(read_start, -step)
        daily_all = store.read("daily", read_start, args.end, columns=COLS)
        limits_all = store.read(
            "stk_limit", read_start, args.end,
            columns=["ts_code", "trade_date", "up_limit", "down_limit"],
        )
        if daily_all.empty or limits_all.empty:
            print("error: daily/stk_limit missing while extending warmup", file=sys.stderr)
            return 2
        daily_all["trade_date"] = daily_all["trade_date"].astype(str)
        limits_all["trade_date"] = limits_all["trade_date"].astype(str)
        earliest = str(daily_all["trade_date"].min())
        print(f"[warmup] open streak: extended read start to {read_start}")
        if earliest >= previous_earliest:
            break
        step *= 2

    # 输入缺天的时候，派生池会**安静地**少掉那一天：分区照样写成功，家数照样能读，
    # 只是那一天的数字退回了厂商口径。所以这里把缺的日期点出来，并且当**目标日**
    # 缺输入时用独立退出码结束——日更把它记成 derived_pools 失败，gate 变红。
    last_gap = False
    for label, frame in (("daily", daily_all), ("stk_limit", limits_all)):
        have = set(frame["trade_date"].astype(str))
        gap = _missing_sessions(store, args.start, args.end, have)
        if not gap:
            continue
        shown = "、".join(gap[:5]) + ("…" if len(gap) > 5 else "")
        print(f"[gap] {label} 缺 {len(gap)} 个交易日：{shown}（这些天不会进派生池）",
              file=sys.stderr)
        if args.end in gap:
            last_gap = True

    names = store.read("stock_basic", columns=["ts_code", "name"])

    if read_start != args.start:
        print(f"[warmup] reading from {read_start}, writing from {args.start}")

    # One pass over the whole window: a streak that crosses a year boundary is
    # still one streak. Splitting first would reset it at every 1 January.
    # 日历取 ``daily_all`` 而不是合并后的帧：`stk_limit` 少一天不该让那天
    # 从日历上消失，否则两边的连板会被接起来（停牌已经被这个函数当作断板了）。
    pools_all = derive_pools(daily_all, limits_all, names,
                             calendar_days=sorted(daily_all["trade_date"].unique()))
    if pools_all.empty:
        print("error: no pools derived", file=sys.stderr)
        return 2
    pools_all["trade_date"] = pools_all["trade_date"].astype(str)

    # Warm-up rows affect streaks but are never write targets. Merge the target
    # dates into each yearly partition so a one-day run cannot erase the year.
    target = pools_all[pools_all["trade_date"].between(args.start, args.end)]

    if args.verify:
        # Compare against what is stored without touching it. The pool is derived
        # from `daily` + `stk_limit`, both of which get revised; a partition built
        # before such a revision keeps whatever the older inputs said, and nothing
        # reads the difference. This is how ~1,000 rows that are not limit-ups
        # accumulated in 2023-2026 while every quality check reported `ok`.
        drifted = 0
        for year in sorted(target["trade_date"].str[:4].unique()):
            pools = target[target["trade_date"].str[:4] == year]
            held = store.read("limit_pool_t0", f"{year}0101", f"{year}1231")
            if held.empty:
                print(f"[{year}] stored partition is empty (derive has never run)")
                drifted += 1
                continue
            held["trade_date"] = held["trade_date"].astype(str)
            held = held[held["trade_date"].isin(set(pools["trade_date"]))]

            def key(frame: pd.DataFrame) -> set[tuple[str, str, str, int]]:
                return {(str(t), str(c), str(p), int(b)) for t, c, p, b in
                        zip(frame["trade_date"], frame["code"], frame["pool"],
                            frame["boards"].fillna(0))}

            fresh_k, held_k = key(pools), key(held)
            stale = held_k - fresh_k
            missing = fresh_k - held_k
            if not stale and not missing:
                print(f"[{year}] ok  rows={len(fresh_k)}")
                continue
            drifted += 1
            print(f"[{year}] DRIFT  stored={len(held_k)} derived={len(fresh_k)} "
                  f"stale={len(stale)} missing={len(missing)}")
            for row in sorted(stale)[:3]:
                print(f"    stale   {row}")
            for row in sorted(missing)[:3]:
                print(f"    missing {row}")
        print(f"[verify] {drifted} partition(s) differ from a fresh derivation")
        return 0 if drifted == 0 else 1

    if last_gap:
        print(f"error: {args.end} 是交易日，但 daily/stk_limit 缺这一天；"
              f"不写分区（写了也只会是空的那一天）", file=sys.stderr)
        return 3

    total = 0
    for year in sorted(target["trade_date"].str[:4].unique()):
        pools = target[target["trade_date"].str[:4] == year]
        d = daily_all[daily_all["trade_date"].str[:4] == year]
        mode = "merge"
        if args.replace:
            # Only sessions *outside* the requested range are carried over; every
            # date this run re-derived is replaced outright, which is what lets a
            # row that is no longer a limit-up disappear. Comparing date strings
            # through a set was the wrong instrument here — one type or format
            # mismatch and every stored row counts as "outside", so the partition
            # becomes old rows plus new rows and a rebuild cannot remove anything.
            held = store.read("limit_pool_t0", f"{year}0101", f"{year}1231")
            if not held.empty:
                held["trade_date"] = held["trade_date"].astype(str)
                outside = held[(held["trade_date"] < args.start)
                               | (held["trade_date"] > args.end)]
                if not outside.empty:
                    pools = pd.concat([outside, pools], ignore_index=True)
            mode = "replace"
        rec = store.write("limit_pool_t0", year, pools,
                          as_of=str(target[target["trade_date"].str[:4] == year]
                                    ["trade_date"].max()),
                          mode=mode)
        q = evaluate("limit_pool_t0", year, pools)
        registry.record_quality("limit_pool_t0", year, q.status, q.rows,
                                [i.as_dict() for i in q.issues])
        total += 0 if rec is None else rec.rows
        counts = pool_counts(pools)
        up = pools[pools["pool"] == "limit_up"]
        peak = int(up["boards"].max()) if not up.empty else 0
        days = pools["trade_date"].nunique()
        print(f"[{year}] rows={len(pools):>6} days={days:>3} max_boards={peak:>2} "
              f"{counts} status={q.status}")

        if args.stats:
            stats = zt_next_day_stats(d, pools)
            stats = stats.dropna(subset=["open_premium"])
            if not stats.empty:
                by_board = stats.groupby("boards")["open_premium"].agg(["count", "mean", "median"])
                by_board = by_board[by_board["count"] >= 20]
                print(f"  next-open premium by boards ({year}):")
                print((by_board * [1, 100, 100]).round(2).to_string())

    print(f"[done] limit_pool_t0 rows={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
