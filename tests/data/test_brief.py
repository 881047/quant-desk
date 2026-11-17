"""The data-trust brief is the contract between the data layer and the LLM."""

from __future__ import annotations

from datetime import datetime

import pytest

import service.adapters.brief as brief_mod
from service.adapters.brief import build_brief
from service.adapters.pit import TradingCalendarStore

CAL = TradingCalendarStore(("20240102", "20240103", "20240104", "20240105", "20240108"))


def _summary(**overrides):
    base = {
        "daily": ("20240108", 0),
        "adj_factor": ("20240108", 0),
        "stk_limit": ("20240108", 0),
        "limit_up_pool": ("20240108", 0),
        "broken_board_pool": ("20240108", 0),
        "limit_down_pool": ("20240108", 0),
        # The 同花顺 tape joined the contract in P26: the pools and the panels
        # come from different vendors and fail independently, so a sleeve that
        # can trade on the pools alone is not "usable".
        "ths_limit_up": ("20240108", 0),
        "ths_limit_down": ("20240108", 0),
        "ths_limit_break": ("20240108", 0),
        "ths_ladder": ("20240108", 0),
        "ths_limit_stat": ("20240108", 0),
        "ths_lhb": ("20240108", 0),
        "ths_lhb_seat": ("20240108", 0),
        "ths_auction": ("20240108", 0),
        "ths_anomaly": ("20240108", 0),
    }
    rows = []
    for name, (cover, quarantined) in base.items():
        row = {"dataset": name, "cover_end": cover, "quarantine_partitions": quarantined,
               "rows": 100, "last_snapshot_id": f"snap-{name}"}
        row.update(overrides.get(name, {}))
        rows.append(row)
    return rows


def _fix_now(monkeypatch, hour: int, minute: int = 0):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2024, 1, 8, hour, minute)
            return value.replace(tzinfo=tz) if tz is not None else value

    monkeypatch.setattr(brief_mod, "datetime", FixedDateTime)


def test_all_fresh_is_usable():
    brief = build_brief(summary=_summary(), calendar=CAL, today="20240108",
                        rt_age_seconds=5, sentiment={"tone": "修复"}, intraday=True)
    assert brief["core"]["usable"] is True
    assert brief["aggressive"]["usable"] is True
    assert brief["quotes"]["fresh"] is True
    # Mid-session the only note is the state of the session itself.
    assert [n for n in brief["notes"] if "session in progress" not in n] == []
    assert brief["core"]["datasets"]["daily"]["snapshot_id"] == "snap-daily"


def test_missing_core_dataset_blocks_core():
    rows = [r for r in _summary() if r["dataset"] != "stk_limit"]
    brief = build_brief(summary=rows, calendar=CAL, today="20240108",
                        rt_age_seconds=5, sentiment=None)
    assert brief["core"]["usable"] is False
    assert "stk_limit" in brief["core"]["missing"]


def test_stale_quotes_block_aggressive_only():
    """A 10-minute-old quote is dangerous when you are about to hit the tape."""
    brief = build_brief(summary=_summary(), calendar=CAL, today="20240108",
                        rt_age_seconds=600, sentiment=None, intraday=True)
    assert brief["core"]["usable"] is True
    assert brief["aggressive"]["usable"] is False
    assert "rt_quote" in brief["aggressive"]["missing"]


def test_lag_is_counted_in_trading_days():
    rows = _summary(daily={"cover_end": "20240104"})
    brief = build_brief(summary=rows, calendar=CAL, today="20240108",
                        rt_age_seconds=1, sentiment=None)
    assert brief["core"]["datasets"]["daily"]["lag_trading_days"] == 2
    assert "daily" in brief["core"]["missing"]


def test_quarantine_blocks_aggressive():
    rows = _summary(limit_up_pool={"quarantine_partitions": 1})
    brief = build_brief(summary=rows, calendar=CAL, today="20240108",
                        rt_age_seconds=1, sentiment=None)
    assert brief["aggressive"]["usable"] is False
    assert "limit_up_pool" in brief["aggressive"]["missing"]


