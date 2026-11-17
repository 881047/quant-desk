"""B1-a regressions: the sealed-ceiling penalty is an amount, not a fee.

Buying into a limit-up ceiling pays a configured 20bps queue-position penalty
(``configs/costs.yaml: limit_up_penalty_bps``). Before this batch that penalty was
folded into ``fill.fees``, so a queue cost audited as if it were a broker
commission: ``fees_paid`` / ``avg_cost`` / ``realized_pnl`` were all inflated, and
because ``order.fees_paid`` carried the inflated amount, ``incremental_buy_fees``
cancelled the next partial fill's commission against it.

The fix keeps ``fees`` equal to the three ``CostModel`` lines and carries the
penalty on ``Fill.limit_up_penalty`` (CNY, not bps). These cases are red on HEAD
``000aba3``: there ``fees`` was 7.21 and the field did not exist.

``LIMIT_UP_QUEUE`` is unreachable under the default ``next_open`` / this desk's
``intraday`` / the test conftest's ``close``. Every case below constructs a bar
with ``open == up_limit`` and ``fill_ratio < 1.0`` so the branch is exercised
instead of silently skipped.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.costs import CostModel
from service.kernel.execution import BarExecutionModel
from service.kernel.ledger import Ledger
from service.kernel.models import (
    Bar,
    D,
    Fill,
    Instrument,
    Intent,
    Order,
    OrderType,
    Side,
    money,
)
from service.kernel.oms import OrderManager

SYMBOL = "600000.SH"
TS = datetime(2026, 10, 30, 10, 0, tzinfo=SHANGHAI)
CAL = TradingCalendar.always()


def _sealed_bar(*, fill_ratio: float = 0.25, ts: datetime = TS) -> Bar:
    """An opening bar sealed at the upper limit, with a partial queue ratio."""
    return Bar(
        symbol=SYMBOL, ts=ts, open=D("11.00"), high=D("11.00"), low=D("11.00"),
        close=D("11.00"), pre_close=D("10.00"), volume=5_000_000,
        amount=D("55000000"), up_limit=D("11.00"), down_limit=D("9.00"),
        prior_volume=5_000_000, fill_ratio=fill_ratio,
    )


def _order(qty: int = 400) -> Order:
    return Order(order_id="O1", client_order_id="c1", symbol=SYMBOL, side=Side.BUY,
                 qty=qty)


def _instrument() -> Instrument:
    return Instrument(SYMBOL)


def test_limit_up_penalty_is_booked_separately():
    model = BarExecutionModel()
    inst = _instrument()
    bar = _sealed_bar()
    result = model.match(_order(qty=400), bar, inst, TS, CAL)

    assert result.filled, result.detail
    fill = result.fill
    assert fill is not None
    assert fill.qty == 100                       # 400 x 0.25
    # 1,100 notional @ 11.00. fees == the three CostModel lines, nothing else.
    assert fill.fees == D("5.01")
    assert fill.fees == CostModel().buy_fees(D("1100"), inst)
    # The 20bps queue cost is a CNY amount on its own field, not inside fees.
    assert fill.limit_up_penalty == D("2.20")
    assert fill.limit_up_penalty == money(
        D("1100") * D(str(model.limit_up_penalty_bps)) / D("10000"))
    assert fill.rule_id == "LIMIT_UP_QUEUE+LIMIT_UP_PENALTY"


def test_fees_paid_equals_cost_model_sum():
    inst = _instrument()
    ledger = Ledger.fresh("20000")
    bar = _sealed_bar()
    order = ledger.submit(
        Intent(SYMBOL, Side.BUY, qty=400, order_type=OrderType.LIMIT,
               limit_price=D("11.00")),
        TS, inst, bar, CAL,
    )
    fill = BarExecutionModel().match(order, bar, inst, TS, CAL).fill
    assert fill is not None and fill.qty == 100
    assert ledger.apply_fill(order, fill)

    # account.fees_paid tracks fill.fees only -> the three CostModel lines.
    assert ledger.account.fees_paid == D("5.01")
    assert ledger.account.fees_paid == CostModel().buy_fees(D("1100"), inst)
    # (1100 + 5.01) / 100 = 11.0501 — not the old (1100 + 7.21) / 100 = 11.0721.
    assert ledger.account.position(SYMBOL).avg_cost == D("11.0501")


def test_incremental_fees_not_offset_by_penalty():
    """The second partial fill must still pay its 0.01, not be cancelled by 7.21.

    ``_settle_fill`` hands ``order.fees_paid`` to ``incremental_buy_fees`` as the
    previous cumulative fee. With the penalty folded in, the first fill's
    ``fees_paid`` was 7.21, so the second fill's ``5.02 - 7.21`` clamped to 0.00.
    """
    inst = _instrument()
    ledger = Ledger.fresh("20000")
    bar1 = _sealed_bar(fill_ratio=0.25)
    order = ledger.submit(
        Intent(SYMBOL, Side.BUY, qty=400, order_type=OrderType.LIMIT,
               limit_price=D("11.00")),
        TS, inst, bar1, CAL,
    )
    matcher = BarExecutionModel()

    first = matcher.match(order, bar1, inst, TS, CAL).fill
    assert first is not None and first.qty == 100
    assert first.fees == D("5.01")
    assert first.limit_up_penalty == D("2.20")
    assert ledger.apply_fill(order, first)

    bar2 = _sealed_bar(fill_ratio=0.5, ts=TS + timedelta(minutes=1))
    second = matcher.match(order, bar2, inst, bar2.ts, CAL).fill
    assert second is not None and second.qty == 100
    # 2,200 cumulative notional: buy_fees(2200) = 5.02, minus 5.01 already paid.
    assert second.fees == D("0.01")
    assert second.limit_up_penalty == D("2.20")
    assert ledger.apply_fill(order, second)

    assert order.fees_paid == D("5.02")
    assert ledger.account.fees_paid == D("5.02")


def test_fill_snapshot_roundtrip_carries_penalty():
    fill = Fill(
        fill_id="f1", order_id="O1", symbol=SYMBOL, side=Side.BUY, qty=100,
        price=D("11.00"), fees=D("5.01"), ts=TS,
        rule_id="LIMIT_UP_QUEUE+LIMIT_UP_PENALTY", ref_price=D("11.00"),
        limit_up_penalty=D("2.20"),
    )

    row = OrderManager.fill_to_dict(fill)
    assert row["limit_up_penalty"] == "2.20"
    assert OrderManager.fill_from_dict(row) == fill

    # A snapshot written before this field existed has no such key: it must read
    # back as zero, never raise. (Old constructors and old JSON both rely on it.)
    legacy = {key: value for key, value in row.items() if key != "limit_up_penalty"}
    restored = OrderManager.fill_from_dict(legacy)
    assert restored.limit_up_penalty == D("0")
    assert restored.fees == D("5.01")
