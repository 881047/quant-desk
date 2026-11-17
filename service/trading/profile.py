"""One symbol, everything the desk knows about it.

The desk answers "what is the market doing" well and "what is *this name*"
not at all: `/market/bars` gives 60 rows of prices, `/knowledge/<topic>` answers
one question at a time, and neither knows that a stock has a valuation history,
a leverage balance, an earnings record or a float that can change size.

This module assembles that dossier. Three rules it holds itself to:

**Every figure carries the date it is true as of.** A valuation percentile is
computed over a named window ending on a named session, not "recently". The
financial block reports ``ann_date`` next to ``end_date``, because a Q2 report
published in August was not knowable in July.

**A missing input is stated, not zeroed.** Nine of the blocks can be absent on
a given day — the flow table lags, a new listing has no percentile, a name is
suspended. Each one returns ``null`` with a reason rather than 0.0, because 0.0
is a number the model will use.

**It describes, it does not conclude.** There is no score, no rank against
sector, no "cheap" or "expensive" label. Percentiles are arithmetic; what counts
as expensive is the caller's judgement, and the whole point of this desk is that
the kernel does not make it.
"""

from __future__ import annotations

import math
import re
import threading
import time
from itertools import count
from typing import Any

import numpy as np
import pandas as pd

from service.kernel.units import SHARES_PER_LOT, TRADING_DAYS_PER_YEAR


class DatasetReadError(RuntimeError):
    """A dataset could not be read; never confuse this with an empty dataset."""

    def __init__(self, dataset: str, cause: Exception):
        self.dataset = dataset
        self.cause = cause
        super().__init__(f"{dataset} 读取失败：{type(cause).__name__}: {cause}")


# Fundamentals the profile reports, in the order a reader wants them.
# Column names are the vendor's and they are checked, not assumed: the first
# version of this tuple asked for `ocf_to_or`, which does not exist in the table,
# so the cash-flow line was silently null on every name for every quarter.
# `test_profile_every_reported_field_exists` now fails if one goes missing again.
FINA_FIELDS = (
    ("roe_waa", "ROE（加权，%）"),
    ("roe", "ROE（摊薄，%）"),
    ("grossprofit_margin", "毛利率（%）"),
    ("netprofit_margin", "净利率（%）"),
    ("or_yoy", "营收同比（%）"),
    ("netprofit_yoy", "净利同比（%）"),
    ("debt_to_assets", "资产负债率（%）"),
    ("q_ocf_to_sales", "经营现金流/营收（单季，%）"),
    ("current_ratio", "流动比率"),
    ("assets_turn", "总资产周转率"),
    # Deducted / cash-flow readings. Tushare's fina_indicator already carries
    # these; exposing them stops a reader from fetching 扣非/FCFF from the web.
    ("profit_dedt", "扣非净利润（元，YTD）"),
    ("extra_item", "非经常性损益（元，YTD）"),
    ("fcff", "企业自由现金流（元，YTD）"),
    ("ocfps", "每股经营现金流（元/股，YTD）"),
    ("roic", "投入资本回报率（%）"),
    ("q_roe", "单季 ROE（%）"),
)

# Valuation series the percentile is computed on.
VALUATION_FIELDS = ("pe_ttm", "pb", "ps_ttm", "dv_ttm", "turnover_rate")


