"""The tripwire: does anything justify waking the model?

The value of this module is not that it detects breaches - ``stops`` does that -
but that it can say *no* cheaply, and that it never says "fine" when it means
"unmeasured".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar

import pytest

from service.kernel.clock import SHANGHAI
from service.trading import realtime, tripwire
from tests.trading.helpers import pick_symbols, proposal

SESSION_AT = datetime(2026, 10, 26, 10, 30, tzinfo=SHANGHAI)


def _quote(desk, symbol: str, **overrides):
    day = desk.market.latest_bar_date()
    snap = desk.market.snapshot(symbol, day)
    fields = {
        "symbol": symbol, "name": symbol, "last": snap.last,
        "pre_close": snap.pre_close, "open": snap.open, "high": snap.high,
        "low": snap.low, "volume": float(snap.volume), "amount": float(snap.amount),
        "up_limit": snap.up_limit, "down_limit": snap.down_limit,
        "quote_ts": SESSION_AT.isoformat(), "age_seconds": 1.0,
        "source": "ths", "is_realtime": True, "suspended": snap.suspended,
    }
    fields.update(overrides)
    return realtime.Quote(**fields)


@pytest.fixture
def quiet(desk, monkeypatch):
    """A flat book with no holdings: nothing can be breached."""
    monkeypatch.setattr(desk, "quotes", lambda symbols: {})
    return desk


def test_an_empty_book_does_not_wake_anyone(quiet):
    result = tripwire.evaluate(quiet, include_tape=False)
    assert result["ok"] is True
    assert result["triggered"] is False
    assert result["verdict"] == "skip"
    assert result["findings"] == []


def test_a_breached_stop_is_critical(desk, monkeypatch):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    desk.decisions.approve(ticket["ticket_id"], actor="test")

    # Price the position below its own stop.
    stop = Decimal(str(ticket["stop_price"]))
    shot = _quote(desk, symbol, last=(stop * Decimal("0.97")).quantize(Decimal("0.01")))
    monkeypatch.setattr(desk, "quotes", lambda symbols: {s: shot for s in symbols})

    result = tripwire.evaluate(desk, include_tape=False)
    codes = {f["code"] for f in result["findings"]}
    assert "stop_breach" in codes
    assert result["counts"]["critical"] == 1
    assert result["verdict"] == "wake"
    assert result["findings"][0]["severity"] == "critical", "critical sorts first"


def test_a_stop_two_percent_away_is_worth_looking_at(desk, monkeypatch):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    desk.decisions.approve(ticket["ticket_id"], actor="test")

    stop = Decimal(str(ticket["stop_price"]))
    shot = _quote(desk, symbol, last=(stop * Decimal("1.01")).quantize(Decimal("0.01")))
    monkeypatch.setattr(desk, "quotes", lambda symbols: {s: shot for s in symbols})

    result = tripwire.evaluate(desk, include_tape=False)
    codes = {f["code"] for f in result["findings"]}
    assert "stop_proximity" in codes
    assert result["counts"]["critical"] == 0


def test_missing_quotes_are_reported_not_treated_as_safe(desk, monkeypatch):
    """The dangerous version of this module returns [] when the feed is down."""
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    desk.decisions.approve(ticket["ticket_id"], actor="test")

    def boom(symbols):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(desk, "quotes", boom)
    result = tripwire.evaluate(desk, include_tape=False)
    codes = {f["code"] for f in result["findings"]}
    assert "quotes_unavailable" in codes
    assert any("vendor down" in f["detail"] for f in result["findings"])


def test_the_tripwire_writes_nothing(desk, monkeypatch):
    monkeypatch.setattr(desk, "quotes", lambda symbols: {})
    before = (len(desk.paper.list_orders(limit=500)),
              len(desk.book.state.get("fills", [])),
              len(desk.paper.list_decisions(limit=500)))
    tripwire.evaluate(desk)
    after = (len(desk.paper.list_orders(limit=500)),
             len(desk.book.state.get("fills", [])),
             len(desk.paper.list_decisions(limit=500)))
    assert before == after, "a check that writes is not a check"


def test_partial_and_daily_quotes_are_explicitly_unmeasured(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(tripwire, "_stops", lambda *args: [])
    monkeypatch.setattr(tripwire, "_moves", lambda *args: [])
    monkeypatch.setattr(tripwire, "_pending", lambda *args: [])

    class FakeDesk:
        class market:
            @staticmethod
            def last_trading_day():
                return "20261028"

        class book:
            state: ClassVar[dict] = {"pending": []}

            @staticmethod
            def summary():
                return {"positions": [{"symbol": "600000.SH"}, {"symbol": "000001.SZ"}]}

        @staticmethod
        def quotes(symbols):
            return {
                "600000.SH": SimpleNamespace(is_realtime=True, age_seconds=1.0),
                "000001.SZ": SimpleNamespace(is_realtime=False, age_seconds=1.0),
                "999999.SH": SimpleNamespace(is_realtime=True, age_seconds=1.0),
            }

    result = tripwire.evaluate(FakeDesk(), include_tape=False)
    finding = next(f for f in result["findings"] if f["code"] == "quotes_unavailable")
    assert finding["numbers"]["symbols"] == ["000001.SZ"]
    assert result["checked"]["quotes"] == 2, "未请求的额外报价不能进入计数"
    assert result["checked"]["live_quotes"] == 1

    class StaleDesk(FakeDesk):
        @staticmethod
        def quotes(symbols):
            return {symbol: SimpleNamespace(is_realtime=True, age_seconds=999.0)
                    for symbol in symbols}

    stale = tripwire.evaluate(StaleDesk(), include_tape=False)
    finding = next(f for f in stale["findings"] if f["code"] == "quotes_unavailable")
    assert finding["numbers"]["symbols"] == ["000001.SZ", "600000.SH"]
    assert stale["checked"]["live_quotes"] == 0


def test_a_tape_finding_names_which_series_it_quoted(monkeypatch):
    """分位数来自同花顺序列，``counts.limit_up`` 来自派生池，同一天可能差一两只。

    2026-11-02 实测：findings 说「涨停家数 131」，同一次 wake 的 changes 说
    ``pools.limit_up 132``，两处都没写口径。两个数都对，读的人却无法判断谁错了。
    """
    from service.trading import tape, tripwire

    monkeypatch.setattr(tape, "limit_history", lambda desk, day: {
        "ok": True, "basis": "ths",
        "limit_up": {"today": 131, "percentile": 0.969, "sessions": 1434},
        "seal_rate": {"today": 0.824, "percentile": 0.5, "sessions": 247},
    })

    findings = tripwire._tape(object(), "20261102")

    assert findings, "97% 分位应当触发 tape_extreme"
    assert "131" in findings[0].detail
    assert "同花顺口径" in findings[0].detail, "叫醒理由必须写明是哪一份序列"
    assert findings[0].numbers["basis"] == "ths"


def test_a_history_block_that_does_not_say_its_basis_is_not_called_ths(monkeypatch):
    """没写 basis 的旧响应不许被默认当成同花顺——那正是这次要修的那种含糊。"""
    from service.trading import tape, tripwire

    monkeypatch.setattr(tape, "limit_history", lambda desk, day: {
        "ok": True,
        "limit_up": {"today": 131, "percentile": 0.969, "sessions": 1434},
        "seal_rate": {"today": 0.824, "percentile": 0.5, "sessions": 247},
    })

    findings = tripwire._tape(object(), "20261102")

    assert "厂商口径" in findings[0].detail
    assert "同花顺口径" not in findings[0].detail


def test_a_supplied_frame_is_used_instead_of_a_second_request(desk, monkeypatch):
    """给了帧就不许再取数。

    抓取循环刚刚落下的那一帧和"再问一次厂商"是两个瞬间。同一个判断里出现两个
    现价，读者没法知道那条破线是按哪个价算的——而 `quant_live` 给的永远是前者。
    """
    import pandas as pd

    from service.kernel.clock import SHANGHAI

    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    desk.decisions.approve(ticket["ticket_id"], actor="test")
    stop = Decimal(str(ticket["stop_price"]))

    day = desk.market.latest_bar_date()
    at = datetime.strptime(day, "%Y%m%d").replace(hour=10, minute=30, tzinfo=SHANGHAI)
    frame = pd.DataFrame([{
        "code": symbol.split(".")[0],
        "market": 1 if symbol.endswith(".SH") else 0,
        "name": symbol,
        "last": float(stop) * 0.97, "pre_close": float(stop) * 0.97,
        "open": float(stop) * 0.97, "high": float(stop) * 0.97, "low": float(stop) * 0.97,
        "volume": 100_000, "amount": 1_000_000.0,
        "quote_ts": at.timestamp(), "source": "eastmoney",
    }])
    monkeypatch.setattr(desk, "quotes",
                        lambda *_args, **_kwargs: pytest.fail("must not fetch"))

    result = tripwire.evaluate(desk, include_tape=False, frame=frame, now=at)

    assert result["checked"]["quotes_basis"] == "frame"
    assert result["checked"]["live_quotes"] == 1
    assert "stop_breach" in {f["code"] for f in result["findings"]}
