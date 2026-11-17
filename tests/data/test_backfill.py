"""Backfill must be resumable, idempotent and honest about missing data."""

from __future__ import annotations

import pandas as pd

from scripts.backfill import WorkUnit, run_unit
from tests.data.helpers import make_daily


class FakeTushare:
    def __init__(self):
        self.calls: list[str] = []

    def fetch_daily(self, trade_date: str):
        self.calls.append(trade_date)
        return make_daily([trade_date])


DAYS = ["20240102", "20240103", "20240104", "20240105", "20240108"]
UNIT = WorkUnit("daily", "2024", DAYS, "20240102", "20240108")


def test_first_run_writes_all_rows_and_marks_done(store, registry):
    ts = FakeTushare()
    res = run_unit(UNIT, store, registry, ts, None, flush_every=2)

    assert res["rows"] == 5
    assert ts.calls == DAYS
    assert len(store.read("daily")) == 5
    state = registry.ingest_state("backfill", "daily", "2024")
    assert state["status"] == "done"
    assert state["cursor"] == "20240108"


def test_finished_unit_is_skipped(store, registry):
    ts = FakeTushare()
    run_unit(UNIT, store, registry, ts, None)
    before = len(ts.calls)

    res = run_unit(UNIT, store, registry, ts, None)
    assert res["status"] == "skipped"
    assert len(ts.calls) == before


def test_resume_continues_from_cursor(store, registry):
    # Simulate a crash after the first three days: partition + cursor exist.
    store.write("daily", "2024", make_daily(DAYS[:3]), as_of="20240104")
    registry.mark_ingest("backfill", "daily", "2024", "running", cursor="20240104", rows=3)

    ts = FakeTushare()
    res = run_unit(UNIT, store, registry, ts, None)

    assert ts.calls == ["20240105", "20240108"]
    assert res["rows"] == 5
    assert len(store.read("daily")) == 5


def test_force_refetch_does_not_duplicate(store, registry):
    ts = FakeTushare()
    run_unit(UNIT, store, registry, ts, None)
    res = run_unit(UNIT, store, registry, ts, None, force=True)

    assert res["rows"] == 5
    assert len(ts.calls) == 2 * len(DAYS)
    assert len(store.read("daily")) == 5


def test_quality_verdict_is_recorded(store, registry):
    ts = FakeTushare()
    run_unit(UNIT, store, registry, ts, None)
    quality = registry.latest_quality("daily")
    assert quality
    assert quality[0]["status"] in {"ok", "warn"}


def test_done_but_narrower_range_is_extended(store, registry):
    """A finished run over 2 days must not block a later 5-day request."""
    ts = FakeTushare()
    narrow = WorkUnit("daily", "2024", DAYS[:2], "20240102", "20240103")
    run_unit(narrow, store, registry, ts, None)

    res = run_unit(UNIT, store, registry, ts, None)
    assert res["status"] != "skipped"
    assert len(store.read("daily")) == 5
    assert set(ts.calls) == set(DAYS)


def test_range_extension_backwards_is_not_skipped(store, registry):
    ts = FakeTushare()
    late = WorkUnit("daily", "2024", DAYS[2:], "20240104", "20240108")
    run_unit(late, store, registry, ts, None)

    res = run_unit(UNIT, store, registry, ts, None)
    assert res["status"] != "skipped"
    assert len(store.read("daily")) == 5
    assert set(ts.calls) == set(DAYS)


# --------------------------------------------------- code-mode coverage (P46)
class FakeFina:
    """Stand-in for `fina_indicator`: one row per symbol, no network."""

    def __init__(self):
        self.calls: list[str] = []

    def fetch_fina_indicator(self, ts_code, start, end):
        import pandas as pd

        self.calls.append(ts_code)
        return pd.DataFrame([{"ts_code": ts_code, "ann_date": end,
                              "end_date": end, "roe": 1.0}])