def test_non_trading_day_points_at_last_session():
    brief = build_brief(summary=_summary(), calendar=CAL, today="20240106",
                        rt_age_seconds=None, sentiment=None)
    assert brief["is_trading_day"] is False
    assert brief["last_trading_day"] == "20240105"
    # Nobody is entering a hot-money position on a Saturday: the sleeve's blocker
    # is `as_of = prev(date)` coverage, not the absence of a live quote.
    assert brief["aggressive"]["usable"] is True
    assert any("not a trading day" in n for n in brief["notes"])


def test_stale_tape_blocks_aggressive_even_when_pools_are_fresh():
    """The 东财 pools and the 同花顺 panels are separate failure domains."""
    rows = _summary(ths_limit_stat={"cover_end": "20240104"})
    brief = build_brief(summary=rows, calendar=CAL, today="20240108",
                        rt_age_seconds=1, sentiment=None)
    assert brief["aggressive"]["usable"] is False
    assert "ths_limit_stat" in brief["aggressive"]["missing"]
    assert brief["core"]["usable"] is True


def test_aux_panels_never_block():
    """龙虎榜 publishes at 20:00 and must not gate a decision made at 15:30."""
    rows = _summary(ths_lhb={"cover_end": None}, ths_auction={"cover_end": None})
    brief = build_brief(summary=rows, calendar=CAL, today="20240108",
                        rt_age_seconds=1, sentiment=None)
    assert brief["aggressive"]["usable"] is True
    assert "ths_lhb" in brief["aux"]["datasets"]
    assert brief["aux"]["datasets"]["ths_lhb"]["cover_end"] is None


def test_optional_datasets_are_reported_but_never_block():
    """Reference and research inputs rot silently because no gate watches them.

    On 2026-10-26 `cb_daily` and `news` both sat three sessions behind while every
    decision-critical dataset was current, and nothing in the brief or on the page
    said so. Reporting them is not the same as blocking on them: a stale bond bar
    must not stop a stock decision.
    """
    rows = _summary() + [
        {"dataset": "cb_daily", "cover_end": "20240103", "quarantine_partitions": 0,
         "rows": 100, "last_snapshot_id": "s1"},
        {"dataset": "news", "cover_end": "20240108", "quarantine_partitions": 0,
         "rows": 40, "last_snapshot_id": "s2"},
        # A reference table: its `cover_end` is the newest entity's own date, so
        # lagging it measures nothing. `stock_basic` said "3 sessions behind" every
        # evening until this split existed.
        {"dataset": "stock_basic", "cover_end": "20240103", "quarantine_partitions": 0,
         "rows": 5000, "last_snapshot_id": "s3"},
        # Opt-in: not fetched on purpose, so its lag is not a defect.
        {"dataset": "moneyflow", "cover_end": "20231201", "quarantine_partitions": 0,
         "rows": 900, "last_snapshot_id": "s4"},
    ]
    brief = build_brief(summary=rows, calendar=CAL, today="20240108",
                        rt_age_seconds=1, sentiment=None)

    assert brief["core"]["usable"] is True
    assert brief["aggressive"]["usable"] is True
    assert "cb_daily" in brief["optional"]["datasets"]
    assert brief["optional"]["stale"] == {"cb_daily": 3}, \
        "three sessions behind when it ends on 0103 and the desk is at 0108"
    assert "news" not in brief["optional"]["stale"]
    assert "stock_basic" not in brief["optional"]["stale"], \
        "a reference table cannot be 'behind'; its date column is per entity"
    assert "stock_basic" not in brief["optional"]["tracked"]
    assert "cb_daily" in brief["optional"]["tracked"]
    assert "moneyflow" not in brief["optional"]["tracked"], \
        "an opt-in dataset is not fetched on purpose; its lag is not a defect"
    assert "moneyflow" in brief["optional"]["datasets"]
    assert "cb_daily" not in brief["notes"], "optional data must not become a caveat"


def test_a_quality_warning_is_visible_in_the_brief():
    """The report wrote the warning; the brief has to repeat it.

    `adj_factor` 2026 carried 15 flagged rows and the brief still said usable,
    because nothing read `quality_report`. A report nobody reads is a log file.
    """
    summary = _summary(adj_factor={"warn_partitions": 1})
    out = build_brief(
        summary=summary, calendar=CAL, today="20240108",
        rt_age_seconds=1.0, sentiment={},
    )
    assert out["quality"]["warned"].get("adj_factor") == 1
    assert any("flagged" in note for note in out["notes"])


