"""Synthetic timing checks for the standalone ETF baseline backtest."""

from __future__ import annotations

import pandas as pd
import pytest
from pandas.testing import assert_series_equal

from scripts.backtest_baseline import _next_open_returns


def _frame(values: list[float], dates: pd.DatetimeIndex, name: str = "A") -> pd.DataFrame:
    return pd.DataFrame({name: values}, index=dates)


def test_signal_close_does_not_change_next_open_realized_return():
    dates = pd.date_range("2026-01-05", periods=3, freq="B")
    target = _frame([1.0, 1.0, 1.0], dates)
    open_ = _frame([100.0, 120.0, 132.0], dates)
    close = _frame([100.0, 132.0, 138.6], dates)
    changed_close = close.copy()
    changed_close.iloc[0, 0] = 1_000.0

    original = _next_open_returns(open_, close, target, 0.0)[0]
    changed = _next_open_returns(open_, changed_close, target, 0.0)[0]

    assert_series_equal(original, changed)
    assert original.iloc[1] == pytest.approx(132.0 / 120.0 - 1)


def test_close_target_cannot_earn_same_day_close_to_close_return():
    dates = pd.date_range("2026-01-05", periods=2, freq="B")
    target = _frame([1.0, 1.0], dates)
    open_ = _frame([100.0, 200.0], dates)
    close = _frame([130.0, 220.0], dates)

    ret, weights, _ = _next_open_returns(open_, close, target, 0.0)

    assert weights.iloc[0, 0] == 0.0
    assert ret.iloc[0] == 0.0
    assert ret.iloc[1] == pytest.approx(0.10)


def test_entry_cost_is_charged_at_execution_open_before_intraday_return():
    dates = pd.date_range("2026-01-05", periods=2, freq="B")
    target = _frame([1.0, 1.0], dates)
    open_ = _frame([100.0, 120.0], dates)
    close = _frame([100.0, 132.0], dates)
    cost = 0.01

    ret, _, turnover = _next_open_returns(open_, close, target, cost)

    assert turnover.tolist() == [0.0, 1.0]
    assert ret.iloc[0] == 0.0
    assert ret.iloc[1] == pytest.approx((1 - cost) * (132.0 / 120.0) - 1)


def test_switch_keeps_overnight_on_old_holding_and_charges_at_that_open():
    dates = pd.date_range("2026-01-05", periods=3, freq="B")
    target = pd.DataFrame({"A": [1.0, 0.0, 0.0], "B": [0.0, 1.0, 1.0]}, index=dates)
    open_ = pd.DataFrame(
        {"A": [100.0, 110.0, 132.0], "B": [100.0, 100.0, 200.0]}, index=dates
    )
    close = pd.DataFrame(
        {"A": [100.0, 121.0, 140.0], "B": [100.0, 100.0, 220.0]}, index=dates
    )
    cost = 0.001

    ret, weights, turnover = _next_open_returns(open_, close, target, cost)

    assert weights.iloc[2].to_dict() == {"A": 0.0, "B": 1.0}
    assert turnover.iloc[2] == 2.0
    expected = (132.0 / 121.0) * (1 - 2 * cost) * (220.0 / 200.0) - 1
    assert ret.iloc[2] == pytest.approx(expected)
