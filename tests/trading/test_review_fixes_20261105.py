"""Regressions for the first fix batch of the 2026-11-05 full review.

F1 contract text · F2 stop gate · F3 autostop state machine · F4 intraday
``fill_id`` · F6 stale-quote config source.

Every case asserts the *fixed* behaviour. The accompanying
``test-writer-report.md`` records why the same assertion fails on HEAD
``000aba3`` (drift / mutation simulation, not a happy-path smoke test).
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from service.notify import alert_log
from service.trading import autostop, observation, realtime, watch
from service.trading.decisions import DecisionError
from tests.trading.helpers import pick_symbols, proposal
from tests.trading.test_autostop import _held, _run
from tests.trading.test_live_fill import _at, _at_hour, _quote
from tests.trading.test_watch import _moment

ROOT = Path(__file__).resolve().parents[2]


def _gate(*_args, **_kwargs):
    """Stand-in for a gate that sees a breach; overridden per test below."""
    raise AssertionError("each test must bind its own symbol")


@pytest.fixture
def intraday_desk(desk, monkeypatch):
    """Reuse the live-fill fixture's clock alignment without importing a fixture."""
    monkeypatch.setenv("FILL_MODE", "intraday")
    desk.book.fill_mode = "intraday"
    _at(desk, _at_hour(desk, 10, 30))
    return desk


# ------------------------------------------------------------------ F2 · gate
def test_idempotent_retry_cannot_bypass_open_violation(desk, monkeypatch):
    """A retry of a still-open BUY must not answer from a stale ticket."""
    symbol = pick_symbols(desk, 1)[0]
    first = desk.decisions.propose(
        proposal(symbol, client_request_id="retry-stop-0001"))
    assert first["status"] == "proposed", first.get("reasons")

    monkeypatch.setattr(
        observation, "evaluate_stop_gate",
        lambda *_a, **_k: {"breached": [symbol], "unknown": [], "no_stop": []})

    with pytest.raises(DecisionError) as exc:
        desk.decisions.propose(
            proposal(symbol, client_request_id="retry-stop-0001"))
    assert exc.value.code == "OPEN_VIOLATION"
    assert exc.value.status == 409


def test_terminal_ticket_retry_is_verbatim(desk, monkeypatch):
    """A filled/rejected ticket is an answer, not a live intent: no gate replay."""
    symbol = pick_symbols(desk, 1)[0]
    first = desk.decisions.propose(
        proposal(symbol, client_request_id="retry-stop-0002"))
    desk.decisions.approve(first["ticket_id"], actor="test")

    calls = []
    monkeypatch.setattr(
        observation, "evaluate_stop_gate",
        lambda *_a, **_k: calls.append(1) or
        {"breached": [], "unknown": [], "no_stop": []})

    again = desk.decisions.propose(
        proposal(symbol, client_request_id="retry-stop-0002"))
    assert again["ticket_id"] == first["ticket_id"]
    assert calls == [], "a terminal ticket must not re-run the stop gate"


def test_approve_rechecks_stop_gate(desk, monkeypatch):
    """A breach appearing between propose and approve must veto the fill."""
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))
    assert ticket["status"] == "proposed", ticket.get("reasons")

    monkeypatch.setattr(
        observation, "evaluate_stop_gate",
        lambda *_a, **_k: {"breached": [symbol], "unknown": [], "no_stop": []})

    result = desk.decisions.approve(ticket["ticket_id"], actor="test")
    assert result["status"] == "rejected", result
    assert "stop_gate" in json.dumps(result, ensure_ascii=False, default=str)


def test_sell_branch_has_no_stop_gate_veto(desk, monkeypatch):
    """The stop gate must never trap a position that needs to be sold."""
    symbol = pick_symbols(desk, 1)[0]
    buy = desk.decisions.propose(proposal(symbol))
    desk.decisions.approve(buy["ticket_id"], actor="test")
    position = desk.book.ledger.account.positions[symbol]
    position.available_qty = position.total_qty
    position.locked_qty = 0
    desk.book.persist()

    monkeypatch.setattr(
        observation, "evaluate_stop_gate",
        lambda *_a, **_k: {"breached": [symbol], "unknown": [], "no_stop": []})

    sell = desk.decisions.propose({
        "symbol": symbol, "side": "SELL", "sleeve": "CORE",
        "intent_type": "manual",
        "thesis": "风险预算已违约，先处理这笔敞口，不追加风险。",
        "evidence": ["daily:latest"],
        "invalidation": "若已无持仓则不应执行",
    }, actor="test")
    body = json.dumps(sell, ensure_ascii=False, default=str)
    assert "OPEN_VIOLATION" not in body
    assert "stop_gate" not in body, "SELL must stay exempt from the stop gate"


# ------------------------------------------------------------- F3 · autostop
def test_recovered_frame_blocks_autosell(desk, tmp_path):
    """A stale open mark must not sell a price that has recovered."""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)

    quiet = _run(desk, tmp_path, symbol, stop, at=(13, 2), day=day,
                 last=float(stop) * 1.10)
    assert quiet["acted"] == [], "price recovered: the stale mark is not a breach"
    assert desk.book.summary()["positions"], "the position must still be held"


def test_lunch_break_does_not_count_as_confirmation(desk, tmp_path):
    """11:29 -> 13:00 is 60 session seconds, not 91 wall-clock minutes."""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _run(desk, tmp_path, symbol, stop, at=(11, 29), day=day)

    shot = _run(desk, tmp_path, symbol, stop, at=(13, 0), day=day)
    assert shot["acted"] == [], "the lunch break must not confirm a breach"
    assert desk.book.summary()["positions"]


