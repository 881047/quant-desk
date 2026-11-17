"""A sealed ceiling means two different things, and the difference is a decision.

``Bar.open`` is the 09:30 auction print on a historical bar and the current
quote on a live one. Before ``priced_at`` existed both produced
``OPEN_AT_LIMIT`` — literally "opened at the upper limit" — so a board that
sealed at 13:00 was recorded as a session that opened sealed. Same impossibility,
opposite next step: an opening seal is a reason to drop the name for the day, an
intraday seal is a reason to queue and wait for the board to open.

The bug this pins is not the refusal (the refusal was right) but the sentence
attached to it, because that sentence is what the model reads when it decides
whether to try again tomorrow.
"""

from __future__ import annotations

from datetime import datetime

from service.kernel.clock import SHANGHAI
from service.kernel.execution import BarExecutionModel
from service.kernel.models import Bar, D, Instrument, Order, OrderType, RejectReason, Side

MODEL = BarExecutionModel()
SYMBOL = "600000.SH"
TS = datetime(2026, 10, 30, 10, 30, tzinfo=SHANGHAI)


def make_bar(*, open_: str, close: str = "", priced_at: str = "open",
             fill_ratio: float = 1.0) -> Bar:
    up = D("11.00")
    return Bar(
        symbol=SYMBOL,
        ts=TS,
        open=D(open_),
        high=D("11.00"),
        low=D("10.00"),
        close=D(close or open_),
        pre_close=D("10.00"),
        volume=5_000_000,
        amount=D("50000000"),
        up_limit=up,
        down_limit=D("9.00"),
        fill_ratio=fill_ratio,
        priced_at=priced_at,
    )


def market_buy(qty: int = 100) -> Order:
    return Order(order_id="O1", client_order_id="c1", symbol=SYMBOL,
                 side=Side.BUY, qty=qty, order_type=OrderType.MARKET)


def test_a_historical_bar_at_the_ceiling_reports_a_sealed_open():
    """09:30 already at the ceiling: the day never offered a crossing trade."""
    result = MODEL.match(market_buy(), make_bar(open_="11.00"), Instrument(symbol=SYMBOL), TS)

    assert result.reject_reason == RejectReason.OPEN_AT_LIMIT
    assert "opened at the upper limit" in result.detail


def test_a_live_quote_at_the_ceiling_reports_a_queue_not_a_closed_day():
    """Sealed at 10:30 is a queueing problem, not a session that opened sealed.

    The board can still open before 15:00, so telling the model the day opened
    sealed would make it abandon a trade that is still live.
    """
    result = MODEL.match(market_buy(),
                         make_bar(open_="11.00", priced_at="live"),
                         Instrument(symbol=SYMBOL), TS)

    assert result.reject_reason == RejectReason.QUEUE_NO_PRIORITY
    assert "queue position unknown" in result.detail
    assert "opened at the upper limit" not in result.detail


def test_a_live_quote_below_the_ceiling_still_fills_at_that_quote():
    """The label must not turn every live order into a queue: only the ceiling does."""
    result = MODEL.match(market_buy(),
                         make_bar(open_="10.50", priced_at="live"),
                         Instrument(symbol=SYMBOL), TS)

    assert result.filled, result.detail
    # 10.50 plus the model's 10bps adverse slippage, rounded to a fen.
    assert result.fill.price == D("10.51")


def test_a_bar_with_a_queue_ratio_fills_its_share_instead_of_refusing():
    """``derive_fill_ratio`` has no producer today; the branch must still work.

    If it ever gets one — a pre-open depth feed, or an explicit conservative
    assumption — the fill has to come through here rather than needing a second
    code path built under time pressure.
    """
    result = MODEL.match(market_buy(qty=800),
                         make_bar(open_="11.00", priced_at="live", fill_ratio=0.25),
                         Instrument(symbol=SYMBOL), TS)

    assert result.filled, result.detail
    # 800 x 0.25 = 200 shares, which is also a legal lot. A ratio that lands on a
    # non-lot quantity is floored by ``+LOT_ROUND`` — 1000 x 0.25 gave 250 and
    # became 200 — so the round number is deliberate, not incidental.
    assert result.fill.qty == 200
    assert result.fill.price == D("11.00")
    # The rule id names every adjustment that touched the fill. ``LIMIT_UP_QUEUE``
    # is the queue share; ``+LIMIT_UP_PENALTY`` is the configured cost of buying
    # into a sealed ceiling, which used to be declared and never charged.
    assert result.fill.rule_id == "LIMIT_UP_QUEUE+LIMIT_UP_PENALTY"
    # 2,200 notional (200 shares @ 11.00). `fees` is now exactly the three
    # CostModel lines: 5.00 commission + 0.02 transfer + 0.00 buy stamp = 5.02.
    #
    # CHANGED 9.42 -> 5.02 by the B1-a fix.
    #   Before: the 20bps queue penalty (2200 x 20/10000 = 4.40) was added into
    #   `fill.fees` in `_settle_fill`, so a queue-position cost was booked as a
    #   broker commission and lifted `fees_paid` / `avg_cost` / `realized_pnl`.
    #   After: 4.40 lives on `Fill.limit_up_penalty` (a CNY amount, never a bps
    #   rate). `book._record_fill` writes it to `state["fills"]` /
    #   `paper_fills.payload`; `tca.summary` reports it under `impact_bps`
    #   (4.40 / 2200 = 20.0 bps) instead of `fee_bps`. The 4.40 is no longer
    #   deducted from cash at all — it is a cost DISCLOSURE, not a payment:
    #   惩罚不再从现金扣款（`fill.fees` 只含三项），改为**成本披露**：金额落在
    #   `fill.limit_up_penalty`，由 `tca.impact_bps` 归集。跨版本比较 `equity` 时
    #   需扣回这 20bps（本例 4.40 元），否则改后净值会显得高 4.40 元。
    #   `rule_id` still marks it.
    assert result.fill.fees == D("5.02")
    assert result.fill.limit_up_penalty == D("4.40")
