"""A missing limit column must not become a fill tag.

``Bar.limits()`` hands back ``(0, 0)`` when neither the bar nor the prior close
can supply a band, and every caller is supposed to read a non-positive limit as
"unknown". ``microstructure.blocked_at_open`` does, ``fill_price_at_open`` does,
``_match_close`` does — the two ``+CAPPED_*`` labels in ``_match_continuous``
did not. ``price >= Decimal("0")`` is true for every price ever printed, so a
bar with no band stamped ``+CAPPED_UP`` on every buy and the ledger recorded a
ceiling the market never had.

That tag is the only place a fill says "slippage hit the board". Read it as a
fact and a review concludes the desk kept buying sealed ceilings; read it as
noise and the one real cap disappears. Neither is recoverable after the fact,
which is why the guard belongs on the comparison and not in the reader.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.execution import BarExecutionModel
from service.kernel.models import (
    Bar,
    D,
    Instrument,
    Order,
    OrderStatus,
    OrderType,
    Side,
)

TS = datetime(2026, 10, 25, 9, 30, tzinfo=SHANGHAI)
CAL = TradingCalendar.always()
INST = Instrument("600000.SH", min_qty=100, qty_step=100)


def _bar(*, up_limit: str, down_limit: str, pre_close: str,
         open_px: str = "10.00") -> Bar:
    """A bar whose own limit columns carry whatever the caller says.

    ``prior_volume`` is large on purpose: a small one adds ``+VOLUME_CAP_TODAY``
    and the assertion below would still pass while measuring the wrong thing.
    """
    return Bar(
        symbol="600000.SH", ts=TS,
        open=D(open_px), high=D("10.50"), low=D("9.80"), close=D("10.20"),
        pre_close=D(pre_close), volume=10_000_000, amount=D("100000000"),
        up_limit=D(up_limit), down_limit=D(down_limit),
        prior_volume=10_000_000,
    )


def _order(side: Side) -> Order:
    return Order(order_id="O1", client_order_id="c1", symbol="600000.SH",
                 side=side, qty=1000, order_type=OrderType.MARKET,
                 status=OrderStatus.NEW)


def _rule(bar: Bar, side: Side = Side.BUY) -> str:
    result = BarExecutionModel().match(_order(side), bar, INST, TS, CAL)
    assert result.filled, result.reject_reason
    return result.fill.rule_id


def test_no_band_no_ceiling_tag():
    """Bar carries no limits and no prior close: the fill is still a fill."""
    rule = _rule(_bar(up_limit="0", down_limit="0", pre_close="0"))
    assert "CAPPED_UP" not in rule, rule
    assert rule.startswith("CONTINUOUS"), rule


def test_a_missing_prior_close_cannot_supply_a_band_either():
    """``Instrument.band`` needs a positive prior close; zero is not one.

    This is the path that actually produces ``(0, 0)``: the bar has no limit
    column *and* the vendor row has no prior close (a resumed or newly listed
    name). Deriving from ``0`` would print a ceiling of ``0.00``, so it refuses
    — and the caller has to take that refusal as "unknown".
    """
    assert INST.band(D("0")) == (D("0"), D("0"))
    rule = _rule(_bar(up_limit="0", down_limit="0", pre_close="0"))
    assert "CAPPED_UP" not in rule, rule


def test_a_real_ceiling_is_still_tagged():
    """The guard must not turn the label off: 10.99 + 10bps reaches 11.00."""
    rule = _rule(_bar(up_limit="11.00", down_limit="9.00", pre_close="10.00",
                      open_px="10.99"))
    assert "+CAPPED_UP" in rule, rule


def test_a_real_floor_is_still_tagged():
    rule = _rule(_bar(up_limit="11.00", down_limit="9.00", pre_close="10.00",
                      open_px="9.01"), side=Side.SELL)
    assert "+CAPPED_DOWN" in rule, rule


def test_a_missing_floor_does_not_tag_a_sell():
    """The sell side never mis-tagged, because no printed price is <= 0.

    Kept so the asymmetry is written down: the buy side needed a guard, the
    sell side needs the guard for symmetry only. Someone reading ``+CAPPED_UP``
    and ``+CAPPED_DOWN`` as two instances of one bug would go looking for a
    sell-side incident that never happened.
    """
    rule = _rule(_bar(up_limit="0", down_limit="0", pre_close="0"),
                 side=Side.SELL)
    assert "CAPPED_DOWN" not in rule, rule


def test_both_labels_are_guarded_in_source():
    """Meta-assertion: two ``+CAPPED_*`` sites, each behind a positive check.

    The behavioural cases above pin today's two branches. This one fails if a
    third site is added without the guard — which is how the first two drifted
    away from ``blocked_at_open`` in the first place.
    """
    source = Path("service/kernel/execution.py").read_text(encoding="utf-8")
    lines = source.splitlines()
    sites = [i for i, line in enumerate(lines) if 'rule += "+CAPPED_' in line]
    assert len(sites) == 2, [lines[i].strip() for i in sites]
    for index in sites:
        line = lines[index].strip()
        guard = "up_limit > 0" if "CAPPED_UP" in line else "down_limit > 0"
        # The condition is the line above: ``if is_buy and up_limit > 0 and ...``
        condition = lines[index - 1].strip()
        assert condition.startswith(("if ", "elif ")), condition
        assert guard in condition, f"{line!r} guarded by {condition!r}, not {guard}"

    tree = ast.parse(source)
    compared = {
        node.comparators[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and isinstance(node.comparators[0], ast.Name)
    }
    # ``up_limit``/``down_limit`` are read all over this module; the assertion
    # above is about the tag lines, and this only records that the names the
    # guard checks are still the ones the comparisons read.
    assert {"up_limit", "down_limit"} <= compared
