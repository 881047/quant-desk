"""The real account: one fill, one row — and two anchors that must not be mixed."""

from __future__ import annotations

import pytest

from service.trading.external import (
    ExternalStore,
    levels,
    limit_pct,
    normalize_date,
    normalize_side,
    normalize_symbol,
    reconcile,
    trade_key,
)


def _buy(symbol="000700.SZ", qty=100, price=13.07, fee=5.01, day="20261026", **kw):
    row = {"trade_date": day, "trade_time": "14:59:00", "symbol": symbol,
           "side": "BUY", "qty": qty, "price": price, "fee": fee,
           "amount": qty * price, "broker_id": kw.get("broker_id", "")}
    row["trade_key"] = trade_key(row)
    return row


def test_symbols_and_dates_from_whatever_the_broker_writes():
    assert normalize_symbol("000700") == "000700.SZ"
    assert normalize_symbol("SZ000700") == "000700.SZ"
    assert normalize_symbol("000700.sz") == "000700.SZ"
    assert normalize_symbol("600000") == "600000.SH"
    assert normalize_symbol("830799") == "830799.BJ"
    assert normalize_symbol("700") == ""
    assert normalize_date("2026-10-26") == "20261026"
    assert normalize_date("2026/10/26 14:59") == "20261026"
    assert normalize_date("20261026") == "20261026"
    assert normalize_side("证券买入") == "BUY"
    assert normalize_side("卖出") == "SELL"
    assert normalize_side("") == ""


def test_reimporting_the_same_statement_is_a_no_op(tmp_path):
    """A statement re-exported tomorrow contains yesterday's fills again."""
    store = ExternalStore(tmp_path / "external.sqlite")
    rows = [_buy(), _buy(day="20261023", price=10.80)]
    assert store.add_trades(rows)["inserted"] == 2
    again = store.add_trades(rows)
    assert again == {"inserted": 0, "duplicates": 2}
    assert len(store.trades()) == 2


def test_cost_includes_fees_but_the_stop_does_not(tmp_path):
    """Stop on the price the market trades at, break-even on what you paid."""
    store = ExternalStore(tmp_path / "external.sqlite")
    store.add_trades([_buy()])
    held = store.holdings()["000700.SZ"]
    assert held.qty == 100
    assert held.cost == pytest.approx(13.1201, abs=1e-4)   # (13.07×100 + 5.01)/100
    assert held.avg_price == pytest.approx(13.07, abs=1e-6)

    out = levels(symbol="000700.SZ", qty=held.qty, cost=held.cost,
                 entry=held.avg_price, prev_close=11.88, last=13.07,
                 auction_anchor=13.07)
    assert out["stop"] == 12.42, "13.07 × 0.95; a fee-dependent stop is not a stop"
    assert out["breakeven"] == pytest.approx(13.1201, abs=1e-4)


def test_auction_line_anchors_on_the_latest_close():
    """Tomorrow's gap is measured from tonight's close, not from the day before."""
    out = levels(symbol="000700.SZ", qty=100, cost=13.12, entry=13.07,
                 prev_close=11.88, last=13.07, auction_anchor=13.07)
    assert out["auction_line"] == 12.81, "13.07 × 0.98 — the level the plan quoted"
    assert out["auction_line"] != round(11.88 * 0.98, 2)


def test_pnl_today_is_zero_on_the_day_you_buy():
    """A position opened today cannot earn yesterday's gap."""
    out = levels(symbol="000700.SZ", qty=100, cost=13.1201, entry=13.07,
                 prev_close=11.88, last=13.07, opened_today=True,
                 auction_anchor=13.07)
    assert out["pnl_today"] == pytest.approx(-5.01), "only the fee is lost today"
    assert out["pnl_total"] == pytest.approx(-5.01)

    held_before = levels(symbol="000700.SZ", qty=100, cost=13.1201, entry=13.07,
                         prev_close=11.88, last=13.07, opened_today=False,
                         auction_anchor=13.07)
    assert held_before["pnl_today"] == 119, "held from yesterday: the gap is yours"


def test_board_limits_follow_the_board():
    assert limit_pct("002966.SZ") == 0.10
    assert limit_pct("300750.SZ") == 0.20
    assert limit_pct("688981.SH") == 0.20
    assert limit_pct("830799.BJ") == 0.30
    # 主板 ST 自 2026-07-06 起与主板普通股同为 10%（交易所把风险警示板的
    # 涨跌幅与主板统一了）。以前这里是 0.05——那会让实盘监控在 -5% 就喊跌停。
    assert limit_pct("002966.SZ", st=True) == 0.10


def test_the_band_is_one_rule_for_every_board_and_every_caller():
    """The rule had four copies; two of them were wrong in ways that fire alerts.

    * ``920xxx`` is the Beijing range that replaced some NEEQ codes — 30%, not
      the 10% a missing prefix produced. A real -30% limit-down was invisible
      while a -10% day was announced as "跌停".
    * The board decides, not the name: ChiNext and STAR keep 20% after a name
      is flagged, and main-board ST has been 10% since 2026-07-06.
    * 未完成股改的股票（简称冠 "S"）恒为 5%，优先于风险警示规则——佳通轮胎
      2023-05-16 公告明写撤销 ST 后"日涨跌幅限制不变，仍为 5%"。
    * A convertible carries a 20% band, 10-unit lots and no stamp duty.
    """
    assert limit_pct("920001.BJ") == 0.30
    assert limit_pct("300750.SZ", st=True) == 0.20
    assert limit_pct("688111.SH", st=True) == 0.20
    assert limit_pct("110043.SH") == 0.20
    assert limit_pct("002966.SZ", st=True) == 0.10
    assert limit_pct("600182.SH", name="S佳通") == 0.05
    assert limit_pct("600182.SH", name="S*ST佳通", st=True) == 0.05

    # And the desk's instrument view must agree with the panel's, because they
    # feed two different readers of the same level.
    from service.adapters.symbols import limit_pct as band

    for symbol, st in (("920001.BJ", False), ("300750.SZ", True), ("002966.SZ", True)):
        assert float(band(symbol, is_st=st)) == limit_pct(symbol, st=st)


