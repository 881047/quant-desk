"""Domain models for the A-share paper-trading kernel.

Rules:
- money and prices are Decimal
- shares are int
- timestamps are tz-aware datetimes
- no business method reads the wall clock
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from .units import (
    LIMIT_PCT_DEFAULT,
    MONEY_QUANT,
    PRICE_EPS,
    PRICE_TICK,
    SHARES_PER_LOT,
)

# Instrument defaults are read from configs/costs.yaml, and ``Instrument`` is
# constructed once per symbol per bar in the hot path. ``settings.num`` takes a
# module-level lock on every call, so the two rate lookups per construction were
# a needless contention point in the API threads. Memoise them; ``settings.reload``
# clears the memo through :func:`reset_defaults`.
_DEFAULT_RATES: dict[str, Decimal] = {}
#: ``settings.reload()`` clears the memo from another thread while an
#: ``Instrument`` may be under construction. A plain dict read is safe under the
#: GIL, but "check then fill" was two operations: a reload landing between them
#: could persist the *old* rate for one instrument. One lock, held for a dict
#: lookup, costs nothing next to the config read it replaces.
_DEFAULT_RATES_LOCK = threading.Lock()


def _fee(key: str) -> Decimal:
    """A cost default read from configs/costs.yaml, memoised per process.

    ``Instrument`` used to carry its own copy of the stamp-duty and transfer-fee
    rates, which is the same number ``costs.yaml`` and ``CostModel`` also held.
    Reading config here keeps the instrument's defaults honest: an ETF still
    says "no tax" with zero rates, but the stock default follows the account's
    actual broker schedule instead of a literal from two years ago.
    """
    with _DEFAULT_RATES_LOCK:
        cached = _DEFAULT_RATES.get(key)
        if cached is None:
            from service import settings

            cached = Decimal(str(settings.num(f"costs.{key}")))
            _DEFAULT_RATES[key] = cached
        return cached


def reset_defaults() -> None:
    """Drop the memoised instrument rates; called by ``settings.reload()``."""
    with _DEFAULT_RATES_LOCK:
        _DEFAULT_RATES.clear()


def _initial_cash() -> Decimal:
    """Opening cash for a bare ``Account()``, from configs/params.yaml.

    It used to be the literal ``5000`` here *and* in ``Ledger.fresh`` *and* in
    three ``os.getenv("INITIAL_CASH", "5000")`` fallbacks, while
    ``risk.initial_cash`` sat in ``params.yaml`` and was never read by any of
    them. One source now: change the account size in the yaml.
    """
    from service import settings

    return D(f"{settings.num('risk.initial_cash'):.2f}")



def D(value: object) -> Decimal:
    """Decimal from str/int/float without binary-float surprises."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)  # type: ignore[arg-type]


