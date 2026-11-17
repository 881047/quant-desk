"""The observation layer must report state and arithmetic, never a recommendation."""

from __future__ import annotations

import json

import pytest

from service.trading import observation
from service.trading.decisions import DecisionError
from tests.trading.helpers import pick_symbols, proposal

# Keys or phrasings that would make this a suggestion rather than a measurement.
FORBIDDEN_KEYS = ("action", "recommendation", "suggest", "should", "must_do")


def _buy(desk, symbol: str):
    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    assert ticket["status"] == "proposed", ticket.get("reasons")
    result = desk.decisions.approve(ticket["ticket_id"], actor="test")
    assert result["status"] == "filled", result
    return ticket


def _breach(desk, symbol: str):
    last = float(desk.market.snapshot(symbol, desk.market.latest_bar_date()).last)
    for ticket in desk.paper.list_tickets(limit=100, status="filled"):
        if ticket.get("side") == "BUY" and ticket.get("symbol") == symbol:
            ticket["stop_price"] = f"{last * 1.05:.2f}"
            # Filled tickets re-anchor to the actual execution price; override
            # that durable anchor too when this fixture simulates a new breach.
            ticket["stop_price_final"] = ticket["stop_price"]
            desk.paper.save_ticket(ticket)
            return
    raise AssertionError(f"no filled BUY for {symbol}")


def _walk(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_observation_has_no_action_field(desk):
    day = desk.market.latest_bar_date()
    symbol = pick_symbols(desk, 1)[0]
    _buy(desk, symbol)
    _breach(desk, symbol)

    snap = observation.snapshot(desk, day)
    assert snap["ok"] is True
    assert snap["constraints"], "a breached stop must show up as a constraint"
    keys = {key.lower() for key in _walk(snap)}
    for word in FORBIDDEN_KEYS:
        assert word not in keys, f"{word!r} is a judgement, not information"

    record = snap["constraints"][0]
    assert record["kind"] == "stop_breach"
    assert record["attention"] in ("high", "normal", "info")
    assert "fact" in record and "measures" in record


def test_stop_breach_reports_exit_arithmetic_and_history(desk):
    day = desk.market.latest_bar_date()
    symbol = pick_symbols(desk, 1)[0]
    _buy(desk, symbol)
    _breach(desk, symbol)

    record = observation.stop_constraints(desk, day)[0]
    exit_ = record["measures"]["exit_at_next_open"]
    assert exit_["fill_basis"] == "next_open"
    assert exit_["qty"] > 0
    assert exit_["fee_cny"] >= 5.0, "the minimum commission is a fact, not an estimate"
    assert exit_["net_if_filled_at_ref"] < exit_["gross"]

    hist = record["measures"]["history"]
    if hist and hist.get("n"):
        assert hist["entry"] == "next_open"
        assert 0.0 <= hist["win_rate"] <= 1.0
        assert hist["p10"] <= hist["median"] <= hist["worst"] or hist["median"] >= hist["worst"]


def test_open_violation_blocks_new_buys_but_not_sells(desk):
    day = desk.market.latest_bar_date()
    symbol = pick_symbols(desk, 1)[0]
    _buy(desk, symbol)
    _breach(desk, symbol)
    assert observation.open_breaches(desk, day) == [symbol]

    other = pick_symbols(desk, 2)[-1]
    with pytest.raises(DecisionError) as exc:
        desk.decisions.propose(proposal(other), actor="test")
    assert exc.value.code == "OPEN_VIOLATION"

    sell = desk.decisions.propose(
        {"symbol": symbol, "side": "SELL", "sleeve": "CORE", "intent_type": "manual",
         "thesis": "风险预算已违约，先处理这笔敞口再看别的机会，不追加风险。",
         "evidence": ["daily:latest"], "invalidation": "若已无持仓则不应执行"},
        actor="test")
    assert "OPEN_VIOLATION" not in json.dumps(sell, ensure_ascii=False)


def test_core_deviation_reports_cost_not_a_command(desk):
    day = desk.market.latest_bar_date()
    records = observation.core_deviation(desk, day)
    assert records
    record = records[0]
    assert record["kind"] == "target_drift"
    assert "orders" in record["measures"]
    assert "fees_cny" in record["measures"]
    assert "action" not in record
    # The unaffordable bond sleeve is not drift: the rule is unexecutable at
    # 5,000 CNY, not violated.
    fact = record["fact"]
    assert fact.get("max_drift") is not None


def test_allocation_and_the_enforced_cap_are_two_different_numbers(desk):
    """算出来的分配被叫过 `caps`，note 还写着"超了会被 guard 拒绝"。

    而 guard 的 strategy_cap 读的是 `REGISTRY[key].max_weight`（静态注册值）——
    分配通常比它严格，于是"超过分配但没超过注册上限"那一段里，那句话是假的。
    判断冒充约束比没有约束更危险：读者以为自己处于"系统允许"的区间，
    而系统只是没说话。这条钉住两个数分开、且各自署名。
    """
    day = desk.market.latest_bar_date()
    state = observation.budget_state(desk, day)
    assert state["ok"] is True
    assert state["regime"] in ("risk_on", "neutral", "risk_off", "stressed")
    assert "allocation" in state and "headroom_vs_allocation" in state
    for key, weight in state["allocation"].items():
        assert state["headroom_vs_allocation"][key] <= weight + 1e-9
        # 分配不得越过注册上限——否则"更严格"这句话就不成立
        assert weight <= state["enforced"]["strategy_max_weight"][key] + 1e-9
    assert "caps" not in state, "旧名字会把判断读成闸门"


def test_snapshot_survives_an_empty_book(desk):
    snap = observation.snapshot(desk, desk.market.latest_bar_date())
    assert snap["ok"] is True
    assert snap["constraints"] == []
    assert snap["counts"]["constraints"] == 0


def test_core_holdings_do_not_consume_the_strategy_cap(desk, monkeypatch):
    """One 18.5% CORE position used to eat a third of llm's 60% cap."""
    day = desk.market.latest_bar_date()
    monkeypatch.setattr("service.trading.arena.scoreboard", lambda _: {"decisions": []})
    monkeypatch.setattr(desk.book, "summary", lambda: {
        "equity": "10000",
        "positions": [{"symbol": "600000.SH", "market_value": "2000"}],
    })
    monkeypatch.setattr(desk.paper, "list_tickets", lambda **kw: [{
        "symbol": "600000.SH", "side": "BUY", "sleeve": "CORE", "strategy": None,
    }])

    state = observation.budget_state(desk, day)
    assert state["core_weight"] == pytest.approx(0.2)
    assert "llm" not in state["used"], "a CORE sleeve is not a strategy bet"
    assert state["headroom_vs_allocation"]["llm"] == pytest.approx(
        state["allocation"]["llm"])
