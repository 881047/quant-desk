"""Store invariants: idempotent snapshot ids, merge semantics, quarantine gate."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from service.adapters import parquet_store
from service.adapters.parquet_store import DatasetStore, SchemaError, StoreCorruptionError
from tests.data.helpers import make_adj, make_daily


def test_write_and_read_roundtrip(store):
    days = ["20240102", "20240103", "20240104"]
    df = make_daily(days)
    rec = store.write("daily", "2024", df, as_of="20240104")

    assert rec is not None
    assert rec.rows == 3
    assert rec.cover_start == "20240102"
    assert rec.cover_end == "20240104"
    assert rec.source == "tushare"
    assert rec.trust_tier == "T0"

    back = store.read("daily")
    assert len(back) == 3
    assert back["trade_date"].tolist() == days
    assert float(back["close"].iloc[-1]) == pytest.approx(10.25)


def test_snapshot_id_is_content_addressed(store):
    df = make_daily(["20240102", "20240103"])
    first = store.write("daily", "2024", df, as_of="20240103")
    second = store.write("daily", "2024", df.copy(), as_of="20240103")

    assert first is not None and second is not None
    assert first.checksum == second.checksum
    assert first.snapshot_id == second.snapshot_id

    other = make_daily(["20240102", "20240103"])
    other.loc[other.index[-1], "close"] = 99.0
    third = store.write("daily", "2024", other, as_of="20240103")
    assert third is not None
    assert third.snapshot_id != first.snapshot_id


def test_merge_dedupes_on_natural_key(store):
    store.write("daily", "2024", make_daily(["20240102", "20240103"]), as_of="20240103")
    update = make_daily(["20240103", "20240104"])
    update.loc[update["trade_date"] == "20240103", "close"] = 88.0

    rec = store.write("daily", "2024", update, as_of="20240104", mode="merge")
    df = store.read("daily")

    assert rec is not None
    assert len(df) == 3
    row = df[df["trade_date"] == "20240103"].iloc[0]
    assert float(row["close"]) == pytest.approx(88.0)


def test_schema_contract_is_enforced(store):
    bad = pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20240102"], "close": [10.0]})
    with pytest.raises(SchemaError):
        store.write("daily", "2024", bad, as_of="20240102")


def test_quarantine_removes_partition_from_reads(store, registry):
    store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert len(store.read("daily")) == 1

    registry.set_manifest_status("daily", "2024", "quarantine", note="test")
    assert store.read("daily").empty
    warned = store.read("daily", status=("ok", "warn", "quarantine"))
    assert len(warned) == 1


def test_an_ordinary_write_does_not_lift_a_quarantine(store, registry):
    """A quarantine is a verdict; a writer must not overturn it by default.

    `write` defaulted to status="ok", so the next backfill or daily run — neither
    of which had an opinion about quality — put the partition straight back into
    `read()`. Clearing the verdict has to be a decision, so lifting it now
    requires saying `status="ok"` explicitly.
    """
    store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    registry.set_manifest_status("daily", "2024", "quarantine", note="bad source")

    store.write("daily", "2024", make_daily(["20240103"]), as_of="20240103",
                mode="merge")
    assert store.read("daily").empty, "still quarantined"
    assert registry.get_manifest("daily", "2024").status == "quarantine"

    store.write("daily", "2024", make_daily(["20240104"]), as_of="20240104",
                mode="merge", status="ok")
    assert len(store.read("daily")) == 3, "an explicit ok lifts it"


def test_dates_are_normalised_to_yyyymmdd(store):
    df = make_daily(["20240102"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    store.write("daily", "2024", df, as_of="20240102")

    back = store.read("daily")
    assert back["trade_date"].iloc[0] == "20240102"


def test_adj_factor_partition_is_stable(store):
    days = ["20240102", "20240103"]
    store.write("adj_factor", "2024", make_adj(days), as_of="20240103")
    back = store.read("adj_factor")
    assert len(back) == 2
    assert "adj_factor" in back.columns


def test_fingerprint_lists_snapshot_ids(store):
    rec = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    fp = store.fingerprint(["daily"])
    assert rec is not None
    assert fp["daily"][0]["snapshot_id"] == rec.snapshot_id


def test_snapshot_for_picks_the_partition_covering_the_date(store):
    r2023 = store.write("daily", "2023", make_daily(["20230601"]), as_of="20230601")
    r2024 = store.write("daily", "2024", make_daily(["20240603"]), as_of="20240603")
    assert r2023 and r2024

    assert store.snapshot_for("daily", "20231101") == r2023.snapshot_id
    assert store.snapshot_for("daily", "20240603") == r2024.snapshot_id
    assert store.snapshot_for("daily", "20250101") == r2024.snapshot_id
    assert store.snapshot_for("adj_factor", "20240603") is None


def test_empty_frame_is_not_persisted(store):
    rec = store.write("daily", "2024", pd.DataFrame(), as_of="20240102")
    assert rec is None
    assert store.read("daily").empty


def test_drop_partition_removes_files_and_lineage(store, registry):
    store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert len(store.read("daily")) == 1

    store.drop_partition("daily", "2024")

    assert store.read("daily").empty
    assert registry.get_manifest("daily", "2024") is None
    assert not store.path_for("daily", "2024").exists()


def test_read_projects_columns_without_the_full_primary_key(store):
    """A column slice that drops part of the pk must not raise KeyError."""
    store.write("daily", "2024", make_daily(["20240102", "20240103"]), as_of="20240103")
    part = store.read("daily", "20240101", "20240131", columns=["ts_code", "close"])
    assert list(part.columns) == ["ts_code", "close"]
    assert len(part) == 2


def test_projection_still_respects_the_date_range(store):
    """Projecting away the date column must not skip the range filter."""
    store.write("daily", "2024", make_daily(["20240102", "20240103", "20240104"]),
                as_of="20240104")
    one_day = store.read("daily", "20240103", "20240103", columns=["ts_code", "close"])
    assert len(one_day) == 1
    assert list(one_day.columns) == ["ts_code", "close"]


def test_read_skips_partitions_outside_the_window(store):
    """Asking for one week must not decode twenty years of parquet.

    ``read`` used to open every partition and filter afterwards. ``/universe``
    needed thirty sessions of ``daily`` and read 16 million rows to keep the tail,
    which is why a fifty-row browser took 9.7 seconds.
    """
    store.write("daily", "2023", make_daily(["20230103", "20231229"]), as_of="20231229")
    store.write("daily", "2024", make_daily(["20240102", "20241231"]), as_of="20241231")

    one_day = store.read("daily", "20240102", "20240102")
    assert set(one_day["trade_date"]) == {"20240102"}

    everything = store.read("daily")
    assert len(everything) == 4


def test_read_pushes_path_date_and_column_pruning_into_pyarrow(store, monkeypatch):
    store.write("daily", "2023", make_daily(["20230103"]), as_of="20230103")
    rec = store.write("daily", "2024", make_daily(["20240102", "20240103"]), as_of="20240103")
    assert rec is not None
    calls = []
    real = pd.read_parquet

    def capture(path, **kwargs):
        calls.append({"path": str(path), **kwargs})
        return real(path, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", capture)
    out = store.read("daily", "20240102", "20240102", columns=["close"])

    assert out["close"].tolist() == pytest.approx([10.05])
    assert len(calls) == 1, "2023 分区必须在 pyarrow 打开前被路径裁剪"
    assert calls[0]["path"] == rec.path
    assert calls[0]["columns"] == ["close", "trade_date", "ts_code"]
    assert calls[0]["filters"] == [
        ("trade_date", ">=", "20240102"), ("trade_date", "<=", "20240102")
    ]


def test_custom_filters_are_pushed_and_their_columns_are_projected_away(store, monkeypatch):
    frame = make_daily(["20240102", "20240103"])
    store.write("daily", "2024", frame, as_of="20240103")
    calls = []
    real = pd.read_parquet

    def capture(path, **kwargs):
        calls.append(kwargs)
        return real(path, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", capture)
    out = store.read(
        "daily", columns=["close"], filters=[("amount", ">", 1_000_000.0)]
    )

    assert out["close"].tolist() == pytest.approx([10.15])
    assert list(out.columns) == ["close"]
    assert calls[0]["columns"] == ["amount", "close", "trade_date", "ts_code"]
    assert calls[0]["filters"] == [("amount", ">", 1_000_000.0)]


def test_date_bounds_are_anded_into_each_disjunctive_filter_group(store):
    frame = pd.concat([
        make_daily(["20240102"], code="600000.SH"),
        make_daily(["20240103"], code="000001.SZ"),
    ], ignore_index=True)
    store.write("daily", "2024", frame, as_of="20240103")

    out = store.read(
        "daily", "20240102", "20240102", columns=["ts_code"],
        filters=[[("ts_code", "==", "600000.SH")], [("ts_code", "==", "000001.SZ")]],
    )
    assert out["ts_code"].tolist() == ["600000.SH"]


def test_iso_date_bounds_match_the_normalized_storage_dates(store):
    store.write("daily", "2024", make_daily(["20240102", "20240103"]), as_of="20240103")

    out = store.read("daily", "2024-01-02", "2024-01-02")
    assert out["trade_date"].tolist() == ["20240102"]


def test_bool_columns_follow_the_int8_storage_contract(store):
    frame = pd.DataFrame({
        "cal_date": ["20240102", "20240103"], "is_open": [True, False],
    })
    store.write("trade_cal", "all", frame, as_of="20240103")

    out = store.read("trade_cal")
    assert str(out["is_open"].dtype) == "int8"
    assert out["is_open"].tolist() == [1, 0]


def test_financial_restatements_survive_merge_and_keep_pit_history(store):
    first = pd.DataFrame({
        "ts_code": ["600000.SH"], "end_date": ["20231231"],
        "ann_date": ["20240315"], "roe": [10.0],
    })
    restated = first.copy()
    restated["ann_date"] = "20240420"
    restated["roe"] = 9.0
    store.write("fina_indicator", "all", first, as_of="20240315")
    store.write("fina_indicator", "all", restated, as_of="20240420", mode="merge")

    from service.adapters.pit import pit_financials

    stored = store.read("fina_indicator")
    assert len(stored) == 2
    before = pit_financials(stored, "20240401")
    assert before["ann_date"].tolist() == ["20240315"]
    assert before["roe"].tolist() == [10.0]


def test_pruning_keeps_a_partition_with_unknown_coverage(store, registry):
    """A wrong skip silently deletes data. Unknown coverage must mean 'read it'."""
    df = make_daily(["20240102", "20240103"])
    store.write("daily", "2024", df, as_of="20240103")

    rec = registry.get_manifest("daily", "2024")
    registry.upsert_manifest(replace(rec, cover_start=None, cover_end=None))

    back = store.read("daily", "20250101", "20250131")
    assert back.empty, "窗外的数据不该被返回"
    still = store.read("daily", "20240101", "20240131")
    assert len(still) == 2


def test_overlap_is_decided_on_recorded_coverage():
    from types import SimpleNamespace

    from service.adapters.parquet_store import _partition_overlaps

    rec = SimpleNamespace(cover_start="20240101", cover_end="20241231")
    assert _partition_overlaps(rec, "20240601", "20240630") is True
    assert _partition_overlaps(rec, "20230101", "20231231") is False
    assert _partition_overlaps(rec, "20250101", None) is False
    assert _partition_overlaps(rec, None, "20250101") is True
    assert _partition_overlaps(rec, None, None) is True

    unknown = SimpleNamespace(cover_start=None, cover_end="20241231")
    assert _partition_overlaps(unknown, "20250101", "20250131") is True

    yearly = SimpleNamespace(cover_start="2024", cover_end="2024")
    assert _partition_overlaps(yearly, "20250101", "20250131") is True, "非 8 位日期不裁剪"


@pytest.mark.parametrize("failure_stage", ["replace", "meta", "upsert"])
def test_publication_failures_keep_old_snapshot_on_old_bytes(
    store, registry, monkeypatch, failure_stage
):
    first = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert first is not None
    old_bytes = Path(first.path).read_bytes()
    failed = [True]

    if failure_stage == "replace":
        original = parquet_store._replace_retrying

        def inject(src, dst):
            if failed[0]:
                raise RuntimeError("injected replace crash")
            return original(src, dst)

        monkeypatch.setattr(parquet_store, "_replace_retrying", inject)
    elif failure_stage == "meta":
        original = parquet_store._atomic_json

        def inject(path, payload):
            if failed[0]:
                raise RuntimeError("injected metadata crash")
            return original(path, payload)

        monkeypatch.setattr(parquet_store, "_atomic_json", inject)
    else:
        original = registry.upsert_manifest

        def inject(rec):
            if failed[0]:
                raise RuntimeError("injected manifest crash")
            return original(rec)

        monkeypatch.setattr(registry, "upsert_manifest", inject)

    update = make_daily(["20240102", "20240103"])
    update.loc[update["trade_date"] == "20240102", "close"] = 88.0
    with pytest.raises(RuntimeError, match="injected"):
        store.write("daily", "2024", update, as_of="20240103", mode="merge")

    assert registry.get_manifest("daily", "2024") == first
    assert Path(first.path).read_bytes() == old_bytes
    assert store.read("daily")["trade_date"].tolist() == ["20240102"]

    failed[0] = False
    retried = store.write("daily", "2024", update, as_of="20240103", mode="merge")
    assert retried is not None
    back = store.read("daily")
    assert back["trade_date"].tolist() == ["20240102", "20240103"]
    assert len(back.drop_duplicates(["trade_date", "ts_code"])) == 2
    assert float(back.loc[back["trade_date"] == "20240102", "close"].iloc[0]) == 88.0


def test_retention_keeps_current_and_one_previous_version(store):
    records = []
    for day in ["20240102", "20240103", "20240104", "20240105"]:
        rec = store.write("daily", "2024", make_daily([day]), as_of=day)
        assert rec is not None
        records.append(rec)

    assert not Path(records[0].path).exists()
    assert not Path(records[1].path).exists()
    assert Path(records[-2].path).exists()
    assert Path(records[-1].path).exists()
    assert store.path_for("daily", "2024") == Path(records[-1].path)

    version_dir = Path(records[-1].path).parent
    assert set(version_dir.glob("*.parquet")) == {
        Path(records[-2].path), Path(records[-1].path)
    }
    assert set(version_dir.glob("*.meta.json")) == {
        Path(records[-2].path).with_suffix(".meta.json"),
        Path(records[-1].path).with_suffix(".meta.json"),
    }
    meta = json.loads(Path(records[-1].path).with_suffix(".meta.json").read_text())
    assert meta["snapshot_id"] == records[-1].snapshot_id
    assert meta["previous_path"] == records[-2].path
    assert len(meta["file_sha256"]) == 64
    assert store.read("daily")["trade_date"].tolist() == ["20240105"]
    assert {key[0] for key in store._validated} == {records[-2].path, records[-1].path}


def test_cleaned_old_manifest_refreshes_and_retries(tmp_path, registry, monkeypatch):
    store = DatasetStore(tmp_path, registry, retain_previous=0)
    old = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert old is not None
    new = store.write("daily", "2024", make_daily(["20240102", "20240103"]),
                      as_of="20240103")
    assert new is not None
    assert not Path(old.path).exists()

    # Simulate a reader that listed the manifest before the second publication.
    monkeypatch.setattr(registry, "list_manifest", lambda dataset=None, status=None: [old])
    back = store.read("daily")
    assert back["trade_date"].tolist() == ["20240102", "20240103"]


def test_snapshot_validation_also_refreshes_a_cleaned_old_manifest(
    tmp_path, registry, monkeypatch
):
    store = DatasetStore(tmp_path, registry, retain_previous=0)
    old = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert old is not None
    new = store.write("daily", "2024", make_daily(["20240103"]), as_of="20240103")
    assert new is not None
    assert not Path(old.path).exists()

    monkeypatch.setattr(registry, "list_manifest", lambda dataset=None, status=None: [old])
    assert store.snapshot_for("daily", "20240103") == new.snapshot_id


def test_current_manifest_with_missing_file_fails_closed(store, monkeypatch):
    # Real corruption must still fail closed. The bounded back-off is only a
    # wait for a concurrent writer; it may not turn a missing file into a
    # silent empty read. Pin the sleeps to zero so this test stays fast.
    monkeypatch.setattr(DatasetStore, "_retry_sleep", staticmethod(lambda attempt: None))
    rec = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert rec is not None
    Path(rec.path).unlink()

    with pytest.raises(StoreCorruptionError, match="current manifest"):
        store.read("daily")
    with pytest.raises(StoreCorruptionError, match="current manifest"):
        store.read_partition("daily", "2024")


def test_missing_meta_that_appears_during_backoff_is_read_not_dropped(store, monkeypatch):
    """The 2026-11-09 race: half-pruned version while a writer republishes.

    A reader holds a manifest whose .meta.json was deleted by a concurrent
    prune. On the first retry the writer publishes a new version. The read must
    return that version's data, not an empty frame and not a corruption error.
    """
    old = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert old is not None
    new = store.write("daily", "2024", make_daily(["20240102", "20240103"]),
                      as_of="20240103")
    assert new is not None
    # The reader holds `old`, whose meta was deleted by a concurrent prune.
    Path(old.path).with_suffix(".meta.json").unlink()

    registry = store.registry
    real_get = registry.get_manifest
    calls = {"n": 0}

    def staged_get(dataset, partition):
        calls["n"] += 1
        # First check: the writer has not published yet, so the pointer still
        # names the half-deleted version. Afterwards it has moved.
        return old if calls["n"] == 1 else real_get(dataset, partition)

    monkeypatch.setattr(registry, "get_manifest", staged_get)
    monkeypatch.setattr(registry, "list_manifest",
                        lambda dataset=None, status=None: [old])
    monkeypatch.setattr(DatasetStore, "_retry_sleep",
                        staticmethod(lambda attempt: None))

    back = store.read("daily")
    assert back["trade_date"].tolist() == ["20240102", "20240103"]
    assert calls["n"] >= 2, "the reader must re-check the pointer, not fail at once"


def test_manifest_backoff_is_bounded(store, monkeypatch):
    """No writer ever publishes -> bounded attempts, then the same hard error."""
    monkeypatch.setattr(DatasetStore, "_retry_sleep",
                        staticmethod(lambda attempt: None))
    rec = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert rec is not None
    Path(rec.path).with_suffix(".meta.json").unlink()
    with pytest.raises(StoreCorruptionError, match="current manifest"):
        store.read("daily")


def test_legacy_stable_file_read_merge_retention_and_drop(store, registry):
    # Build the pre-versioning layout: stable parquet, basic metadata, manifest path.
    initial = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert initial is not None
    version = Path(initial.path)
    legacy = store.parquet_root / "daily" / "2024.parquet"
    legacy.write_bytes(version.read_bytes())
    legacy.with_suffix(".meta.json").write_text(json.dumps({
        "snapshot_id": initial.snapshot_id,
        "checksum": initial.checksum,
        "rows": initial.rows,
    }))
    registry.upsert_manifest(replace(initial, path=str(legacy)))
    for candidate in version.parent.iterdir():
        candidate.unlink()
    version.parent.rmdir()

    assert store.path_for("daily", "2024") == legacy
    assert store.read("daily")["trade_date"].tolist() == ["20240102"]
    assert store.read_partition("daily", "2024")["trade_date"].tolist() == ["20240102"]

    merged = store.write("daily", "2024", make_daily(["20240102", "20240103"]),
                         as_of="20240103", mode="merge")
    assert merged is not None
    assert Path(merged.path) != legacy
    # 扁平路径现在是**活版本的镜像**，不再是"保留下来的前一个版本"：每次写入都
    # 重新硬链接，所以它必然存在，且与清单指向同一个 inode。
    assert legacy.exists()
    assert legacy.stat().st_ino == Path(merged.path).stat().st_ino
    assert store.read("daily")["trade_date"].tolist() == ["20240102", "20240103"]

    third = store.write("daily", "2024", make_daily(["20240104"]),
                        as_of="20240104", mode="merge")
    assert third is not None
    assert legacy.exists(), "镜像不能因为保留策略被删掉——备份就靠它"
    assert legacy.stat().st_ino == Path(third.path).stat().st_ino
    # 保留策略管的是 .versions 里的不可变文件：当前 + 一个前驱
    kept = list((store.parquet_root / "daily" / ".versions").glob("**/*.parquet"))
    assert 1 <= len(kept) <= 3, kept
    assert store.read("daily")["trade_date"].tolist() == [
        "20240102", "20240103", "20240104"
    ]

    store.drop_partition("daily", "2024")
    assert registry.get_manifest("daily", "2024") is None
    assert store.read("daily").empty
    assert not store.path_for("daily", "2024").exists()
    assert not list((store.parquet_root / "daily" / ".versions").glob("**/*.parquet"))
    assert not list((store.parquet_root / "daily" / ".versions").glob("**/*.meta.json"))


def test_abandoned_legacy_staging_is_swept_but_locks_and_neighbours_are_kept(store):
    """A crash in the pre-versioning writer left a tmp beside the stable file."""
    folder = store.parquet_root / "daily"
    folder.mkdir(parents=True, exist_ok=True)
    abandoned = folder / ".2024.6accdf6e.tmp"
    abandoned.write_bytes(b"half a parquet")
    keep_tmp = folder / ".2023.0badc0de.tmp"
    keep_tmp.write_bytes(b"another partition, not ours")
    lock = folder / ".2024.lock"
    lock.write_bytes(b"\0")
    neighbour = folder / "2024.parquet.bak"
    neighbour.write_bytes(b"not a staging name")

    store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")

    assert not abandoned.exists()
    assert keep_tmp.exists()
    assert lock.exists()
    assert neighbour.exists()


def test_concurrent_read_write_never_returns_empty_or_partial_versions(tmp_path, registry):
    store = DatasetStore(tmp_path, registry, retain_previous=0)
    days = ["20240102", "20240103"]
    initial = make_daily(days)
    initial["close"] = 0.0
    store.write("daily", "2024", initial, as_of="20240103")

    stop = threading.Event()
    errors = []

    def read_loop():
        while not stop.is_set():
            try:
                frame = store.read("daily")
                if (frame["trade_date"].tolist() != days or frame["close"].nunique() != 1):
                    errors.append(AssertionError(f"partial version: {frame}"))
                    return
            except Exception as exc:  # noqa: BLE001 - relay thread failures to pytest
                errors.append(exc)
                return

    readers = [threading.Thread(target=read_loop) for _ in range(2)]
    for reader in readers:
        reader.start()
    try:
        for generation in range(1, 16):
            frame = make_daily(days)
            frame["close"] = float(generation)
            store.write("daily", "2024", frame, as_of="20240103")
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=10)

    assert not errors
    assert all(not reader.is_alive() for reader in readers)


def test_read_partition_honours_quarantine_and_can_opt_in(store, registry):
    store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    registry.set_manifest_status("daily", "2024", "quarantine")

    assert store.read_partition("daily", "2024").empty
    warned = store.read_partition("daily", "2024", status=("quarantine",))
    assert warned["trade_date"].tolist() == ["20240102"]


def test_corrupt_live_file_cannot_be_read_or_used_as_a_snapshot(store):
    rec = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert rec is not None
    with Path(rec.path).open("r+b") as handle:
        handle.write(b"BAD!")

    with pytest.raises(StoreCorruptionError):
        store.read("daily")
    with pytest.raises(StoreCorruptionError):
        store.read_partition("daily", "2024")
    with pytest.raises(StoreCorruptionError):
        store.snapshot_for("daily", "20240102")
    with pytest.raises(StoreCorruptionError):
        store.write("daily", "2024", make_daily(["20240103"]),
                    as_of="20240103", mode="merge")
    assert store.registry.get_manifest("daily", "2024") == rec


def test_metadata_mismatch_fails_closed_before_read_or_merge(store):
    rec = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    assert rec is not None
    meta_path = Path(rec.path).with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text())
    meta["snapshot_id"] = "not-the-manifest-snapshot"
    meta_path.write_text(json.dumps(meta))

    with pytest.raises(StoreCorruptionError, match="manifest/metadata mismatch"):
        store.read("daily")
    with pytest.raises(StoreCorruptionError, match="manifest/metadata mismatch"):
        store.write("daily", "2024", make_daily(["20240103"]),
                    as_of="20240103", mode="merge")
    assert store.registry.get_manifest("daily", "2024") == rec


def test_concurrent_writers_leave_one_verifiable_commit(store):
    from concurrent.futures import ThreadPoolExecutor

    def write(index):
        frame = make_daily(["20240102"])
        frame["close"] = float(index + 1)
        return store.write("daily", "2024", frame, as_of="20240102")

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(write, range(8)))

    final = store.registry.get_manifest("daily", "2024")
    expected = {rec.snapshot_id: float(index + 1) for index, rec in enumerate(records)}
    assert final.snapshot_id in expected
    assert float(store.read("daily")["close"].iloc[0]) == pytest.approx(
        expected[final.snapshot_id]
    )
    assert not list(Path(final.path).parent.glob(".*.tmp"))


def test_an_object_boolean_column_survives_its_own_manifest(store):
    """A fully populated True/False column must not break its own checksum.

    Parquet stores an object column of booleans as BOOLEAN and reads it back as
    ``bool``. Hashing the pre-write frame therefore disagreed with every later
    read, and the staging check refused the write — but only on sessions where
    the column had no nulls, which is why 29 of 32 ths_ladder partitions carried
    a checksum their file could never reproduce.
    """
    frame = pd.DataFrame({
        "ts_code": ["600000.SH", "600001.SH"],
        "trade_date": ["20261014", "20261014"],
        "board": ["1板", "1板"],
        "board_num": [1, 1],
        "name": ["甲", "乙"],
        "seal_nextday": pd.Series([True, False], dtype=object),  # no nulls
        "sign_level": [0.1, 0.2],
    })
    assert frame["seal_nextday"].dtype == object

    rec = store.write("ths_ladder", "20261014", frame, as_of="20261014")
    assert rec is not None

    back = store.read("ths_ladder", "20261014", "20261014")
    assert len(back) == 2
    assert back["seal_nextday"].tolist() == [1.0, 0.0]


# A child process that holds one partition lock for N seconds, so the parent can
# measure how long the *other* side is willing to wait.
_HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from service.adapters.parquet_store import _partition_lock
with _partition_lock(Path(sys.argv[2])):
    print("held", flush=True)
    time.sleep(float(sys.argv[3]))
"""


