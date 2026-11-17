"""Deterministic bar-based execution model for A-share paper trading.

The same model is used by backtest and paper. No wall clock, no randomness: a
fill is a pure function of the bar, the order and the cost model.

**Pricing rule (P27).** What a fill costs may only depend on what was knowable
when the order was placed. A market order sent for the open is priced off the
open; whether the stock *closed* at the limit is a fact from 15:00 and cannot
decide a 09:30 price. The earlier model priced any buy on a limit-up close at
``up_limit`` and any sell on a limit-down close at ``down_limit``, which meant a
strategy's entry cost silently depended on the rest of the day. That is a
look-ahead, and it hit exactly the stocks event strategies select.

Whether a fill can happen *at all* is a different question, and there the close
is irrelevant too: a board that is sealed at the open is not buyable, and a
limit-down open has no bid. Both are refused rather than filled optimistically.
The closing auction is held to the same standard: a board sealed at the ceiling
at 15:00 has no seller left, and one sealed at the floor has no buyer, so neither
side is filled just because a price printed.

**One place the day is still used as evidence.** A limit order on the far side of
a *historical* open (buy below it, sell above it) is filled when the bar's low/high
shows the price reached it, at the order's own limit, labelled ``+TOUCH_DAY``. The
*price* is not a look-ahead — an exchange does not give the trader a better one —
but the *fillability* is: the day's range is a 15:00 fact, and the order may have
been placed after the touch. That is the standard "day order fills if touched"
assumption for daily bars, and the label keeps it separable from an open print.

A **live** bar gets no such licence. There ``open`` is the current quote, and the
day's low/high describe prints that happened before the ticket existed, so a limit
on the far side of the quote is not marketable yet: it is refused with
``PRICE_LIMIT`` and the current quote in the detail. Before that rule existed the
matcher filled it at the limit, which handed the account the best print of the day
and a zero delay cost — a fabricated trade, and the one kind of error a track
record cannot survive.

Slippage is bounded by the order's own limit. A limit buy at 10.00 that fills at
10.01 is a trade no exchange could have printed; on a gap-through-limit day it is
also precisely the chase the desk promises not to make.

Partial fills are expressed by ``Bar.fill_ratio``. When a bar carries a ratio
below 1 for a limit-up open, the order joins the queue and gets that fraction;
without such data the model refuses instead of pretending to be first in line.

**Who supplies that ratio.** ``market.bar`` and ``live_fill.build`` both leave
``fill_ratio`` at its 1.0 default and ``seal_amount`` at ``None``. The one producer
is ``PaperBook._ceiling_ratio``, and only for tickets explicitly queued as
``queue_kind="ceiling"``: it computes ``traded / (traded + seal)`` from the
session's *closing* seal and turnover, because the depth in front of an order at
09:30 is not in any feed this desk subscribes to. That is a size assumption, not a
price one — the fill still prints at the ceiling, which was knowable when the
order was placed — and it is labelled ``ceiling_queue`` on the fill so nobody
reads it as a measured queue position. For every other path no ratio exists and a
board sealed at the open is refused rather than filled optimistically.

Because no ratio exists, *why* the ceiling blocked the order is the only thing
that can differ, and it decides what the trader does next. A historical bar's
``open`` is the 09:30 print, so a refusal there means the board opened sealed:
give up on the name for today. A live bar's ``open`` is the current quote, so a
refusal there means the board is sealed *now*: the order can still fill if it
opens, and waiting is a real choice. ``Bar.priced_at`` carries that difference and
the two get different rejection codes (``OPEN_AT_LIMIT`` / ``QUEUE_NO_PRIORITY``),
so the record shows which question was actually answered.

**Costs are per asset class.** The slippage assumption used to be a two-way
branch on ``instrument.is_etf``, which quietly gave a convertible bond the ETF's
5bps while its lot, tick, settlement and band are all its own. The table now has
one entry per ``Instrument.asset`` and comes from ``costs.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .calendar import TradingCalendar
from .costs import CostModel
from .microstructure import blocked_at_open, fill_price_at_open
from .models import (
    Bar,
    D,
    Fill,
    Instrument,
    Order,
    OrderType,
    RejectReason,
    Session,
    Side,
    money,
)
from .units import PRICE_EPS

#: Asset classes that may carry their own slippage assumption.
ASSET_CLASSES = ("stock", "etf", "cb")


@dataclass(frozen=True)
class FillResult:
    fill: Fill | None
    reject_reason: RejectReason = RejectReason.OK
    detail: str = ""

    @property
    def filled(self) -> bool:
        return self.fill is not None


def derive_fill_ratio(seal_amount: Decimal | None, traded_amount: Decimal | None) -> float | None:
    """Share of a limit-board order that gets filled, from the queue in front of it.

    ``seal_amount`` is the unfilled bid sitting at the limit price; ``traded_amount``
    is how much turnover the board absorbed. The model assumes your order sits at
    the back of the queue, so the fraction that clears is the traded amount divided
    by the total ahead of you::

        ratio = traded / (traded + seal)

    A seal four times the turnover means roughly 20% fills. This is an assumption,
    not a measurement - it exists so that a 5亿 seal and a 2千万 seal stop being the
    same event, and so the ratio has one place to live instead of being invented
    per call site.

    Returns ``None`` when either input is missing: no queue data means no ratio,
    and the caller must refuse rather than assume a full fill.
    """
    if seal_amount is None or traded_amount is None:
        return None
    seal = float(seal_amount)
    traded = float(traded_amount)
    if seal <= 0 or traded <= 0:
        return None
    return max(0.0, min(1.0, traded / (traded + seal)))


def _cost(key: str) -> float:
    from service import settings

    return settings.num(f"costs.{key}")


def _slippage_table() -> dict[str, float]:
    """One slippage assumption per asset class, from ``costs.yaml``.

    A missing key falls back to the stock number, which is the widest of the
    three — a wrong default should cost more, not less.
    """
    table: dict[str, float] = {}
    for asset in ASSET_CLASSES:
        try:
            table[asset] = _cost(f"slippage.{asset}_bps")
        except KeyError:
            continue
    fallback = table.get("stock")
    if fallback is None:  # pragma: no cover - the yaml always has stock_bps
        raise KeyError("costs.slippage.stock_bps is required")
    for asset in ASSET_CLASSES:
        table.setdefault(asset, fallback)
    return table


_SLIPPAGE_TABLE: dict[str, float] | None = None


def slippage_table() -> dict[str, float]:
    """The process-wide slippage table, read from config once.

    ``trading/impact.py`` kept a third copy of the stock/ETF numbers; this is the
    accessor it should have had. Returns a copy so a caller cannot edit the cache.
    """
    global _SLIPPAGE_TABLE
    if _SLIPPAGE_TABLE is None:
        _SLIPPAGE_TABLE = _slippage_table()
    return dict(_SLIPPAGE_TABLE)


def reset_slippage_cache() -> None:
    """Drop the memoised table; called by ``settings.reload()``."""
    global _SLIPPAGE_TABLE
    _SLIPPAGE_TABLE = None


def slippage_bps_for(instrument: Instrument) -> float:
    """`BarExecutionModel.slippage_bps` without constructing a model."""
    table = slippage_table()
    return table.get(instrument.asset, table.get("stock", 0.0))


@dataclass(frozen=True)
class BarExecutionModel:
    """Bar-level fill assumptions, read from configs/costs.yaml.

    The numbers below were literals here *and* in ``trading/impact.py`` *and* in
    ``costs.yaml``. Only the two code copies were live, so changing the slippage
    assumption in the yaml changed nothing. They now have one home.
    """

    cost_model: CostModel = field(default_factory=CostModel)
    slippage_bps_by_asset: dict[str, float] = field(default_factory=slippage_table)
    limit_up_penalty_bps: float = field(
        default_factory=lambda: _cost("limit_up_penalty_bps"))
    volume_cap_ratio: float = field(
        default_factory=lambda: _cost("volume_cap_ratio"))

    # ── public ──────────────────────────────────────────────────────────────
    def slippage_bps(self, instrument: Instrument) -> float:
        """Adverse price drift assumed for this asset class, in basis points."""
        table = self.slippage_bps_by_asset
        return table.get(instrument.asset, table.get("stock", 0.0))

    def match(
        self,
        order: Order,
        bar: Bar,
        instrument: Instrument,
        ts: datetime,
        calendar: TradingCalendar | None = None,
        ref_price: Decimal | None = None,
    ) -> FillResult:
        if order.remaining_qty <= 0:
            return FillResult(None, RejectReason.BAD_QTY, "no remaining qty")
        if bar.suspended:
            return FillResult(None, RejectReason.SUSPENDED, "suspended")
        if bar.volume <= 0:
            return FillResult(None, RejectReason.NO_LIQUIDITY, "zero volume")

        # Direct PaperBook/research callers get the same session guard as the
        # engine; omitting a calendar must not turn into an open-price bypass.
        session = (calendar or TradingCalendar.always()).session_at(ts)
        up_limit, down_limit = bar.limits(instrument)

        # Enum identity, not a string comparison. ``session.value == "AUCTION_CLOSE"``
        # silently became "no matching in ..." if the member were ever renamed,
        # while the type check below still read as correct.
        if session is Session.AUCTION_CLOSE:
            result = self._match_close(order, bar, up_limit, down_limit)
        elif session is not Session.CONTINUOUS:
            return FillResult(None, RejectReason.MARKET_CLOSED,
                              f"no matching in {session.value}")
        else:
            result = self._match_continuous(
                order, bar, instrument, up_limit, down_limit
            )

        if result.fill is None:
            return result

        return self._settle_fill(order, bar, instrument, result.fill, ts,
                                 session.value, ref_price)

    # ── internals ───────────────────────────────────────────────────────────
    def _match_continuous(
        self,
        order: Order,
        bar: Bar,
        instrument: Instrument,
        up_limit: Decimal,
        down_limit: Decimal,
    ) -> FillResult:
        """One implementation for both sides.

        The buy and sell branches were 60 near-identical lines each, and they
        drifted: the sell side never learned the ``QUEUE_NO_PRIORITY`` distinction
        the buy side got, and a fix to the lot rounding had to be written twice.
        """
        side = order.side
        is_buy = side == Side.BUY
        limit = order.limit_price if order.order_type == OrderType.LIMIT else None

        if order.order_type == OrderType.LIMIT:
            if limit is None:
                return FillResult(None, RejectReason.PRICE_LIMIT,
                                  "limit order without price")
            # A live bar's ``open`` is the price *now*, so an order on the far
            # side of it is simply not marketable yet. The day's low/high cannot
            # stand in as evidence: they describe prints that happened before
            # this ticket existed, and using them books a fill at a price the
            # market never offered this order. Filling at the limit also hands
            # the account the best possible print and a zero delay cost, which
            # is the opposite of what a track record is for. Refuse; the caller
            # re-proposes at a marketable price or waits.
            if bar.priced_at == "live" and (
                (is_buy and limit < bar.open) or (not is_buy and limit > bar.open)
            ):
                return FillResult(
                    None, RejectReason.PRICE_LIMIT,
                    f"limit {limit} is not marketable at the current quote {bar.open}",
                )
            # A buy limit under the day's low never traded; a sell limit above the
            # day's high likewise. Both are "the price was never there".
            if is_buy and limit < bar.low:
                return FillResult(None, RejectReason.PRICE_LIMIT, "limit below day low")
            if not is_buy and limit > bar.high:
                return FillResult(None, RejectReason.PRICE_LIMIT, "limit above day high")
            base = min(bar.open, limit) if is_buy else max(bar.open, limit)
            rule = "LIMIT_ORDER"
            # A limit on the far side of a *historical* open can only have filled
            # later in the session: the matcher is using the day's range to decide
            # the price came to the order, then pricing it at the order's own
            # limit. The price is the one an exchange would print, but the time is
            # not 09:30, and without a label "filled at the open" and "filled some
            # time before 15:00" look identical in ``paper_fills.rule_id``.
            if (is_buy and limit < bar.open) or (not is_buy and limit > bar.open):
                rule += "+TOUCH_DAY"
        else:
            base = bar.open
            rule = "CONTINUOUS"

        refusal = blocked_at_open(side, bar.open, up_limit, down_limit)
        if refusal:
            if is_buy:
                if limit is not None and limit < up_limit:
                    # The ceiling is above what this trader is willing to pay, so
                    # no seller at the seal can reach the bid. Filling at the
                    # ceiling would be a price the trader never offered.
                    return FillResult(None, RejectReason.PRICE_LIMIT,
                                      "sealed ceiling above the limit price")
                if bar.fill_ratio >= 1.0:
                    if bar.priced_at == "live":
                        # Same impossibility, different situation: the board is
                        # sealed *now*, so this order could still fill if it
                        # opened. Saying "opened at the upper limit" here told the
                        # model the day was over when the session had hours left.
                        return FillResult(
                            None, RejectReason.QUEUE_NO_PRIORITY,
                            "ceiling sealed at the current quote; "
                            "queue position unknown",
                        )
                    return FillResult(
                        None, RejectReason.OPEN_AT_LIMIT,
                        f"{refusal}; no queue data to price a fill",
                    )
                price = up_limit
                rule = "LIMIT_UP_QUEUE"
            else:
                # A limit-down open has no bid: nothing clears at 09:30. If the
                # board breaks later that is a different bar, not this one, and a
                # queue ratio cannot manufacture a counterparty.
                return FillResult(None, RejectReason.LIMIT_DOWN, refusal)
        else:
            price = fill_price_at_open(
                side, base, self.slippage_bps(instrument), up_limit, down_limit,
                instrument.price_tick,
            )
            if is_buy and up_limit > 0 and price >= up_limit:
                rule += "+CAPPED_UP"
            elif not is_buy and down_limit > 0 and price <= down_limit:
                rule += "+CAPPED_DOWN"

        # Slippage may never push a limit order through its own limit: the
        # exchange does not produce that fill, so neither may the model. This
        # used to bite exactly on the gap-through-limit days the desk promises
        # to cancel instead of chase.
        if limit is not None:
            price = min(price, limit) if is_buy else max(price, limit)

        qty = order.remaining_qty
        if bar.fill_ratio < 1.0:
            qty = int(qty * bar.fill_ratio)
        if qty <= 0:
            return FillResult(None, RejectReason.BAD_QTY, "fill ratio zero")
        return FillResult(self._pending(order, qty, price, bar, rule))

    def _match_close(
        self,
        order: Order,
        bar: Bar,
        up_limit: Decimal,
        down_limit: Decimal,
    ) -> FillResult:
        """The closing call auction clears at the close, without open slippage."""
        price = bar.close
        if not price.is_finite() or price <= 0:
            return FillResult(None, RejectReason.PRICE_LIMIT, "invalid closing price")
        if order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                return FillResult(None, RejectReason.PRICE_LIMIT,
                                  "limit order without price")
            if order.side == Side.BUY and price > order.limit_price:
                return FillResult(None, RejectReason.PRICE_LIMIT,
                                  "close above buy limit")
            if order.side == Side.SELL and price < order.limit_price:
                return FillResult(None, RejectReason.PRICE_LIMIT,
                                  "close below sell limit")

        # A board sealed at the closing print has no counterparty left: every
        # resting order on the other side was consumed. Filling anyway is the
        # same optimism the open path refuses, just at a different hour.
        if order.side == Side.BUY and up_limit > 0 and price >= up_limit - PRICE_EPS:
            return FillResult(
                None, RejectReason.QUEUE_NO_PRIORITY,
                "ceiling sealed at the closing auction; queue position unknown",
            )
        if order.side == Side.SELL and down_limit > 0 and price <= down_limit + PRICE_EPS:
            return FillResult(None, RejectReason.LIMIT_DOWN,
                              "floor sealed at the closing auction; no bid")

        qty = order.remaining_qty
        if bar.fill_ratio < 1.0:
            qty = int(qty * bar.fill_ratio)
        if qty <= 0:
            return FillResult(None, RejectReason.BAD_QTY, "fill ratio zero")
        return FillResult(self._pending(order, qty, price, bar, "CLOSE_AUCTION"))

    @staticmethod
    def _pending(order: Order, qty: int, price: Decimal, bar: Bar,
                 rule: str) -> Fill:
        """A fill whose fees and id are filled in by :meth:`_settle_fill`."""
        return Fill(
            fill_id="pending",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            fees=D("0"),
            ts=bar.ts,
            rule_id=rule,
        )

    def _settle_fill(
        self,
        order: Order,
        bar: Bar,
        instrument: Instrument,
        fill: Fill,
        ts: datetime,
        session: str,
        ref_price: Decimal | None,
    ) -> FillResult:
        """Apply the capacity cap, legal lots and fees to a priced fill."""
        # Capacity is judged on the previous session's turnover, because the
        # order is being priced at 09:30 and today's total volume is a 15:00
        # fact. Using `bar.volume` here let the future enlarge a fill: a stock
        # that traded 100x its normal size after the open made a 09:30 order
        # "absorb" ten times more than anyone could have known it would.
        # A zero means the bar builder supplied nothing (synthetic bars in
        # tests); falling back keeps those fixtures working, and
        # `market.bar()` always supplies it on the production path.
        capacity = bar.prior_volume if bar.prior_volume > 0 else bar.volume
        max_qty = max(0, int(capacity * self.volume_cap_ratio))
        capped = min(fill.qty, max_qty)
        qty = capped
        if qty <= 0:
            return FillResult(None, RejectReason.NO_LIQUIDITY, "volume cap")
        if order.side == Side.BUY:
            qty = instrument.buy_lot(qty)
        elif qty < order.remaining_qty:
            # A partial fill may leave an odd remainder *on the book* — it may
            # not itself be an odd quantity. ``sell_lot`` answers a different
            # question ("may this be submitted at all", where 150 of 300 really
            # is illegal) and returns 0, which cancelled the whole sell whenever
            # the volume cap or a queue ratio bit. Rounding down is what a
            # partial fill looks like.
            step = max(1, instrument.qty_step)
            qty = (qty // step) * step
        else:
            qty = instrument.sell_lot(qty, order.remaining_qty)
        if qty <= 0:
            return FillResult(None, RejectReason.BAD_LOT, "lot rounding to zero")

        amount = money(D(qty) * fill.price)
        fees = (
            self.cost_model.incremental_buy_fees(
                amount, instrument, order.filled_notional, order.fees_paid
            )
            if order.side == Side.BUY
            else self.cost_model.incremental_sell_fees(
                amount, instrument, order.filled_notional, order.fees_paid
            )
        )
        # Name the reason the quantity changed. Both used to be reported as
        # VOLUME_CAP, so "the order was rounded from 250 to 200 shares to make a
        # legal lot" and "the book could only absorb 200 of them" looked the same
        # in `paper_fills.rule_id` — and the TCA review that counts liquidity
        # constraints was counting lot rounding as one.
        rule = fill.rule_id
        if qty < capped:
            rule = rule + "+LOT_ROUND"
        elif capped < fill.qty:
            # Name the basis when it is the fallback. ``prior_volume == 0`` on a
            # historical bar means "the builder had no prior session" — a resumed
            # stock, or a synthetic fixture — and the cap then used *today's*
            # volume, which is a 15:00 fact. At this account size the cap never
            # binds, so the label is what makes the compromise auditable instead
            # of invisible.
            suffix = "+VOLUME_CAP"
            if bar.prior_volume <= 0 and bar.priced_at != "live":
                suffix += "_TODAY"
            rule = rule + suffix
        penalty = D("0")
        if rule.startswith("LIMIT_UP_QUEUE"):
            # ``limit_up_penalty_bps`` was declared, documented in costs.yaml and
            # never read: buying into a sealed ceiling costs queue position and
            # usually marks an immediate loss, and that cost has to appear
            # somewhere or the parameter is a lie. It is carried on its own
            # ``Fill`` field, NOT folded into ``fees``: ``fees`` must stay exactly
            # the three CostModel lines, otherwise ``order.fees_paid`` (handed to
            # ``incremental_buy_fees`` below) would subtract the penalty from the
            # next partial fill's commission, and ``fees_paid``/``avg_cost``/
            # ``realized_pnl`` would all be inflated by a queue cost.
            penalty = money(
                amount * D(str(self.limit_up_penalty_bps)) / D("10000")
            )
            if penalty > 0:
                rule = rule + "+LIMIT_UP_PENALTY"
        # What the trade is measured against for implementation shortfall. The
        # caller knows the decision price; the matcher can only fall back to the
        # price it matched on, which understates delay cost but never invents one.
        reference = ref_price
        if reference is None or not reference.is_finite() or reference <= 0:
            reference = bar.close if session == "AUCTION_CLOSE" else bar.open
        return FillResult(
            Fill(
                fill_id=f"{order.order_id}-{order.filled_qty + qty}",
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                qty=qty,
                price=fill.price,
                fees=fees,
                ts=ts,
                rule_id=rule,
                ref_price=reference,
                limit_up_penalty=penalty,
            )
        )
