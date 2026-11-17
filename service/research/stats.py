"""Statistics that decide whether a backtest is real.

A single Sharpe ratio cannot answer "is this edge or is this selection?".
This module implements the three corrections that can:

- **Deflated Sharpe** (Bailey & Lopez de Prado 2014): the expected maximum
  Sharpe of N independent trials under the null, so a grid search cannot
  manufacture significance by trying enough variants.
- **PBO via CSCV** (Bailey et al. 2015): split the timeline into blocks, pick
  the best config in-sample, and measure how often it lands below the median
  out-of-sample. That fraction is the probability of backtest overfitting.
- **CPCV** (Lopez de Prado 2018): purged, embargoed combinatorial splits, so
  overlapping labels do not leak across the train/test boundary.

No scipy dependency: the normal CDF/PPF are implemented directly.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import combinations

import numpy as np
import pandas as pd

from service.kernel.units import TRADING_DAYS_PER_YEAR

TRADING_DAYS = TRADING_DAYS_PER_YEAR
EULER_GAMMA = 0.5772156649015329


# --------------------------------------------------------------- distribution
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, |err| < 1.2e-9)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# -------------------------------------------------------------------- returns
def clean_returns(returns: pd.Series | np.ndarray | list[float]) -> pd.Series:
    """Keep only observed, finite returns.

    Missing data is not a zero-return session: filling it would dilute volatility,
    alter the sample count and can move a candidate across a promotion gate.
    """
    r = pd.Series(returns, dtype="float64")
    return r.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)


def sharpe_per_period(returns: pd.Series) -> float:
    r = clean_returns(returns)
    if len(r) < 2:
        return 0.0
    sd = float(r.std(ddof=1))
    if sd <= 0 or not np.isfinite(sd):
        return 0.0
    return float(r.mean() / sd)


def annualise_sharpe(sr_per_period: float, periods: int = TRADING_DAYS) -> float:
    return float(sr_per_period * math.sqrt(periods))


def max_drawdown(returns: pd.Series) -> float:
    r = clean_returns(returns)
    if r.empty:
        return 0.0
    equity = (1 + r).cumprod()
    # Wealth starts at 1 before the first return. Without that initial point an
    # immediate loss can never be a drawdown because the first observation is
    # incorrectly treated as the historical peak.
    equity = pd.concat([pd.Series([1.0]), equity], ignore_index=True)
    peak = equity.cummax()
    return float((equity / peak - 1).min())


def metrics(returns: pd.Series, periods: int = TRADING_DAYS) -> dict:
    """Annualised with ``periods`` sessions a year — 252 by convention, while the
    A-share calendar runs 242-244. CAGR is ~3.7% high and Sharpe ~1.8% high
    against a 243-session year; the number travels with its basis so a reader
    cannot quote it as if the basis were the exchange's."""
    """Return / risk summary. Empty series yields zeros, never NaN."""
    r = clean_returns(returns)
    n = len(r)
    if n == 0:
        return {"n": 0, "total_return": 0.0, "cagr": 0.0, "ann_vol": 0.0,
                "sharpe": 0.0, "max_drawdown": 0.0, "calmar": 0.0,
                "hit_rate": 0.0, "skew": 0.0, "kurtosis": 3.0}

    equity = float((1 + r).cumprod().iloc[-1])
    years = n / periods
    cagr = equity ** (1 / years) - 1 if years > 0 and equity > 0 else -1.0
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    sr = sharpe_per_period(r)
    mdd = max_drawdown(r)
    active = r[r != 0]
    return {
        "n": n,
        "exposure": round(float(len(active) / n), 4) if n else 0.0,
        "total_return": round(equity - 1, 6),
        "cagr": round(float(cagr), 6),
        "ann_vol": round(float(sd * math.sqrt(periods)), 6),
        "sharpe": round(annualise_sharpe(sr, periods), 4),
        "max_drawdown": round(mdd, 6),
        "calmar": round(float(cagr / abs(mdd)), 4) if mdd < 0 else 0.0,
        "hit_rate": round(float((active > 0).mean()), 4) if len(active) else 0.0,
        "skew": round(float(r.skew()), 4) if n > 2 else 0.0,
        "kurtosis": round(float(r.kurtosis() + 3.0), 4) if n > 3 else 3.0,
    }


# ----------------------------------------------------------- deflated sharpe
def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """E[max SR] of N independent trials under the null (Bailey & Lopez de Prado)."""
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    return math.sqrt(sr_variance) * (
        (1 - EULER_GAMMA) * norm_ppf(1 - 1.0 / n_trials)
        + EULER_GAMMA * norm_ppf(1 - 1.0 / (n_trials * math.e))
    )


