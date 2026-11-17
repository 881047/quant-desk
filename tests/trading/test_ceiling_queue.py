"""天花板单：当日以涨停价挂单，收盘后按"开没开板 + 封单/成交额"结算。

这一层存在的理由，是 ``next_open`` 与打板根本是两件事：前者在次日开盘买，
正是被证伪的那个动作；而 ``intraday`` 在挂单那一刻就判"封死、排队未知"而拒绝，
所以打板在这套系统里此前**没有任何一条能成交的路径**。

``execution`` 里早就留好了分支（``fill_ratio < 1`` 时买单以涨停价成交那一份），
只是从来没有 builder 提供过这个比例。这里验证三件事：

* 全天没开板 → 不成交，理由可核对；
* 开过板 → 只成交排队比例那一份，不是全量；
* 没有排队数据 → 保守拒绝，不编一个"排到了"。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from service.kernel.clock import SHANGHAI
from service.kernel.models import D
from service.trading.book import PaperBook
from service.trading.state import PaperStore
from tests.trading.test_book_execution_safety import (
    DAY1,
    DAY2,
    SYMBOL,
    StubMarket,
    make_bar,
    make_book,
    ticket,
)


class _PoolStore:
    """只回答涨停池相关两张表的最小数据集仓库。"""

    def __init__(self, frame, derived=None):
        self._frame = frame
        self._derived = derived

    def read(self, name, start=None, end=None, columns=None):
        if name == "limit_up_pool":
            return self._frame
        if name == "limit_pool_t0" and self._derived is not None:
            return self._derived
        raise KeyError(name)


def _market(open_times, seal, amount, *, low: str):
    """收盘封在涨停价的一天。``low`` 决定它是"开过板"还是"一字板"。"""
    bar = make_bar(DAY1, open_="11.00", high="11.00", low=low, close="11.00")
    market = StubMarket({DAY1: bar})
    market.store = _PoolStore(pd.DataFrame({
        "code": [SYMBOL.split(".")[0]],
        "open_times": [open_times],
        "seal_amount": [seal],
        "amount": [amount],
    }))
    return market


def _book(db, market, *, last: str | None = "11.00", ceiling: str = "11.00"):
    """limit_queue 模式的账本。``last`` 是"此刻的价"，用来判有没有封住。"""
    quotes = None
    if last is not None:
        quotes = (lambda syms: {s: SimpleNamespace(last=D(last), up_limit=D(ceiling))
                                for s in syms})
    book = PaperBook(PaperStore(db), market, initial_cash="5000",
                     fill_mode="limit_queue", quotes=quotes)
    book.clock = lambda: datetime(2026, 1, 5, 10, 30, tzinfo=SHANGHAI)
    return book


def test_a_sealed_board_never_fills(tmp_path):
    """全天没开板：排队买不到就是买不到。"""
    market = _market(open_times=0, seal=1e8, amount=2e7, low="11.00")
    book = _book(tmp_path / "p.sqlite", market)

    queued = book.execute_ticket(ticket(limit="11.00"))
    assert queued.status == "queued"

    results = book.settle_ceiling(DAY1)
    assert len(results) == 1
    assert results[0]["status"] == "cancelled"
    assert "OPEN_AT_LIMIT" in results[0]["reason"]
    held = book.ledger.account.position(SYMBOL)
    assert held is None or held.total_qty == 0


def test_an_opened_board_fills_only_the_queue_fraction(tmp_path):
    """开过板：成交额 2e7、封单 1e6 → 原始比例 0.952，400 股成交 300 股。"""
    market = _market(open_times=2, seal=1e6, amount=2e7, low="10.60")
    book = _book(tmp_path / "p.sqlite", market)

    assert book.execute_ticket(ticket(limit="11.00", qty=400)).status == "queued"
    results = book.settle_ceiling(DAY1)

    assert results[0]["status"] == "filled"
    position = book.ledger.account.position(SYMBOL)
    # 400 × (2e7 / 2.1e7) = 380 → 整手 300
    assert position.total_qty == 300
    # 成交价是涨停价；均价因双边费用略高于它
    assert float(position.avg_cost) >= 11.00


def test_a_bigger_seal_fills_less(tmp_path):
    """封单越厚排得越后，成交越少——这正是这个比例存在的意义。"""
    thin = _market(open_times=1, seal=1e6, amount=2e7, low="10.60")
    thick = _market(open_times=1, seal=4e8, amount=2e7, low="10.60")

    thin_book = _book(tmp_path / "a.sqlite", thin)
    thick_book = _book(tmp_path / "b.sqlite", thick)
    thin_book.execute_ticket(ticket(limit="11.00", qty=400))
    thick_book.execute_ticket(ticket(limit="11.00", qty=400))
    thin_book.settle_ceiling(DAY1)
    thick_book.settle_ceiling(DAY1)

    thin_pos = thin_book.ledger.account.position(SYMBOL)
    thick_pos = thick_book.ledger.account.position(SYMBOL)
    # 薄封单 0.952 → 380 股；厚封单 0.0476 → 19 股 → 一手都买不到
    assert thin_pos.total_qty == 300
    assert thick_pos is None or thick_pos.total_qty < 300


def test_without_queue_data_the_order_is_refused(tmp_path):
    """没有封单数据时不许假设成交：``fill_ratio`` 保持 1.0，撮合器保守拒绝。"""
    bar = make_bar(DAY1, open_="11.00", high="11.00", low="10.60", close="11.00")
    market = StubMarket({DAY1: bar})          # 故意不给 store
    book = _book(tmp_path / "p.sqlite", market)

    assert book.execute_ticket(ticket(limit="11.00")).status == "queued"
    results = book.settle_ceiling(DAY1)

    assert results[0]["status"] == "cancelled"
    held = book.ledger.account.position(SYMBOL)
    assert held is None or held.total_qty == 0


def test_settlement_is_idempotent(tmp_path):
    """同一天结算两次，不能成交两次。"""
    market = _market(open_times=1, seal=1e6, amount=2e7, low="10.60")
    book = _book(tmp_path / "p.sqlite", market)
    book.execute_ticket(ticket(limit="11.00", qty=400))

    first = book.settle_ceiling(DAY1)
    second = book.settle_ceiling(DAY1)

    assert first and first[0]["status"] == "filled"
    assert second == []
    assert book.ledger.account.position(SYMBOL).total_qty == 300


def test_pending_next_open_orders_are_untouched(tmp_path):
    """天花板单的结算不能顺手把次日开盘单也结算了。"""
    market = _market(open_times=1, seal=1e7, amount=2e7, low="10.60")
    book = make_book(tmp_path / "p.sqlite", market, "next_open")

    assert book.execute_ticket(ticket(limit="10.20")).status == "queued"
    assert book.settle_ceiling(DAY1) == []
    assert book.state["pending"], "次日开盘单必须留在队列里"


def test_expected_fill_reports_the_gap_between_frozen_and_filled(tmp_path):
    """模型按 qty 算风险，实际只成交一部分——这个差必须能被读出来。"""
    market = _market(open_times=1, seal=1e6, amount=2e7, low="10.60")
    book = _book(tmp_path / "p.sqlite", market)

    # 2e7/(2e7+1e6) = 0.952。内核给原始值，不打折——打折是读的人的判断。
    assert book.expected_ceiling_fill(SYMBOL, DAY1) == pytest.approx(0.952, abs=0.01)


def test_expected_fill_is_none_when_the_board_never_opened(tmp_path):
    """全天封死时不是"概率低"，是排队买不到——返回 None 而不是 0。"""
    market = _market(open_times=0, seal=1e6, amount=2e7, low="11.00")
    book = _book(tmp_path / "p.sqlite", market)

    assert book.expected_ceiling_fill(SYMBOL, DAY1) is None


def test_an_unsealed_open_fills_at_the_market_price(tmp_path):
    """挂涨停价单、但此刻市价远低于它 → 成交在**市价**，不是涨停价。

    这是交易所规则，不是内核的判断：限价高于市价时，买单会以市价成交。
    所以对一只开盘 16.38、收盘涨停 19.51 的票，挂 19.51 的买单成交在 16.38——
    那不是打板，是开盘买入。**内核不拦这一单**：挂什么价是调用方的决定，
    内核只负责按限价撮合并把成交价如实写进记录，读的人自己看得出差价。
    """
    from datetime import datetime as _dt

    from service.kernel.clock import SHANGHAI as _SH
    from service.kernel.models import Bar
    from service.kernel.models import D as _D

    ts = _dt.strptime(DAY1, "%Y%m%d").replace(hour=15, tzinfo=_SH)
    bar = Bar(symbol=SYMBOL, ts=ts, open=_D("16.38"), high=_D("19.51"),
              low=_D("16.10"), close=_D("19.51"), pre_close=_D("17.74"),
              volume=5_000_000, amount=_D("19.51") * 5_000_000,
              up_limit=_D("19.51"), down_limit=_D("14.53"))
    market = StubMarket({DAY1: bar})
    market.store = _PoolStore(
        pd.DataFrame({"code": [SYMBOL.split(".")[0]], "open_times": [0.0],
                      "seal_amount": [3e8], "amount": [1e8]}),
        derived=pd.DataFrame({"code": [SYMBOL], "pool": ["limit_up"],
                              "open": [16.38], "limit_price": [19.51]}))

    book = _book(tmp_path / "p.sqlite", market, last="16.38", ceiling="19.51")
    assert book.execute_ticket(ticket(limit="19.51", qty=100)).status == "queued"

    results = book.settle_ceiling(DAY1)
    assert results[0]["status"] == "filled"
    position = book.ledger.account.position(SYMBOL)
    assert position.total_qty == 100
    # 成交价贴着开盘价，离涨停价还差 15% 以上——成交记录本身说明了这件事
    assert float(position.avg_cost) < 17.0


def test_queueing_does_not_need_a_live_quote(tmp_path):
    """挂单不需要实时报价：内核不判断"该不该挂"，只按限价撮合。"""
    market = _market(open_times=1, seal=1e6, amount=2e7, low="10.60")
    book = _book(tmp_path / "p.sqlite", market, last=None)

    assert book.execute_ticket(ticket(limit="11.00", qty=400)).status == "queued"
    results = book.settle_ceiling(DAY1)
    assert results[0]["status"] == "filled"
    assert book.ledger.account.position(SYMBOL).total_qty == 300


def test_the_next_rollover_settles_before_it_expires_the_ticket(tmp_path):
    """必须"先结算、后过期"，否则天花板单永远走不到结算。

    当日 17:35 的日更没有跑（服务停了、数据晚了）时，唯一的机会就是次日开盘。
    修改前 ``settle_pending`` 先把 ``expires_day=创建日`` 的 ceiling 单按
    ``QUEUE_EXPIRED`` 取消，``settle_ceiling_due`` 再跑时已经无单可结算——
    打板这条路在生产里从未成交过。
    """
    market = _market(open_times=2, seal=1e6, amount=2e7, low="10.60")
    book = _book(tmp_path / "p.sqlite", market)
    assert book.execute_ticket(ticket(limit="11.00", qty=400)).status == "queued"

    book.rollover(DAY1)
    assert book.state["pending"], "创建当天不该结算：收盘前拿不到封单与开板次数"

    book.rollover(DAY2)

    assert book.state["pending"] == []
    assert book.ledger.account.peek(SYMBOL).total_qty == 300
    assert book.state["fills"][0]["fill_basis"] == "ceiling_queue"


def test_a_missing_limit_up_pool_waits_instead_of_cancelling(tmp_path):
    """池子还没落库 ≠ 排不上队；数据晚到之后必须还能结算。

    17:30 的任务按 dataset 逐个写，日线和涨停池不是同一秒到。旧行为在池子
    缺失时返回"没有比例"，撮合器据此判 ``OPEN_AT_LIMIT``——那是一个**终态**
    取消，数据五分钟后再到也补不回来。
    """
    market = _market(open_times=2, seal=1e6, amount=2e7, low="10.60")
    market.store = _PoolStore(pd.DataFrame())        # 池子目录还没写
    book = _book(tmp_path / "p.sqlite", market)
    assert book.execute_ticket(ticket(limit="11.00", qty=400)).status == "queued"

    assert book.settle_same_session_due(DAY1) == []
    assert book.summary()["pending"], "数据没到不能当成'排不上队'"
    assert book.ledger.account.peek(SYMBOL) is None

    # 池子到了之后，同一条待结算的单必须还能成交。
    market.store = _PoolStore(pd.DataFrame({
        "code": [SYMBOL.split(".")[0]],
        "open_times": [2],
        "seal_amount": [1e6],
        "amount": [2e7],
    }))
    settled = book.settle_same_session_due(DAY1)

    assert [row["status"] for row in settled] == ["filled"], settled
    # 400 × 成交额/(成交额+封单额) = 380.8，按手取整 300
    assert book.ledger.account.peek(SYMBOL).total_qty == 300


def test_a_late_opening_pool_does_not_cancel_the_queue(tmp_path):
    """开盘涨停池晚到 ≠ 买不到：同一个形状的第二份数据源。

    11.3 只把 ``limit_up_pool`` 纳入了"缺数据要等"的门槛，而判定"开盘在不在
    涨停上"还要读 ``limit_pool_t0``。日更按 dataset 逐个写，两份池子的到达顺序
    不固定；后到的那份缺席时，``open_times=0`` 的票会被判成 ``OPEN_AT_LIMIT``
    并**终态**取消——池子几分钟后再到也补不回来。

    这里让日线缺涨跌停价（``up_limit=0``，表示未知），逼内核去问开盘池。
    """
    from dataclasses import replace

    bar = make_bar(DAY1, open_="11.00", high="11.00", low="10.60", close="11.00")
    bar = replace(bar, up_limit=D("0"))
    market = StubMarket({DAY1: bar})
    market.store = _PoolStore(
        pd.DataFrame({"code": [SYMBOL.split(".")[0]], "open_times": [2],
                      "seal_amount": [1e6], "amount": [2e7]}),
        derived=None)                     # limit_pool_t0 还没落库

    book = _book(tmp_path / "p.sqlite", market)
    assert book.execute_ticket(ticket(limit="11.00", qty=400)).status == "queued"

    assert book.settle_same_session_due(DAY1) == []
    assert book.summary()["pending"], "判不出来的时候必须等，不是取消"
    assert book.ledger.account.peek(SYMBOL) is None

    # 开盘池到了：开盘就在涨停上、当天开过两次板 → 按排队比例成交。
    market.store = _PoolStore(
        pd.DataFrame({"code": [SYMBOL.split(".")[0]], "open_times": [2],
                      "seal_amount": [1e6], "amount": [2e7]}),
        derived=pd.DataFrame({"code": [SYMBOL], "pool": ["limit_up"],
                              "open": [11.00], "limit_price": [11.00]}))
    settled = book.settle_same_session_due(DAY1)

    assert [row["status"] for row in settled] == ["filled"], settled
    assert book.ledger.account.peek(SYMBOL).total_qty == 300
