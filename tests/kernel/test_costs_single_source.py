"""One fee schedule, three places that used to keep a copy of it.

`CostModel` had its own stamp-duty and transfer-fee constants, `Instrument`
carried the same two numbers as fields nobody read, and `trading/fees.py` had a
third copy in a YAML file that rounded independently. Changing a rate meant
finding all three.

Now the rates live on the instrument, `CostModel` only decides how they combine
with the broker minimum, and `fees.py` reads the schedule and shapes the output.
"""

from __future__ import annotations

import pytest

from service.kernel.costs import CostModel
from service.kernel.models import Board, D, Instrument
from service.research.spec import fee_instrument


def test_a_stock_pays_commission_duty_and_transfer_fee():
    model = CostModel()
    fees = model.sell_fees(D("10000"), Instrument("600000.SH"))
    # 5 + 0.10 transfer + 5.00 stamp = 10.10
    assert fees == D("10.10")


def test_zero_rates_on_the_instrument_are_what_makes_it_not_a_stock():
    etf = Instrument("510300.SH", is_etf=True, board=Board.ETF,
                     stamp_duty=D("0"), transfer_fee=D("0"))
    assert CostModel().sell_fees(D("10000"), etf) == D("5.00")


def test_an_etf_cannot_silently_keep_a_stamp_duty():
    # The defaults carry 0.0005; an ETF that forgets to zero them is a bug, not
    # a slightly expensive ETF. Fail at construction instead of at the tax line.
    with pytest.raises(ValueError, match="stamp duty"):
        Instrument("510300.SH", is_etf=True, board=Board.ETF)


def test_convertible_bond_pays_no_stamp_duty_or_transfer_fee():
    bond = fee_instrument("cb")
    assert bond.is_etf
    assert bond.stamp_duty == D("0") and bond.transfer_fee == D("0")
    # 10000 notional: 5 buy + 5 sell, nothing else.
    assert CostModel().round_trip(D("10000"), bond) == D("10.00")


def test_minimum_commission_dominates_a_small_ticket():
    model = CostModel()
    stock = Instrument("600000.SH")
    small = model.round_trip_bps(D("1200"), stock)
    large = model.round_trip_bps(D("50000"), stock)
    assert small > 3 * large
    # 1200 元来回：5.00 + 5.00 佣金 + 0.60 印花税 + 0.02 过户费 = 10.62 → 88.5bps
    assert small == pytest.approx(88.5, abs=0.2)


def test_fees_module_uses_the_same_model():
    from service.trading import fees

    bond = fee_instrument("cb")
    computed = fees.compute("SELL", 100.0, 100, instrument=bond)
    expected = CostModel().sell_fees(D("10000"), bond)
    assert computed["stamp_duty"] == 0.0
    assert computed["transfer_fee"] == 0.0
    assert computed["total"] == pytest.approx(float(expected))
    # The itemised lines must still add up to the total the kernel charged.
    assert (computed["commission"] + computed["stamp_duty"]
            + computed["transfer_fee"]) == pytest.approx(computed["total"])


def test_zero_notional_has_no_commission_or_round_trip_fee():
    """No transaction cannot incur the broker's minimum commission."""
    model = CostModel()
    stock = Instrument("600000.SH")
    assert model.commission(D("0")) == D("0")
    assert model.buy_fees(D("0"), stock) == D("0")
    assert model.sell_fees(D("0"), stock) == D("0")
    assert model.round_trip(D("0"), stock) == D("0")
    assert model.round_trip_bps(D("0"), stock) == 0.0


def test_fees_module_itemises_a_stock_sale():
    from service.trading import fees

    computed = fees.compute("SELL", 8.30, 200)
    assert computed["stamp_duty"] == pytest.approx(0.83, abs=0.01)
    assert computed["transfer_fee"] == pytest.approx(0.02, abs=0.01)
    assert computed["total"] == pytest.approx(5.85, abs=0.01)
