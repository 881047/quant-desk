"""Injected-truth calibration for the statistical referee.

The gate in :mod:`service.research.runner` can say "no". Until this module
existed, nothing in the repository could prove it can say "yes": a gate that
rejects everything and a market with no alpha produce the same output, so the
verdicts in `reports/P5-P8` were evidence about the gate as much as about the
structures.

This module builds panels where the answer is known, runs the *real* gate on
them, and reports its operating characteristic:

``TPR(alpha)``
    How often a structure with a real per-trade edge is admitted.
``FDR(0)``
    How often pure noise is admitted.
``lift``
    TPR minus the admission rate a random gate at the same rate would get. A
    gate that does not beat random admission raises a threshold, it does not
    carry information (arXiv 2610.07701, injected-truth protocol).

Two controls matter more than the headline numbers:

``placebo``
    The same drift is injected on random stock-days that are *not* selected by
    the pool. A gate that admits this is reading the future somewhere.
``null``
    No drift at all. Any admission here is a false discovery by construction.

The synthetic world is deliberately boring: a random walk with a gap and an
intraday leg, a 5% winner threshold, and a drift applied to the *open of t+2*.
That timing is the only legal place a T+1 stock can be helped: the pool is known
at t, entry is t+1 open, and the earliest exit is t+2 open.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product

import numpy as np
import pandas as pd

from .runner import STRESS_MULTIPLIERS, run
from .spec import SCAN_THRESHOLD, EventStudy, SpecError


@dataclass(frozen=True)
class PanelConfig:
    """Shape of the synthetic market. Small by default so tests stay fast."""

    symbols: int = 240
    days: int = 260
    sigma: float = 0.022          # intraday log-ish noise, per symbol-day
    gap_sigma: float = 0.004      # overnight noise
    threshold: float = 0.05       # must match spec.SCAN_THRESHOLD: run() hardcodes it
    start: str = "20230102"
    price0: float = 10.0
    volume: float = 2.0e7
    seed: int = 0

    def __post_init__(self) -> None:
        if abs(self.threshold - SCAN_THRESHOLD) > 1e-12:
            raise ValueError(
                f"threshold must equal spec.SCAN_THRESHOLD ({SCAN_THRESHOLD}): "
                "runner.run() builds the winners pool with that constant, so a "
                "different cut would inject alpha into names the gate never selects"
            )

    def dates(self) -> list[str]:
        return list(pd.bdate_range(self.start, periods=self.days).strftime("%Y%m%d"))


@dataclass(frozen=True)
class World:
    """One known truth, to be handed to the gate as if it were a discovery."""

    name: str
    alpha: float = 0.0            # per-trade drift on the post-signal leg
    mode: str = "signal"          # signal | placebo | null

    def __post_init__(self) -> None:
        if self.mode not in {"signal", "placebo", "null"}:
            raise ValueError(f"bad mode: {self.mode}")
        if self.mode == "null" and self.alpha != 0:
            raise ValueError("a null world cannot carry alpha")


class SyntheticStore:
    """The minimum ``store`` the research layer needs: ``read(name, start, end)``."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self._frames = frames

    def read(self, name: str, start: str | None = None,
             end: str | None = None) -> pd.DataFrame:
        frame = self._frames.get(name)
        if frame is None:
            return pd.DataFrame()
        return frame.copy()


