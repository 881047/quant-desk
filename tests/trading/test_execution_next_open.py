"""Execution fidelity: a decision made at the close must not fill at that close.

These tests use synthetic bars so they can control the overnight gap, which is
exactly the variable the old same-bar fill was hiding.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.models import Bar, D, Instrument
from service.trading.book import PaperBook
from service.trading.market import Snapshot
from service.trading.state import PaperStore

DAY1, DAY2, DAY3 = "20260105", "20260106", "20260107"
SYMBOL = "600000.SH"


def make_bar(day: str, *, open_: str, high: str, low: str, close: str,
             pre_close: str = "10.00", volume: int = 5_000_000) -> Bar:
    ts = datetime.strptime(day, "%Y%m%d").replace(hour=15, tzinfo=SHANGHAI)
    return Bar(
        symbol=SYMBOL,
        ts=ts,
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        pre_close=D(pre_close),
        volume=volume,
        amount=D(close) * volume,
        up_limit=D(pre_close) * D("1.10"),
        down_limit=D(pre_close) * D("0.90"),
    )


class _Cal:
    def calendar(self):
        return TradingCalendar.always()

    def is_trading_day(self, day: str) -> bool:
        return True

    def shift(self, day: str, n: int) -> str:
        from datetime import timedelta

        return (datetime.strptime(day, "%Y%m%d") + timedelta(days=n)).strftime("%Y%m%d")


class StubMarket:
    """Only the surface PaperBook touches: bar, snapshot, calendar, last day."""

    def __init__(self, bars: dict[str, Bar], last: str):
        self.bars = bars
        self.calendar = _Cal()
        self.last = last
        self.actions_map: dict[str, list] = {}

    def actions(self, day: str):
        return self.actions_map.get(day, [])

    def bar(self, symbol: str, date: str):
        bar = self.bars.get(date)
        if bar is None or symbol != SYMBOL:
            return None
        return bar, Instrument(symbol=symbol, name="stub", min_qty=100, qty_step=100)

    def snapshot(self, symbol: str, date: str | None = None):
        bar = self.bars.get(date or self.last)
        if bar is None or symbol != SYMBOL:
            return None
        return Snapshot(symbol=symbol, trade_date=date or self.last, last=bar.close,
                        pre_close=bar.pre_close, open=bar.open, high=bar.high,
                        low=bar.low, up_limit=bar.up_limit, down_limit=bar.down_limit,
                        volume=float(bar.volume), amount=float(bar.amount),
                        suspended=bar.suspended, source="stub")

    def last_trading_day(self, as_of: str | None = None) -> str:
        return self.last

    def names(self) -> dict[str, str]:
        return {SYMBOL: "stub"}


def ticket(qty: int = 100, limit: str = "10.20", ticket_id: str = "td-test") -> dict:
    return {
        "ticket_id": ticket_id,
        "symbol": SYMBOL,
        "side": "BUY",
        "qty": qty,
        "limit_price": limit,
        "trade_date": DAY1,
        "sleeve": "CORE",
        "intent_type": "test",
        "thesis": "测试次日开盘成交的契约，确保决策所用的收盘价不会被当成成交价。",
        "invalidation": "若成交价等于决策日收盘价，则本测试失败",
        "stop_price": "9.50",
        "stop_pct": 0.05,
        "created_at": "2026-01-05T17:00:00+08:00",
        "status": "proposed",
    }


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setenv("FILL_MODE", "next_open")
    bars = {
        DAY1: make_bar(DAY1, open_="9.95", high="10.05", low="9.90", close="10.00"),
        DAY2: make_bar(DAY2, open_="10.10", high="10.30", low="10.05", close="10.20",
                       pre_close="10.00"),
    }
    store = PaperStore(tmp_path / "paper.sqlite")
    return PaperBook(store, StubMarket(bars, DAY2), initial_cash="5000")


def test_a_ticket_is_queued_not_filled_at_the_decision_close(book):
    book.rollover(DAY1)
    result = book.execute_ticket(ticket())

    assert result.status == "queued"
    assert book.ledger.account.position(SYMBOL).total_qty == 0
    assert book.summary()["pending"][0]["symbol"] == SYMBOL


def test_the_queue_fills_at_the_next_open_with_slippage(book):
    book.rollover(DAY1)
    book.execute_ticket(ticket())

    book.rollover(DAY2)  # settles the queue at DAY2's open

    pos = book.ledger.account.position(SYMBOL)
    assert pos.total_qty == 100
    assert pos.available_qty == 0, "T+1: shares bought today are not sellable today"
    fill = book.state["fills"][-1]
    # 10.10 open + 10 bps slippage = 10.11, not the 10.00 decision close
    assert D(fill["price"]) == pytest.approx(D("10.11"))
    assert fill["fill_basis"] == "next_open"
    assert fill["trade_date"] == DAY2


def test_a_gap_through_the_limit_cancels_instead_of_chasing(book):
    book.market.bars[DAY2] = make_bar(DAY2, open_="10.60", high="10.80", low="10.50",
                               close="10.70", pre_close="10.00")
    book.rollover(DAY1)
    book.execute_ticket(ticket(limit="10.20"))

    book.rollover(DAY2)

    assert book.ledger.account.position(SYMBOL).total_qty == 0
    assert book.summary()["pending"] == []
    rows = book.store.list_decisions(limit=10)
    assert any(r.get("kind") == "cancel" for r in rows), "a cancelled entry must be audited"


def test_a_late_bar_still_prices_at_the_session_the_ticket_bet_on(book):
    """DAY2 的 bar 晚到、DAY3 才结算时，成交价仍必须是 DAY2 的开盘价。

    这条曾经按结算当天的 bar 取价：数据晚一天，"次日开盘"单就按**后一天**的
    开盘成交。一张在 10.60 开盘时本该被取消的单，会在 10.50"成交"——一个当天
    根本没出现过的价格。所以才在排队时把目标交易日钉进 entry。
    """
    book.rollover(DAY1)
    book.execute_ticket(ticket())

    book.rollover(DAY3)  # DAY2 的 bar 迟到，但价格就该用它

    fill = book.state["fills"][-1]
    assert fill["trade_date"] == DAY2
    assert D(fill["price"]) == pytest.approx(D("10.11"))
    assert book.ledger.account.position(SYMBOL).total_qty == 100


def test_a_session_whose_bar_never_arrived_is_cancelled_not_re_priced(book):
    """目标日的 bar 真的不存在（停牌、分区缺口）→ 取消，绝不改按后一天的价格成交。

    判据是"后一个交易日有 bar"：那证明管线已经越过目标日，缺的就是缺的。
    """
    book.market.bars[DAY3] = make_bar(DAY3, open_="10.40", high="10.60",
                                      low="10.30", close="10.50", pre_close="10.20")
    del book.market.bars[DAY2]
    book.rollover(DAY1)
    book.execute_ticket(ticket())

    book.rollover(DAY3)

    assert book.summary()["pending"] == []
    assert book.state["fills"] == []
    assert book.store.get_ticket("td-test")["status"] == "cancelled"


def test_a_missing_bar_alone_does_not_cancel_the_ticket(book):
    """只缺 bar、没有更晚的 bar 作证时，只能等——"数据没到"不等于"这张单没成交"。"""
    del book.market.bars[DAY2]
    book.rollover(DAY1)
    book.execute_ticket(ticket())

    book.rollover(DAY3)

    assert book.summary()["pending"], "must not cancel a ticket just because data is late"
    assert book.ledger.account.position(SYMBOL).total_qty == 0


def test_a_one_word_limit_up_cannot_be_bought(book):
    up = D("11.00")
    bar = make_bar(DAY2, open_="11.00", high="11.00", low="11.00", close="11.00",
                   pre_close="10.00")
    book.market.bars[DAY2] = bar
    book.rollover(DAY1)
    book.execute_ticket(ticket(limit="11.00"))

    book.rollover(DAY2)

    assert book.ledger.account.position(SYMBOL).total_qty == 0
    assert book.summary()["pending"] == []
    assert up == D("11.00")


def test_queue_is_idempotent(book):
    book.rollover(DAY1)
    first = book.execute_ticket(ticket())
    second = book.execute_ticket(ticket())

    assert first.status == second.status == "queued"
    assert len(book.summary()["pending"]) == 1


def test_a_ticket_created_today_does_not_use_todays_open(book):
    """Queueing on the same session must not fill at a price that already happened."""
    book.rollover(DAY1)
    book.execute_ticket(ticket())          # created_day = DAY1

    book.rollover(DAY1)                    # same session again (e.g. a late API read)

    assert book.ledger.account.position(SYMBOL).total_qty == 0
    assert book.summary()["pending"], "the ticket must still be waiting for DAY2"


def test_a_late_bar_still_fills_yesterdays_ticket(book):
    """When the bar arrives after the date already rolled, the queue must settle."""
    book.rollover(DAY1)
    book.execute_ticket(ticket())

    book.rollover(DAY2)                    # no bar path is exercised: DAY2 exists
    book.rollover(DAY2)                    # second read on the same day must be a no-op

    assert book.ledger.account.position(SYMBOL).total_qty == 100
    assert len(book.state["fills"]) == 1


def test_impact_grows_with_size_and_splits_above_thirty_percent():
    from service.trading.impact import estimate

    small = estimate(1_000, 10.0, adv_cny=50_000_000, sigma_daily=0.02)
    large = estimate(2_000_000, 10.0, adv_cny=50_000_000, sigma_daily=0.02)

    assert large.impact_bps > small.impact_bps
    assert large.days_needed > 1
    assert sum(large.schedule) == large.qty
    assert large.participation <= 0.30 + 1e-9
    assert small.days_needed == 1


def test_split_impact_is_repriced_per_day_and_aggregated():
    from service.trading.impact import estimate

    out = estimate(2_000_000, 10.0, adv_cny=50_000_000, sigma_daily=0.02)
    expected_bps = 0.4 * 0.02 * (0.2 ** 0.5) * 10_000
    assert out.schedule == [1_000_000, 1_000_000]
    assert out.participation == pytest.approx(0.2)
    assert out.impact_bps == pytest.approx(expected_bps)
    assert out.cost_cny == pytest.approx(2 * 1_000_000 * 10 * expected_bps / 10_000)


@pytest.mark.parametrize("field", ["qty", "price", "adv_cny", "sigma_daily", "c", "max_participation"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_impact_rejects_non_finite_inputs(field, bad):
    from service.trading.impact import estimate

    kwargs = {"qty": 1000, "price": 10.0, "adv_cny": 50_000_000, "sigma_daily": 0.02,
              "c": 0.4, "max_participation": 0.3}
    kwargs[field] = bad
    with pytest.raises(ValueError, match="finite"):
        estimate(**kwargs)


@pytest.mark.parametrize("cap", [0.0, -0.1])
def test_impact_requires_positive_participation_cap(cap):
    from service.trading.impact import estimate

    with pytest.raises(ValueError, match="positive"):
        estimate(1000, 10.0, 50_000_000, 0.02, max_participation=cap)


class _VetoGuard:
    """Allows the first N fills, vetoes the rest — stands in for a real cap."""

    def __init__(self, allow: int):
        self.allow = allow
        self.calls = 0

    def preflight(self, book, **kwargs):
        from service.trading.guard import Check, GuardResult

        self.calls += 1
        ok = self.calls <= self.allow
        return GuardResult([Check("probe_cap", ok, "ok" if ok else "cap reached")])


def test_a_queued_ticket_is_rechecked_when_it_fills(book):
    """The account at the open is not the account that proposed the ticket."""
    book.rollover(DAY1)
    book.execute_ticket(ticket(ticket_id="td-a", limit="10.50"))
    second = book.execute_ticket(ticket(ticket_id="td-b", limit="10.50"))
    assert second.status == "queued"

    book.guard = _VetoGuard(allow=1)     # only the first may fill
    book.rollover(DAY2)

    assert book.ledger.account.position(SYMBOL).total_qty == 100, "one fill, not two"
    assert book.summary()["pending"] == []
    cancels = [r for r in book.store.list_decisions(limit=20) if r.get("kind") == "cancel"]
    assert any("GUARD:" in str(r.get("payload")) for r in cancels)


def test_an_unguarded_book_still_fills(book):
    """No guard injected (unit tests only): the fill path must not silently skip work."""
    book.rollover(DAY1)
    book.execute_ticket(ticket())
    book.rollover(DAY2)
    assert book.ledger.account.position(SYMBOL).total_qty == 100


def test_a_queued_ticket_expires_after_the_ttl(book):
    """A bar that never arrives must not keep a three-week-old thesis alive."""
    book.rollover(DAY1)
    book.execute_ticket(ticket())
    assert book.state["pending"], "created on DAY1, waiting for DAY2"

    book.rollover("20260109")          # created DAY1, TTL 3 sessions -> 20260108

    assert book.state["pending"] == []
    assert book.ledger.account.position(SYMBOL).total_qty == 0
    cancels = [r for r in book.store.list_decisions(limit=20) if r.get("kind") == "cancel"]
    assert any("QUEUE_EXPIRED" in str(r.get("payload")) for r in cancels)


def test_a_filled_queued_ticket_is_closed_out_and_reanchors_its_stop(book):
    """A ticket that filled through the queue must stop looking like a queue entry."""
    book.rollover(DAY1)
    book.store.save_ticket(ticket(ticket_id="td-a"))     # propose writes the row
    book.execute_ticket(ticket(ticket_id="td-a"))

    book.rollover(DAY2)

    stored = book.store.get_ticket("td-a")
    assert stored["status"] == "filled", "stops.py looks for filled BUY tickets"
    assert stored["fill"]["price"] == "10.11"
    # 10.11 x (1 - 5%) = 9.6045 -> 9.60; the 9.50 written at proposal time was
    # anchored to a close that never traded.
    assert D(stored["stop_price_final"]) == D("9.60")
    assert stored["stop_basis"] == "fill"


def test_mark_prices_uses_a_quote_when_the_bar_is_missing(book):
    """During a session today's bar does not exist; the quote does."""
    from types import SimpleNamespace

    book.rollover(DAY1)
    pos = book.ledger.account.position(SYMBOL)
    pos.total_qty, pos.available_qty, pos.last_price = 100, 100, D("10.00")
    book.quotes = lambda symbols: {
        SYMBOL: SimpleNamespace(last=D("8.50"), low=D("8.40"), source="stub",
                                quote_ts=None, age_seconds=1.0)
    }

    book.mark_prices("20260109", live=True)
    assert pos.last_price == D("8.50")

    pos.last_price = D("10.00")
    book.mark_prices("20260109")            # no live flag, no bar -> unchanged
    assert pos.last_price == D("10.00")