def test_the_partition_lock_outlasts_a_real_write(tmp_path):
    """A waiter must not give up while the holder is still legitimately busy.

    `msvcrt.LK_LOCK` looks blocking but retries only ten times, one second
    apart, then raises OSError("Resource deadlock avoided") — 9.1 seconds. One
    `stk_mins` partition takes 158 seconds, so the scheduled job and a manual
    backfill that overlapped by a few seconds killed one of them, with an error
    that said "deadlock" and meant "busy". Measured here, not assumed.
    """
    import subprocess
    import sys
    import time

    target = tmp_path / "2026.parquet"
    target.write_bytes(b"")
    root = str(Path(__file__).resolve().parents[2])
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, root, str(target), "10.5"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "held", proc.stderr.read()
        started = time.monotonic()
        with parquet_store._partition_lock(target, wait=30):
            waited = time.monotonic() - started
    finally:
        proc.wait(timeout=30)

    assert waited >= 9.5, f"gave up after {waited:.1f}s, before the holder finished"


def test_a_busy_partition_says_busy_not_deadlock(tmp_path):
    """The error must name the partition and the wait, not claim a deadlock."""
    import subprocess
    import sys

    target = tmp_path / "2026.parquet"
    target.write_bytes(b"")
    root = str(Path(__file__).resolve().parents[2])
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, root, str(target), "3"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "held", proc.stderr.read()
        with (pytest.raises(parquet_store.StoreBusyError, match="2026.parquet"),
              parquet_store._partition_lock(target, wait=0.4)):
            pass
    finally:
        proc.wait(timeout=30)



