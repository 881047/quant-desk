"""这只票的历史超额，扣掉**这个账本**的交易成本之后还剩多少。

`analog` 的 caveats 一直写着"未计佣金、印花税与滑点"，而 ledger 里每一条被证伪的
结构都是在扣掉 20bps 之后才判死的。两个数字分住在两个工具里，减法留给读者——
于是"-0.36% / 5 日"看起来像个中性读数。

而这台子的规模让这个减法不是细节：5,007 元的账本，一笔 1,502 元的往返，
最低佣金 5 元 × 2 就吃掉 66bps。这不是估算，是 `costs.yaml` 里的费率加上
执行模型的双边滑点，也就是内核真的会收的钱。
"""

from __future__ import annotations

import pytest

from service.kernel.models import Board, D, Instrument
from service.trading.analog import net_of_cost
from service.trading.impact import round_trip_cost_bps


class FakeBook:
    def __init__(self, equity: float):
        self.equity = equity

    def summary(self):
        return {"equity": self.equity}


class FakeMarket:
    def instrument(self, symbol, day):
        return Instrument(symbol=symbol, board=Board.MAIN, limit_pct=D("0.10"),
                          stamp_duty=D("0.0005"), transfer_fee=D("0.00001"))


class FakeDesk:
    def __init__(self, equity: float = 5007.51):
        self.book = FakeBook(equity)
        self.market = FakeMarket()

    def limits(self):
        return {"max_core_single": 0.3}


REPORT = {
    "5d": {"excess_day_mean": -0.0036, "excess_ci95": [-0.026, 0.013]},
    "10d": {"excess_day_mean": 0.0400, "excess_ci95": [0.005, 0.070]},
    "20d": {"excess_day_mean": None, "excess_ci95": [None, None]},
}


def test_the_cost_is_the_one_the_kernel_would_charge():
    """成本必须来自内核的费率表，不能是这里再算一遍的经验值。"""
    out = net_of_cost(FakeDesk(), "600519.SH", "20261030", REPORT)
    notional = 5007.51 * 0.3
    expected = round_trip_cost_bps(notional, FakeMarket().instrument("600519.SH", "20261030"))

    assert out["notional_cny"] == pytest.approx(notional, abs=0.01)
    assert out["cost_bps_roundtrip"] == expected["total_bps"]
    assert out["fees_bps"] == expected["fees_bps"]
    assert out["slippage_bps"] == expected["slippage_bps"]


def test_at_this_account_size_the_minimum_commission_dominates():
    """5,007 元的账本，一笔往返的费用是两位数百分比**的千分之一**——92bps 上下。

    这条断言是这轮改动的理由本身：一笔 1,502 元的成交，最低佣金 5 元 × 2 就是
    66bps。任何"历史超额 0.36%"在这种成本面前都不成立，而读者之前看不到它。
    """
    from service.kernel.costs import CostModel

    out = net_of_cost(FakeDesk(), "600519.SH", "20261030", REPORT)
    fees_cny = out["fees_bps"] / 10_000 * out["notional_cny"]

    # 两边各收一次最低佣金，本身就是 10 元——占 1,502 元的 66bps
    floor = 2 * float(CostModel().min_commission)
    assert fees_cny > floor, "费用必须至少是两个最低佣金"
    assert floor / out["notional_cny"] * 1e4 == pytest.approx(66.6, abs=0.3)
    assert out["fees_bps"] > 3 * out["slippage_bps"], "费用应显著大于滑点，否则单位错了"


def test_net_is_the_excess_minus_that_cost():
    out = net_of_cost(FakeDesk(), "600519.SH", "20261030", REPORT)
    cost = out["cost_bps_roundtrip"] / 10_000

    assert out["by_horizon"]["5d"] == pytest.approx(-0.0036 - cost, abs=1e-6)
    assert out["by_horizon"]["10d"] == pytest.approx(0.0400 - cost, abs=1e-6)


def test_missing_excess_stays_missing():
    """没有超额就写 null，不要用 0 或 -成本 冒充"测过"。"""
    out = net_of_cost(FakeDesk(), "600519.SH", "20261030", REPORT)

    assert out["by_horizon"]["20d"] is None
    assert out["survives_cost"]["20d"] is None


def test_survives_cost_needs_the_whole_interval_above_zero():
    """点估计超过成本、区间跨零，只能叫噪声。

    `10d` 的区间下界是 0.5%，成本约 0.92%——点估计 4% 看着很赚，扣完成本之后
    **整个区间不可能都在 0 上方**，所以是 false。这正是这块要防的误读。
    """
    out = net_of_cost(FakeDesk(), "600519.SH", "20261030", REPORT)

    assert out["by_horizon"]["10d"] > 0
    assert out["survives_cost"]["10d"] is False
    assert out["survives_cost"]["5d"] is False


def test_an_unpriced_book_says_it_cannot_say():
    """权益为 0 或标的定不了价时返回 None，不是"成本为 0"。"""
    assert net_of_cost(FakeDesk(0.0), "600519.SH", "20261030", REPORT) is None