def _n(value, ndigits: int = 4) -> float | None:
    """JSON-safe number, or None. Never silently turns a gap into zero."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, ndigits)


def _pct_rank(series: pd.Series, value: float | None) -> float | None:
    """Share of the window at or below ``value``. 0 = cheapest of the window.

    A NaN must come back as None. Passing it through makes every comparison
    False, so ``(clean <= nan).mean()`` returns 0.0 — the number that means
    "cheapest in the window", invented for a name whose PE is missing.
    """
    if value is None or series is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) < 60:
        return None
    return round(float((clean <= number).mean()), 3)


def _identity(desk, symbol: str, day: str) -> dict:
    basic = desk.market.basic()
    row: dict[str, Any] = {}
    if not basic.empty:
        hit = basic[basic["ts_code"] == symbol]
        if not hit.empty:
            row = hit.iloc[0].to_dict()
    inst = desk.market.instrument(symbol, day)
    names = desk.market.names()
    former: list[dict] = []
    try:
        changes = desk.market.namechange()
        if not changes.empty:
            mine = changes[changes["ts_code"] == symbol].copy()
            mine["start_date"] = mine["start_date"].astype(str)
            mine = mine[mine["start_date"] <= str(day)].sort_values("start_date")
            former = [{"name": str(r["name"]), "from": str(r["start_date"]),
                       "reason": str(r.get("change_reason") or "")}
                      for _, r in mine.tail(3).iterrows()]
    except (KeyError, ValueError):
        former = []

    list_date = str(row.get("list_date") or "")
    # `delist_date=null` 以前靠旁边的 `delist_date_note` 消歧，但那个字段只在
    # "空"时才有，而且是个兄弟键——只取 delist_date 的消费者（以及字段被截断时）
    # 仍然分不清"正常上市"与"资料缺失"。显式给状态字段（docs/14 D3）。
    delist_raw = str(row.get("delist_date") or "")
    if not row:
        delist_status = "unknown"          # 连这只票都没查到，不是"未退市"
    elif not delist_raw:
        delist_status = "listed"
    elif delist_raw > str(day):
        delist_status = "scheduled"        # 已公告、尚未到退市日
    else:
        delist_status = "delisted"
    listed_sessions = None
    if list_date and len(list_date) == 8:
        try:
            listed_sessions = len(desk.market.calendar.between(list_date, day))
        except (ValueError, KeyError):
            listed_sessions = None

    sw = desk.market.sw_of(symbol)
    peers = desk.market.industry_peers(symbol)
    return {
        "symbol": symbol,
        "name": inst.name or names.get(symbol) or str(row.get("name") or ""),
        "industry": desk.market.industry_of(symbol),
        # Two taxonomies, deliberately both. `industry` is the vendor's 104-bucket
        # label from `stock_basic` (「银行」, 42 names); `sw` is 申万 2021, whose
        # level 3 splits that same group into 国有大型 / 股份制 / 城商行 / 农商行.
        # `comparison_group` says which bucket the peer statistics below used, so
        # a median is never quoted without the set it came from.
        "sw": sw or None,
        "comparison_group": {
            "level": peers.get("level"),
            "name": peers.get("name"),
            "size": peers.get("size"),
            "skipped": peers.get("skipped"),
            "note": peers.get("reason") or (
                f"申万 {peers.get('level')}，{peers.get('size')} 只"
                + (f"；更细的 {peers['skipped'][0]['name']} 只有 "
                   f"{peers['skipped'][0]['size']} 只，样本太少未采用"
                   if peers.get("skipped") else "")
            ),
        } if peers.get("available") else None,
        "board": inst.board.value,
        "is_etf": inst.is_etf,
        "is_st": inst.is_st,
        "is_hs_connect": bool(row.get("is_hs") == "H"),
        "area": str(row.get("area") or "") or None,
        "list_date": list_date or None,
        "listed_sessions": listed_sessions,
        "delist_date": delist_raw or None,
        "delist_status": delist_status,
        "delist_status_note": (
            "listed=正常上市（null 表示没有退市日）；delisted=已退市；"
            "scheduled=已公告、尚未到退市日；unknown=参考表里查不到这只票，"
            "与「未退市」不是一回事。"),
        **({} if delist_raw else {"delist_date_note": "未退市（仍正常交易）"}),
        "former_names": former,
    }


def _tradability(desk, symbol: str, day: str, snap) -> dict:
    inst = desk.market.instrument(symbol, day)
    if snap is None:
        return {"ok": False, "reason": f"{day} 没有日线（停牌或未上市）"}
    last = float(snap.last)
    up, down = float(snap.up_limit), float(snap.down_limit)
    return {
        "ok": True,
        "last": _n(last, 3),
        "pre_close": _n(snap.pre_close, 3),
        "change_pct": _n((last / float(snap.pre_close) - 1) * 100, 2)
        if snap.pre_close else None,
        "open": _n(snap.open, 3), "high": _n(snap.high, 3), "low": _n(snap.low, 3),
        "up_limit": _n(up, 3), "down_limit": _n(down, 3),
        "to_up_limit_pct": _n((up / last - 1) * 100, 2) if last > 0 and up > 0 else None,
        "to_down_limit_pct": _n((down / last - 1) * 100, 2) if last > 0 and down > 0 else None,
        "suspended": bool(snap.suspended),
        "amount_cny": _n(snap.amount, 0),
        "volume_shares": _n(snap.volume, 0),
        "min_lot": int(inst.min_qty),
        "lot_step": int(inst.qty_step),
        "min_ticket_cny": _n(inst.min_qty * last, 2),
    }


def _valuation(desk, symbol: str, day: str, window: int) -> dict:
    hist = desk.market.basic_history(symbol, day, window)
    today = desk.market.basic_row(symbol, day)
    if hist.empty or not today:
        return {"available": False,
                "reason": "daily_basic 里没有这只票的估值序列（次新股或已退市）"}
    latest = hist.iloc[-1]

    def _wan_to_cny(value) -> float | None:
        number = _n(value)
        return _n(number * 1e4, 0) if number is not None else None

    block: dict[str, Any] = {
        "available": True,
        "as_of": str(latest["trade_date"]),
        "window_sessions": len(hist),
        "window_start": str(hist.iloc[0]["trade_date"]),
        "current": {
            "pe_ttm": _n(today.get("pe_ttm"), 2),
            "pb": _n(today.get("pb"), 3),
            "ps_ttm": _n(today.get("ps_ttm"), 2),
            "dividend_yield_ttm_pct": _n(today.get("dv_ttm"), 3),
            "total_mv_cny": _wan_to_cny(today.get("total_mv")),
            "circ_mv_cny": _wan_to_cny(today.get("circ_mv")),
            "turnover_rate_pct": _n(today.get("turnover_rate"), 3),
            "turnover_rate_free_pct": _n(today.get("turnover_rate_f"), 3),
            "volume_ratio": _n(today.get("volume_ratio"), 2),
            # 某个口径为 null 时，读者要能分清"这家没有这项披露"和"我们没算"。
            "note": ("null = 该口径不适用或没有披露（北交所有些估值字段不取、"
                     "未分红就没有股息率）；不是 0，也不是算不出来"),
        },
        "percentile_in_window": {},
        "median_in_window": {},
    }
    # A percentile without a direction is a number the reader can invert. PE at
    # the 92nd percentile is expensive; a dividend yield at the 3rd percentile is
    # *also* expensive. Reporting both as "percentile 92" and "percentile 3" with
    # no sign convention is how a model reads a high yield as cheap.
    block["direction"] = {
        "pe_ttm": "越高越贵", "pb": "越高越贵", "ps_ttm": "越高越贵",
        "turnover_rate": "越高越活跃（不分贵贱）", "dv_ttm": "越高越便宜（股息率越高价格越低）",
    }
    for field in VALUATION_FIELDS:
        if field not in hist.columns:
            continue
        series = pd.to_numeric(hist[field], errors="coerce")
        # PE is meaningless when earnings are negative: tushare returns a number
        # anyway, so the percentile is computed on positive values only and the
        # count of excluded sessions is reported.
        if field in ("pe_ttm", "ps_ttm"):
            series = series.where(series > 0)
        current = pd.to_numeric(pd.Series([today.get(field)]), errors="coerce").iloc[0]
        block["percentile_in_window"][field] = _pct_rank(series, current)
        block["median_in_window"][field] = _n(series.median(), 3) if series.notna().any() else None
        block["percentile_in_window"][f"{field}_sessions"] = int(series.notna().sum())
    # 说明要写在**填完之后**：分红率为空是"没分红"（688981 这种），不是"没算"。
    # 写在填之前会被后面的赋值当成普通键，读者仍然只看到一个 null。
    for part in (block["percentile_in_window"], block["median_in_window"]):
        part["note"] = "null = 该口径没有样本（例如从未分红就没有股息率分位）；不是 0"
    return block


def _fundamentals(desk, symbol: str, day: str, periods: int = 6) -> dict:
    """Latest reported figures, plus the last few quarters as a trend.

    Point in time: only rows announced on or before ``day`` count. A Q2 report
    that came out on 2026-08-28 was not known on 2026-07-15, and using it would
    make every backtest of this block a look-ahead.
    """
    def _coverage() -> str:
        """What the table actually covers, so "no record" is not read as "no data"."""
        try:
            recs = [r for r in desk.registry.list_manifest("fina_indicator")
                    if r.status == "ok" and r.cover_start and r.cover_end]
        except (AttributeError, KeyError):
            return "覆盖范围未知"
        if not recs:
            return "尚无已写入的分区"
        return f"当前覆盖 {recs[0].cover_start}..{recs[0].cover_end}（回填可能仍在进行）"

    try:
        frame = _read_cached(desk, "fina_indicator")
    except DatasetReadError as exc:
        return {"available": False, "status": "error", "reason": str(exc)}
    if frame is None or frame.empty:
        return {"available": False, "status": "empty", "reason": "fina_indicator 为空"}
    mine = frame[frame["ts_code"] == symbol].copy()
    if mine.empty:
        # Distinguishing "this name never reported" from "the backfill has not
        # reached it" matters: the first is a fact about the company, the second
        # is a fact about this desk, and confusing them changes the conclusion.
        return {"available": False, "status": "empty",
                "reason": f"{symbol} 在 fina_indicator 里没有记录；{_coverage()}"}
    for col in ("ann_date", "end_date"):
        mine[col] = mine[col].astype(str)
    known = mine[mine["ann_date"] <= str(day)]
    if known.empty:
        return {"available": False, "status": "empty",
                "reason": f"{day} 之前没有已公告的财报（最早 {mine['ann_date'].min()}）"}
    known = known.sort_values(["end_date", "ann_date"], kind="mergesort")
    known = known.drop_duplicates(subset=["end_date"], keep="last")
    latest = known.iloc[-1]
    fields = {}
    for key, label in FINA_FIELDS:
        if key in known.columns:
            fields[key] = _n(latest[key], 3)
    trend = []
    for _, row in known.tail(periods).iterrows():
        trend.append({
            "period": str(row["end_date"]),
            "announced": str(row["ann_date"]),
            "is_year_end": str(row["end_date"]).endswith("1231"),
            "roe_ytd": _n(row.get("roe"), 2),
            "or_yoy": _n(row.get("or_yoy"), 2),
            "netprofit_yoy": _n(row.get("netprofit_yoy"), 2),
        })
    stale_days = None
    try:
        stale_days = (pd.Timestamp(str(day)) - pd.Timestamp(str(latest["ann_date"]))).days
    except (ValueError, TypeError):
        stale_days = None
    # Which of the reported metrics the latest report simply does not carry.
    # A null that means "the vendor has no value this period" (6.9% of names for
    # roe_waa) reads identically to a null that means "this ratio does not exist
    # for this business" (gross margin for a bank). The reader cannot tell, so
    # say which ones are gaps.
    missing_metrics = [k for k, v in fields.items() if v is None]

    # YTD -> single-quarter restoration. fina_indicator reports year-to-date
    # cumulative figures, so a Q3 扣非 profit is not "the quarter's profit".
    # Subtract the previous period of the SAME year; crossing a year boundary
    # resets the accumulator and must not be subtracted.
    def _ytd(row, col):
        try:
            value = row[col]
            return float(value) if pd.notna(value) else None
        except (KeyError, TypeError, ValueError):
            return None

    ded_ytd = _ytd(latest, "profit_dedt")
    extra_ytd = _ytd(latest, "extra_item")
    period = str(latest["end_date"])
    prev = known.iloc[-2] if len(known) > 1 else None
    same_year = bool(prev is not None and str(prev["end_date"])[:4] == period[:4])
    if period.endswith("0331"):
        q_ded, q_extra = ded_ytd, extra_ytd
        q_basis = "Q1 的 YTD 即单季，无需还原"
    elif same_year:
        prev_ded, prev_extra = _ytd(prev, "profit_dedt"), _ytd(prev, "extra_item")
        q_ded = ded_ytd - prev_ded if ded_ytd is not None and prev_ded is not None else None
        q_extra = (extra_ytd - prev_extra
                   if extra_ytd is not None and prev_extra is not None else None)
        q_basis = "本期 YTD − 同年上一报告期 YTD"
    else:
        q_ded = q_extra = None
        q_basis = "缺同年上一报告期，单季不可还原（不跨年相减）"
    denom = (ded_ytd or 0.0) + (extra_ytd or 0.0)
    nonrec_share = (round(100.0 * extra_ytd / denom, 2)
                    if extra_ytd is not None and denom > 0 else None)
    month = int(period[4:6]) if len(period) >= 6 and period[4:6].isdigit() else 0
    return {
        "available": True,
        "status": "ok",
        "labels": {k: label for k, label in FINA_FIELDS},
        "latest_period": period,
        "announced_on": str(latest["ann_date"]),
        "deducted": {
            "profit_dedt_ytd_cny": ded_ytd,
            "extra_item_ytd_cny": extra_ytd,
            "nonrecurring_share_pct": nonrec_share,
            "q_profit_dedt_cny": q_ded,
            "q_extra_item_cny": q_extra,
            "q_basis": q_basis,
            "period_month": month or None,
            "annualization_factor": round(12.0 / month, 4) if month else None,
            "basis": ("profit_dedt / extra_item 是 YTD 累计（元），不是单季；"
                      "扣非年化 PE 请用 profit_dedt_ytd_cny × annualization_factor "
                      "与同日市值自行计算，本块不给估值结论。"),
        },
        "days_since_announcement": stale_days,
        # Every figure here is year-to-date cumulative, not a single quarter.
        # 002966.SZ reads 2.81 -> 5.62 -> 7.99 -> 9.39 within 2025 and then
        # 2.72 in 2026Q1: a trend list without this label looks like a collapse.
        "basis": "年初至今累计（YTD），不是单季；每年前三个月会重置",
        "is_year_end": str(latest["end_date"]).endswith("1231"),
        "metrics": fields,
        "missing_metrics": missing_metrics,
        "trend": trend,
        "note": "按 ann_date <= 当日 过滤，未公告的季度不出现在这里；roe 为 YTD 累计值",
    }


def _flow(desk, symbol: str, day: str) -> dict:
    """Who has been buying: order-size buckets, plus the leverage balance."""
    out: dict[str, Any] = {"available": False}
    hist = desk.market.flow_history(symbol, day, 25)
    if hist is not None and not hist.empty:
        h = hist.copy()
        for col in h.columns:
            if col not in ("ts_code", "trade_date"):
                h[col] = pd.to_numeric(h[col], errors="coerce")
        # Tushare reports 万元 for moneyflow amounts.
        h["main_net"] = ((h["buy_elg_amount"] - h["sell_elg_amount"])
                         + (h["buy_lg_amount"] - h["sell_lg_amount"])) * 1e4
        today = h.iloc[-1]
        out = {
            "available": True,
            "as_of": str(today["trade_date"]),
            "sessions": len(h),
            "main_net_cny": {
                "today": _n(today["main_net"], 0),
                "5d": _n(h["main_net"].tail(5).sum(), 0),
                "20d": _n(h["main_net"].tail(20).sum(), 0),
            },
            "main_net_days_positive_20d": int((h["main_net"].tail(20) > 0).sum()),
            # Retail net is the exact arithmetic complement of the main figure
            # (every trade has a buyer and a seller inside the same buckets), so
            # reporting it would look like a second, independent observation.
            "note": "主力 = 超大单 + 大单净额；口径来自 tushare moneyflow 的聚合分档，非逐笔还原",
        }
    else:
        out = {"available": False, "reason": "moneyflow 里没有这只票的近期记录"}

    margin = desk.market.margin_row(symbol, day)
    if margin:
        lag = margin.get("_lag_sessions")
        out["margin"] = {
            "available": True,
            "as_of": str(margin.get("trade_date")),
            # The exchanges publish margin detail after the close, so between the
            # bell and publication this is yesterday's balance. Saying which is
            # the difference between "no leverage" and "one session stale".
            "lag_sessions": lag,
            "financing_balance_cny": _n(margin.get("rzye"), 0),
            "financing_buy_cny": _n(margin.get("rzmre"), 0),
            "financing_repay_cny": _n(margin.get("rzche"), 0),
            "short_balance_cny": _n(margin.get("rqye"), 0),
            "net_buy_cny": _n((float(margin.get("rzmre") or 0)
                               - float(margin.get("rzche") or 0)), 0),
            "note": ("余额是存量，最近一个有数据的交易日；lag_sessions=0 表示就是当日。"
                     "两融明细由交易所收盘后发布，未发布时这里是上一交易日"),
        }
    else:
        out["margin"] = {"available": False,
                         "reason": ("margin_detail 里没有这只票在当日及以前的记录："
                                    "它不是两融标的，或这张表还没回填到近年")}
    return out


def _technicals(desk, symbol: str, day: str) -> dict:
    hist = desk.market.history(symbol, day, 260)
    if hist.empty or len(hist) < 25:
        return {"available": False, "reason": "历史不足 25 个交易日"}
    close = pd.to_numeric(hist["adj_close"], errors="coerce").astype(float)
    last = float(close.iloc[-1])
    out: dict[str, Any] = {
        "available": True,
        "as_of": str(hist.iloc[-1]["trade_date"]),
        "sessions": len(hist),
        "return_pct": {},
        "ma": {},
        "volatility_annual_pct": {},
        "range_250d": None,
    }
    # `Nd` means N sessions of return, so it needs N+1 prices. Slicing `tail(N)`
    # and dividing ends gives N-1 intervals wearing an N label, and the two
    # definitions of "the 20-day return" then disagree by one session's move —
    # the kind of discrepancy nobody notices until it is being arbitrated.
    for window in (5, 20, 60, 120, 250):
        if len(close) >= window + 1:
            out["return_pct"][f"{window}d"] = _n(
                (last / float(close.iloc[-(window + 1)]) - 1) * 100, 2)
    for window in (20, 60, 200):
        if len(close) >= window:
            ma = float(close.tail(window).mean())
            out["ma"][f"ma{window}"] = {
                "value": _n(ma, 3),
                "price_vs_pct": _n((last / ma - 1) * 100, 2) if ma > 0 else None,
            }
    rets = close.pct_change().dropna()
    for window in (20, 60):
        if len(rets) >= window:
            out["volatility_annual_pct"][f"{window}d"] = _n(
                float(rets.tail(window).std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100, 2)
    window = close.tail(250)
    if len(window) >= 60:
        hi, lo = float(window.max()), float(window.min())
        out["range_250d"] = {
            "high": _n(hi, 3), "low": _n(lo, 3),
            "position": _n((last - lo) / (hi - lo), 3) if hi > lo else None,
            "from_high_pct": _n((last / hi - 1) * 100, 2) if hi > 0 else None,
            "sessions": len(window),
        }
    atr = float(desk.market.atr(symbol, day, 20))
    out["atr20"] = _n(atr, 4)
    # ATR 是**价格单位**，不是百分比，而且是在前复权价里算的。读者拿真实价减
    # 2×ATR 算止损时，除权股上会得到错的数——600519 的 atr20=25.92 看着像 25.92 元，
    # 实际是复权价空间里的 25.92。给出百分比，止损距离就不用自己换算。
    out["atr20_pct"] = _n(atr / last * 100, 2) if last else None
    out["note"] = ("全部按前复权价计算，末端等于当日收盘价；收益已剔除分红送转的缺口。"
                   "atr20 是价格单位（元），不是百分比——止损距离用 atr20_pct 或 "
                   "atr20/tradability.last，别把两个口径的价格混着减。")
    return out


def _sentiment(desk, symbol: str, day: str) -> dict:
    """Today's pool membership, the last 20 sessions of 龙虎榜, latest 异动."""
    # `pools: {}` and `lhb_20d: null` used to mean two different things at once:
    # "this name was not on the list" and "the list was never fetched". The
    # difference decides whether the model may conclude "no unusual activity", so
    # every block now reports how many sessions it could actually read.
    out: dict[str, Any] = {"pools": {}, "lhb_20d": None, "anomaly": None,
                           "checked": {"pool_day": day, "pools_read": 0,
                                       "lhb_sessions_read": 0, "lhb_sessions_asked": 0,
                                       "anomaly_days_read": 0}}
    sessions = desk.market.calendar.last_n(day, 20) or []

    def _pool(dataset: str) -> dict | None:
        frame = _partition(desk, dataset, day)
        if frame is None or frame.empty:
            return None
        code_col = "code" if "code" in frame.columns else "ts_code"
        # The three 东财 pools key on a bare six-digit code ("000554"); every
        # other table in this desk uses a ts_code ("000554.SZ"). Comparing the
        # two directly meant the pool block was empty for *every* symbol — the
        # check ran, read three partitions, and matched nothing. Normalise to the
        # stem and compare that.
        stem = symbol.split(".")[0]
        codes = frame[code_col].astype(str).str.split(".").str[0]
        hit = frame[codes == stem]
        if hit.empty:
            return None
        row = hit.iloc[0].to_dict()
        keep = {k: (_n(v, 2) if isinstance(v, (int, float, np.floating, np.integer)) else v)
                for k, v in row.items() if k in
                ("name", "boards", "first_seal_time", "last_seal_time", "seal_amount",
                 "open_times", "consecutive_down", "industry", "reason", "pct_chg",
                 "turnover_rate", "limit_up_time", "max_seal_money", "continue_cnt")}
        return keep

    for name, dataset in (("limit_up", "limit_up_pool"),
                          ("broken_board", "broken_board_pool"),
                          ("limit_down", "limit_down_pool")):
        frame = _partition(desk, dataset, day)
        if frame is not None and not frame.empty:
            out["checked"]["pools_read"] += 1
        hit = _pool(dataset)
        if hit:
            out["pools"][name] = hit

    rows = []
    out["checked"]["lhb_sessions_asked"] = len(sessions[-20:])
    for session in sessions[-20:]:
        frame = _partition(desk, "ths_lhb", session)
        if frame is None or frame.empty:
            continue
        out["checked"]["lhb_sessions_read"] += 1
        hit = frame[frame["ts_code"].astype(str) == symbol]
        if not hit.empty:
            row = hit.iloc[0]
            rows.append({"day": session, "net_value": _n(row.get("net_value"), 0),
                         "reason": str(row.get("reason") or "")})
    if rows:
        out["lhb_20d"] = {
            "appearances": len(rows),
            "net_value_cny": _n(sum(r["net_value"] or 0 for r in rows), 0),
            "latest": rows[-1],
        }

    for session in (sessions or [])[::-1][:5]:
        frame = _partition(desk, "ths_anomaly", session)
        if frame is None or frame.empty:
            continue
        out["checked"]["anomaly_days_read"] += 1
        hit = frame[frame["ts_code"].astype(str) == symbol] if "ts_code" in frame.columns else frame
        if not hit.empty:
            row = hit.iloc[0]
            out["anomaly"] = {"day": session, "tag": str(row.get("tag") or ""),
                              "analysis": str(row.get("analysis") or "")}
            break
    if out["checked"]["lhb_sessions_read"] == 0:
        out["lhb_note"] = "近 20 个交易日的龙虎榜分区一个都没读到，不能据此判断「没上榜」"
    elif out["lhb_20d"] is None:
        out["lhb_note"] = (f"已读 {out['checked']['lhb_sessions_read']}/"
                           f"{out['checked']['lhb_sessions_asked']} 个交易日，本票未上榜")
    return out


