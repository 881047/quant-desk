"""Corporate actions: cash dividends, stock dividends, transfers.

The ledger must adjust shares, cash and average cost on the ex-date.
No market-price adjustment is done here; the ex-date bar already reflects it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from .models import Account, D, money
from .units import CB_PAR, COST_BASIS_QUANT

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from .oms import OrderManager


def _withholding() -> Decimal:
    """Interest tax withheld at payment, from configs/params.yaml.

    It is a tax rule, not a mechanism: a literal here would make the next change
    to the rate look like a code change nobody dares to make.
    """
    from service import settings

    return Decimal(str(settings.num("tax.cb_coupon_withholding")))


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    ex_date: date
    cash_div_per_share: Decimal = D("0")
    stock_div_per_share: Decimal = D("0")
    transfer_per_share: Decimal = D("0")

    def apply(self, account: Account, *, oms=None) -> dict:
        """Credit the ex-date entitlement **gross**, and say why.

        A 现金分红 is not withheld at the ex-date. A-share dividend tax is
        collected when the shares are *sold*, at a rate set by the holding period
        (20% under one month, 10% up to a year, exempt beyond), which is the
        opposite of a bond coupon: interest is withheld at payment. That is why
        :class:`CBCoupon` below books a net amount and this class books a gross
        one — not an oversight, a difference in when the tax happens.

        What is therefore **not** modelled: the tax owed on a sale that closes a
        position held for less than a year. At this desk's holding periods (days)
        that is the 20% bucket, so a dividend captured and then sold inside a
        month is overstated by 20% of the payout. Small in absolute terms on a
        5,000 CNY account, and deliberately not guessed at here.
        """
        pos = account.peek(self.symbol)
        old_qty = pos.total_qty if pos is not None else 0
        if pos is None or old_qty <= 0:
            return {"symbol": self.symbol, "applied": False, "reason": "NO_POSITION"}

        cash = money(D(old_qty) * self.cash_div_per_share)
        new_shares = int(
            (D(old_qty) * (self.stock_div_per_share + self.transfer_per_share)).to_integral_value(
                rounding=ROUND_DOWN
            )
        )
        new_qty = old_qty + new_shares

        if new_qty > 0:
            gross_cost = pos.avg_cost * D(old_qty)
            adjusted = (gross_cost - cash) / D(new_qty)
            pos.avg_cost = adjusted.quantize(COST_BASIS_QUANT, rounding=ROUND_HALF_UP)
            if pos.avg_cost < 0:
                pos.avg_cost = D("0")

        pos.total_qty = new_qty
        # Assume bonus shares are available on the ex-date for paper trading.
        pos.available_qty += new_shares
        # A working sell order may still reserve more shares than the position
        # now holds (possible when the position was reduced by something else
        # first). Clamping ``frozen_qty`` alone would satisfy the share identity
        # while leaving the order claiming shares that do not exist, and the next
        # snapshot refuses to load that shape. Release the claim on the order.
        if pos.frozen_qty > pos.total_qty and oms is not None:
            oms.release_share_excess(account, self.symbol)
        pos.frozen_qty = min(pos.frozen_qty, pos.total_qty)

        account.cash_available += cash
        account.cash_withdrawable += cash
        account.dividend_income += cash

        return {
            "symbol": self.symbol,
            "applied": True,
            "old_qty": old_qty,
            "new_qty": new_qty,
            "new_shares": new_shares,
            "cash": str(cash),
            "new_avg_cost": str(pos.avg_cost),
        }


@dataclass(frozen=True)
class CBMaturity:
    """A convertible reaching its last trading day.

    A bond that is redeemed does not "keep trading at the last close": the
    position stops existing and the holder receives par (plus a final coupon,
    which this desk does not model — see below). Leaving it marked at the last
    print would carry a delisted instrument forever and value it at a price
    nobody can transact at.

    **What is modelled**: principal at par on the delisting session.
    **What is not**: the final coupon, the 20% interest tax, and voluntary
    conversion into the underlying. A forced-redemption (强赎) announcement is
    not in the store — the vendor's announcement feed carries titles, not a
    redemption schedule — so the trigger is the delist date, and the guard
    refuses new buys inside the last two weeks rather than pretending to know
    the call date. For the T+0 structures this desk actually tests, a bond is
    held for minutes, so this is a tail-risk guard, not a return assumption.
    """

    symbol: str
    delist_date: date
    price: Decimal = CB_PAR

    def apply(self, account: Account, *, oms: OrderManager | None = None,
              ts: datetime | None = None) -> dict:
        """Redeem at par and retire anything still pointing at the bond.

        A position zeroed while a sell order still holds ``frozen_shares`` is
        not a cosmetic mismatch: the next snapshot carries
        ``frozen_qty = 0`` beside an order reserving 10 units, and
        ``Ledger.from_snapshot`` refuses to load it ("snapshot share
        reservations do not match open orders"). The book then cannot be
        reloaded after a restart, and the failure only shows up at the worst
        time — on the next boot.

        ``oms`` and ``ts`` are therefore part of the contract, not a
        convenience: when they are given, every open order on the symbol is
        expired first (a delisted bond has no session left to trade in). A
        caller that does not own the order book gets a refusal instead of a
        silently corrupt ledger.

        *Every* open order — including buys. The guard here used to be
        ``if pos.frozen_qty > 0``, but a buy order does not touch
        ``frozen_qty`` at all: it freezes cash. So on a bond redeemed while a
        buy order was working, the only call that could release that money was
        skipped, ``cash_frozen`` kept it for good, and no later session could
        settle it because the instrument no longer exists. ``settle()``
        re-computes reservations but does not expire orders.
        """
        pos = account.peek(self.symbol)
        qty = pos.total_qty if pos is not None else 0
        expired = 0
        if oms is not None and ts is not None:
            # Lifecycle cleanup is required even with no holding: an unfilled
            # buy freezes cash without creating a position.
            expired = len(oms.expire_symbol(self.symbol, ts, account,
                                            reason="CB_MATURITY"))
        if pos is None or qty <= 0:
            return {
                "symbol": self.symbol, "applied": False, "reason": "NO_POSITION",
                "expired_orders": expired,
            }
        if pos.frozen_qty > 0:
            if oms is None or ts is None:
                return {
                    "symbol": self.symbol,
                    "applied": False,
                    "reason": "OPEN_SELL_ORDERS",
                    "frozen_qty": pos.frozen_qty,
                }
            # Expiry releases what it reserved; anything left means the
            # books disagreed before this call. Refuse rather than zero a
            # position somebody still has a claim on.
            return {
                "symbol": self.symbol,
                "applied": False,
                "reason": "FROZEN_SHARES_REMAIN",
                "frozen_qty": pos.frozen_qty,
            }
        if oms is not None:
            # Same rule for cash: if any order on this symbol is still open,
            # expiry could not release it, and zeroing the position would hide
            # the claim instead of answering it.
            stranded = [o for o in oms.open_orders() if o.symbol == self.symbol]
            if stranded:
                return {
                    "symbol": self.symbol,
                    "applied": False,
                    "reason": "OPEN_ORDERS_REMAIN",
                    "order_ids": [o.order_id for o in stranded],
                }
        proceeds = money(D(qty) * self.price)
        realized = money((self.price - pos.avg_cost) * D(qty))
        account.cash_available += proceeds
        account.cash_withdrawable += proceeds
        account.realized_pnl += realized
        pos.realized_pnl += realized
        pos.total_qty = 0
        pos.available_qty = 0
        pos.frozen_qty = 0
        # Redemption clears the T+1 lock too: the position stops existing, and a
        # lock left behind would break total == available + frozen + locked.
        pos.locked_qty = 0
        pos.avg_cost = D("0")
        return {
            "symbol": self.symbol,
            "kind": "cb_maturity",
            "applied": True,
            "qty": qty,
            "price": str(self.price),
            "cash": str(proceeds),
            "realized": str(realized),
            "expired_orders": expired,
            "reason": "redeemed at par on the delisting session",
        }


@dataclass(frozen=True)
class CBCoupon:
    """One interest payment on a convertible, net of withholding.

    中国个人投资者持有债券的利息按 20% 代扣个人所得税，所以**净额**才是能用的
    现金；按面值乘票面利率入账会高估 25%。

    The rate must come from the period that is actually ending, not from
    ``coupon_rate``: most convertibles step up (0.2%, 0.4%, ... 2.0%), and
    ``coupon_rate`` carries only the final period. Pricing every coupon off it
    would overstate the early years and understate the last one.
    """

    symbol: str
    pay_date: date
    par: Decimal
    rate: Decimal
    tax_rate: Decimal = field(default_factory=_withholding)

    def gross(self, qty: int) -> Decimal:
        return money(self.par * self.rate * D(qty))

    def net(self, qty: int) -> Decimal:
        return money(self.gross(qty) * (D("1") - self.tax_rate))

    def apply(self, account: Account, *, oms=None) -> dict:
        pos = account.peek(self.symbol)
        if pos is None or pos.total_qty <= 0:
            return {"symbol": self.symbol, "applied": False, "reason": "no position"}
        gross = self.gross(pos.total_qty)
        cash = self.net(pos.total_qty)
        # Interest is income, not a return of capital: average cost is untouched.
        # A dividend lowers cost because the share price drops by the same
        # amount on the ex-date; a coupon does not touch the bond's clean price.
        account.cash_available += cash
        account.cash_withdrawable += cash
        account.dividend_income += cash
        return {
            "symbol": self.symbol,
            "applied": True,
            "kind": "coupon",
            "qty": pos.total_qty,
            "rate": str(self.rate),
            "gross": str(gross),
            "tax": str(gross - cash),
            "cash": str(cash),
        }
