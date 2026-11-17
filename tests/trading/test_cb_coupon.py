"""A held convertible pays interest every year, not only at delisting.

Before this, a bond bought in 2024 and held to 2026 showed zero income: the only
event modelled was the return of par. That understates the return of every carry
trade and, more importantly, makes the ledger disagree with any statement the
holder could produce.

Two rules the tests pin, both easy to get wrong:

1. The rate comes from the period that is **ending**. `cb_basic.coupon_rate` is
   a single number and for a step-up bond it is the final period, so using it
   everywhere overstates year one by an order of magnitude.
2. Interest is taxed at 20% at source. Crediting the gross amount overstates
   usable cash by 25%; the difference is not a rounding detail on a 2% coupon.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from service.kernel.corp_action import CBCoupon
from service.kernel.models import Account, D, Position
from service.trading.market import _parse_rate_clause


def _account(qty: int, symbol: str = "113050.SH") -> Account:
    account = Account()
    account.positions[symbol] = Position(symbol=symbol, total_qty=qty,
                                         available_qty=qty, avg_cost=D("100"))
    return account


def test_a_step_up_clause_is_read_period_by_period():
    clause = ("20180101-20181231,票面利率:0.20%;20190101-20191231,票面利率:0.50%;"
              "20200101-20201231,票面利率:1.00%")

    assert _parse_rate_clause(clause) == [
        ("20180101", "20181231", Decimal("0.002")),
        ("20190101", "20191231", Decimal("0.005")),
        ("20200101", "20201231", Decimal("0.01")),
    ]


def test_an_unparseable_clause_pays_nothing_rather_than_a_guess():
    assert _parse_rate_clause("") == []
    assert _parse_rate_clause("按年付息，具体见募集说明书") == []


def test_the_coupon_taxes_at_source():
    coupon = CBCoupon(symbol="113050.SH", pay_date=date(2026, 10, 29),
                      par=D("100"), rate=D("0.02"))

    # 10 bonds x ¥100 par = ¥1,000 face; 2% = ¥20 gross, ¥16 after withholding.
    assert coupon.gross(10) == D("20.00")
    assert coupon.net(10) == D("16.00")


def test_applying_a_coupon_credits_cash_and_leaves_cost_alone():
    """A coupon is income, not a return of capital — unlike a dividend."""
    account = _account(10)
    before = account.positions["113050.SH"].avg_cost
    cash_before = account.cash_available
    coupon = CBCoupon(symbol="113050.SH", pay_date=date(2026, 10, 29),
                      par=D("100"), rate=D("0.02"))

    row = coupon.apply(account)

    assert row["applied"] and row["kind"] == "coupon"
    assert account.cash_available - cash_before == D("16.00")
    assert account.dividend_income == D("16.00")
    assert account.positions["113050.SH"].avg_cost == before


def test_a_coupon_without_a_position_pays_nothing():
    row = CBCoupon(symbol="113050.SH", pay_date=date(2026, 10, 29),
                   par=D("100"), rate=D("0.02")).apply(Account())

    assert row["applied"] is False
    assert row["reason"] == "no position"


def test_the_pay_date_is_the_end_of_the_period_not_its_start():
    """`20180101-20181231` pays on 1231. Paying on 0101 shifts every coupon."""
    periods = _parse_rate_clause("20180101-20181231,票面利率:0.20%")

    assert periods[0][1] == "20181231"
    assert "20180101" not in (periods[0][1],)