def deflated_sharpe(returns: pd.Series, trials: int,
                    trial_sharpes: list[float] | None = None) -> dict:
    """Probability that the observed Sharpe exceeds the best of ``trials`` nulls.

    ``trial_sharpes`` should be the per-period Sharpes of every configuration
    tried. Without it we fall back to the variance implied by the observed
    distribution, which is optimistic -- always pass the grid when you have it.
    """
    r = clean_returns(returns)
    n = len(r)
    sr = sharpe_per_period(r)
    if n < 30 or trials < 1:
        return {"dsr": 0.0, "sr_per_period": round(sr, 6),
                "sr_annual": round(annualise_sharpe(sr), 4),
                "sr0_per_period": 0.0, "trials": int(trials), "n": n}

    if trial_sharpes and len(trial_sharpes) > 1:
        sr_var = float(np.var(trial_sharpes, ddof=1))
        variance_source = "grid"
    else:
        # Optimistic by construction: this is the variance of the *observed*
        # series, not of the trials that produced it. Reported so a verdict can
        # be read with the right amount of doubt instead of silently.
        sr_var = (1.0 + 0.5 * sr ** 2) / max(n, 2)
        variance_source = "implied"

    sr0 = expected_max_sharpe(trials, sr_var)
    skew = float(r.skew()) if n > 2 else 0.0
    kurt = float(r.kurtosis() + 3.0) if n > 3 else 3.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return {"dsr": 0.0, "sr_per_period": round(sr, 6),
                "sr_annual": round(annualise_sharpe(sr), 4),
                "sr0_per_period": round(sr0, 6), "trials": int(trials), "n": n}
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)
    return {
        "dsr": round(norm_cdf(z), 4),
        "sr_per_period": round(sr, 6),
        "sr_annual": round(annualise_sharpe(sr), 4),
        "sr0_per_period": round(sr0, 6),
        "trials": int(trials),
        "n": n,
        "sr_variance_source": variance_source,
        "skew": round(skew, 3),
        "kurtosis": round(kurt, 3),
    }