def test_a_one_session_merge_is_distinguishable_from_a_full_recompute(store):
    """`rows` alone cannot tell these apart; `input_rows` can.

    On 2026-10-29 the 2026 partition was rewritten twice while a verdict was
    being compared against one recorded six days earlier. Whether the panel had
    actually changed could not be answered afterwards: a one-row append and a
    full recompute leave manifests of the same shape, and the older version had
    already been pruned.
    """
    base = make_daily(["20240102", "20240103"])
    first = store.write("daily", "2024", base, as_of="20240103")
    assert first.input_rows == 2
    assert first.rows == 2

    appended = store.write("daily", "2024", make_daily(["20240104"]),
                           as_of="20240104", mode="merge")
    assert appended.input_rows == 1, "supplied one session"
    assert appended.rows == 3, "the partition now holds three"

    recomputed = store.write("daily", "2024",
                             make_daily(["20240102", "20240103", "20240104"]),
                             as_of="20240105")
    assert recomputed.input_rows == 3
    assert recomputed.rows == 3


def test_snapshot_for_never_binds_a_future_partition(store):
    r2023 = store.write("daily", "2023", make_daily(["20230601"]), as_of="20230601")
    r2025 = store.write("daily", "2025", make_daily(["20250602"]), as_of="20250602")
    assert r2023 and r2025

    assert store.snapshot_for("daily", "20200101") is None
    assert store.snapshot_for("daily", "20240101") == r2023.snapshot_id
    assert store.snapshot_for("daily", "20260101") == r2025.snapshot_id


