"""A seat name is not a track record. This turns one into the other.

"T王 net-bought 654万" is a fact about yesterday, not about tomorrow. The board is
published around 20:00, so the first price the information was tradeable at is the
**next open** — a close entry would be buying a price that no longer exists. That
choice is the difference between a number a strategy can use and a number that
only looks like one.

Two horizons are reported because they answer different questions: the overnight
gap (what a T+1 buyer collects by selling into the open) and the full next
session (what is left if they hold).
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from service.trading import tape


class FakeData:
    def __init__(self, frames):
        self.frames = frames

    def read(self, name, start=None, end=None, columns=None):
        frame = self.frames.get(name, pd.DataFrame()).copy()
        return frame[columns] if columns and not frame.empty else frame


def _desk(seats: list[dict], daily: list[dict], calendar: list[str] | None = None):
    frames = {
        "ths_lhb_seat": pd.DataFrame(seats),
        "daily": pd.DataFrame(daily),
    }
    if calendar:
        frames["trade_cal"] = pd.DataFrame({"cal_date": calendar, "is_open": [1] * len(calendar)})
    return SimpleNamespace(
        data=FakeData(frames),
        market=SimpleNamespace(latest_bar_date=lambda: "20261003"),
    )


def _seat(day: str, code: str, name: str = "某人", net: float = 1e7) -> dict:
    return {"trade_date": day, "ts_code": code, "name": name,
            "seat": "X", "net_value": net, "net_rate": 1.0}


def _bar(day: str, code: str, open_: float, close: float) -> dict:
    return {"trade_date": day, "ts_code": code, "open": open_, "close": close}


def test_the_gap_is_measured_from_the_close_to_the_next_open():
    desk = _desk(
        seats=[_seat("20261001", "600000.SH"), _seat("20261002", "600001.SH")],
        daily=[
            _bar("20261001", "600000.SH", 9.80, 10.00),
            _bar("20261002", "600000.SH", 10.50, 10.20),
            _bar("20261003", "600000.SH", 10.10, 10.30),
            _bar("20261002", "600001.SH", 19.50, 20.00),
            _bar("20261003", "600001.SH", 19.00, 21.00),
        ],
    )
    result = tape.seat_record(desk, "X", day="20261003")

    assert result["ok"] is True
    assert result["appearances"] == 2
    # +5% and -5%: the mean is zero and the median is zero, which is the point.
    assert result["next_open_gap"]["mean"] == pytest.approx(0.0, abs=1e-9)
    assert result["next_open_gap"]["median"] == pytest.approx(0.0, abs=1e-9)
    assert result["next_open_gap"]["win"] == 0.5
    # +2% and +5% if the session is held instead.
    assert result["next_close"]["mean"] == pytest.approx(0.035, abs=1e-9)


def test_a_suspension_gap_is_dropped_rather_than_credited_to_the_seat():
    """A name that reopens three weeks later is not a next-session return."""
    desk = _desk(
        seats=[_seat("20261001", "600000.SH")],
        daily=[
            _bar("20261001", "600000.SH", 9.80, 10.00),
            _bar("20261022", "600000.SH", 14.00, 14.50),
        ],
    )
    result = tape.seat_record(desk, "X", day="20261022")

    assert result["appearances"] == 0
    assert result["next_open_gap"] == {"n": 0}
    # An empty sample is not a verdict; the caveat says why.
    assert "不足 20 个" in result["caveats"][-1]


def test_a_thin_sample_is_flagged_not_averaged_into_confidence():
    desk = _desk(
        seats=[_seat("20261001", "600000.SH")],
        daily=[_bar("20261001", "600000.SH", 9.80, 10.00),
               _bar("20261002", "600000.SH", 10.50, 10.20)],
    )
    result = tape.seat_record(desk, "X", day="20261002")

    assert result["next_open_gap"]["n"] == 1
    assert result["usable"] is False


def test_an_unknown_seat_returns_zero_appearances_without_guessing():
    desk = _desk(
        seats=[_seat("20261001", "600000.SH")],
        daily=[_bar("20261001", "600000.SH", 9.80, 10.00),
               _bar("20261002", "600000.SH", 10.50, 10.20)],
    )
    result = tape.seat_record(desk, "查无此人", day="20261002")

    assert result["ok"] is True
    assert result["appearances"] == 0
    assert "note" in result


def test_a_blank_seat_is_an_error_not_a_wildcard():
    desk = _desk(seats=[], daily=[])
    result = tape.seat_record(desk, "   ", day="20261002")

    assert result["ok"] is False
    assert "seat" in result["error"]


def test_the_latest_appearance_carries_its_own_outcome():
    desk = _desk(
        seats=[_seat("20261001", "600000.SH", "旧票"),
               _seat("20261002", "600001.SH", "新票", net=2e7)],
        daily=[
            _bar("20261001", "600000.SH", 9.80, 10.00),
            _bar("20261002", "600000.SH", 10.50, 10.20),
            _bar("20261002", "600001.SH", 19.50, 20.00),
            _bar("20261003", "600001.SH", 19.00, 21.00),
        ],
    )
    latest = tape.seat_record(desk, "X", day="20261003")["latest"]

    assert latest["day"] == "20261002"
    assert latest["name"] == "新票"
    assert latest["net_value_cny"] == 2e7
    assert latest["next_open_gap"] == pytest.approx(-0.05, abs=1e-6)


def test_a_two_day_halt_is_not_a_next_open():
    """停两天再赶上周末只跨 4 个自然日，旧的 `span <= 7` 会放它过去。

    那样算出来的"次日溢价"其实是 T+2 的价，而样本里没人能在这个价位上成交。
    有交易日历时，next_date 必须恰好是下一个交易日。
    """
    desk = _desk(
        seats=[_seat("20261001", "600000.SH")],
        daily=[
            _bar("20261001", "600000.SH", 9.80, 10.00),
            _bar("20261005", "600000.SH", 10.80, 10.60),   # 中间停了两天
        ],
        calendar=["20261001", "20261002", "20261003", "20261005"],
    )

    out = tape.seat_record(desk, "X", day="20261003")

    assert out["gap_basis"] == "next_session"
    assert out["next_open_gap"]["n"] == 0, "跨停牌的那一笔不算次日开盘"


def test_without_a_calendar_the_old_window_is_used_and_says_so():
    desk = _desk(
        seats=[_seat("20261001", "600000.SH")],
        daily=[
            _bar("20261001", "600000.SH", 9.80, 10.00),
            _bar("20261002", "600000.SH", 10.50, 10.20),
        ],
    )

    out = tape.seat_record(desk, "X", day="20261003")

    assert out["gap_basis"] == "calendar_days<=7"
    assert out["next_open_gap"]["n"] == 1