_MARKET_WORDS = ("大盘", "股指", "两市", "沪指", "深证", "创业板指", "科创板", "A股",
                 "港股", "美股", "北向", "主力资金", "行业", "板块", "指数")


def news_scope(title: str, name: str = "", industry: str = "") -> tuple[str, str]:
    """Whose news this is, and the word that decided it.

    `tagged_only` is the honest default: the upstream `ts_code` is a concept tag,
    not an attribution, and a wall of headlines was once read as if it were.

    Two things the first version got wrong. 「银行间」 is a funding market, not a
    bank, so an industry match must not be followed by 间. And the market
    vocabulary stopped at 行业/板块, so 「沪指站上 3600 点」 fell through to
    `tagged_only` — reading as "about this stock" when it was about the index.
    """
    text = str(title or "")
    short = name[:-1] if len(name) > 2 and name[-1] in "AB" else ""
    for candidate in (name, short):
        if candidate and candidate in text:
            return "company", candidate
    if industry:
        hit = re.search(re.escape(industry) + r"(?!间)", text)
        if hit:
            return "industry", hit.group(0)
    for word in _MARKET_WORDS:
        if word in text:
            return "market", word
    return "tagged_only", ""


def _events(desk, symbol: str, day: str, limit: int = 4) -> dict:
    """Corporate actions around today, and the most recent news tagged to this name.

    The news block used to be called `recent_filings`, which it is not. The
    vendor's `ts_code` on the `news` table is a *tag*, not a filing link: of the
    ten rows tagged to 002966.SZ, the titles are 「银行行业今日净流入资金2.75亿元」,
    「科技股不香了？有基金经理止损科技 转身重仓银行股」 and one about 中国石油. A
    model told these are the company's filings will attribute industry flow to a
    specific bank. The block is now `recent_news`, every row says what it is
    actually about, and the tag itself is described.
    """
    actions: list[dict] = []
    try:
        frame = _read_cached(desk, "dividend")
        if frame is not None and not frame.empty:
            mine = frame[(frame["ts_code"] == symbol)
                         & (frame["div_proc"].astype(str) == "实施")].copy()
            mine["ann_date"] = mine["ann_date"].astype(str)
            mine = mine[mine["ann_date"] <= str(day)]
            mine["ex_date"] = mine["ex_date"].astype(str)
            mine = mine[mine["ex_date"].str.len() == 8]
            mine["gap"] = (pd.to_datetime(day) - pd.to_datetime(mine["ex_date"])).dt.days
            past = mine[(mine["gap"] >= 0)].sort_values("gap").head(2)
            future = mine[(mine["gap"] < 0)].sort_values("gap", ascending=False).head(2)
            for _, row in pd.concat([past, future]).iterrows():
                actions.append({
                    "ex_date": str(row["ex_date"]),
                    "days_from_today": int(row["gap"]),
                    "cash_per_share": _n(row.get("cash_div"), 4),
                    "bonus_per_share": _n(row.get("stk_div"), 4),
                    "pay_date": str(row.get("pay_date") or "") or None,
                })
    except (KeyError, FileNotFoundError, ValueError) as exc:
        raise DatasetReadError("dividend", exc) from exc

    name = desk.market.names().get(symbol, "")
    industry = desk.market.industry_of(symbol)
    news: list[dict] = []
    news_error: str | None = None
    try:
        frame = _read_cached(desk, "news", start=day[:4] + "0101", end=day)
        if frame is not None and not frame.empty and "ts_code" in frame.columns:
            mine = frame[frame["ts_code"].astype(str) == symbol].copy()
            if not mine.empty:
                sort_col = "pub_date" if "pub_date" in mine.columns else mine.columns[0]
                mine = mine.sort_values(sort_col, ascending=False)
                # The same wire copy is republished on consecutive days
                # (「科技股不香了？」 on 10-26 and again on 10-27), so five slots
                # can hold two stories. Keep the newest occurrence of each title.
                mine = mine.drop_duplicates(subset=["title"], keep="first").head(limit)
                for _, row in mine.iterrows():
                    title = str(row.get("title") or "")
                    scope, scope_basis = news_scope(title, name, industry)
                    news.append({
                        "date": str(row.get("pub_date") or ""),
                        "title": title[:120],
                        "scope": scope,
                        "scope_basis": scope_basis,
                        "summary": str(row.get("summary") or "")[:100] or None,
                        "source": str(row.get("source") or ""),
                    })
    except DatasetReadError as exc:
        # News is an optional attribution aid. Its absence must not hide valid
        # corporate actions and filings, but an empty list alone would again
        # confuse "not collected" with "no relevant news". Keep the error local
        # to this block and expose it in the return value.
        news_error = str(exc)

    filings: list[dict] = []
    # 6 -> 5：六条公告在最长的那只票上把整个档案顶到 16.5 KB，而宿主 16 KB 以上
    # 是整包丢弃（再退化成每块 6 行/120 字）。少一条最旧的，换整包完整送达。
    filing_limit = 5
    try:
        rows = desk.market.announcements(symbol, day, limit=filing_limit)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        raise DatasetReadError("anns", exc) from exc
    for row in rows:
        ann_id = str(row.get("announcement_id") or "")
        cached = desk.market.announcement_text(ann_id) if ann_id else None
        title = str(row.get("title") or "")
        entry = {
            "announcement_id": ann_id,
            "pub_date": str(row.get("pub_date") or ""),
            "category": str(row.get("category") or ""),
            "kind": str(row.get("kind") or "公告"),
            "stage": str(row.get("stage")) if row.get("stage") else None,
            "paperwork": bool(int(row.get("paperwork") or 0)),
            "title": title,
            "pdf_url": str(row.get("pdf_url") or ""),
            "size_kb": _n(row.get("size_kb"), 0),
        }
        # Which word produced each label. Without this a wrong label is
        # indistinguishable from a right one, and the reader has to trust the
        # table instead of checking it.
        try:
            from ..adapters.cninfo_adapter import labels

            # 四个标签各自的命中词。没命中的留空字符串，而六条公告里常常一个都没命中——
            # 那就是每行 60 字节的占位。只留真正有词的那几项；全空时整个键不出现，
            # 读者看到 key 缺失就知道"标签不是从标题推出来的"，比看四个空串更准。
            basis = {k: v for k, v in (labels(title)["basis"] or {}).items() if v}
            if basis:
                entry["basis"] = basis
        except (ImportError, KeyError, TypeError):
            pass
        if cached and cached.get("text"):
            # The excerpt is the first 420 characters and says so. A filing's
            # first paragraph carries the announcement number, the parties and
            # the subject; the numbers that decide materiality are further in,
            # which is why `full_text` stays available by id.
            entry["text"] = {
                "available": True,
                "chars": int(cached.get("chars") or 0),
                "pages_read": int(cached.get("pages_read") or 0),
                "pages_empty": int(cached.get("pages_empty") or 0),
                # 420 是"够判断相关性"的长度，但六条公告各 420 字就是 7.5 KB，
                # 而整个 profile 要挤在 16 KB 内：实测 300750 的档案 16,795 B，
                # 超过后宿主把它 shrink 成每块 6 行/120 字——读者拿到的是被截断的
                # 整包，而不是短一点的完整包。320 字仍覆盖首段的当事方与事由。
                "excerpt": " ".join(str(cached["text"]).split())[:220],
                "truncated": len(str(cached["text"])) > 220,
            }
        else:
            entry["text"] = {"available": False,
                             "reason": "正文未下载（scripts/fetch_anns_text.py）"}
        filings.append(entry)

    # Forward-looking fundamentals. These are *predictions* (预告) or
    # preliminary numbers (快报), never reported results: keep them in their own
    # block so a reader cannot mistake a guidance range for an achieved quarter.
    guidance: list[dict] = []
    guidance_errors: dict[str, str] = {}
    for api, kind in (("forecast", "业绩预告"), ("express", "业绩快报")):
        try:
            frame = _read_cached(desk, api)
        except DatasetReadError as exc:
            guidance_errors[api] = str(exc)
            continue
        if frame is None or frame.empty or "ts_code" not in frame.columns:
            continue
        mine = frame[(frame["ts_code"].astype(str) == symbol)
                     & (frame["ann_date"].astype(str) <= str(day))].copy()
        if mine.empty:
            continue
        mine = mine.sort_values(["ann_date", "end_date"]).tail(3)
        for _, row in mine.iterrows():
            guidance.append({
                "kind": kind,
                "ann_date": str(row.get("ann_date") or ""),
                "period": str(row.get("end_date") or ""),
                "type": str(row.get("type") or "") or None,
                "p_change_min_pct": _n(row.get("p_change_min"), 2),
                "p_change_max_pct": _n(row.get("p_change_max"), 2),
                "net_profit_min_wan": _n(row.get("net_profit_min"), 2),
                "net_profit_max_wan": _n(row.get("net_profit_max"), 2),
                "summary": str(row.get("summary") or "")[:200] or None,
                "change_reason": str(row.get("change_reason") or "")[:200] or None,
                "basis": "预测/初步数，不是已实现业绩；按 ann_date <= 当日 过滤",
            })
    guidance.sort(key=lambda g: g["ann_date"], reverse=True)

    out = {
        "corporate_actions": actions,
        "announcements": filings,
        "guidance": guidance,
        "announcements_note": (
            "公告来自巨潮资讯网，日期只有日级精度：当天发布的公告可能在下班后才到，"
            "所以拿它做盘中回放最多会多看几小时。四个标签都从标题关键词推，不是官方分类："
            "category 说的是什么事，kind 是什么体裁，stage 是这件事走到哪一步"
            "（多数标题没有阶段标记，stage 为 null），paperwork 表示文件本身不带来新事实"
            "（议程、章程、意见书、摘要之类）。basis 是每个标签实际命中的那个词，"
            "标签可疑时先看它。是否重大要看正文，text.excerpt 只截了前 220 字——"
            "全文用 quant_filing(announcement_id) 分段读，每次约 4,000 字。"),
        "recent_news": news,
        "news_note": ("ts_code 是上游打的概念标签，不是公告归属。scope=company 表示标题里出现了"
                      "本票名称；scope=tagged_only 表示只有标签、标题与本票无关，不能当作公司公告。"
                      "scope_basis 是判定的那个词，空字符串表示没有找到任何依据。"),
    }
    # A successful read keeps the original wire shape: null error fields add no
    # information, and their bytes can push a marginal dossier into dropping a
    # whole block.  Degraded reads still state exactly which optional input failed.
    if guidance_errors:
        out["guidance_status"] = "error" if len(guidance_errors) == 2 else "degraded"
        out["guidance_errors"] = guidance_errors
    if news_error:
        out["news_status"] = "error"
        out["news_error"] = news_error
    return out