# ---------------------------------------------------------------------- PBO
def cscv_pbo(matrix: pd.DataFrame, blocks: int = 8) -> dict:
    """Probability of backtest overfitting via combinatorially symmetric CV.

    ``matrix``: rows = periods, columns = configurations (each a return series).
    The best in-sample config is tracked out-of-sample; if it lands in the
    bottom half more often than not, the selection was noise.
    """
    trials = 0 if matrix is None else int(matrix.shape[1])
    invalid = {"ok": False, "pbo": None, "splits": 0, "trials": trials}
    if matrix is None or matrix.empty or matrix.shape[1] < 2:
        return {**invalid, "note": "PBO requires at least two configurations"}
    m = matrix.apply(pd.to_numeric, errors="coerce")
    m = m.replace([np.inf, -np.inf], np.nan)
    if m.isna().any().any():
        return {**invalid, "note": "matrix contains missing or non-finite returns"}
    n_rows, n_cols = m.shape
    blocks = max(4, min(blocks, n_rows // 20 * 2))
    if blocks % 2:
        blocks -= 1
    if blocks < 4:
        return {**invalid, "note": "sample too short or contains non-finite returns"}

    edges = np.linspace(0, n_rows, blocks + 1).astype(int)

    def sr(frame: pd.DataFrame) -> np.ndarray:
        sd = frame.std(ddof=1).to_numpy(dtype=float)
        mu = frame.mean().to_numpy(dtype=float)
        out = np.zeros_like(mu)
        ok = sd > 0
        out[ok] = mu[ok] / sd[ok]
        return out

    half = blocks // 2
    logits: list[float] = []
    for combo in combinations(range(blocks), half):
        is_rows = np.concatenate([np.arange(edges[i], edges[i + 1]) for i in combo])
        oos_rows = np.setdiff1d(np.arange(n_rows), is_rows)
        if len(oos_rows) < 2:
            continue
        is_sr = sr(m.iloc[is_rows])
        oos_sr = sr(m.iloc[oos_rows])
        best = int(np.argmax(is_sr))
        # Average ranks are essential here: arbitrary tie-breaking can turn a
        # family of identical configurations into apparent overfitting (or no
        # overfitting) solely because of column order.
        ranks = pd.Series(oos_sr).rank(method="average", ascending=True).to_numpy()
        rank = float(ranks[best])
        omega = rank / (n_cols + 1.0)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        logits.append(math.log(omega / (1 - omega)))

    if not logits:
        return {**invalid, "note": "no valid CSCV splits"}
    arr = np.array(logits)
    return {
        "ok": True,
        "pbo": round(float((arr < 0).mean()), 4),
        "splits": len(arr),
        "trials": int(n_cols),
        "mean_logit": round(float(arr.mean()), 4),
        "interpretation": "probability the best in-sample config is below median OOS",
    }


# --------------------------------------------------------------------- CPCV
def cpcv_folds(n_obs: int, groups: int = 6, k_test: int = 2,
               embargo: int = 0, *,
               label_spans: Sequence[int] | np.ndarray | None = None,
               ) -> list[tuple[np.ndarray, np.ndarray]]:
    """Combinatorial purged CV splits.

    ``label_spans[i]`` is the exclusive observation index at which row ``i``'s
    label ends. Supplying it purges every training label that overlaps a test
    label, rather than assuming all labels have the same one-row span. The
    runner must derive these spans from its actual event exits; a fixed embargo
    alone cannot prove that overlapping labels are absent.
    """
    if n_obs < groups * 2 or k_test >= groups or k_test < 1:
        return []
    spans = None
    if label_spans is not None:
        spans = np.asarray(label_spans, dtype=int)
        if spans.shape != (n_obs,) or np.any(spans < np.arange(n_obs) + 1):
            raise ValueError("label_spans must contain one exclusive end per observation")
    edges = np.linspace(0, n_obs, groups + 1).astype(int)
    idx = np.arange(n_obs)
    folds: list[tuple[np.ndarray, np.ndarray]] = []
    for combo in combinations(range(groups), k_test):
        test = np.concatenate([idx[edges[i]:edges[i + 1]] for i in combo])
        mask = np.ones(n_obs, dtype=bool)
        for i in combo:
            lo, hi = edges[i], edges[i + 1]
            if spans is None:
                mask[max(0, lo - embargo):min(n_obs, hi + embargo)] = False
                continue
            test_end = int(spans[lo:hi].max())
            # [row, span[row]) and [lo, test_end) overlap.
            overlap = (spans > lo) & (idx < test_end)
            mask[overlap] = False
            mask[test_end:min(n_obs, test_end + embargo)] = False
        train = idx[mask]
        if len(train) >= 20 and len(test) >= 2:
            folds.append((train, test))
    return folds


def select_and_test(matrix: pd.DataFrame, groups: int = 6, k_test: int = 2,
                    embargo: int = 0, *,
                    label_spans: Sequence[int] | np.ndarray | None = None) -> dict:
    """Honest OOS estimate: pick the best config on train, score it on test.

    Returns the distribution of OOS Sharpe across CPCV paths plus how often the
    selection procedure picks the same config (instability is itself a red flag).
    """
    invalid = {"ok": False, "paths": 0, "oos_sharpe_mean": 0.0,
               "oos_sharpe_p05": 0.0, "positive": 0.0}
    if matrix is None or matrix.empty or matrix.shape[1] < 2:
        return invalid
    m = matrix.apply(pd.to_numeric, errors="coerce")
    m = m.replace([np.inf, -np.inf], np.nan)
    if m.isna().any().any():
        return {**invalid, "note": "matrix contains missing or non-finite returns"}
    folds = cpcv_folds(len(m), groups, k_test, embargo, label_spans=label_spans)
    if not folds:
        return invalid

    def sr(frame: pd.DataFrame) -> np.ndarray:
        sd = frame.std(ddof=1).to_numpy(dtype=float)
        mu = frame.mean().to_numpy(dtype=float)
        out = np.zeros_like(mu)
        ok = sd > 0
        out[ok] = mu[ok] / sd[ok]
        return out

    oos: list[float] = []
    picks: list[int] = []
    for train, test in folds:
        is_sr = sr(m.iloc[train])
        if not np.isfinite(is_sr).any() or np.all(is_sr == 0):
            continue
        best = int(np.argmax(is_sr))
        picks.append(best)
        oos.append(annualise_sharpe(sr(m.iloc[test])[best]))
    if not oos:
        return invalid
    arr = np.array(oos)
    values, counts = np.unique(picks, return_counts=True)
    return {
        "ok": True,
        "paths": len(arr),
        "oos_sharpe_mean": round(float(arr.mean()), 4),
        "oos_sharpe_median": round(float(np.median(arr)), 4),
        "oos_sharpe_p05": round(float(np.percentile(arr, 5)), 4),
        "positive": round(float((arr > 0).mean()), 4),
        "selected_config": m.columns[int(values[int(np.argmax(counts))])],
        "selection_stability": round(float(counts.max() / counts.sum()), 4),
    }


# ----------------------------------------------------------------- bootstrap
def boot_p(statistics: np.ndarray, observed: float = 0.0) -> float:
    """Bootstrap tail probability, floored at the resolution the run can support.

    ``(k + 1) / (n_boot + 1)``, not ``k / n_boot``. With 2,000 resamples the
    smallest honest p is 1/2001 = 0.0005, never 0: "no resample crossed zero" is
    evidence, not proof. Reporting 0.0 is worse than imprecise — sorted into a
    Benjamini-Hochberg family it sits at the top and becomes the one discovery
    the whole procedure reports. This desk found that out the first time an
    overlay p-value of exactly 0.0 was booked as a survivor at q=0.10.
    """
    n = len(statistics)
    if n == 0:
        return 1.0
    k = int((statistics <= observed).sum())
    return round((k + 1) / (n + 1), 6)


def bootstrap_mean(values: pd.Series | np.ndarray, n_boot: int = 5000,
                   seed: int = 7) -> dict:
    """Bootstrap CI and one-sided p-value for mean > 0 (H0: mean <= 0)."""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 10:
        return {"n": len(x), "mean": float(x.mean()) if len(x) else 0.0,
                "ci_low": 0.0, "ci_high": 0.0, "p_value": 1.0}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(n_boot, len(x)))
    means = x[idx].mean(axis=1)
    # A bootstrap test must approximate the sampling distribution under H0.
    # Resampling the observed positive mean and counting values <= 0 instead
    # tests whether the observed centre is positive, making the p-value depend
    # on the effect twice. Keep the ordinary bootstrap for the CI, but centre a
    # second copy at the null for inference.
    null_means = (x - x.mean())[idx].mean(axis=1)
    return {
        "n": len(x),
        "mean": round(float(x.mean()), 6),
        "ci_low": round(float(np.percentile(means, 2.5)), 6),
        "ci_high": round(float(np.percentile(means, 97.5)), 6),
        "p_value": boot_p(-null_means, observed=-float(x.mean())),
        "p_basis": "H0-centered",
    }


def block_bootstrap_mean(returns: pd.Series, block: int = 5,
                         n_boot: int = 2000, seed: int = 7) -> dict:
    """Bootstrap the mean by resampling *blocks of periods*, not single rows.

    Trades on the same day are not independent observations: a limit-up session
    produces dozens of them at once, all sharing the same market move. Resampling
    trades one by one pretends there are more independent draws than there are,
    which shrinks the standard error and flatters the p-value.
    """
    r = clean_returns(returns).to_numpy()
    n = len(r)
    if n < 10:
        return {"n": n, "mean": float(r.mean()) if n else 0.0,
                "ci_low": 0.0, "ci_high": 0.0, "se": None, "p_value": 1.0,
                "block_scheme": "not_run", "null_center": None}
    block = max(1, min(block, n // 5))
    n_blocks = math.ceil(n / block)
    rng = np.random.default_rng(seed)
    # Circular moving blocks give every observation the same selection
    # probability. Plain sliding windows omit edge-starting combinations and
    # over-weight interior rows; under H0 that shifts the null centre whenever
    # the edge observations differ from the interior, biasing the p-value.
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    extended = np.concatenate([r, r[:block - 1]])
    windows = np.lib.stride_tricks.sliding_window_view(extended, block)
    sample = windows[starts.ravel()]
    means = sample.mean(axis=1).reshape(n_boot, n_blocks).mean(axis=1)
    centred = r - r.mean()
    null_extended = np.concatenate([centred, centred[:block - 1]])
    null_windows = np.lib.stride_tricks.sliding_window_view(null_extended, block)
    null_means = null_windows[starts.ravel()].mean(axis=1).reshape(
        n_boot, n_blocks).mean(axis=1)
    return {
        "n": n,
        "mean": round(float(r.mean()), 6),
        "ci_low": round(float(np.percentile(means, 2.5)), 6),
        "ci_high": round(float(np.percentile(means, 97.5)), 6),
        # The spread of the resampled means: the standard error under the same
        # block assumption as the interval and the p-value, so a caller does not
        # have to fall back to an iid ``std/sqrt(n)`` that ignores clustering.
        "se": round(float(np.std(means, ddof=1)), 6),
        "p_value": boot_p(-null_means, observed=-float(r.mean())),
        "p_basis": "H0-centered-day-blocks",
        "block_scheme": "circular",
        # Monte Carlo diagnostic; the circular scheme makes its expectation 0.
        "null_center": round(float(null_means.mean()), 8),
    }
