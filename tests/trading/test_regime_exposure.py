"""Regime and exposure: the two facts a strategy needs before it sizes up."""

from __future__ import annotations

import pandas as pd
import pytest

from service.trading import exposure
from service.trading.exposure import describe
from service.trading.regime import compute
from tests.trading.helpers import pick_symbols, proposal


def test_regime_returns_a_known_label_with_bounded_components(desk):
    regime = compute(desk.market)

    assert regime.label in {"risk_on", "neutral", "risk_off", "stressed"}
    assert regime.trend in {"up", "down"}
    assert regime.vol_state in {"calm", "normal", "stressed"}
    assert 0.0 <= regime.breadth <= 1.0
    assert 0.0 <= regime.vol_percentile <= 1.0
    assert regime.reasons, "a label without its numbers is an opinion"


def test_regime_is_cached_for_the_same_session(desk):
    first = compute(desk.market)
    second = compute(desk.market)
    assert first is second, "recomputing 300 sessions per tool call is waste"


def test_stressed_volatility_overrides_the_trend_label(desk):
    regime = compute(desk.market)
    if regime.vol_state == "stressed":
        assert regime.label == "stressed"
    else:
        assert regime.label != "stressed"


def test_exposure_weights_sum_to_the_invested_share(desk):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol, target_weight=0.2))
    assert ticket["status"] == "proposed", ticket.get("reasons")
    desk.decisions.approve(ticket["ticket_id"])

    exposure = describe(desk.book, desk.market)
    held = sum(p["weight"] for p in exposure.positions)  # stored rounded to 4dp
    assert held == pytest.approx(exposure.invested, abs=5e-4)
    assert exposure.cash_weight + exposure.invested == pytest.approx(1.0, abs=1e-9)
    assert sum(exposure.by_industry.values()) == pytest.approx(exposure.invested, abs=1e-9)
    assert sum(exposure.by_board.values()) == pytest.approx(exposure.invested, abs=1e-9)


def test_one_theme_is_one_position(desk):
    """Two names from the same industry must not add up past the industry cap."""
    day = desk.market.latest_bar_date()
    daily = desk.market.daily(day)
    basic = desk.market.basic()[["ts_code", "name", "industry"]]
    frame = daily.merge(basic, on="ts_code", how="left")
    frame = frame[(pd.to_numeric(frame["close"], errors="coerce") <= 9.5)
                  & (pd.to_numeric(frame["close"], errors="coerce") > 3)
                  & (pd.to_numeric(frame["vol"], errors="coerce") > 0)]
    frame = frame[~frame["ts_code"].astype(str).str.endswith(".BJ")]
    frame = frame[~frame["name"].astype(str).str.contains("ST", na=False)]
    counts = frame.groupby("industry").size().sort_values(ascending=False)
    pair = None
    for industry, count in counts.items():
        if count >= 2:
            pair = (industry, frame[frame["industry"] == industry]["ts_code"].astype(str)
                    .head(2).tolist())
            break
    if pair is None:
        pytest.skip("no industry with two tradable names in the fixture data")
    industry, symbols = pair

    first = desk.decisions.propose(proposal(symbols[0], target_weight=0.3))
    assert first["status"] == "proposed", first.get("reasons")
    desk.decisions.approve(first["ticket_id"])

    second = desk.decisions.propose(proposal(symbols[1], target_weight=0.3))
    assert second["status"] == "rejected"
    assert any("industry_cap" in r for r in second["reasons"]), (industry, second["reasons"])


def test_beta_returns_none_when_none_of_the_positions_have_series():
    frame = pd.DataFrame({
        "trade_date": [f"202601{i:02d}" for i in range(1, 26)],
        "ts_code": ["OTHER.SH"] * 25,
        "close": list(range(10, 35)),
    })
    from types import SimpleNamespace

    market = SimpleNamespace(panel=lambda day, count: frame)
    assert exposure._beta_uncached(market, "20260125", {"MISSING.SH": 1.0}) is None


def test_beta_cache_key_keeps_each_weight_with_its_sorted_symbol():
    class CachedMarket:
        def __init__(self):
            self.keys = []

        def _cached(self, key, fn):
            self.keys.append(key)

    market = CachedMarket()
    exposure._beta(market, "20260125", ["A", "B"], {"B": 0.25, "A": 0.75})
    exposure._beta(market, "20260125", ["A", "B"], {"A": 0.75, "B": 0.25})
    assert len(market.keys) == 2 and market.keys[0] == market.keys[1]
    # The invariant is the pairing, not the precision. Eight decimals made every
    # mark a new key in a 512-slot cache shared with the year partitions, so the
    # expensive frames were evicted by a beta nobody asked for; three decimals is
    # the same ordering with a stable key.
    assert "A:0.750,B:0.250" in market.keys[0]


def test_unrealized_return_uses_cost_not_market_value():
    from types import SimpleNamespace

    book = SimpleNamespace(summary=lambda: {
        "equity": "1000", "cash_available": "880", "cash_frozen": "0",
        "positions": [{"symbol": "600000.SH", "name": "测试", "qty": 10,
                       "available_qty": 10, "market_value": "120", "avg_cost": "10",
                       "unrealized_pnl": "20"}],
    })
    market = SimpleNamespace(latest_bar_date=lambda: "20260125",
                             basic=lambda: pd.DataFrame(),
                             panel=lambda day, count: pd.DataFrame())
    out = exposure.describe(book, market)
    assert out.positions[0]["unrealized_pnl_pct"] == pytest.approx(0.2)


def test_a_flat_book_says_why_beta_does_not_apply(desk):
    """空仓时 beta 是 null，但 null 旁边要有原因：不适用 ≠ 算不出来。"""
    from service.trading.exposure import describe

    out = describe(desk.book, desk.market, desk.market.latest_bar_date()).as_dict()

    if out.get("beta_vs_equal_weight") is None:
        # 说明的键名必须跟字段名对上（`<key>_note`），否则读的人/探查器都找不到它。
        note = out.get("beta_vs_equal_weight_note")
        assert note, "空值必须自带原因"
        assert ("空仓" in note) or ("历史不足" in note)
