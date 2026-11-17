"""队列单不能无限期地等一个已经过去的日子。

``settle_same_session_due`` 按 ``created_day`` 分组，把所有 ``created_day <= today``
的同日队列（ceiling / open / close）各结算一遍，代价是它不看 ``expires_day``——
而这三个队列的 ``expires_day`` 就是创建日。正常夜里跑一次没问题；引擎停摆一周
再起来，一周前那张"赌当天收盘"的票仍会在那天的价格上补出一笔，账户凭空多出
一个按一周前价格建仓的持仓，而当天的面板上没有任何地方说过这件事。

这里钉住上限：超过 ``QUEUE_TTL_SESSIONS`` 个交易日仍未结算的同日队列单，按
``QUEUE_EXPIRED`` 取消——与次日开盘单同一个窗口、同一个理由串。窗口内照旧结算，
因为日更迟到一两天是常态，不是故障。

两个用例必须一起看：只测"过期"会逼出一个把窗口内也一起取消的过修；只测"窗口内"
则旧代码本来就绿。
"""

from __future__ import annotations

from datetime import datetime

from service.kernel.clock import SHANGHAI
from service.trading.book import PaperBook
from tests.trading.test_book_execution_safety import (
    DAY2,
    SYMBOL,
    StubMarket,
    make_bar,
    make_book,
    ticket,
)


def _at(book: PaperBook, hour: int, minute: int = 0) -> PaperBook:
    book.clock = lambda: datetime(2026, 1, 6, hour, minute, tzinfo=SHANGHAI)
    return book


def _queued_close(book: PaperBook, ticket_id: str) -> None:
    """一张 09:26 下的、赌当日收盘价的单，当天 bar 还没落库。"""
    assert book.execute_ticket(ticket(ticket_id, day=DAY2)).status == "queued"


def test_a_stale_same_session_queue_expires_instead_of_filling(tmp_path):
    """停摆一周后回来：那天的 bar 在，也不许按它的收盘价补一笔。"""
    market = StubMarket(bars={})
    book = _at(make_book(tmp_path / "p.sqlite", market, "close"), 9, 26)
    book.rollover(DAY2)
    _queued_close(book, "td-stale")

    # 引擎停了七天，日更把那一周的 bar 全补上了——包括这张单赌的那一天。
    market.bars[DAY2] = make_bar(DAY2, close="10.20")
    results = book.settle_same_session_due("20260113")

    stored = book.store.get_ticket("td-stale")
    assert stored["status"] == "cancelled"
    assert any("QUEUE_EXPIRED" in reason for reason in stored["reasons"])
    assert [row["status"] for row in results] == ["cancelled"]
    # 钱和股都还在原处：没有按一周前的 10.20 建仓。
    assert book.ledger.account.position(SYMBOL).total_qty == 0
    assert book.ledger.account.cash_frozen == 0


def test_a_late_bar_within_the_ttl_still_settles(tmp_path):
    """迟到三个交易日内照旧结算：日更晚一两天是常态，不许把它当陈旧单取消。"""
    market = StubMarket(bars={})
    book = _at(make_book(tmp_path / "p.sqlite", market, "close"), 9, 26)
    book.rollover(DAY2)
    _queued_close(book, "td-late")

    # 20260106 + QUEUE_TTL_SESSIONS(3) = 20260109，正好在窗口的最后一格。
    market.bars[DAY2] = make_bar(DAY2, close="10.20")
    results = book.settle_same_session_due("20260109")

    assert [row["status"] for row in results] == ["filled"]
    assert results[0]["price"] == "10.20"
    assert book.store.get_ticket("td-late")["status"] == "filled"
    assert book.ledger.account.position(SYMBOL).total_qty == 100
