"""Characteristic-matched alpha: is the edge anything more than beta/size/reversal?

The referee's "excess" subtracts an equal-weight market return. That is not a
factor model. Nine event structures built on limit-ups and limit-downs all load
on high beta, small size and short-term reversal by construction, so a positive
"excess" can be compensation for exposures the account already carries.

Two ways to correct it. Fitting a pooled regression on the same trades and then
reporting its residuals is the wrong one here: with 300 trades and four factors
the coefficients are estimated from the very sample being judged, so the
"residual alpha" is in-sample by construction.

This module takes the other road — **matching**:

- for every trade, the benchmark is the mean forward return of the k nearest
  names **on the same signal date**, matched on characteristics known at that
  close;
- no coefficient is estimated, so there is nothing to overfit;
- exposures are z-scored cross-sectionally within the day, clipped at ±5;
- the neighbour's return uses the **same entry/exit convention** as the trade,
  including T+1 open entry.

Look-ahead guards, stated because they are the whole point:

1. every characteristic uses data up to and including the signal close ``T``;
2. the trade enters at ``T+1`` open, so nothing about ``T+1`` can enter the match;
3. the universe is whatever the panel holds that day — delisted names included,
   because the panel is historical, not a current snapshot;
4. a neighbour without a forward bar (suspension) is dropped, not filled.

What this cannot fix: if the edge lives in a characteristic nobody measured, the
matched benchmark will not remove it. The residual is "alpha after these four
exposures", not "alpha".
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from service import settings

from . import stats
from .spec import EventStudy

# Long-form columns produced by :func:`build_exposures`.
EXPOSURES = ("log_mcap", "beta_60", "rev_20", "log_amount")
DEFAULT_K = settings.integer("research.default_k")
MIN_NEIGHBOURS = settings.integer("research.min_neighbours")


def _shifted(panel: pd.DataFrame, column: str, k: int) -> pd.Series:
    """``column`` shifted ``k`` bars forward **within each symbol**.

    Reads a materialised ``<column>_f<k>`` when the panel carries one (entry,
    k=1) and computes the shift otherwise. Long holds must not be materialised
    as columns: a 60-session study would add 300 columns to a multi-million-row
    panel to produce one number per event.
    """
    name = f"{column}_f{k}"
    if name in panel.columns:
        return pd.to_numeric(panel[name], errors="coerce")
    ordered = panel.sort_values(["ts_code", "trade_date"], kind="mergesort")
    values = pd.to_numeric(ordered[column], errors="coerce").groupby(
        ordered["ts_code"], sort=False).shift(-k)
    return values.reindex(panel.index)


def forward_returns(panel: pd.DataFrame, spec: EventStudy) -> pd.DataFrame:
    """The return a trade with this spec would earn, for **every** name and day.

    Same convention as :func:`service.research.spec.build_trades`: entry is the
    next open (or close), exit is the close/open ``hold`` sessions later. Using
    one function for trades and benchmarks is what keeps the comparison honest.
    """
    entry = _shifted(panel, "open" if spec.entry == "next_open" else "close", 1)
    if spec.hold == 0:
        exit_ = _shifted(panel, "close", 1)
    else:
        exit_ = _shifted(panel, "open" if spec.exit_at == "open" else "close",
                         spec.hold + 1)
    out = panel[["ts_code", "trade_date"]].copy()
    out["fwd_ret"] = (exit_ / entry - 1.0).replace([np.inf, -np.inf], np.nan)
    return out


def _rolling_beta(ret: pd.DataFrame, window: int) -> pd.DataFrame:
    """Trailing beta against the equal-weight market, ending at each row's date."""
    market = ret.mean(axis=1)
    min_periods = max(20, window // 2)
    e_r = ret.rolling(window, min_periods=min_periods).mean()
    e_m = market.rolling(window, min_periods=min_periods).mean()
    e_rr = ret.mul(market, axis=0).rolling(window, min_periods=min_periods).mean()
    e_mm = (market * market).rolling(window, min_periods=min_periods).mean()
    var_m = e_mm - e_m ** 2
    beta = (e_rr - e_r.mul(e_m, axis=0)).div(var_m.where(var_m > 0), axis=0)
    return beta.clip(-3.0, 3.0)


def build_exposures(panel: pd.DataFrame, basics: pd.DataFrame | None = None, *,
                    beta_window: int = 60, rev_window: int = 20,
                    liq_window: int = 20) -> pd.DataFrame:
    """Long frame of exposures known at each day's close.

    ``basics`` is optional ``daily_basic`` (Tushare): ``total_mv``/``circ_mv`` on
    the same trade date. Without it the size term falls back to log turnover,
    which is a liquidity proxy, not market cap — the result says which one ran.
    """
    # Exposures that compare two dates (beta, reversal) run on the adjusted
    # series. A cash dividend is not a negative return, and the reversal factor
    # is a difference between two price levels.
    px_col = "adj_close" if "adj_close" in panel.columns else "close"
    close = panel.pivot(index="trade_date", columns="ts_code", values=px_col).sort_index()
    ret = close.pct_change(fill_method=None)
    amount = panel.pivot(index="trade_date", columns="ts_code",
                         values="amount").sort_index()

    beta = _rolling_beta(ret, beta_window)
    rev = close / close.shift(rev_window) - 1.0
    log_amount = np.log1p(amount.rolling(liq_window, min_periods=liq_window // 2).mean())

    frames = {
        "beta_60": beta,
        "rev_20": rev,
        "log_amount": log_amount,
    }

    if basics is not None and not basics.empty:
        cap_col = "circ_mv" if "circ_mv" in basics.columns else "total_mv"
        if cap_col in basics.columns:
            cap = basics[["ts_code", "trade_date", cap_col]].copy()
            cap["trade_date"] = cap["trade_date"].astype(str)
            cap[cap_col] = pd.to_numeric(cap[cap_col], errors="coerce")
            cap = cap[cap[cap_col] > 0]
            wide = cap.pivot_table(index="trade_date", columns="ts_code",
                                   values=cap_col, aggfunc="last").sort_index()
            frames["log_mcap"] = np.log(wide)

    # The universe is the panel, not the union of the factor tables. An outer
    # join would add every stock in `daily_basic` to a bond panel, and the bond
    # columns would then look 90% empty — which silently disables matching.
    long = panel[["trade_date", "ts_code"]].drop_duplicates()
    long["trade_date"] = long["trade_date"].astype(str)
    for name, frame in frames.items():
        tidy = frame.stack(future_stack=True).rename(name).reset_index()
        tidy.columns = ["trade_date", "ts_code", name]
        tidy["trade_date"] = tidy["trade_date"].astype(str)
        long = long.merge(tidy.dropna(subset=[name]), on=["trade_date", "ts_code"], how="left")
    return long.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)


def _zscore(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = frame.copy()
    for column in columns:
        values = pd.to_numeric(out[column], errors="coerce")
        sd = values.std(ddof=0)
        if not np.isfinite(sd) or sd <= 0:
            out[column] = 0.0
        else:
            out[column] = ((values - values.mean()) / sd).clip(-5.0, 5.0)
    return out


def _usable_factors(exposures: pd.DataFrame) -> tuple[str, ...]:
    """Columns that exist *and* have enough data to match on.

    A column that exists but is empty is worse than a missing one: merging on it
    would drop every row. Market cap is the usual culprit — `daily_basic` covers
    stocks, not convertible bonds.
    """
    return tuple(f for f in EXPOSURES
                 if f in exposures.columns and exposures[f].notna().mean() >= 0.30)


def match_benchmark(trades: pd.DataFrame, exposures: pd.DataFrame,
                    fwd: pd.DataFrame, *, factors: tuple[str, ...] | None = None,
                    k: int = DEFAULT_K) -> pd.DataFrame:
    """Attach ``matched_ret`` / ``matched_excess`` to every trade.

    Matching is within the signal date only. A trade whose characteristics are
    missing is returned with NaN rather than dropped, so the caller can report
    coverage instead of silently shrinking the sample.
    """
    if trades.empty:
        return trades.assign(matched_ret=np.nan, matched_excess=np.nan)

    factors = tuple(factors or _usable_factors(exposures))
    if len(factors) < 2:
        raise ValueError(f"need at least two exposures to match on, got {factors}")

    uni = exposures[["trade_date", "ts_code", *factors]].merge(
        fwd, on=["trade_date", "ts_code"], how="inner")
    uni = uni.dropna(subset=[*factors, "fwd_ret"])
    uni["trade_date"] = uni["trade_date"].astype(str)

    events = trades[["trade_date", "ts_code", "net"]].copy()
    events["trade_date"] = events["trade_date"].astype(str)
    # Keep a positional handle: merge() returns a fresh RangeIndex, while the
    # trade frame's index is whatever survived the pool filters.
    events["_row"] = np.arange(len(events))
    events = events.merge(
        uni[["trade_date", "ts_code", *factors]].rename(
            columns={f: f"ev_{f}" for f in factors}),
        on=["trade_date", "ts_code"], how="left")

    matched = np.full(len(trades), np.nan, dtype=float)
    event_columns = [f"ev_{f}" for f in factors]
    for day, block in events.groupby("trade_date", sort=False):
        pool = uni[uni["trade_date"] == day]
        if pool.empty or len(pool) < MIN_NEIGHBOURS + 1:
            continue
        valid = block.dropna(subset=event_columns)
        if valid.empty:
            continue

        pool_z = _zscore(pool, list(factors))
        matrix = pool_z[list(factors)].to_numpy(dtype=float)
        returns = pool_z["fwd_ret"].to_numpy(dtype=float)
        symbols = pool_z["ts_code"].to_numpy()

        # Standardise events with the *pool's* mean/std: the universe defines the
        # axes, not the handful of names being judged.
        centre = pool[list(factors)].mean().to_numpy(dtype=float)
        scale = pool[list(factors)].std(ddof=0).to_numpy(dtype=float)
        scale = np.where(np.isfinite(scale) & (scale > 0), scale, 1.0)
        vec = np.clip((valid[event_columns].to_numpy(dtype=float) - centre) / scale,
                      -5.0, 5.0)

        distances = ((matrix[None, :, :] - vec[:, None, :]) ** 2).sum(axis=2)
        own = valid["ts_code"].to_numpy()[:, None] == symbols[None, :]
        distances = np.where(own, np.inf, distances)
        take = min(k, len(pool) - 1)
        nearest = np.argpartition(distances, take - 1, axis=1)[:, :take]
        matched[valid["_row"].to_numpy()] = returns[nearest].mean(axis=1)

    out = trades.copy()
    out["matched_ret"] = matched
    out["matched_excess"] = out["net"] - out["matched_ret"]
    return out


def summarise(trades: pd.DataFrame, *, block: int = 5) -> dict:
    """Mean matched excess, its day-clustered p-value, and the raw comparison."""
    if trades.empty or "matched_excess" not in trades.columns:
        return {"n": 0, "coverage": 0.0}
    valid = trades.dropna(subset=["matched_excess"])
    n = len(valid)
    if n == 0:
        return {"n": 0, "coverage": 0.0}
    daily = valid.groupby("trade_date")["matched_excess"].mean()
    # One uncertainty convention. The interval and the t-statistic used to be iid
    # normal (±1.96 × std/√n) while the p-value came from a block bootstrap: the
    # daily means are overlapping h-day labels, so the iid standard error is too
    # narrow, the interval too tight and the t too large — and `blocks_promotion`
    # gates on t ≥ 2.
    boot = stats.block_bootstrap_mean(daily, block=block)
    raw_daily = valid.groupby("trade_date")["excess"].mean() if "excess" in valid.columns else None
    mean = float(daily.mean())
    se = boot.get("se")
    return {
        "n": int(n),
        "coverage": round(n / len(trades), 4),
        "mean_net": round(float(valid["net"].mean()), 6),
        "mean_matched_ret": round(float(valid["matched_ret"].mean()), 6),
        "mean_matched_excess": round(float(valid["matched_excess"].mean()), 6),
        "ci_low": boot.get("ci_low"),
        "ci_high": boot.get("ci_high"),
        "t_stat": (round(mean / se, 3)
                   if se is not None and np.isfinite(se) and se > 0 else None),
        "ci_basis": f"day-block bootstrap (block={block})",
        "p_value": round(float(boot["p_value"]), 4),
        "days": len(daily),
        "mean_equal_weight_excess": (round(float(valid["excess"].mean()), 6)
                                     if "excess" in valid.columns else None),
        "raw_days": len(raw_daily) if raw_daily is not None else 0,
    }


def blocks_promotion(summary: dict | None, *, min_n: int = 100,
                     min_t: float = 2.0, min_coverage: float = 0.80) -> tuple[bool, str]:
    """The gate: a candidate survives only on alpha the exposures do not explain.

    Returns ``(blocked, reason)``. Stated as a pure function so it can be tested
    without a store, and so the threshold is one place rather than three.
    """
    if not summary or summary.get("n", 0) < min_n:
        return True, f"attribution sample {summary.get('n', 0) if summary else 0} < {min_n}"
    coverage = summary.get("coverage")
    if coverage is None or not np.isfinite(coverage) or coverage < min_coverage:
        return True, f"attribution coverage {coverage} < {min_coverage}"
    # ``x or 0.0`` does not catch NaN: NaN is truthy, so an uncomputable alpha
    # walked straight through the one gate that exists to stop it. An attribution
    # that could not be measured must block, exactly like a negative one.
    alpha = summary.get("mean_matched_excess")
    if alpha is None or not np.isfinite(alpha) or alpha <= 0:
        return True, f"residual alpha {alpha} <= 0"
    t_stat = summary.get("t_stat")
    if t_stat is None or t_stat < min_t:
        return True, f"residual t {t_stat} < {min_t}"
    return False, ""


def attribute(trades: pd.DataFrame, panel: pd.DataFrame, spec: EventStudy,
              basics: pd.DataFrame | None = None, *, k: int = DEFAULT_K) -> dict:
    """Full pass: exposures -> forward returns -> match -> summary."""
    if trades is None or trades.empty:
        return {"n": 0, "coverage": 0.0, "k": k}
    exposures = build_exposures(panel, basics)
    fwd = forward_returns(panel, spec)
    matched = match_benchmark(trades, exposures, fwd, k=k)
    factors = list(_usable_factors(exposures))
    summary = summarise(matched)
    summary.update({
        "k": k,
        "factors": factors,
        "size_proxy": "mcap" if "log_mcap" in factors else "turnover",
        "method": "same-day characteristic matching, no fitted coefficients",
    })
    return summary