def test_a_cash_dividend_is_credited_on_the_ex_date(book):
    """The stored bars are unadjusted: without this the ex-date drop is a fake loss."""
    from datetime import date

    from service.kernel.corp_action import CorporateAction

    book.rollover(DAY1)
    pos = book.ledger.account.position(SYMBOL)
    pos.total_qty = pos.available_qty = 100
    pos.avg_cost, pos.last_price = D("11.00"), D("10.00")
    cash_before = book.ledger.account.cash_available

    # The exchange prints an adjusted pre_close on the ex-date; the tape check
    # in the book compares the payout against that drop.
    book.market.bars[DAY2] = make_bar(DAY2, open_="9.55", high="9.65", low="9.50",
                                      close="9.60", pre_close="9.50")
    book.market.actions_map[DAY2] = [CorporateAction(
        symbol=SYMBOL, ex_date=date(2026, 1, 6), cash_div_per_share=D("0.50"))]
    book.rollover(DAY2)

    assert book.ledger.account.cash_available == cash_before + D("50.00")
    assert book.ledger.account.dividend_income == D("50.00")
    assert pos.total_qty == 100
    assert pos.avg_cost == D("10.5000"), "cost basis absorbs the payout"

    cash_after = book.ledger.account.cash_available
    book.rollover(DAY2)                       # same session again: no double credit
    assert book.ledger.account.cash_available == cash_after


