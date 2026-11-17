"""Readiness decides whether a *conclusion* may be written, not just a quote.

The case that motivated it: on 2026-10-29 the bar was missing, `data_ready` said
so in a field nobody read, and a close report was written off an intraday
snapshot. These tests pin the state machine so "missing data" can never again be
silently downgraded to a boolean someone may ignore.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from service.kernel.clock import SHANGHAI
from service.trading import readiness


class _Calendar:
    def __init__(self, days):
        self.days = days


class _Registry:
    def __init__(self, records=()):
        self._records = list(records)

    def list_manifest(self):
        return self._records


class _Rec:
    def __init__(self, dataset, cover_end, status="ok"):
        self.dataset = dataset
        self.cover_end = cover_end
        self.status = status


class _Store:
    def __init__(self, registry):
        self.registry = registry


class FakeMarket:
    """Only the four things readiness is allowed to ask for."""

    def __init__(self, *, last_bar, days, rows, records=()):
        self.calendar = _Calendar(days)
        self._last_bar = last_bar
        self._rows = rows
        self.store = _Store(_Registry(records))

    def latest_bar_date(self):
        return self._last_bar

    def daily(self, date):
        return pd.DataFrame({"x": range(self._rows.get(date, 0))})


DAYS = ["20261026", "20261027", "20261028", "20261029", "20261030"]
GOOD = {d: 5000 for d in DAYS}


def test_a_published_session_is_ready():
    market = FakeMarket(last_bar="20261029", days=DAYS, rows=GOOD,
                        records=[_Rec("daily", "20261029"), _Rec("stk_limit", "20261029")])
    got = readiness.check(market, day="20261029")

    assert got.ok and got.code == "READY"
    assert got.coverage == 1.0
    assert got.as_dict()["blocks"] == []


def test_the_publish_window_blocks_the_close_report():
    """15:00–17:30 is where the bad report was written; it must be refused."""
    market = FakeMarket(last_bar="20261028", days=DAYS, rows=GOOD)
    got = readiness.check(market, day="20261029")

    assert not got.ok and got.code == "AWAITING_PUBLISH"
    assert got.bars_behind == 1
    assert "close_report" in got.as_dict()["blocks"]
    assert "10-29" not in got.detail  # the message names the raw date, not prose


def test_an_open_session_is_an_observation_not_a_verdict():
    market = FakeMarket(last_bar="20261028", days=DAYS, rows=GOOD)
    now = datetime(2026, 10, 29, 14, 0, tzinfo=SHANGHAI)
    got = readiness.check(market, now=now)

    assert not got.ok and got.code == "SESSION_OPEN"
    assert got.as_dict()["blocks"] == ["verdict", "settlement"]


def test_a_truncated_feed_is_not_a_quiet_day():
    """A bar that exists but covers 60% of yesterday looks like data. It is not."""
    rows = dict(GOOD)
    rows["20261029"] = 3000
    market = FakeMarket(last_bar="20261029", days=DAYS, rows=rows)
    got = readiness.check(market, day="20261029")

    assert not got.ok and got.code == "THIN_COVERAGE"
    assert got.coverage == 0.6
    assert "close_report" in got.as_dict()["blocks"]


def test_a_missing_limit_table_blocks_the_verdict_even_with_prices():
    """Prices without the limit table cannot prove a fill was legal."""
    market = FakeMarket(last_bar="20261029", days=DAYS, rows=GOOD,
                        records=[_Rec("daily", "20261029")])
    got = readiness.check(market, day="20261029")

    assert not got.ok
    assert got.missing == ("stk_limit",)


def test_the_ladder_outage_does_not_block_settlement():
    """ths_ladder explains structure; it does not settle P&L."""
    market = FakeMarket(last_bar="20261029", days=DAYS, rows=GOOD,
                        records=[_Rec("daily", "20261029"), _Rec("stk_limit", "20261029")])
    got = readiness.check(market, day="20261029")

    assert got.ok and got.missing == ()


def test_a_non_trading_day_needs_no_data():
    market = FakeMarket(last_bar="20261029", days=DAYS, rows=GOOD)
    got = readiness.check(market, day="20261101")

    assert got.ok and got.code == "NON_TRADING_DAY"


def test_an_empty_calendar_is_never_ready():
    """Silence must not read as "nothing to settle"."""
    market = FakeMarket(last_bar=None, days=[], rows={})
    got = readiness.check(market)

    assert not got.ok and got.code == "NO_CALENDAR"
