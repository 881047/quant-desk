"""Synthetic integrity tests for the in-memory trading kernel."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI, SimClock
from service.kernel.engine import StaticDataView, TradingEngine
from service.kernel.execution import BarExecutionModel
from service.kernel.ledger import Ledger
from service.kernel.models import (
    Bar,
    D,
    Fill,
    Instrument,
    Intent,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
)
from service.kernel.risk import RiskConfig, RiskEngine


def at(day: int = 25, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=SHANGHAI)


def make_bar(
    ts: datetime | None = None,
    *,
    symbol: str = "600000.SH",
    open_: str = "10.00",
    high: str = "11.00",
    low: str = "9.00",
    close: str = "10.00",
    volume: int = 1_000_000,
    fill_ratio: float = 1.0,
) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts or at(),
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        pre_close=D("10.00"),
        volume=volume,
        amount=D(volume) * D(close),
        up_limit=D("11.00"),
        down_limit=D("9.00"),
        fill_ratio=fill_ratio,
    )


def make_engine(bar: Bar, instrument: Instrument, cash: str = "10000") -> TradingEngine:
    return TradingEngine(
        clock=SimClock(bar.ts),
        calendar=TradingCalendar.always(),
        data=StaticDataView({bar.ts: {bar.symbol: bar}}),
        instruments={instrument.symbol: instrument},
        ledger=Ledger.fresh(cash),
    )


def submit(
    ledger: Ledger,
    instrument: Instrument,
    bar: Bar,
    intent: Intent,
):
    return ledger.submit(intent, bar.ts, instrument, bar, TradingCalendar.always())


def test_partial_fills_charge_minimum_once_and_conserve_cash():
    ledger = Ledger.fresh("2005.02")
    inst = Instrument("600000.SH")
    bar = make_bar(fill_ratio=0.5)
    order = submit(
        ledger,
        inst,
        bar,
        Intent(inst.symbol, Side.BUY, qty=200, order_type=OrderType.LIMIT,
               limit_price=D("10.00")),
    )
    matcher = BarExecutionModel()

    first = matcher.match(order, bar, inst, bar.ts).fill
    assert first is not None
    assert ledger.apply_fill(order, first)
    assert first.fees == D("5.01")
    assert ledger.account.cash_available == D("0.00")
    assert ledger.account.cash_frozen == D("1000.01")

    second_bar = replace(bar, ts=bar.ts + timedelta(minutes=1), fill_ratio=1.0)
    second = matcher.match(order, second_bar, inst, second_bar.ts).fill
    assert second is not None
    assert ledger.apply_fill(order, second)
    assert second.fees == D("0.01")
    assert order.status == OrderStatus.FILLED
    assert order.fees_paid == D("5.02")
    assert ledger.account.fees_paid == D("5.02")
    assert ledger.account.cash_total() == D("0.00")
    assert ledger.account.position(inst.symbol).total_qty == 200


def test_invalid_fill_is_atomic_and_does_not_create_a_position():
    ledger = Ledger.fresh("2005.02")
    inst = Instrument("600000.SH")
    bar = make_bar()
    order = submit(
        ledger, inst, bar,
        Intent(inst.symbol, Side.BUY, qty=200, order_type=OrderType.LIMIT,
               limit_price=D("10.00")),
    )
    before = deepcopy(ledger.snapshot())
    bad = Fill("bad", order.order_id, order.symbol, order.side, 100,
               D("10.00"), D("3000.00"), bar.ts)

    with pytest.raises(ValueError, match="cash shortfall"):
        ledger.apply_fill(order, bad)

    assert ledger.snapshot() == before
    assert inst.symbol not in ledger.account.positions
    assert ledger.fills == []


def test_fill_id_is_idempotent_and_terminal_orders_cannot_revive():
    ledger = Ledger.fresh("10000")
    inst = Instrument("600000.SH")
    bar = make_bar(fill_ratio=0.5)
    order = submit(
        ledger, inst, bar,
        Intent(inst.symbol, Side.BUY, qty=200, order_type=OrderType.LIMIT,
               limit_price=D("10.00"), client_order_id="fill-key"),
    )
    fill = BarExecutionModel().match(order, bar, inst, bar.ts).fill
    assert fill is not None
    assert ledger.apply_fill(order, fill)
    before = deepcopy(ledger.snapshot())

    assert not ledger.apply_fill(order, fill)
    assert ledger.snapshot() == before
    assert len(ledger.fills) == 1
    with pytest.raises(ValueError, match="different payload"):
        ledger.apply_fill(order, replace(fill, fees=fill.fees + D("1")))
    other = replace(fill, fill_id="wrong-symbol", symbol="000001.SZ")
    with pytest.raises(ValueError, match="symbol/side"):
        ledger.apply_fill(order, other)
    with pytest.raises(ValueError, match="symbol/side"):
        ledger.apply_fill(order, replace(fill, fill_id="wrong-side", side=Side.SELL))
    with pytest.raises(ValueError, match="order_id"):
        ledger.apply_fill(order, replace(fill, fill_id="wrong-order", order_id="O999"))
    with pytest.raises(ValueError, match="finite and positive"):
        ledger.apply_fill(order, replace(fill, fill_id="negative-price", price=D("-1")))
    assert ledger.snapshot() == before

    assert ledger.cancel(order.order_id, bar.ts, TradingCalendar.always()).success
    before_cancel = deepcopy(ledger.snapshot())
    with pytest.raises(ValueError, match="not open"):
        ledger.apply_fill(order, replace(fill, fill_id="after-cancel"))
    assert ledger.snapshot() == before_cancel


def test_snapshot_restores_open_orders_client_index_and_fill_dedup():
    ledger = Ledger.fresh("10000")
    inst = Instrument("600000.SH")
    bar = make_bar(fill_ratio=0.5)
    order = submit(
        ledger, inst, bar,
        Intent(inst.symbol, Side.BUY, qty=200, order_type=OrderType.LIMIT,
               limit_price=D("10.00"), client_order_id="persist"),
    )
    fill = BarExecutionModel().match(order, bar, inst, bar.ts).fill
    assert fill is not None
    ledger.apply_fill(order, fill)
    ledger.account.peak_equity = D("12345.00")

    snapshot = ledger.snapshot()
    restored = Ledger.from_snapshot(snapshot)
    assert restored.reconcile(snapshot) == []
    assert restored.account.peak_equity == D("12345.00")
    restored_order = restored.oms.client_index["persist"]
    assert restored_order.order_id == order.order_id
    assert restored_order.filled_qty == 100
    assert len(restored.fills) == 1
    assert not restored.apply_fill(restored_order, fill)

    cash_total = restored.account.cash_total()
    frozen = restored.account.cash_frozen
    restored.settle()
    assert restored.account.cash_total() == cash_total
    assert restored.account.cash_frozen == frozen == restored_order.frozen_cash

    duplicate = submit(
        restored, inst, bar,
        Intent(inst.symbol, Side.BUY, qty=200, order_type=OrderType.LIMIT,
               limit_price=D("10.00"), client_order_id="persist"),
    )
    assert duplicate is restored_order


def test_legacy_snapshot_with_live_reservations_fails_closed():
    ledger = Ledger.fresh("5000")
    inst = Instrument("600000.SH")
    bar = make_bar()
    submit(
        ledger, inst, bar,
        Intent(inst.symbol, Side.BUY, qty=100, order_type=OrderType.LIMIT,
               limit_price=D("10.00")),
    )
    snapshot = ledger.snapshot()
    del snapshot["order_records"]
    del snapshot["fills"]

    with pytest.raises(ValueError, match="live reservations"):
        Ledger.from_snapshot(snapshot)


def test_close_auction_uses_close_and_enforces_limit():
    inst = Instrument("600000.SH")
    close_bar = make_bar(at(hour=14, minute=58), close="10.20")
    engine = make_engine(close_bar, inst)
    result = engine.step(close_bar.ts, [Intent(inst.symbol, Side.BUY, qty=100)])

    assert len(result.fills) == 1
    assert result.fills[0].price == D("10.20")
    assert result.fills[0].rule_id == "CLOSE_AUCTION"

    limited_engine = make_engine(close_bar, inst)
    limited = limited_engine.step(
        close_bar.ts,
        [Intent(inst.symbol, Side.BUY, qty=100, order_type=OrderType.LIMIT,
                limit_price=D("10.00"))],
    )
    assert limited.fills == []
    assert limited_engine.account.position(inst.symbol).total_qty == 0


def test_pre_open_lunch_and_after_hours_never_match_at_open():
    inst = Instrument("600000.SH")
    pre_open = make_bar(at(hour=9, minute=27))
    engine = make_engine(pre_open, inst)
    result = engine.step(
        pre_open.ts,
        [Intent(inst.symbol, Side.BUY, qty=100, order_type=OrderType.LIMIT,
                limit_price=D("10.00"), client_order_id="session")],
    )
    assert result.fills == []
    order = engine.ledger.oms.client_index["session"]
    assert order.is_open
    direct = BarExecutionModel().match(order, pre_open, inst, pre_open.ts)
    assert not direct.filled
    assert direct.reject_reason == RejectReason.MARKET_CLOSED

    lunch = make_bar(at(hour=11, minute=45))
    engine.data = StaticDataView({lunch.ts: {inst.symbol: lunch}})
    assert engine.step(lunch.ts).fills == []
    assert order.is_open

    after = make_bar(at(hour=15, minute=10))
    engine.data = StaticDataView({after.ts: {inst.symbol: after}})
    assert engine.step(after.ts).fills == []
    assert engine.account.position(inst.symbol).total_qty == 0
    assert order.status == OrderStatus.EXPIRED


def test_first_step_next_day_expires_and_settles_without_close_bar():
    inst = Instrument("600000.SH")
    day_one = make_bar(at(25))
    engine = make_engine(day_one, inst)
    engine.step(day_one.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    stale = submit(
        engine.ledger, inst, day_one,
        Intent(inst.symbol, Side.BUY, qty=100, order_type=OrderType.LIMIT,
               limit_price=D("9.50"), client_order_id="stale"),
    )
    assert stale.is_open
    assert engine.account.position(inst.symbol).available_qty == 0

    day_two = make_bar(at(26), open_="9.40", low="9.30")
    engine.data = StaticDataView({day_two.ts: {inst.symbol: day_two}})
    result = engine.step(day_two.ts)

    assert result.fills == []
    assert stale.status == OrderStatus.EXPIRED
    assert engine.account.cash_frozen == D("0.00")
    assert engine.account.position(inst.symbol).available_qty == 100


def test_same_order_cannot_consume_the_same_bar_twice():
    inst = Instrument("600000.SH")
    bar = make_bar(fill_ratio=0.5)
    engine = make_engine(bar, inst)
    intent = Intent(inst.symbol, Side.BUY, qty=200, order_type=OrderType.LIMIT,
                    limit_price=D("10.00"))

    first = engine.step(bar.ts, [intent])
    second = engine.step(bar.ts, [intent])

    assert len(first.fills) == 1
    assert second.fills == []
    assert len(engine.ledger.fills) == 1
    assert engine.account.position(inst.symbol).total_qty == 100


def test_t_plus_zero_inventory_can_be_sold_on_the_same_bar():
    inst = Instrument("600000.SH", t_plus=0)
    bar = make_bar()
    engine = make_engine(bar, inst)
    engine.step(bar.ts, [Intent(inst.symbol, Side.BUY, qty=100)])

    assert engine.account.position(inst.symbol).available_qty == 100
    sold = engine.step(bar.ts, [Intent(inst.symbol, Side.SELL, qty=100)])
    assert len(sold.fills) == 1
    assert engine.account.position(inst.symbol).total_qty == 0


def test_client_id_fingerprint_and_limit_validation():
    ledger = Ledger.fresh("10000")
    inst = Instrument("600000.SH")
    bar = make_bar()
    intent = Intent(inst.symbol, Side.BUY, qty=100, order_type=OrderType.LIMIT,
                    limit_price=D("10.00"), reason="full-fingerprint")
    first = submit(ledger, inst, bar, intent)
    assert submit(ledger, inst, bar, intent) is first
    later = replace(bar, ts=bar.ts + timedelta(minutes=1))
    later_order = submit(ledger, inst, later, intent)
    assert later_order.order_id != first.order_id

    before = deepcopy(ledger.snapshot())
    with pytest.raises(ValueError, match="different intent"):
        submit(ledger, inst, bar, replace(intent, qty=200,
                                          client_order_id=first.client_order_id))
    assert ledger.snapshot() == before

    for bad_price in ("NaN", "Infinity", "-1", "0", "10.001", "11.01"):
        candidate = Ledger.fresh("10000")
        rejected = submit(
            candidate, inst, bar,
            Intent(inst.symbol, Side.BUY, qty=100, order_type=OrderType.LIMIT,
                   limit_price=D(bad_price)),
        )
        assert rejected.status == OrderStatus.REJECTED
        assert rejected.reason == RejectReason.PRICE_LIMIT.value
        assert candidate.account.cash_available == D("10000")
        assert candidate.account.cash_frozen == D("0")


def test_sleeve_cash_floor_and_reduce_only_are_enforced():
    inst = Instrument("600000.SH")
    bar = make_bar()
    config = RiskConfig(
        max_core_weight=D("1"), max_core_stock_single=D("1"),
        min_cash_weight=D("0.50"), max_gap_loss=D("1"),
    )
    risk = RiskEngine(config)
    decision = risk.check(Intent(inst.symbol, Side.BUY, qty=300),
                          Ledger.fresh("5000").account, inst, bar)
    assert not decision.allowed
    assert decision.reason == RejectReason.INSUFFICIENT_CASH

    limit_risk = RiskEngine(RiskConfig(
        max_core_weight=D("1"), max_core_stock_single=D("1"),
        min_cash_weight=D("0.79"), max_gap_loss=D("1"),
    ))
    decision = limit_risk.check(
        Intent(inst.symbol, Side.BUY, qty=100, order_type=OrderType.LIMIT,
               limit_price=D("11.00")),
        Ledger.fresh("5000").account, inst, bar,
    )
    assert not decision.allowed
    assert decision.reason == RejectReason.INSUFFICIENT_CASH

    sleeve_risk = RiskEngine(RiskConfig(
        max_core_weight=D("0.20"), max_core_stock_single=D("1"),
        min_cash_weight=D("0"), max_gap_loss=D("1"),
    ))
    account = Ledger.fresh("5000").account
    account.cash_available = D("4000")
    pos = account.position(inst.symbol)
    pos.total_qty = pos.available_qty = 100
    pos.last_price = pos.avg_cost = D("10")
    decision = sleeve_risk.check(Intent(inst.symbol, Side.BUY, qty=100),
                                 account, inst, bar)
    assert not decision.allowed
    assert decision.reason == RejectReason.POSITION_CAP

    reduce_risk = RiskEngine(RiskConfig(
        daily_loss_no_new=D("-0.20"), daily_loss_reduce_only=D("-0.10"),
        max_core_weight=D("1"), max_core_stock_single=D("1"),
        min_cash_weight=D("0"), max_gap_loss=D("1"),
    ))
    decision = reduce_risk.check(Intent(inst.symbol, Side.BUY, qty=100),
                                 account, inst, bar, daily_pnl_pct=D("-0.11"))
    assert not decision.allowed
    assert decision.reason == RejectReason.DAILY_LOSS
    assert reduce_risk.check(Intent(inst.symbol, Side.SELL, qty=100),
                             account, inst, bar, daily_pnl_pct=D("-0.11")).allowed


def test_reconcile_is_bidirectional_and_checks_cash_fees_and_pnl():
    ledger = Ledger.fresh("5000")
    snapshot = ledger.snapshot()
    restored = Ledger.from_snapshot(snapshot)
    restored.account.position("unexpected.SH")
    restored.account.fees_paid = D("1.00")
    restored.account.cash_frozen = D("2.00")
    restored.account.realized_pnl = D("3.00")

    diffs = restored.reconcile(snapshot)
    assert any("unexpected position" in diff for diff in diffs)
    assert any("fees_paid" in diff for diff in diffs)
    assert any("cash_frozen" in diff for diff in diffs)
    assert any("realized_pnl" in diff for diff in diffs)


def test_sale_proceeds_become_withdrawable_only_at_settlement():
    inst = Instrument("600000.SH", t_plus=0)
    bar = make_bar()
    engine = make_engine(bar, inst)
    engine.step(bar.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    withdrawable_after_buy = engine.account.cash_withdrawable
    engine.step(bar.ts, [Intent(inst.symbol, Side.SELL, qty=100)])

    assert engine.account.cash_withdrawable == withdrawable_after_buy
    assert engine.account.cash_available > engine.account.cash_withdrawable
    engine.ledger.settle()
    assert engine.account.cash_withdrawable == engine.account.cash_available


# ---------------------------------------------------------------- snapshot schema

def test_a_snapshot_declares_its_schema_version():
    """The writer says what it wrote; the reader no longer probes for keys."""
    from service.kernel.oms import SNAPSHOT_VERSION

    ledger = Ledger.fresh("10000")
    snapshot = ledger.snapshot()

    assert snapshot["v"] == SNAPSHOT_VERSION
    assert Ledger.from_snapshot(snapshot).snapshot() == snapshot


def test_a_snapshot_from_a_newer_writer_is_refused():
    """Ignoring fields this kernel does not know is how a restart loses cash."""
    from service.kernel.oms import SNAPSHOT_VERSION

    snapshot = Ledger.fresh("10000").snapshot()
    snapshot["v"] = SNAPSHOT_VERSION + 1

    with pytest.raises(ValueError, match="newer"):
        Ledger.from_snapshot(snapshot)


def test_a_snapshot_without_a_version_is_read_as_v1():
    from service.kernel.oms import SNAPSHOT_VERSION

    ledger = Ledger.fresh("10000")
    snapshot = ledger.snapshot()
    snapshot.pop("v")

    restored = Ledger.from_snapshot(snapshot)

    assert restored.account.cash_available == ledger.account.cash_available
    assert restored.snapshot()["v"] == SNAPSHOT_VERSION


@pytest.mark.parametrize(("field", "value"), [
    ("cash_available", "-0.01"),
    ("cash_frozen", "-0.01"),
    ("cash_withdrawable", "-0.01"),
    ("cash_withdrawable", "10000.01"),
])
def test_a_snapshot_with_impossible_cash_pools_fails_closed(field, value):
    snapshot = Ledger.fresh("10000").snapshot()
    snapshot[field] = value

    with pytest.raises(ValueError, match="cash pools"):
        Ledger.from_snapshot(snapshot)


def test_a_snapshot_with_impossible_position_components_fails_closed():
    ledger = Ledger.fresh("10000")
    pos = ledger.account.position("600000.SH")
    pos.total_qty = pos.available_qty = 100
    snapshot = ledger.snapshot()
    snapshot["positions"]["600000.SH"]["available_qty"] = 101

    with pytest.raises(ValueError, match="position 600000.SH"):
        Ledger.from_snapshot(snapshot)


def test_ledger_fills_is_a_view_of_the_oms_container():
    """One container of fills, not two that must be kept in step."""
    ledger = Ledger.fresh("10000")
    bar = make_bar(at())
    instrument = Instrument("600000.SH")
    order = ledger.submit(Intent("600000.SH", Side.BUY, qty=100), bar.ts,
                          instrument, bar, TradingCalendar.always())
    fill = BarExecutionModel().match(
        order, bar, instrument, bar.ts, TradingCalendar.always()).fill
    ledger.apply_fill(order, fill)

    assert ledger.fills == list(ledger.oms.fills.values())
    assert ledger.fills == [fill]

    ledger.fills.clear()          # a copy: clearing it must not touch the OMS
    assert ledger.fills == [fill], "the view handed out the container itself"
