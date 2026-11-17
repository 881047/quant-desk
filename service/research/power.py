"""How large a sample does this claim need before "not significant" means anything?

`MIN_TRADES = 100` was a guess. A study can be too small to distinguish a real
edge from zero, and then "testing" is not a verdict — it is a wait with no end.
This module answers two questions in the units the desk actually trades in:

- **MDE**: with this many independent days and this much day-to-day variance,
  what is the smallest per-trade edge the design can detect 80% of the time?
- **required**: how many days (and trades) would it take to detect the effect
  the sample actually showed?

The decision rule that follows: if the MDE is *larger* than the edge worth
having — here, surviving the 2x cost stress — then the sample cannot resolve the
question at all, and the honest verdict is ``underpowered``, not ``testing``.

Everything is computed on **daily** means, not on trades, because trades on the
same day are one observation of the market, not twenty.
"""

from __future__ import annotations

import math

import pandas as pd

from . import stats


def mde(sigma: float, n: int, *, alpha: float = 0.10, power: float = 0.8) -> float:
    """Minimum detectable effect for a one-sided test of mean > 0."""
    if n <= 1 or sigma <= 0:
        return float("inf")
    z_alpha = stats.norm_ppf(1 - alpha)
    z_power = stats.norm_ppf(power)
    return (z_alpha + z_power) * sigma / math.sqrt(n)


def required_n(effect: float, sigma: float, *, alpha: float = 0.10,
               power: float = 0.8) -> int | None:
    """Independent observations needed to detect ``effect`` at this power."""
    if effect <= 0 or sigma <= 0:
        return None
    z_alpha = stats.norm_ppf(1 - alpha)
    z_power = stats.norm_ppf(power)
    return math.ceil(((z_alpha + z_power) * sigma / effect) ** 2)


def assess(trades: pd.DataFrame, *, cost_bps: float = 0.0, floor_mult: float = 2.0,
           alpha: float = 0.10, power: float = 0.8) -> dict:
    """Power of this sample against the edge that would justify trading it.

    ``floor_mult`` mirrors the referee's 2x cost stress: an edge that dies at
    twice the cost is not worth having, so it is the effect the design must be
    able to see. With ``cost_bps=0`` the floor is zero and ``underpowered`` is
    never raised — the caller has to say what "worth having" means.
    """
    empty = {"n": 0, "days": 0, "underpowered": False, "floor_bps": floor_mult * cost_bps}
    if trades is None or trades.empty or "excess" not in trades.columns:
        return empty
    frame = trades.dropna(subset=["excess"]).copy()
    if frame.empty:
        return empty
    frame["trade_date"] = frame["trade_date"].astype(str)
    daily = frame.groupby("trade_date")["excess"].mean()
    n_days = len(daily)
    if n_days < 2:
        return {**empty, "n": len(frame), "days": n_days}

    effect = float(daily.mean())
    sigma = float(daily.std(ddof=1))
    se = sigma / math.sqrt(n_days)
    detectable = mde(sigma, n_days, alpha=alpha, power=power)
    floor = floor_mult * cost_bps / 10000.0
    per_day = len(frame) / n_days
    # Units matter here and used to be mixed. `detectable` is the MDE of the
    # **daily mean**; `floor` is the cost barrier of **one trade**. Comparing them
    # directly assumes exactly one trade per day, so with k trades a day the test
    # demanded MDE > floor instead of MDE > k x floor and called designs
    # underpowered that could see the bar perfectly well. Both sides are now in
    # daily units; the per-trade figure is still reported, labelled for what it is.
    floor_per_day = floor * per_day
    need_days = required_n(effect, sigma, alpha=alpha, power=power)
    return {
        "n": len(frame),
        "days": n_days,
        "trades_per_day": round(per_day, 2),
        "effect": round(effect, 6),
        "sigma_daily": round(sigma, 6),
        "se": round(se, 6),
        "mde_per_day": round(detectable, 6),
        "mde_per_trade": round(detectable / per_day, 6) if per_day else round(detectable, 6),
        "mde_bps": round(detectable * 10000, 1),
        "floor_bps": round(floor * 10000, 1),
        "floor_bps_per_day": round(floor_per_day * 10000, 1),
        "underpowered": bool(floor > 0 and detectable > floor_per_day),
        "required_days": need_days,
        "required_trades": (math.ceil(need_days * per_day)
                            if need_days is not None else None),
        "alpha": alpha,
        "power": power,
        "independence": {
            "unit": "trade_date 的等权日均",
            "days": n_days,
            "assumption": "各独立交易日的均值相互独立；未建模跨日自相关",
        },
        "caveats": [
            "required_trades 只是按当前 trades_per_day 外推的估计，不是独立样本数。",
            "若收益跨日自相关，需按 block 的有效样本量重算；本结果可能高估功效。",
        ],
    }
