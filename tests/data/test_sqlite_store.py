"""SQLite registry transactions and current-coverage summaries."""

from __future__ import annotations

import sqlite3

import pytest

from service.adapters.sqlite_store import ManifestRecord, Registry


def _manifest(tmp_path, partition: str, cover_end: str, status: str = "ok") -> ManifestRecord:
    return ManifestRecord(
        dataset="daily",
        partition=partition,
        source="synthetic",
        trust_tier="T0",
        as_of=cover_end,
        ingested_at=f"{cover_end}T00:00:00+00:00",
        rows=10,
        snapshot_id=f"snap-{partition}",
        checksum=f"checksum-{partition}",
        schema_hash="schema",
        cover_start=f"{partition}0101" if partition.isdigit() else None,
        cover_end=cover_end,
        status=status,
        path=str(tmp_path / f"{partition}.parquet"),
    )


def test_quality_and_manifest_status_commit_together(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    registry.upsert_manifest(_manifest(tmp_path, "2024", "20241231"))

    registry.record_quality("daily", "2024", "warn", 9, [{"code": "synthetic_issue"}])

    assert registry.get_manifest("daily", "2024").status == "warn"
    assert registry.get_manifest("daily", "2024").note == "synthetic_issue"
    report = registry.latest_quality("daily")[0]
    assert report["status"] == "warn"
    assert report["rows"] == 9


def test_manifest_update_failure_rolls_back_quality_report(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    registry.upsert_manifest(_manifest(tmp_path, "2024", "20241231"))
    registry.record_quality("daily", "2024", "ok", 10, [])
    with registry._conn() as conn:
        conn.execute("""
            CREATE TRIGGER reject_manifest_update
            BEFORE UPDATE ON dataset_manifest
            BEGIN SELECT RAISE(ABORT, 'forced manifest failure'); END
        """)

    with pytest.raises(sqlite3.IntegrityError, match="forced manifest failure"):
        registry.record_quality("daily", "2024", "quarantine", 1, [{"code": "bad"}])

    assert registry.get_manifest("daily", "2024").status == "ok"
    report = registry.latest_quality("daily")[0]
    assert report["status"] == "ok"
    assert report["rows"] == 10


def test_summary_uses_readable_coverage_and_only_current_quarantines(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    registry.upsert_manifest(_manifest(tmp_path, "2023", "20231231", "quarantine"))
    registry.upsert_manifest(_manifest(tmp_path, "2024", "20241231"))

    row = registry.summary()[0]
    assert row["rows"] == 10
    assert row["cover_end"] == "20241231"
    assert row["last_snapshot_id"] == "snap-2024"
    assert row["quarantine_partitions"] == 0, "an old bad year must not block current data"

    registry.upsert_manifest(_manifest(tmp_path, "2025", "20251231", "quarantine"))
    row = registry.summary()[0]
    assert row["cover_end"] == "20241231"
    assert row["last_snapshot_id"] == "snap-2024"
    assert row["quarantine_partitions"] == 1, "the newest bad partition is a current defect"
