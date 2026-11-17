"""2026-11-04 内核审查：四处"同一事实、两个答案"的修正。

每条用例对应审查报告里的一项改动，断言的是可观测行为而不是实现细节：

1. 超卖与 T+1 是两件事，不能共用一个拒绝码；
2. 风控与订单管理必须用同一把尺子量涨跌停带宽；
3. 股数校验不能靠 float，否则大数抛 OverflowError、长小数被静默取整；
4. 转债到期时若还挂着卖单，必须先把单子退掉，否则快照重载会失败。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.corp_action import CBMaturity
from service.kernel.execution import BarExecutionModel
from service.kernel.ledger import Ledger
from service.kernel.models import (
    Account,
    Bar,
    Board,
    D,
    Instrument,
    Intent,
    Order,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
)
from service.kernel.risk import RiskEngine

TS = datetime(2026, 11, 4, 9, 30, tzinfo=SHANGHAI)
CAL = TradingCalendar.always()
MODEL = BarExecutionModel()


def make_bar(
    *,
    symbol: str = "600000.SH",
    open_: str = "10.00",
    high: str = "10.50",
    low: str = "9.50",
    close: str = "10.20",
    pre_close: str = "10.00",
    up: str = "11.00",
    down: str = "9.00",
    volume: int = 5_000_000,
    prior_volume: int = 5_000_000,
    priced_at: str = "open",
    ts: datetime = TS,
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
        priced_at=priced_at,
    )


def stock(symbol: str = "600000.SH") -> Instrument:
    return Instrument(symbol)


def bond(symbol: str = "113001.SH") -> Instrument:
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


def limit_order(price: str, qty: int = 100, side: Side = Side.BUY,
                symbol: str = "600000.SH") -> Order:
    return Order(
        order_id="O1",
        client_order_id="c1",
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.LIMIT,
        limit_price=D(price),
    )


def buy_and_fill(ledger: Ledger, instrument: Instrument, bar: Bar,
                 qty: int = 100) -> None:
    order = ledger.submit(
        Intent(instrument.symbol, Side.BUY, qty=qty,
               order_type=OrderType.LIMIT, limit_price=bar.open,
               client_order_id=f"seed-buy-{instrument.symbol}"),
        bar.ts, instrument, bar, CAL)
    result = MODEL.match(order, bar, instrument, bar.ts, CAL)
    assert result.filled, result.detail
    ledger.apply_fill(order, result.fill)


# ── 1. 超卖 ≠ T+1 ───────────────────────────────────────────────────────────
def test_selling_more_than_held_is_not_reported_as_t_plus_one():
    """卖 500 股而只持 100 股，曾经回 T_PLUS_ONE——等于让人等明天再试。

    明天同样失败，所以这个错误码把"永远不行"伪装成"等一天"，是最坏的一类
    诊断。真正的 T+1 仍然必须是 T_PLUS_ONE（见下一个用例）。
    """
    ledger = Ledger.fresh("100000")
    instrument = stock()
    bar = make_bar()
    buy_and_fill(ledger, instrument, bar)

    oversell = ledger.submit(
        Intent("600000.SH", Side.SELL, qty=500, client_order_id="oversell"),
        TS, instrument, bar, CAL)

    assert oversell.status is OrderStatus.REJECTED
    assert oversell.reason == RejectReason.INSUFFICIENT_SHARES.value


def test_selling_todays_shares_is_still_t_plus_one():
    """当天买入的股票确实只是"还没到账"，不能被上面的修正顺手放行。"""
    ledger = Ledger.fresh("100000")
    instrument = stock()
    bar = make_bar()
    buy_and_fill(ledger, instrument, bar)

    same_day = ledger.submit(
        Intent("600000.SH", Side.SELL, qty=100, client_order_id="t1"),
        TS, instrument, bar, CAL)

    assert same_day.status is OrderStatus.REJECTED
    assert same_day.reason == RejectReason.T_PLUS_ONE.value


def test_the_reference_risk_rules_make_the_same_distinction():
    """``RiskEngine`` 与 OMS 是两条路径，拒绝码必须一致。"""
    account = Account(cash_available=D("100000"), cash_withdrawable=D("100000"),
                      peak_equity=D("100000"))
    account.position("600000.SH").total_qty = 100
    account.position("600000.SH").available_qty = 100
    engine = RiskEngine()

    oversell = engine.check(Intent("600000.SH", Side.SELL, qty=500),
                            account, stock(), make_bar())
    assert oversell.reason == RejectReason.INSUFFICIENT_SHARES

    account.position("600000.SH").available_qty = 0
    t1 = engine.check(Intent("600000.SH", Side.SELL, qty=100),
                      account, stock(), make_bar())
    assert t1.reason == RejectReason.T_PLUS_ONE


# ── 2. 风控与 OMS 用同一把尺子量带宽 ────────────────────────────────────────
def test_risk_accepts_a_limit_inside_the_band_derived_from_pre_close():
    """行情缺 up_limit/down_limit 列时，两边都必须从昨收推带宽。

    修改前：风控直接读 ``bar.up_limit``（0），判 PRICE_LIMIT 拒绝；OMS 走
    ``bar.limits()`` 推出 11.00/9.00 并接受。同一个 bar，两个答案，而且被拒绝
    的那一单是完全合法的。
    """
    bar = make_bar(up="0", down="0")          # 供应商没给带宽列
    assert bar.limits(stock()) == (D("11.00"), D("9.00"))
    account = Account(cash_available=D("100000"), cash_withdrawable=D("100000"),
                      peak_equity=D("100000"))

    decision = RiskEngine().check(
        Intent("600000.SH", Side.BUY, qty=100, order_type=OrderType.LIMIT,
               limit_price=D("10.10")),
        account, stock(), bar)

    assert decision.allowed, decision.detail


def test_risk_still_refuses_when_no_band_can_be_established():
    """推不出带宽时仍然拒绝：没有带宽就没有"这个价格合法"这回事。"""
    bar = make_bar(up="0", down="0", pre_close="0")
    account = Account(cash_available=D("100000"), cash_withdrawable=D("100000"),
                      peak_equity=D("100000"))

    decision = RiskEngine().check(
        Intent("600000.SH", Side.BUY, qty=100, order_type=OrderType.LIMIT,
               limit_price=D("10.10")),
        account, stock(), bar)

    assert not decision.allowed
    assert decision.reason == RejectReason.PRICE_LIMIT


# ── 3. 股数校验不靠 float ───────────────────────────────────────────────────
def test_an_unrepresentable_share_count_is_refused_not_raised():
    """``float(10**400)`` 抛 OverflowError，旧代码让它直接冲出 submit（API 500）。"""
    ledger = Ledger.fresh("100000")
    bar = make_bar()

    order = ledger.submit(
        Intent("600000.SH", Side.BUY, qty=10 ** 400, client_order_id="huge"),
        TS, stock(), bar, CAL)

    assert order.status is OrderStatus.REJECTED
    assert order.reason in {RejectReason.BAD_QTY.value,
                            RejectReason.INSUFFICIENT_CASH.value}


@pytest.mark.parametrize("qty", [D("100.00000000000000001"), D("100.5"), 100.5])
def test_a_fractional_share_count_is_refused_even_when_float_would_hide_it(qty):
    """``float`` 只有 53 位有效数字：长小数会被抹成整数然后当成合法股数。"""
    ledger = Ledger.fresh("100000")
    bar = make_bar()

    order = ledger.submit(
        Intent("600000.SH", Side.BUY, qty=qty, client_order_id="frac"),
        TS, stock(), bar, CAL)

    assert order.status is OrderStatus.REJECTED
    assert order.reason == RejectReason.BAD_QTY.value


def test_an_integral_decimal_from_json_is_still_accepted():
    """JSON 往返可能把 100 变成 Decimal("100")，这不是小数。"""
    ledger = Ledger.fresh("100000")
    bar = make_bar()

    order = ledger.submit(
        Intent("600000.SH", Side.BUY, qty=D("100"), client_order_id="dec"),
        TS, stock(), bar, CAL)

    assert order.status is OrderStatus.NEW


# ── 4. 到期赎回不能把挂单留在账上 ──────────────────────────────────────────
def _bond_with_open_sell() -> tuple[Ledger, Order]:
    ledger = Ledger.fresh("100000")
    instrument = bond()
    bar = make_bar(symbol=instrument.symbol, open_="100.000", high="101.000",
                   low="99.000", close="100.500", pre_close="100.000",
                   up="120.000", down="80.000")
    buy_and_fill(ledger, instrument, bar, qty=10)
    sell = ledger.submit(
        Intent(instrument.symbol, Side.SELL, qty=10,
               order_type=OrderType.LIMIT, limit_price=D("100.000"),
               client_order_id="sell"),
        TS, instrument, bar, CAL)
    assert sell.status is OrderStatus.NEW, sell.reason
    return ledger, sell


def test_maturity_refuses_rather_than_zeroing_a_reserved_position():
    """没有订单簿就拒绝赎回。零掉持仓会把 10 张冻结留在卖单上，快照随即不可重载。"""
    ledger, sell = _bond_with_open_sell()

    row = CBMaturity("113001.SH", date(2026, 11, 4)).apply(ledger.account)

    assert row["applied"] is False
    assert row["reason"] == "OPEN_SELL_ORDERS"
    assert ledger.account.peek("113001.SH").total_qty == 10
    assert sell.frozen_shares == 10


def test_maturity_with_the_book_retires_the_order_and_the_snapshot_reloads():
    ledger, sell = _bond_with_open_sell()

    row = CBMaturity("113001.SH", date(2026, 11, 4)).apply(
        ledger.account, oms=ledger.oms, ts=TS)

    assert row["applied"] is True
    assert row["expired_orders"] == 1
    assert sell.status is OrderStatus.EXPIRED
    assert ledger.account.peek("113001.SH").total_qty == 0
    snapshot = json.loads(json.dumps(ledger.snapshot()))
    restored = Ledger.from_snapshot(snapshot)
    assert restored.reconcile(snapshot) == []


def test_expire_symbol_leaves_the_other_symbol_alone():
    ledger = Ledger.fresh("100000")
    bar_a = make_bar(symbol="600000.SH")
    bar_b = make_bar(symbol="000001.SZ")
    for symbol, bar in (("600000.SH", bar_a), ("000001.SZ", bar_b)):
        buy_and_fill(ledger, stock(symbol), bar)
        ledger.settle()          # 让今天的买入变成可卖
        ledger.submit(
            Intent(symbol, Side.SELL, qty=100, order_type=OrderType.LIMIT,
                   limit_price=bar.open, client_order_id=f"sell-{symbol}"),
            TS, stock(symbol), bar, CAL)

    expired = ledger.oms.expire_symbol("600000.SH", TS, ledger.account)

    assert len(expired) == 1
    a = ledger.account.peek("600000.SH")
    b = ledger.account.peek("000001.SZ")
    assert a.frozen_qty == 0 and a.available_qty == a.total_qty
    assert b.frozen_qty == 100
    assert len(ledger.oms.open_orders()) == 1
    assert a.is_consistent() and b.is_consistent()


# ── 5. 限价单成交要标明"这是日内触及，不是开盘价" ──────────────────────────
def test_a_limit_below_the_open_is_labelled_as_a_day_touch():
    """买价低于开盘价时，成交价只能是限价，而且不是 09:30 的成交。"""
    result = MODEL.match(limit_order("9.80"), make_bar(), stock(), TS)

    assert result.filled, result.detail
    assert result.fill.price == D("9.80")
    assert result.fill.rule_id.startswith("LIMIT_ORDER")
    assert "+TOUCH_DAY" in result.fill.rule_id


def test_a_limit_at_the_open_is_not_labelled_as_a_day_touch():
    result = MODEL.match(limit_order("10.00"), make_bar(), stock(), TS)

    assert result.filled, result.detail
    assert "+TOUCH_DAY" not in result.fill.rule_id


# ── 6. 实时 bar 上"挂低了"不等于"成交了" ──────────────────────────────────
def test_a_live_limit_below_the_quote_is_refused_not_filled_at_the_limit():
    """现价 16.38 挂买 16.00：这笔单还没轮到成交，不能用当日最低价当证据。

    修改前它按 16.00 立即成交——拿到全天最好的价格、TCA 延迟恒为 0。那是
    凭空造出来的一笔交易，而账本的全部价值就在于没有这种记录。
    """
    bar = make_bar(open_="16.38", high="16.80", low="15.90", close="16.38",
                   pre_close="16.00", up="17.60", down="14.40",
                   priced_at="live")

    result = MODEL.match(limit_order("16.00"), bar, stock(), TS)

    assert not result.filled
    assert result.reject_reason == RejectReason.PRICE_LIMIT
    assert "not marketable" in result.detail


def test_a_live_limit_at_the_quote_still_fills():
    """可成交的实时限价单不能被上面的规则误伤。"""
    bar = make_bar(open_="16.38", high="16.80", low="15.90", close="16.38",
                   pre_close="16.00", up="17.60", down="14.40",
                   priced_at="live")

    result = MODEL.match(limit_order("16.38"), bar, stock(), TS)

    assert result.filled, result.detail
    assert result.fill.price == D("16.38")



def test_maturity_expires_a_working_buy_and_frees_its_cash():
    """到期只撤卖单，是这条 bug 留下的那一半。

    买单冻的是**现金**，不改 ``frozen_qty``，所以 ``if pos.frozen_qty > 0``
    那道闸门根本不会为一个挂着的买单打开：赎回照做、持仓清零，``cash_frozen``
    却把 900 元永远占着。而且再也解不掉——标的已经不存在，没有哪个交易日会
    来结算它。
    """
    from service.kernel.models import OrderStatus as _Status

    ledger = Ledger.fresh("100000")
    instrument = bond()
    bar = make_bar(symbol=instrument.symbol, open_="100.000", high="101.000",
                   low="99.000", close="100.500", pre_close="100.000",
                   up="120.000", down="80.000")
    buy_and_fill(ledger, instrument, bar, qty=10)

    # 一张挂着不成交的买单：限价 90 低于当日最低 99。
    working = ledger.submit(
        Intent(instrument.symbol, Side.BUY, qty=10,
               order_type=OrderType.LIMIT, limit_price=D("90.000"),
               client_order_id="working-buy"),
        TS + timedelta(hours=1), instrument, bar, CAL)
    assert working.status is _Status.NEW, working.reason
    frozen_before = ledger.account.cash_frozen
    assert frozen_before >= D("900"), frozen_before

    row = CBMaturity("113001.SH", date(2026, 11, 4)).apply(
        ledger.account, oms=ledger.oms, ts=TS + timedelta(hours=2))

    assert row["applied"] is True, row
    assert row["expired_orders"] == 1
    assert working.status is _Status.EXPIRED
    assert ledger.account.cash_frozen == D("0"), "买单冻结的现金没有释放"
    snapshot = json.loads(json.dumps(ledger.snapshot()))
    Ledger.from_snapshot(snapshot).reconcile(snapshot)
