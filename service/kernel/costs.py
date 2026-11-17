"""A-share transaction costs.

The rates live on the **instrument**; this model only decides how they combine
with the broker's minimum. That split matters because "stock" was doing two jobs
at once: `is_stock` decided both "pays stamp duty" and "pays transfer fee", so a
convertible bond had to pretend to be an ETF to avoid a tax it does not owe, and
an ETF that ever gained a fee would have needed a code change instead of a
number.

Now a non-stock is expressed by zero rates on the instrument:

    stock:       stamp_duty 0.0005, transfer_fee 0.00001
    ETF:         stamp_duty 0,      transfer_fee 0
    convertible: stamp_duty 0,      transfer_fee 0   (T+0, 10-unit lot)

Commission stays here: it is a property of the account's agreement with the
broker, not of the security.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import D, Instrument, money


def _rate(key: str) -> Decimal:
    """Commission terms from configs/costs.yaml.

    These were literals here while ``costs.yaml`` and ``trading/fees.py`` held
    the same numbers; the yaml was documentation and this dataclass was the
    truth, which meant the file the docstring pointed at did nothing.
    """
    from service import settings

    return Decimal(str(settings.num(f"costs.{key}")))


@dataclass(frozen=True)
class CostModel:
    commission_rate: Decimal = field(default_factory=lambda: _rate("commission_rate"))
    min_commission: Decimal = field(default_factory=lambda: _rate("min_commission"))

    def commission(self, amount: Decimal) -> Decimal:
        # The broker minimum applies to a real execution, not to an empty
        # scenario. Keeping this consistent with round_trip_bps(0) also prevents
        # zero-notional estimates from acquiring a phantom CNY 5 charge.
        if amount == 0:
            return D("0.00")
        return money(max(self.min_commission, amount * self.commission_rate))

    def buy_fees(self, amount: Decimal, instrument: Instrument) -> Decimal:
        fees = self.commission(amount)
        if instrument.transfer_fee:
            fees += money(amount * instrument.transfer_fee)
        return money(fees)

    def sell_fees(self, amount: Decimal, instrument: Instrument) -> Decimal:
        fees = self.commission(amount)
        if instrument.transfer_fee:
            fees += money(amount * instrument.transfer_fee)
        if instrument.stamp_duty:
            fees += money(amount * instrument.stamp_duty)
        return money(fees)

    def incremental_buy_fees(
        self,
        amount: Decimal,
        instrument: Instrument,
        previous_amount: Decimal = Decimal(0),
        previous_fees: Decimal = Decimal(0),
    ) -> Decimal:
        """Charge the order-level minimum commission once across partial fills."""
        total = self.buy_fees(money(previous_amount + amount), instrument)
        return money(max(D("0"), total - previous_fees))

    def incremental_sell_fees(
        self,
        amount: Decimal,
        instrument: Instrument,
        previous_amount: Decimal = Decimal(0),
        previous_fees: Decimal = Decimal(0),
    ) -> Decimal:
        """Sell-side counterpart of :meth:`incremental_buy_fees`."""
        total = self.sell_fees(money(previous_amount + amount), instrument)
        return money(max(D("0"), total - previous_fees))

    def round_trip(self, amount: Decimal, instrument: Instrument) -> Decimal:
        return money(self.buy_fees(amount, instrument) + self.sell_fees(amount, instrument))

    def round_trip_bps(self, amount: Decimal, instrument: Instrument) -> float:
        """All-in fee cost of one round trip at this notional, in basis points."""
        if amount <= 0:
            return 0.0
        return float(self.round_trip(amount, instrument)) / float(amount) * 10_000
