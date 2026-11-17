"""P60 kernel audit: the rules that were declared and the rules that ran.

Each test below pins a place where the kernel's *documented* intent and its
executed behaviour had come apart — a configured parameter nobody read, a
getter that wrote, a private convention applied to the wrong asset class, or a
missing limit read as "the ceiling is everywhere".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from service import settings
from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.execution import BarExecutionModel
from service.kernel.ledger import Ledger
from service.kernel.microstructure import fill_price_at_open
from service.kernel.models import (
    Account,
    Bar,
    Board,
    D,
    Fill,
    Instrument,
    Intent,
    OrderStatus,
    OrderType,
    Position,
    RejectReason,
    Side,
    money,
)
from service.kernel.oms import OrderManager
from service.kernel.risk import RiskConfig, RiskEngine

TS = datetime(2026, 10, 30, 10, 0, tzinfo=SHANGHAI)
CLOSE_TS = datetime(2026, 10, 30, 14, 58, tzinfo=SHANGHAI)
CAL = TradingCalendar.always()


def stock(symbol: str = "600000.SH") -> Instrument:
    return Instrument(symbol)


def bond(symbol: str = "113050.SH") -> Instrument:
    return Instrument(
        symbol=symbol,
        board=Board.CB,
        min_qty=10,
        qty_step=10,
        price_tick=D("0.001"),
        t_plus=0,
        limit_pct=D("0.20"),
        stamp_duty=D("0"),
        transfer_fee=D("0"),
    )


def make_bar(
    *,
    open_: str = "10.00",
    high: str = "10.50",
    low: str = "9.80",
    close: str = "10.20",
    pre_close: str = "10.00",
    up: str = "11.00",
    down: str = "9.00",
    volume: int = 5_000_000,
    prior_volume: int = 5_000_000,
    ts: datetime = TS,
    symbol: str = "600000.SH",
    fill_ratio: float = 1.0,
) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts,
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        pre_close=D(pre_close),
        volume=volume,
        amount=D(volume) * D(close),
        up_limit=D(up),
        down_limit=D(down),
        prior_volume=prior_volume,
        fill_ratio=fill_ratio,
    )


def order(qty: int = 100, side: Side = Side.BUY, symbol: str = "600000.SH"):
    from service.kernel.models import Order

    return Order(order_id="O1", client_order_id="c1", symbol=symbol, side=side, qty=qty)


# ── 1. 配置里的数必须真的被读 ────────────────────────────────────────────────
def test_opening_capital_comes_from_the_config_not_a_literal():
    """``risk.initial_cash`` sat in params.yaml while four places wrote 5000."""
    assert Ledger.fresh().account.cash_available == settings.initial_cash()
    assert Account().cash_available == settings.initial_cash()
    assert Account().peak_equity == settings.initial_cash()


def test_the_market_buy_buffer_is_the_configured_one():
    """Frozen cash = notional x (1 + risk.market_buy_buffer) + fees.

    1,000 notional, 2% buffer, 5.00 commission + 0.01 transfer.
    """
    oms = OrderManager()
    submitted = oms.submit(
        Intent("600000.SH", Side.BUY, qty=100), TS, Account(), stock(), make_bar(), CAL
    )

    assert submitted.status == OrderStatus.NEW
    assert submitted.frozen_cash == D("1025.01")


def test_a_convertible_pays_its_own_slippage_and_keeps_its_tick():
    """CB quotes in 0.001 and is neither a stock nor an ETF for cost purposes.

    A two-way ``is_etf`` branch gave the bond the stock's 10bps in production and
    the ETF's 5bps in research; the fill itself was rounded to one fen, a price
    the exchange cannot print.
    """
    model = BarExecutionModel()

    assert model.slippage_bps(bond()) == settings.num("costs.slippage.cb_bps")
    assert model.slippage_bps(stock()) == settings.num("costs.slippage.stock_bps")

    bar = make_bar(open_="100.000", high="101.000", low="99.000", close="100.000",
                   pre_close="100.000", up="120.000", down="80.000",
                   symbol="113050.SH")
    result = model.match(order(qty=10, symbol="113050.SH"), bar, bond(), TS, CAL)

    assert result.filled, result.detail
    # 100.000 x (1 + 20bps) = 100.200 exactly; three decimals survive.
    assert result.fill.price == D("100.200")


def test_the_fill_price_uses_the_instruments_tick():
    assert fill_price_at_open(Side.BUY, D("100.000"), 20, D("120"), D("80"),
                              D("0.001")) == D("100.200")
    assert fill_price_at_open(Side.BUY, D("100.000"), 20, D("120"), D("80"),
                              D("0.01")) == D("100.20")


# ── 2. 没有涨跌停价的 bar 不再等于"全部封涨停" ───────────────────────────────
def test_a_bar_without_limits_derives_the_band_instead_of_refusing_everything():
    """``up_limit=0`` used to make ``blocked_at_open`` see a sealed ceiling."""
    bar = make_bar(up="0", down="0")
    assert not bar.is_limit_up, "a missing ceiling is not a ceiling"

    result = BarExecutionModel().match(order(), bar, stock(), TS, CAL)

    assert result.filled, result.detail
    assert result.fill.price == D("10.01")


def test_a_bar_with_no_limits_and_no_prior_close_still_prices_a_fill():
    """Nothing to derive from: the model must not turn "unknown" into "sealed"."""
    bar = make_bar(up="0", down="0", pre_close="0")
    result = BarExecutionModel().match(order(), bar, stock(), TS, CAL)

    assert result.filled, result.detail
    assert result.fill.price == D("10.01")


def test_a_limit_order_is_not_refused_because_the_bar_has_no_limits():
    """The OMS and the matcher must agree that an empty band is "unknown"."""
    oms = OrderManager()
    submitted = oms.submit(
        Intent("600000.SH", Side.BUY, qty=100, order_type=OrderType.LIMIT,
               limit_price=D("10.50")),
        TS, Account(), stock(), make_bar(up="0", down="0"), CAL,
    )

    assert submitted.status == OrderStatus.NEW, submitted.reason


def test_the_derived_band_uses_the_instruments_own_percentage():
    growth = Instrument("300001.SZ", board=Board.CHINEXT, limit_pct=D("0.20"))
    assert growth.band(D("10.00")) == (D("12.00"), D("8.00"))
    assert stock().band(D("10.00")) == (D("11.00"), D("9.00"))
    assert stock().band(D("0")) == (D("0"), D("0")), "no reference price, no band"


def test_a_convertible_cannot_be_built_with_a_stamp_duty():
    with pytest.raises(ValueError, match="convertible"):
        Instrument("113050.SH", board=Board.CB)


# ── 3. 收盘竞价与开盘同一把尺子 ─────────────────────────────────────────────
def test_the_closing_auction_refuses_a_sealed_ceiling():
    bar = make_bar(open_="10.80", high="11.00", low="10.70", close="11.00",
                   ts=CLOSE_TS)
    result = BarExecutionModel().match(order(), bar, stock(), CLOSE_TS, CAL)

    assert not result.filled
    assert result.reject_reason == RejectReason.QUEUE_NO_PRIORITY


def test_the_closing_auction_refuses_a_sealed_floor():
    bar = make_bar(open_="9.40", high="9.60", low="9.00", close="9.00", ts=CLOSE_TS)
    result = BarExecutionModel().match(
        order(side=Side.SELL), bar, stock(), CLOSE_TS, CAL
    )

    assert not result.filled
    assert result.reject_reason == RejectReason.LIMIT_DOWN


def test_the_closing_auction_still_fills_an_unsealed_board():
    bar = make_bar(close="10.42", ts=CLOSE_TS)
    result = BarExecutionModel().match(order(), bar, stock(), CLOSE_TS, CAL)

    assert result.filled, result.detail
    assert result.fill.price == D("10.42"), "the auction clears at the close"


# ── 4. 拒绝原因要能区分"没有"和"今天不能卖" ─────────────────────────────────
def test_a_sell_without_a_position_is_not_a_t_plus_one_problem():
    engine = RiskEngine()
    decision = engine.check(
        Intent("600000.SH", Side.SELL, qty=100), Account(), stock(), make_bar()
    )

    assert not decision.allowed
    assert decision.reason == RejectReason.INSUFFICIENT_SHARES


def test_a_sell_of_todays_shares_is_still_t_plus_one():
    account = Account()
    account.positions["600000.SH"] = Position(
        symbol="600000.SH", total_qty=100, available_qty=0, avg_cost=D("10")
    )
    decision = RiskEngine().check(
        Intent("600000.SH", Side.SELL, qty=100), account, stock(), make_bar()
    )

    assert decision.reason == RejectReason.T_PLUS_ONE


# ── 5. 读不写、写不藏 ────────────────────────────────────────────────────────
def test_equity_is_a_pure_read_and_marking_is_explicit():
    account = Account(cash_available=D("1000"), peak_equity=D("2000"))
    account.positions["600000.SH"] = Position(
        symbol="600000.SH", total_qty=100, last_price=D("10")
    )

    assert account.equity({"600000.SH": D("12")}) == D("2200")
    assert account.positions["600000.SH"].last_price == D("10"), "a read wrote"

    assert account.update_peak({"600000.SH": D("12")}) == D("2200")
    assert account.positions["600000.SH"].last_price == D("12")
    assert account.peak_equity == D("2200")


def test_the_sleeve_cap_marks_positions_the_same_way_equity_does():
    """Stale ``last_price`` vs supplied ``prices`` used to give two answers."""
    account = Account(cash_available=D("5000"))
    account.positions["600000.SH"] = Position(
        symbol="600000.SH", total_qty=1000, available_qty=1000,
        last_price=D("1"), avg_cost=D("1"), sleeve="CORE",
    )
    prices = {"600000.SH": D("10"), "600519.SH": D("10")}
    decision = RiskEngine(RiskConfig(max_core_weight=D("0.70"))).check(
        Intent("600519.SH", Side.BUY, qty=100, sleeve="CORE"),
        account, stock("600519.SH"), make_bar(open_="10.00"), prices,
    )

    assert not decision.allowed
    assert decision.reason == RejectReason.POSITION_CAP
    assert "sleeve" in decision.detail, "the sleeve was valued at the stale mark"


# ── 6. 订单索引与快照一致 ────────────────────────────────────────────────────
def test_the_live_order_index_survives_a_snapshot_round_trip():
    ledger = Ledger.fresh("5000", )
    submitted = ledger.submit(
        Intent("600000.SH", Side.BUY, qty=100, order_type=OrderType.LIMIT,
               limit_price=D("10.00")),
        TS, stock(), make_bar(), CAL,
    )
    assert [o.order_id for o in ledger.oms.open_orders()] == [submitted.order_id]

    restored = Ledger.from_snapshot(ledger.snapshot())
    assert [o.order_id for o in restored.oms.open_orders()] == [submitted.order_id]
    assert set(restored.oms.orders) == set(ledger.oms.orders)
    assert restored.account.cash_frozen == ledger.account.cash_frozen


def test_the_live_index_tracks_every_terminal_transition():
    oms = OrderManager()
    first = oms.submit(Intent("600000.SH", Side.BUY, qty=100), TS, Account(),
                       stock(), make_bar(), CAL)
    second = oms.submit(Intent("600000.SH", Side.BUY, qty=100, reason="second"),
                        TS, Account(), stock(), make_bar(), CAL)
    assert {o.order_id for o in oms.open_orders()} == {first.order_id, second.order_id}

    account = Account()
    oms.cancel(first.order_id, TS, account, CAL)
    assert [o.order_id for o in oms.open_orders()] == [second.order_id]

    oms.expire(second.order_id, TS, account)
    assert oms.open_orders() == []
    assert len(oms.orders) == 2, "history is kept, only the live index shrinks"


# ── 7. 状态机与时间戳 ────────────────────────────────────────────────────────
def test_a_fill_cannot_precede_the_order_that_produced_it():
    oms = OrderManager()
    submitted = oms.submit(Intent("600000.SH", Side.BUY, qty=100), TS, Account(),
                           stock(), make_bar(), CAL)
    early = Fill(
        fill_id="f1", order_id=submitted.order_id, symbol="600000.SH",
        side=Side.BUY, qty=100, price=D("10.00"), fees=D("5.01"),
        ts=TS - timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="precedes"):
        oms.apply_fill(submitted, early, Account())


def test_a_fractional_quantity_is_rejected_and_an_integral_float_is_not():
    oms = OrderManager()
    bad = oms.submit(Intent("600000.SH", Side.BUY, qty=100.5), TS, Account(),
                     stock(), make_bar(), CAL)
    assert bad.status == OrderStatus.REJECTED
    assert bad.reason == RejectReason.BAD_QTY.value

    good = oms.submit(Intent("600000.SH", Side.BUY, qty=Decimal(100)), TS,
                      Account(), stock(), make_bar(), CAL)
    assert good.status == OrderStatus.NEW
    assert good.qty == 100


def test_a_t_plus_two_instrument_is_refused_rather_than_settled_as_t_plus_one():
    oms = OrderManager()
    with pytest.raises(ValueError, match="t_plus"):
        oms.submit(Intent("600000.SH", Side.BUY, qty=100), TS, Account(),
                   Instrument("600000.SH", t_plus=2), make_bar(), CAL)


# ── 8. 涨停排队惩罚不再是死参数 ──────────────────────────────────────────────
def test_buying_into_a_sealed_ceiling_charges_the_configured_penalty():
    model = BarExecutionModel()
    bar = make_bar(open_="11.00", high="11.00", low="11.00", close="11.00",
                   fill_ratio=0.25)
    result = model.match(order(qty=400), bar, stock(), TS, CAL)

    assert result.filled, result.detail
    assert result.fill.qty == 100
    # 1,100 notional (100 shares @ 11.00): 5.00 commission + 0.01 transfer =
    # 5.01, and buy-side stamp duty is zero.
    #
    # CHANGED (dynamic `5.01 + penalty` -> `fees == 5.01`, penalty asserted on
    # its own field) by the B1-a fix.
    #   Before: this test proved `limit_up_penalty_bps` was a live parameter by
    #   expecting `fees` to GROW by the penalty (5.01 + 2.20 = 7.21), because
    #   `_settle_fill` folded the penalty into `fees`. That made a queue cost
    #   look like commission and inflated `fees_paid` / `avg_cost`.
    #   After: the penalty is an amount on `Fill.limit_up_penalty`, so `fees`
    #   must stay exactly the three CostModel lines and the configured penalty
    #   must be asserted on that field. It is still DERIVED from
    #   `model.limit_up_penalty_bps`, so a config that stops being read still
    #   fails here — dropping the penalty assertion would silently turn this
    #   into a test of nothing, so it is kept deliberately.
    #   The 2.20 is no longer deducted from cash at all — it is a cost
    #   DISCLOSURE, not a payment: it is written to `state["fills"]` /
    #   `paper_fills.payload` by `book._record_fill` and reported by
    #   `tca.summary` under `impact_bps` (2.20 / 1100 = 20.0 bps).
    #   惩罚不再从现金扣款（`fill.fees` 只含三项），改为**成本披露**：金额落在
    #   `fill.limit_up_penalty`，由 `tca.impact_bps` 归集。跨版本比较 `equity` 时
    #   需扣回这 20bps，否则改后净值会显得高 2.20 元。
    penalty = D(str(model.limit_up_penalty_bps)) / D("10000") * D("100") * D("11.00")
    assert result.fill.fees == D("5.01")
    assert result.fill.limit_up_penalty == money(penalty)
    assert result.fill.limit_up_penalty == D("2.20")
    assert result.fill.rule_id.endswith("+LIMIT_UP_PENALTY")
