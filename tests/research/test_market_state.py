"""Market-state percentiles and distances must degrade without inventing data."""

from __future__ import annotations

import numpy as np
import pandas as pd

from service.research.market_state import _pct_rank, market_state


class _Store:
    def __init__(self, daily: pd.DataFrame, margin: pd.DataFrame | None = None):
        self.daily = daily
        self.margin = margin

    def read(self, name, **kwargs):
        if name == "daily":
            return self.daily
        if name == "margin_detail" and self.margin is not None:
            return self.margin
        raise KeyError(name)


def _daily(n: int = 420) -> tuple[pd.DataFrame, list[str]]:
    days = pd.bdate_range("2024-01-01", periods=n).strftime("%Y%m%d").tolist()
    rows = []
    for j, day in enumerate(days):
        for i in range(35):
            close = 10 + j * 0.01 + i * 0.02
            previous = 10 + (j - 1) * 0.01 + i * 0.02
            rows.append({"ts_code": f"{i:06d}.SZ", "trade_date": day,
                         "close": close, "pre_close": previous,
                         "amount": 1e6 * (1 + i * 0.01)})
    return pd.DataFrame(rows), days


def test_rolling_percentile_preserves_unknown_current_value():
    series = pd.Series([1.0, 2.0, np.nan, *np.arange(130.0), np.nan])
    assert np.isnan(_pct_rank(series).iloc[-1])


def test_missing_margin_is_a_four_dimension_degradation_not_short_history():
    daily, days = _daily()
    out = market_state(_Store(daily), as_of=days[-1], k=30)
    assert out["ok"] is True, out
    assert out["available_days"] >= 200
    assert out["degraded"] is True
    assert out["missing_metrics"] == ["margin_chg"]
    assert out["picture"]["margin_chg"]["value"] is None
    assert out["picture"]["margin_chg"]["percentile"] is None
    assert out["peers"]["dimensions_today"] == 4
    assert out["peers"]["k"] > 0
    assert out["peers"]["distance_normalization"] == "rmse_over_pairwise_available_dimensions"


def test_current_margin_gap_does_not_drop_the_other_four_dimensions():
    daily, days = _daily()
    margin = pd.DataFrame({
        "ts_code": ["000000.SZ"] * (len(days) - 1),
        "trade_date": days[:-1],
        "rzye": [1e8 + i * 1e5 for i in range(len(days) - 1)],
    })
    out = market_state(_Store(daily, margin), as_of=days[-1], k=30)
    assert out["ok"] is True, out
    assert out["picture"]["amt_rel"]["percentile"] is not None
    assert out["picture"]["margin_chg"]["percentile"] is None
    assert out["missing_metrics"] == ["margin_chg"]
    assert out["peers"]["dimensions_today"] == 4