def test_snapshot_for_accepts_the_same_date_in_any_shape(store):
    """``as_of`` arrives from a URL; ``2026-10-30`` is a shape, not a date.

    Un-normalised, the exact partition match failed and ``as_of[:4]`` became
    ``"2026-"`` -- so the lineage receipt recorded the *newest* partition instead
    of the one that served the decision, with no error anywhere.
    """
    r2023 = store.write("daily", "2023", make_daily(["20230601"]), as_of="20230601")
    r2024 = store.write("daily", "2024", make_daily(["20240603"]), as_of="20240603")
    assert r2023 and r2024

    assert store.snapshot_for("daily", "2023-11-01") == r2023.snapshot_id
    assert store.snapshot_for("daily", "2024/06/03") == r2024.snapshot_id


def test_every_write_leaves_the_flat_mirror_in_place(store):
    """`parquet/<dataset>/<partition>.parquet` 是给备份看的镜像，不是"上一个版本"。

    它由 `_link_canonical` 每次写入时硬链接出来，然后曾被 `_prune_versions`
    当成旧版本删掉——于是扁平目录里永远少最近写的那一个分区。实测 2026-11-01：
    stk_mins 30 个分区 29 个文件，缺的总是最新的那个；备份拷扁平目录会静默丢一天。
    """
    days = ["20240102", "20240103"]
    df = make_daily(days)
    rec = store.write("daily", "2024", df, as_of="20240103")

    flat = Path(store.parquet_root) / "daily" / "2024.parquet"
    assert flat.is_file(), "写入之后扁平镜像必须还在"
    assert flat.stat().st_ino == Path(rec.path).stat().st_ino, "镜像应与清单指向同一个文件"