def test_a_clean_partition_set_reports_no_warnings():
    out = build_brief(
        summary=_summary(), calendar=CAL, today="20240108",
        rt_age_seconds=1.0, sentiment={},
    )
    assert out["quality"]["warned"] == {}
    assert not any("flagged" in note for note in out["notes"])


def test_intraday_asks_for_yesterday_not_today():
    """During a session today's bar cannot exist; demanding it blocks the whole day.

    `Guard.data_checks` already used `as_of = prev(date)` for exactly this reason.
    Two answers to one question is how a desk stops trading for no reason.
    """
    summary = _summary(daily={"cover_end": "20240105"}, stk_limit={"cover_end": "20240105"})
    out = build_brief(
        summary=summary, calendar=CAL, today="20240108",
        rt_age_seconds=1.0, sentiment={}, intraday=True,
    )
    assert out["session"]["intraday"] is True
    assert out["session"]["priced_through"] == "20240105"
    assert out["core"]["usable"] is True, out["core"]["missing"]
    assert any("session in progress" in note for note in out["notes"])


def test_after_the_close_the_same_gap_is_a_real_problem():
    summary = _summary(daily={"cover_end": "20240105"}, stk_limit={"cover_end": "20240105"})
    out = build_brief(
        summary=summary, calendar=CAL, today="20240108",
        rt_age_seconds=1.0, sentiment={}, intraday=False,
    )
    assert out["core"]["usable"] is False
    assert "daily" in out["core"]["missing"]


def test_a_live_quote_is_required_in_the_session_not_in_the_evening():
    """No tape at 19:30 is not the same as a stale tape at 11:00.

    `aggressive_ok` was false every evening because `rt_quote` was missing after
    the close. The prompt reads that flag as a ban on the sleeve, so the hot-money
    persona could only ever plan trades between 09:30 and 15:00 — and the order it
    queues for tomorrow's open is sized off tonight's bars, not a live quote.
    """
    summary = _summary(daily={"cover_end": "20240108"}, stk_limit={"cover_end": "20240108"})

    evening = build_brief(summary=summary, calendar=CAL, today="20240108",
                          rt_age_seconds=None, sentiment={}, intraday=False)
    assert evening["aggressive"]["usable"] is True
    assert "rt_quote" not in evening["aggressive"]["missing"]
    assert any("live quotes are not required" in note for note in evening["notes"])

    intraday = build_brief(summary=summary, calendar=CAL, today="20240108",
                           rt_age_seconds=None, sentiment={}, intraday=True)
    assert intraday["aggressive"]["usable"] is False
    assert "rt_quote" in intraday["aggressive"]["missing"]


def test_a_stale_quote_still_blocks_the_sleeve_mid_session():
    summary = _summary(daily={"cover_end": "20240108"}, stk_limit={"cover_end": "20240108"})
    out = build_brief(summary=summary, calendar=CAL, today="20240108",
                      rt_age_seconds=900.0, sentiment={}, intraday=True, stale_after=120)
    assert out["aggressive"]["usable"] is False
    assert "rt_quote" in out["aggressive"]["missing"]


@pytest.mark.parametrize(("hour", "pause"), [(12, "lunch"), (15, "closed")])
def test_no_new_quote_is_expected_during_lunch_or_after_close(monkeypatch, hour, pause):
    _fix_now(monkeypatch, hour, 30 if hour == 15 else 0)

    out = build_brief(summary=_summary(), calendar=CAL, today="20240108",
                      rt_age_seconds=5400.0, sentiment={}, intraday=True,
                      stale_after=120)

    assert out["quotes"]["fresh"] is False, "最后一帧仍然旧，只是此刻不该有新帧"
    assert out["quotes"]["required"] is False
    assert out["quotes"]["pause"] == pause
    assert "rt_quote" not in out["aggressive"]["missing"]
    assert out["aggressive"]["usable"] is True


def test_an_old_quote_still_blocks_when_a_new_frame_is_expected(monkeypatch):
    _fix_now(monkeypatch, 10)

    out = build_brief(summary=_summary(), calendar=CAL, today="20240108",
                      rt_age_seconds=900.0, sentiment={}, intraday=True,
                      stale_after=120)

    assert out["quotes"]["required"] is True
    assert out["quotes"]["pause"] is None
    assert "rt_quote" in out["aggressive"]["missing"]
    assert out["aggressive"]["usable"] is False
