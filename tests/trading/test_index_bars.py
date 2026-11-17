"""An index is a fourth quote namespace, and it was the one nobody routed.

`quote_dataset` knows three traded namespaces — bond, fund, stock — and an index
fell through to `daily`, which holds zero index rows. `GET /market/bars` then
answered 404 NO_BARS for `000001.SH` while `index_daily` on the same disk held
every session back to 2023. Measured 2026-10-31: 上证指数 3599.02, 创业板指
2413.63, 沪深300 3985.50 — all unreachable from the API.

The routing lives inside `history()` rather than in `quote_dataset` on purpose:
`snapshot()` and `instrument()` build price bands from that resolver, and an
index has neither a band nor a lot to trade.
"""

from __future__ import annotations

import pytest

SHANGHAI = "000001.SH"      # 上证指数
CHINEXT = "399006.SZ"       # 创业板指
CSI300 = "000300.SH"        # 沪深300


@pytest.mark.parametrize("symbol", [SHANGHAI, CHINEXT, CSI300])
def test_an_index_has_history(desk, symbol):
    day = desk.market.latest_bar_date()
    frame = desk.market.history(symbol, day, 5)

    assert not frame.empty, f"{symbol} 的日线在 index_daily 里，history 却不返回"
    assert frame["close"].iloc[-1] > 0
    assert str(frame["trade_date"].iloc[-1]) == day


def test_the_index_and_the_stock_are_not_confused(desk):
    """`000001.SH` is 上证指数; `000001.SZ` is 平安银行. Same digits, other table."""
    day = desk.market.latest_bar_date()
    index = desk.market.history(SHANGHAI, day, 5)
    stock = desk.market.history("000001.SZ", day, 5)

    assert not index.empty and not stock.empty
    # 3599 vs 12 — a prefix-based rule would have returned one for the other.
    assert float(index["close"].iloc[-1]) > 1000
    assert float(stock["close"].iloc[-1]) < 100


def test_adj_close_on_an_index_equals_close(desk):
    """An index has no ex-date, so the anchored adjustment must be a no-op."""
    day = desk.market.latest_bar_date()
    frame = desk.market.history(SHANGHAI, day, 20)

    assert "adj_close" in frame.columns
    assert frame["adj_close"].round(6).tolist() == frame["close"].round(6).tolist()


def test_history_never_reads_past_as_of(desk):
    """The upper bound is what keeps a replay from pricing the future."""
    frame = desk.market.history(SHANGHAI, "20240110", 5)

    assert not frame.empty
    assert str(frame["trade_date"].max()) <= "20240110"