def test_a_second_write_does_not_lose_the_mirror(store):
    days = ["20240102", "20240103"]
    store.write("daily", "2024", make_daily(days), as_of="20240103")
    rec = store.write("daily", "2024", make_daily(days + ["20240104"]), as_of="20240104")

    flat = Path(store.parquet_root) / "daily" / "2024.parquet"
    assert flat.is_file()
    assert Path(rec.path).exists(), "清单指向的版本必须存在"


def test_prune_keeps_whatever_the_manifest_currently_names(store, registry):
    """清理的保留集合要以注册表为准，不是以这次写入自己的链条为准。

    两个写入者交错时（服务在响应请求、日更在合并当天），各自按自己的 previous
    链清理；先提交清单的那个会被后清理的那个删掉文件，之后每次读都报
    "current manifest has no parquet"。
    """
    df = make_daily(["20240102", "20240103"])
    rec = store.write("daily", "2024", df, as_of="20240103")

    # 模拟"另一个进程把清单指向了更早的版本"
    earlier = registry.get_manifest("daily", "2024")
    assert earlier is not None
    store._prune_versions("daily", "2024", earlier)

    assert Path(rec.path).exists()
    assert registry.get_manifest("daily", "2024").path == rec.path


def test_pending_cleanup_never_deletes_the_flat_mirror(store):
    """待清理列表里可能留着一条指向扁平路径的旧记录——重试时不能照删。

    版本文件不可变、路径不复用，删了不会有事；扁平路径每次写入都会重建，
    删它就是把备份里的这一天抹掉。
    """
    store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    flat = Path(store.parquet_root) / "daily" / "2024.parquet"
    assert flat.is_file()

    store._pending_cleanup.add(str(flat))
    store._retry_cleanup()

    assert flat.is_file(), "扁平镜像被待清理列表误删"


