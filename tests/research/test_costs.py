"""Research and execution must charge the same price for the same ticket.

The research layer used to charge a flat 2 x 10bps. The kernel charges a CNY 5
minimum commission, 0.05% stamp duty on sells and a 0.001% transfer fee. On a
CNY 5,000 account the minimum commission is the dominant term, so the two
numbers disagreed by 2-6x — in the direction that makes bad strategies look
fundable.
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.research import runner
from service.research.spec import (
    EventStudy,
    build_trades,
    effective_cost_bps,
    load_panel,
    market_returns,
    parse_spec,
)

SPEC = {"pool": "limit_up", "boards": [1], "hold": 1}


def test_minimum_commission_dominates_small_tickets():
    spec = EventStudy(pool="limit_up", boards=(1,), hold=1)
    small = effective_cost_bps(spec, 1_000)
    big = effective_cost_bps(spec, 5_000)
    # 105bps of fees + 20bps slippage at CNY 1,000; 25bps + 20bps at CNY 5,000.
    assert small["total_bps"] == pytest.approx(125.2, abs=0.2)
    assert big["total_bps"] == pytest.approx(45.2, abs=0.2)
    assert small["total_bps"] > 2.5 * big["total_bps"]


def test_convertible_bonds_pay_no_stamp_duty_or_transfer_fee():
    stock = effective_cost_bps(EventStudy(pool="limit_up", boards=(1,), hold=1), 1_000)
    bond = effective_cost_bps(EventStudy(pool="cb", hold=0), 1_000)
    assert bond["fee_cny"] == pytest.approx(10.0, abs=0.01)   # 5 + 5 commission only
    assert bond["fee_bps"] < stock["fee_bps"]


def test_cost_mult_stresses_fees_as_well_as_slippage():
    spec = EventStudy(pool="limit_up", boards=(1,), hold=1)
    base = effective_cost_bps(spec, 1_000)
    stress = effective_cost_bps(spec.with_cost_mult(3.0), 1_000)
    assert stress["total_bps"] == pytest.approx(3 * base["total_bps"], rel=1e-6)
    # Tripling only the slippage would leave the fees untouched — the old bug.
    assert stress["total_bps"] > 3 * base["slippage_bps"]


def test_spec_rejects_impossible_cost_inputs():
    from service.research.spec import SpecError, parse_spec

    for bad in ({"cost_mult": 0}, {"cost_mult": -1}, {"ticket_cny": -100},
                {"cost_bps": -1}):
        with pytest.raises(SpecError):
            parse_spec({**SPEC, **bad})


def test_runner_reports_the_cost_it_charged(store):
    payload = runner.run(store, SPEC)
    cost = payload["cost"]
    assert cost["model"] == "kernel"
    assert cost["ticket_cny"] > 0
    assert payload["cost_bps_effective"] == pytest.approx(cost["total_bps"])
    assert cost["total_bps"] == pytest.approx(cost["fee_bps"] + cost["slippage_bps"], abs=0.02)
    # The 2x stress must double the *whole* cost, not just the bps term.
    assert payload["stress"]["2x"]["cost"]["total_bps"] == pytest.approx(
        2 * cost["total_bps"], rel=1e-6)


def test_cost_stress_uses_account_cash_curve_not_overlapping_event_average(store):
    payload = runner.run(store, SPEC)
    stress = payload["stress"]["2x"]
    assert stress["metrics"] == stress["account"]["metrics"]
    assert "signal_metrics" in stress


def test_auto_ticket_is_equity_over_the_names_that_fire(store, store_two_tradable):
    equity = runner._equity()
    one = runner.run(store, SPEC)
    two = runner.run(store_two_tradable, SPEC)
    assert one["cost"]["ticket_cny"] == pytest.approx(equity / 2, rel=0.01)
    assert two["cost"]["ticket_cny"] == pytest.approx(equity / 3, rel=0.01)
    assert two["cost"]["fee_bps"] > one["cost"]["fee_bps"], "more tickets, smaller each"


def test_explicit_ticket_overrides_the_auto_size(store):
    payload = runner.run(store, {**SPEC, "ticket_cny": 1_000})
    assert payload["cost"]["ticket_cny"] == pytest.approx(1_000)
    assert payload["cost_bps_effective"] > 100


def test_future_signal_count_cannot_change_an_existing_days_cost(store_two_tradable):
    limits = store_two_tradable.data["stk_limit"]
    limits.loc[limits["trade_date"] == "20240104", "up_limit"] = 100.0
    panel = load_panel(store_two_tradable, "20240101", "20240131")
    pools = store_two_tradable.read("limit_pool_t0")
    d2_one = pools[pools["ts_code"] == "600001.SH"].copy()
    d2_one["trade_date"] = "20240103"
    d2_two = pools[pools["ts_code"] == "600002.SH"].copy()
    d2_two["trade_date"] = "20240103"
    before_pools = pd.concat([pools, d2_one], ignore_index=True)
    after_pools = pd.concat([before_pools, d2_two], ignore_index=True)
    spec = parse_spec(SPEC)

    before = build_trades(panel, before_pools, spec, market_returns(panel))
    after = build_trades(panel, after_pools, spec, market_returns(panel))
    before_d1 = before[before["trade_date"] == "20240102"].set_index("ts_code")["cost"]
    after_d1 = after[after["trade_date"] == "20240102"].set_index("ts_code")["cost"]
    pd.testing.assert_series_equal(before_d1, after_d1)
    assert before.attrs["cost"]["by_date"]["20240102"]["ticket_cny"] == pytest.approx(1666.67)
    assert after.attrs["cost"]["by_date"]["20240103"]["ticket_cny"] == pytest.approx(2500)
