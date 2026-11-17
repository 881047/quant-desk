"""Capacity is a 09:30 question, so it may not use the 15:00 volume.

The cap exists to stop the model from "absorbing" ten percent of a session's
turnover at the open. It used to read ``bar.volume`` — the whole day's volume,
which is not known when the order is priced. A stock that traded a hundred times
its normal size after 10:00 therefore made a 09:30 order ten times larger than
anyone could have justified at 09:30. The direction was optimistic, which is the
worst direction for a capacity rule.

``prior_volume`` is the previous session's turnover: printed, observable, and a
defensible proxy for how much can clear at the open.
"""

from __future__ import annotations

from datetime import datetime

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


def _bar(*, volume: int, prior_volume: int) -> Bar:
    return Bar(
        symbol="600000.SH", ts=TS,
        open=D("10.00"), high=D("10.50"), low=D("9.80"), close=D("10.20"),
        pre_close=D("10.00"), volume=volume, amount=D("10000000"),
        up_limit=D("11.00"), down_limit=D("9.00"),
        prior_volume=prior_volume,
    )


def _order(qty: int = 1000) -> Order:
    return Order(order_id="O1", client_order_id="c1", symbol="600000.SH",
                 side=Side.BUY, qty=qty, order_type=OrderType.MARKET,
                 status=OrderStatus.NEW)


def _filled_qty(bar: Bar, qty: int = 1000) -> int:
    result = BarExecutionModel().match(_order(qty), bar, INST, TS, CAL)
    assert result.filled, result.reject_reason
    return result.fill.qty


def test_the_cap_reads_the_prior_session():
    """1,000 shares traded yesterday -> at most 100 shares at today's open."""
    assert _filled_qty(_bar(volume=10_000_000, prior_volume=1_000)) == 100


def test_a_thin_prior_session_does_not_limit_a_thick_one():
    assert _filled_qty(_bar(volume=1_000, prior_volume=10_000_000)) == 1000


def test_todays_volume_cannot_change_the_fill():
    """Same past, same order, wildly different afternoon: same fill."""
    quiet = _filled_qty(_bar(volume=1_000, prior_volume=5_000))
    busy = _filled_qty(_bar(volume=500_000_000, prior_volume=5_000))
    assert quiet == busy == 500


def test_a_bar_without_prior_volume_keeps_the_old_fallback():
    """Synthetic bars in tests carry no history; the fallback is deliberate."""
    assert _filled_qty(_bar(volume=1_000, prior_volume=0)) == 100


def _rule(bar: Bar, qty: int = 1000) -> str:
    result = BarExecutionModel().match(_order(qty), bar, INST, TS, CAL)
    assert result.filled, result.reject_reason
    return result.fill.rule_id


def test_the_fallback_basis_is_named_when_it_caps_a_fill():
    """Using today's volume to justify an open fill is a compromise, not a fact.

    It cannot be avoided when the builder has no prior session (a resumed stock,
    a synthetic fixture), and at this account size the cap rarely binds — so the
    honest thing is to record which basis the cap used instead of letting a
    15:00 number pass as a 09:30 one.
    """
    assert _rule(_bar(volume=1_000, prior_volume=0)) == "CONTINUOUS+VOLUME_CAP_TODAY"


def test_a_live_bar_using_the_sessions_cumulative_volume_is_not_a_compromise():
    """At 10:31 the cumulative volume is on screen; nothing is being looked back at."""
    bar = _bar(volume=1_000, prior_volume=0)
    live = Bar(**{**bar.__dict__, "priced_at": "live"})
    assert _rule(live) == "CONTINUOUS+VOLUME_CAP"


def test_a_cap_that_did_not_bind_leaves_no_trace():
    """The label marks the constraint, not every bar that happened to lack history."""
    assert _rule(_bar(volume=10_000_000, prior_volume=0), qty=100) == "CONTINUOUS"
