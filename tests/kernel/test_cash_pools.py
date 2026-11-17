"""可提现现金的两个池子必须对称地动。

A 股的现金有两个池子：可用余额（能用来下单）和可提现余额（T+1 之前不能提出
去的钱不算）。本台子把两者记在 ``cash_available`` 与 ``cash_withdrawable`` 上。

曾经这两边不对称：

* 冻结时只把可提现余额夹到可用余额以内，**没有从可提现池子里扣**；
* 释放时却把释放出来的钱**全额加回**可提现池子。

于是每一次"用当天卖出得到的钱去买入"，可提现余额就会往上飘一点。实测
2026-11-02：一个可提现余额 1,000 元的账户花掉 2,005 元之后，可提现余额变成
1,040.02 元——比开始时还多。

当前没有任何规则读这个字段，所以它不影响成交；但它是账户快照里的一个数字，
将来谁写"可提现/可再投资"规则就会被它带偏。这个文件把两个池子的对称性钉住。
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
    Side,
)
from service.kernel.oms import OrderManager

TS = datetime(2026, 10, 30, 10, 0, tzinfo=SHANGHAI)
CAL = TradingCalendar.always()


def bar(price: str = "10.00") -> Bar:
    return Bar(symbol="600000.SH", ts=TS, open=D(price), high=D("11.00"),
               low=D("9.00"), close=D(price), pre_close=D(price),
               volume=1_000_000, amount=D("10000000"),
               up_limit=D("11.00"), down_limit=D("9.00"))


def account(withdrawable: str = "1000") -> Account:
    """An account whose cash is partly T+1-restricted."""
    acct = Account()
    acct.cash_available = D("5000")
    acct.cash_withdrawable = D(withdrawable)
    return acct


def _buy(acct: Account, qty: int = 200):
    manager = OrderManager()
    order = manager.submit(
        Intent("600000.SH", Side.BUY, qty=qty), TS, acct, Instrument("600000.SH"),
        bar(), CAL)
    return manager, order


def _fill(manager: OrderManager, order, acct: Account, qty: int = 200,
          price: str = "10.00", fees: str = "5.00") -> Fill:
    fill = Fill(fill_id=f"f-{order.order_id}-{qty}", order_id=order.order_id,
                symbol="600000.SH", side=Side.BUY, qty=qty, price=D(price),
                fees=D(fees), ts=TS, rule_id="TEST")
    manager.apply_fill(order, fill, acct)
    return fill


def test_spending_cash_never_grows_the_withdrawable_pool():
    acct = account("1000")
    manager, order = _buy(acct)
    _fill(manager, order, acct)

    assert acct.cash_withdrawable <= D("1000"), (
        f"可提现余额从 1000 变成了 {acct.cash_withdrawable}——花掉钱反而变多了")


def test_reserving_and_releasing_gives_the_withdrawable_money_back():
    """冻结再撤销，钱要原样回到可提现池子里。"""
    acct = account("1000")
    manager, order = _buy(acct)

    assert acct.cash_withdrawable < D("1000"), "冻结的钱不该还能提现"
    manager.expire(order.order_id, TS, acct, CAL)
    assert acct.cash_withdrawable == D("1000"), (
        f"撤单之后可提现余额是 {acct.cash_withdrawable}，应该回到 1000")


def test_the_withdrawable_pool_never_exceeds_what_is_available():
    acct = account("5000")
    manager, order = _buy(acct)
    _fill(manager, order, acct)

    assert acct.cash_withdrawable <= acct.cash_available, (
        f"可提现 {acct.cash_withdrawable} > 可用 {acct.cash_available}")


def test_the_order_remembers_which_part_of_the_freeze_was_withdrawable():
    """订单要记住自己从可提现池子里拿了多少，释放时才放得回去。"""
    acct = account("1000")
    manager, order = _buy(acct)

    assert order.frozen_withdrawable == D("1000"), (
        f"订单记住了 {order.frozen_withdrawable}，应该是 1000")
    assert acct.cash_withdrawable == D("0"), (
        f"可提现池子剩 {acct.cash_withdrawable}，应该被冻结占光")


@pytest.mark.parametrize(("initial", "expected"), [
    ("0", "0"),             # every available cent was T+1 restricted
    ("1000", "0"),          # mixed reserve, wholly spent withdrawable slice
    ("2005", "0"),          # withdrawable exactly equals the eventual cost
    ("3000", "995"),        # mixed pool with an unused reserve slice
    ("5000", "2995"),       # every available cent was withdrawable
])
def test_a_fill_debits_the_withdrawable_pool_exactly_once(initial, expected):
    """Submit already removed the reserve; a fill only returns the unused part."""
    acct = account(initial)
    manager, order = _buy(acct)

    _fill(manager, order, acct)       # 200 x 10 + 5 commission = 2,005

    assert acct.cash_available == D("2995")
    assert acct.cash_withdrawable == D(expected)


def test_a_partial_fill_and_cancel_keep_the_withdrawable_pool_exact():
    acct = account("5000")
    manager, order = _buy(acct)

    _fill(manager, order, acct, qty=100)   # 100 x 10 + 5 commission = 1,005

    assert acct.cash_total() == D("3995")
    assert acct.cash_withdrawable == acct.cash_available

    manager.expire(order.order_id, TS, acct, CAL)
    assert acct.cash_available == D("3995")
    assert acct.cash_withdrawable == D("3995")
