"""Synthetic pre-trade guard regressions; no real ledger or market store."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from service.kernel.models import Board, D, Instrument
from service.trading.guard import Guard
from service.trading.market import Snapshot


class _Account:
    def __init__(self, cash: Decimal, qty: int = 0, symbol: str = "600000.SH"):
        self.cash_available = cash
        self.positions = {}
        if qty:
            self.positions[symbol] = SimpleNamespace(
                symbol=symbol, total_qty=qty, available_qty=qty,
                last_price=D("10"))

    def position(self, symbol):
        return self.positions.get(symbol, SimpleNamespace(
            symbol=symbol, total_qty=0, available_qty=0, last_price=D("0")))

    def peek(self, symbol):
        """Read-only lookup, matching ``kernel.models.Account.peek``.

        The stub's ``position`` above returns a throwaway empty object instead of
        storing one, so it was never able to show the difference. ``peek`` is the
        one the guard calls now, and returning ``None`` is the point: "no such
        holding" must not be spelled as "a holding of zero".
        """
        return self.positions.get(symbol)


class _Book:
    def __init__(self, equity: Decimal, drawdown: float, qty: int = 0,
                 symbol: str = "600000.SH", queued: Decimal | None = None):
        self.ledger = SimpleNamespace(account=_Account(equity, qty, symbol))
        self.state = {"kill_switch": False, "safe_mode": False, "fills": []}
        self.cost = SimpleNamespace(buy_fees=lambda value, inst: D("0"))
        self._equity = equity
        self._drawdown = drawdown
        # 已排队、未结算的买单占用；0 表示队列为空
        self._queued = queued if queued is not None else D("0")

    def equity(self):
        return self._equity

    def actions_on(self, date):
        return 0

    def sleeve_value(self, sleeve):
        return D("0")

    def summary(self):
        return {"positions": []}

    def daily_pnl_pct(self):
        return 0.0

    def drawdown(self):
        return self._drawdown

    def queued_buy_commitment(self, *, exclude=None):
        return self._queued


class _Market:
    def __init__(self, *, is_st: bool, adv: dict,
                 instrument: Instrument | None = None):
        self.calendar = SimpleNamespace(prev=lambda day: day)
        self._instrument = instrument or Instrument("600000.SH", is_st=is_st)
        self._adv = adv
        self._snapshot = Snapshot(
            symbol="600000.SH", trade_date="20260107", last=D("10"),
            pre_close=D("10"), open=D("10"), high=D("10"), low=D("10"),
            up_limit=D("11"), down_limit=D("9"), volume=10000,
            amount=100000, suspended=False, source="test")

    def snapshot(self, symbol, date):
        return self._snapshot

    def instrument(self, symbol, date):
        return self._instrument

    def adv_map(self, date):
        return self._adv

    def industry_of(self, symbol):
        return ""


def _preflight(monkeypatch, *, side: str, is_st: bool = False,
               drawdown: float = 0.0, equity: Decimal | None = None,
               adv: dict | None = None, strategy: str | None = None,
               symbol: str = "600000.SH",
               queued: Decimal | None = None,
               instrument: Instrument | None = None):
    equity = D("5000") if equity is None else equity
    monkeypatch.setattr("service.trading.guard.build_brief", lambda **kwargs: {
        "core": {"usable": True, "missing": []}})
    market = _Market(is_st=is_st, adv={symbol: 1_000_000_000} if adv is None else adv,
                     instrument=instrument)
    book = _Book(equity, drawdown, qty=100 if side == "SELL" else 0, symbol=symbol,
                 queued=queued)
    guard = Guard(market, SimpleNamespace(summary=list))
    return guard.preflight(
        book, symbol=symbol, side=side, sleeve="CORE", qty=100,
        price=D("10"), date="20260107", strategy=strategy,
        snapshot=market.snapshot(symbol, "20260107"))


def _check(result, name):
    return next(row for row in result.checks if row.name == name)


def test_st_and_drawdown_stop_block_buys_but_not_sells(monkeypatch):
    buy = _preflight(monkeypatch, side="BUY", is_st=True, drawdown=-0.20)
    sell = _preflight(monkeypatch, side="SELL", is_st=True, drawdown=-0.20)

    assert not _check(buy, "st").ok
    assert not _check(buy, "drawdown").ok
    assert not buy.ok
    assert _check(sell, "st").ok
    assert _check(sell, "drawdown").ok
    assert sell.ok


@pytest.mark.parametrize("adv", [{}, {"600000.SH": 0.0}])
def test_missing_or_zero_adv_fails_closed_for_buys(monkeypatch, adv):
    result = _preflight(monkeypatch, side="BUY", adv=adv)

    assert not _check(result, "liquidity").ok
    assert "unavailable" in _check(result, "liquidity").detail
    assert not result.ok


def test_non_positive_equity_blocks_buys_without_dividing_by_zero(monkeypatch):
    result = _preflight(monkeypatch, side="BUY", equity=D("0"), strategy="llm")

    assert not _check(result, "equity").ok
    assert not _check(result, "min_cash").ok
    assert "unavailable" in _check(result, "min_cash").detail
    assert not result.ok


def test_beijing_names_can_be_read_but_not_bought(monkeypatch):
    """北交所只做信息，不做仓。

    口径由用户定：可以拿北交所的数据看盘（家数、连板、情绪都算它），但选股与买入不碰。
    挡在 guard 而不是只挡 screen——`screen` 只是"找"，提案可以从别的路径直接进来；
    检查项是唯一绕不过去的地方。卖出放行：已有仓位必须永远能出。
    """
    bse = Instrument("920592.BJ", board=Board.BSE)

    buy = _preflight(monkeypatch, side="BUY", symbol="920592.BJ", instrument=bse)
    sell = _preflight(monkeypatch, side="SELL", symbol="920592.BJ", instrument=bse)

    assert not _check(buy, "board").ok
    assert "北交所" in _check(buy, "board").detail
    assert not buy.ok
    assert _check(sell, "board").ok, "卖出不能因为标的是北交所被拦"
    assert sell.ok


def test_a_main_board_ticket_names_its_board_in_the_checks(monkeypatch):
    """通过的那张票也要写清板块——审计链里不能只有出问题时才看得见板块。"""
    result = _preflight(monkeypatch, side="BUY")

    assert _check(result, "board").ok
    assert _check(result, "board").detail == "MAIN"


def test_a_queued_buy_consumes_the_cash_the_next_one_needs(monkeypatch):
    """排队中的买单没有账本订单、不冻结现金，但风控不能当它不存在。

    09:26（或昨晚 19:30）排进去的买单在结算前不动 ``cash_available``。只按余额
    判断，14:35 的窗口会把同一笔钱再承诺一次；17:30 结算时被取消的是**先下的**
    那张——而模型已经被告知"已挂单"。
    """
    free = _preflight(monkeypatch, side="BUY")
    blocked = _preflight(monkeypatch, side="BUY", queued=D("4500"))

    assert _check(free, "cash").ok
    cash = _check(blocked, "cash")
    assert not cash.ok
    assert "queued" in cash.detail and "4500" in cash.detail


def test_the_reservation_also_moves_the_min_cash_floor(monkeypatch):
    """最低现金比例算的是**能花的钱**，不是账面余额。"""
    ok = _preflight(monkeypatch, side="BUY", equity=D("100000"))
    blocked = _preflight(monkeypatch, side="BUY", equity=D("100000"),
                         queued=D("96000"))

    assert _check(ok, "min_cash").ok
    assert not _check(blocked, "min_cash").ok