def test_a_bonus_issue_adds_tradable_shares_on_the_ex_date(book):
    from datetime import date

    from service.kernel.corp_action import CorporateAction

    book.rollover(DAY1)
    pos = book.ledger.account.position(SYMBOL)
    pos.total_qty = pos.available_qty = 100
    pos.avg_cost, pos.last_price = D("10.00"), D("10.00")

    # 10 送 5 转 5: one extra share per share held, tradable on the ex-date.
    book.market.bars[DAY2] = make_bar(DAY2, open_="5.05", high="5.15", low="5.00",
                                      close="5.10", pre_close="5.00")
    book.market.actions_map[DAY2] = [CorporateAction(
        symbol=SYMBOL, ex_date=date(2026, 1, 6),
        stock_div_per_share=D("0.5"), transfer_per_share=D("0.5"))]
    book.rollover(DAY2)

    assert pos.total_qty == 200
    assert pos.available_qty == 200
    assert pos.avg_cost == D("5.0000")


def test_a_payout_the_tape_does_not_support_is_not_credited(book):
    """A stale plan must not mint money: cash larger than the drop is refused."""
    from datetime import date

    from service.kernel.corp_action import CorporateAction

    book.rollover(DAY1)
    pos = book.ledger.account.position(SYMBOL)
    pos.total_qty = pos.available_qty = 100
    pos.avg_cost, pos.last_price = D("10.00"), D("10.00")
    cash_before = book.ledger.account.cash_available

    # DAY2's stub bar keeps pre_close at 10.00: the price did not go ex anything.
    book.market.actions_map[DAY2] = [CorporateAction(
        symbol=SYMBOL, ex_date=date(2026, 1, 6), cash_div_per_share=D("0.50"))]
    book.rollover(DAY2)

    assert book.ledger.account.cash_available == cash_before
    assert book.ledger.account.dividend_income == D("0")
    kinds = [r.get("kind") for r in book.store.list_decisions(limit=20)]
    assert "corporate_action_mismatch" in kinds