#: Tokens for reader implementations, one per function object, never reused.
_READER_TOKENS = count(1)


def _reader_fingerprint(desk, name: str) -> int:
    """Identity of the *implementation* behind ``desk.data.<name>``.

    A bound method's ``__func__`` is the stable function on the class, so every
    real reader keeps one key for the process. A monkeypatched lambda has no
    ``__func__`` and is its own object; it gets a token written onto it, because
    ``id(fn)`` is a memory address and CPython reuses it as soon as the object is
    collected — 50 lambdas created in a loop produced **one** distinct id. The
    frame cache lives on ``desk.market`` and is shared for a whole session, so a
    replacement reader could inherit an entry built by another one: on a full
    ``tests/trading`` run the "empty fina_indicator" case answered
    ``available: true`` while passing alone.

    Putting this token in the cache key means replacing the reader (a test
    double, a new store backend) invalidates the cached frames instead of
    silently serving the old one.
    """
    fn = getattr(getattr(desk, "data", None), name, None)
    target = getattr(fn, "__func__", fn)
    if target is None:
        return 0
    token = getattr(target, "_quant_reader_token", None)
    if token is None:
        token = next(_READER_TOKENS)
        try:
            target._quant_reader_token = token
        except AttributeError:      # a C function has no attribute slot
            return id(target)
    return token


def _read_cached(desk, dataset: str, **filters):
    """Dataset read through the market cache.

    ``filters`` go into the key: `news` is read with a year range, and a cache
    keyed only by dataset would answer a January request with a June frame.
    """
    suffix = ":".join(f"{k}={v}" for k, v in sorted(filters.items()))
    key = (f"table:{dataset}:r{_reader_fingerprint(desk, 'read')}"
           + (f":{suffix}" if suffix else ""))

    def build():
        try:
            return desk.data.read(dataset, **filters)
        except (KeyError, FileNotFoundError, ValueError) as exc:
            raise DatasetReadError(dataset, exc) from exc

    # 测试里的 fake desk 是 SimpleNamespace，没有 market/缓存；真实 desk 一定有。
    # 缓存是优化而非语义，缺失时直读，别让一个只读页面依赖宿主类型。
    cache = getattr(getattr(desk, "market", None), "_cached", None)
    return cache(key, build) if cache is not None else build()


def _partition(desk, dataset: str, part: str):
    """One partition through the same cache.

    ``_sentiment`` reads twenty sessions of 龙虎榜, five of 异动, and each of the
    three pool partitions **twice** (once to see whether it exists, once to find
    the symbol). Every one of those was a fresh parquet read per profile request.
    A missing partition is cached as ``None`` too — otherwise a dataset that does
    not exist gets re-opened twenty times on every page load.
    """
    key = (f"partition:{dataset}:{part}"
           f":r{_reader_fingerprint(desk, 'read_partition')}")

    def build():
        try:
            return desk.data.read_partition(dataset, part)
        except (KeyError, FileNotFoundError, ValueError) as exc:
            raise DatasetReadError(f"{dataset}[{part}]", exc) from exc

    cache = getattr(getattr(desk, "market", None), "_cached", None)
    return cache(key, build) if cache is not None else build()


