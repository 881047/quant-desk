"""The external-account console report must never look like an empty book.

Quotes for the external book come from stored daily bars, so between the 15:00
close and the 17:30 backfill they are unavailable *every* trading day. The first
version dropped an unpriced holding from the table entirely, which left the
header reading "市值 0 成本 0" — line-for-line identical to holding nothing. A
position that exists but cannot be priced has to keep its row; only the price
and the market value are unknown.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "external.py"
    spec = importlib.util.spec_from_file_location("external_account", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(**over):
    base = {
        "as_of": "20261102",
        "mode": "close",
        "positions": [],
        "market_value": 0.0,
        "cost_value": 0.0,
        "pnl_today": 0.0,
        "pnl_total": 0.0,
        "pnl_total_pct": None,
        "realized_total": 0.0,
        "breaches": [],
        "priced": 0,
        "unpriced": [],
        "unpriced_holdings": [],
        "totals_partial": False,
    }
    base.update(over)
    return base


def _holding(symbol="002602.SZ", qty=300, cost=12.377):
    return {"symbol": symbol, "qty": qty, "cost": cost,
            "cost_value": round(cost * qty, 2)}


def test_a_fully_unpriced_book_does_not_read_as_an_empty_one():
    out = _module()._render_console(_payload(
        unpriced=["002602.SZ"],
        unpriced_holdings=[_holding()],
        totals_partial=True,
    ))
    assert "市值 0" not in out
    assert "市价不可用" in out
    assert "002602.SZ" in out and "300股" in out
    assert "12.377" in out


def test_an_unpriced_row_keeps_its_shares_and_cost():
    out = _module()._render_console(_payload(
        unpriced=["002602.SZ"],
        unpriced_holdings=[_holding()],
        totals_partial=True,
    ))
    line = next(row for row in out.splitlines() if "002602.SZ" in row)
    assert "300股" in line
    assert "12.377" in line
    assert "未计入市值" in line


def test_a_fully_priced_book_keeps_the_old_totals_line():
    priced = {"symbol": "002602.SZ", "qty": 300, "last": 12.42, "cost": 12.377,
              "pnl_today": 18.0, "pnl_total": 13.0, "pnl_total_pct": 0.35,
              "stop": None, "auction_line": None, "up_limit": None,
              "down_limit": None, "breach_stop": False, "breach_auction": False}
    out = _module()._render_console(_payload(
        positions=[priced], market_value=3726.0, cost_value=3713.1,
        pnl_today=18.0, pnl_total=13.0, pnl_total_pct=0.35, priced=1,
    ))
    assert "市值 3,726" in out
    assert "市价不可用" not in out
    assert "未计入市值" not in out


def test_a_partly_priced_book_shows_both_kinds_of_row():
    priced = {"symbol": "601857.SH", "qty": 300, "last": 14.02, "cost": 13.617,
              "pnl_today": 54.0, "pnl_total": 121.0, "pnl_total_pct": 2.96,
              "stop": None, "auction_line": None, "up_limit": None,
              "down_limit": None, "breach_stop": False, "breach_auction": False}
    out = _module()._render_console(_payload(
        positions=[priced], market_value=4206.0, cost_value=4085.1,
        pnl_today=54.0, pnl_total=121.0, pnl_total_pct=2.96,
        priced=1, unpriced=["002602.SZ"], unpriced_holdings=[_holding()],
        totals_partial=True,
    ))
    assert "601857.SH" in out and "14.02" in out
    assert "002602.SZ" in out and "未计入市值" in out
    assert "市值 4,206" in out