def test_session_elapsed_excludes_lunch(desk):
    """Direct arithmetic on the replacement for the old wall-clock delta."""
    day = desk.market.latest_bar_date()
    calendar = desk.market.calendar
    assert autostop._age_seconds(
        _moment(day, 10, 30).isoformat(), _moment(day, 10, 33), calendar) == 180.0
    assert autostop._age_seconds(
        _moment(day, 11, 29).isoformat(), _moment(day, 13, 0), calendar) == 60.0


def test_confirmed_breach_still_sells_once(desk, tmp_path):
    """Regression guard: a real 180s breach must still be executed."""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)

    sold = _run(desk, tmp_path, symbol, stop, at=(10, 33), day=day)
    assert [row["status"] for row in sold["acted"]] == ["filled"]
    sides = [row["side"] for row in desk.paper.recent_fills(5)]
    assert sides.count("SELL") == 1


def test_idempotency_key_does_not_include_qty(desk, tmp_path, monkeypatch):
    """One entry ticket, one trading day, one idempotency key."""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    seen: list[dict] = []
    original = desk.decisions.propose

    def spy(req, *args, **kwargs):
        seen.append(dict(req))
        return original(req, *args, **kwargs)

    monkeypatch.setattr(desk.decisions, "propose", spy)
    _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)
    sold = _run(desk, tmp_path, symbol, stop, at=(10, 33), day=day)
    assert [row["status"] for row in sold["acted"]] == ["filled"]

    keys = [row["client_request_id"] for row in seen
            if str(row.get("client_request_id", "")).startswith("autostop-")]
    assert keys == [f"autostop-{day}-{symbol}-{entry}"]


def test_autostop_error_is_written_to_alert_log(desk, tmp_path, monkeypatch):
    """A failed delegated sell must leave a line the model can actually read."""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)

    def boom(*_args, **_kwargs):
        raise RuntimeError("vendor timeout")

    monkeypatch.setattr(desk.decisions, "propose", boom)
    shot = _run(desk, tmp_path, symbol, stop, at=(10, 33), day=day)
    assert shot["acted"] and shot["acted"][0]["status"] == "error"

    path = alert_log.alerts_path(day, tmp_path)
    rows = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(row.get("event") == "autostop_error"
               and "RuntimeError" in str(row.get("title", "")) for row in rows)
    heart = json.loads(
        alert_log.heartbeat_path(day, tmp_path).read_text(encoding="utf-8"))
    assert heart["outcome"] == "ok", "the heartbeat outcome must stay unchanged"


def test_findings_none_keeps_legacy_behavior(desk, tmp_path):
    """Pin the lead's decision: ``None`` is a legacy caller, not an empty frame."""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _run(desk, tmp_path, symbol, stop, at=(10, 30), day=day)

    acted = autostop.run(desk, day=day, scope=watch.SCOPE,
                         now=_moment(day, 10, 33), base=tmp_path)
    assert [row["status"] for row in acted] == ["filled"]


# ------------------------------------------------------------------ F4 · fill
def test_live_fill_id_is_ticket_scoped(intraday_desk):
    """The intraday path must book the same ticket-scoped key as the other two."""
    desk = intraday_desk
    symbol = pick_symbols(desk, 1)[0]
    shot = _quote(desk, symbol)
    desk.book.quotes = lambda symbols: {name: shot for name in symbols}

    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    assert ticket["status"] == "proposed", ticket.get("reasons")
    desk.decisions.approve(ticket["ticket_id"], actor="test")

    record = [fill for fill in desk.book.state["fills"]
              if fill["symbol"] == symbol][-1]
    assert record["fill_basis"] == "intraday"
    assert record["fill_id"] == f"fill-{ticket['ticket_id']}"


# --------------------------------------------------------------- F6 · config
def test_stop_gate_staleness_follows_config(desk, monkeypatch):
    """A 150s quote is fresh when the configured cap is raised to 200s."""
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    monkeypatch.setattr(realtime, "STALE_AFTER", 200.0)

    # A real Quote carries ``low`` / ``source`` / ``quote_ts`` too — open_stops
    # reads them, so a bare SimpleNamespace is not a valid stand-in here.
    quote = _quote(desk, symbol, age_seconds=150.0,
                   last=stop * Decimal("0.97"))
    gate = observation.stop_gate(desk, day, quotes={symbol: quote})
    assert symbol in gate["breached"], (
        "a fresh-but-older-than-120s quote must be measured, not fall back "
        "to yesterday's close")


# ------------------------------------------------------------------ F1 · text
def test_contract_text_matches_autostop_switch():
    """The prose the model reads must describe the feature that is switched on."""
    prompt = (ROOT / "plugin" / "lib" / "index.js").read_text(encoding="utf-8")
    assert "autostop.enabled" in prompt
    assert "autostop.confirm_seconds" in prompt
    assert "不会替你卖" not in prompt
    assert "没有代码写的止损" not in prompt

    for rel in ("AGENTS.md", "scripts/stop_check.py", "service/trading/stops.py",
                "service/trading/duty.py", "service/kernel/models.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "系统不替你卖" not in text, rel
        assert "不会替你卖" not in text, rel

    contract = (ROOT / "docs" / "API_CONTRACT.md").read_text(encoding="utf-8")
    assert "autostop" in contract