def _ownership(desk, symbol: str, day: str) -> dict:
    """Who holds this stock, and what supply is about to arrive.

    The gap this fills was the largest one left: every figure in the profile
    described the *price*, and none of them described the *float*. In A-shares a
    good part of "why did it move" is who was holding it and what is about to
    become sellable — a 90-day unlock calendar is a known supply shock, and a
    falling holder count is the cheapest available proxy for accumulation.

    Three sub-blocks, each independently available:

    * ``holders`` — 股东户数. Published at irregular dates (some companies file
      ten-day snapshots), so the comparison is against the nearest earlier
      observation rather than "the previous quarter".
    * ``top10_float`` — 十大流通股东 as of the latest disclosed period: their
      combined share of the float, how it changed, and who they are. 香港中央结算
      appears in this list when the name is northbound-eligible, which is the
      only stock-level northbound figure this token can reach.
    * ``unlocks`` — 限售解禁 in the next 120 sessions, by 解禁日 not 公告日.

    None of it is a conclusion. A high concentration is not "good"; it is a fact
    about who can sell.
    """
    out: dict[str, Any] = {"holders": {"available": False}, "top10_float": {"available": False},
                           "unlocks": {"available": False}}

    # ---------------------------------------------------------------- holders
    frame = _read_cached(desk, "stk_holdernumber")
    if frame is not None and not frame.empty and "ts_code" in frame.columns:
        mine = frame[frame["ts_code"] == symbol].copy()
        if not mine.empty:
            mine["ann_date"] = mine["ann_date"].astype(str)
            mine["end_date"] = mine["end_date"].astype(str)
            known = mine[mine["ann_date"] <= str(day)].sort_values("end_date")
            if not known.empty:
                latest = known.iloc[-1]
                num = float(latest["holder_num"]) if pd.notna(latest["holder_num"]) else None
                # The prior observation at least 60 days older, so a burst of
                # ten-day filings does not read as a collapse in one week.
                earlier = known[
                    pd.to_datetime(known["end_date"]) <= pd.to_datetime(str(latest["end_date"]))
                    - pd.Timedelta(days=60)]
                prev = float(earlier.iloc[-1]["holder_num"]) if not earlier.empty else None
                share = desk.market.basic_row(symbol, day).get("total_share")
                per_holder = (float(share) * 1e4 / num
                              if share is not None and pd.notna(share) and num else None)
                out["holders"] = {
                    "available": True,
                    "as_of": str(latest["end_date"]),
                    "announced_on": str(latest["ann_date"]),
                    "holder_count": int(num) if num else None,
                    "previous": int(prev) if prev else None,
                    "previous_as_of": (str(earlier.iloc[-1]["end_date"])
                                       if prev else None),
                    "change_pct": _n((num / prev - 1) * 100, 2) if num and prev else None,
                    "avg_shares_per_holder": _n(per_holder, 0),
                    "note": ("户数下降 = 筹码向少数人集中；上升 = 分散。"
                             "区间按最近一次披露与至少 60 天前的一次比较，"
                             "不是固定季度对比"),
                }

    if not out["holders"].get("available"):
        out["holders"] = {
            "available": False,
            "reason": ("stk_holdernumber 里没有这只票在当日之前已披露的户数"
                       if frame is not None and not frame.empty
                       else "stk_holdernumber 尚未回填"),
        }

    # ------------------------------------------------------------ top10 float
    frame = _read_cached(desk, "top10_floatholders")
    if frame is not None and not frame.empty and "ts_code" in frame.columns:
        mine = frame[frame["ts_code"] == symbol].copy()
        if not mine.empty:
            mine["end_date"] = mine["end_date"].astype(str)
            mine["ann_date"] = mine["ann_date"].astype(str)
            known = mine[(mine["end_date"] <= str(day)) & (mine["ann_date"] <= str(day))]
            if not known.empty:
                periods = sorted(known["end_date"].unique())
                latest = known[known["end_date"] == periods[-1]]
                prev = known[known["end_date"] == periods[-2]] if len(periods) > 1 else None
                ratio = pd.to_numeric(latest["hold_float_ratio"], errors="coerce")
                change = pd.to_numeric(latest["hold_change"], errors="coerce")
                types = latest.assign(_r=ratio).groupby("holder_type")["_r"].sum()
                north = latest[latest["holder_name"].astype(str).str.contains("香港中央结算")]
                raised = int((change > 0).sum())
                cut = int((change < 0).sum())
                entry: dict[str, Any] = {
                    "available": True,
                    "as_of": periods[-1],
                    "holders_reported": len(latest),
                    "combined_float_pct": _n(ratio.sum(), 2),
                    "combined_change_shares": _n(change.sum(), 0),
                    "raised": raised, "cut": cut,
                    "by_type_float_pct": {str(k): _n(v, 2) for k, v in types.items()},
                    "northbound_holder": ({
                        "name": str(north.iloc[0]["holder_name"]),
                        "float_pct": _n(north.iloc[0]["hold_float_ratio"], 3),
                        "change_shares": _n(north.iloc[0]["hold_change"], 0),
                    } if not north.empty else None),
                    "note": ("hold_amount 单位是股；combined_float_pct 是前十大合计占流通比。"
                             "香港中央结算出现在名单里时，它的持股就是这只票的北向口径。"),
                }
                if prev is not None:
                    prev_ratio = pd.to_numeric(prev["hold_float_ratio"], errors="coerce")
                    entry["previous_as_of"] = periods[-2]
                    entry["combined_float_pct_change"] = _n(
                        float(ratio.sum()) - float(prev_ratio.sum()), 2)
                out["top10_float"] = entry
    if not out["top10_float"].get("available"):
        out["top10_float"] = {
            "available": False,
            "reason": ("top10_floatholders 里没有这只票在当日之前已披露的期次"
                       if frame is not None and not frame.empty
                       else "top10_floatholders 尚未回填"),
        }

    # ---------------------------------------------------------------- unlocks
    frame = _read_cached(desk, "share_float")
    if frame is not None and not frame.empty and "float_date" in frame.columns:
        mine = frame[frame["ts_code"] == symbol].copy()
        if not mine.empty:
            mine["float_date"] = mine["float_date"].astype(str)
            mine["ann_date"] = mine["ann_date"].astype(str)
            mine = mine[mine["ann_date"] <= str(day)]
            ahead = mine[mine["float_date"] > str(day)].copy()
            # One row in 326,938 carries float_ratio = 14,845.7 (002116.SZ,
            # 2026-12-29, float_share 8.3e10). A single unlock cannot exceed
            # 100% of total shares, so it is a vendor error — and left in, it
            # turns "0.3% of the float unlocks in 180 days" into 14,846%.
            ratio_all = pd.to_numeric(ahead["float_ratio"], errors="coerce")
            suspect = ratio_all > 100
            suspect_count = int(suspect.sum())
            ahead = ahead[~suspect].sort_values("float_date")
            covered_through = str(frame["float_date"].astype(str).max())
            if ahead.empty:
                out["unlocks"] = {
                    "available": False,
                    "reason": (f"截至 {covered_through} 没有可用的解禁记录"
                               + (f"（另有 {suspect_count} 行因比例超过 100% 被剔除）"
                                  if suspect_count else "")),
                    "checked_through": covered_through,
                    "excluded_suspect_rows": suspect_count,
                }
            if not ahead.empty:
                horizon = (pd.to_datetime(str(day)) + pd.Timedelta(days=180)).strftime("%Y%m%d")
                window = ahead[ahead["float_date"] <= horizon]
                nearest = ahead.iloc[0]
                ratio = pd.to_numeric(ahead["float_ratio"], errors="coerce")
                out["unlocks"] = {
                    "available": True,
                    "excluded_suspect_rows": suspect_count,
                    "next_float_date": str(nearest["float_date"]),
                    "days_until": int((pd.to_datetime(str(nearest["float_date"]))
                                       - pd.to_datetime(str(day))).days),
                    "next_float_shares": _n(nearest["float_share"], 0),
                    "next_float_pct_of_total": _n(nearest["float_ratio"], 3),
                    "next_holder": str(nearest.get("holder_name") or "") or None,
                    "within_180d_events": len(window),
                    "within_180d_shares": _n(pd.to_numeric(window["float_share"],
                                                           errors="coerce").sum(), 0),
                    "within_180d_pct_of_total": _n(
                        pd.to_numeric(window["float_ratio"], errors="coerce").sum(), 3),
                    "all_future_events": len(ahead),
                    "largest_future": _n(ratio.max(), 3) if ratio.notna().any() else None,
                    "note": ("float_ratio 是解禁股数占总股本的比例（%）；"
                             "按解禁日（float_date）筛，不是公告日。解禁不等于减持。"),
                }
    if not out["unlocks"].get("available") and not out["unlocks"].get("reason"):
        covered = (str(frame["float_date"].astype(str).max())
                   if frame is not None and not frame.empty else None)
        has_rows = (frame is not None and not frame.empty
                    and bool((frame["ts_code"] == symbol).any()))
        if covered and has_rows:
            reason = f"该票在 {covered} 之前没有未来解禁记录"
        elif covered:
            reason = (f"这只票在 share_float 里没有任何记录（表覆盖到 {covered}）；"
                      f"不代表不会解禁，只代表这几年的公告里没有")
        else:
            reason = "share_float 尚未回填"
        out["unlocks"] = {"available": False, "reason": reason,
                          "checked_through": covered}

    # ----------------------------------------------------------------- pledge
    pledge_frame = _read_cached(desk, "pledge_stat")
    pledge_ready = (pledge_frame is not None and not pledge_frame.empty
                    and "pledge_ratio" in pledge_frame.columns)
    if pledge_ready:
        mine = pledge_frame[pledge_frame["ts_code"] == symbol].copy()
        if not mine.empty:
            mine["end_date"] = mine["end_date"].astype(str)
            known = mine[mine["end_date"] <= str(day)].sort_values("end_date")
            if not known.empty:
                latest = known.iloc[-1]
                ratio = float(latest["pledge_ratio"])
                same_day = pledge_frame[
                    pledge_frame["end_date"].astype(str) == str(latest["end_date"])]
                entry: dict[str, Any] = {
                    "available": True,
                    "as_of": str(latest["end_date"]),
                    "pledge_ratio_pct": _n(ratio, 2),
                    "pledged_shares": _n((float(latest.get("unrest_pledge") or 0)
                                          + float(latest.get("rest_pledge") or 0)) * 1e4, 0),
                    "restricted_pledged_shares": _n(
                        float(latest.get("rest_pledge") or 0) * 1e4, 0),
                    "pledge_events": (int(latest["pledge_count"])
                                      if pd.notna(latest.get("pledge_count")) else None),
                    "market_median_same_day_pct": _n(
                        pd.to_numeric(same_day["pledge_ratio"], errors="coerce").median(), 2),
                    "peers_reporting": int(pd.to_numeric(
                        same_day["pledge_ratio"], errors="coerce").notna().sum()),
                    "note": ("pledge_ratio 是质押股数占总股本的比例；unrest_pledge / "
                             "rest_pledge 原始单位是万股，这里已换算成股。"
                             "表里只有发生过质押的公司，未质押的票不会出现。"),
                }
                if len(known) > 1:
                    prev = known.iloc[-2]
                    entry["previous_as_of"] = str(prev["end_date"])
                    entry["pledge_ratio_change_pp"] = _n(
                        ratio - float(prev["pledge_ratio"]), 2)
                out["pledge"] = entry
    if "pledge" not in out:
        out["pledge"] = {
            "available": False,
            "reason": ("这只票不在 pledge_stat 的季末快照里——通常表示当期没有股权质押，"
                       "不是数据缺失" if pledge_ready else "pledge_stat 尚未回填"),
        }

    # ---------------------------------------------------------------- insider
    trade_frame = _read_cached(desk, "stk_holdertrade")
    trade_ready = (trade_frame is not None and not trade_frame.empty
                   and "in_de" in trade_frame.columns)
    if trade_ready:
        mine = trade_frame[trade_frame["ts_code"] == symbol].copy()
        if not mine.empty:
            mine["ann_date"] = mine["ann_date"].astype(str)
            horizon = (pd.to_datetime(str(day))
                       - pd.Timedelta(days=180)).strftime("%Y%m%d")
            recent = mine[(mine["ann_date"] <= str(day))
                          & (mine["ann_date"] >= horizon)].copy()
            if not recent.empty:
                vol = pd.to_numeric(recent["change_vol"], errors="coerce").fillna(0)
                sign = recent["in_de"].map({"IN": 1.0, "DE": -1.0}).fillna(0.0)
                net = float((vol * sign).sum())
                recent["_abs"] = vol.abs()
                top = recent.nlargest(3, "_abs")
                out["insider"] = {
                    "available": True,
                    "window_days": 180,
                    "events": len(recent),
                    "buy_events": int((recent["in_de"] == "IN").sum()),
                    "sell_events": int((recent["in_de"] == "DE").sum()),
                    "net_shares": _n(net, 0),
                    "direction": "净增持" if net > 0 else ("净减持" if net < 0 else "持平"),
                    "largest": [{
                        "date": str(row["ann_date"]),
                        "holder": str(row.get("holder_name") or "")[:40],
                        "kind": "增持" if row["in_de"] == "IN" else "减持",
                        "shares": _n(row.get("change_vol"), 0),
                        "pct_of_total": _n(row.get("change_ratio"), 4),
                        "avg_price": _n(row.get("avg_price"), 3),
                    } for _, row in top.iterrows()],
                    "note": ("change_vol 单位是股，change_ratio 是占总股本比例（%）。"
                             "这是公告口径，含高管、大股东与员工持股平台的二级市场交易。"),
                }
                # A single 31%-of-company "减持" is a block transfer or a judicial
                # assignment, not selling into the tape — 600707.SH moved 1.11bn
                # shares that way on 2026-09-11 with no average price recorded.
                # Reporting it as supply hitting the market would be wrong by an
                # order of magnitude.
                big = pd.to_numeric(top["change_ratio"], errors="coerce").max()
                out["insider"]["largest_single_pct_of_total"] = _n(big, 3)
                if pd.notna(big) and float(big) >= 5:
                    out["insider"]["caveat"] = (
                        f"最大一笔占总股本 {float(big):.2f}%，这个量级通常是协议转让、"
                        f"大宗或司法划转，不是二级市场集中竞价卖出；"
                        f"avg_price 为空也指向场外成交。")
    if "insider" not in out:
        out["insider"] = {
            "available": False,
            "reason": ("近 180 天没有增减持公告" if trade_ready
                       else "stk_holdertrade 尚未回填"),
            "window_days": 180,
        }
    return out


