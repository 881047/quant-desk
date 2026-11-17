"""Facts for the model to reason over — not conclusions.

The failure mode of an "AI trading desk" is that the model is handed a ranking
and asked to pick. This module hands it the opposite: a compact panel of
measurements (breadth, limit-up structure, industry dispersion, gap behaviour,
core regime, data health) and lets it form a view. Nothing here is a signal.

Everything is computed from stored data and stamped with an ``as_of`` date, so a
memo written on top of it can be audited and scored later.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from service import settings
from service import vocabulary as vocab
from service.kernel.clock import SHANGHAI
from service.kernel.units import TRADING_DAYS_PER_YEAR

MAX_DAYS = settings.integer("research.max_days")


def _shift(yyyymmdd: str, days: int) -> str:
    stamp = datetime.strptime(yyyymmdd, "%Y%m%d") - timedelta(days=days)
    return stamp.strftime("%Y%m%d")


# Five of the eleven columns `daily` carries. Nothing below reads high, low, vol
# or amount, and decoding 1.5 M rows of five columns instead of eleven is a
# third of this endpoint's read time.
_DAILY_COLUMNS = ("ts_code", "trade_date", "open", "close", "pre_close")


def _window(store, end: str, days: int) -> pd.DataFrame:
    return store.read("daily", _shift(end, days), end, columns=list(_DAILY_COLUMNS))


def _ew_market(daily: pd.DataFrame) -> pd.Series:
    px = daily[["ts_code", "trade_date", "close", "pre_close"]].copy()
    px["ret"] = px["close"] / px["pre_close"] - 1
    return px.groupby("trade_date")["ret"].mean().sort_index()


def _breadth(daily: pd.DataFrame, window: int) -> float:
    """Share of listed stocks trading above their own moving average.

    Computed on the long frame instead of a pivoted one. The old form pivoted
    1.4 M rows into a (260, 5000) panel and then ran ``rolling(window).mean()``
    over it, which took 0.29 s for the two calls; the per-stock window is the
    same arithmetic on the long frame and lands in 0.02 s.

    ``rolling`` with the default ``min_periods`` requires a full window of
    observations, so a stock that listed three weeks ago is *not* above its
    MA60 — it has no MA60. ``count == window`` keeps that, rather than letting a
    short history produce a mean that reads as a signal.
    """
    d = daily[["ts_code", "trade_date", "close"]]
    if d.empty:
        return float("nan")
    # The window is a number of *sessions*, not a number of rows: a stock that
    # was suspended for three of the last twenty has no MA20, and counting rows
    # instead of dates would quietly average the nineteen it does have.
    sessions = sorted(d["trade_date"].unique())[-window:]
    agg = d[d["trade_date"].isin(sessions)].groupby("ts_code", sort=False)["close"].agg(
        ["mean", "count"])
    ma = agg["mean"].where(agg["count"] == window)

    peak = d["trade_date"].max()
    last = d[d["trade_date"] == peak].set_index("ts_code")["close"].reindex(ma.index)
    above = (last > ma).sum()
    valid = int(last.notna().sum())
    return float(above / valid) if valid else float("nan")


def _limit_stats(pools: pd.DataFrame, dates: list[str]) -> dict:
    """Derived-pool half of the limit-up picture. Never the only half.

    On 2026-10-28 this function reported ``max_boards=1, first_board_share=1.0``
    while the session's ladder actually reached five: the derived pool had been
    rebuilt for that single day with no warm-up, so every streak restarted at 1.
    A number that is wrong and confident is worse than a missing one, so the
    caller checks this for the single tell-tale sign — ``max_boards <= 1`` while
    another source reports 2+ — and only then demotes it.
    """
    if pools.empty:
        return {}
    p = pools[pools["trade_date"].astype(str).isin(dates)]
    if p.empty:
        return {}
    counts = p.groupby(["trade_date", "pool"]).size().unstack(fill_value=0)
    out = {"last5": counts.tail(5).to_dict("index")}
    # Ladder and first-board share are properties of *the last session*, not of
    # the five-day window: taking the max over a week reports the best day of the
    # week as if it were today, and the mean of the share as if it were a rate.
    last_day = max(dates) if dates else None
    up = p[(p["pool"] == "limit_up") & (p["trade_date"].astype(str) == str(last_day))]
    if not up.empty:
        out["max_boards"] = int(pd.to_numeric(up["boards"], errors="coerce").max())
        out["first_board_share"] = round(
            float((pd.to_numeric(up["boards"], errors="coerce") == 1).mean()), 3)
    return out


def _limit_structure(store, pools: pd.DataFrame, dates: list[str]) -> dict:
    """The ladder, read from every source that claims it, with the spread shown.

    Authority is the derived pool unless it looks unheated. The earlier rule —
    "THS is authoritative because its ladder cannot be off by a warm-up window" —
    was true about warm-up and wrong about coverage: 同花顺 dropped 16 limit-ups on
    2026-10-26 and turned a 5-board day into a 4-board one on 2026-09-02. The
    derived pool's own failure mode (rebuilt with no warm-up, everything reads 1
    board) is detectable, so neither side gets unconditional trust. Disagreement
    is never averaged: it is reported in ``sources``.
    """
    derived = _limit_stats(pools, dates)
    window = dates[-5:]

    ths = store.read("ths_limit_up", window[0], window[-1]) if window else pd.DataFrame()
    up_counts: dict[str, int | None] = {}
    streak: dict[str, int | None] = {}

    if not ths.empty:
        ths = ths.copy()
        ths["trade_date"] = ths["trade_date"].astype(str)
        by_day = ths.groupby("trade_date").size()
        derived["last5_ths"] = {k: int(v) for k, v in by_day.tail(5).items()}
        last = ths[ths["trade_date"] == str(window[-1])] if window else ths.iloc[0:0]
        # Same rule as the derived side: the ladder is today's, not the week's.
        if not last.empty:
            streak["ths_limit_up"] = int(pd.to_numeric(last["continue_cnt"],
                                                       errors="coerce").max())
        up_counts["ths_limit_up"] = int(by_day.loc[window[-1]]) if window and window[-1] in by_day else None

    if "max_boards" in derived:
        streak["derived_t0"] = derived["max_boards"]

    em = store.read("limit_up_pool", window[-1], window[-1]) if window else pd.DataFrame()
    if not em.empty:
        up_counts["eastmoney"] = len(em)
        if "boards" in em.columns:
            streak["eastmoney"] = int(pd.to_numeric(em["boards"], errors="coerce").max())

    derived_last = (derived.get("last5") or {})
    if window:
        cells = derived_last.get(window[-1]) or {}
        up_counts["derived_t0"] = int(cells.get("limit_up", 0)) or None

    up_counts = {k: v for k, v in up_counts.items() if v is not None}
    streak = {k: v for k, v in streak.items() if v is not None}

    # 谁说话算数：派生池优先，除非它明显没有预热。
    #
    # 这里原来写的是「THS 权威，因为它带厂商自己的天梯，不受预热窗口影响」。前半句
    # 成立，后半句没有：厂商天梯确实不受预热影响，但它受「名单不全」影响。2026-09-01
    # 至 10-30 的 38 个交易日里，同花顺的连板高度有 1 天报错（2026-09-02 把 5 板报成
    # 4 板，因为它漏掉了当天那只 5 板），涨停家数有 22 天偏低、平均少 1.6 只。
    #
    # 派生池唯一的已知失败模式恰好是「没带预热重建」，而它可以被认出来：它说 1 板
    # 而别人说 2 板以上。两个失败模式都能识别，就不该无条件信任其中一家。
    options = {k: v for k, v in streak.items() if v is not None}
    others = [v for k, v in options.items() if k != "derived_t0"]
    unheated = ("derived_t0" in options and options["derived_t0"] <= 1
                and any(v >= 2 for v in others))
    if "derived_t0" not in options or unheated:
        source_used = ("ths_limit_up" if "ths_limit_up" in options
                       else "eastmoney" if "eastmoney" in options else None)
    else:
        source_used = "derived_t0"
    out = {
        "as_of": window[-1] if window else None,
        # 原来叫 `authority`。它说的是"max_boards 这个数取自哪个源"，是出处，
        # 不是"哪个源可信"。sources 里三个值都在，读者可以自己比。
        "source_used": source_used,
        "selection_rule": ("derived_t0 优先；它说 1 板而其他源 ≥2 板时判为"
                           "未预热（unheated），改取 ths_limit_up，再退 eastmoney"),
        "max_boards": None if source_used is None else options.get(source_used),
        "sources": {"max_boards": streak, "limit_up": up_counts},
        "conflict": max(set(streak.values())) - min(set(streak.values())) > 0
        if len(streak) > 1 else False,
    }
    if "first_board_share" in derived:
        out["first_board_share"] = derived["first_board_share"]
    if derived.get("last5"):
        out["last5"] = derived["last5"]
    if unheated:
        out["note"] = ("派生池看起来没有预热（最高只到 1 板，而另一源报到 2 板以上），"
                       "故改用厂商天梯。要修派生池：scripts/derive_pools.py --warmup-days")
    elif out["conflict"]:
        out["note"] = ("连板高度两源不一致，以派生池为准：它的定义是 close == up_limit，"
                       "可以逐只核对；厂商天梯会漏名字（2026-09-02 同花顺把 5 板报成 4 板，"
                       "因为漏了那只票）。sources.max_boards 列出各源原值。")
    return out


def _industry_dispersion(daily: pd.DataFrame, basic: pd.DataFrame, days: int = 5) -> dict:
    if basic.empty or "industry" not in basic.columns:
        return {}
    # Pivot the six sessions this actually asks about, not the 260 it is handed.
    # The old form built the full (260, 5000) panel and kept six rows of it.
    dates = sorted(daily["trade_date"].unique())[-(days + 1):]
    if len(dates) < days + 1:
        return {}
    px = (daily[daily["trade_date"].isin(dates)]
          .pivot_table(index="trade_date", columns="ts_code", values="close", aggfunc="last")
          .sort_index())
    if len(px) < days + 1:
        return {}
    ret = (px.iloc[-1] / px.iloc[0] - 1).dropna()
    names = basic.set_index("ts_code")["industry"]
    grouped = ret.groupby(names.reindex(ret.index)).mean().dropna()
    if grouped.empty:
        return {}
    top = grouped.sort_values(ascending=False)
    return {
        "window_days": days,
        "spread_top_minus_bottom": round(float(top.iloc[0] - top.iloc[-1]), 4),
        "top": [[str(k), round(float(v), 4)] for k, v in top.head(5).items()],
        "bottom": [[str(k), round(float(v), 4)] for k, v in top.tail(5).items()],
        # 这里只有"涨跌幅"，且用的是东财标签（104 个桶）。宽度、涨停家数、主力净流入
        # 需要把日线/申万成员/涨停价/资金流按成员对齐，那一层在 knowledge(topic=industries)。
        # 指过去而不是在这里再算一遍：同一个问题两份口径，第一次分歧就没人说得清。
        "deeper": ("quant_knowledge(topic=industries) 是同一件事的深一层：申万一级口径、"
                   "全量 31 个行业、带宽度/涨停家数/成交额占比/主力净流入。"),
    }


def _gap_behaviour(daily: pd.DataFrame) -> dict:
    """What happened *today* conditional on the overnight gap, market-wide."""
    # Slice to the last session before doing any arithmetic. Deriving the gap
    # over the whole 260-session window and then keeping one day cost a full
    # copy of five columns and a dropna over ~1.4 M rows to answer a question
    # about 5,000 of them.
    peak = daily["trade_date"].max()
    p = daily[daily["trade_date"] == peak][
        ["ts_code", "trade_date", "open", "close", "pre_close"]].copy()
    p["gap"] = p["open"] / p["pre_close"] - 1
    p["intra"] = p["close"] / p["open"] - 1
    p = p.dropna()
    if p.empty:
        return {}
    bins = [-1, -0.03, -0.01, 0.01, 0.03, 1]
    labels = ["gap<-3%", "-3~-1%", "-1~1%", "1~3%", "gap>3%"]
    p["bucket"] = pd.cut(p["gap"], bins=bins, labels=labels)
    today = p
    agg = today.groupby("bucket", observed=True)["intra"].agg(["mean", "count"])
    return {str(k): [round(float(r["mean"]), 4), int(r["count"])]
            for k, r in agg.iterrows()}


def _core_regime(store, as_of: str | None = None, *, book) -> dict:
    """The core sleeve's regime, read through the caller's book.

    ``book`` is injected, not imported. This module used to do
    ``from service.trading.core import CoreBook`` inside the function — a cycle
    dodge, because ``trading`` sits above ``research`` and ``trading.arena``
    already imports ``research.stats`` at module level. The dodge worked only as
    long as nobody moved that import to the top of the file; the ETF code lists
    above did not even get that treatment and sat at module scope.

    The book must answer ``universe()`` → ``(defensive, risk)``,
    ``prices()`` and ``target_weights(day)``. Anything satisfying that is fine —
    the API passes ``desk.core_book()``.
    """
    defensive, risk_etfs = book.universe()
    px = book.prices()
    if as_of is not None:
        px = px.loc[:str(as_of)]
    if px.empty:
        raise RuntimeError(f"no core prices up to {as_of}")
    day = str(px.index.max())
    weights = book.target_weights(day)
    meta = weights.pop("_meta", {})
    risk = [c for c in risk_etfs if c in px.columns]
    mom20 = (px[risk].iloc[-1] / px[risk].iloc[-21] - 1).sort_values(ascending=False)

    # ``meta["benchmark"]`` is close x cumulative adj_factor — a back-adjusted
    # series. It is the right scale for comparing to its own MA200 and the wrong
    # number to quote as a price: on 2026-10-28 it read 5.261 while the ETF
    # traded at 4.152. A model cross-checking quant_quote against this would see
    # a 27% gap and have no way to tell which one was broken.
    benchmark = meta.get("benchmark")
    ma = meta.get("ma")
    raw_close = None
    raw_bond = None
    wanted = [c for c in ("510300.SH", defensive) if c in px.columns]
    if wanted:
        raw = store.read("fund_daily", day, day, columns=["ts_code", "close"])
        if not raw.empty:
            for code in wanted:
                hit = raw[raw["ts_code"] == code]
                if hit.empty:
                    continue
                price = round(float(hit["close"].iloc[-1]), 3)
                if code == "510300.SH":
                    raw_close = price
                if code == defensive:
                    # ``bond_lot_cny`` decides whether the account can hold the
                    # defensive leg at all, so it has to be the tradeable price:
                    # 147.16 (back-adjusted) quotes a lot at 14,716 when the ETF
                    # actually costs 141.216 x 100 = 14,122.
                    raw_bond = price

    return {
        "as_of": day,
        "trend_ok": meta.get("trend_ok"),
        "benchmark_vs_ma200": {
            **meta,
            "benchmark_close": raw_close,
            "distance": (round(benchmark / ma - 1, 4)
                         if benchmark and ma else None),
            "note": ("benchmark/ma 是后复权价（close×累计复权因子），只能和彼此比；"
                     "benchmark_close 才是可成交价。distance 是两者之比，与单位无关。"),
        },
        "momentum_20d": [[c, round(float(mom20[c]), 4)] for c in mom20.index[:5]],
        "bond_lot_cny": round(raw_bond * 100, 0) if raw_bond else None,
        "bond_lot_basis": "一手 = 100 份 × 当日收盘价（可成交口径，不是后复权）",
    }


def discover(store, registry, as_of: str | None = None, *, core_book) -> dict:
    """One call, ~3KB, everything a researcher needs to form a view.

    ``core_book`` is required, not defaulted: the core block is one of the seven
    measurements on this panel, and a default of ``None`` would turn "the caller
    forgot the sleeve" into a silently shorter panel. The API passes
    ``desk.core_book()``; a test passes ``CoreBook(store, None)``.
    """
    # Read the window, not the archive. `store.read("daily")` with no bounds
    # decoded 16 million rows to keep the last 260 sessions of them, and that one
    # line was most of this endpoint's 7.5 seconds.
    end = as_of or datetime.now(SHANGHAI).strftime("%Y%m%d")
    daily = _window(store, end, 420)
    if daily.empty:
        daily = store.read("daily", None, end, columns=list(_DAILY_COLUMNS))
    if daily.empty:
        raise RuntimeError("daily is empty")
    daily["trade_date"] = daily["trade_date"].astype(str)
    dates = sorted(daily["trade_date"].unique())
    eligible = [d for d in dates if d <= str(end)]
    if not eligible:
        raise RuntimeError(f"daily is empty up to {end}")
    # A weekend/as-of between sessions refers to the last observable close, not
    # to a synthetic row and never to the next session.
    day = eligible[-1]
    window_dates = eligible[-MAX_DAYS:]
    daily = daily[daily["trade_date"].isin(window_dates)].copy()

    ew = _ew_market(daily)
    rets = ew.tail(MAX_DAYS)
    vol20 = float(rets.tail(20).std() * np.sqrt(TRADING_DAYS_PER_YEAR)) if len(rets) >= 20 else None
    vol_pct = None
    if len(rets) >= 60:
        rolling = rets.rolling(20).std().dropna() * np.sqrt(TRADING_DAYS_PER_YEAR)
        if len(rolling) > 20 and vol20 is not None:
            vol_pct = round(float((rolling < vol20).mean()), 3)

    pools = store.read("limit_pool_t0", window_dates[0], day) if len(window_dates) else pd.DataFrame()
    basic = store.read("stock_basic", columns=["ts_code", "name", "industry"])

    summary = {row["dataset"]: row for row in registry.summary()}
    available = {name: row for name, row in summary.items()
                 if row.get("cover_start") and str(row["cover_start"]) <= day}
    core_ok = all(name in available for name in ("daily", "adj_factor", "stk_limit"))

    def observed_end(name: str) -> str | None:
        value = available.get(name, {}).get("cover_end")
        return min(str(value), day) if value else None

    # The prior behind the readings. `market` says breadth is 19.7%; this says
    # that is the 3rd percentile of the last year, that the equal-weight market
    # has done +3.2% over the next 20 sessions after such days in 205 separate
    # episodes since 2005, and how much of that sample is the last year. Without
    # it the model can only narrate; with it, it can disagree with a number.
    from . import state as state_mod

    try:
        # ``daily`` is deliberately *not* handed to the state builder: state
        # percentiles need the full 2005-onward history, and feeding it a
        # 260-session window would silently re-base every percentile. It reads
        # its own cache, keyed on the last session, so this costs nothing here.
        state_block = state_mod.block(state_mod.cached_state(store, last=day, as_of=day),
                                      day=day)
    except Exception as exc:  # noqa: BLE001 - an information source must not take the desk down
        state_block = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "as_of": day,
        "data_health": {
            "core_ok": core_ok,
            "quarantined": [d for d, row in available.items()
                            if row.get("quarantine_partitions")
                            and str(row.get("cover_end") or "") <= day],
            "daily_last": observed_end("daily"),
            "pool_last": observed_end("limit_pool_t0"),
        },
        "market": {
            "ew_return_1d": round(float(rets.iloc[-1]), 4) if len(rets) else None,
            "ew_return_5d": round(float((1 + rets.tail(5)).prod() - 1), 4) if len(rets) >= 5 else None,
            "ew_return_20d": round(float((1 + rets.tail(20)).prod() - 1), 4) if len(rets) >= 20 else None,
            "realised_vol_20d": round(vol20, 4) if vol20 else None,
            "vol_percentile_1y": vol_pct,
            "breadth_above_ma20": round(_breadth(daily, 20), 3),
            "breadth_above_ma60": round(_breadth(daily, 60), 3),
            "units": vocab.note("returns"),
        },
        "state": state_block,
        "limits": _limit_structure(store, pools, window_dates[-5:] if window_dates else []),
        "industries": _industry_dispersion(daily, basic),
        "gap_today": _gap_behaviour(daily),
        "core": _core_regime(store, as_of=day, book=core_book),
        "notes": [
            "breadth = share of listed stocks above their own MA; it measures participation, not direction",
            "limits.last5 counts are per pool, not deduplicated across pools",
            ("limits.max_boards 以派生池为准（除它明显没预热）；limits.sources 列出各源原值，"
             "conflict=true 时不要引用那个数，先看 source_used 与 note"),
            "gap_today is descriptive: mean intraday move today by overnight gap bucket",
            "state.history 是同一状态桶之后的市场分布；forward_basis 写了从哪个价格起算；edge_* 的定义见 state.edge_definition",
        ],
    }
