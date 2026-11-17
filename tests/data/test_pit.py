"""Point-in-time correctness: no future announcements, no future constituents."""

from __future__ import annotations

import pandas as pd
import pytest

from service.adapters.pit import (
    LookaheadError,
    PitUniverse,
    TradingCalendarStore,
    assert_no_lookahead,
    audit_decision_inputs,
    pit_financials,
)


@pytest.fixture
def basic():
    return pd.DataFrame(
        {
            "ts_code": ["600000.SH", "300001.SZ", "688001.SH", "830001.BJ", "600002.SH"],
            "name": ["浦发银行", "特锐德", "华兴源创", "北交样本", "退市样本"],
            "list_date": ["20100101", "20240101", "20220101", "20210101", "20150101"],
            "delist_date": [None, None, None, None, "20230101"],
        }
    )


@pytest.fixture
def namechange():
    return pd.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "name": ["ST浦发"],
            "start_date": ["20230101"],
            "end_date": ["20231231"],
        }
    )


def test_universe_excludes_unlisted_delisted_and_bse(basic, namechange):
    u = PitUniverse(basic, namechange)
    got = set(u.as_of("20240601", min_list_days=60).ts_code)
    assert got == {"600000.SH", "300001.SZ", "688001.SH"}


def test_st_status_is_as_of_not_current(basic, namechange):
    u = PitUniverse(basic, namechange)
    assert u.is_st("600000.SH", "20230615") is True
    assert u.is_st("600000.SH", "20240115") is False
    assert "600000.SH" not in set(u.as_of("20230615", min_list_days=0).ts_code)


def test_min_list_days_uses_calendar_days(basic, namechange):
    u = PitUniverse(basic, namechange)
    # 300001.SZ listed 2024-01-01; 40 days later it is still too new.
    assert "300001.SZ" not in set(u.as_of("20240210", min_list_days=60).ts_code)
    assert "300001.SZ" in set(u.as_of("20240401", min_list_days=60).ts_code)


def test_pit_financials_never_uses_future_announcement():
    df = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH", "300001.SZ"],
            "end_date": ["20231231", "20240331", "20240331"],
            "ann_date": ["20240315", "20240420", "20240425"],
            "roe": [10.0, 11.0, 5.0],
        }
    )
    before = pit_financials(df, "20240401")
    assert len(before) == 1
    assert before.iloc[0]["end_date"] == "20231231"

    after = pit_financials(df, "20240501")
    assert set(after["ts_code"]) == {"600000.SH", "300001.SZ"}
    assert after[after.ts_code == "600000.SH"].iloc[0]["end_date"] == "20240331"


def test_pit_financials_keeps_latest_restatement():
    df = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH"],
            "end_date": ["20231231", "20231231"],
            "ann_date": ["20240315", "20240410"],
            "roe": [10.0, 9.0],
        }
    )
    got = pit_financials(df, "20240501")
    assert len(got) == 1
    assert got.iloc[0]["roe"] == 9.0


def test_lookahead_audit_raises_with_detail():
    df = pd.DataFrame({"ts_code": ["600000.SH"], "ann_date": ["20240601"]})
    with pytest.raises(LookaheadError, match="20240601"):
        assert_no_lookahead(df, "20240501", frame_name="fina")
    report = audit_decision_inputs({"fina": df}, "20240501")
    assert report["fina"].startswith("fina:")


def test_calendar_navigation():
    cal = TradingCalendarStore(("20240102", "20240103", "20240104", "20240105"))
    assert cal.prev("20240103") == "20240102"
    assert cal.next("20240104") == "20240105"
    assert cal.shift("20240102", 2) == "20240104"
    assert cal.shift("20240105", 1) is None
    assert cal.between("20240103", "20240105") == ["20240103", "20240104", "20240105"]
    assert cal.last_n("20240104", 2) == ["20240103", "20240104"]
    assert cal.is_trading_day("20240104")
    assert not cal.is_trading_day("20240106")


def test_calendar_from_frame_filters_closed_days():
    frame = pd.DataFrame(
        {"cal_date": ["20240105", "20240106", "20240108"], "is_open": [1, 0, 1]}
    )
    cal = TradingCalendarStore.from_frame(frame)
    assert cal.days == ("20240105", "20240108")


def test_calendar_range_uses_open_cal_dates_not_pretrade_or_weekdays():
    frame = pd.DataFrame([
        {"cal_date": "20240104", "is_open": 1, "pretrade_date": "20240103"},
        {"cal_date": "20240105", "is_open": 1, "pretrade_date": "20240104"},
        {"cal_date": "20240106", "is_open": 0, "pretrade_date": "20240105"},
        {"cal_date": "20240108", "is_open": 1, "pretrade_date": "20240105"},
        {"cal_date": "20240109", "is_open": 1, "pretrade_date": "20240108"},
    ])
    cal = TradingCalendarStore.from_frame(frame, "20240105", "20240108")
    assert cal.days == ("20240105", "20240108")
    assert cal.last_n("20240108", 3) == ["20240105", "20240108"]
    assert cal.next("20240108") is None, "pretrade/后续日历不能越过请求的区段"


def test_calendar_beyond_coverage_falls_back_to_weekdays():
    """A date past the stored calendar must not read as a permanent holiday."""
    from service.adapters.pit import TradingCalendarStore

    cal = TradingCalendarStore.from_frame(pd.DataFrame([
        {"cal_date": "20240102", "is_open": 1},
        {"cal_date": "20240103", "is_open": 1},
    ]))
    assert cal.is_trading_day("20240102")
    assert not cal.is_trading_day("20240107")      # inside range, weekend
    assert cal.is_trading_day("20240108")          # Monday, beyond coverage
    assert not cal.is_trading_day("20240113")      # Saturday, beyond coverage


def test_st_map_agrees_with_is_st_for_every_code_and_date(basic, namechange):
    """The bulk path must be an optimisation, not a second definition of ST.

    ``is_st`` used to be called once per code — two thousand filters over the
    name-change table, 4.7 seconds — and was replaced by one grouped pass. A
    divergence here would silently change which stocks the screen excludes, so
    the two paths are compared rather than trusted.
    """
    u = PitUniverse(basic, namechange)
    codes = list(basic["ts_code"]) + ["000000.SZ"]
    for day in ("20230101", "20230615", "20240101", "20240115", "20240601"):
        bulk = u.st_map(codes, day)
        assert set(bulk) == set(codes)
        for code in codes:
            assert bulk[code] == u.is_st(code, day), f"{code} @ {day}"


def test_st_map_handles_a_code_absent_from_every_table(basic, namechange):
    u = PitUniverse(basic, namechange)
    assert u.st_map(["999999.SH"], "20240601") == {"999999.SH": False}
