"""The pools are filled progressively; the API must say which stage it is in.

2026-10-30: the 09:41 read gave limit_up=0 / limit_down=1, the close gave 49 / 86.
Six downstream sentences quoted the first pair as market structure. These tests
pin the classification that was added to stop that.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from service.api.trading import _pool_phase

SH = ZoneInfo("Asia/Shanghai")


def at(day: str, hour: int, minute: int) -> datetime:
    return datetime(int(day[:4]), int(day[4:6]), int(day[6:]), hour, minute, tzinfo=SH)


def frame(loaded_at: str) -> pd.DataFrame:
    return pd.DataFrame({"code": ["000001"], "ingested_at": [loaded_at]})


def test_first_thirty_minutes_are_the_opening_window():
    phase, state = _pool_phase("20261030", frame("2026-10-30T09:41:00+08:00"),
                               now=at("20261030", 9, 41))
    assert phase == "opening_window"
    assert state["settled"] is False
    assert "未成形" in state["warning"]
    assert state["loaded_at"] == "2026-10-30T09:41:00+08:00"


def test_after_ten_it_is_a_live_read_not_a_final_one():
    phase, state = _pool_phase("20261030", frame("2026-10-30T12:30:00+08:00"),
                               now=at("20261030", 12, 30))
    assert phase == "intraday"
    assert state["settled"] is False
    assert "收盘值收敛" in state["warning"]


def test_after_the_close_it_is_settled():
    phase, state = _pool_phase("20261030", frame("2026-10-30T17:30:52+08:00"),
                               now=at("20261030", 19, 30))
    assert phase == "settled"
    assert state["settled"] is True
    assert state["loaded_at"] == "2026-10-30T17:30:52+08:00"
    assert "warning" not in state


def test_a_past_date_is_always_settled():
    """Asking about yesterday is asking about a frozen file, whatever the clock says."""
    phase, _ = _pool_phase("20261029", frame("2026-10-29T17:30:00+08:00"),
                           now=at("20261030", 9, 41))
    assert phase == "settled"


def test_empty_frames_do_not_break_the_classification():
    phase, state = _pool_phase("20261030", pd.DataFrame(), pd.DataFrame(),
                               now=at("20261030", 9, 35))
    assert phase == "opening_window"
    assert state["loaded_at"] is None


def test_latest_ingest_timestamp_wins_across_the_three_pools():
    phase, state = _pool_phase(
        "20261030",
        frame("2026-10-30T09:38:00+08:00"),
        frame("2026-10-30T09:41:00+08:00"),
        frame("2026-10-30T09:39:00+08:00"),
        now=at("20261030", 9, 42))
    assert state["loaded_at"] == "2026-10-30T09:41:00+08:00"


def test_the_window_between_the_last_snapshot_and_the_daily_job_is_not_settled():
    """14:45 snapshot, 17:30 daily job: the hours between are neither.

    On 2026-10-30 the 14:45 snapshot read 涨停 47 / 跌停 62 and the daily job then
    wrote 47 / 82. A payload that called the 16:00 read "settled" would have put a
    finality stamp on a twenty-minute-old count — the exact failure this module
    was added to prevent, only wearing a better label.
    """
    phase, state = _pool_phase("20261030", frame("2026-10-30T14:45:02+08:00"),
                               now=at("20261030", 16, 0))
    assert phase == "stale_close"
    assert state["settled"] is False
    assert "14:45" in state["warning"]
    assert "17:30" in state["warning"]


def test_just_before_the_close_is_still_intraday():
    phase, _ = _pool_phase("20261030", frame("2026-10-30T14:45:02+08:00"),
                           now=at("20261030", 14, 59))
    assert phase == "intraday"


def test_before_the_bell_is_not_intraday():
    """09:05 must not be described as a live session; that hour has no tape yet."""
    phase, state = _pool_phase("20261030", pd.DataFrame(),
                               now=at("20261030", 9, 5))
    assert phase == "pre_open"
    assert state["settled"] is False
    assert "上一交易日" in state["warning"]


def test_the_bell_opens_the_window():
    phase, _ = _pool_phase("20261030", pd.DataFrame(), now=at("20261030", 9, 30))
    assert phase == "opening_window"
