"""The kernel and the research layer must agree on what "opened at the limit" means.

Before P27 they did not: research skipped a next-open entry when the open sat at
the upper limit, while the kernel filled it at the limit price. Same bar, two
answers. The rule now lives in ``kernel/microstructure.py``; these tests run both
evaluations — scalar for the matcher, vectorised for the panel — over the same
values and require them to agree.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pandas as pd
import pytest

from service.kernel.clock import SHANGHAI
from service.kernel.execution import BarExecutionModel
from service.kernel.microstructure import PRICE_EPS, blocked_at_open, fill_price_at_open
from service.kernel.models import Bar, D, Instrument, Order, RejectReason, Side
from service.research.spec import (
    EventStudy,
    blocked_at_open_mask,
    build_trades,
    load_panel,
    market_returns,
)

UP = Decimal("11.00")
DOWN = Decimal("9.00")


def test_price_tolerance_is_shared():
    assert PRICE_EPS == Decimal("0.001")


@pytest.mark.parametrize("open_px", ["10.95", "10.98", "10.99", "10.999", "11.00", "11.01"])
def test_scalar_and_vector_block_the_same_rows(open_px):
    scalar = blocked_at_open(Side.BUY, D(open_px), UP, DOWN) is not None
    vector = blocked_at_open_mask(pd.Series([float(open_px)]),
                                  pd.Series([float(UP)])).iloc[0]
    assert bool(vector) is scalar


def test_a_missing_limit_never_blocks():
    assert blocked_at_open(Side.BUY, D("11.00"), None, None) is None
    mask = blocked_at_open_mask(pd.Series([11.0]), pd.Series([None]))
    assert not bool(mask.iloc[0])


def test_a_zero_limit_is_a_missing_limit_on_both_sides():
    """An empty limit column must not read as "everything is at the ceiling"."""
    assert blocked_at_open(Side.BUY, D("10.00"), D("0"), D("0")) is None
    vector = blocked_at_open_mask(pd.Series([10.0]), pd.Series([0.0]))
    assert not bool(vector.iloc[0])


def test_sell_side_blocks_at_the_lower_limit():
    assert blocked_at_open(Side.SELL, D("9.00"), UP, DOWN)
    assert blocked_at_open(Side.SELL, D("9.01"), UP, DOWN) is None


def test_slippage_is_capped_by_the_band():
    # 10.99 + 50bps would be 11.045; the band caps it at the limit.
    assert fill_price_at_open(Side.BUY, D("10.99"), 50, UP, DOWN) == D("11.00")
    assert fill_price_at_open(Side.SELL, D("9.01"), 50, UP, DOWN) == D("9.00")


def test_research_drops_the_name_that_opens_at_its_limit(store):
    panel = load_panel(store, "20240102", "20240105")
    pools = store.read("limit_pool_t0", "20240102", "20240105")
    spec = EventStudy(pool="limit_up", boards=(1,), entry="next_open", hold=1)
    trades = build_trades(panel, pools, spec, market_returns(panel))

    # 600002 opens at 11.00 == its limit and cannot fill; 600003's suspended
    # entry rolls to the first actual bar on 20240104 instead of disappearing.
    assert "600002.SH" not in set(trades["ts_code"])
    deferred = trades.set_index("ts_code").loc["600003.SH"]
    assert deferred["entry_date"] == "20240104"
    assert deferred["entry_price"] == pytest.approx(8.4)
    assert "600001.SH" in set(trades["ts_code"])


def test_kernel_refuses_the_same_bar_research_drops(store):
    bar = Bar(
        symbol="600002.SH", ts=datetime(2024, 1, 3, 9, 30, tzinfo=SHANGHAI),
        open=D("11.00"), high=D("11.00"), low=D("10.60"), close=D("10.80"),
        pre_close=D("10.00"), volume=1_000_000, amount=D("1e7"),
        up_limit=UP, down_limit=DOWN,
    )
    order = Order(order_id="O1", client_order_id="c1", symbol="600002.SH",
                  side=Side.BUY, qty=100)
    result = BarExecutionModel().match(order, bar, Instrument("600002.SH"), bar.ts)
    assert not result.filled
    assert result.reject_reason == RejectReason.OPEN_AT_LIMIT


def test_kernel_prices_off_the_open_with_the_shared_rule():
    bar = Bar(
        symbol="600001.SH", ts=datetime(2024, 1, 3, 9, 30, tzinfo=SHANGHAI),
        open=D("10.50"), high=D("11.00"), low=D("10.40"), close=D("11.00"),
        pre_close=D("10.00"), volume=1_000_000, amount=D("1e7"),
        up_limit=UP, down_limit=DOWN,
    )
    order = Order(order_id="O2", client_order_id="c2", symbol="600001.SH",
                  side=Side.BUY, qty=100)
    result = BarExecutionModel().match(order, bar, Instrument("600001.SH"), bar.ts)
    assert result.filled
    expected = fill_price_at_open(Side.BUY, bar.open, 10, bar.up_limit, bar.down_limit)
    assert result.fill.price == expected == D("10.51")
