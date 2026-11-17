"""Post-promotion decay: has the edge gone, and how would we know?

A backtest is a claim about the future. Once a strategy is live, the only
question that matters is whether the realised trades still look like the ones
the claim was built on. Nothing in this repository asked that before: `promoted`
was a one-way door.

Three detectors, because each fails differently:

``cusum``
    Cumulative sum of standardised shortfalls. Good at a *sudden* break, blind
    to a slow bleed smaller than its slack.
``rolling``
    The last ``window`` trades against the baseline standard error. Good at a
    sustained shift, noisy on a single bad run.
``sprt``
    Sequential likelihood ratio between "edge as advertised" and "edge is zero".
    This is the only one that can *conclude* anything, and it needs the most
    trades — roughly 100 for the examples in the tests.

The output is a state and the evidence for it. **It never liquidates and never
changes a position**: the desk's rule is that code does not trade. A decayed
strategy is a fact for the operator, not an order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from service.kernel.units import TRADING_DAYS_PER_YEAR


@dataclass(frozen=True)
class Baseline:
    """What the promotion claimed, in per-trade return units."""

    mean: float
    sigma: float
    n: int = 0
    source: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.mean) or not math.isfinite(self.sigma):
            raise ValueError("baseline mean and sigma must be finite")
        if self.sigma <= 0:
            raise ValueError("baseline sigma must be positive")


TRADING_DAYS = TRADING_DAYS_PER_YEAR


def baseline_from_evidence(evidence: dict | None) -> Baseline | None:
    """Read a baseline out of a strategy's stored evidence, if it has one.

    Two accepted forms:

    - ``mean_excess`` + ``std``: a per-trade claim from the referee (the honest
      one — the samples that produced it are on disk);
    - ``cagr`` + ``sharpe``: a daily claim, used by the core book, where there
      are no discrete trades. ``vol = cagr / sharpe`` and ``sigma_daily =
      vol / sqrt(252)`` assume a zero risk-free rate and arithmetic returns, so
      the resulting baseline is an approximation and says so in ``source``.

    Refuses to invent a sigma: without it every detector below is meaningless,
    and a made-up one would produce confident nonsense.
    """
    if not evidence:
        return None
    mean = evidence.get("mean_excess")
    sigma = evidence.get("std")
    if mean is not None and sigma is not None:
        mean_value, sigma_value = float(mean), float(sigma)
        if math.isfinite(mean_value) and math.isfinite(sigma_value) and sigma_value > 0:
            return Baseline(mean=mean_value, sigma=sigma_value,
                            n=int(evidence.get("n") or 0), source="evidence:per_trade")
    cagr = evidence.get("cagr")
    sharpe = evidence.get("sharpe")
    if cagr is not None and sharpe:
        cagr_value, sharpe_value = float(cagr), float(sharpe)
        annual_vol = cagr_value / sharpe_value
        if math.isfinite(cagr_value) and math.isfinite(annual_vol) and annual_vol > 0:
            return Baseline(mean=cagr_value / TRADING_DAYS,
                            sigma=annual_vol / math.sqrt(TRADING_DAYS),
                            n=0, source="evidence:cagr/sharpe")
    return None


def cusum(values: list[float], baseline: Baseline, *, slack: float = 0.5,
          threshold: float = 5.0) -> dict:
    """One-sided CUSUM for a downward shift, in baseline-sigma units."""
    stat = 0.0
    peak = 0.0
    alarm_at: int | None = None
    for i, value in enumerate(values):
        z = (value - baseline.mean) / baseline.sigma
        stat = max(0.0, stat - z - slack)
        peak = max(peak, stat)
        if stat > threshold and alarm_at is None:
            alarm_at = i
    return {"stat": round(stat, 3), "peak": round(peak, 3),
            "alarm": alarm_at is not None, "alarm_at": alarm_at}


def rolling(values: list[float], baseline: Baseline, *, window: int = 20,
            z_threshold: float = -2.0) -> dict:
    """Last-window mean against the baseline standard error."""
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        raise ValueError("window must be a positive integer")
    if len(values) < window:
        return {"window": window, "n": len(values), "z": None, "mean": None,
                "alarm": False}
    recent = np.asarray(values[-window:], dtype=float)
    se = baseline.sigma / math.sqrt(window)
    z = float((recent.mean() - baseline.mean) / se) if se > 0 else 0.0
    return {"window": window, "n": len(values), "z": round(z, 3),
            "mean": round(float(recent.mean()), 6), "alarm": z <= z_threshold}


def sprt(values: list[float], baseline: Baseline, *, mu1: float = 0.0,
         alpha: float = 0.05, beta: float = 0.2) -> dict:
    """Sequential test: H0 mean = baseline, H1 mean = ``mu1`` (default: zero).

    Boundaries follow Wald: accept H0 at ``log((1-beta)/alpha)``, accept H1 at
    ``log(beta/(1-alpha))``. Sigma is taken from the baseline, not re-estimated —
    a decay monitor that re-estimates its own yardstick can drift with the thing
    it is supposed to catch.
    """
    if not 0 < alpha < 1 or not 0 < beta < 1:
        raise ValueError("alpha and beta must be between 0 and 1")
    upper = math.log((1 - beta) / alpha)
    lower = math.log(beta / (1 - alpha))
    llr = 0.0
    decision = "continue"
    decided_at: int | None = None
    var = 2 * baseline.sigma ** 2
    for i, value in enumerate(values):
        # log f(x | H0) - log f(x | H1): positive evidence that the edge is intact.
        llr += ((value - mu1) ** 2 - (value - baseline.mean) ** 2) / var
        if llr >= upper:
            decision, decided_at = "edge_intact", i
            break
        if llr <= lower:
            decision, decided_at = "edge_gone", i
            break
    return {"llr": round(llr, 3), "upper": round(upper, 3), "lower": round(lower, 3),
            "decision": decision, "decided_at": decided_at,
            "mu0": round(baseline.mean, 6), "mu1": round(mu1, 6)}


def evaluate(returns: list[float] | np.ndarray, baseline: Baseline, *,
             window: int = 20, slack: float = 0.5, threshold: float = 5.0,
             z_threshold: float = -2.0) -> dict:
    """State of a live track record against its promotion baseline.

    Order matters: ``insufficient`` beats everything, because a verdict on four
    trades is noise with a label. ``decaying`` needs either a concluded SPRT or
    two independent alarms agreeing.
    """
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        raise ValueError("window must be a positive integer")
    values = [float(x) for x in np.asarray(returns, dtype=float).ravel()
              if math.isfinite(float(x))]
    n = len(values)
    if n < max(10, window):
        need = max(10, window)
        return {"state": "insufficient", "n": n, "window": window,
                "baseline": {"mean": round(baseline.mean, 6), "sigma": round(baseline.sigma, 6),
                             "source": baseline.source},
                # `note` 保留原英文句子（旧消费者与测试都读它）；可变参数单独
                # 给一份机器读的，前端不必再用正则从句子里抓 need/have（docs/14 C3）。
                "note": f"need at least {need} closed trades, have {n}",
                "note_meta": {"reason": "sample_below_min", "need": need, "have": n}}

    c = cusum(values, baseline, slack=slack, threshold=threshold)
    r = rolling(values, baseline, window=window, z_threshold=z_threshold)
    s = sprt(values, baseline)

    if s["decision"] == "edge_gone" or (c["alarm"] and r["alarm"]):
        state = "decaying"
    elif c["alarm"] or r["alarm"]:
        state = "watch"
    else:
        state = "ok"

    return {
        "state": state,
        "n": n,
        "window": window,
        "baseline": {"mean": round(baseline.mean, 6), "sigma": round(baseline.sigma, 6),
                     "source": baseline.source},
        "live": {"mean": round(float(np.mean(values)), 6),
                 "last": round(values[-1], 6)},
        "cusum": c,
        "rolling": r,
        "sprt": s,
        "note": "",
        "note_meta": {"reason": "none"},
    }


def monitor(cases: list[dict], baseline: Baseline, *, strategy: str | None = None,
            window: int = 20) -> dict:
    """Evaluate closed cases, optionally filtered to one strategy.

    Cases carry ``pnl_pct`` as a fraction (0.02 = +2%), which is the same unit
    the research layer reports, so no conversion happens here on purpose.
    """
    rows = cases
    if strategy:
        rows = [c for c in rows if str(c.get("strategy") or "llm") == strategy]
    ordered = sorted(rows, key=lambda c: str(c.get("closed_at") or ""))
    returns = [float(c.get("pnl_pct") or 0.0) for c in ordered]
    result = evaluate(returns, baseline, window=window)
    result["strategy"] = strategy
    result["filtered"] = len(rows)
    result["basis"] = "closed_cases"
    return result


def monitor_nav(nav_history: list[dict], baseline: Baseline, *,
                strategy: str | None = None, window: int = 60) -> dict:
    """Same detectors on a daily NAV series, for books without discrete trades.

    The core book rebalances a handful of ETFs and holds them; its track record
    is a NAV curve, not a list of round trips. Comparing a 40-day NAV window
    against a per-trade baseline would be a unit error, so this path exists.
    """
    rows = sorted(nav_history, key=lambda r: str(r.get("trade_date") or ""))
    nav = [float(r["nav"]) for r in rows if r.get("nav") is not None]
    returns = [nav[i] / nav[i - 1] - 1.0 for i in range(1, len(nav)) if nav[i - 1] > 0]
    result = evaluate(returns, baseline, window=window)
    result["strategy"] = strategy
    result["filtered"] = len(nav)
    result["basis"] = "daily_nav"
    return result


def slippage(fills: list[dict], bars: dict[tuple[str, str], float], *,
             side: str = "BUY") -> dict:
    """Realised fill vs the session's open, for open-priced fills.

    This is the one execution fact available without a stored reference price:
    the book fills at the open plus a modelled slippage, so the gap between the
    fill and the open *is* the cost that actually happened. Positive means worse
    than the open.

    Two bases qualify, because both are priced off a session's opening print:
    ``next_open`` (a decision made after the close, filled at the next open) and
    ``open_auction`` (a decision made 09:25–09:30, filled at *that* session's
    open). Counting only the first silently dropped every 09:26 entry from the
    slippage study — the window with the least time between decision and print,
    and therefore the most interesting one. ``ceiling_queue`` does not qualify:
    its price is the board, not the open.
    """
    OPEN_PRICED = {"next_open", "open_auction"}
    errors = []
    for fill in fills:
        if str(fill.get("fill_basis") or "") not in OPEN_PRICED:
            continue
        key = (str(fill.get("symbol")), str(fill.get("trade_date")))
        open_price = bars.get(key)
        price = fill.get("price")
        if not open_price or price is None:
            continue
        try:
            diff = (float(price) / float(open_price) - 1.0) * 10_000
        except (TypeError, ValueError):
            continue
        errors.append(diff if str(side).upper() == "BUY" else -diff)
    if not errors:
        return {"n": 0, "mean_bps": None, "p95_bps": None}
    arr = np.asarray(errors, dtype=float)
    return {"n": len(errors), "mean_bps": round(float(arr.mean()), 1),
            "p95_bps": round(float(np.percentile(arr, 95)), 1)}
