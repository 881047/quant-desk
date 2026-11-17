"""Free browsing of the whole market — facts only, no ranking, no advice.

The rule screen answers "which of these five should I buy", which makes the model
a switch operator. This module answers a different question: "show me what is
there", with the fields the rules never use (turnover, volume ratio, float cap,
valuation, industry). The model decides what to look at, and why.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from service import settings
from service.adapters.pit import PitUniverse
from service.adapters.symbols import board_of, limit_pct

MAX_LIMIT = settings.integer("screen.browse_limit")

# Enough calendar days to contain the 30 sessions the panel needs, even across a
# Spring Festival break. Reading "everything up to as_of" instead pulled 16
# million rows through parquet to keep the last thirty of them.
PANEL_DAYS = settings.integer("screen.panel_days")


def _shift(yyyymmdd: str, days: int) -> str:
    return (datetime.strptime(yyyymmdd, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")


def _panel(store, as_of: str, lookback: int = 30) -> pd.DataFrame:
    daily = store.read("daily", _shift(as_of, PANEL_DAYS), as_of,
                       columns=["ts_code", "trade_date", "close", "pre_close", "amount"])
    if daily.empty:
        return pd.DataFrame()
    daily["trade_date"] = daily["trade_date"].astype(str)
    days = sorted(daily["trade_date"].unique())[-lookback:]
    daily = daily[daily["trade_date"].isin(days)].copy()
    daily = daily.sort_values(["ts_code", "trade_date"], kind="mergesort")
    close = pd.to_numeric(daily["close"], errors="coerce")
    pre_close = pd.to_numeric(daily["pre_close"], errors="coerce")
    ret = close / pre_close.where(pre_close > 0) - 1
    ret = ret.mask(~daily["ts_code"].duplicated(), 0.0).fillna(0.0)
    level = (1.0 + ret).groupby(daily["ts_code"], sort=False).cumprod()
    last_level = level.groupby(daily["ts_code"], sort=False).transform("last")
    anchor = close.groupby(daily["ts_code"], sort=False).transform("last")
    daily["adj_close"] = level * (anchor / last_level.where(last_level > 0)).fillna(0.0)
    wide = daily.pivot_table(index="trade_date", columns="ts_code", values="adj_close", aggfunc="last")
    wide = wide.sort_index()
    return wide


def _ret(wide: pd.DataFrame, periods: int) -> pd.Series:
    if len(wide) <= periods:
        return pd.Series(dtype="float64")
    return wide.iloc[-1] / wide.iloc[-1 - periods] - 1


def browse(store, as_of: str, *, industry: str = "", query: str = "",
           min_amount_cny: float = 0.0, max_price: float | None = None,
           min_price: float | None = None, exclude_st: bool = True,
           limit: int = 50, order: str = "code") -> dict:
    """Return a filtered slice with raw facts. Ordering is display only.

    Every row carries ``board``, ``band`` and ``tradeable``. ``board`` is set for
    all 5,000+ names, not just 北交所: the board decides the band, the lot and the
    tick, so a null there left the reader to infer it from the code. ``band`` is
    the daily limit as a fraction (0.10 / 0.20 / 0.30, or 0.05 for ST), which is
    the number behind "how far is +8% from the ceiling".

    北交所 names are shown because they are part of the tape, but they are
    marked ``tradeable=false`` — this desk does not select or buy them (30% band,
    different liquidity). The blocking itself lives in the guard, not here, so a
    ticket proposed from any other path still hits it.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    wide = _panel(store, as_of)
    if wide.empty:
        return {"as_of": as_of, "universe_size": 0, "rows": [], "note": "no daily bars"}

    basic = store.read("stock_basic", columns=["ts_code", "name", "industry"])
    daily_basic = store.read("daily_basic", as_of, as_of)
    daily = store.read("daily", as_of, as_of, columns=["ts_code", "amount"])

    last = wide.iloc[-1].dropna()
    frame = pd.DataFrame({
        "symbol": last.index.astype(str),
        "close": last.to_numpy(),
        "ret_1d": _ret(wide, 1).reindex(last.index).to_numpy(),
        "ret_5d": _ret(wide, 5).reindex(last.index).to_numpy(),
        "ret_20d": _ret(wide, 20).reindex(last.index).to_numpy(),
        "low_20d": wide.tail(20).min().reindex(last.index).to_numpy(),
        "high_20d": wide.tail(20).max().reindex(last.index).to_numpy(),
    })
    # Declared unconditionally. ``daily`` is read over a 75-day window that ends
    # at ``as_of``; on a weekend or holiday that window still holds rows, but a
    # window that ends before the first stored session holds none — and then the
    # column never existed, so ``amount_cny`` filters and the ``order="amount"``
    # sort raised KeyError instead of returning an empty list.
    frame["amount_cny"] = float("nan")
    if not daily.empty:
        amt = daily.set_index("ts_code")["amount"].astype(float) * 1000
        frame["amount_cny"] = frame["symbol"].map(amt).to_numpy()
    frame["name"] = None
    frame["industry"] = None
    if not basic.empty:
        info = basic.drop_duplicates("ts_code").set_index("ts_code")
        frame["name"] = frame["symbol"].map(info["name"])
        frame["industry"] = frame["symbol"].map(info["industry"])
    # 申万 taxonomy, so a filter means the same thing here as it does in
    # `quant_profile`. The vendor label has 104 buckets (`银行` covers 42 names,
    # `建筑工程` covers 128) and 申万 L3 has 337; without this, "给我看城商行"
    # cannot be expressed and the caller falls back to sector-level guessing.
    try:
        sw = store.read("index_member", columns=["ts_code", "l1_name", "l3_name"])
    except (KeyError, FileNotFoundError, ValueError):
        sw = pd.DataFrame()
    if sw is not None and not sw.empty and "l1_name" in sw.columns:
        sw = sw.drop_duplicates("ts_code").set_index("ts_code")
        frame["sw_l1"] = frame["symbol"].map(sw["l1_name"])
        frame["sw_l3"] = frame["symbol"].map(sw["l3_name"])
    if not daily_basic.empty:
        db = daily_basic.drop_duplicates("ts_code").set_index("ts_code")
        for src, dst in (("turnover_rate", "turnover"), ("volume_ratio", "volume_ratio"),
                         ("circ_mv", "float_cap_1e4"), ("pe", "pe"), ("pb", "pb")):
            if src in db.columns:
                frame[dst] = frame["symbol"].map(pd.to_numeric(db[src], errors="coerce"))

    frame["range_pos_20d"] = ((frame["close"] - frame["low_20d"])
                              / (frame["high_20d"] - frame["low_20d"]).replace(0, np.nan))
    # The same predicate `screen` and the guard use, not "the name contains ST".
    # Two definitions meant a name that was *about to be* ST (or is in a
    # delisting period, 退/退市) showed up in `browse` while `screen` excluded
    # it — and `browse(exclude_st=True)` is where the model goes looking for
    # names it has not been shown yet.
    flags: dict[str, bool] = {}
    try:
        namechange = store.read("namechange")
    except (KeyError, FileNotFoundError, ValueError, OSError):
        namechange = pd.DataFrame()
    if not basic.empty:
        flags = PitUniverse(basic=basic, namechange=namechange).st_map(
            frame["symbol"].tolist(), as_of)
    frame["is_st"] = frame["symbol"].map(lambda code: bool(flags.get(code, False)))

    if exclude_st:
        frame = frame[~frame["is_st"]]
    if industry:
        # Matches either taxonomy: 银行 (vendor) and 城商行Ⅲ (申万 L3) both work,
        # because a caller should not have to know which table a name came from.
        hit = frame["industry"].astype(str).str.contains(industry, na=False)
        for col in ("sw_l1", "sw_l3"):
            if col in frame.columns:
                hit = hit | frame[col].astype(str).str.contains(industry, na=False)
        frame = frame[hit]
    if query:
        hit = (frame["symbol"].str.contains(query, na=False)
               | frame["name"].astype(str).str.contains(query, na=False))
        frame = frame[hit]
    if min_amount_cny:
        frame = frame[pd.to_numeric(frame["amount_cny"], errors="coerce") >= min_amount_cny]
    if max_price is not None:
        frame = frame[frame["close"] <= max_price]
    if min_price is not None:
        frame = frame[frame["close"] >= min_price]

    total = len(frame)
    if order == "amount":
        frame = frame.sort_values("amount_cny", ascending=False)
    elif order == "move":
        frame = frame.reindex(frame["ret_1d"].abs().sort_values(ascending=False).index)
    else:
        frame = frame.sort_values("symbol")
    frame = frame.head(limit)

    rows = []
    for row in frame.to_dict("records"):
        board = board_of(row["symbol"])
        rows.append({
            "symbol": row["symbol"],
            "name": row.get("name"),
            "industry": row.get("industry"),
            "sw_l1": row.get("sw_l1"),
            "sw_l3": row.get("sw_l3"),
            "close": round(float(row["close"]), 3) if pd.notna(row["close"]) else None,
            "ret_1d": _r(row.get("ret_1d")),
            "ret_5d": _r(row.get("ret_5d")),
            "ret_20d": _r(row.get("ret_20d")),
            "amount_cny": None if pd.isna(row.get("amount_cny")) else round(float(row["amount_cny"]), 0),
            "turnover": None if pd.isna(row.get("turnover")) else round(float(row["turnover"]), 2),
            "volume_ratio": None if pd.isna(row.get("volume_ratio")) else round(float(row["volume_ratio"]), 2),
            "float_cap_1e4": None if pd.isna(row.get("float_cap_1e4")) else round(float(row["float_cap_1e4"]), 0),
            "pe": None if pd.isna(row.get("pe")) else round(float(row["pe"]), 1),
            "pb": None if pd.isna(row.get("pb")) else round(float(row["pb"]), 2),
            "range_pos_20d": _r(row.get("range_pos_20d"), pct=False),
            # 板块对每一行都有信息，不只是北交所那 200 多只：它决定涨跌幅、
            # 一手多少股、最小价格变动。原先这里是 `"BSE" if .BJ else None`，
            # 于是 5,377 只沪深股全部拿到 null——一个只对 4% 的行携带信息的字段，
            # 等于让读者自己从代码猜板块，而"猜板块"正是这张表要免掉的动作。
            #
            # `band` 是板块的后果，不是标签的重复：涨跌幅 10%/20%/30% 决定
            # "现在 +8% 离天花板还有多远"。它同时也是"不能从代码推"的保险——
            # 主板 ST 在 2026-07-06 之前是 5%，与其它主板不同（现已同为 10%）。
            # 小数口径，与 ret_* 一致。
            #
            # 北交所可以看，不可以买：30% 涨跌幅、流动性和主板不是一回事，
            # 而这个台子的选股与买入口径不含它。这里只标注，不过滤——
            # 信息是信息，仓位是仓位。真正的拦截在 guard（`board` 检查），
            # 因为 propose 可以绕过 screen，绕过不了一个检查项。
            "board": board,
            "band": float(limit_pct(
                row["symbol"],
                is_st=bool(row.get("is_st")),
                name=row.get("name"),
                as_of=as_of,
            )),
            "tradeable": board != "BSE",
        })
    return {
        "as_of": as_of,
        "universe_size": total,
        "returned": len(rows),
        "order": order,
        "note": "facts only; ordering is for display, not a recommendation",
        "rows": rows,
    }


def _r(value, pct: bool = True):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    return round(float(value), 4) if pct else round(float(value), 3)