def test_catchup_watermark_never_skips_sessions_beyond_the_batch_cap(
        book, monkeypatch):
    """A bounded replay must resume at the first unprocessed session."""
    from datetime import date, timedelta

    from service.kernel.corp_action import CorporateAction

    first = date(2026, 1, 1)
    days = [(first + timedelta(days=offset)).strftime("%Y%m%d")
            for offset in range(1, 33)]
    visited = []
    original_actions = book.market.actions

    def actions(day):
        visited.append(day)
        return original_actions(day)

    monkeypatch.setattr(book.market, "actions", actions)
    monkeypatch.setattr(book.market.calendar, "between", lambda _start, _end: days,
                        raising=False)
    monkeypatch.setattr(
        book, "_resolve_action",
        lambda action, _session: (action.cash_div_per_share, D("0")),
    )
    book.state["actions_through"] = "20260101"
    pos = book.ledger.account.position(SYMBOL)
    pos.total_qty = pos.available_qty = 100
    pos.avg_cost = pos.last_price = D("10.00")
    book.market.actions_map[days[0]] = [CorporateAction(
        symbol=SYMBOL, ex_date=date(2026, 1, 2), cash_div_per_share=D("0.10"))]
    book.market.actions_map[days[-1]] = [CorporateAction(
        symbol=SYMBOL, ex_date=date(2026, 2, 2), cash_div_per_share=D("0.20"))]

    book.apply_corporate_actions(days[-1])
    book.apply_corporate_actions(days[-1])

    assert visited == days
    assert book.ledger.account.dividend_income == D("30.00")


