"""Event independence must be measured on sessions, not sparse event dates."""

from __future__ import annotations

import pandas as pd
import pytest

from service.research.independence import dedup_events, independence


def test_month_apart_events_are_not_one_session_apart():
    events = pd.DataFrame({
        "ts_code": ["600000.SH", "600000.SH"],
        "trade_date": ["2026-01-01", "2026-02-01"],
    })
    sessions = pd.DataFrame({"cal_date": [
        "20260105", "20260106", "20260112", "20260113", "20260119",
        "20260120", "20260126", "20260127", "20260202", "20260203",
    ]})
    kept = dedup_events(events, horizon=5, calendar=sessions)
    assert len(kept) == 2, "Jan 1 and Feb 1 must not become adjacent event indices"
    out = independence(events, horizon=5, calendar=sessions)
    assert out["n_obs"] == 2
    assert out["n_independent"] == 2
    assert out["calendar_basis"] == "supplied_sessions"


def test_trade_cal_store_is_injected_without_importing_the_trading_layer():
    events = pd.DataFrame({
        "ts_code": ["600000.SH", "600000.SH"],
        "trade_date": ["2026-01-01", "2026-02-01"],
    })
    sessions = pd.DataFrame({"cal_date": [
        "20260105", "20260106", "20260112", "20260113", "20260119",
        "20260120", "20260126", "20260127", "20260202", "20260203",
    ]})

    class _Store:
        def read(self, dataset, **kwargs):
            assert dataset == "trade_cal"
            return sessions

    out = independence(events, horizon=5, calendar=_Store())
    assert out["n_independent"] == 2
    assert out["calendar_basis"] == "trade_cal"
    assert out["n_calendar_sessions"] == 10


def test_same_code_events_inside_five_sessions_still_collapse():
    events = pd.DataFrame({
        "ts_code": ["600000.SH"] * 2,
        "trade_date": ["20260105", "20260107"],
    })
    sessions = pd.bdate_range("2026-01-05", periods=10).strftime("%Y%m%d")
    kept = dedup_events(events, horizon=5, calendar=sessions)
    assert kept["trade_date"].tolist() == ["20260105"]


def test_no_calendar_fails_loudly_instead_of_inventing_sessions():
    events = pd.DataFrame({"ts_code": ["X"], "trade_date": ["20260101"]})
    for missing in (None, []):
        with pytest.raises(ValueError, match="trading calendar"):
            dedup_events(events, horizon=5, calendar=missing)
