"""Order management: validation, freezing, idempotency, state transitions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from .calendar import TradingCalendar
from .costs import CostModel
from .models import (
    Account,
    Bar,
    D,
    Fill,
    Instrument,
    Intent,
    Order,
    OrderStatus,
    OrderType,
    RejectReason,
    Session,
    Side,
    integral_qty,
    money,
)
from .units import COST_BASIS_QUANT

#: Snapshot schema. Bump it when a field's *meaning* changes, not when one is
#: added: ``from_snapshot`` reads the version and branches, instead of inferring
#: the era from which keys happen to be present. Version 1 is the unlabelled
#: shape — ``locked_qty`` and ``order_records`` may be absent.
SNAPSHOT_VERSION = 2


def _buy_buffer() -> Decimal:
    """Cash reserved above the estimated notional for a market buy.

    It lived as ``D("1.02")`` here and again in ``risk.check``: two copies of one
    assumption, so a 20%-band stock and a 5%-band ST stock were buffered by the
    same 2% nobody could tune.
    """
    from service import settings

    return Decimal(str(settings.num("risk.market_buy_buffer")))


def _session_key(value: str | date | None) -> str | None:
    """``YYYYMMDD`` for a session date, or ``None`` when the caller gave none."""
    if value is None:
        return None
    if isinstance(value, date):          # datetime is a date; both format the same
        return value.strftime("%Y%m%d")
    digits = str(value).strip().replace("-", "").replace("/", "")[:8]
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"settle needs a trading day, got {value!r}")
    return digits


@dataclass(frozen=True)
class CancelResult:
    success: bool
    order_id: str
    reason: RejectReason = RejectReason.OK
    detail: str = ""


class FillRejected(ValueError):
    """A fill that failed validation, with a stable code.

    The sentence used to be the only channel: ``book`` wrote
    ``FILL_ERROR:{sentence}`` into the ticket, so "why did this bounce" could
    only be counted by parsing English, and renaming a phrase broke the count.
    ``code`` is the machine half, ``detail`` keeps the numbers, and the message
    carries both so existing readers and ``match=`` assertions still work.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class OrderManager:
    def __init__(self, cost_model: CostModel | None = None) -> None:
        self.cost_model = cost_model or CostModel()
        self.orders: dict[str, Order] = {}
        self.client_index: dict[str, Order] = {}
        self.fills: dict[str, Fill] = {}
        # An index of the live subset. ``open_orders()`` used to filter the whole
        # history on every call, and it is called from ``settle``, ``expire_ioc``
        # and every ``_release`` — so a book with a year of history paid
        # O(orders) per session and O(orders²) per year to answer "what is open".
        self._open: dict[str, Order] = {}
        self._seq = 0
        #: The session this manager last settled. ``settle`` is the only call
        #: that turns today's purchases into sellable inventory, so "which day
        #: did this rollover open" has to be part of its identity.
        self._settled_session: str | None = None

    @staticmethod
    def _intent_fingerprint(intent: Intent) -> str:
        payload = {
            "symbol": intent.symbol,
            "side": intent.side.value,
            # ``qty`` may still be a non-int here: ``submit`` computes the
            # fingerprint before it validates the quantity, and ``Intent``'s own
            # normaliser only rewrites *integral* Decimals. A fractional Decimal
            # then reached ``json.dumps`` and raised TypeError — the crash
            # ``Intent.__post_init__`` documents as fixed, still reachable from
            # the one path that never looked. Ints keep their JSON form, so the
            # fingerprint of every valid intent is unchanged and a retry after a
            # restart still matches; anything else is encoded as its string so it
            # can be recorded as a rejection instead of becoming a 500.
            "qty": (intent.qty if isinstance(intent.qty, int)
                    else None if intent.qty is None else str(intent.qty)),
            "target_weight": None if intent.target_weight is None else str(intent.target_weight),
            "order_type": intent.order_type.value,
            "limit_price": None if intent.limit_price is None else str(intent.limit_price),
            "reason": intent.reason,
            "sleeve": intent.sleeve,
            "stop_price": None if intent.stop_price is None else str(intent.stop_price),
            "max_loss": None if intent.max_loss is None else str(intent.max_loss),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _positive_finite(value: Decimal | None) -> bool:
        return value is not None and value.is_finite() and value > 0

    @property
    def seq(self) -> int:
        """Last order number handed out.

        It is part of the ledger's persisted state: an order id that restarts at
        ``O00000001`` after a process restart collides with the audit row of the
        first order ever sent, because ``orders`` is keyed by ``order_id``.
        """
        return self._seq

    @seq.setter
    def seq(self, value: int) -> None:
        self._seq = max(0, int(value))

    def _new_order_id(self) -> str:
        self._seq += 1
        return f"O{self._seq:08d}"

    def submit(
        self,
        intent: Intent,
        ts: datetime,
        account: Account,
        instrument: Instrument,
        bar: Bar,
        calendar: TradingCalendar,
    ) -> Order:
        fingerprint = self._intent_fingerprint(intent)
        client_id = intent.client_order_id or f"auto-{ts.isoformat()}-{fingerprint}"
        if client_id in self.client_index:
            existing = self.client_index[client_id]
            if existing.intent_fingerprint and existing.intent_fingerprint != fingerprint:
                raise ValueError("client_order_id reused with a different intent")
            if existing.status is not OrderStatus.REJECTED:
                return existing
            # A rejection never reached the market and froze nothing, so the
            # same handle may be retried with the conditions fixed (a market
            # that was closed at 20:00 is open at 10:00). Returning the stale
            # REJECTED order made that retry impossible to express.
            #
            # The old row is dropped from the *live* indexes — not from the
            # ``orders`` table — because ``Ledger.from_snapshot`` refuses a
            # snapshot carrying two orders with one client id, and the audit
            # record is the table's job.
            self.orders.pop(existing.order_id, None)
            self.client_index.pop(client_id, None)
            self._open.pop(existing.order_id, None)

        session = calendar.session_at(ts)
        if not calendar.can_submit(ts):
            return self._reject(client_id, intent, ts, RejectReason.MARKET_CLOSED, session,
                                fingerprint)

        if bar.suspended:
            return self._reject(client_id, intent, ts, RejectReason.SUSPENDED, session,
                                fingerprint)
        # ST 只拦买入。卖出必须永远放行——见 ``kernel.risk._gate`` 里的同一段
        # 说明：曾经这里对买卖一律拒绝，于是股票被 ST 之后持仓卖不掉，
        # 止损单和清仓单一律 ST 拒绝。
        if instrument.is_st and intent.side == Side.BUY:
            return self._reject(client_id, intent, ts, RejectReason.ST, session, fingerprint)
        if intent.order_type == OrderType.LIMIT:
            limit = intent.limit_price
            tick = instrument.price_tick
            up_limit, down_limit = bar.limits(instrument)
            if not self._positive_finite(limit):
                return self._reject(client_id, intent, ts, RejectReason.PRICE_LIMIT, session,
                                    fingerprint)
            if (not self._positive_finite(tick)
                    or (limit / tick) != (limit / tick).to_integral_value()):
                return self._reject(client_id, intent, ts, RejectReason.PRICE_LIMIT, session,
                                    fingerprint)
            # A band the bar did not carry is derived from the instrument, so a
            # limit order is not refused merely because the feed left the columns
            # empty. A band that cannot be established at all is still a refusal:
            # without one there is no way to say the price is legal.
            if (not self._positive_finite(down_limit) or not self._positive_finite(up_limit)
                    or limit < down_limit or limit > up_limit):
                return self._reject(client_id, intent, ts, RejectReason.PRICE_LIMIT, session,
                                    fingerprint)

        qty = intent.qty
        if qty is None and intent.target_weight is not None:
            price = bar.open if self._positive_finite(bar.open) else bar.pre_close
            if (not self._positive_finite(price) or not intent.target_weight.is_finite()
                    or intent.target_weight < 0):
                return self._reject(client_id, intent, ts, RejectReason.BAD_QTY, session,
                                    fingerprint)
            # ``target_weight`` is a **target**, not an order size: asking for
            # 30% of a name already held at 30% used to buy another 30% (a
            # measured 30% position became 60.6%). The trade is the distance
            # from where the position is to where the caller wants it, valued at
            # the same reference price used for sizing.
            equity = account.equity({intent.symbol: price})
            held = account.peek(intent.symbol)
            held_value = (money(D(held.total_qty) * price)
                          if held is not None and held.total_qty > 0 else D("0"))
            target_value = money(equity * intent.target_weight)
            if intent.side == Side.BUY:
                delta = target_value - held_value
            else:
                delta = held_value - target_value
            qty = int(delta / price) if delta > 0 else 0
        if qty is None or qty <= 0:
            return self._reject(client_id, intent, ts, RejectReason.BAD_QTY, session,
                                fingerprint)
        # ``qty`` is a share count: 100.5 means the caller divided money by a
        # price and forgot to floor it. Integral floats are accepted because a
        # JSON round trip may hand one back; fractions are not.
        integral = integral_qty(qty)
        if integral is None:
            return self._reject(client_id, intent, ts, RejectReason.BAD_QTY, session,
                                fingerprint)
        qty = integral
        if instrument.t_plus > 1:
            # The settlement rule below is a two-state model: available now (T+0)
            # or available after the next ``settle()`` (T+1). Silently treating a
            # T+2 instrument as T+1 would lend shares that are not yet deliverable.
            raise ValueError(
                f"{instrument.symbol}: t_plus={instrument.t_plus} is not modelled; "
                "the kernel settles T+0 and T+1 only"
            )

        if intent.side == Side.BUY:
            qty = instrument.buy_lot(qty)
            if qty <= 0:
                return self._reject(client_id, intent, ts, RejectReason.BAD_LOT, session,
                                    fingerprint)
        else:
            pos = account.peek(intent.symbol)
            # ``qty`` from here on, not ``intent.qty``: a target_weight intent has
            # no qty at all — that is the branch condition above — so reading
            # intent.qty again rejected every weight-sized sell with BAD_QTY.
            if pos is None:
                return self._reject(client_id, intent, ts,
                                    RejectReason.INSUFFICIENT_SHARES, session, fingerprint)
            # Two different questions used to share one answer. "You do not own
            # that many" is final; "you own them but cannot deliver them today"
            # is a wait. Reporting the second for the first told the caller to
            # retry tomorrow, which fails identically forever — the sell of 500
            # shares against a 100-share position came back as T+1.
            if qty > pos.total_qty:
                return self._reject(client_id, intent, ts,
                                    RejectReason.INSUFFICIENT_SHARES, session, fingerprint)
            if qty > pos.available_qty:
                # The shares exist but are not deliverable: either bought today
                # (T+1) or already reserved by another sell order.
                return self._reject(client_id, intent, ts, RejectReason.T_PLUS_ONE,
                                    session, fingerprint)
            qty = instrument.sell_lot(qty, pos.available_qty)
            if qty <= 0:
                return self._reject(client_id, intent, ts, RejectReason.BAD_LOT, session,
                                    fingerprint)

        price = (intent.limit_price if intent.order_type == OrderType.LIMIT
                 else bar.open if self._positive_finite(bar.open) else bar.pre_close)
        if not self._positive_finite(price):
            return self._reject(client_id, intent, ts, RejectReason.PRICE_LIMIT, session,
                                fingerprint)
        amount = money(D(qty) * price)
        if intent.side == Side.BUY:
            fees = self.cost_model.buy_fees(amount, instrument)
            freeze_amount = (
                amount if intent.order_type == OrderType.LIMIT
                else money(amount * (D("1") + _buy_buffer()))
            )
            freeze = money(freeze_amount + fees)
            if account.cash_available < freeze:
                return self._reject(client_id, intent, ts, RejectReason.INSUFFICIENT_CASH,
                                    session, fingerprint)
            account.cash_available -= freeze
            account.cash_frozen += freeze
            # The two cash pools must move together. Reserved cash is not
            # withdrawable cash, so the withdrawable pool gives up whatever part
            # of the freeze it covered — and the order remembers how much that
            # was, because the release has to put exactly that much back.
            #
            # It used to only clamp the balance to the available one, and the
            # release added the whole returned amount back. Between them the
            # withdrawable balance grew every time a T+1-restricted sale funded
            # a buy: measured 2026-11-02, spending 2,005 元 of a 5,000 元
            # account whose withdrawable pool was 1,000 元 left the withdrawable
            # balance at 1,040.02 — more than it started with.
            taken = money(min(account.cash_withdrawable, freeze))
            account.cash_withdrawable = money(account.cash_withdrawable - taken)
            frozen_withdrawable = taken
            frozen_cash = freeze
            frozen_shares = 0
        else:
            pos = account.peek(intent.symbol)
            pos.available_qty -= qty
            pos.frozen_qty += qty
            frozen_cash = D("0")
            frozen_withdrawable = D("0")
            frozen_shares = qty

        order = Order(
            order_id=self._new_order_id(),
            client_order_id=client_id,
            symbol=intent.symbol,
            side=intent.side,
            qty=qty,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            status=OrderStatus.NEW,
            created_ts=ts,
            updated_ts=ts,
            session=session,
            frozen_cash=frozen_cash,
            frozen_shares=frozen_shares,
            frozen_withdrawable=frozen_withdrawable,
            sleeve=intent.sleeve,
            intent_fingerprint=fingerprint,
            t_plus=instrument.t_plus,
        )
        return self.register(order)

    def _reject(
        self,
        client_id: str,
        intent: Intent,
        ts: datetime,
        reason: RejectReason,
        session,
        fingerprint: str | None = None,
    ) -> Order:
        order = Order(
            order_id=self._new_order_id(),
            client_order_id=client_id,
            symbol=intent.symbol,
            side=intent.side,
            # `qty` is a share count everywhere else; a Decimal or a float
            # reaching the snapshot would make `json.dumps` fail on the *next*
            # write, far from the rejection that produced it.
            qty=integral_qty(intent.qty) or 0,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            status=OrderStatus.REJECTED,
            created_ts=ts,
            updated_ts=ts,
            reason=reason.value,
            session=session,
            sleeve=intent.sleeve,
            intent_fingerprint=fingerprint or self._intent_fingerprint(intent),
            t_plus=1,
        )
        return self.register(order)

    def register(self, order: Order) -> Order:
        """Put an order into every index it belongs to, including the live one.

        One door, because ``Ledger.from_snapshot`` restores orders too and must
        not end up with a history that disagrees with ``open_orders()``.
        """
        self.orders[order.order_id] = order
        self.client_index[order.client_order_id] = order
        if order.is_open:
            self._open[order.order_id] = order
        else:
            self._open.pop(order.order_id, None)
        return order

    def cancel(
        self,
        order_id: str,
        ts: datetime,
        account: Account,
        calendar: TradingCalendar,
        reason: str = "CANCELLED",
    ) -> CancelResult:
        """Retire an open order inside the exchange's cancellation window.

        ``reason`` is carried into the order record instead of the generic
        ``CANCELLED``. Callers retire an order *for a reason* — "the queued ticket
        only got a quarter filled", "the board opened and the chase is off" — and
        that sentence used to be discarded here, surviving only when the cancel
        window was shut and the caller fell back to ``expire``. The audit trail
        kept the mechanism and lost the reason.
        """
        order = self.orders.get(order_id)
        if order is None:
            return CancelResult(False, order_id, RejectReason.UNKNOWN, "order not found")
        if not order.is_open:
            return CancelResult(False, order_id, RejectReason.ALREADY_FILLED, order.status.value)
        if not calendar.can_cancel(ts):
            return CancelResult(False, order_id, RejectReason.CANCEL_NOT_ALLOWED, "window closed")

        self._release(order, account)
        order.status = OrderStatus.CANCELLED
        order.updated_ts = ts
        order.reason = reason
        self._open.pop(order.order_id, None)
        return CancelResult(True, order_id, RejectReason.OK, "cancelled")

    def expire(self, order_id: str, ts: datetime, account: Account,
               reason: str = "EXPIRED_AT_CLOSE") -> CancelResult:
        """Retire an order that is no longer valid.

        Two cases, one mechanism: a day order at the close (A-share orders live
        for one session), and a market order that did not fill on the bar it was
        sent to (it must not be repriced off tomorrow's open).
        """
        order = self.orders.get(order_id)
        if order is None:
            return CancelResult(False, order_id, RejectReason.UNKNOWN, "order not found")
        if not order.is_open:
            return CancelResult(False, order_id, RejectReason.ALREADY_FILLED, order.status.value)

        self._release(order, account)
        order.status = OrderStatus.EXPIRED
        order.updated_ts = ts
        order.reason = reason
        self._open.pop(order.order_id, None)
        return CancelResult(True, order_id, RejectReason.OK, "expired")

    def expire_open_orders(self, ts: datetime, account: Account) -> list[Order]:
        """Expire everything still open at ``ts`` and return what was expired."""
        expired: list[Order] = []
        for order in self.open_orders():
            result = self.expire(order.order_id, ts, account)
            if result.success:
                expired.append(order)
        return expired

    def expire_symbol(self, symbol: str, ts: datetime, account: Account,
                      reason: str = "SYMBOL_GONE") -> list[Order]:
        """Retire every open order on ``symbol`` and release what it reserved.

        For an instrument that stops existing — a convertible redeemed at par,
        a delisting — an open sell order is not "still working": there is
        nothing left to sell and no session left to sell it in. Leaving it open
        strands its ``frozen_shares`` on an order whose position is about to be
        zeroed, and the very next snapshot fails its own consistency check
        (``snapshot share reservations do not match open orders``), so the book
        cannot be reloaded after a restart. ``settle()`` repairs that state, but
        only at a session boundary — a restart between the redemption and the
        close reads the broken snapshot first.
        """
        expired: list[Order] = []
        for order in self.open_orders():
            if order.symbol != symbol:
                continue
            result = self.expire(order.order_id, ts, account, reason=reason)
            if result.success:
                expired.append(order)
        return expired

    @staticmethod
    def _release(order: Order, account: Account) -> None:
        """Return whatever the order had frozen, so expiry leaves no phantom cash."""
        if order.side == Side.BUY:
            release = min(order.frozen_cash, account.cash_frozen)
            account.cash_frozen -= release
            account.cash_available += release
            # Only the part of the freeze that came out of the withdrawable pool
            # goes back into it. Adding the whole released amount would hand
            # back T+1-restricted cash as if it were withdrawable — the mirror
            # image of the bug where freezing did not take from that pool.
            returned = money(min(order.frozen_withdrawable, release))
            account.cash_withdrawable = money(
                min(account.cash_withdrawable + returned, account.cash_available))
            # Release what was actually returned, not the whole reservation. The
            # ``min`` above exists because the two can disagree; zeroing the
            # reservation while releasing less would make the difference
            # disappear from both sides instead of surfacing as a breach.
            order.frozen_cash = money(order.frozen_cash - release)
            order.frozen_withdrawable = money(order.frozen_withdrawable - returned)
        else:
            pos = account.peek(order.symbol)
            qty = min(order.frozen_shares, pos.frozen_qty) if pos is not None else 0
            if pos is not None:
                pos.frozen_qty -= qty
                pos.available_qty += qty
            order.frozen_shares -= qty

    def apply_fill(self, order: Order, fill: Fill, account: Account) -> bool:
        """Validate and precompute a fill, then commit every field together.

        The order-level minimum commission is charged by ``BarExecutionModel`` on
        the first fill; later fills carry only the incremental cumulative fee.
        A duplicate id is a no-op only when the complete fill payload is equal.
        """
        previous = self.fills.get(fill.fill_id)
        if previous is not None:
            if previous != fill:
                raise FillRejected("FILL_ID_REUSED", "fill_id reused with a different payload")
            return False
        if fill.order_id != order.order_id or self.orders.get(order.order_id) is not order:
            raise FillRejected("FILL_ORDER_MISMATCH", "fill order_id does not match the managed order")
        if not order.is_open:
            raise FillRejected("FILL_ORDER_NOT_OPEN", f"order is not open: {order.status.value}")
        if fill.symbol != order.symbol or fill.side != order.side:
            raise FillRejected("FILL_SIDE_MISMATCH", "fill symbol/side does not match order")
        if not isinstance(fill.qty, int) or isinstance(fill.qty, bool) or fill.qty <= 0:
            raise FillRejected("FILL_QTY_INVALID", "fill qty must be a positive integer")
        if fill.qty > order.remaining_qty:
            raise FillRejected("FILL_QTY_EXCEEDS", "fill qty exceeds remaining")
        if not self._positive_finite(fill.price):
            raise FillRejected("FILL_PRICE_INVALID", "fill price must be finite and positive")
        if not fill.fees.is_finite() or fill.fees < 0:
            raise FillRejected("FILL_FEES_INVALID", "fill fees must be finite and non-negative")
        if order.created_ts is not None and fill.ts < order.created_ts:
            # A fill cannot precede the order that produced it. Accepting one
            # would let a replay write history backwards and still reconcile.
            raise FillRejected("FILL_TS_PRECEDES_ORDER", "fill timestamp precedes the order")
        if order.updated_ts is not None and fill.ts < order.updated_ts:
            raise FillRejected("FILL_TS_PRECEDES_ORDER", "fill timestamp precedes the order's last update")

        gross = money(D(fill.qty) * fill.price)
        new_filled_qty = order.filled_qty + fill.qty
        new_notional = money(order.filled_notional + gross)
        # Half-up, like every other money rounding here. The default for
        # ``quantize`` is banker's rounding, which made a 0.005 average cost
        # depend on whether the digit before it was even.
        new_avg_fill = (new_notional / D(new_filled_qty)).quantize(
            COST_BASIS_QUANT, rounding=ROUND_HALF_UP
        )
        new_status = (OrderStatus.FILLED if new_filled_qty == order.qty
                      else OrderStatus.PARTIALLY_FILLED)

        if order.side == Side.BUY:
            cost = money(gross + fill.fees)
            proportional = (
                order.frozen_cash if fill.qty == order.remaining_qty
                else money(order.frozen_cash * D(fill.qty) / D(order.remaining_qty))
            )
            # The first partial may carry the whole minimum commission. Release
            # enough of the existing reserve for it without touching other cash.
            release = min(order.frozen_cash, max(proportional, cost))
            new_cash_available = money(account.cash_available + release - cost)
            new_cash_frozen = money(account.cash_frozen - release)
            if release < 0 or order.frozen_cash < release or new_cash_frozen < 0:
                raise FillRejected("FILL_CASH_RESERVATION", "cash reservation is inconsistent")
            if new_cash_available < 0:
                raise FillRejected("FILL_CASH_SHORTFALL", "cash shortfall after fill")
            # ``submit`` already removed frozen_withdrawable from the account's
            # withdrawable pool. A fill spends that reserve; it must not debit the
            # pool a second time. Only an unused, released slice returns here.
            # Track the spent slice on the order so a later partial fill or cancel
            # still knows which part of the remaining reserve was withdrawable.
            spent_from_withdrawable = money(min(order.frozen_withdrawable, cost))
            returned_withdrawable = money(min(
                order.frozen_withdrawable - spent_from_withdrawable,
                max(D("0"), release - cost),
            ))
            new_withdrawable = max(
                D("0"),
                min(account.cash_withdrawable + returned_withdrawable,
                    new_cash_available),
            )
            new_frozen_withdrawable = money(
                order.frozen_withdrawable - spent_from_withdrawable - returned_withdrawable)
            pos = account.positions.get(fill.symbol)
            old_qty = pos.total_qty if pos is not None else 0
            old_cost = pos.avg_cost if pos is not None else D("0")
            old_available = pos.available_qty if pos is not None else 0
            old_locked = pos.locked_qty if pos is not None else 0
            new_qty = old_qty + fill.qty
            new_avg_cost = ((old_cost * D(old_qty) + cost) / D(new_qty)).quantize(
                COST_BASIS_QUANT, rounding=ROUND_HALF_UP
            )
        else:
            proceeds = money(gross - fill.fees)
            if proceeds < 0:
                raise FillRejected("FILL_FEES_EXCEED_PROCEEDS", "fill fees exceed sale proceeds")
            pos = account.positions.get(fill.symbol)
            if pos is None or pos.total_qty < fill.qty or pos.frozen_qty < fill.qty:
                raise FillRejected("FILL_SHARE_RESERVATION", "share reservation is inconsistent")
            if order.frozen_shares < fill.qty:
                raise FillRejected("FILL_ORDER_SHARE_RESERVATION", "order share reservation is inconsistent")
            new_cash_available = money(account.cash_available + proceeds)
            new_withdrawable = min(account.cash_withdrawable, new_cash_available)
            realized = money((fill.price - pos.avg_cost) * D(fill.qty) - fill.fees)

        # Commit only after every validation and Decimal operation has succeeded.
        if order.side == Side.BUY:
            account.cash_available = new_cash_available
            account.cash_frozen = new_cash_frozen
            account.cash_withdrawable = new_withdrawable
            pos = account.position(fill.symbol)
            if old_qty == 0:
                pos.sleeve = order.sleeve
            pos.total_qty = new_qty
            if order.t_plus == 0:
                pos.available_qty = old_available + fill.qty
                pos.locked_qty = old_locked
            else:
                # Deliverable only after the next ``settle()``. Recorded rather
                # than implied: the identity total == available + frozen + locked
                # is what a restart and a reconcile can actually check.
                pos.available_qty = old_available
                pos.locked_qty = old_locked + fill.qty
            pos.avg_cost = new_avg_cost
            pos.last_price = fill.price
            order.frozen_cash = money(order.frozen_cash - release)
            order.frozen_withdrawable = new_frozen_withdrawable
        else:
            account.cash_available = new_cash_available
            account.cash_withdrawable = new_withdrawable
            pos.realized_pnl += realized
            account.realized_pnl += realized
            pos.total_qty -= fill.qty
            pos.frozen_qty -= fill.qty
            pos.last_price = fill.price
            order.frozen_shares -= fill.qty
            if pos.total_qty == 0:
                pos.avg_cost = D("0")

        account.fees_paid += fill.fees
        order.fees_paid += fill.fees
        order.filled_qty = new_filled_qty
        order.filled_notional = new_notional
        order.avg_fill_price = new_avg_fill
        order.updated_ts = fill.ts
        order.status = new_status
        self.fills[fill.fill_id] = fill
        if not order.is_open:
            self._open.pop(order.order_id, None)
        return True

    def release_share_excess(self, account: Account, symbol: str) -> int:
        """Shrink open sell orders on ``symbol`` so the claim never exceeds the holding.

        Used by corporate actions that can *reduce* a position while a sell
        order is still working. ``settle`` does the same reconciliation for the
        whole book, but it also clears the T+1 lock — which is correct once per
        session and wrong in the middle of one. Returns the shares released.
        """
        pos = account.peek(symbol)
        if pos is None:
            return 0
        excess = int(pos.frozen_qty) - int(pos.total_qty)
        if excess <= 0:
            return 0
        released = 0
        for order in self.open_orders():
            if excess <= 0:
                break
            if order.symbol != symbol or order.frozen_shares <= 0:
                continue
            take = min(int(order.frozen_shares), excess)
            order.frozen_shares -= take
            pos.frozen_qty -= take
            excess -= take
            released += take
        return released

    def settle(self, account: Account, session: str | date | None = None) -> None:
        """Session rollover: unlock T+1 inventory and re-derive reservations.

        Called once per session boundary (``PaperBook.rollover``, and the engine
        at 15:00). Two jobs, both of them "trust the orders, not the counters":

        * shares bought yesterday become deliverable today;
        * ``cash_frozen``/``frozen_qty`` are recomputed from the *live* orders, so
          a crash between a fill and a snapshot cannot leave a phantom freeze.

        T+0 instruments (``order.t_plus == 0``) were credited at fill time, so
        this is a no-op for them. The model covers T+0 and T+1 because that is
        what A-shares have; ``submit`` refuses a ``t_plus`` above 1 rather than
        settling it as if it were T+1.

        **Pass the session this rollover opens.** ``settle`` is the only call in
        the kernel that turns shares bought today into shares that can be sold
        today, so a second call inside one session is not a harmless repeat: it
        mints deliverable inventory, and every check downstream will accept the
        sell.         ``session`` makes the call idempotent and refuses to move
        backwards. Callers without a calendar — unit fixtures, the fuzz harness —
        may omit it and get the old unconditional behaviour; every production
        path passes the day it is opening.
        """
        key = _session_key(session)
        if key is not None:
            if self._settled_session is not None:
                if key < self._settled_session:
                    raise ValueError(
                        f"settle cannot move backwards: {key} < {self._settled_session}")
                if key == self._settled_session:
                    return
            self._settled_session = key
        orders = self.open_orders()
        frozen_shares: dict[str, int] = {}
        sells_by_symbol: dict[str, list[Order]] = {}
        cash_frozen = D("0")
        for order in orders:
            if order.side == Side.BUY:
                cash_frozen += order.frozen_cash
            else:
                frozen_shares[order.symbol] = (
                    frozen_shares.get(order.symbol, 0) + order.frozen_shares
                )
                if order.frozen_shares > 0:
                    sells_by_symbol.setdefault(order.symbol, []).append(order)
        # A reservation can outlive the shares it reserved: a convertible bond
        # that matures while a sell order is still open has its position zeroed
        # by the corporate action, and the order keeps `frozen_shares = 100`.
        # The old line was `max(0, total - frozen)`, which silently hid the
        # breach — `available` became 0, `frozen_qty` stayed 100, and
        # `is_consistent()` returned False for the rest of the book's life while
        # the desk kept trading. Release the excess on the order instead, so both
        # sides of the invariant move together; the order stays open and will
        # expire at the close, which is what a sell with no shares deserves.
        #
        # The inner loop used to re-scan every open order for every symbol with
        # an excess, calling ``open_orders()`` (which copies the live index) once
        # per symbol. One pass builds the per-symbol index; the result is the
        # same list of orders, in the same insertion order.
        for symbol, reserved in list(frozen_shares.items()):
            pos = account.peek(symbol)
            held = int(pos.total_qty) if pos is not None else 0
            excess = reserved - held
            if excess <= 0:
                continue
            for order in sells_by_symbol.get(symbol, ()):
                if excess <= 0:
                    break
                take = min(int(order.frozen_shares), excess)
                order.frozen_shares -= take
                excess -= take
            frozen_shares[symbol] = held

        for symbol, pos in account.positions.items():
            pos.frozen_qty = frozen_shares.get(symbol, 0)
            pos.locked_qty = 0
            pos.available_qty = max(0, pos.total_qty - pos.frozen_qty)
        account.cash_withdrawable = account.cash_available
        account.cash_frozen = money(cash_frozen)

    def open_orders(self) -> list[Order]:
        """Live orders only, from the index rather than by scanning history."""
        return list(self._open.values())

    @staticmethod
    def _timestamp(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    @staticmethod
    def order_to_dict(order: Order) -> dict:
        return {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "qty": order.qty,
            "order_type": order.order_type.value,
            "limit_price": None if order.limit_price is None else str(order.limit_price),
            "status": order.status.value,
            "filled_qty": order.filled_qty,
            "avg_fill_price": str(order.avg_fill_price),
            "created_ts": order.created_ts.isoformat() if order.created_ts else None,
            "updated_ts": order.updated_ts.isoformat() if order.updated_ts else None,
            "reason": order.reason,
            "session": order.session.value,
            "frozen_cash": str(order.frozen_cash),
            "frozen_shares": order.frozen_shares,
            "sleeve": order.sleeve,
            "intent_fingerprint": order.intent_fingerprint,
            "t_plus": order.t_plus,
            "fees_paid": str(order.fees_paid),
            "filled_notional": str(order.filled_notional),
            "frozen_withdrawable": str(order.frozen_withdrawable),
            "last_matched_bar_ts": (
                order.last_matched_bar_ts.isoformat() if order.last_matched_bar_ts else None
            ),
        }

    @classmethod
    def order_from_dict(cls, row: dict) -> Order:
        return Order(
            order_id=row["order_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=Side(row["side"]),
            qty=int(row["qty"]),
            order_type=OrderType(row.get("order_type", OrderType.MARKET.value)),
            limit_price=(None if row.get("limit_price") is None
                         else D(row["limit_price"])),
            status=OrderStatus(row["status"]),
            filled_qty=int(row.get("filled_qty", 0)),
            avg_fill_price=D(row.get("avg_fill_price", "0")),
            created_ts=cls._timestamp(row.get("created_ts")),
            updated_ts=cls._timestamp(row.get("updated_ts")),
            reason=row.get("reason", ""),
            session=Session(row.get("session", Session.CONTINUOUS.value)),
            frozen_cash=D(row.get("frozen_cash", "0")),
            frozen_shares=int(row.get("frozen_shares", 0)),
            sleeve=row.get("sleeve", "CORE"),
            intent_fingerprint=row.get("intent_fingerprint", ""),
            t_plus=int(row.get("t_plus", 1)),
            fees_paid=D(row.get("fees_paid", "0")),
            filled_notional=D(row.get("filled_notional", "0")),
            frozen_withdrawable=D(row.get("frozen_withdrawable", "0")),
            last_matched_bar_ts=cls._timestamp(row.get("last_matched_bar_ts")),
        )

    @staticmethod
    def fill_to_dict(fill: Fill) -> dict:
        return {
            "fill_id": fill.fill_id,
            "order_id": fill.order_id,
            "symbol": fill.symbol,
            "side": fill.side.value,
            "qty": fill.qty,
            "price": str(fill.price),
            "fees": str(fill.fees),
            "ts": fill.ts.isoformat(),
            "rule_id": fill.rule_id,
            "ref_price": None if fill.ref_price is None else str(fill.ref_price),
            "limit_up_penalty": str(fill.limit_up_penalty),
        }

    @staticmethod
    def fill_from_dict(row: dict) -> Fill:
        return Fill(
            fill_id=row["fill_id"],
            order_id=row["order_id"],
            symbol=row["symbol"],
            side=Side(row["side"]),
            qty=int(row["qty"]),
            price=D(row["price"]),
            fees=D(row["fees"]),
            ts=datetime.fromisoformat(row["ts"]),
            rule_id=row.get("rule_id", ""),
            ref_price=None if row.get("ref_price") is None else D(row["ref_price"]),
            limit_up_penalty=D(row.get("limit_up_penalty", "0")),
        )

    def snapshot(self, account: Account) -> dict:
        # Iterate over tuples, not the live containers. FastAPI serves requests
        # on a thread pool while a fill may be running on another thread, and
        # `Account.position()` inserts a key the moment a new name is bought:
        # `dictionary changed size during iteration` is a 500, and a snapshot
        # that saw the position but not the cash it cost is worse than a 500
        # because it looks like a valid answer.
        positions = tuple(account.positions.items())
        orders = tuple(self.orders.values())
        fills = tuple(self.fills.values())
        return {
            "v": SNAPSHOT_VERSION,
            "cash_available": str(account.cash_available),
            "cash_frozen": str(account.cash_frozen),
            "cash_withdrawable": str(account.cash_withdrawable),
            "realized_pnl": str(account.realized_pnl),
            "fees_paid": str(account.fees_paid),
            "dividend_income": str(account.dividend_income),
            "peak_equity": str(account.peak_equity),
            "order_seq": self._seq,
            # Session-level idempotence is ledger state, not process state. Keep
            # the key out only for a genuinely fresh book so old fresh snapshots
            # still round-trip byte-for-byte.
            **({"settled_session": self._settled_session}
               if self._settled_session is not None else {}),
            "positions": {
                s: {
                    "total_qty": p.total_qty,
                    "available_qty": p.available_qty,
                    "frozen_qty": p.frozen_qty,
                    "locked_qty": p.locked_qty,
                    "avg_cost": str(p.avg_cost),
                    "last_price": str(p.last_price),
                    "realized_pnl": str(p.realized_pnl),
                    "sleeve": p.sleeve,
                }
                for s, p in positions
            },
            # Keep the original status map for readers of the old snapshot API.
            "orders": {o.order_id: o.status.value for o in orders},
            "order_records": [self.order_to_dict(o) for o in orders],
            "fills": [self.fill_to_dict(f) for f in fills],
        }