# ── 缺列不等于"不许成交" ──────────────────────────────────────────────────
def test_a_bar_without_a_band_leaves_the_ticket_limit_alone():
    """``up_limit=0`` 是"这一列没有"，不是"涨停价是 0"。

    三条成交路径都写过 ``min(limit, bar.up_limit)``。缺列时它把一个好端端的
    限价改写成 0，``submit`` 再以 PRICE_LIMIT 拒绝——缺数据穿上了"非法价格"
    的外衣，票据被取消的理由完全指错了方向。
    """
    from dataclasses import replace

    from service.kernel.models import Side

    bar = replace(make_bar(DAY2, open_="10.00", high="10.50", low="9.80",
                           close="10.20"),
                  up_limit=D("0"), down_limit=D("0"))

    assert PaperBook._clamp_to_band(D("10.10"), Side.BUY, bar) == D("10.10")
    assert PaperBook._clamp_to_band(D("9.90"), Side.SELL, bar) == D("9.90")


def test_a_real_band_still_clamps_in_both_directions():
    """带宽存在时行为不变：越过涨停的买价、跌破跌停的卖价都要被夹住。"""
    from service.kernel.models import Side

    bar = make_bar(DAY2, open_="10.00", high="10.50", low="9.80", close="10.20")

    assert PaperBook._clamp_to_band(D("12.00"), Side.BUY, bar) == bar.up_limit
    assert PaperBook._clamp_to_band(D("8.00"), Side.SELL, bar) == bar.down_limit

