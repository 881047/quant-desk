"""F4 / F6 regressions: ticket-scoped fill ids and one freshness config source.

* F4-1 ``_execute_live`` used to book the matcher's ``O0000000N-<qty>`` fill id
  straight into ``paper_fills`` (whose ``fill_id`` is UNIQUE). Two processes, or
  one process after an OMS sequence reset, collide on that key. The other two
  settlement paths (``_try_fill`` / ``_execute_at_close``) already wrote
  ``fill-{ticket_id}``; this case pins the third.
* F6-1 ``observation.stop_gate`` hardcoded ``<= 120`` while ``tripwire`` read
  ``realtime.STALE_AFTER``. Moving the config must move *both* judgements. The
  daily snapshot is deliberately removed here so a quote judged stale has
  nowhere to fall back but ``unknown`` — that is what makes the drift visible.
"""

from __future__ import annotations

import pytest

from service.trading import observation, realtime, tripwire
from tests.trading.helpers import pick_symbols, proposal
from tests.trading.test_autostop import _held
from tests.trading.test_live_fill import _at, _at_hour, _quote


# ---------------------------------------------------------------------------
# F4-1 · intraday live fill id is scoped to the ticket, not to the OMS sequence
# ---------------------------------------------------------------------------
@pytest.fixture
def intraday(desk, monkeypatch):
    """The default ``next_open`` / conftest ``close`` modes never reach
    ``_execute_live``; pin every clock in the fill path to one open-session
    moment (mirrors tests/trading/test_live_fill.py)."""
    monkeypatch.setenv("FILL_MODE", "intraday")
    desk.book.fill_mode = "intraday"
    _at(desk, _at_hour(desk, 10, 30))
    return desk


def test_live_fill_id_is_ticket_scoped(intraday):
    desk = intraday
    symbol = pick_symbols(desk, 1)[0]
    shot = _quote(desk, symbol)
    desk.book.quotes = lambda symbols: {s: shot for s in symbols}

    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    assert ticket["status"] == "proposed", ticket.get("reasons")
    result = desk.decisions.approve(ticket["ticket_id"], actor="test")
    assert result["status"] == "filled", result

    expected = f"fill-{ticket['ticket_id']}"

    # state["fills"] is the book's own record; paper_fills.payload is the
    # durable, UNIQUE-keyed row. Both must agree.
    record = [row for row in desk.book.state["fills"]
              if row["symbol"] == symbol][-1]
    assert record["fill_basis"] == "intraday", "必须真的走了 _execute_live"
    assert record["fill_id"] == expected

    durable = desk.paper.get_fill(ticket["ticket_id"])
    assert durable is not None
    assert durable["fill_id"] == expected
    assert desk.paper.recent_fills(5)[0]["fill_id"] == expected
    assert not durable["fill_id"].startswith("O"), (
        "不能是 matcher 的 order_id（旧代码落 O0000000N-<qty>，重启会撞主键）")

    # Consistency with the other two settlement paths: all three now use the
    # same ticket-scoped shape, so a repeat settlement cannot conflict.
    assert record["fill_id"] == f"fill-{record['ticket_id']}"


# ---------------------------------------------------------------------------
# F6-1 · stop_gate and tripwire must follow the same STALE_AFTER config
# ---------------------------------------------------------------------------
def test_stop_gate_staleness_follows_config(desk, monkeypatch):
    from types import SimpleNamespace

    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)

    # age=150s, realtime: within a 200s window but outside the old hardcoded
    # 120s. Built before the snapshot is removed (the helper reads a bar).
    shot = _quote(desk, symbol, age_seconds=150.0, is_realtime=True)
    quotes = {symbol: shot}

    # Environment simulation: take the daily bar away. A quote that is judged
    # stale then has no fallback price and must be reported as ``unknown``.
    monkeypatch.setattr(desk.market, "snapshot", lambda *a, **k: None)

    # --- state judgement -------------------------------------------------
    monkeypatch.setattr(realtime, "STALE_AFTER", 200)
    fresh = observation.stop_gate(desk, day, quotes=quotes)
    assert symbol not in fresh["unknown"], (
        "150s 报价在 200s 配置下是 fresh；旧硬编码 120 会把它判成 unknown")

    # Drift control: put the threshold back to the old literal. The very same
    # quote must now land in ``unknown`` — this is exactly what old code did,
    # so the assertion above is proven to be sensitive to the fix.
    monkeypatch.setattr(realtime, "STALE_AFTER", 120)
    stale = observation.stop_gate(desk, day, quotes=quotes)
    assert symbol in stale["unknown"], (
        "把配置改回 120 后 150s 报价必须变 unknown（否则上一条是空断言）")

    # --- tripwire follows the same source --------------------------------
    # No vendor: a local desk double returns one in-memory quote, and the
    # finding generators that need real market data are stubbed out.
    monkeypatch.setattr(tripwire, "_stops", lambda *a: [])
    monkeypatch.setattr(tripwire, "_moves", lambda *a: [])
    monkeypatch.setattr(tripwire, "_pending", lambda *a: [])

    class FrameDesk:
        class market:
            @staticmethod
            def last_trading_day():
                return day

        class book:
            state = {"pending": []}

            @staticmethod
            def summary():
                return {"positions": [{"symbol": symbol}]}

        @staticmethod
        def quotes(symbols):
            return {s: SimpleNamespace(is_realtime=True, age_seconds=150.0)
                    for s in symbols}

    monkeypatch.setattr(realtime, "STALE_AFTER", 200)
    wide = tripwire.evaluate(FrameDesk(), day=day, include_tape=False)
    assert "quotes_unavailable" not in {f["code"] for f in wide["findings"]}
    assert wide["checked"]["live_quotes"] == 1

    monkeypatch.setattr(realtime, "STALE_AFTER", 120)
    narrow = tripwire.evaluate(FrameDesk(), day=day, include_tape=False)
    assert "quotes_unavailable" in {f["code"] for f in narrow["findings"]}
    assert narrow["checked"]["live_quotes"] == 0
