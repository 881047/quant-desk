"""The stop is executed while the model sleeps — but only once it is real.

``stops.py`` refuses to sell ("stop *state*, never stop *orders*") because the
``enforce()`` it replaced wrote its own thesis and then acted on it. This module
takes the other half of that sentence: the stop price is the model's own number,
written at entry, so executing it is not a second decision. The tests below pin
the three ways that could go wrong: selling too early, selling twice, and selling
on a number nobody chose.
"""

from __future__ import annotations

from decimal import Decimal

from service.trading import autostop, watch
from tests.trading.helpers import pick_symbols, proposal
from tests.trading.test_watch import _frame, _moment


def _held(desk, *, sellable: bool = True) -> tuple[str, str, Decimal]:
    """A filled position and the ticket it came from.

    ``sellable`` writes ``available_qty`` back through ``persist``: the fixture
    buys at today's close, so under T+1 nothing is sellable — and ``watch.run``
    calls ``reload()``, which would discard an in-memory edit. A position held
    from a previous session is the case a stop is actually meant to protect.
    """
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    desk.decisions.approve(ticket["ticket_id"], actor="test")
    if sellable:
        position = desk.book.ledger.account.positions[symbol]
        position.available_qty = position.total_qty
        position.locked_qty = 0
        desk.book.persist()
    return symbol, ticket["ticket_id"], Decimal(str(ticket["stop_price"]))


def _run(desk, tmp_path, symbol: str, stop: Decimal, *, at: tuple[int, int],
         day: str, last: float | None = None) -> dict:
    moment = _moment(day, *at)
    path = _frame(tmp_path, day=day, at=moment, symbol=symbol,
                  last=float(stop) * 0.97 if last is None else last)
    return watch.run(desk, path, day=day, now=moment, base=tmp_path)


def test_a_breach_younger_than_the_confirmation_is_not_sold(desk, tmp_path):
    """第一帧破线不卖。停损价压在整数关口上，价格在它旁边抖一帧不是"破了"。"""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)

    shot = _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)

    assert [row["code"] for row in shot["findings"]] == ["stop_breach"], "破线本身要记"
    assert shot["acted"] == [], "还没到确认时间，不该动手"
    assert desk.book.summary()["positions"], "仓位还在"


def test_a_confirmed_breach_is_sold_once(desk, tmp_path):
    """持续两分钟仍在线下才卖，且只卖一次。"""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)

    sold = _run(desk, tmp_path, symbol, stop, at=(10, 33), day=day)
    assert [row["status"] for row in sold["acted"]] == ["filled"]
    assert sold["acted"][0]["ticket_id"] == entry, "审计要指回建仓那张票"
    assert desk.book.summary()["positions"] == [], "仓位已经卖掉"

    again = _run(desk, tmp_path, symbol, stop, at=(10, 36), day=day)
    assert again["acted"] == [], "已经卖过，不该再来一笔"
    sides = [row["side"] for row in desk.paper.recent_fills(5)]
    assert sides.count("SELL") == 1


def test_a_frozen_position_is_reported_not_silently_skipped(desk, tmp_path):
    """T+1 卖不出要说出来。静默跳过等于"止损执行了"，而仓位还在。"""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk, sellable=False)
    _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)

    shot = _run(desk, tmp_path, symbol, stop, at=(10, 33), day=day)

    assert [row["status"] for row in shot["acted"]] == ["blocked"]
    assert "T+1" in shot["acted"][0]["reasons"][0]
    assert desk.book.summary()["positions"], "卖不掉的时候仓位必须还在"


def test_the_switch_off_leaves_the_position_alone(desk, tmp_path, monkeypatch):
    """`autostop.enabled: false` 就是回到"只记不卖"，一行配置的事。"""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)
    monkeypatch.setattr(autostop.settings, "flag", lambda key: False)

    shot = _run(desk, tmp_path, symbol, stop, at=(10, 33), day=day)

    assert shot["acted"] == []
    assert desk.book.summary()["positions"], "关掉之后仓位不该被动"


def test_a_vetoed_sale_is_reported_not_swallowed(desk, tmp_path):
    """风控否决要说出来。静默返回"没卖"和"卖不了"在读者眼里是同一句话。

    autostop 不自己判断能不能卖：它把单交给 `decisions`，那儿的 guard 会把仓位、
    T+1、价格带、总开关全过一遍。这里按住 kill switch，验证否决结果原样出来。
    """
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)
    desk.book.set_kill_switch(True)

    shot = _run(desk, tmp_path, symbol, stop, at=(10, 33), day=day)

    assert [row["status"] for row in shot["acted"]] == ["rejected"]
    assert any("kill_switch" in reason for reason in shot["acted"][0]["reasons"]), shot["acted"]
    assert desk.book.summary()["positions"], "被否决时仓位必须还在"


def test_the_audit_says_who_authorized_the_sale(desk, tmp_path):
    """记录里要能读出"谁定的线、谁执行的"，否则这就是被删掉的 enforce。"""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)

    _run(desk, tmp_path, symbol, stop, at=(10, 33), day=day)

    sell = desk.paper.recent_fills(1)[0]
    order = desk.paper.get_ticket(sell["ticket_id"])
    assert entry in order["thesis"], "理由要指回建仓那张票"
    assert str(stop) in order["thesis"], "理由要带上模型自己写的那个数"
    actors = [row["actor"] for row in desk.paper.list_decisions(20)]
    assert autostop.actor_for(entry) in actors