def test_a_copied_manifest_goes_stale_and_the_fallback_heals_it(tmp_path):
    """复制清单再跑测试，是 2026-11-04 那次 4 连红的原因。

    采集器每 40 秒发布一个新版本、剪掉旧文件；复制出来的清单在几分钟后就
    指向已删除的路径，读取抛 "current manifest has no readable parquet/metadata"。
    复制解决的是"不想让测试跑到生产库里写 DDL"；这仍然需要，因为测试会写
    （`intraday.collect` 抓到数据要合并分区）。缺的是另一半：行悬空时不能
    判"库坏了"，要去问当前的那一份。这条钉住两者的差别。
    """
    import sqlite3

    import pytest

    from service.adapters.parquet_store import DatasetStore, StoreCorruptionError
    from service.adapters.sqlite_store import Registry

    live_db = tmp_path / "registry.sqlite"
    live = Registry(live_db)
    writer = DatasetStore(tmp_path, live, retain_previous=0)   # 每次写都剪掉上一版

    from tests.data.helpers import make_daily

    first_day = make_daily(["20260105"])
    writer.write("daily", "20260105", first_day, as_of="20260105")
    first = live.get_manifest("daily", "20260105")

    # 复制清单——旧夹具的做法
    copy_db = tmp_path / "copy.sqlite"
    with sqlite3.connect(live_db) as src, sqlite3.connect(copy_db) as dst:
        src.backup(dst)
    stale = Registry(copy_db)

    # 写者又发一版，并把上一版的文件删掉
    second_day = make_daily(["20260105"]).assign(close=11.0)
    writer.write("daily", "20260105", second_day, as_of="20260105")
    assert not Path(first.path).exists(), "retain_previous=0 应当剪掉旧文件"

    with pytest.raises(StoreCorruptionError, match="no readable parquet"):
        DatasetStore(tmp_path, stale).read("daily", "20260105", "20260105")

    # 带 fallback 的拷贝：行悬空时去问当前的那一份，读得到
    healed = Registry(copy_db, fallback_to=live_db)
    out = DatasetStore(tmp_path, healed).read("daily", "20260105", "20260105")
    assert float(out["close"].iloc[0]) == 11.0
    assert healed.get_manifest("daily", "20260105").path != first.path,         "拿到的是当前版本，不是那份已经删掉的"