def build_world(cfg: PanelConfig, world: World, seed: int | None = None) -> dict[str, pd.DataFrame]:
    """A long-format daily panel plus an empty limit table.

    The panel is built forwards in time, one session at a time, because the
    drift is conditional on a *signal that is observable at the close*: a name
    that closes up 5% today gaps by ``alpha`` at the open two sessions later.
    Building it any other way lets the injection change the very signal that
    selected it, which dilutes the measured edge and would make the control lie
    about the gate's power.
    """
    rng = np.random.default_rng(cfg.seed if seed is None else seed)
    sym, day = cfg.symbols, cfg.days

    gap = rng.normal(0.0, cfg.gap_sigma, size=(sym, day))
    intraday = rng.normal(0.0, cfg.sigma, size=(sym, day))

    placebo = np.zeros((sym, day), dtype=bool)
    if world.mode == "placebo" and world.alpha:
        # Same number of injections, same size, no relation to the pool.
        pct0 = (1.0 + gap) * (1.0 + intraday) - 1.0
        count = min(int((pct0 >= cfg.threshold).sum()), sym * (day - 2))
        picked = rng.choice(sym * (day - 2), size=count, replace=False)
        ii, jj = np.unravel_index(picked, (sym, day - 2))
        placebo[ii, jj + 2] = True

    open_ = np.empty((sym, day))
    close = np.empty((sym, day))
    pre = np.empty((sym, day))
    winner = np.zeros((sym, day), dtype=bool)
    for t in range(day):
        g = gap[:, t]
        if t >= 2 and world.alpha:
            if world.mode == "signal":
                g = g + world.alpha * winner[:, t - 2]
            elif world.mode == "placebo":
                g = g + world.alpha * placebo[:, t]
        pre[:, t] = cfg.price0 if t == 0 else close[:, t - 1]
        open_[:, t] = cfg.price0 if t == 0 else close[:, t - 1] * (1.0 + g)
        close[:, t] = open_[:, t] * (1.0 + intraday[:, t])
        winner[:, t] = (close[:, t] / pre[:, t] - 1.0) >= cfg.threshold

    volume = rng.lognormal(np.log(cfg.volume), 0.25, size=(sym, day))
    codes = np.array([f"{600000 + i:06d}.SH" for i in range(sym)])
    dates = np.array(cfg.dates())

    frame = pd.DataFrame({
        "ts_code": np.repeat(codes, day),
        "trade_date": np.tile(dates, sym),
        "open": open_.ravel(),
        "high": np.maximum(open_, close).ravel() * 1.002,
        "low": np.minimum(open_, close).ravel() * 0.998,
        "close": close.ravel(),
        "pre_close": pre.ravel(),
        "volume": volume.ravel().astype("int64"),
        "amount": (close.ravel() * volume.ravel() / 1000.0),
    })
    return {"daily": frame, "stk_limit": pd.DataFrame()}


def default_spec(cfg: PanelConfig) -> EventStudy:
    """The same shape as the nine rejected structures: pool -> next open -> T+1."""
    dates = cfg.dates()
    return EventStudy(
        pool="winners", boards=(1,), entry="next_open", hold=1, exit_at="open",
        cost_bps=10.0, start=dates[0], end=dates[-1], max_per_day=0,
    )


def run_world(cfg: PanelConfig, world: World, seed: int | None = None,
              spec: EventStudy | None = None) -> dict:
    """Run the real gate once against a known world. No shortcuts, no mocks."""
    frames = build_world(cfg, world, seed)
    store = SyntheticStore(frames)
    try:
        payload = run(store, spec or default_spec(cfg))
    except SpecError as exc:
        return {"world": world.name, "alpha": world.alpha, "error": str(exc),
                "admitted": False, "verdict": "error", "shadow_candidate": False}
    reading = payload.get("gate_reading") or payload.get("verdict")
    return {
        "world": world.name,
        "alpha": world.alpha,
        "admitted": bool(payload.get("shadow_candidate")),
        "promoted": reading == "promoted",
        # 机械读数（预注册阈值），不是判决。两个键都认：2026-11-03 之前叫 verdict。
        "gate_reading": reading,
        "verdict": reading,
        "trades": (payload.get("best") or {}).get("trades", {}).get("n", 0),
        "excess": (payload.get("best") or {}).get("trades", {}).get("mean_excess"),
        "p_value": (payload.get("best") or {}).get("trades", {}).get("p_value"),
        "sharpe": (payload.get("best") or {}).get("metrics", {}).get("sharpe"),
        "dsr": (payload.get("deflated_sharpe") or {}).get("dsr"),
        "pbo": (payload.get("pbo") or {}).get("pbo"),
        "cpcv_p05": (payload.get("cpcv") or {}).get("oos_sharpe_p05"),
        "trials": payload.get("trials"),
        "cost_bps": payload.get("cost_bps_effective"),
        "ticket_cny": (payload.get("cost") or {}).get("ticket_cny"),
        "blockers": payload.get("shadow_blockers") or [],
    }