def _position(desk, symbol: str, day: str) -> dict:
    summary = desk.book.summary()
    held = next((p for p in summary.get("positions", [])
                 if p.get("symbol") == symbol), None)
    if held is None:
        return {"held": False}
    # Key names must follow ``book.positions()``: ``qty`` / ``weight`` /
    # ``unrealized_pnl``. Reading ``total_qty`` / ``weight_pct`` / ``unrealized_pct``
    # returned None for all three — so "how many shares do I hold, at what weight,
    # up how much" were blank in every per-name profile, and the blanks looked
    # like missing data rather than a wrong key.
    qty = held.get("qty")
    weight = held.get("weight")
    unrealized = held.get("unrealized_pnl")
    cost = _n(held.get("avg_cost"), 4)
    unrealized_pct = None
    try:
        if qty and cost and unrealized is not None:
            basis = float(qty) * float(cost)
            if basis > 0:
                unrealized_pct = round(float(unrealized) / basis * 100, 2)
    except (TypeError, ValueError):
        unrealized_pct = None
    out = {
        "held": True,
        "qty": qty,
        "available_qty": held.get("available_qty"),
        "avg_cost": cost,
        "last_price": _n(held.get("last_price"), 3),
        "market_value": _n(held.get("market_value"), 2),
        "weight_pct": None if weight is None else round(float(weight) * 100, 2),
        "unrealized_pnl": _n(unrealized, 2),
        "unrealized_pct": unrealized_pct,
    }
    try:
        from .stops import open_stops

        view = next((v for v in open_stops(desk, day) if v.symbol == symbol), None)
        if view is not None:
            out["stop"] = {
                "price": _n(view.stop, 3),
                "distance_pct": _n((float(view.last / view.stop) - 1) * 100, 2)
                if view.stop else None,
                "breached": bool(view.last <= view.stop),
            }
    except (ValueError, KeyError):
        pass
    return out


def _peers(desk, symbol: str, day: str) -> dict:
    """Where this name sits inside its own industry, today.

    A percentile against its own history says "cheap for this stock"; it cannot
    say "cheap for this business". That needs the cross-section, which is what
    `daily_basic` holds for every listed name on the same session — a comparison
    no single-name endpoint can produce.

    The set is 申万 level 3 where it is big enough to summarise, because
    `stock_basic.industry` lumps 42 banks together and a city commercial bank does
    not trade like ICBC. `level` and `peers_used` report which bucket was used, so
    no median is ever quoted without the set it came from.
    """
    frame = desk.market.basic_day(day)
    if frame is None or frame.empty:
        return {"available": False, "reason": f"{day} 没有 daily_basic 截面"}
    mine = frame[frame["ts_code"] == symbol]
    if mine.empty:
        return {"available": False, "reason": f"{symbol} 不在当日截面里"}

    group = desk.market.industry_peers(symbol)
    if group.get("available"):
        members = group["members"]
        level, name = group.get("level"), group.get("name")
        skipped = group.get("skipped")
    else:
        # No 申万 row (an ETF, a name added this week): fall back to the vendor
        # label so the block still answers, and say which taxonomy was used.
        industry = desk.market.industry_of(symbol)
        if not industry or industry == "未知":
            return {"available": False,
                    "reason": group.get("reason") or "股票列表里没有行业分类"}
        basic = desk.market.basic()
        members = set(basic[basic["industry"].astype(str) == industry]
                      ["ts_code"].astype(str))
        level, name, skipped = "vendor", industry, None

    peers = frame[frame["ts_code"].astype(str).isin(set(members))]
    if len(peers) < 5:
        return {"available": False,
                "reason": f"{name} 只有 {len(peers)} 只票，中位数没有意义"}

    def _median(col: str, positive_only: bool = False):
        series = pd.to_numeric(peers[col], errors="coerce")
        if positive_only:
            series = series.where(series > 0)
        series = series.dropna()
        if series.empty:
            return None, 0
        return float(series.median()), len(series)

    me = mine.iloc[0]
    out: dict[str, Any] = {
        "available": True,
        "industry": name,
        "taxonomy": "申万2021" if level != "vendor" else "stock_basic.industry",
        "level": level,
        "skipped_levels": skipped,
        "as_of": day,
        "peer_count": len(peers),
        "median": {},
        "mine": {},
        "rank_pct": {},
    }
    # total_mv arrives in 万元; the valuation block reports CNY, so this one does
    # too. Two units for the same quantity in one payload is how a reader ends up
    # comparing 1.69e8 with 1.69e12 and concluding the company shrank.
    scale = {"total_mv": 1e4}
    for col in ("pe_ttm", "pb", "total_mv", "turnover_rate"):
        factor = scale.get(col, 1.0)
        med, n = _median(col, positive_only=col in ("pe_ttm",))
        out["median"][col] = _n(med * factor, 0 if factor != 1.0 else 3) if med is not None else None
        out["median"][f"{col}_n"] = n
        value = pd.to_numeric(pd.Series([me.get(col)]), errors="coerce").iloc[0]
        out["mine"][col] = (_n(float(value) * factor, 0 if factor != 1.0 else 3)
                            if math.isfinite(float(value)) else None)
        series = pd.to_numeric(peers[col], errors="coerce").dropna()
        if col == "pe_ttm":
            series = series.where(series > 0).dropna()
        current = float(value)
        if series.empty or not math.isfinite(current) or (col == "pe_ttm" and current <= 0):
            out["rank_pct"][col] = None
        else:
            out["rank_pct"][col] = round(float((series <= current).mean()), 3)

    # Who the median is made of. A median tells you where a name sits; it does
    # not tell you what to compare it with, and "the 4 nearest by market cap" is
    # a question a model cannot answer from a median.
    names = desk.market.names()
    daily = desk.market.daily(day)
    pct_by_symbol: dict[str, float] = {}
    if daily is not None and not daily.empty:
        d = daily.copy()
        close = pd.to_numeric(d["close"], errors="coerce")
        pre = pd.to_numeric(d["pre_close"], errors="coerce")
        chg = close / pre.where(pre > 0) - 1
        pct_by_symbol = {str(k): float(v) for k, v in zip(d["ts_code"], chg)
                         if pd.notna(v)}
    others = peers[peers["ts_code"].astype(str) != symbol].copy()
    mine_mv = float(me.get("total_mv") or 0)
    if not others.empty and mine_mv > 0:
        ratio = pd.to_numeric(others["total_mv"], errors="coerce") / mine_mv
        others["gap"] = ratio.where(ratio > 0).apply(
            lambda x: abs(math.log(x)) if pd.notna(x) else float("inf"))
        comps = []
        for _, row in others.nsmallest(4, "gap").iterrows():
            code = str(row["ts_code"])
            mv = row.get("total_mv")
            comps.append({
                "symbol": code,
                "name": names.get(code, ""),
                "total_mv_cny": _n(float(mv) * 1e4, 0) if pd.notna(mv) else None,
                "size_vs_mine": _n(float(mv) / mine_mv, 3) if pd.notna(mv) else None,
                "pe_ttm": _n(row.get("pe_ttm"), 2),
                "pb": _n(row.get("pb"), 3),
                "change_pct": _n(pct_by_symbol.get(code, float("nan")) * 100, 2),
            })
        out["comparables"] = comps
        out["comparables_note"] = "同行业、总市值最接近的 4 只；按市值距离排序，不是推荐"

    # Same-day move of the industry, so "it fell 3%" has a reference.
    daily = desk.market.daily(day)
    if daily is not None and not daily.empty:
        d = daily[daily["ts_code"].astype(str).isin(set(members))].copy()
        close = pd.to_numeric(d["close"], errors="coerce")
        pre = pd.to_numeric(d["pre_close"], errors="coerce")
        chg = (close / pre.where(pre > 0) - 1).dropna()
        if len(chg) >= 5:
            out["industry_change_pct"] = _n(float(chg.median()) * 100, 2)
            mine_chg = chg[d["ts_code"].astype(str) == symbol]
            out["mine_change_pct"] = _n(float(mine_chg.iloc[0]) * 100, 2) if len(mine_chg) else None
    out["note"] = ("同日同行业截面；total_mv 单位为元。rank_pct 是本票在同行中的位置，"
                   "0 = 同行最低。pe_ttm 的分位与中位数只统计 PE 为正的同行"
                   "（_n 后缀是实际参与计算的样本数），亏损公司的 PE 是无意义的负数。")
    return out



