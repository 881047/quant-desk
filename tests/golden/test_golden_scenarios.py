"""12 golden scenarios for the A-share paper-trading kernel.

These are the contract for "backtest and paper use the same code path".
Run: .venv/Scripts/python.exe -m pytest tests/golden -q
"""

from __future__ import annotations

from datetime import UTC, datetime

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI, SimClock
from service.kernel.corp_action import CorporateAction
from service.kernel.engine import StaticDataView, TradingEngine
from service.kernel.execution import BarExecutionModel
from service.kernel.ledger import Ledger
from service.kernel.models import (
    Bar,
    Board,
    D,
    Instrument,
    Intent,
    OrderStatus,
    OrderType,
    RejectReason,
    Session,
    Side,
    money,
)
from service.kernel.risk import RiskConfig, RiskEngine


def day(h: int = 10, m: int = 0, d: int = 25) -> datetime:
    return datetime(2026, 10, d, h, m, tzinfo=SHANGHAI)


def stock(symbol: str = "600000.SH", is_etf: bool = False, is_st: bool = False) -> Instrument:
    return Instrument(
        symbol=symbol,
        name="Test",
        board=Board.ETF if is_etf else Board.MAIN,
        is_etf=is_etf,
        is_st=is_st,
    )


def bar(
    symbol: str = "600000.SH",
    ts: datetime | None = None,
    open_: str = "10.00",
    high: str = "10.50",
    low: str = "9.80",
    close: str = "10.20",
    pre: str = "10.00",
    up: str = "11.00",
    down: str = "9.00",
    volume: int = 1_000_000,
    suspended: bool = False,
    fill_ratio: float = 1.0,
    seal_amount: str | None = None,
) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts or day(),
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        pre_close=D(pre),
        volume=volume,
        amount=D(volume) * D(close),
        up_limit=D(up),
        down_limit=D(down),
        suspended=suspended,
        fill_ratio=fill_ratio,
        seal_amount=None if seal_amount is None else D(seal_amount),
    )


def make_engine(bars: dict[str, Bar], instrument: Instrument, cash: str = "5000"):
    ledger = Ledger.fresh(cash)
    ts0 = next(iter(bars.values())).ts
    engine = TradingEngine(
        clock=SimClock(ts0),
        calendar=TradingCalendar.always(),
        data=StaticDataView({ts0: bars}),
        instruments={instrument.symbol: instrument},
        ledger=ledger,
    )
    return engine, ledger