def _finite(value) -> float:
    """`x or 0.0` is not a NaN guard: NaN is truthy, so the `or` never fires."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _summarise(rows: list[dict]) -> dict:
    if not rows:
        return {"runs": 0, "admitted": 0, "rate": 0.0, "verdicts": {}}
    admitted = sum(1 for r in rows if r["admitted"])
    verdicts: dict[str, int] = {}
    for row in rows:
        verdicts[str(row.get("verdict"))] = verdicts.get(str(row.get("verdict")), 0) + 1
    return {
        "runs": len(rows),
        "admitted": admitted,
        "rate": round(admitted / len(rows), 4),
        "promoted": sum(1 for r in rows if r.get("promoted")),
        "verdicts": verdicts,
        "median_trades": float(np.median([r.get("trades") or 0 for r in rows])),
        "median_excess": float(np.median([_finite(r.get("excess")) for r in rows])),
    }


# A ladder of edges, not a pass/fail pair. The `strong` rung sits at 3% because
# the 2x cost stress is now a real 2x: with the multiplier applied to commission
# as well as slippage, a 2%/trade structure dies at that gate on this account
# (where the 5-yuan minimum commission dominates), so keeping it as the top rung
# would label the calibration "TPR 0" and hide the fact that the gate works.
DEFAULT_WORLDS = (
    World("null", 0.0, "null"),
    World("placebo", 0.010, "placebo"),
    World("weak", 0.005, "signal"),
    World("mid", 0.012, "signal"),
    World("good", 0.020, "signal"),
    World("strong", 0.030, "signal"),
)


def calibrate(cfg: PanelConfig | None = None, worlds: tuple[World, ...] = DEFAULT_WORLDS,
              seeds: tuple[int, ...] = (0, 1, 2, 3, 4)) -> dict:
    """Run every world across seeds and return the gate's operating characteristic.

    ``lift`` is the number that decides whether the gate is worth keeping: at the
    observed adoption rate, a gate that ignores the evidence admits true signals
    at exactly that rate. Only a gate that beats it is measuring something.

    TPR is computed over worlds whose edge clears the bar the gate actually
    enforces: the referee rejects anything that dies at the 2x cost stress, so a
    world below ``2 x cost`` is *expected* to be rejected. Counting it as a miss
    would punish a calibrated gate for being calibrated.
    """
    cfg = cfg or PanelConfig()
    rows: list[dict] = []
    per_world: dict[str, dict] = {}

    for world, seed in product(worlds, seeds):
        row = run_world(cfg, world, seed)
        rows.append(row)
        per_world.setdefault(world.name, {"alpha": world.alpha, "rows": []})["rows"].append(row)

    summary = {name: _summarise(block["rows"]) for name, block in per_world.items()}
    charged = [r["cost_bps"] for r in rows if r.get("cost_bps")]
    cost_floor = float(np.median(charged)) / 10000 if charged else 0.0
    stress = STRESS_MULTIPLIERS[0]
    admission_floor = cost_floor * stress
    positive = [r for r in rows
                if r["world"] != "placebo" and (r.get("alpha") or 0.0) > admission_floor]
    adoption = (sum(1 for r in rows if r["admitted"]) / len(rows)) if rows else 0.0
    tpr = (sum(1 for r in positive if r["admitted"]) / len(positive)) if positive else 0.0

    return {
        "config": {"symbols": cfg.symbols, "days": cfg.days, "sigma": cfg.sigma,
                   "threshold": cfg.threshold, "seeds": list(seeds),
                   "cost_floor_bps": round(cost_floor * 10000, 1),
                   "admission_floor_bps": round(admission_floor * 10000, 1),
                   "stress_mult": stress},
        "worlds": {name: {"alpha": block["alpha"], **summary[name]}
                   for name, block in per_world.items()},
        "runs": rows,
        "pooled": {
            "runs": len(rows),
            "adoption_rate": round(adoption, 4),
            "positive_runs": len(positive),
            "tpr": round(tpr, 4),
            "random_tpr": round(adoption, 4),
            "lift": round(tpr - adoption, 4),
            "carries_information": bool(positive) and tpr > adoption,
        },
    }
