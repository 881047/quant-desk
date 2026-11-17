"""Capital allocation across strategies.

Deliberately boring, because the literature says boring wins here: DeMiguel,
Garlappi and Uppal (2009) show 1/N survives out of sample against fourteen
optimised schemes, and practitioner guides put inverse volatility ahead of full
mean-variance once covariance estimates get noisy. With a handful of strategies
and a few dozen closed trades, a covariance matrix would be fiction.

So the weight is a product of four explainable factors:

    w_i ∝ 1(eligible in this regime) × probation × track-record × drawdown throttle

then normalised, then capped by each strategy's hard max_weight.

Every factor returns a reason string, so ``/strategies`` can explain itself
instead of printing a number nobody trusts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .strategy import REGISTRY, StrategyMeta


@dataclass(frozen=True)
class StrategyStats:
    n: int = 0
    mean_excess: float | None = None
    std_excess: float | None = None
    max_drawdown: float = 0.0
    mean_raw: float | None = None
    std_raw: float | None = None
    basis: str = "raw_pnl_fallback"
    raw_closed: int = 0
    unscored: int = 0

    @property
    def has_excess(self) -> bool:
        return self.mean_excess is not None and self.std_excess is not None

    @property
    def se(self) -> float:
        if self.n <= 1 or not self.has_excess or self.std_excess <= 0:
            return float("inf")
        return self.std_excess / math.sqrt(self.n)

    @property
    def proven(self) -> bool:
        return (self.has_excess and self.n >= 20
                and self.mean_excess - 1.96 * self.se > 0)

    @property
    def discredited(self) -> bool:
        return (self.has_excess and self.n >= 20
                and self.mean_excess + 1.96 * self.se < 0)


def _factor(meta: StrategyMeta, stats: StrategyStats) -> tuple[float, list[str]]:
    reasons: list[str] = []
    factor = 1.0

    if meta.status == "probation":
        factor *= 0.5
        reasons.append("probation: 只给半额")
    if not stats.has_excess:
        factor *= 0.5
        reasons.append("缺少同窗口超额样本：未证明，减半")
    elif stats.n < 20:
        factor *= 0.5
        reasons.append(f"样本 {stats.n}/20：未证明，减半")
    elif stats.discredited:
        factor *= 0.25
        reasons.append("20 笔以上且超额区间全负：降到 1/4")
    elif stats.proven:
        factor *= 1.5
        reasons.append("20 笔以上且超额区间为正：加到 1.5 倍")
    else:
        reasons.append("20 笔以上但区间跨零：维持基准")

    if stats.max_drawdown <= -0.08:
        factor *= 0.5
        reasons.append(f"回撤 {stats.max_drawdown:.1%}：再减半")
    return factor, reasons


def allocate(regime: str, stats: dict[str, StrategyStats] | None = None,
             *, include_cash: bool = True, book: str | None = "paper") -> dict:
    """Return target capital weights for this regime.

    A strategy that is not eligible in the current regime gets exactly zero —
    not "a small weight". Half-sizing a mean-reversion book inside a trend
    breakdown is how a small account dies slowly.
    """
    stats = stats or {}
    raw: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    skipped: dict[str, str] = {}

    for key, meta in REGISTRY.items():
        if book is not None and meta.book != book:
            skipped[key] = f"spends from the {meta.book} book"
            continue
        if meta.status in ("research", "retired"):
            skipped[key] = f"status={meta.status}"
            continue
        if not meta.auto_allocate:
            skipped[key] = "capital requires an explicit frozen target_weight"
            continue
        if regime not in meta.regimes:
            skipped[key] = f"{regime} 不在允许区间 {list(meta.regimes)}"
            continue
        st = stats.get(key, StrategyStats())
        factor, why = _factor(meta, st)
        raw[key] = factor
        reasons[key] = why

    total = sum(raw.values())
    weights: dict[str, float] = {}
    if total > 0:
        weights = {k: v / total for k, v in raw.items()}

    # Hard caps until fixed point: each pass caps at least one strategy and the
    # room set only shrinks, so there is no arbitrary five-iteration truncation.
    while True:
        over = {k: w for k, w in weights.items() if w > REGISTRY[k].max_weight + 1e-12}
        if not over:
            break
        freed = 0.0
        for k, w in over.items():
            freed += w - REGISTRY[k].max_weight
            weights[k] = REGISTRY[k].max_weight
            reasons.setdefault(k, []).append(f"触及上限 {REGISTRY[k].max_weight:.0%}")
        room = {k: w for k, w in weights.items()
                if w < REGISTRY[k].max_weight - 1e-12}
        room_total = sum(room.values())
        if room_total <= 0:
            break
        for k, share in room.items():
            weights[k] += freed * share / room_total

    residual = max(0.0, 1.0 - sum(weights.values()))
    cash = residual if include_cash else 0.0
    return {
        "regime": regime,
        "weights": {k: round(v, 4) for k, v in sorted(weights.items(), key=lambda kv: -kv[1])},
        "cash_weight": round(cash, 4),
        "residual_weight": round(residual, 4),
        "residual_owner": "cash" if include_cash else "unallocated",
        "reasons": {k: reasons.get(k, []) for k in weights},
        "skipped": skipped,
        "book": book,
        "method": ("eligible × probation × track-record × drawdown, normalised then capped; "
                   "inverse-volatility is approximated by the sample-size probation factor "
                   "until each strategy has >=20 closed trades"),
    }


def stats_from_cases(cases: list[dict], arena_decisions: list[dict] | None = None
                     ) -> dict[str, StrategyStats]:
    """Combine raw cases with same-window excess without conflating the two.

    When arena rows are supplied, only a ``case_id`` match contributes to ``n``
    and the excess moments. Unmatched cases remain visible as raw/unscored counts.
    The optional argument preserves the old single-argument API, where an already
    embedded ``excess``/``excess_pct`` is still preferred over raw pnl.
    """
    decisions = None
    if arena_decisions is not None:
        decisions = {str(row.get("case_id")): row for row in arena_decisions
                     if row.get("case_id")}
    # A drawdown is a property of the *order the trades happened in*, so the
    # sequence has to be chronological before it is compounded. ``cases`` arrives
    # newest-first, which made the walk run backwards: the equity curve started
    # at yesterday's outcome and ended at the first trade ever, so a strategy
    # that fell early and recovered read as a deep drawdown and one that fell
    # late read as flat. That number halves a strategy's capital.
    ordered = sorted(
        cases,
        key=lambda case: str(case.get("closed_at") or case.get("opened_at") or ""),
    )
    raw_by: dict[str, list[float]] = {}
    excess_by: dict[str, list[float]] = {}
    for case in ordered:
        key = str(case.get("strategy") or "llm")
        raw_by.setdefault(key, []).append(float(case.get("pnl_pct") or 0.0))
        if decisions is not None:
            decision = decisions.get(str(case.get("case_id") or "")) or {}
            # User-directed and unknown tickets are not evidence of the model's
            # judgement, even though arena still reports their realised return.
            excess = (decision.get("excess")
                      if decision.get("directed_by") in (None, "agent") else None)
        else:
            excess = (case.get("excess")
                      if case.get("directed_by") in (None, "agent") else None)
            if excess is None and case.get("directed_by") in (None, "agent"):
                excess = case.get("excess_pct")
        if excess is not None:
            excess_by.setdefault(key, []).append(float(excess))

    def moments(values: list[float]) -> tuple[float, float]:
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1) \
            if len(values) > 1 else 0.0
        return mean, math.sqrt(var)

    out: dict[str, StrategyStats] = {}
    for key, raw_values in raw_by.items():
        raw_mean, raw_std = moments(raw_values)
        equity, peak, dd = 1.0, 1.0, 0.0
        for value in raw_values:
            equity *= 1 + value
            peak = max(peak, equity)
            dd = min(dd, equity / peak - 1)
        excess_values = excess_by.get(key) or []
        excess_mean = excess_std = None
        if excess_values:
            excess_mean, excess_std = moments(excess_values)
        out[key] = StrategyStats(
            n=len(excess_values), mean_excess=excess_mean, std_excess=excess_std,
            max_drawdown=dd, mean_raw=raw_mean, std_raw=raw_std,
            basis="arena_same_window_excess" if excess_values else "raw_pnl_fallback",
            raw_closed=len(raw_values), unscored=len(raw_values) - len(excess_values))
    return out
