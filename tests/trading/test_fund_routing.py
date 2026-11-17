"""ETFs are priced from `fund_daily`, and they book into the same account.

The desk had two accounts: the paper book (stocks, and convertibles since today)
and a separate `CoreBook` with its own `core_trade` / `core_nav` tables. The core
book never traded — the rule asks for 100% 511010 in a defensive tape, one lot of
which is ~CNY 14,700 against a CNY 5,000 account — so its curve was a flat 5,000
and carried no information. Meanwhile the ETF path was closed one layer below the
instrument rules: `daily` holds zero ETFs (0 of 18), so `snapshot` returned None
and nothing could be priced at all.

These tests pin the merged state: one quote namespace resolver, one ledger.
"""

from __future__ import annotations

import pytest

CORE_ETF = "510300.SH"        # 沪深300, stock ETF -> T+1
BOND_ETF = "511010.SH"        # 国债, T+0
CROSS_ETF = "513100.SH"       # 纳指, T+0


def test_each_namespace_reads_its_own_file(desk):
    assert desk.market.quote_dataset("600519.SH") == "daily"
    assert desk.market.quote_dataset("113050.SH") == "cb_daily"
    assert desk.market.quote_dataset(CORE_ETF) == "fund_daily"


def test_an_etf_is_priced_at_all(desk):
    """Before the routing, `daily` had no row for any ETF and this was None."""
    day = desk.market.latest_bar_date()
    snap = desk.market.snapshot(CORE_ETF, day)

    assert snap is not None
    assert snap.source == "fund_daily"
    assert snap.last > 0


def test_an_etf_has_liquidity_not_zero(desk):
    """ADV 0 is read by guard.liquidity as "unknown" and blocks the buy.

    This is the same one-layer-below failure that shut the convertible path:
    the instrument rules were right and the liquidity table was empty.
    """
    day = desk.market.latest_bar_date()
    adv = float(desk.market.adv_map(day).get(CORE_ETF, 0.0))

    assert adv > 1e6


@pytest.mark.parametrize("symbol,expected", [(CORE_ETF, 1), (BOND_ETF, 0), (CROSS_ETF, 0)])
def test_settlement_follows_the_product_not_the_prefix(desk, symbol, expected):
    day = desk.market.latest_bar_date()

    assert desk.market.instrument(symbol, day).t_plus == expected


def test_the_volume_unit_matches_the_stock_feed(desk):
    """`vol` is 手 in both feeds (measured: amount*1000/(vol*close) ≈ 100)."""
    # The fund feed lands after the stock one, so its own cover is the date to
    # read it at — `latest_bar_date()` is the stock feed's answer.
    day = desk.market.dataset_cover_end("fund_daily")
    frame = desk.market.fund_daily(day)
    row = frame.iloc[0]
    ratio = float(row["amount"]) * 1000 / (float(row["vol"]) * float(row["close"]))

    assert 50 < ratio < 200, f"unexpected unit ratio {ratio}"


def test_a_board_lot_of_the_index_etf_is_affordable(desk):
    """The merge is only meaningful if the account can actually buy one lot."""
    day = desk.market.latest_bar_date()
    snap = desk.market.snapshot(CORE_ETF, day)
    equity = float(desk.book.summary()["equity"])

    assert snap.last * 100 <= equity * 0.5
