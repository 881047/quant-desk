"""实盘那本账：止损看得见，看不见的时候要说出"我看不见"。

2026-11-02 的事故不是止损算错，是**内核从来没打开过那本账**：`quant_stops` 只读模拟盘，
对实盘的 300 股回答"没有持仓，所以没有止损"，整个下午没有告警。
"""

from __future__ import annotations

import json

import pytest

from service.trading.external import ExternalStore
from service.trading.external_stops import external_stops


def _book(root, trades) -> None:
    store = ExternalStore(root / "data" / "external.sqlite")
    store.add_trades(trades, source="test")


BUY = {"trade_date": "20261030", "symbol": "601857.SH", "side": "BUY",
       "qty": 300, "price": 13.60, "fee": 5.0}


def test_a_live_position_gets_a_stop_and_a_distance(tmp_path, desk):
    _book(tmp_path, [BUY])

    out = external_stops(desk, root=tmp_path)

    assert out["account"] == "external"
    assert [row["symbol"] for row in out["stops"]] == ["601857.SH"]
    row = out["stops"][0]
    assert row["qty"] == 300
    assert row["rule"] == "pct5"
    assert row["stop"] == pytest.approx(row["cost"] * 0.95, abs=1e-3)
    assert row["distance_pct"] is not None


def test_a_closed_position_is_not_a_position(tmp_path, desk):
    """0 股的行曾出现在报告里并触发"破止损"，指向的是一笔已经不在的仓位。"""
    _book(tmp_path, [BUY, {"trade_date": "20261030", "symbol": "601857.SH",
                           "side": "SELL", "qty": 300, "price": 13.9, "fee": 6.0}])

    out = external_stops(desk, root=tmp_path)

    assert out["stops"] == []
    assert out["breached"] == []


def test_a_pinned_price_wins_over_every_rule(tmp_path, desk):
    """11.74 是照风险预算反算的，不是任何百分比算得出——所以钉下的价必须优先。"""
    _book(tmp_path, [BUY])
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "stops.json").write_text(
        json.dumps({"601857.SH": {"stop": 11.74, "note": "风险预算"}}), encoding="utf-8")

    row = external_stops(desk, root=tmp_path)["stops"][0]

    assert row["stop"] == pytest.approx(11.74)
    assert row["rule"] == "custom"


def test_an_atr_stop_says_when_it_cannot_compute(tmp_path, desk):
    """算不出来就说算不出来，不能退化成"没有止损"。"""
    _book(tmp_path, [BUY])

    row = external_stops(desk, root=tmp_path, rule="atr")["stops"][0]

    assert row["rule"] == "atr"
    assert row["stop"] is not None or "ATR" in row["note"]


def test_the_book_says_how_old_it_is(tmp_path, desk):
    """账本比今天旧 = 今天的成交没记进来。这句话必须出现在响应里。"""
    _book(tmp_path, [BUY])

    out = external_stops(desk, root=tmp_path)

    assert out["book_as_of"] == "20261030"
    import datetime as dt

    from service.kernel.clock import SHANGHAI

    today = dt.datetime.now(SHANGHAI).strftime("%Y%m%d")
    if today > "20261030" and desk.market.calendar.is_trading_day(today):
        assert out["stale_book"] is True
        assert "还没有记录" in out["note"]


def test_an_empty_book_is_not_the_same_as_a_paper_position(tmp_path, desk):
    out = external_stops(desk, root=tmp_path)

    assert out["stops"] == []
    assert out["book_as_of"] is None