def _relative_strength(desk, symbol: str, day: str, windows=(20, 60)) -> dict:
    """Is this name beating its industry and the market, or just moving with them?

    `technicals.return_pct` says +15% over 60 sessions, which is information only
    next to what the other 41 banks did. Without it a model reports a sector-wide
    rally as stock picking — the arithmetic behind the finding that most
    LLM-generated "alpha" is beta with a story attached.

    Both benchmarks are equal-weight portfolios over the same sessions, built
    from `close / pre_close` so an ex-date is not a return. The industry split
    uses 申万 level 3 (widened until the bucket has at least eight names), which
    is narrower than the vendor label and therefore a harder benchmark; the level
    used is reported. Classification is today's applied to history — the table is
    a snapshot with no membership history — so a name that changed sector is
    measured against the set it is in now. That is stated rather than hidden.
    """
    panel = desk.market.panel(day, max(windows) + 1)
    if panel.empty:
        return {"available": False, "reason": "没有可用的横截面"}
    group = desk.market.industry_peers(symbol, max_size=400)
    if not group.get("available"):
        return {"available": False,
                "reason": group.get("reason") or "缺少行业分类，无法算相对强弱"}
    industry, members = group["name"], set(group["members"])

    p = panel.copy()
    close = pd.to_numeric(p["close"], errors="coerce")
    pre = pd.to_numeric(p["pre_close"], errors="coerce")
    p["ret"] = close / pre.where(pre > 0) - 1
    p = p.sort_values(["ts_code", "trade_date"])
    p["px"] = (1.0 + p["ret"].fillna(0.0)).groupby(p["ts_code"], sort=False).cumprod()

    industry_ret = (p[p["ts_code"].isin(members)].groupby("trade_date")["ret"]
                    .mean().dropna())
    market_ret = p.groupby("trade_date")["ret"].mean().dropna()
    mine = p[p["ts_code"] == symbol].set_index("trade_date")["px"]
    if mine.empty or len(industry_ret) < max(windows):
        return {"available": False, "reason": "该票或行业的交易日不足"}

    ind_cum = (1.0 + industry_ret).cumprod()
    mkt_cum = (1.0 + market_ret).cumprod()
    out: dict[str, Any] = {
        "available": True,
        "industry": industry,
        "level": group.get("level"),
        "peer_count": len(members),
        "window": {},
        "note": ("相对强弱 = 本票区间收益 - 同行业等权收益；vs_industry_pp 为正表示跑赢行业。"
                 "行业用申万分类的当前快照，无历史归属"),
    }
    for window in windows:
        if len(mine) < window + 1 or len(ind_cum) < window + 1:
            continue
        stock = float(mine.iloc[-1] / mine.iloc[-(window + 1)] - 1)
        ind = float(ind_cum.iloc[-1] / ind_cum.iloc[-(window + 1)] - 1)
        mkt = float(mkt_cum.iloc[-1] / mkt_cum.iloc[-(window + 1)] - 1)
        out["window"][f"{window}d"] = {
            "stock_pct": _n(stock * 100, 2),
            "industry_pct": _n(ind * 100, 2),
            "market_pct": _n(mkt * 100, 2),
            "vs_industry_pp": _n((stock - ind) * 100, 2),
            "vs_market_pp": _n((stock - mkt) * 100, 2),
        }
    if not out["window"]:
        return {"available": False, "reason": "窗口长度不足"}
    return out


def _intraday(desk, symbol: str, day: str, stop: float | None = None) -> dict:
    """The shape of today's session, from 5-minute bars.

    `close` is a single number; whether it was reached on a morning gap and
    held, or on a 14:55 spike, is a different fact about the same close. Two
    figures carry most of that: how much of the day's volume traded in the first
    half hour (a gap-and-hold prints 25-40% there; a drift prints 15%), and where
    the close sits relative to the volume-weighted average price.

    Coverage used to be the watchlist, which excluded exactly the names a reader
    is asking about: a candidate the screen just surfaced. So a miss now pulls
    the bars once and stores them (~0.3s), and `fetched` says which happened.
    A session the free endpoint no longer keeps is reported as such — "provider
    has no 20260801" is a different fact from "nobody collected it".

    `source` matters: 东财 rows carry 成交额 so VWAP is exact; 腾讯 rows do not,
    and VWAP is then null rather than approximated from (H+L+C)/3, which is a
    different number wearing the same name.
    """
    from service.trading.intraday import collect

    frame, how = collect(desk.market, symbol, day, fetch=True)
    if frame.empty:
        reason = {
            "not_collected": "当日没有 5 分钟线，且这次没有联网抓（fetch=False）",
            "unavailable": "当日没有 5 分钟线，东财与腾讯都没有返回",
        }.get(how.split(":")[0], None)
        if reason is None:
            if how.startswith("provider_has_no_"):
                reason = (f"{day} 的分钟线不在免费源保留范围里（东财约 32 个交易日、"
                          "腾讯约 7 个）——这是能力边界，不是当日没有交易")
            else:
                reason = f"当日没有 5 分钟线（{how}）"
        return {"available": False, "reason": reason}

    from decimal import Decimal as _D

    from service.trading.intraday import describe, session_metrics

    view = describe(frame, symbol, day, _D(str(stop)) if stop else None)
    close = pd.to_numeric(frame["close"], errors="coerce")
    vol = pd.to_numeric(frame["vol"], errors="coerce")
    source = str(frame["source"].iloc[0]) if "source" in frame.columns else "unknown"

    vwap = None
    if "amount" in frame.columns:
        amount = pd.to_numeric(frame["amount"], errors="coerce")
        if amount.notna().all() and amount.sum() > 0:
            shares = vol.sum() * SHARES_PER_LOT   # 手 -> 股
            if shares > 0:
                vwap = float(amount.sum()) / shares

    first_half_hour = frame[frame["trade_time"].str[11:16] <= "10:00"]
    total_vol = float(vol.sum()) if vol.notna().any() else 0.0
    early_share = (float(pd.to_numeric(first_half_hour["vol"], errors="coerce").sum())
                   / total_vol) if total_vol > 0 else None
    last = float(close.iloc[-1])
    metrics = session_metrics(frame)

    out: dict[str, Any] = {
        "available": True,
        "as_of": day,
        "bars": len(frame),
        "source": source,
        "bar_coverage": _n(metrics["bar_coverage"], 3),
        "complete_session": metrics["complete_session"],
        # `stored` = 表里本来就有；`fetched:eastmoney` = 这次为这个请求抓的。
        # 读者要能区分"台子一直在看"和"因为你问才去看"。
        "fetched": how,
        "first_bar": str(frame["trade_time"].iloc[0]),
        "last_bar": str(frame["trade_time"].iloc[-1]),
        "session_low": _n(float(pd.to_numeric(frame["low"], errors="coerce").min()), 3),
        "session_high": _n(float(pd.to_numeric(frame["high"], errors="coerce").max()), 3),
        "close": _n(last, 3),
        "vwap": _n(vwap, 3),
        "close_vs_vwap_pct": _n((last / vwap - 1) * 100, 2) if vwap else None,
        "first_30min_volume_share": _n(early_share, 3),
        "afternoon_volume_share": _n(metrics.get("afternoon_volume_share"), 3),
        "last_30min_volume_share": _n(metrics.get("last_30min_volume_share"), 3),
        "afternoon_return_pct": _n(metrics.get("afternoon_return_pct"), 2),
        "close_position_in_range": _n(metrics.get("close_position_in_range"), 3),
        "worst_drawdown_from_high_pct": _n((view.worst_drawdown if view else 0.0) * 100, 2),
        "stop_breach_at": view.breach_at if view else None,
        "note": ("仅为描述，非信号；午后收益基准为上午最后收盘；"
                 "complete_session=false 表示非完整日。"),
    }
    if vwap is None:
        out["vwap_note"] = (f"{source} 源的成交额缺失/全为 0 或成交量无效，"
                            "VWAP 无法精确计算，故留空")
    return out


#: The thirteen blocks, in the order a reader should take them. One tuple so
#: `sections=` and the default path cannot drift apart.
BLOCK_ORDER = ("identity", "tradability", "valuation", "peers", "relative_strength",
               "fundamentals", "flow", "technicals", "sentiment", "ownership",
               "events", "position", "intraday")


#: 每条读法开头点到的区块名，用来在分段请求时过滤。
_READING_BLOCKS = ("valuation", "peers", "relative_strength", "ownership",
                   "fundamentals", "flow", "technicals", "tradability",
                   "position", "events", "intraday", "sentiment")


def _applies(line: str, wanted: list[str]) -> bool:
    """这条读法是不是在讲本次请求里的某一块。全局说明（"没有结论"那句）永远保留。"""
    for name in _READING_BLOCKS:
        if line.startswith(name) or f"{name}." in line[:24]:
            return name in wanted
    return True


#: Bytes of blocks the default dossier may carry. The host drops a result past
#: 16,000 B rather than shortening it; the envelope (`as_of`, `how_to_read` ≈1 KB,
#: `missing`, `omitted_sections`, `size_note`) takes about 1.4 KB, so blocks get
#: 14,500 and the whole response lands near 15,900 with room to spare.
BLOCK_BUDGET = 14_500

#: What survives when the dossier does not fit — read this as "drop order,
#: reversed". Declared apart from ``BLOCK_ORDER`` because "read this first" and
#: "keep this most" are different questions. The ranking is by *unique value per
#: byte*, not by importance in the abstract:
#:
#: * `intraday` (~600 B) used to sit last because it was usually unavailable. Now
#:   that a miss is filled on demand it is the only intraday evidence the desk
#:   has, and dropping it first hid exactly the capability that had just been
#:   built: the default dossier for three of four symbols lost it while keeping
#:   `sentiment`, which two other tools reproduce.
#: * `sentiment` (1,325 B) and `peers` (1,543 B) are duplicated by `quant_pools` /
#:   `quant_lhb` and `quant_compare`. Dropping them costs the reader one call.
#: * `ownership` (2,592 B) has no other tool: 股东户数、北向席位、未来 180 天解禁.
#:   An earlier version dropped it while keeping `sentiment`, which is exactly
#:   backwards — 2.6 KB of facts nothing else can give, traded for 1.3 KB of
#:   facts available in two other tools.
KEEP_PRIORITY = ("identity", "tradability", "position", "valuation", "technicals",
                 "relative_strength", "events", "intraday", "ownership",
                 "fundamentals", "flow", "peers", "sentiment")


def _fit_blocks(blocks: dict, wanted: list[str]) -> tuple[list[str], list[str]]:
    """Which of ``wanted`` fit under ``BLOCK_BUDGET``, and which were left out.

    Returns ``(kept_in_display_order, dropped_in_display_order)``. Applied to the
    default dossier and to an explicit `sections=` request that is too large: a
    caller who asks for all thirteen blocks cannot have known the answer is 16.7
    KB, and the host's response to that is to cut strings inside them. At least
    four blocks always survive — an identity without a price and without a
    position is not a smaller dossier, it is a different document.
    """
    import json

    def size(names: list[str]) -> int:
        chunk = {name: blocks[name] for name in names}
        return len(json.dumps(chunk, ensure_ascii=False).encode("utf-8"))

    keep = [name for name in BLOCK_ORDER if name in wanted]
    while len(keep) > 4 and size(keep) > BLOCK_BUDGET:
        # Skip blocks too small to close the gap: dropping 91 bytes of `intraday`
        # to save 900 looks like a decision and isn't one.
        candidates = [name for name in reversed(KEEP_PRIORITY)
                      if name in keep and size([name]) >= 120]
        if not candidates:
            candidates = [name for name in reversed(KEEP_PRIORITY) if name in keep]
        if not candidates:                            # pragma: no cover
            break
        keep.remove(candidates[0])
    return keep, [name for name in wanted if name not in keep]


