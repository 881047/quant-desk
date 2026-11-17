"""Pure pre-trade risk rules — the reference set, not the desk's enforcement point.

The desk enforces `service/trading/guard.py`, which runs on every proposal *and*
again immediately before a queued ticket fills, and which can see the whole
portfolio (industry weight, sleeve, daily action count). These rules are a
smaller, position-local subset used by `engine.py` in the golden scenarios.

The two are not interchangeable and this module does not pretend otherwise:
`daily_pnl_pct` and `consecutive_losses` are inputs here, supplied by whoever
drives the engine, so a caller that forgets to update them silently disables two
breakers. That is exactly why production does not use this path.

No LLM, no I/O, no wall clock.

**Shape.** ``check`` used to be one 160-line method with nine returns, so "which
rule refused this order" could only be answered by counting lines. It is now a
sequence of named steps — gate, price, size, buy caps, sell — and each step
returns either a decision or ``None`` for "not my business". The rules and their
order are unchanged; only the reading is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from service import settings

from .costs import CostModel
from .models import (
    MAX_ORDER_QTY,
    Account,
    Bar,
    D,
    Instrument,
    Intent,
    OrderType,
    Position,
    RejectReason,
    RiskDecision,
    Side,
    money,
)


def _dec(key: str):
    """A Decimal default from configs/params.yaml (risk.*).

    The reference rules had their own literal defaults, which had already drifted
    from the enforcing guard: this class said ``max_positions=3`` while the
    dataclass and the never-read risk.yaml said the same number in two other
    places. One source, read at construction.
    """
    return field(default_factory=lambda: D(str(settings.num(f"risk.{key}"))))


def _count(key: str):
    return field(default_factory=lambda: settings.integer(f"risk.{key}"))


def _pct(value: object) -> Decimal:
    """A signed fraction from whatever the driver passed.

    ``float`` goes through ``str`` so ``-0.060000000000000005`` does not become
    a different number than the one the caller typed. ``None`` means "no
    information", which is zero, not an error.
    """
    if value is None:
        return D("0")
    if isinstance(value, Decimal):
        return value
    return D(str(value))


@dataclass(frozen=True)
class RiskConfig:
    max_aggressive_weight: Decimal = _dec("max_aggressive_weight")
    max_core_weight: Decimal = _dec("max_core_weight")
    min_cash_weight: Decimal = _dec("min_cash_weight")
    max_aggressive_single: Decimal = _dec("max_aggressive_single")
    max_core_stock_single: Decimal = _dec("max_core_single")
    max_core_etf_single: Decimal = _dec("max_core_etf_single")
    max_single_trade_loss: Decimal = _dec("max_trade_risk")
    max_gap_loss: Decimal = _dec("max_gap_loss")
    max_positions: int = _count("max_positions")
    daily_loss_no_new: Decimal = _dec("daily_loss_no_new")
    daily_loss_reduce_only: Decimal = _dec("daily_loss_reduce_only")
    max_consecutive_losses: int = _count("max_consecutive_losses")
    market_buy_buffer: Decimal = _dec("market_buy_buffer")


class RiskEngine:
    def __init__(self, config: RiskConfig | None = None, cost_model: CostModel | None = None) -> None:
        self.config = config or RiskConfig()
        self.cost_model = cost_model or CostModel()

    # ── public ──────────────────────────────────────────────────────────────
    def check(
        self,
        intent: Intent,
        account: Account,
        instrument: Instrument,
        bar: Bar,
        prices: dict | None = None,
        kill_switch: bool = False,
        safe_mode: bool = False,
        daily_pnl_pct: object = D("0"),
        consecutive_losses: int = 0,
    ) -> RiskDecision:
        cfg = self.config
        refusal = self._gate(intent, bar, instrument, kill_switch, safe_mode)
        if refusal is not None:
            return refusal
        refusal = self._check_limit(intent, bar, instrument)
        if refusal is not None:
            return refusal

        px = self._reference_price(intent, bar, prices)
        if px <= 0:
            return RiskDecision(False, RejectReason.PRICE_LIMIT, None,
                                "no knowable positive price")
        equity = account.equity(prices or {intent.symbol: px})
        qty = self._resolve_qty(intent, account, equity, px, prices)
        if qty is None or qty <= 0:
            return RiskDecision(False, RejectReason.BAD_QTY, None,
                                "qty is not a positive share count")

        loss_pct = _pct(daily_pnl_pct)
        if intent.side == Side.BUY and loss_pct <= D(cfg.daily_loss_reduce_only):
            return RiskDecision(False, RejectReason.DAILY_LOSS, None,
                                "daily loss: reduce-only mode")

        if intent.side == Side.BUY:
            return self._check_buy(intent, account, instrument, prices,
                                   px, qty, equity, loss_pct, consecutive_losses)
        return self._check_sell(intent, account, instrument, qty)

    # ── steps ───────────────────────────────────────────────────────────────
    @staticmethod
    def _gate(intent: Intent, bar: Bar, instrument: Instrument,
              kill_switch: bool, safe_mode: bool) -> RiskDecision | None:
        if kill_switch:
            return RiskDecision(False, RejectReason.KILL_SWITCH, None, "kill switch active")
        if safe_mode and intent.side == Side.BUY:
            return RiskDecision(False, RejectReason.SAFE_MODE, None, "safe mode: no new buys")
        if bar.suspended:
            return RiskDecision(False, RejectReason.SUSPENDED, None, "suspended")
        # ST 只拦买入，不拦卖出。
        #
        # 这里曾经对买卖一律拒绝，于是"股票被 ST 之后"持仓卖不掉——止损单、
        # 减仓单、清仓单一律 ST 拒绝，而台子的规矩是"卖不卖由你决定，系统不
        # 替你卖"。风控在最需要你跑的时候把你锁住，是最坏的一类故障。
        # ``trading/guard.py`` 从来只拦 BUY（文案就写着 "ST: sells allowed"），
        # 两处口径不一致才让这个洞一直没被发现。北交所那条已经放行，也是同一个
        # 原则：已有的仓位必须永远能出。
        if instrument.is_st and intent.side == Side.BUY:
            return RiskDecision(False, RejectReason.ST, None, "ST stock: buys disabled")
        return None

    @staticmethod
    def _check_limit(intent: Intent, bar: Bar, instrument: Instrument) -> RiskDecision | None:
        if intent.order_type is not OrderType.LIMIT:
            return None
        limit = intent.limit_price
        tick = instrument.price_tick
        if limit is None or not limit.is_finite() or limit <= 0:
            return RiskDecision(False, RejectReason.PRICE_LIMIT, None,
                                "limit price must be finite and positive")
        # The band comes from ``Bar.limits`` — the same call the order manager
        # makes — not from the raw columns. Reading ``bar.up_limit`` here meant a
        # feed row with the limit columns empty was refused by risk as "outside
        # the band" while the OMS would have derived 11.00/9.00 from ``pre_close``
        # and accepted it: one bar, two answers, and the stricter one was not the
        # correct one. A band that genuinely cannot be established still refuses.
        up_limit, down_limit = bar.limits(instrument)
        if (not tick.is_finite() or tick <= 0
                or not down_limit.is_finite() or down_limit <= 0
                or not up_limit.is_finite() or up_limit <= 0
                or limit / tick != (limit / tick).to_integral_value()
                or limit < down_limit or limit > up_limit):
            return RiskDecision(False, RejectReason.PRICE_LIMIT, None,
                                "limit price violates tick or daily band")
        return None

    @staticmethod
    def _reference_price(intent: Intent, bar: Bar, prices: dict | None) -> Decimal:
        """The price a sizing decision may use: open, then quote, then prior close.

        Never the close. Sizing off a 15:00 print at 09:30 is the same
        look-ahead the execution model was fixed for.
        """
        if bar.open.is_finite() and bar.open > 0:
            return bar.open
        if prices:
            candidate = prices.get(intent.symbol)
            if candidate is not None:
                value = D(candidate)
                if value.is_finite() and value > 0:
                    return value
        if bar.pre_close.is_finite() and bar.pre_close > 0:
            return bar.pre_close
        return D("0")

    @staticmethod
    def _resolve_qty(intent: Intent, account: Account, equity: Decimal,
                     px: Decimal, prices: dict | None = None) -> int | None:
        """The order size, with ``target_weight`` read as a target.

        Same rule as ``OrderManager.submit``: the trade is the distance between
        the current position and the weight asked for, not another order of that
        size. This module is the reference implementation the golden scenarios
        run against, and a reference that sizes differently from the code it is
        supposed to specify is worse than no reference.
        """
        qty = intent.qty
        if qty is None and intent.target_weight is not None:
            if not intent.target_weight.is_finite() or intent.target_weight < 0:
                return None
            held = account.peek(intent.symbol)
            mark = px
            if prices:
                candidate = prices.get(intent.symbol)
                if candidate is not None:
                    value = D(candidate)
                    if value.is_finite() and value > 0:
                        mark = value
            held_value = (money(D(held.total_qty) * mark)
                          if held is not None and held.total_qty > 0 else D("0"))
            target_value = money(equity * intent.target_weight)
            delta = (target_value - held_value if intent.side == Side.BUY
                     else held_value - target_value)
            qty = int(delta / px) if delta > 0 else 0
        # The same ceiling the order manager applies: a count above it is a unit
        # error, and letting it through turns ``money(D(qty) * px)`` into an
        # InvalidOperation inside the risk check.
        if not isinstance(qty, int) or isinstance(qty, bool):
            return None
        if qty > MAX_ORDER_QTY:
            return None
        return qty

    def _check_buy(
        self,
        intent: Intent,
        account: Account,
        instrument: Instrument,
        prices: dict | None,
        px: Decimal,
        qty: int,
        equity: Decimal,
        loss_pct: Decimal,
        consecutive_losses: int,
    ) -> RiskDecision:
        cfg = self.config
        qty = instrument.buy_lot(qty)
        if qty <= 0:
            return RiskDecision(False, RejectReason.BAD_LOT, None, "lot rounding to zero")

        is_limit = intent.order_type is OrderType.LIMIT
        amount = money(D(qty) * px)
        reserve_notional = money(D(qty) * intent.limit_price) if is_limit else amount
        fees = self.cost_model.buy_fees(reserve_notional, instrument)
        # The buffer is a *reservation* rule: the fill may land above the open
        # (slippage, a gap between the sizing print and the auction). It used to
        # be the literal 1.02 here and again in ``oms.submit``.
        reserve = money(
            (reserve_notional
             if is_limit
             else reserve_notional * (D("1") + cfg.market_buy_buffer)) + fees
        )
        if reserve > account.cash_available:
            return RiskDecision(
                False,
                RejectReason.INSUFFICIENT_CASH,
                None,
                f"need {reserve}, have {account.cash_available}",
            )
        cash_after = money(account.cash_available - reserve)
        min_cash = money(equity * cfg.min_cash_weight)
        if cash_after < min_cash:
            return RiskDecision(
                False, RejectReason.INSUFFICIENT_CASH, None,
                f"cash after {cash_after} < minimum {min_cash}",
            )

        pos = account.peek(intent.symbol)
        sleeve = intent.sleeve.upper()
        # Marked the same way ``equity`` is. Reading ``last_price`` while equity
        # read ``prices`` let the two disagree, and a sleeve cap computed against
        # a stale mark is a cap that can be walked through.
        sleeve_value = money(sum(
            (D(p.total_qty) * self._mark(p, prices)
             for p in account.positions.values() if p.sleeve.upper() == sleeve),
            D("0"),
        ))
        sleeve_cap = D(
            cfg.max_aggressive_weight if sleeve == "AGGRESSIVE"
            else cfg.max_core_weight
        )
        if equity > 0 and (sleeve_value + amount) / equity > sleeve_cap:
            return RiskDecision(
                False, RejectReason.POSITION_CAP, None,
                f"sleeve {sleeve_value + amount} > cap {equity * sleeve_cap}",
            )
        cap = (
            D(cfg.max_aggressive_single)
            if sleeve == "AGGRESSIVE"
            else D(cfg.max_core_etf_single if instrument.asset == "etf"
                   else cfg.max_core_stock_single)
        )
        new_value = D((pos.total_qty if pos is not None else 0) + qty) * px
        if equity > 0 and new_value / equity > cap:
            return RiskDecision(
                False,
                RejectReason.POSITION_CAP,
                None,
                f"position {new_value / equity:.2%} > cap {cap:.2%}",
            )

        active = sum(1 for p in account.positions.values() if p.total_qty > 0)
        if (pos is None or pos.total_qty == 0) and active >= cfg.max_positions:
            return RiskDecision(False, RejectReason.MAX_POSITIONS, None, "max positions")

        refusal = self._check_stop(intent, instrument, px, qty, equity)
        if refusal is not None:
            return refusal

        # A gap through the band is the loss this account cannot stop out of:
        # overnight, with no chance to act. Reserve for the instrument's own band,
        # not a main-board 10%.
        gap_loss = amount * instrument.limit_pct
        if gap_loss > equity * cfg.max_gap_loss:
            return RiskDecision(
                False, RejectReason.GAP_LOSS, None,
                f"gap loss {gap_loss} > cap {equity * cfg.max_gap_loss}",
            )

        if loss_pct <= D(cfg.daily_loss_no_new):
            return RiskDecision(False, RejectReason.DAILY_LOSS, None,
                                "daily loss circuit breaker")
        if consecutive_losses >= cfg.max_consecutive_losses:
            return RiskDecision(
                False, RejectReason.CONSECUTIVE_LOSSES, None,
                f"{consecutive_losses} consecutive losses",
            )
        return RiskDecision(True, RejectReason.OK, qty, "ok")

    def _check_stop(
        self, intent: Intent, instrument: Instrument, px: Decimal, qty: int, equity: Decimal
    ) -> RiskDecision | None:
        """Reject (and downsize) a buy whose stop implies more risk than allowed."""
        stop = intent.stop_price
        if stop is None:
            return None
        # An unusable stop is not "no stop": a zero or NaN would make the sizing
        # division meaningless, and ignoring it would let the order through with
        # the risk cap unapplied.
        if not stop.is_finite() or stop <= 0:
            return RiskDecision(
                False, RejectReason.SINGLE_TRADE_LOSS, None,
                f"stop {stop} is not a positive price",
            )
        budget = equity * D(self.config.max_single_trade_loss)
        if stop >= px:
            # A stop at or above the entry is a trailing profit-take, not a loss
            # limit: there is no per-share downside to size against. The gap rule
            # still applies to the position.
            return None
        loss = D(qty) * (px - stop)
        if loss <= budget:
            return None
        max_qty = instrument.buy_lot(int(budget / (px - stop)))
        return RiskDecision(
            False,
            RejectReason.SINGLE_TRADE_LOSS,
            max_qty,
            f"loss {loss} > cap {budget}",
        )

    @staticmethod
    def _check_sell(
        intent: Intent, account: Account, instrument: Instrument, qty: int
    ) -> RiskDecision:
        pos = account.peek(intent.symbol)
        if pos is None:
            # "You do not hold it" and "you hold it but cannot deliver it today"
            # are different answers, and the second one is actionable tomorrow.
            return RiskDecision(False, RejectReason.INSUFFICIENT_SHARES, None,
                                "no position")
        # Selling more than is owned is not a settlement problem, and answering
        # it with T_PLUS_ONE told the caller to wait a day for shares that will
        # never arrive. Ask the two questions in order.
        if qty > pos.total_qty:
            return RiskDecision(False, RejectReason.INSUFFICIENT_SHARES, None,
                                f"holding {pos.total_qty} < sell {qty}")
        if qty > pos.available_qty:
            return RiskDecision(False, RejectReason.T_PLUS_ONE, None, "T+1: not available")
        qty = instrument.sell_lot(qty, pos.available_qty)
        if qty <= 0:
            return RiskDecision(False, RejectReason.BAD_LOT, None, "sell lot zero")
        return RiskDecision(True, RejectReason.OK, qty, "ok")

    @staticmethod
    def _mark(pos: Position, prices: dict | None) -> Decimal:
        """The best available mark for one position, without mutating it."""
        if prices:
            candidate = prices.get(pos.symbol)
            if candidate is not None:
                value = D(candidate)
                if value.is_finite() and value > 0:
                    return value
        return pos.last_price