def test_a_partial_code_run_does_not_mark_the_whole_partition_done(store, registry):
    """`cover_end` describes the data's newest date, not how many symbols landed.

    On 2026-10-28 a three-symbol trial of `anns` wrote rows dated today, which
    satisfied `cover_end >= end`, and the ledger recorded the partition done —
    so `--datasets anns` then skipped all 5,919 codes and reported success.
    """
    first = WorkUnit("fina_indicator", "all", ["000001.SZ", "600519.SH"], "20240101", "20240105")
    ts = FakeFina()
    run_unit(first, store, registry, ts, None, flush_every=10)
    assert sorted(ts.calls) == ["000001.SZ", "600519.SH"]

    # The wider run must fetch, not trust the "done" from the narrow one.
    wider = WorkUnit("fina_indicator", "all",
                     ["000001.SZ", "600519.SH", "002966.SZ"], "20240101", "20240105")
    ts2 = FakeFina()
    res = run_unit(wider, store, registry, ts2, None, flush_every=10)
    assert res["status"] != "skipped", "被上一次的小范围运行骗过去了"
    assert "002966.SZ" in ts2.calls


def test_a_completed_code_run_is_still_skipped(store, registry):
    """The fix must not turn every rerun into a refetch of 5,919 symbols."""
    codes = ["000001.SZ", "600519.SH"]
    unit = WorkUnit("fina_indicator", "all", codes, "20240101", "20240105")
    ts = FakeFina()
    run_unit(unit, store, registry, ts, None, flush_every=10)
    before = len(ts.calls)

    res = run_unit(unit, store, registry, ts, None, flush_every=10)
    assert res["status"] == "skipped"
    assert len(ts.calls) == before


class FakeBasic:
    """Reference-table fetch: one call, one row, no date keys."""

    def __init__(self):
        self.calls = 0

    def fetch_stock_basic(self):
        self.calls += 1
        return pd.DataFrame({
            "ts_code": ["600001.SH"],
            "name": ["合成股票"],
            "list_date": ["20200101"],
        })


BASIC = WorkUnit("stock_basic", "all", [], "20240102", "20240108")


def test_a_reference_table_is_fetched_again_on_a_later_session(store, registry):
    """A finished "single" table used to be final, and that is a real gap.

    `stock_basic` / `fund_basic` / `namechange` have no date boundary to compare
    against, so the covered check could only ask "did this unit finish" — and it
    had. Measured 2026-10-31: `stock_basic` was still the 2026-10-25 pull, so a
    name listed, renamed to ST or added to an index in between was invisible to
    `screen` and `universe` while every bar around it was current.
    """
    ts = FakeBasic()
    first = run_unit(BASIC, store, registry, ts, None, last_session="20240108")

    assert first["status"] != "skipped"
    assert ts.calls == 1


def test_a_reference_table_pulled_this_session_is_skipped(store, registry):
    """The nightly job must not re-pull 36k reference rows for nothing."""
    ts = FakeBasic()
    run_unit(BASIC, store, registry, ts, None, last_session="20240108")
    before = ts.calls

    again = run_unit(BASIC, store, registry, ts, None, last_session="20240108")
    assert again["status"] == "skipped"
    assert ts.calls == before


def test_a_later_session_than_the_pull_refetches(store, registry):
    """`last_session` after the pull date means the file predates the window."""
    ts = FakeBasic()
    run_unit(BASIC, store, registry, ts, None, last_session="20240108")

    later = run_unit(BASIC, store, registry, ts, None, last_session="20991231")
    assert later["status"] != "skipped"
    assert ts.calls == 2


def test_without_last_session_a_done_unit_stays_done(store, registry):
    """The primitive keeps the old contract; only the CLI passes a session.

    Tests that call `run_unit` directly are asserting the unit mechanics, not
    the scheduling policy, and quietly turning every one of them into a refetch
    would hide a real regression behind a green suite.
    """
    ts = FakeBasic()
    run_unit(BASIC, store, registry, ts, None)
    before = ts.calls

    res = run_unit(BASIC, store, registry, ts, None)
    assert res["status"] == "skipped"
    assert ts.calls == before