# 1. 一字板买入不可成交
def test_one_word_board_buy_no_fill():
    inst = stock()
    b = bar(open_="11.00", high="11.00", low="11.00", close="11.00", up="11.00")
    engine, ledger = make_engine({inst.symbol: b}, inst)
    res = engine.step(b.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    assert not res.fills
    assert engine.account.position(inst.symbol).total_qty == 0
    order = next(iter(ledger.oms.orders.values()))
    # 市价单只对它自己的那根 K 线有效：没成交就作废，不留到明天按明天的价格成交
    assert order.status == OrderStatus.EXPIRED
    assert order.reason == "IOC_NO_FILL"
    assert engine.account.cash_available == D("5000.00")


# 2. 收盘涨停不影响开盘那笔成交的定价（P27 修正）
def test_limit_up_close_is_priced_off_the_open():
    inst = stock()
    # 开 10.20 → 全天拉到涨停 11.00 收盘。成交发生在 09:30，价格只能来自开盘。
    b = bar(open_="10.20", high="11.00", low="10.10", close="11.00", up="11.00")
    engine, _ = make_engine({inst.symbol: b}, inst)
    res = engine.step(b.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    assert len(res.fills) == 1
    fill = res.fills[0]
    # 10.20 * (1 + 10bps) = 10.2102 -> 10.21，而不是 11.00
    assert fill.price == D("10.21")
    assert "BROKEN_BOARD" not in fill.rule_id

    # 同样的开盘，收盘没涨停，成交价必须完全一致：收盘价不参与定价。
    b2 = bar(open_="10.20", high="10.50", low="10.00", close="10.30", up="11.00")
    engine2, _ = make_engine({inst.symbol: b2}, inst)
    res2 = engine2.step(b2.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    assert res2.fills[0].price == fill.price


# 2b. 开盘即涨停：买不到，且给出明确原因
def test_open_at_limit_up_refused():
    inst = stock()
    b = bar(open_="11.00", high="11.00", low="10.60", close="10.80", up="11.00")
    engine, ledger = make_engine({inst.symbol: b}, inst)
    res = engine.step(b.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    assert not res.fills
    order = next(iter(ledger.oms.orders.values()))
    assert order.status == OrderStatus.EXPIRED
    assert order.reason == "IOC_NO_FILL"
    assert engine.account.cash_available == D("5000.00")


# 2c. 有排队比例时，开盘涨停按比例成交（封单数据可用才放开）
def test_open_at_limit_up_fills_only_its_queue_share():
    inst = stock()
    b = bar(open_="11.00", high="11.00", low="11.00", close="11.00", up="11.00",
            fill_ratio=0.25)
    engine, _ = make_engine({inst.symbol: b}, inst, cash="20000")
    res = engine.step(b.ts, [Intent(inst.symbol, Side.BUY, qty=400)])
    assert len(res.fills) == 1
    assert res.fills[0].qty == 100
    assert res.fills[0].price == D("11.00")
    # Queue share plus the configured penalty for buying into a sealed ceiling.
    assert res.fills[0].rule_id == "LIMIT_UP_QUEUE+LIMIT_UP_PENALTY"
    # 1,100 notional (100 shares @ 11.00). `fees` is now exactly the three
    # CostModel lines: 5.00 commission + 0.01 transfer + 0.00 buy-side stamp
    # duty = 5.01.
    #
    # CHANGED 7.21 -> 5.01 by the B1-a fix.
    #   Before: the 20bps sealed-ceiling penalty (1100 x 20/10000 = 2.20) was
    #   added into `fill.fees` in `BarExecutionModel._settle_fill`
    #   (`fees = money(fees + penalty)`). `fees` was therefore 7.21 and the
    #   queue cost audited as if it were a broker commission: it inflated
    #   `avg_cost` (1107.21/100 = 11.0721 instead of 11.0501), `account.fees_paid`
    #   and `realized_pnl`, and because `order.fees_paid` carried the inflated
    #   number, `incremental_buy_fees` cancelled the next partial fill's
    #   commission against it (5.02 - 7.21 -> clamped to 0.00).
    #   After: the penalty is an *amount* on its own field,
    #   `Fill.limit_up_penalty` (2.20), not a rate. `book._record_fill` writes it
    #   into `state["fills"]` / `paper_fills.payload`, and `tca.summary`
    #   reclassifies it into `impact_bps` (2.20 / 1100 = 20.0 bps) instead of
    #   `fee_bps`. The 2.20 is no longer deducted from cash at all — it is a cost
    #   DISCLOSURE, not a payment:
    #   惩罚不再从现金扣款（`fill.fees` 只含三项），改为**成本披露**：金额落在
    #   `fill.limit_up_penalty`，由 `tca.impact_bps` 归集。跨版本比较 `equity` 时
    #   需扣回这 20bps，否则改后净值会显得高 2.20 元。
    #   `limit_up_penalty_bps` is still read and `rule_id` still names it.
    assert res.fills[0].fees == D("5.01")
    assert res.fills[0].limit_up_penalty == D("2.20")
    # Derived from the model's own configured rate: a parameter nobody reads
    # fails here rather than passing with a hardcoded number.
    model = engine.exec_model
    assert res.fills[0].limit_up_penalty == money(
        D("1100") * D(str(model.limit_up_penalty_bps)) / D("10000"))


# 3. 跌停一字板卖不出
def test_limit_down_one_word_sell_no_fill():
    inst = stock()
    b1 = bar(ts=day(d=25), open_="10.00", high="10.50", low="9.80", close="10.20")
    engine, ledger = make_engine({inst.symbol: b1}, inst)
    engine.step(b1.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    ledger.settle()
    assert engine.account.position(inst.symbol).total_qty == 100

    b2 = bar(ts=day(d=26), open_="9.00", high="9.00", low="9.00", close="9.00", pre="10.20")
    engine.data = StaticDataView({b2.ts: {inst.symbol: b2}})
    res = engine.step(b2.ts, [Intent(inst.symbol, Side.SELL, qty=100)])
    assert not res.fills
    assert engine.account.position(inst.symbol).total_qty == 100


# 4. T 日买入不可卖，T+1 可卖
def test_t_plus_one():
    inst = stock()
    b1 = bar(ts=day(d=25), open_="10.00", high="10.50", low="9.80", close="10.20")
    engine, ledger = make_engine({inst.symbol: b1}, inst)
    engine.step(b1.ts, [Intent(inst.symbol, Side.BUY, qty=100)])

    sell = Intent(inst.symbol, Side.SELL, qty=100)
    decision = engine.risk.check(sell, engine.account, inst, b1, {inst.symbol: b1.close})
    assert not decision.allowed
    assert decision.reason == RejectReason.T_PLUS_ONE

    ledger.settle()
    b2 = bar(ts=day(d=26), open_="10.30", high="10.80", low="10.10", close="10.50", pre="10.20")
    engine.data = StaticDataView({b2.ts: {inst.symbol: b2}})
    res = engine.step(b2.ts, [Intent(inst.symbol, Side.SELL, qty=100)])
    assert len(res.fills) == 1
    assert engine.account.position(inst.symbol).total_qty == 0


# 5. 零股必须一次卖完
def test_odd_lot_must_close_entire_position():
    inst = stock()
    b = bar()
    engine, ledger = make_engine({inst.symbol: b}, inst)
    pos = engine.account.position(inst.symbol)
    pos.total_qty = 150
    pos.available_qty = 150
    pos.avg_cost = D("10.00")

    # 120 is neither a lot multiple nor the entire position -> reject.
    res = engine.step(b.ts, [Intent(inst.symbol, Side.SELL, qty=120)])
    assert not res.fills
    assert res.alerts and res.alerts[0].reason == RejectReason.BAD_LOT

    # 150 closes the entire position -> allowed.
    res2 = engine.step(b.ts, [Intent(inst.symbol, Side.SELL, qty=150)])
    assert len(res2.fills) == 1
    assert engine.account.position(inst.symbol).total_qty == 0


# 6. 除权送股 + 派息
def test_corporate_action_cash_and_stock_dividend():
    ledger = Ledger.fresh("1000")
    pos = ledger.account.position("600000.SH")
    pos.total_qty = 100
    pos.available_qty = 100
    pos.avg_cost = D("10.00")

    action = CorporateAction(
        symbol="600000.SH",
        ex_date=day(d=26).date(),
        cash_div_per_share=D("0.50"),
        stock_div_per_share=D("0.30"),
    )
    out = action.apply(ledger.account)
    assert out["applied"]
    assert pos.total_qty == 130
    assert pos.available_qty == 130
    assert ledger.account.cash_available == D("1050.00")
    assert ledger.account.dividend_income == D("50.00")
    assert pos.avg_cost == D("7.3077")


# 7. 停牌不能买；复牌跳空成交
def test_suspension_then_gap_resume():
    inst = stock()
    b1 = bar(ts=day(d=25), suspended=True)
    engine, ledger = make_engine({inst.symbol: b1}, inst)
    res = engine.step(b1.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    assert not res.fills
    assert res.alerts and res.alerts[0].reason == RejectReason.SUSPENDED
    assert engine.account.position(inst.symbol).total_qty == 0

    b2 = bar(ts=day(d=26), open_="9.00", high="9.50", low="8.80", close="9.20", pre="10.20")
    engine.data = StaticDataView({b2.ts: {inst.symbol: b2}})
    res2 = engine.step(b2.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    assert len(res2.fills) == 1
    assert res2.fills[0].price == D("9.01")


# 8. 09:21 撤单被拒；09:19 可撤
def test_cancel_window():
    inst = stock()
    ts_submit = day(9, 15)
    b = bar(ts=ts_submit, open_="10.00", high="10.20", low="9.90", close="10.00")
    ledger = Ledger.fresh("5000")
    calendar = TradingCalendar.always()
    o1 = ledger.submit(Intent(inst.symbol, Side.BUY, qty=100, client_order_id="A"), ts_submit, inst, b, calendar)
    o2 = ledger.submit(Intent(inst.symbol, Side.BUY, qty=100, client_order_id="B"), ts_submit, inst, b, calendar)

    ok = ledger.cancel(o1.order_id, day(9, 19), calendar)
    assert ok.success

    bad = ledger.cancel(o2.order_id, day(9, 21), calendar)
    assert not bad.success
    assert bad.reason == RejectReason.CANCEL_NOT_ALLOWED


# 9. 整手约束导致目标仓位不可达
def test_lot_constraint_makes_target_unreachable():
    inst = stock()
    b = bar(open_="60.00", high="61.00", low="59.00", close="60.00", up="66.00", down="54.00")
    engine, _ = make_engine({inst.symbol: b}, inst)
    res = engine.step(
        b.ts,
        [Intent(inst.symbol, Side.BUY, target_weight=D("0.50"))],
    )
    assert not res.fills
    assert res.alerts and res.alerts[0].reason == RejectReason.BAD_LOT
    assert engine.account.position(inst.symbol).total_qty == 0


# 10. 部分成交 + 剩余撤单（限价单当日有效，所以剩余部分可以挂到收盘）
def test_partial_fill_then_cancel_remainder():
    inst = stock()
    b = bar(volume=1_000_000, fill_ratio=0.5)
    engine, ledger = make_engine({inst.symbol: b}, inst, cash="10000")
    res = engine.step(b.ts, [Intent(inst.symbol, Side.BUY, qty=200,
                                   order_type=OrderType.LIMIT, limit_price=D("10.10"))])
    assert len(res.fills) == 1
    order = next(iter(ledger.oms.orders.values()))
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.filled_qty == 100

    cancel = ledger.cancel(order.order_id, day(10, 5), TradingCalendar.always())
    assert cancel.success
    assert order.status == OrderStatus.CANCELLED
    assert engine.account.position(inst.symbol).total_qty == 100


# 10b. 市价单当日只撮合一次；限价单挂到收盘才作废
def test_market_is_ioc_limit_is_day_order():
    inst = stock()
    b = bar(open_="11.00", high="11.00", low="10.60", close="10.80", up="11.00")
    engine, ledger = make_engine({inst.symbol: b}, inst)

    engine.step(b.ts, [Intent(inst.symbol, Side.BUY, qty=100, client_order_id="M")])
    market = ledger.oms.client_index["M"]
    assert market.status == OrderStatus.EXPIRED
    assert market.reason == "IOC_NO_FILL"

    # 无成交量的 K 线：限价单进不去，但它是当日有效的，10:00 不作废
    dead = bar(ts=day(10, 0), volume=0)
    engine.data = StaticDataView({dead.ts: {inst.symbol: dead}})
    engine.step(dead.ts, [Intent(inst.symbol, Side.BUY, qty=100,
                                 order_type=OrderType.LIMIT, limit_price=D("10.90"),
                                 client_order_id="L")])
    limit = ledger.oms.client_index["L"]
    assert limit.is_open, "限价单当日有效，10:00 不该作废"

    close_bar = bar(ts=day(15, 0), volume=0)
    engine.data = StaticDataView({close_bar.ts: {inst.symbol: close_bar}})
    engine.step(close_bar.ts)
    assert limit.status == OrderStatus.EXPIRED
    assert limit.reason == "EXPIRED_AT_CLOSE"
    assert engine.account.cash_frozen == D("0.00")


# 10c. 收盘跌停不影响开盘卖出的价格
def test_limit_down_close_is_priced_off_the_open():
    inst = stock()
    b1 = bar(ts=day(d=25), open_="10.00", high="10.50", low="9.80", close="10.20")
    engine, ledger = make_engine({inst.symbol: b1}, inst)
    engine.step(b1.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    ledger.settle()

    # 开 10.10，一路砸到跌停 9.00 收盘：09:30 的卖单拿到 10.09，不是 9.00
    b2 = bar(ts=day(d=26), open_="10.10", high="10.20", low="9.00", close="9.00",
             pre="10.20", up="11.22", down="9.00")
    engine.data = StaticDataView({b2.ts: {inst.symbol: b2}})
    res = engine.step(b2.ts, [Intent(inst.symbol, Side.SELL, qty=100)])
    assert len(res.fills) == 1
    assert res.fills[0].price == D("10.09")


# 10d. 开盘即跌停：没有买盘，卖不出
def test_open_at_limit_down_cannot_sell():
    inst = stock()
    b = bar(open_="9.00", high="9.00", low="9.00", close="9.00",
            pre="10.00", up="11.00", down="9.00")
    engine, ledger = make_engine({inst.symbol: b}, inst)
    pos = engine.account.position(inst.symbol)
    pos.total_qty = 100
    pos.available_qty = 100
    pos.avg_cost = D("10.00")

    res = engine.step(b.ts, [Intent(inst.symbol, Side.SELL, qty=100)])
    assert not res.fills
    assert engine.account.position(inst.symbol).total_qty == 100


# 11. 重复 client_order_id 只产生一单
def test_idempotent_client_order_id():
    inst = stock()
    b = bar()
    ledger = Ledger.fresh("5000")
    calendar = TradingCalendar.always()
    intent = Intent(inst.symbol, Side.BUY, qty=100, client_order_id="SAME")
    o1 = ledger.submit(intent, b.ts, inst, b, calendar)
    o2 = ledger.submit(intent, b.ts, inst, b, calendar)
    assert o1.order_id == o2.order_id
    assert len(ledger.oms.orders) == 1


# 12. 重启后从快照恢复并对账
def test_restart_from_snapshot_reconciles():
    inst = stock()
    b = bar()
    engine, ledger = make_engine({inst.symbol: b}, inst)
    engine.step(b.ts, [Intent(inst.symbol, Side.BUY, qty=100)])
    ledger.settle()
    ledger.account.position(inst.symbol).realized_pnl = D("1.23")

    snap = ledger.snapshot()
    restored = Ledger.from_snapshot(snap)
    assert restored.reconcile(snap) == []
    assert restored.account.cash_available == ledger.account.cash_available
    assert restored.account.position(inst.symbol).total_qty == 100
    assert restored.account.position(inst.symbol).available_qty == 100
    # The snapshot must be a round trip, including the fields nothing reads yet.
    assert restored.account.fees_paid == ledger.account.fees_paid
    assert restored.account.realized_pnl == ledger.account.realized_pnl
    assert restored.account.dividend_income == ledger.account.dividend_income
    assert (
        restored.account.position(inst.symbol).realized_pnl
        == ledger.account.position(inst.symbol).realized_pnl
    )
    # ... and the order counter, or the next order reuses an id already in `orders`.
    assert restored.oms.seq == ledger.oms.seq
    again = restored.submit(Intent(inst.symbol, Side.BUY, qty=100,
                                   order_type=OrderType.LIMIT, limit_price=D("10.00")),
                            b.ts, inst, b, TradingCalendar.always())
    assert again.order_id != next(iter(ledger.oms.orders))


def test_board_lot_rules():
    """STAR starts at 200 and steps by 1; BSE starts at 100 and steps by 1."""
    main = Instrument("600000.SH", board=Board.MAIN, min_qty=100, qty_step=100)
    star = Instrument("688001.SH", board=Board.STAR, min_qty=200, qty_step=1)
    bse = Instrument("830799.BJ", board=Board.BSE, min_qty=100, qty_step=1)

    assert main.buy_lot(150) == 100 and main.buy_lot(99) == 0
    assert star.buy_lot(100) == 0 and star.buy_lot(200) == 200
    assert star.buy_lot(237) == 237
    assert bse.buy_lot(137) == 137

    # Selling a partial position still needs a legal step on the main boards.
    assert main.sell_lot(150, 300) == 0
    assert star.sell_lot(150, 300) == 150


# 19. 限价买入不得成交在限价之上（滑点夹在限价内）
def test_a_limit_buy_never_fills_above_its_limit():
    inst = stock()
    b = bar(open_="10.00", high="10.50", low="9.80", close="10.20")
    engine, ledger = make_engine({inst.symbol: b}, inst)

    res = engine.step(b.ts, [Intent(inst.symbol, Side.BUY, qty=100,
                                    order_type=OrderType.LIMIT, limit_price=D("10.00"))])

    assert len(res.fills) == 1
    assert res.fills[0].price == D("10.00"), "10 bps of slippage may not breach the limit"


# 20. 限价卖出不得成交在限价之下
def test_a_limit_sell_never_fills_below_its_limit():
    inst = stock()
    b = bar(open_="9.10", high="9.25", low="9.05", close="9.20")
    engine, ledger = make_engine({inst.symbol: b}, inst, cash="0")
    pos = ledger.account.position(inst.symbol)
    pos.total_qty = pos.available_qty = 100

    res = engine.step(b.ts, [Intent(inst.symbol, Side.SELL, qty=100,
                                    order_type=OrderType.LIMIT, limit_price=D("9.20"))])

    assert len(res.fills) == 1
    assert res.fills[0].price == D("9.20")


# 21. 封在涨停上的板，买价低于涨停价时不能成交
def test_a_limit_buy_below_a_sealed_ceiling_does_not_fill():
    inst = stock()
    b = bar(open_="11.00", high="11.00", low="11.00", close="11.00", up="11.00")
    engine, ledger = make_engine({inst.symbol: b}, inst)

    res = engine.step(b.ts, [Intent(inst.symbol, Side.BUY, qty=100,
                                    order_type=OrderType.LIMIT, limit_price=D("10.50"))])

    assert res.fills == [], "no seller at the ceiling can reach a bid below it"
    assert ledger.account.position(inst.symbol).total_qty == 0


def test_close_auction_accepts_orders_but_not_cancellation():
    calendar = TradingCalendar.always()
    ts = day(14, 58)
    assert calendar.session_at(ts) == Session.AUCTION_CLOSE
    assert calendar.can_submit(ts)
    assert not calendar.can_cancel(ts)


def test_aware_timestamps_are_normalized_to_exchange_time():
    calendar = TradingCalendar.always()
    ts = datetime(2026, 10, 25, 1, 21, tzinfo=UTC)
    assert calendar.session_at(ts) == Session.AUCTION_OPEN
    assert not calendar.can_cancel(ts), "09:21 Shanghai is inside the no-cancel window"


def test_gap_risk_uses_the_instruments_actual_limit():
    inst = Instrument(
        symbol="300001.SZ",
        board=Board.CHINEXT,
        limit_pct=D("0.20"),
    )
    b = bar(symbol=inst.symbol, up="12.00", down="8.00")
    risk = RiskEngine(RiskConfig(max_core_stock_single=D("1.00")))

    decision = risk.check(
        Intent(inst.symbol, Side.BUY, qty=200),
        Ledger.fresh("5000").account,
        inst,
        b,
    )

    assert not decision.allowed
    assert decision.reason == RejectReason.GAP_LOSS


def test_daily_loss_breaker_uses_knowable_open_not_future_close():
    inst = stock()
    b1 = bar(ts=day(d=25), close="10.00")
    engine, ledger = make_engine({inst.symbol: b1}, inst, cash="5000")
    engine.risk = RiskEngine(RiskConfig(max_core_stock_single=D("1.00")))
    pos = ledger.account.position(inst.symbol)
    pos.total_qty = pos.available_qty = 100
    pos.avg_cost = pos.last_price = D("10.00")
    ledger.account.cash_available = D("4000")
    engine._day_start_equity = ledger.account.equity()
    engine.step(b1.ts)

    b2 = bar(
        ts=day(d=26),
        open_="5.00",
        high="9.00",
        low="4.50",
        # The future close recovers most of the gap and must not disable the
        # pre-trade breaker: at order time only the 5.00 open is knowable.
        close="9.00",
        pre="10.00",
        up="11.00",
        down="4.00",
    )
    engine.data = StaticDataView({b2.ts: {inst.symbol: b2}})
    result = engine.step(b2.ts, [Intent(inst.symbol, Side.BUY, qty=100)])

    assert engine.daily_pnl_pct == D("-0.10")
    assert result.alerts
    assert result.alerts[0].reason == RejectReason.DAILY_LOSS


def test_settlement_preserves_live_order_reservations():
    ledger = Ledger.fresh("2000")
    calendar = TradingCalendar.always()
    sell_inst = stock("600000.SH")
    buy_inst = stock("000001.SZ")
    pos = ledger.account.position(sell_inst.symbol)
    pos.total_qty = pos.available_qty = 100
    pos.avg_cost = D("10.00")

    sell = ledger.submit(
        Intent(sell_inst.symbol, Side.SELL, qty=100,
               order_type=OrderType.LIMIT, limit_price=D("10.00")),
        day(), sell_inst, bar(symbol=sell_inst.symbol), calendar,
    )
    buy = ledger.submit(
        Intent(buy_inst.symbol, Side.BUY, qty=100,
               order_type=OrderType.LIMIT, limit_price=D("10.00")),
        day(), buy_inst, bar(symbol=buy_inst.symbol), calendar,
    )
    assert sell.is_open and buy.is_open

    ledger.settle()

    assert pos.available_qty == 0
    assert pos.frozen_qty == 100
    assert ledger.account.cash_frozen == buy.frozen_cash
    assert ledger.account.cash_available + ledger.account.cash_frozen == D("2000.00")


def test_restart_open_etf_order_preserves_cash_and_order_lifecycle():
    """A restored live order must survive settlement, cancellation and filling."""
    inst = Instrument(
        symbol="510300.SH",
        board=Board.ETF,
        is_etf=True,
        stamp_duty=D("0"),
        transfer_fee=D("0"),
    )
    b = bar(symbol=inst.symbol)
    calendar = TradingCalendar.always()
    intent = Intent(
        inst.symbol,
        Side.BUY,
        qty=100,
        order_type=OrderType.LIMIT,
        limit_price=D("10.00"),
        client_order_id="ETF-SNAPSHOT",
    )
    ledger = Ledger.fresh("5000")
    original = ledger.submit(intent, b.ts, inst, b, calendar)
    assert original.is_open
    assert ledger.account.cash_available == D("3995.00")
    assert ledger.account.cash_frozen == D("1005.00")
    snapshot = ledger.snapshot()

    restored = Ledger.from_snapshot(snapshot)
    assert restored.reconcile(snapshot) == []
    assert set(restored.oms.orders) == {original.order_id}
    assert set(restored.oms.client_index) == {"ETF-SNAPSHOT"}
    restored_order = restored.oms.client_index["ETF-SNAPSHOT"]
    assert restored_order.is_open
    assert restored.submit(intent, b.ts, inst, b, calendar) is restored_order
    assert len(restored.oms.orders) == 1

    restored.settle()
    assert restored.account.cash_available == D("3995.00")
    assert restored.account.cash_frozen == D("1005.00")
    assert restored.account.cash_available + restored.account.cash_frozen == D("5000.00")

    cancelled = Ledger.from_snapshot(snapshot)
    cancelled_order = cancelled.oms.client_index["ETF-SNAPSHOT"]
    cancelled.settle()
    assert cancelled.cancel(cancelled_order.order_id, b.ts, calendar).success
    assert cancelled.account.cash_available == D("5000.00")
    assert cancelled.account.cash_frozen == D("0.00")

    filled = Ledger.from_snapshot(snapshot)
    filled_order = filled.oms.client_index["ETF-SNAPSHOT"]
    filled.settle()
    fill = BarExecutionModel().match(filled_order, b, inst, b.ts).fill
    assert fill is not None
    assert filled.apply_fill(filled_order, fill)
    assert filled.account.cash_available == D("3995.00")
    assert filled.account.cash_frozen == D("0.00")
    assert filled.account.position(inst.symbol).total_qty == 100
    assert (
        filled.account.cash_available
        + D(fill.qty) * fill.price
        + filled.account.fees_paid
        == D("5000.00")
    )
