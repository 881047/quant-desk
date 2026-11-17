"""One definition of "can this order fill at the open, and at what price".

The kernel and the research layer each encode this rule, and they disagreed: the
research layer refused a next-open entry when the open sat at the upper limit,
while the kernel filled it at the limit — the same bar, two answers, neither of
them derived from the other.

The rule itself is short enough to state once:

* an order sent for the open is priced off the open, never off the close;
* a board already sealed at the open cannot be bought, a limit-down open cannot
  be sold, and that is a question about the *open*, not about the rest of the day;
* slippage moves the price against you, and the limit band caps it.

This module has no pandas and no I/O, because the kernel must stay importable
from a bare script. The research layer evaluates the same rule vectorised over a
DataFrame; ``tests/research/test_microstructure_parity.py`` asserts the two agree
across the whole band, so the duplication is of *evaluation strategy*, not of the
rule.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .models import D, Side
from .units import PRICE_EPS, PRICE_TICK

# Re-exported for the callers that import it from here; the definition itself
# lives in ``units`` so the "is this the limit price" tolerance cannot drift
# between the kernel and the research layer.
__all__ = ["PRICE_EPS", "blocked_at_open", "fill_price_at_open"]


def blocked_at_open(
    side: Side,
    open_px: Decimal,
    up_limit: Decimal | None = None,
    down_limit: Decimal | None = None,
) -> str | None:
    """Why an order sent for this open cannot fill, or ``None`` if it can.

    A non-positive limit means "this bar did not carry one", not "the ceiling is
    zero": the caller derives a band from the instrument or passes ``None``. The
    ``> 0`` guard is what stops a missing column from refusing every buy with
    "opened at the upper limit".
    """
    if open_px <= 0:
        return "no opening price"
    if (side == Side.BUY and up_limit is not None and up_limit > 0
            and open_px >= up_limit - PRICE_EPS):
        return "opened at the upper limit"
    if (side == Side.SELL and down_limit is not None and down_limit > 0
            and open_px <= down_limit + PRICE_EPS):
        return "opened at the lower limit"
    return None


def fill_price_at_open(
    side: Side,
    base: Decimal,
    slip_bps: float,
    up_limit: Decimal,
    down_limit: Decimal,
    tick: Decimal = PRICE_TICK,
) -> Decimal:
    """Apply slippage against the trader and clamp to the daily band.

    ``base`` is the price the order would get before slippage: the open for a
    market order, ``min(open, limit)`` for a buy and ``max(open, limit)`` for a
    sell. Passing the open straight through would ignore the limit price the
    trader set; passing the limit would ignore the improvement the auction gave.

    ``tick`` is the instrument's own quotation step. Rounding every fill to one
    fen reported convertible-bond fills at a price the exchange cannot print: a
    bond quotes in 0.001, so 100.234 became 100.23 and every downstream P&L was
    off by up to 0.4bp on a instrument whose whole edge is measured in basis
    points.
    """
    step = tick if tick is not None and tick.is_finite() and tick > 0 else PRICE_TICK
    slip = D(str(slip_bps)) / D("10000")
    if side == Side.BUY:
        price = (base * (D("1") + slip)).quantize(step, rounding=ROUND_HALF_UP)
        return min(price, up_limit) if up_limit > 0 else price
    price = (base * (D("1") - slip)).quantize(step, rounding=ROUND_HALF_UP)
    return max(price, down_limit) if down_limit > 0 else price
