"""``close`` 模式的盘前/盘中：排到当日收盘，而不是判"市场拒绝"。

2026-11-04 修 09:25–09:30 那一格时只收了 ``intraday`` 与 ``next_open`` 两条路：
它们排队为 ``open``，按当日开盘价结算。``close`` 模式走的是
``_execute_at_close``，而那条路在当日 bar 还没落库时直接返回终态
``NO_BAR``——09:26 下的单在那一刻就死了，尽管八个半小时后它要赌的那个收盘价
会准时印出来。同一形状还有更宽的一面：``close`` 模式在盘中任何时刻执行都读
不到当日 bar，所以它其实只在 17:30 之后可用，而没有任何地方说过这件事。

这里钉住的是：数据没到 ≠ 市场拒绝。``close`` 票进"当日收盘结算"队列，
``settle_same_session_due`` 在 bar 落库后按收盘价结；那天真的没有 bar，就按
过期取消（可核对），绝不改用次日开盘价。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from service.kernel.clock import SHANGHAI
from service.kernel.models import D
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


def _pending(book: PaperBook, ticket_id: str) -> dict | None:
    return next((row for row in book.state["pending"]
                 if row.get("ticket_id") == ticket_id), None)


@pytest.mark.parametrize(("mode", "kind"), [
    ("next_open", "open"),      # 09:26 的下一日开盘单也加入当天开盘集合
    ("intraday", "open"),       # 当日开盘
    ("close", "close"),         # 当日收盘
])
def test_every_mode_queues_at_0926_instead_of_dying(tmp_path, mode, kind):
    """三种模式 × 09:26：都没有当日 bar，都不许落终态。"""
    market = StubMarket(bars={})          # 日更还没跑，当天一根 bar 都没有
    book = _at(make_book(tmp_path / "p.sqlite", market, mode), 9, 26)

    result = book.execute_ticket(ticket("td-0926", day=DAY2))

    assert result.ok is True
    assert result.status == "queued"
    assert _pending(book, "td-0926") is not None
    assert _pending(book, "td-0926").get("queue_kind") == kind
    assert (book.store.get_ticket("td-0926") or {}).get("status") == "queued"


def test_close_queue_settles_at_the_close_print(tmp_path):
    """bar 落库后按当日收盘价成交，且成交记录自己说得出这是 close。"""
    market = StubMarket(bars={})
    book = _at(make_book(tmp_path / "p.sqlite", market, "close"), 9, 26)
    assert book.execute_ticket(ticket("td-close", day=DAY2)).status == "queued"
    entry = _pending(book, "td-close")
    # 只赌这一天：target 与 expires 都是创建日，不是"下个交易日"。
    assert entry["target_day"] == DAY2
    assert entry["expires_day"] == DAY2

    market.bars[DAY2] = make_bar(DAY2, close="10.20")
    results = book.settle_same_session_due(DAY2)

    assert [row["status"] for row in results] == ["filled"]
    assert results[0]["price"] == "10.20"
    stored = book.store.get_ticket("td-close")
    assert stored["status"] == "filled"
    assert stored["fill"]["basis"] == "close"
    # 不是开盘价：这根 bar 的开盘是 10.10，用错那一侧的票会在这里露出来。
    assert stored["fill"]["price"] == "10.20"
    assert book.ledger.account.position(SYMBOL).total_qty == 100


def test_close_mode_intraday_without_a_bar_also_waits(tmp_path):
    """10:30 同样没有当日 bar。旧代码在这一格也给终态 NO_BAR。"""
    market = StubMarket(bars={})
    book = _at(make_book(tmp_path / "p.sqlite", market, "close"), 10, 30)

    result = book.execute_ticket(ticket("td-1030", day=DAY2))

    assert result.ok is True
    assert result.status == "queued"
    stored = book.store.get_ticket("td-1030")
    assert stored["status"] == "queued"
    assert "reasons" not in stored            # 没有被判过任何终态理由


def test_close_limit_on_the_wrong_side_cancels_instead_of_repricing(tmp_path):
    """限价 10.00、收盘 10.20：没成交，不是"按 10.00 记一笔"。"""
    market = StubMarket(bars={})
    book = _at(make_book(tmp_path / "p.sqlite", market, "close"), 9, 26)
    assert book.execute_ticket(ticket("td-limit", day=DAY2, limit="10.00")).status == "queued"

    market.bars[DAY2] = make_bar(DAY2, close="10.20")
    results = book.settle_same_session_due(DAY2)

    assert [row["status"] for row in results] == ["cancelled"]
    assert "PRICE_LIMIT:close" in results[0]["reason"]
    assert book.ledger.account.position(SYMBOL).total_qty == 0
    assert book.ledger.account.cash_frozen == 0


def test_close_queue_expires_rather_than_borrowing_the_next_session(tmp_path):
    """当日 bar 永远没到、次日 bar 到了：取消，而不是按次日开盘价成交。"""
    market = StubMarket(bars={})
    book = _at(make_book(tmp_path / "p.sqlite", market, "close"), 9, 26)
    book.rollover(DAY2)                      # 账本先落在当天
    assert book.execute_ticket(ticket("td-gone", day=DAY2)).status == "queued"

    # 次日：日更跳过了前一天，只写了次日的 bar。
    market.bars["20260107"] = make_bar("20260107", open_="10.60", close="10.70")
    book.rollover("20260107")

    stored = book.store.get_ticket("td-gone")
    assert stored["status"] == "cancelled"
    assert any("QUEUE_EXPIRED" in reason for reason in stored["reasons"])
    assert _pending(book, "td-gone") is None
    assert book.ledger.account.position(SYMBOL).total_qty == 0


def test_close_mode_with_the_bar_already_published_still_fills_now(tmp_path):
    """18:50 执行（bar 已在）：不排队，直接成交——排队分支不能吞掉正常路径。"""
    market = StubMarket(bars={DAY2: make_bar(DAY2, close="10.20")})
    book = _at(make_book(tmp_path / "p.sqlite", market, "close"), 18, 50)

    result = book.execute_ticket(ticket("td-now", day=DAY2))

    assert result.ok is True
    assert result.status == "filled"
    assert result.price == D("10.20")
    assert _pending(book, "td-now") is None
    assert book.store.get_ticket("td-now")["fill"]["basis"] == "close"


def test_close_queue_sell_uses_the_close_and_sells_nothing_early(tmp_path):
    """卖出侧同一把尺子：排队期间一股不动，结算按收盘价卖。"""
    market = StubMarket(bars={})
    book = _at(make_book(tmp_path / "p.sqlite", market, "close", shares=300), 9, 26)

    assert book.execute_ticket(
        ticket("td-sell", side="SELL", qty=300, limit="10.00", day=DAY2)).status == "queued"
    held = book.ledger.account.position(SYMBOL)
    assert held.total_qty == 300            # 还没卖

    market.bars[DAY2] = make_bar(DAY2, close="10.20")
    results = book.settle_same_session_due(DAY2)

    assert [row["status"] for row in results] == ["filled"]
    assert results[0]["price"] == "10.20"
    assert book.ledger.account.position(SYMBOL).total_qty == 0


def test_settle_pending_leaves_close_tickets_to_the_close_pass(tmp_path):
    """两条结算通道不许串：有 bar 也不能用开盘规则结 close 单。

    这条专盯"队列进了、basis 丢了"的半修。``close`` 若不在
    ``SAME_SESSION_QUEUES`` 里，``settle_pending`` 会按 ``target_day`` 调
    ``_try_fill(basis="next_open")``——成交价会是 10.10（开盘）而不是 10.20
    （收盘），``fill_basis`` 也记成 ``next_open``。两个错都只在数据迟到那天
    出现，正是最容易漏测的那一侧。
    """
    market = StubMarket(bars={})
    book = _at(make_book(tmp_path / "p.sqlite", market, "close"), 9, 26)
    assert book.execute_ticket(ticket("td-basis", day=DAY2)).status == "queued"

    market.bars[DAY2] = make_bar(DAY2, open_="10.10", close="10.20")
    book.settle_pending(DAY2)

    assert _pending(book, "td-basis") is not None        # 开盘通道没有碰它
    assert (book.store.get_ticket("td-basis") or {}).get("fill") is None

    results = book.settle_same_session_due(DAY2)
    assert [row["status"] for row in results] == ["filled"]
    stored = book.store.get_ticket("td-basis")
    assert stored["fill"]["basis"] == "close"
    assert stored["fill"]["price"] == "10.20"
