"""Score the model's own claims so "deep thinking" is measurable.

A memo is only worth writing if it can be wrong. Every prediction carries a
probability and a resolvable kind, and this module turns the outcome into a
Brier score. A model that writes confident prose but cannot beat 0.25 Brier on
coin-flip questions has told us something important about itself.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd

KINDS = {"market_up", "symbol_up", "core_up"}


def _ew_market(store, start: str | None = None) -> pd.Series:
    """Equal-weight market return per session.

    Bounded from the earliest memo that still needs scoring. This used to read the
    whole archive — 16 million rows, about eight seconds — to answer a question
    about the last few weeks, on a command the daily job runs unattended.
    """
    daily = store.read("daily", start, None, columns=["trade_date", "close", "pre_close"])
    daily = daily.copy()
    daily["trade_date"] = daily["trade_date"].astype(str)
    px = daily[["trade_date", "close", "pre_close"]].copy()
    px["ret"] = px["close"] / px["pre_close"] - 1
    return px.groupby("trade_date")["ret"].mean().sort_index()


def _symbol_series(store, symbol: str) -> pd.Series:
    """Closes for one symbol, from whichever dataset actually lists it.

    The ETF test was ``.SH`` plus a 51/58/56 prefix, so 159915.SZ and every 50/52/
    53/55 Shanghai ETF fell through to ``daily``, found nothing, and returned an
    empty series — ``_resolve_one`` then reported the prediction as unresolved
    instead of scoring it. Those memos never close, and the Brier calibration is
    computed on the subset that happened to be common stocks.
    """
    fund = store.read("fund_daily", columns=["ts_code", "trade_date", "close"])
    if not fund.empty:
        hit = fund[fund["ts_code"].astype(str) == symbol]
        if not hit.empty:
            return (hit.set_index(hit["trade_date"].astype(str))["close"]
                    .astype(float).sort_index())
    daily = store.read("daily", columns=["ts_code", "trade_date", "close"])
    d = daily[daily["ts_code"].astype(str) == symbol]
    return d.set_index(d["trade_date"].astype(str))["close"].astype(float).sort_index()


def _core_nav(paper) -> pd.Series:
    rows = paper.core_nav_history(limit=2000)
    if not rows:
        return pd.Series(dtype="float64")
    return pd.Series({r["trade_date"]: float(r["nav"]) for r in rows}).sort_index()


def _validate_prediction(pred: dict) -> tuple[int, str, float]:
    raw_horizon = pred.get("horizon", 5)
    try:
        horizon = int(raw_horizon)
        exact = float(raw_horizon) == horizon
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("horizon 必须是正整数") from exc
    if horizon <= 0 or not exact or isinstance(raw_horizon, bool):
        raise ValueError("horizon 必须是正整数")
    kind = pred.get("kind", "market_up")
    if kind not in KINDS:
        raise ValueError(f"未知 prediction kind: {kind}")
    try:
        probability = float(pred["probability"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("probability 必须是 [0, 1] 内的有限数") from exc
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ValueError("probability 必须是 [0, 1] 内的有限数")
    if kind == "symbol_up" and not str(pred.get("symbol") or "").strip():
        raise ValueError("symbol_up 必须提供 symbol")
    return horizon, kind, probability


def _resolve_one(pred: dict, start: str, market: pd.Series, symbol_cache: dict,
                 store, paper) -> float | None:
    horizon, kind, _ = _validate_prediction(
        {**pred, "probability": pred.get("probability", 0.5)})
    if kind == "core_up":
        series = _core_nav(paper)
    elif kind == "symbol_up":
        symbol = pred.get("symbol", "")
        if symbol not in symbol_cache:
            symbol_cache[symbol] = _symbol_series(store, symbol)
        series = symbol_cache[symbol]
    else:
        # ``market`` is a return series; compound it so the comparison is a price.
        series = (1 + market).cumprod()

    # The anchor is the last session **at or before** the memo date, not the
    # first at or after it. A memo written on a Sunday refers to Friday's close;
    # using the next Monday as the anchor shifted every horizon by one day and,
    # worse, made a one-day prediction unresolvable until the day after — while
    # /duty was already reporting it as due. Two components, two definitions of
    # "matured"; this is the one that has to win, because it is the one that
    # produces a number.
    prior = [d for d in series.index if d <= start]
    if not prior:
        return None
    anchor = prior[-1]
    after = [d for d in series.index if d > anchor]
    if len(after) < horizon:
        return None
    start_price = float(series.loc[anchor])
    end_price = float(series.loc[after[horizon - 1]])
    if start_price <= 0 or not math.isfinite(start_price) or not math.isfinite(end_price):
        return None
    return 1.0 if end_price > start_price else 0.0


def score_memos(store, paper, *, dry_run: bool = False) -> dict:
    memos = paper.list_memos(limit=500, status="open")
    # Anchor the read on the oldest memo still open, with a month of lead so the
    # first prediction's window has a base.
    anchors = sorted(str(m.get("created_at") or "")[:10].replace("-", "") for m in memos)
    start = None
    if anchors and anchors[0]:
        start = (datetime.strptime(anchors[0], "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
    market = _ew_market(store, start)
    symbol_cache: dict[str, pd.Series] = {}
    scored = []
    invalid = []
    for memo in memos:
        start = memo["created_at"][:10].replace("-", "")
        outcomes = []
        pending = 0
        errors = []
        for pred in memo.get("predictions", []):
            try:
                _, kind, probability = _validate_prediction(pred)
                outcome = _resolve_one(pred, start, market, symbol_cache, store, paper)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if outcome is None:
                pending += 1
                continue
            outcomes.append({
                "claim": pred.get("claim", ""),
                "kind": kind,
                "probability": probability,
                "outcome": outcome,
            })
        if errors:
            invalid.append({"id": memo["id"], "topic": memo["topic"], "errors": errors})
        if not outcomes:
            continue
        brier = sum((o["probability"] - o["outcome"]) ** 2 for o in outcomes) / len(outcomes)
        payload = {"predictions": outcomes, "brier": round(brier, 4),
                   "resolved": len(outcomes), "pending": pending}
        if pending or errors:
            # A partial Brier is not a settled memo. Keep it open so the final
            # calibration contains every prediction exactly once.
            scored.append({"id": memo["id"], "topic": memo["topic"],
                           "status": "partial", **payload})
            continue
        scored.append({"id": memo["id"], "topic": memo["topic"], **payload})
        if not dry_run:
            paper.close_memo(memo["id"], payload)
    summary = paper.calibration()
    return {"scored": scored, "invalid": invalid, "calibration": summary}
