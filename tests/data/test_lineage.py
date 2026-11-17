"""Decision lineage: every decision must name the exact data versions it used."""

from __future__ import annotations

from service.adapters.lineage import collect
from tests.data.helpers import make_daily


def test_lineage_binds_snapshots_and_reports_gaps(store):
    rec = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert rec is not None

    fp = collect(store, "20240102")
    assert fp.snapshots["daily"] == rec.snapshot_id
    assert fp.complete is False
    assert "adj_factor" in fp.missing
    payload = fp.as_dict()
    assert payload["as_of"] == "20240102"
    assert payload["complete"] is False


def test_lineage_does_not_mark_a_future_snapshot_as_historical_evidence(store):
    future = store.write("daily", "2026", make_daily(["20261113"]), as_of="20261113")
    assert future is not None

    fp = collect(store, "20200101")

    assert fp.snapshots["daily"] is None
    assert "daily" in fp.missing
    assert fp.complete is False


def test_lineage_can_include_the_consumers_actual_evidence_datasets(store):
    fp = collect(store, "20240102", datasets=["daily_basic", "daily_basic"])

    assert "daily_basic" in fp.snapshots
    assert fp.snapshots["daily_basic"] is None
    assert "daily_basic" in fp.missing
    assert len(fp.snapshots) == len(set(fp.snapshots))


def test_lineage_is_empty_when_nothing_is_stored(store):
    fp = collect(store, "20240102")
    assert fp.snapshots["daily"] is None
    assert fp.complete is False
    assert len(fp.missing) == len(fp.snapshots)
