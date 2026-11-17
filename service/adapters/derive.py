"""Derive limit-up / broken-board / limit-down pools from T0 bars.

Why this exists
---------------
EastMoney's ``getTopicZTPool`` only serves roughly the last 10-15 trading days;
asking for 2024 returns ``data: null``. Depending on it alone would leave the
aggressive sleeve with two weeks of history.

We already store the two authoritative inputs: unadjusted daily bars and the
exchange limit prices (``stk_limit``). From those we can derive, for every
trading day since 2023:

- ``limit_up``: close at the upper limit
- ``broken_board``: high touched the upper limit but close did not
- ``limit_down``: close at the lower limit
- ``boards``: consecutive limit-up streak (连板数)，只对已封住的板有意义
- ``prev_boards``: 上一交易日的连板数。炸板行的 ``boards`` 是 0，但"几进几"
  要看 ``prev_boards + 1``——身位就是这么比的

This is deterministic, T0-grade and complete. It lacks seal amount / first-seal
time, which only a live snapshot can provide — EastMoney remains the live
enrichment source, not the historical backbone.
"""

from __future__ import annotations

import pandas as pd

from service.kernel.units import PRICE_EPS as _PRICE_EPS_DEC

# A limit-up is an exact price, not "within 0.1%": stocks that close one cent
# below the limit (差一分钱未封板) must not enter the pool. EastMoney agrees.
PRICE_EPS = float(_PRICE_EPS_DEC)

POOL_UP = "limit_up"
POOL_BROKEN = "broken_board"
POOL_DOWN = "limit_down"


