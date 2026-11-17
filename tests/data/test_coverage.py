"""The coverage report is the only tool that says whether the data is complete.

If it lies, every downstream conclusion inherits the lie: a hole reads as a quiet
day, and a quiet day reads as a fact.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "coverage.py"
    spec = importlib.util.spec_from_file_location("coverage_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rec(dataset, partition, rows=1, start=None, end=None, status="ok"):
    return SimpleNamespace(dataset=dataset, partition=partition, rows=rows,
                           cover_start=start or partition, cover_end=end or partition,
                           status=status, source="ths", trust_tier="T1")


class _Registry:
    def __init__(self, records, ingest=()):
        self._records = records
        self._ingest = list(ingest)

    def list_manifest(self):
        return self._records

    def list_ingest(self, job=None):
        return self._ingest


class _Store:
    def __init__(self, calendar):
        self._calendar = calendar

    def read(self, dataset, *a, **k):
        if dataset == "trade_cal":
            return self._calendar
        raise KeyError(dataset)


def _calendar(first="2026-09-01", last="2026-09-11"):
    days = pd.bdate_range(first, last).strftime("%Y%m%d").tolist()
    return pd.DataFrame({"cal_date": days, "is_open": [1] * len(days)})


def test_a_missing_session_inside_the_range_is_reported_as_a_gap():
    mod = _module()
    cal = _calendar()
    days = cal["cal_date"].tolist()
    records = [_rec("ths_limit_up", d) for d in days if d != days[3]]
    report = mod.collect(_Store(cal), _Registry(records))

    row = next(r for r in report if r["dataset"] == "ths_limit_up")
    assert row["sessions"] == len(days) - 1
    assert row["missing_sessions"] == [days[3]]
    assert row["missing_count"] == 1


def test_a_day_upstream_answered_empty_is_not_a_gap():
    """Zero rows is a fact about the market; absence of a partition is a fact
    about us. The ledger is what separates them."""
    mod = _module()
    cal = _calendar()
    days = cal["cal_date"].tolist()
    records = [_rec("ths_limit_down", d) for d in days if d != days[2]]
    ingest = [{"dataset": "ths_limit_down", "partition": days[2], "status": "empty"}]
    report = mod.collect(_Store(cal), _Registry(records, ingest))

    row = next(r for r in report if r["dataset"] == "ths_limit_down")
    assert row["missing_count"] == 0
    assert row["empty_sessions"] == 1


def test_on_demand_datasets_are_not_gap_checked():
    """news is fetched for a few symbols on request. A day without news means
    nobody asked, and flagging it as a hole would train the reader to ignore gaps."""
    mod = _module()
    cal = _calendar()
    days = cal["cal_date"].tolist()
    records = [_rec("news", days[0]), _rec("news", days[-1])]
    report = mod.collect(_Store(cal), _Registry(records))

    row = next(r for r in report if r["dataset"] == "news")
    assert "missing_count" not in row


def test_missing_calendar_does_not_masquerade_as_zero_gaps():
    mod = _module()
    records = [_rec("ths_limit_up", "20260901")]
    report = mod.collect(_Store(pd.DataFrame(columns=["cal_date", "is_open"])),
                         _Registry(records))

    row = report[0]
    assert row["missing_count"] is None
    assert row["missing_sessions"] is None
    assert row["expected_sessions"] is None
    assert row["coverage_scope"] == "partitions_only"


def test_sessions_count_only_samples_inside_the_calendar_checked_range():
    mod = _module()
    cal = _calendar()
    days = cal["cal_date"].tolist()
    records = [_rec("ths_limit_up", d) for d in days]
    records.append(_rec("ths_limit_up", "20270104"))
    report = mod.collect(_Store(cal), _Registry(records))

    row = report[0]
    assert row["sessions"] == len(days)
    assert row["sample_partitions"] == len(days) + 1
    assert row["expected_sessions"] == len(days)


def test_quarantined_partitions_do_not_count_as_coverage():
    mod = _module()
    cal = _calendar()
    days = cal["cal_date"].tolist()
    records = [_rec("ths_limit_up", d) for d in days] + [
        _rec("ths_limit_up", "20991231", status="quarantine")]
    report = mod.collect(_Store(cal), _Registry(records))
    row = next(r for r in report if r["dataset"] == "ths_limit_up")
    assert row["partitions"] == len(days), "the quarantined row is withheld, not counted"
    assert "20991231" not in row["missing_sessions"], "outside the covered span anyway"
