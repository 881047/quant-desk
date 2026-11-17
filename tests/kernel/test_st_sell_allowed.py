"""ST 股：只挡买入，绝不挡卖出。

这条规则在三个地方写着，含义一致——ST 只挡买入，卖出永远放行，风控不把持仓
锁在里面。自动止损是另一条链路，不在这里：内核/接口不替你下**新判断**的单，
但破线持续 ``autostop.confirm_seconds`` 后，抓取循环经 ``/decision/propose +
approve`` 代执行建仓票上写好的止损（``configs/params.yaml`` 的
``autostop.enabled`` 可关）。

* ``trading/guard.py``：``st_blocked = inst.is_st and side == "BUY"``，
  检查项文案就写着 "ST: sells allowed"；
* ``kernel/risk.py``：ST 只在买入时拒绝；
* ``kernel/oms.py``：同上。

曾经 ``risk`` 与 ``oms`` 对买卖一律拒绝，于是**股票被 ST 之后持仓就卖不掉了**：
止损单、减仓单、清仓单一律 ``ST`` 拒绝，而 guard 那一层放行——两层口径不一致，
所以谁也没发现。风控在最需要你跑的时候把你锁住，是最坏的一类故障。

北交所那条已经放行（"已有的仓位必须永远能出"），说明原则本来就清楚，
只是漏了 ST 这一档。本文件把三个口径钉在一起，让它不会再分叉。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.models import (
    Account,
    Bar,
    D,
    Fill,
    Instrument,
    Intent,
    OrderStatus,
    RejectReason,
    Side,
)
from service.kernel.oms import OrderManager
from service.kernel.risk import RiskEngine

TS = datetime(2026, 10, 30, 10, 0, tzinfo=SHANGHAI)
CAL = TradingCalendar.always()


def bar(price: str = "10.00") -> Bar:
    return Bar(symbol="600000.SH", ts=TS, open=D(price), high=D("11.00"),
               low=D("9.00"), close=D(price), pre_close=D(price),
               volume=1_000_000, amount=D("10000000"),
               up_limit=D("11.00"), down_limit=D("9.00"))


def st_instrument() -> Instrument:
    return Instrument("600000.SH", is_st=True)


def account_with_position(qty: int = 100) -> Account:
    account = Account()
    account.cash_available = D("5000")
    account.cash_withdrawable = D("5000")
    account.position("600000.SH")
    pos = account.positions["600000.SH"]
    pos.total_qty = qty
    pos.available_qty = qty
    pos.avg_cost = D("10.00")
    return account


# ── 买入仍然被挡 ────────────────────────────────────────────────────────────
def test_buying_an_st_stock_is_still_refused():
    decision = RiskEngine().check(
        Intent("600000.SH", Side.BUY, qty=100), Account(), st_instrument(), bar())
    assert not decision.allowed
    assert decision.reason == RejectReason.ST


def test_the_order_manager_also_refuses_an_st_buy():
    order = OrderManager().submit(
        Intent("600000.SH", Side.BUY, qty=100), TS, Account(), st_instrument(),
        bar(), CAL)
    assert order.status == OrderStatus.REJECTED
    assert order.reason == RejectReason.ST.value


# ── 卖出必须放行 ────────────────────────────────────────────────────────────
def test_selling_an_st_position_passes_the_risk_gate():
    decision = RiskEngine().check(
        Intent("600000.SH", Side.SELL, qty=100), account_with_position(),
        st_instrument(), bar())
    assert decision.allowed, decision.reason


def test_selling_an_st_position_is_accepted_by_the_order_manager():
    order = OrderManager().submit(
        Intent("600000.SH", Side.SELL, qty=100), TS, account_with_position(),
        st_instrument(), bar(), CAL)
    assert order.status != OrderStatus.REJECTED
    assert order.reason != RejectReason.ST.value
    assert order.qty == 100


def test_an_st_position_can_be_sold_end_to_end():
    """买 → 变成 ST → 卖出：卖出必须成交。

    这是那个 bug 的完整形状——bug 出在"股票被 ST 之后"这一步，
    只有整条链走完才算真的修好。
    """
    account = account_with_position()
    manager = OrderManager()
    order = manager.submit(
        Intent("600000.SH", Side.SELL, qty=100), TS, account, st_instrument(),
        bar(), CAL)
    fill = Fill(fill_id="f1", order_id=order.order_id, symbol="600000.SH",
                side=Side.SELL, qty=100, price=D("10.00"), fees=D("5.00"),
                ts=TS, rule_id="TEST")
    assert manager.apply_fill(order, fill, account)
    assert order.status == OrderStatus.FILLED
    assert account.positions["600000.SH"].total_qty == 0
    assert account.cash_available > D("5000")


def test_a_full_position_of_an_st_name_can_be_closed_even_on_an_odd_lot():
    """清仓允许零股：持有 150 股时卖掉全部 150 股是合法的。"""
    account = account_with_position(qty=150)
    order = OrderManager().submit(
        Intent("600000.SH", Side.SELL, qty=150), TS, account, st_instrument(),
        bar(), CAL)
    assert order.status != OrderStatus.REJECTED
    assert order.qty == 150


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_the_two_layers_agree_on_what_st_means(side):
    """guard 放行卖出，内核就必须放行；guard 拦下买入，内核也必须拦。

    口径分叉过一次（guard 只拦 BUY，内核买卖都拦），所以这次把两层放在
    同一个测试里对着看，而不是各写各的断言。
    """
    account = account_with_position() if side == Side.SELL else Account()
    decision = RiskEngine().check(
        Intent("600000.SH", side, qty=100), account, st_instrument(), bar())
    order = OrderManager().submit(
        Intent("600000.SH", side, qty=100), TS, account, st_instrument(),
        bar(), CAL)
    risk_says_no = decision.reason == RejectReason.ST
    oms_says_no = order.reason == RejectReason.ST.value
    assert risk_says_no == oms_says_no
    assert risk_says_no is (side == Side.BUY)