def test_a_registry_that_cannot_answer_deletes_nothing(tmp_path, registry, monkeypatch):
    """清单读不出来时，一切都可能是指向目标——所以一个文件都不许删。

    清理的 `keep` 只是**这次**写入自己的 previous 链。另一个写入者的版本不在
    链里，靠注册表那一次 re-read 才被认出来。旧代码把读取失败吞成 `live=None`，
    和"确实没有清单"变成同一个值，于是异常路径恰好删掉了权威指向的那个文件；
    之后每次读都抛 "current manifest has no parquet"——就是 MEMORY 里
    「清单指针不许倒退」记过的那次三个池分区 500。
    """
    import sqlite3

    store = DatasetStore(tmp_path, registry, retain_previous=0)
    first = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    second = store.write("daily", "2024", make_daily(["20240102", "20240103"]),
                         as_of="20240103")
    assert not Path(first.path).exists(), "retain_previous=0 应剪掉上一版"
    assert Path(second.path).exists()

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(registry, "get_manifest", boom)
    # 一个持有旧视图的写入者来剪枝：它的链里只有 first，第二版只靠注册表认识。
    store._prune_versions("daily", "2024", first)

    assert Path(second.path).exists(), "读不到清单时删掉了当前版本"


def test_a_registry_failure_midway_stops_the_remaining_deletes(tmp_path, registry,
                                                              monkeypatch):
    """循环里每次删除前都要重读指针；读到一半失败就必须停在原地。

    只保住"进入循环前读到的那一份"是不够的：每轮重读正是为了让并发写入者
    刚发布的版本不被删。读不出来时继续删，等于把重读当成装饰。
    """
    import sqlite3

    store = DatasetStore(tmp_path, registry, retain_previous=0)
    first = store.write("daily", "2024", make_daily(["20240102"]), as_of="20240102")
    second = store.write("daily", "2024", make_daily(["20240102", "20240103"]),
                         as_of="20240103")

    calls = {"n": 0}

    def flaky(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return first          # 旧视图：声称清单指的还是第一版
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(registry, "get_manifest", flaky)
    store._prune_versions("daily", "2024", first)

    assert calls["n"] >= 2, "循环没有重读清单，这条用例就没走到要测的分支"
    assert Path(second.path).exists(), "循环中途读不到清单仍然删了文件"