def money(value: object) -> Decimal:
    return D(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


#: 单笔股数的合理上限。A 股单只股本最大约 3,000 亿股（农业银行），再往上是
#: 调用方算错了（把金额当股数、漏除价格、单位从"手"算成"股"），而不是一笔
#: 大单。这个界同时是一道算术护栏：Decimal 默认上下文只有 28 位有效数字，
#: ``D(10**400) * price`` 再 ``quantize`` 会抛 InvalidOperation，而那会从
#: ``submit`` 里直接冲出去。**拒绝一个不可能的数量，比让它变成 500 好。**
MAX_ORDER_QTY = 10 ** 12


def integral_qty(value: object) -> int | None:
    """``value`` as a whole share count, or ``None`` when it is not one.

    The check used to be ``float(qty).is_integer()``. Three ways that lies: a
    share count above ``1.8e308`` raises ``OverflowError`` straight out of
    ``submit`` (an unhandled 500 on the API); a Decimal carrying more precision
    than a float survives (``100.00000000000000001`` becomes ``100.0``, an
    integer); and the arithmetic downstream is not safe either — ``Decimal(10**400)
    * price`` exceeds the 28-digit context and raises ``InvalidOperation`` from
    inside ``money()``. Comparing in the value's own type, and refusing a count
    no listed company could issue, cannot raise and cannot round.

    It lives here rather than on the order manager because two callers ask the
    same question and had answered it differently: ``oms.submit`` refused a
    fractional quantity, while ``decisions._size_buy`` wrote ``int(100.7)`` into
    the ticket — 100 shares, silently, which is a size the caller never asked
    for and cannot see was changed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        count = value
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            return None
        count = int(value)
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        count = int(value)
    else:
        return None
    if count < 0 or count > MAX_ORDER_QTY:
        return None
    return count


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class Board(str, Enum):
    MAIN = "MAIN"
    CHINEXT = "CHINEXT"
    STAR = "STAR"
    BSE = "BSE"
    ETF = "ETF"
    # Convertible / exchangeable bonds: 10-unit lots, T+0, 20% band, no stamp
    # duty. A separate member because every one of those four rules differs from
    # the main board, and expressing them through `is_etf` would repeat the
    # mistake `Instrument.stamp_duty` was introduced to end.
    CB = "CB"


class Session(str, Enum):
    CLOSED = "CLOSED"
    PRE_OPEN = "PRE_OPEN"
    AUCTION_OPEN = "AUCTION_OPEN"
    CONTINUOUS = "CONTINUOUS"
    AUCTION_CLOSE = "AUCTION_CLOSE"
    AFTER_HOURS = "AFTER_HOURS"


class RejectReason(str, Enum):
    OK = "OK"
    KILL_SWITCH = "KILL_SWITCH"
    SAFE_MODE = "SAFE_MODE"
    MARKET_CLOSED = "MARKET_CLOSED"
    SUSPENDED = "SUSPENDED"
    #: ST 股不新开仓。**只对买入生效**——卖出永远放行，否则股票被 ST 之后
    #: 持仓就卖不掉了。内核/接口不替你下新判断的单；破线持续
    #: autostop.confirm_seconds 后，抓取循环代执行建仓票上写好的止损
    #: （configs/params.yaml 的 autostop.enabled 可关）。
    ST = "ST"
    NOT_IN_UNIVERSE = "NOT_IN_UNIVERSE"
    BAD_LOT = "BAD_LOT"
    BAD_QTY = "BAD_QTY"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_SHARES = "INSUFFICIENT_SHARES"
    T_PLUS_ONE = "T_PLUS_ONE"
    PRICE_LIMIT = "PRICE_LIMIT"
    POSITION_CAP = "POSITION_CAP"
    SINGLE_TRADE_LOSS = "SINGLE_TRADE_LOSS"
    GAP_LOSS = "GAP_LOSS"
    DAILY_LOSS = "DAILY_LOSS"
    CONSECUTIVE_LOSSES = "CONSECUTIVE_LOSSES"
    MAX_POSITIONS = "MAX_POSITIONS"
    LIMIT_DOWN = "LIMIT_DOWN"
    OPEN_AT_LIMIT = "OPEN_AT_LIMIT"
    # The ceiling is sealed *now* on a live quote. Same impossibility as
    # ``OPEN_AT_LIMIT``, different remedy: this one can still fill if the board
    # opens, so it is a queueing problem rather than a closed door.
    QUEUE_NO_PRIORITY = "QUEUE_NO_PRIORITY"
    NO_LIQUIDITY = "NO_LIQUIDITY"
    CANCEL_NOT_ALLOWED = "CANCEL_NOT_ALLOWED"
    ALREADY_FILLED = "ALREADY_FILLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Instrument:
    """Trading rules for one security.

    ``stamp_duty`` and ``transfer_fee`` are the single source of truth for what
    this thing pays; ``CostModel`` no longer keeps a second copy. An ETF or a
    convertible bond says "no tax" by carrying zero rates, not by pretending to
    be a different asset class.

    ``is_etf`` is a legacy flag that production still sets for funds. Anything
    that needs "what kind of thing is this" must use :attr:`asset`, because a
    convertible is *not* an ETF — it has a 10-unit lot, a 0.001 tick, T+0 and a
    20% band — yet the fee-only path used to classify it as one and inherit the
    ETF's 5bps slippage assumption instead of its own.
    """

    symbol: str
    name: str = ""
    board: Board = Board.MAIN
    is_etf: bool = False
    # Exchange board lots are not uniform: STAR starts at 200 shares and then
    # increments by 1, BSE starts at 100 and then increments by 1, while the main
    # boards and ChiNext stay on 100-share multiples.
    #
    # There used to be a ``lot_size`` field beside these two. Nothing read it —
    # and production set ``min_qty=10`` for a convertible while ``lot_size``
    # stayed at its 100 default, so the first consumer would have sized bond
    # orders ten times too large. A field that is never read cannot be kept
    # honest, so it is gone rather than documented.
    min_qty: int = SHARES_PER_LOT
    qty_step: int = SHARES_PER_LOT
    price_tick: Decimal = PRICE_TICK
    t_plus: int = 1
    # Main-board band. A caller that knows the code must use
    # ``adapters.symbols.limit_pct`` (or ``market.instrument``) instead of
    # relying on this default; the magnitudes live in ``units.LIMIT_PCT_*``.
    limit_pct: Decimal = LIMIT_PCT_DEFAULT
    stamp_duty: Decimal = field(default_factory=lambda: _fee("stamp_duty_sell"))
    transfer_fee: Decimal = field(default_factory=lambda: _fee("transfer_fee"))
    is_st: bool = False

    def __post_init__(self) -> None:
        if self.board is Board.CB and (self.stamp_duty or self.transfer_fee):
            raise ValueError(
                f"{self.symbol}: a convertible pays no stamp duty or transfer fee; "
                "zero the rates on the instrument"
            )
        if self.is_etf and (self.stamp_duty or self.transfer_fee):
            raise ValueError(
                f"{self.symbol}: an ETF pays no stamp duty or transfer fee; "
                "zero the rates on the instrument instead of relying on is_etf"
            )
        if self.t_plus < 0:
            raise ValueError(f"{self.symbol}: t_plus cannot be negative")
        if self.min_qty < 1 or self.qty_step < 1:
            raise ValueError(f"{self.symbol}: min_qty/qty_step must be positive")

    @property
    def asset(self) -> str:
        """``"stock"``, ``"etf"`` or ``"cb"`` — the axis costs and caps key off."""
        if self.board is Board.CB:
            return "cb"
        if self.is_etf or self.board is Board.ETF:
            return "etf"
        return "stock"

    @property
    def is_stock(self) -> bool:
        return self.asset == "stock"

    def buy_lot(self, qty: int) -> int:
        """Round a buy down to a legal quantity for this board."""
        if qty <= 0:
            return 0
        minimum = max(1, self.min_qty)
        step = max(1, self.qty_step)
        if qty < minimum:
            return 0
        return minimum + ((qty - minimum) // step) * step

    def sell_lot(self, qty: int, available: int) -> int:
        """Sell may be an odd lot only when closing the entire position."""
        if qty <= 0 or available <= 0:
            return 0
        if qty >= available:
            return available
        if qty % max(1, self.qty_step) != 0:
            return 0
        return qty

    def band(self, pre_close: Decimal) -> tuple[Decimal, Decimal]:
        """The daily price band implied by this instrument's own limit.

        Used when a bar arrives without ``up_limit``/``down_limit`` — a synthetic
        fixture, or a vendor row whose limit columns are empty. The alternative
        (treating a zero limit as "no band") made ``blocked_at_open`` see every
        open as sealed, so a bar without limits refused every buy.
        """
        if pre_close is None or not pre_close.is_finite() or pre_close <= 0:
            return D("0"), D("0")
        pct = self.limit_pct if self.limit_pct > 0 else LIMIT_PCT_DEFAULT
        tick = self.price_tick if self.price_tick > 0 else PRICE_TICK
        return (
            (pre_close * (D("1") + pct)).quantize(tick, rounding=ROUND_HALF_UP),
            (pre_close * (D("1") - pct)).quantize(tick, rounding=ROUND_HALF_UP),
        )


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    pre_close: Decimal
    volume: int
    amount: Decimal
    up_limit: Decimal
    down_limit: Decimal
    suspended: bool = False
    # Share of a queued order that clears, from ``execution.derive_fill_ratio``.
    # 1.0 means "no queue constraint stated"; a bar builder that knows the seal
    # must set it, because the difference between a sealed board and a broken one
    # is the difference between a fill and a wish.
    fill_ratio: float = 1.0
    # Unfilled bid sitting at the limit price, when a source provides it. Kept on
    # the bar so the ratio above can be derived and audited instead of guessed.
    seal_amount: Decimal | None = None
    # Traded volume of the *previous* session. The capacity cap is applied at the
    # open, so it may only use liquidity that was already observable at 09:30;
    # today's ``volume`` is a 15:00 fact and using it to justify a 09:30 fill is
    # the one place the kernel used to let the future enlarge a trade. Zero means
    # "the builder did not supply it" — synthetic bars in tests, never the market
    # repository.
    prior_volume: int = 0
    # Where ``open`` came from, because the matcher asks it "is the ceiling
    # sealed" and the answer means two different things:
    #
    # * ``"open"`` — a historical bar. ``open`` is the 09:30 auction print, so
    #   "at the ceiling" means the board opened sealed and stays unbuyable.
    # * ``"live"`` — a bar assembled from the quote on screen. ``open`` is
    #   ``quote.last`` so the fill cannot look back, which makes "at the ceiling"
    #   mean *sealed right now*. The trade is equally impossible by market order,
    #   but the next step differs: an opening seal is a reason to give up on the
    #   name, while an intraday seal is a reason to queue and wait for the board
    #   to open. Collapsing the two into one rejection code told the model to
    #   abandon trades it could still have worked.
    priced_at: str = "open"

    @property
    def is_limit_up(self) -> bool:
        """Closed at the upper limit. A 15:00 fact: never price a fill with it."""
        return self.up_limit > 0 and self.close >= self.up_limit - PRICE_EPS

    @property
    def is_limit_down(self) -> bool:
        """Closed at the lower limit. A 15:00 fact: never price a fill with it."""
        return self.down_limit > 0 and self.close <= self.down_limit + PRICE_EPS

    # ``is_one_word_up`` / ``is_broken_board_up`` / ``opened_at_limit_up`` used
    # to live here. Nothing in the service read them: the fill question is
    # answered once, in ``microstructure.blocked_at_open``, and the tape
    # questions are answered vectorised in ``research``. Two definitions of
    # "opened at the limit" is how the kernel and the research layer came to
    # disagree in the first place — a predicate with no caller cannot be held
    # to the same rule as the one that decides fills, so it is gone.

    def limits(self, instrument: Instrument) -> tuple[Decimal, Decimal]:
        """The band to price this bar against, derived when the bar carries none.

        One implementation for the matcher and the order manager: a bar with an
        empty limit column used to be read as "the ceiling is zero", so the
        matcher refused every buy and the OMS refused every limit order — the
        same missing data producing two different rejections. When neither the
        bar nor the prior close can supply a band, the original values come back
        and the callers treat a non-positive limit as "unknown".
        """
        up, down = self.up_limit, self.down_limit
        if up > 0 and down > 0 and up.is_finite() and down.is_finite() and up >= down:
            return up, down
        derived_up, derived_down = instrument.band(self.pre_close)
        if derived_up > 0 and derived_down > 0:
            return derived_up, derived_down
        return up, down

    @classmethod
    def production(cls, *, symbol: str, ts: datetime, open: Decimal, high: Decimal,
                   low: Decimal, close: Decimal, pre_close: Decimal, volume: int,
                   amount: Decimal, up_limit: Decimal, down_limit: Decimal,
                   prior_volume: int, priced_at: str,
                   suspended: bool = False, fill_ratio: float = 1.0,
                   seal_amount: Decimal | None = None) -> Bar:
        """A bar from a real source: the two fillability inputs are required.

        On the dataclass they default to the optimistic reading — ``prior_volume=0``
        lets the capacity cap fall back to *today's* volume (a 15:00 fact), and
        ``priced_at="open"`` claims the open is the 09:30 auction print. Each is
        right for some bars and wrong for others, and a builder that forgets them
        gets a bar that reads better than the data behind it. The production
        builders state both; synthetic fixtures keep the dataclass constructor.
        """
        return cls(
            symbol=symbol, ts=ts, open=open, high=high, low=low, close=close,
            pre_close=pre_close, volume=volume, amount=amount,
            up_limit=up_limit, down_limit=down_limit, suspended=suspended,
            fill_ratio=fill_ratio, seal_amount=seal_amount,
            prior_volume=prior_volume, priced_at=priced_at,
        )


@dataclass
class Position:
    symbol: str
    total_qty: int = 0
    available_qty: int = 0
    frozen_qty: int = 0
    # Bought but not yet deliverable: the T+1 lock, made explicit.
    #
    # It used to exist only as the unnamed difference ``total - available -
    # frozen``, which meant nothing could assert it, a restart could not tell
    # "bought today" from "snapshot is corrupt", and any call to ``settle()``
    # released it regardless of when that call happened. Now the identity
    # ``total == available + frozen + locked`` holds at every point in the
    # lifecycle and ``settle()`` is the only thing that clears it.
    locked_qty: int = 0
    avg_cost: Decimal = D("0")
    last_price: Decimal = D("0")
    realized_pnl: Decimal = D("0")
    sleeve: str = "CORE"

    @property
    def market_value(self) -> Decimal:
        return money(D(self.total_qty) * self.last_price)

    @property
    def unrealized_pnl(self) -> Decimal:
        return money(D(self.total_qty) * (self.last_price - self.avg_cost))

    @property
    def unsettled_qty(self) -> int:
        """Shares in the account but not yet sellable and not reserved.

        Equal to :attr:`locked_qty` by construction; it exists so callers can ask
        the question in their own vocabulary without knowing the settlement rule.
        """
        return self.locked_qty

    def is_consistent(self) -> bool:
        """Every share is either sellable, reserved, or locked. No other place."""
        return (
            self.total_qty == self.available_qty + self.frozen_qty + self.locked_qty
            and self.available_qty >= 0
            and self.frozen_qty >= 0
            and self.locked_qty >= 0
        )


@dataclass
class Account:
    cash_available: Decimal = field(default_factory=_initial_cash)
    cash_frozen: Decimal = D("0")
    cash_withdrawable: Decimal = D("0")
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: Decimal = D("0")
    dividend_income: Decimal = D("0")
    fees_paid: Decimal = D("0")
    peak_equity: Decimal = field(default_factory=_initial_cash)

    def position(self, symbol: str) -> Position:
        """The position, created empty if this symbol has never been held.

        Only for callers that are about to *write* to it. Anything that merely
        asks a question must use :meth:`peek`: a rejected sell for a symbol the
        account does not hold used to leave an empty ``Position`` behind, which
        was then persisted in every snapshot and made ``Ledger.reconcile``
        report an "unexpected position" for a trade that never happened.
        """
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)
        return self.positions[symbol]

    def peek(self, symbol: str) -> Position | None:
        """The position if it exists, without inventing one."""
        return self.positions.get(symbol)

    def mark(self, prices: dict[str, Decimal] | None) -> None:
        """Write marks onto positions. A *write*, and named like one.

        This used to happen inside :meth:`market_value`, so every read-only
        caller — a risk check, a drawdown query, a reconcile — silently rewrote
        the account's last prices. A getter with a side effect is how a rejected
        order ends up changing the equity print.
        """
        if not prices:
            return
        for symbol, price in prices.items():
            pos = self.positions.get(symbol)
            if pos is None or pos.total_qty == 0:
                continue
            value = D(price)
            if value.is_finite() and value > 0:
                pos.last_price = value

    def market_value(self, prices: dict[str, Decimal] | None = None) -> Decimal:
        """Mark-to-market of held positions. Pure: ``prices`` wins, else last mark.

        Iterates a snapshot of the items. ``equity()`` is read from the API
        thread while a fill in another thread may insert a new position, and
        iterating a dict that grows raises ``RuntimeError: dictionary changed
        size during iteration``. A copy cannot raise; the worst case is that a
        position created mid-read is left out of this one valuation.
        """
        total = D("0")
        for symbol, pos in tuple(self.positions.items()):
            if pos.total_qty == 0:
                continue
            price = pos.last_price
            if prices and symbol in prices:
                candidate = D(prices[symbol])
                if candidate.is_finite() and candidate > 0:
                    price = candidate
            total += D(pos.total_qty) * price
        return money(total)

    def equity(self, prices: dict[str, Decimal] | None = None) -> Decimal:
        return money(self.cash_available + self.cash_frozen + self.market_value(prices))

    def cash_total(self) -> Decimal:
        return money(self.cash_available + self.cash_frozen)

    def update_peak(self, prices: dict[str, Decimal] | None = None) -> Decimal:
        """Mark, then track the high-water equity. Returns the marked equity."""
        self.mark(prices)
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        return eq

    def drawdown(self, prices: dict[str, Decimal] | None = None) -> Decimal:
        eq = self.equity(prices)
        if self.peak_equity <= 0:
            return D("0")
        return (eq - self.peak_equity) / self.peak_equity


@dataclass
class Order:
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    status: OrderStatus = OrderStatus.PENDING_NEW
    filled_qty: int = 0
    avg_fill_price: Decimal = D("0")
    created_ts: datetime | None = None
    updated_ts: datetime | None = None
    reason: str = ""
    session: Session = Session.CONTINUOUS
    frozen_cash: Decimal = D("0")
    frozen_shares: int = 0
    sleeve: str = "CORE"
    intent_fingerprint: str = ""
    t_plus: int = 1
    fees_paid: Decimal = D("0")
    filled_notional: Decimal = D("0")
    last_matched_bar_ts: datetime | None = None
    #: How much of this order's frozen cash came out of the *withdrawable*
    #: pool. T+1 cash cannot be taken out of the account today, so the two
    #: pools must be kept apart: freezing takes from both, and the order has
    #: to remember which part it took from the withdrawable one — otherwise the
    #: release cannot put the right amount back, and the withdrawable balance
    #: drifts upward every time a T+1-restricted sale funds a buy.
    frozen_withdrawable: Decimal = D("0")

    @property
    def remaining_qty(self) -> int:
        return max(0, self.qty - self.filled_qty)

    @property
    def is_open(self) -> bool:
        return self.status in {
            OrderStatus.PENDING_NEW,
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.PENDING_CANCEL,
        }


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: Side
    qty: int
    price: Decimal
    fees: Decimal
    ts: datetime
    rule_id: str = ""
    # The price the decision was made against — the prior close for a ticket
    # written after the bell, the live quote for an intraday fill. Without it a
    # fill records what was paid but not what it was measured from, so
    # implementation shortfall (delay + slippage + impact) cannot be computed at
    # all. Optional because a synthetic fill in a test has no decision behind it.
    ref_price: Decimal | None = None
    # Queue-position cost of buying into a sealed limit-up ceiling, in CNY — an
    # amount, not a bps rate, so TCA never multiplies it by the notional again.
    # It is deliberately NOT part of ``fees``: ``fees`` is exactly the three
    # CostModel lines (commission, stamp duty, transfer fee), which keeps
    # ``fees_paid``/``avg_cost``/``realized_pnl`` clean and stops the amount from
    # being deducted twice by ``incremental_*_fees`` on later partial fills.
    # Defaulted and appended last so old constructors and old JSON snapshots
    # still work (``oms.fill_from_dict`` reads it with .get).
    limit_up_penalty: Decimal = D("0")


@dataclass(frozen=True)
class Intent:
    symbol: str
    side: Side
    qty: int | None = None
    target_weight: Decimal | None = None
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    reason: str = ""
    sleeve: str = "CORE"
    client_order_id: str | None = None
    stop_price: Decimal | None = None
    max_loss: Decimal | None = None

    def __post_init__(self) -> None:
        """Normalise an integral quantity to ``int``.

        ``qty`` is a share count, so ``100``, ``100.0`` and ``Decimal("100")``
        are the same intent and must hash the same — the idempotency fingerprint
        is computed from these fields, and a caller that passed a Decimal used to
        crash it outright (``json`` cannot serialise Decimal) while a float
        silently produced a different key for the same order.
        """
        qty = self.qty
        if qty is None or isinstance(qty, (bool, int)):
            return
        kind = type(qty)
        if kind is Decimal:
            integral = qty.is_finite() and qty == qty.to_integral_value()
        elif kind is float:
            integral = qty.is_integer()
        else:
            return
        if integral:
            object.__setattr__(self, "qty", int(qty))


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: RejectReason = RejectReason.OK
    adjusted_qty: int | None = None
    detail: str = ""
