"""Hard stops: a stop that is never executed is a lie, so these tests pin it."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from service.trading import stops
from service.trading.stops import open_stops
from tests.trading.helpers import pick_symbols, proposal


def _buy(desk, symbol):
    ticket = desk.decisions.propose(proposal(symbol, target_weight=0.2))
    assert ticket["status"] == "proposed", ticket.get("reasons")
    desk.decisions.approve(ticket["ticket_id"])
    return ticket


def _break(desk, monkeypatch, symbol, stop, *, low=None):
    real = desk.market.snapshot

    def fake(sym, day=None, *args, **kwargs):
        snap = real(sym, day, *args, **kwargs)
        if snap is not None and sym == symbol:
            return replace(snap, last=stop - Decimal("0.01"),
                           low=low if low is not None else stop - Decimal("0.02"))
        return snap

    monkeypatch.setattr(desk.market, "snapshot", fake)


def test_a_stop_that_is_breached_is_detected(desk, monkeypatch):
    symbol = pick_symbols(desk, 1)[0]
    ticket = _buy(desk, symbol)
    stop = Decimal(str(ticket["stop_price"]))
    _break(desk, monkeypatch, symbol, stop)

    views = {v.symbol: v for v in open_stops(desk, desk.market.latest_bar_date())}
    assert views[symbol].breached is True
    assert views[symbol].stop == stop


def test_a_breach_on_frozen_shares_is_reported_as_blocked(desk, monkeypatch):
    """T+1 is a fact to report, not a reason to pretend the breach did not happen."""
    symbol = pick_symbols(desk, 1)[0]
    ticket = _buy(desk, symbol)
    _break(desk, monkeypatch, symbol, Decimal(str(ticket["stop_price"])))

    views = {v.symbol: v for v in open_stops(desk, desk.market.latest_bar_date())}
    assert views[symbol].breached is True
    assert views[symbol].blocked is True, "today's purchase cannot be sold today"
    # And nothing sold it on its own.
    assert desk.book.ledger.account.position(symbol).total_qty > 0


def test_the_stops_module_has_no_way_to_sell(desk):
    """The kernel measures the breach. It does not close it.

    There used to be an `enforce()` here that proposed and approved a SELL with
    `actor="stops"` and its own thesis ("independent of model judgement"). A
    kernel that prices the exit and then takes it has stopped being an
    instrument. If this test fails, someone has put the trigger back.
    """
    assert not hasattr(stops, "enforce")
    offenders = [name for name in dir(stops)
                 if any(word in name.lower() for word in ("sell", "execute", "close"))
                 and not name.startswith("__")]
    assert offenders == [], f"stops should not be able to trade: {offenders}"


def test_an_intraday_pierce_that_recovers_is_reported_not_traded(desk, monkeypatch):
    symbol = pick_symbols(desk, 1)[0]
    ticket = _buy(desk, symbol)
    stop = Decimal(str(ticket["stop_price"]))
    real = desk.market.snapshot

    def fake(sym, day=None, *args, **kwargs):
        snap = real(sym, day, *args, **kwargs)
        if snap is not None and sym == symbol:
            return replace(snap, last=stop + Decimal("0.50"), low=stop - Decimal("0.10"))
        return snap

    monkeypatch.setattr(desk.market, "snapshot", fake)
    views = {v.symbol: v for v in open_stops(desk, desk.market.latest_bar_date())}
    assert views[symbol].breached is False
    assert views[symbol].pierce_only is True
    assert desk.book.ledger.account.position(symbol).total_qty > 0


def _frame(prices: list[float]):
    import pandas as pd

    times = [f"2026-10-23 {9 + (i * 5) // 60:02d}:{(i * 5) % 60:02d}:00" for i in range(len(prices))]
    return pd.DataFrame({"ts_code": "002966.SZ", "trade_time": times,
                         "open": prices, "high": [p * 1.005 for p in prices],
                         "low": [p * 0.995 for p in prices], "close": prices,
                         "vol": [1000] * len(prices), "amount": [1e6] * len(prices)})


def test_intraday_breach_is_located_at_the_first_touch():
    from service.trading.intraday import describe

    frame = _frame([9.50, 9.20, 8.85, 8.95, 9.10])
    info = describe(frame, "002966.SZ", "20261023", stop=Decimal("8.90"))
    assert info is not None
    assert info.breach_at is not None
    assert info.breach_at.endswith(("09:10:00", "09:11:00", "09:12:00")) or "09:1" in info.breach_at
    assert info.bars == 5


def test_no_touch_means_no_breach_time():
    from service.trading.intraday import describe

    frame = _frame([9.50, 9.40, 9.30])
    info = describe(frame, "002966.SZ", "20261023", stop=Decimal("8.90"))
    assert info is not None and info.breach_at is None


def test_intraday_low_comes_from_low_and_all_nan_is_empty():
    from service.trading.intraday import describe

    frame = _frame([10.0, 9.0])
    frame["low"] = [8.25, 8.75]
    info = describe(frame, "002966.SZ", "20261023")
    assert info is not None and info.low == Decimal("8.25")

    frame[["low", "high", "close"]] = float("nan")
    info = describe(frame, "002966.SZ", "20261023")
    assert info is not None
    assert info.low is None and info.high is None
    assert info.worst_drawdown == 0.0


# ── 止损只查"当前持有"的那几只，不扫全历史 ────────────────────────────────
def test_stop_map_only_queries_held_symbols():
    """``stop_map`` 把持仓代码交给存储层过滤，而不是让调用方扫 500 张票据。

    修改前每次提案要解码最近 500 张已成交票据（实测 4.1 ms/次，而
    ``stop_gate`` 调了两次）；更糟的是持有超过 500 张票据之后，止损会
    **静默消失**——"没有止损"在这个台子里是一个合法状态，所以没人会发现。
    """
    from types import SimpleNamespace

    calls: list[list[str]] = []
    desk = SimpleNamespace(
        paper=SimpleNamespace(stop_tickets=lambda syms: calls.append(list(syms)) or []),
        book=SimpleNamespace(ledger=SimpleNamespace(account=SimpleNamespace(
            positions={"600000.SH": SimpleNamespace(total_qty=100),
                       "000001.SZ": SimpleNamespace(total_qty=0)}))),
    )

    assert stops.stop_map(desk) == {}
    assert calls == [["600000.SH"]], "已清仓的代码不该进查询"


# ── 止损相容性测算：纯事实，不含 action / 建议 ──────────────────────────────
def test_stop_risk_context_reproduces_the_budget_vs_noise_conflict():
    """The 2026-11-09 audit did this arithmetic by hand; the kernel must own it.

    002185.SZ, 2026-11-06 close: last 14.40, ATR20 6.99% of price, 100 shares,
    ¥5,000 equity, 4% per-ticket budget. A pct5 stop (0.72) is narrower than one
    ATR; a 2.5xATR stop risks ¥251.62 on one lot — 25.8% over the ¥200 budget.
    """
    from service.trading.stops import stop_risk_context

    out = stop_risk_context(last=14.40, stop=14.40 * 0.95, qty=100,
                            equity=5000.0, max_trade_risk=0.04,
                            atr20_fraction=0.0699)
    assert out["available"] is True
    assert out["risk_budget_cny"] == 200.0
    assert out["stop_width_cny"] == pytest.approx(0.72, abs=1e-4)
    assert out["risk_to_stop_cny"] == pytest.approx(72.0, abs=0.01)
    assert out["risk_budget_used_pct"] == pytest.approx(0.36, abs=1e-3)
    assert out["stop_width_in_atr"] < 1.0, "a 0.72-ATR stop sits inside daily noise"
    assert out["atr_rule"]["one_lot_risk_cny"] > out["risk_budget_cny"]
    assert out["atr_rule"]["budget_used_pct"] > 1.0
    assert "action" not in out, "the kernel measures; it does not decide"


def test_stop_risk_context_is_dimensionless_and_never_invents_numbers():
    from service.trading.stops import stop_risk_context

    # ATR is passed as a fraction of the *adjusted* close; the ratio must be
    # scale-free. Doubling every price leaves the ATR multiple unchanged.
    small = stop_risk_context(last=10.0, stop=9.5, qty=100, equity=5000.0,
                              max_trade_risk=0.04, atr20_fraction=0.05)
    large = stop_risk_context(last=20.0, stop=19.0, qty=100, equity=5000.0,
                              max_trade_risk=0.04, atr20_fraction=0.05)
    assert small["stop_width_in_atr"] == large["stop_width_in_atr"] == 1.0

    # Missing ATR still answers the budget side, with explicit nulls.
    no_atr = stop_risk_context(last=10.0, stop=9.5, qty=100, equity=5000.0,
                               max_trade_risk=0.04)
    assert no_atr["available"] is True
    assert no_atr["atr20_pct"] is None and no_atr["stop_width_in_atr"] is None
    assert no_atr["atr_rule"] is None
    assert no_atr["risk_budget_cny"] == 200.0

    # Invalid inputs are refused, not filled with plausible-looking zeros.
    for bad in ({"last": 0.0}, {"stop": -1.0}, {"qty": 0},
                {"equity": 0.0}, {"max_trade_risk": 0.0},
                {"last": float("nan")}, {"atr20_fraction": float("inf")}):
        args = {"last": 10.0, "stop": 9.5, "qty": 100, "equity": 5000.0,
                "max_trade_risk": 0.04}
        args.update(bad)
        got = stop_risk_context(**args)
        # An infinite ATR is ignored rather than poisoning the whole answer.
        if "atr20_fraction" in bad:
            assert got["available"] is True
            assert got["stop_width_in_atr"] is None
        else:
            assert got["available"] is False
            assert "reason" in got


def test_stop_tickets_returns_the_newest_filled_buy_per_symbol(tmp_path):
    """一只票反复交易过很多次，止损只认**最新那一笔买入**。"""
    from service.trading.state import PaperStore

    store = PaperStore(tmp_path / "p.sqlite")
    base = {"side": "BUY", "status": "filled", "stop_price": "9.80",
            "sleeve": "CORE", "created_at": "2026-11-01T09:30:00+08:00"}
    store.save_ticket(dict(base, ticket_id="old", symbol="600000.SH"))
    store.save_ticket(dict(base, ticket_id="new", symbol="600000.SH",
                           created_at="2026-11-04T09:30:00+08:00"))
    store.save_ticket(dict(base, ticket_id="other", symbol="000001.SZ"))
    store.save_ticket(dict(base, ticket_id="sell", symbol="600000.SH", side="SELL",
                           created_at="2026-11-05T09:30:00+08:00"))

    got = store.stop_tickets(["600000.SH"])
    assert [row["ticket_id"] for row in got] == ["new"]
    assert store.stop_tickets([]) == []