def test_selling_removes_a_proportional_slice_of_cost(tmp_path):
    store = ExternalStore(tmp_path / "external.sqlite")
    store.add_trades([_buy(qty=200, price=10.00, fee=0.0)])
    sell = _buy(qty=100, price=12.00, fee=0.0)
    sell.update(side="SELL", broker_id="s1")
    sell["trade_key"] = trade_key(sell)
    store.add_trades([sell])
    held = store.holdings()["000700.SZ"]
    assert held.qty == 100
    assert held.cost == pytest.approx(10.00, abs=1e-6)
    assert held.realized == pytest.approx(200.00, abs=1e-6)


def test_no_snapshot_is_not_a_mismatch(tmp_path):
    """Until a position table is imported, the ledger cannot be 'wrong'."""
    store = ExternalStore(tmp_path / "external.sqlite")
    store.add_trades([_buy()])
    out = reconcile(store)
    assert out["ok"] is True
    assert out["snapshot_missing"] is True
    assert out["mismatches"] == []


def test_a_dividend_shows_up_as_a_position_gap(tmp_path):
    """送股/转股 change the position without producing a fill; say so."""
    store = ExternalStore(tmp_path / "external.sqlite")
    store.add_trades([_buy(qty=100)])
    store.save_snapshot("20261027", [{"symbol": "000700.SZ", "qty": 130,
                                      "cost": 13.07, "market_value": 1699.1}])
    out = reconcile(store)
    assert out["ok"] is False
    assert out["mismatches"] == [{"symbol": "000700.SZ", "broker_qty": 130,
                                  "ledger_qty": 100, "delta": 30}]


def test_a_complete_snapshot_replaces_rows_missing_from_the_new_file(tmp_path):
    store = ExternalStore(tmp_path / "external.sqlite")
    store.replace_snapshot("20261027", [
        {"symbol": "000700.SZ", "qty": 100},
        {"symbol": "002966.SZ", "qty": 200},
    ])
    store.replace_snapshot("20261027", [{"symbol": "000700.SZ", "qty": 100}])
    assert set(store.snapshot("20261027")) == {"000700.SZ"}


def test_an_invalid_snapshot_row_cannot_erase_the_previous_snapshot(tmp_path):
    store = ExternalStore(tmp_path / "external.sqlite")
    store.replace_snapshot("20261027", [{"symbol": "000700.SZ", "qty": 100}])
    before = store.snapshot("20261027")
    with pytest.raises(ValueError):
        store.replace_snapshot("20261027", [
            {"symbol": "002966.SZ", "qty": 200},
            {"symbol": "000590.SZ", "qty": "not-a-number"},
        ])
    assert store.snapshot("20261027") == before


def test_zero_available_is_not_replaced_with_total_quantity(tmp_path):
    store = ExternalStore(tmp_path / "external.sqlite")
    store.save_snapshot("20261027", [{"symbol": "000700.SZ", "qty": 100, "available": 0}])
    assert store.snapshot("20261027")["000700.SZ"]["available"] == 0

    store.replace_snapshot("20261028", [{"symbol": "000700.SZ", "qty": 100, "available": 0}])
    assert store.snapshot("20261028")["000700.SZ"]["available"] == 0
    store.replace_snapshot("20261028", [{"symbol": "000700.SZ", "qty": 100}])
    assert store.snapshot("20261028")["000700.SZ"]["available"] == 100


def test_historical_reconcile_excludes_fills_after_the_snapshot(tmp_path):
    store = ExternalStore(tmp_path / "external.sqlite")
    store.add_trades([_buy(day="20261027"), _buy(day="20261028")])
    store.replace_snapshot("20261027", [{"symbol": "000700.SZ", "qty": 100}])
    out = reconcile(store, as_of="20261027")
    assert out["ok"] is True
    assert out["mismatches"] == []


def test_failed_alert_claim_is_retryable_but_sent_alert_is_permanent(tmp_path):
    store = ExternalStore(tmp_path / "external.sqlite")
    claim = {"key": "20261027|stop|000700.SZ", "day": "20261027", "rule": "stop",
             "symbol": "000700.SZ", "severity": "warn", "title": "破止损"}
    assert store.claim_alert(**claim) is True
    assert store.release_alerts([claim["key"]], note="mail failed") == 1
    assert store.claim_alert(**claim) is True, "未发送的失败 claim 必须能重试"

    store.mark_alert_sent([claim["key"]])
    assert store.release_alerts([claim["key"]], note="late failure") == 0
    assert store.claim_alert(**claim) is False, "已发送告警不能回收后重发"
    assert store.alerts(day="20261027")[0]["status"] == "sent"
