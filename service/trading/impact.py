"""Pre-trade cost: square-root impact and a participation schedule.

**This module estimates; it does not charge.** The matcher prices every fill at
fees + a fixed slippage (10bps on stocks, 5bps on ETFs), because a daily bar
cannot tell you what the book looked like when the order arrived. Impact needs a
depth proxy that only exists once you have L2 data, so it stays advisory — and
``round_trip_cost_bps`` below is what makes that explicit: it returns the number
the desk will actually pay, so a caller can put the two side by side instead of
quoting 38bps while the ledger quietly books 25.

The model is the practitioner standard rather than a new one:

    impact_bps = c * sigma_daily * sqrt(Q / ADV) * 10_000

with ``c`` around 0.2-0.5; 0.4 is used for A-share small caps, where liquidity is
thinner than the US names the coefficient was fitted on. This is an estimate,
not a law — its job is to stop a 30% participation order from being treated as
free.

Scheduling follows the same references: POV keeps participation constant, TWAP
splits evenly. Above 30% participation, execution is split across days instead
of pretending one session can absorb it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from service import settings
from service.kernel.costs import CostModel
from service.kernel.execution import slippage_bps_for
from service.kernel.models import D, Instrument

# All of these live in configs/params.yaml (impact.* and costs.*). They were
# module constants here while the same slippage numbers sat in costs.yaml and in
# kernel.execution.BarExecutionModel - three copies, two of them live.
C_SMALL_CAP = settings.num("impact.c_small_cap")
MAX_SCHEDULE_DAYS = settings.integer("impact.max_schedule_days")
MAX_PARTICIPATION = settings.num("impact.max_participation")


@dataclass(frozen=True)
class Impact:
    qty: int
    price: float
    adv_shares: float
    sigma_daily: float
    participation: float
    impact_bps: float
    cost_cny: float
    days_needed: int
    schedule: list[int]

    def as_dict(self) -> dict:
        return {
            "qty": self.qty,
            "price": round(self.price, 3),
            "adv_shares": int(self.adv_shares),
            "sigma_daily": round(self.sigma_daily, 4),
            "participation": round(self.participation, 4),
            "impact_bps": round(self.impact_bps, 2),
            "cost_cny": round(self.cost_cny, 2),
            "cost_scope": "single_side_modelled_impact_only_excludes_fees_taxes_slippage",
            "days_needed": self.days_needed,
            "schedule": self.schedule,
            "schedule_scope": "liquidity_twap_not_fillability_or_order_authorization",
            "note": ("impact_bps 为单边估计；participation>30% 时自动拆成多日 TWAP，"
                     "不要把一日成交当成可行。cost_cny 只含模型冲击，不是完整费用。"),
        }


def _finite(value: float, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def estimate(qty: int, price: float, adv_cny: float, sigma_daily: float,
             *, c: float = C_SMALL_CAP,
             max_participation: float = MAX_PARTICIPATION) -> Impact:
    qty = max(0, int(_finite(qty, "qty")))
    price = max(_finite(price, "price"), 1e-9)
    adv_cny = _finite(adv_cny, "adv_cny")
    sigma = max(_finite(sigma_daily, "sigma_daily"), 1e-4)
    c = _finite(c, "c")
    max_participation = _finite(max_participation, "max_participation")
    if max_participation <= 0:
        raise ValueError("max_participation must be positive")
    if adv_cny <= 0:
        # ``max(adv_cny / price, 1.0)`` turned "we do not know this name's
        # ADV" into "its ADV is one share", which made a 20,000-share order
        # need 66,667 sessions: a schedule loop that runs that many times and
        # an impact figure nobody should believe. A missing denominator is not
        # a tiny one, so callers that can answer without impact skip it.
        raise ValueError("adv_cny must be positive")
    adv_shares = adv_cny / price

    ratio = qty / adv_shares
    days_needed = 1
    if ratio > max_participation:
        # Capped so the schedule can never be unbounded, whatever the inputs:
        # one trading year is already well past the point where "can this be
        # executed" answers itself.
        days_needed = min(MAX_SCHEDULE_DAYS,
                          math.ceil(ratio / max_participation))

    per_day = math.ceil(qty / days_needed)
    schedule = []
    left = qty
    for _ in range(days_needed):
        take = min(per_day, left)
        schedule.append(take)
        left -= take
        if left <= 0:
            break

    # Impact is nonlinear in participation: the whole-order estimate is not the
    # cost of the schedule. Re-price each day, then aggregate both CNY and bps.
    daily = []
    for day_qty in schedule:
        day_participation = day_qty / adv_shares
        day_bps = c * sigma * math.sqrt(day_participation) * 10_000.0
        daily.append((day_participation, day_bps,
                      day_qty * price * day_bps / 10_000.0))
    participation = max((row[0] for row in daily), default=0.0)
    cost_cny = sum(row[2] for row in daily)
    impact_bps = cost_cny / (qty * price) * 10_000.0 if qty else 0.0
    return Impact(qty=qty, price=price, adv_shares=adv_shares, sigma_daily=sigma,
                  participation=participation, impact_bps=impact_bps,
                  cost_cny=cost_cny, days_needed=len(schedule), schedule=schedule)


def participation(qty: int, price: float, adv_cny: float) -> float:
    adv_shares = max(float(adv_cny) / max(price, 1e-9), 1.0)
    return qty / adv_shares


def finite(value: object, default: float = 0.0) -> float:
    """A float, or ``default`` when the value is missing or not finite.

    Replaces ``float(x or 0.0)``, which does not work: ``nan`` is truthy, so a
    NaN standard deviation sailed through the ``or`` and then made
    :func:`estimate` refuse the whole calculation with "sigma_daily must be
    finite". The caller lost the exit-cost estimate because of a missing value
    it could have read as "no volatility information".
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def round_trip_cost_bps(notional_cny: float, instrument: Instrument, *,
                        slippage_bps: int | None = None,
                        cost_model: CostModel | None = None) -> dict:
    """What the desk actually charges for a round trip at this notional.

    The matcher's own number, not an estimate of it: fees from the kernel's
    ``CostModel`` at these exact rates, plus the slippage the execution model
    applies per side. Anything quoting a cost should quote this, and add
    ``estimate().impact_bps`` as a separate, clearly labelled premium.
    """
    if notional_cny <= 0:
        return {"fees_bps": 0.0, "slippage_bps": 0, "total_bps": 0.0,
                "model": "kernel"}
    # The kernel's own table, by asset class. This used to re-derive the choice
    # from ``is_etf``, which gave a convertible bond the stock number while the
    # matcher gave it something else.
    slip = (slippage_bps if slippage_bps is not None
            else round(slippage_bps_for(instrument)))
    model = cost_model or CostModel()
    notional = D(str(round(float(notional_cny), 2)))
    fees_bps = model.round_trip_bps(notional, instrument)
    return {"fees_bps": round(fees_bps, 2), "slippage_bps": 2 * slip,
            "total_bps": round(fees_bps + 2 * slip, 2), "model": "kernel"}