# ------------------------------------------------------------------ 计算计划
# 默认全量档案把 13 块全算完，_fit_blocks 再按 16 KB 上限丢掉末尾的
# peers / sentiment（这两个其它工具能复现）。算它们要读 20 个交易日的龙虎榜、
# 扫整日横截面，纯属白付。
#
# 做法不是"猜哪两块会被丢"——那会随当天公告正文长度变化——而是把**首次请求的
# 真实判定**按 (desk, 代码, 交易日, 请求集合, 窗口) 记 60 s：第一次照旧全算，
# 之后的同键请求只算留下的块。输出与首次逐字一致，包括 omitted_sections /
# size_note。把 desk 的 identity 放进 key，是为了测试里换一个 fake desk 注入新
# 数据时不会被上一个用例的计划钉住；交易日/日更换 day 也自然换键。
_PLAN_TTL = 60.0
_PLAN_LOCK = threading.Lock()
_PLAN_CACHE: dict[tuple, tuple[float, list[str], list[str]]] = {}
#: Tokens for desks, one per object, never reused.
_PLAN_TOKENS = count(1)


def _desk_token(desk) -> int:
    """A key component that stays this desk's identity for its whole life.

    The key used ``id(desk)``, which CPython reuses as soon as the object is
    collected: forty desks built in a loop produced **one** id, and the cache
    lives 60 seconds while every test builds a fresh desk. A case could therefore
    inherit the previous case's plan — and the plan decides which blocks are
    dropped, so the symptom is a missing section rather than a smaller answer.
    """
    token = getattr(desk, "_quant_plan_token", None)
    if token is None:
        token = next(_PLAN_TOKENS)
        try:
            desk._quant_plan_token = token
        except AttributeError:      # slots / frozen: no attribute slot
            return id(desk)
    return token


def _plan_key(desk, symbol, day, wanted, valuation_window):
    return (_desk_token(desk), symbol, str(day), tuple(wanted), int(valuation_window))


def _plan_lookup(key):
    now = time.monotonic()
    with _PLAN_LOCK:
        hit = _PLAN_CACHE.get(key)
        if hit is not None and now - hit[0] < _PLAN_TTL:
            return list(hit[1]), list(hit[2])
    return None


def _plan_store(key, kept, dropped):
    with _PLAN_LOCK:
        _PLAN_CACHE[key] = (time.monotonic(), list(kept), list(dropped))
        if len(_PLAN_CACHE) > 256:  # 按 symbol+day 增长，别让它无界
            for old in sorted(_PLAN_CACHE, key=lambda k: _PLAN_CACHE[k][0])[:64]:
                _PLAN_CACHE.pop(old, None)


def build(desk, symbol: str, day: str | None = None, *,
          valuation_window: int = 750, sections=None) -> dict:
    """Assemble the dossier. ``day`` defaults to the last session with bars.

    ``sections`` keeps the payload under the host's 16 KB ceiling by sending only
    the blocks asked for. It does **not** change any block's contents: a reader
    who asks for `events` alone gets exactly the `events` it would have got
    inside the full dossier. Splitting is a delivery decision, not a depth one.
    """
    symbol = symbol.upper().strip()
    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "没有可用的日线日期"}

    snap = desk.market.snapshot(symbol, day)
    if snap is None:
        # A suspended or not-yet-listed name is still a legitimate question, so
        # answer the parts that can be answered and say which part is empty.
        known = desk.market.names().get(symbol)
        if not known:
            return {"ok": False, "error": f"{symbol} 不在股票列表里"}
    wanted = list(BLOCK_ORDER)
    if sections:
        asked = {str(name).strip() for name in sections if str(name).strip()}
        unknown = sorted(asked - set(BLOCK_ORDER))
        if unknown:
            return {"ok": False, "error": f"没有这些区块：{unknown}",
                    "sections": list(BLOCK_ORDER)}
        wanted = [name for name in BLOCK_ORDER if name in asked]

    # 显式 sections= 是调用方点名要的，绝不能用缓存计划跳过；只有默认全量
    # 请求才复用上次的保留判定。
    plan_key = (None if sections else
                _plan_key(desk, symbol, day, list(wanted), valuation_window))
    plan = _plan_lookup(plan_key) if plan_key is not None else None

    # `intraday` needs the held stop from `position`; compute it without emitting it.
    compute = set(plan[0] if plan is not None else wanted)
    if "intraday" in compute:
        compute.add("position")

    blocks: dict[str, Any] = {}
    for name in BLOCK_ORDER:
        if name not in compute:
            continue
        try:
            if name == "identity":
                # 身份段读的是 basic / namechange / 申万分类这些日内不变的参考表，
                # 每个请求重算一遍是白付的（个股页首屏冷调用实测 1463 ms）。
                # 底层表本身已走 market._cached，这里再缓存一层"组装后的结果"。
                #
                # 键里带 sw_table 的形状：分类表被替换（日更回填、或调用方注入一张
                # 空表来测回退路径）就必须重算，否则缓存会把一次注入后的状态一直
                # 冻到 TTL 到期——这比不缓存更坏。
                sw = desk.market.sw_table()
                fingerprint = f"{len(sw)}:{len(sw.columns)}"
                cache = getattr(desk.market, "_cached", None)
                key = f"profile:identity:{symbol}:{day}:{fingerprint}"

                def build_identity():
                    return _identity(desk, symbol, day)

                blocks[name] = (cache(key, build_identity)
                                if cache is not None else build_identity())
            elif name == "tradability":
                blocks[name] = _tradability(desk, symbol, day, snap)
            elif name == "valuation":
                blocks[name] = _valuation(desk, symbol, day, valuation_window)
            elif name == "peers":
                blocks[name] = _peers(desk, symbol, day)
            elif name == "relative_strength":
                blocks[name] = _relative_strength(desk, symbol, day)
            elif name == "fundamentals":
                blocks[name] = _fundamentals(desk, symbol, day)
            elif name == "flow":
                blocks[name] = _flow(desk, symbol, day)
            elif name == "technicals":
                blocks[name] = _technicals(desk, symbol, day)
            elif name == "sentiment":
                blocks[name] = _sentiment(desk, symbol, day)
            elif name == "ownership":
                blocks[name] = _ownership(desk, symbol, day)
            elif name == "events":
                blocks[name] = _events(desk, symbol, day)
            elif name == "position":
                blocks[name] = _position(desk, symbol, day)
            elif name == "intraday":
                held_stop = (blocks.get("position", {}).get("stop") or {}).get("price")
                blocks[name] = _intraday(desk, symbol, day, held_stop)
        except (DatasetReadError, KeyError, FileNotFoundError, ValueError) as exc:
            # A broken input is explicit per block.  It must not become an empty
            # frame, an empty event list, or a whole-profile HTTP 500.
            blocks[name] = {
                "available": False,
                "status": "error",
                "reason": (str(exc) if isinstance(exc, DatasetReadError)
                           else f"{type(exc).__name__}: {exc}"),
            }

    dropped: list[str] = []
    if plan is not None:
        # 只算保留块，判定沿用首次的：输出（含 omitted_sections / size_note）
        # 与第一次逐字一致，只是不再为注定被丢的块付计算成本。
        wanted, dropped = plan
    elif blocks:
        wanted, dropped = _fit_blocks(blocks, wanted)
        if plan_key is not None:
            _plan_store(plan_key, wanted, dropped)

    missing = [name for name in wanted
               if isinstance(blocks.get(name), dict) and blocks[name].get("available") is False]
    # A block can be present but partly empty — `ownership.unlocks` on a name with
    # nothing scheduled. Reporting only top-level keys hid that: the payload said
    # `missing: []` while an entire sub-block carried no data.
    for name in wanted:
        block = blocks.get(name)
        if not isinstance(block, dict):
            continue
        for sub, value in block.items():
            if isinstance(value, dict) and value.get("available") is False:
                missing.append(f"{name}.{sub}")
    out = {"ok": True, "symbol": symbol, "as_of": day}
    for name in wanted:
        out[name] = blocks[name]
    if sections or dropped:
        # 被省略的区块要列出来：读者要知道"还有哪些没给我"，否则会把
        # "没请求"读成"没有这一块"。
        out["omitted_sections"] = [n for n in BLOCK_ORDER if n not in wanted]
    if dropped:
        # The dossier grew past what the host will carry — 600519 reached 16,572 B
        # the day one announcement got its body downloaded, because the excerpt
        # stopped being a 60-byte "未下载" and became 320 characters of prose. That
        # variable is content, not code, so no amount of trimming fixes it. Drop
        # whole blocks instead of letting the host cut strings inside them: a
        # missing block is one `sections=` call away, a truncated string looks
        # like a short fact.
        out["size_note"] = (
            f"这份档案超出单次上限，{len(dropped)} 块留给 sections= 单独取：{dropped}。"
            "这里没有的信息不等于没有——用 quant_profile(sections=[...]) 取回来。")
    errors = {name: blocks[name]["reason"] for name in wanted
              if isinstance(blocks.get(name), dict)
              and blocks[name].get("status") == "error"}
    out.update({
        "status": "degraded" if errors else "ok",
        "missing": missing,
        "how_to_read": [
            "valuation.percentile_in_window 是这只票自己在窗口内的位置，不是和行业比；0 = 窗口最低。",
            "peers 是同日同行业的横截面：median 是同行中位数，rank_pct 是本票在同行中的位置。",
            "relative_strength 把区间收益拆成「票 / 行业 / 全市场」三段；vs_industry_pp 才是选股那部分。",
            "ownership 讲的是谁拿着：户数下降=集中，前十大占流通比与北向席位是谁在拿，unlocks 是未来 180 天会变成可卖的数量。",
            "fundamentals 是年初至今累计（YTD），不是单季；roe_ytd 每年前三个月会重置。",
            "fundamentals 只包含 ann_date <= as_of 的财报，所以季报刚出时会有跳变。",
            "flow.main_net_cny 是超大单+大单净额，来自聚合口径，不是逐笔还原。",
            "technicals 全部按前复权价算；tradability 和 position 用的是真实成交价。",
            "这里没有一个结论：没有评分、没有贵/便宜、没有买卖建议。",
        ],
    })
    if errors:
        out["errors"] = errors
    if sections or dropped:
        # 只保留与本次请求相关的读法。全部返回时 979 字节是值得的；
        # 只问 events 时还附上 valuation 的读法，那是 18% 的浪费。
        out["how_to_read"] = [line for line in out["how_to_read"]
                              if _applies(line, wanted)]
    return out
