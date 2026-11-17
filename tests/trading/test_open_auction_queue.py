"""09:25–09:30 下的单：按当日开盘价成交，跳空就放弃。

A 股这段是「接受委托、不撤单、09:30 开盘价成交」。内核拒绝在 PRE_OPEN *撮合*
（还没有价），但把这张单判 `MARKET_CLOSED` 退票是另一回事——09:26 那个窗口
产出的每一张票都会当场死掉。正确形状是排队，等当日 bar 落库后按 `bar.open` 结算。
"""

from __future__ import annotations

from datetime import datetime

from service.kernel.clock import SHANGHAI
from service.kernel.models import D
from tests.trading.test_book_execution_safety import (
    DAY1,
    SYMBOL,
    StubMarket,
    make_bar,
    make_book,
    ticket,
)


def _pre_open_book(tmp_path, market):
    book = make_book(tmp_path / "p.sqlite", market, "intraday")
    book.clock = lambda: datetime(2026, 1, 5, 9, 26, tzinfo=SHANGHAI)
    return book


def test_a_pre_open_ticket_waits_for_that_sessions_open(tmp_path):
    market = StubMarket()
    book = _pre_open_book(tmp_path, market)

    result = book.execute_ticket(ticket(limit="10.10"))

    assert result.status == "queued", result.reason
    assert len(book.state["pending"]) == 1
    assert book.ledger.account.peek(SYMBOL) is None, "还没开盘就持仓，是凭空成交"

    settled = book.settle_same_session_due(DAY1)

    assert [row["status"] for row in settled] == ["filled"]
    assert book.state["pending"] == []
    assert book.ledger.account.peek(SYMBOL).total_qty == 100
    assert book.state["fills"][0]["fill_basis"] == "open_auction"
    assert book.state["fills"][0]["price"] == "10.10"


def test_a_pre_open_buy_whose_open_gaps_above_the_limit_is_cancelled(tmp_path):
    market = StubMarket({DAY1: make_bar(DAY1, open_="10.50", high="10.60",
                                        low="9.80", close="10.20")})
    book = _pre_open_book(tmp_path, market)
    assert book.execute_ticket(ticket(limit="10.10")).status == "queued"

    settled = book.settle_same_session_due(DAY1)

    assert [row["status"] for row in settled] == ["cancelled"]
    assert "OPEN_ABOVE_LIMIT" in settled[0]["reason"]
    assert book.ledger.account.peek(SYMBOL) is None, "跳空后回落不是这张单能拿到的价"
    assert book.state["pending"] == []


def test_a_pre_open_sell_whose_open_below_the_limit_is_cancelled(tmp_path):
    market = StubMarket({DAY1: make_bar(DAY1, open_="9.90", high="10.40",
                                        low="9.80", close="10.00")})
    book = _pre_open_book(tmp_path, market)
    book.ledger.account.position(SYMBOL).total_qty = 100
    book.ledger.account.position(SYMBOL).available_qty = 100

    assert book.execute_ticket(ticket(side="SELL", limit="10.05")).status == "queued"
    settled = book.settle_same_session_due(DAY1)

    assert [row["status"] for row in settled] == ["cancelled"]
    assert "OPEN_BELOW_LIMIT" in settled[0]["reason"]
    assert book.ledger.account.peek(SYMBOL).total_qty == 100


def test_a_settlement_a_session_late_does_not_cost_an_extra_t_plus_one(tmp_path):
    """交易日是昨天、结算发生在今天 → 今天就能卖。

    ``_rollover`` 的顺序是「先 ``ledger.settle()``、再结算挂单」。所以一条在次日
    才被结算的盘前单，是在解锁之后才被记进 ``locked_qty`` 的——若不补这一步，
    份额要等到**再下一个** rollover 才可用：T+1 变成 T+2，第二天早上的止损会被
    内核以 T_PLUS_ONE 拒掉。
    """
    from datetime import datetime as _dt

    book = make_book(tmp_path / "p.sqlite", StubMarket(), "intraday")
    book.rollover(DAY1)
    book.clock = lambda: _dt(2026, 1, 5, 9, 26, tzinfo=SHANGHAI)   # DAY1 盘前
    assert book.execute_ticket(ticket(limit="10.10")).status == "queued"

    book.rollover("20260106")          # 次日首次调用才结算，成交日仍是 DAY1

    pos = book.ledger.account.peek(SYMBOL)
    assert book.state["fills"][0]["trade_date"] == DAY1
    assert (pos.total_qty, pos.available_qty, pos.locked_qty) == (100, 100, 0)
    assert pos.is_consistent()

    book.clock = lambda: _dt(2026, 1, 6, 10, 0, tzinfo=SHANGHAI)
    sold = book.execute_ticket(ticket("td-sell", side="SELL", qty=100,
                                      limit="10.00", day="20260106"))
    assert sold.status != "rejected" or "T_PLUS_ONE" not in (sold.reason or "")


def test_a_queued_buy_is_not_spendable_cash(tmp_path):
    """09:26 排队的买单不动账本余额，但也不算"能花的钱"。

    排队中的票没有订单、不冻结现金。只看 ``cash_available`` 的话，14:35 的窗口
    会把同一笔钱再承诺一次，而 17:30 结算时先下的那张被"现金不足"取消。
    """
    book = make_book(tmp_path / "p.sqlite", StubMarket(), "intraday")
    book.rollover(DAY1)
    book.clock = lambda: datetime(2026, 1, 5, 9, 26, tzinfo=SHANGHAI)
    assert book.execute_ticket(ticket(limit="40.00", qty=100)).status == "queued"

    summary = book.summary()

    assert D(summary["cash_available"]) == D("5000"), "账本余额不该被排队动作改动"
    assert D(summary["cash_committed"]) > D("4000"), "4000 本金 + 费用"
    assert D(summary["cash_free"]) < D("1000")
    assert D(summary["cash_committed"]) + D(summary["cash_free"]) == D("5000")


def test_a_ticket_does_not_pay_for_itself(tmp_path):
    """结算那一刻这张票还在 pending 里；占用若不排除自己，每张买单都会自撞。

    那就是"需要 1005、可用 1000"的循环：所有排队买单都会以 INSUFFICIENT_CASH
    被取消，包括本来买得起的那张。
    """
    book = make_book(tmp_path / "p.sqlite", StubMarket(), "intraday")
    book.rollover(DAY1)
    book.clock = lambda: datetime(2026, 1, 5, 9, 26, tzinfo=SHANGHAI)
    assert book.execute_ticket(ticket(limit="40.00", qty=100)).status == "queued"

    assert book.queued_buy_commitment() > D("4000")
    assert book.queued_buy_commitment(exclude="td-test") == D("0")