def derive_pools(
    daily: pd.DataFrame,
    limits: pd.DataFrame,
    names: pd.DataFrame | None = None,
    *,
    calendar_days: list[str] | set[str] | None = None,
) -> pd.DataFrame:
    cols = ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"]
    if daily.empty or limits.empty:
        return pd.DataFrame(columns=[*cols, "code", "name", "pool", "limit_price",
                                     "boards", "prev_boards"])

    d = daily[[c for c in cols if c in daily.columns]].copy()
    lim = limits[["ts_code", "trade_date", "up_limit", "down_limit"]].copy()
    df = d.merge(lim, on=["ts_code", "trade_date"], how="inner")
    if df.empty:
        return pd.DataFrame()

    up = pd.to_numeric(df["up_limit"], errors="coerce")
    down = pd.to_numeric(df["down_limit"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    valid = up.gt(0) & down.gt(0) & close.gt(0)
    df = df[valid].copy()
    up, down, close, high = up[valid], down[valid], close[valid], high[valid]

    # Positional from here on. `df[valid]` keeps the *original* labels, so the
    # frame carries a scattered index while the masks below are built from it.
    # Any later `merge` renumbers to 0..n-1 — and the join of `names` did exactly
    # that — after which `mask.reindex(df.index)` matched a mask row to whatever
    # row happened to hold that integer label. The result was a pool whose members
    # and `limit_price` belonged to different stocks: a Beijing name listed with
    # another stock's ceiling, and a `boards` count that read 0 for rows that WERE
    # limit-ups. Re-labelling once, here, makes every index in this function the
    # same index.
    df = df.reset_index(drop=True)
    up = up.reset_index(drop=True)
    down = down.reset_index(drop=True)
    close = close.reset_index(drop=True)
    high = high.reset_index(drop=True)

    at_up = close >= up - PRICE_EPS
    at_down = close <= down + PRICE_EPS
    touched_up = high >= up - PRICE_EPS

    # Consecutive limit-up streak: consecutive **sessions**, reset by any
    # non-limit day *or any missing session*.
    #
    # The row-based version counted one row per session and therefore joined a
    # streak across a suspension: 002309.SZ was sealed 2024-11-04..11-08, halted
    # for two months, and reopened at the cap on 2025-01-09 — which the stored
    # pool called its *sixth* board and carried on to eight. A resumption day is a
    # 复牌首板, never the continuation of a ladder, and every reader downstream
    # (max_boards, promotion rate, the sentiment label) took that number at face
    # value.
    #
    # ``calendar_days`` is the market's session list. Deriving it from ``df``
    # alone is enough in production — the frame is the whole market — but a
    # single-name call would then see only that name's rows and re-introduce the
    # bug, so the caller can pass the real calendar.
    sessions = sorted({str(d) for d in (calendar_days if calendar_days is not None
                                        else df["trade_date"])})
    previous_of = {day: (sessions[i - 1] if i else None) for i, day in enumerate(sessions)}

    ordered = df.sort_values(["ts_code", "trade_date"], kind="mergesort").copy()
    hit = (close.reindex(ordered.index) >= up.reindex(ordered.index) - PRICE_EPS).astype(int)
    prior = ordered.groupby("ts_code")["trade_date"].shift(1)
    expected = ordered["trade_date"].map(previous_of)
    contiguous = prior.astype("string").eq(expected.astype("string")).fillna(False).astype(bool)
    reset = (hit == 0) | ~contiguous
    block = reset.groupby(ordered["ts_code"]).cumsum()
    ordered["_boards"] = hit.groupby([ordered["ts_code"], block]).cumsum()
    boards = ordered["_boards"].reindex(df.index).fillna(0).astype(int)

    # ``prev_boards``：上一交易日的连板数。炸板行自己的 ``boards`` 恒为 0
    # （没有"完成"的连板），但它"今天是几进几"仍是一个有意义的数——游资看
    # 身位看的就是这个。没有这一列，「3 板的回封率比 1 板高多少」算不出来，
    # 而这正是打板唯一能优化的东西。
    #
    # 不把 ``boards`` 复写成"尝试高度"：同一个字段两种含义，读的人迟早会
    # 把炸板行当成连板行（2026-11-02 那次 `quality` 一个字段三种含义就是
    # 这么来的）。``boards`` 继续只表示已完成的连板。
    #
    # 停牌那一格不算"上一交易日"：``contiguous`` 与连板重置用的是同一条判据，
    # 否则复牌前一天会被当成身位。
    prior_streak = ordered.groupby("ts_code")["_boards"].shift(1)
    prior_streak = prior_streak.where(contiguous, 0).fillna(0).astype(int)
    df["prev_boards"] = prior_streak.reindex(df.index).fillna(0).astype(int)

    df["boards"] = boards
    df["pct_chg"] = (close / pd.to_numeric(df["pre_close"], errors="coerce") - 1) * 100
    df["code"] = df["ts_code"]
    if names is not None and not names.empty:
        df = df.merge(names[["ts_code", "name"]], on="ts_code", how="left")
    else:
        df["name"] = ""
    df["name"] = df["name"].fillna("")

    frames = []
    for pool, mask, price in (
        (POOL_UP, at_up, up),
        (POOL_BROKEN, touched_up & ~at_up, up),
        (POOL_DOWN, at_down, down),
    ):
        sub = df[mask.reindex(df.index, fill_value=False)].copy()
        if sub.empty:
            continue
        sub["pool"] = pool
        sub["limit_price"] = price.reindex(sub.index)
        if pool != POOL_UP:
            sub["boards"] = 0
        frames.append(sub)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    keep = [
        "trade_date", "code", "name", "pool", "open", "high", "low", "close",
        "pre_close", "pct_chg", "limit_price", "boards", "prev_boards", "amount", "vol",
    ]
    out = out[[c for c in keep if c in out.columns]]
    out["source"] = "derived:tushare"
    out = out.sort_values(["trade_date", "pool", "code"], kind="mergesort").reset_index(drop=True)
    return out


def pool_counts(df: pd.DataFrame) -> dict[str, int]:
    if df.empty:
        return {}
    return {str(k): int(v) for k, v in df.groupby("pool").size().items()}


def zt_next_day_stats(
    daily: pd.DataFrame,
    pools: pd.DataFrame,
    *,
    calendar_days: set[str] | None = None,
) -> pd.DataFrame:
    """Attach next-session open/close returns to limit-up rows.

    ``next_pre_close`` is the exchange-adjusted basis on an ex-corporate-action
    day; the pool's raw close is not a valid denominator there. A bar after a
    suspension is also not the next trading session, so its returns stay missing.
    """
    zt = pools[pools["pool"] == POOL_UP] if not pools.empty else pd.DataFrame()
    if zt.empty:
        return pd.DataFrame()
    px = daily[["ts_code", "trade_date", "open", "close", "pre_close"]].copy()
    px["trade_date"] = px["trade_date"].astype(str)
    px = px.sort_values(["ts_code", "trade_date"], kind="mergesort")
    grouped = px.groupby("ts_code")
    px["next_trade_date"] = grouped["trade_date"].shift(-1)
    px["next_open"] = grouped["open"].shift(-1)
    px["next_close"] = grouped["close"].shift(-1)
    px["next_pre_close"] = grouped["pre_close"].shift(-1)

    sessions = sorted(calendar_days or set(px["trade_date"]))
    next_session = {day: sessions[i + 1] if i + 1 < len(sessions) else None
                    for i, day in enumerate(sessions)}
    px["expected_next_trade_date"] = px["trade_date"].map(next_session)
    is_next_session = px["next_trade_date"].eq(px["expected_next_trade_date"])
    return_cols = ["next_open", "next_close", "next_pre_close"]
    px.loc[~is_next_session, return_cols] = float("nan")

    merged = zt.merge(
        px[["ts_code", "trade_date", "next_trade_date", "expected_next_trade_date",
            *return_cols]],
        left_on=["code", "trade_date"], right_on=["ts_code", "trade_date"], how="left",
    )
    merged["open_premium"] = merged["next_open"] / merged["next_pre_close"] - 1
    merged["open_to_close"] = merged["next_close"] / merged["next_open"] - 1
    return merged
