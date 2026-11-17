"""Partitioned Parquet store with lineage and quarantine.

A manifest points at an immutable parquet version. Writers stage and read back a
new version before atomically publishing its manifest; they never replace the
file named by a live manifest. A small number of previous versions are retained
for readers that already hold an older manifest. If cleanup wins that race, the
reader refreshes its manifest and retries instead of silently skipping data.

``snapshot_id`` is derived from a content checksum, not from the clock, so
re-ingesting identical data yields the same id. Quarantined partitions stay on
disk but are excluded from ``read()``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.api.types import infer_dtype, is_numeric_dtype

from service import settings

from .catalog import DatasetSpec, get_spec
from .sqlite_store import ManifestRecord, Registry

log = logging.getLogger(__name__)

DATE_COLUMNS = {
    "trade_date", "cal_date", "pretrade_date", "ann_date", "f_ann_date",
    "end_date", "start_date", "ex_date", "record_date", "pay_date",
    "imp_ann_date", "list_date", "delist_date", "nav_date", "found_date",
    "due_date", "issue_date", "div_listdate", "change_date",
}

READABLE_STATUS = ("ok", "warn")

# How many validated (path, snapshot) pairs to remember. Each entry is three
# scalars keyed by a string path, so this is a few hundred kilobytes — large
# enough that a full read of the store stays cached, small enough to be bounded.
VALIDATED_CACHE_LIMIT = settings.integer("storage.validated_cache_limit")

# A writer publishes parquet + meta before upserting the manifest, and prunes
# old versions immediately afterwards. On Windows an open reader handle can
# postpone deleting a parquet file while its .meta.json goes away first, leaving
# a half-deleted version the manifest may still name for a few milliseconds.
# A bounded back-off lets that writer finish. It does NOT weaken validation and
# exhaustion still fails closed. Measured 2026-11-09: the 17:30 intraday_sweep
# rewrote stk_mins[day] 24 times in ~2 minutes, and a concurrent full pytest
# run hit this gap 23 times.
MANIFEST_RETRY_DELAYS = (0.02, 0.04, 0.08, 0.16, 0.32)


class _TransientManifestGap(Exception):
    """The manifest has not moved but its files are momentarily unreadable.

    Not a corruption verdict: raised only so the read loops can back off and
    re-check. If no writer publishes, the caller converts it into the same
    ``StoreCorruptionError`` it has always raised.
    """


class SchemaError(ValueError):
    """Raised when a frame does not match the dataset contract."""


class StoreCorruptionError(RuntimeError):
    """The current manifest names data that cannot be read."""


class StoreBusyError(RuntimeError):
    """Another process held a partition lock past the configured wait.

    Distinct from corruption on purpose: nothing is wrong with the bytes, the
    writer simply could not get its turn. Callers retry; they must not "repair".
    """


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stage_json(path: Path, payload: dict[str, Any]) -> Path:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _stage_json(tmp, payload)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

# How long a writer waits for another process to finish the same partition.
# 600s is not a guess: one `stk_mins` partition measured 158s end to end, and a
# backfill can chain a few. The wait is bounded so a genuinely stuck holder
# surfaces as an error instead of hanging the scheduled job forever.
LOCK_WAIT_SECONDS = settings.num("storage.lock_wait_seconds")


def _thread_lock(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _lock_busy_message(path: Path, waited: float, limit: float) -> str:
    """The hint has to name a variable that something actually reads.

    It used to say ``QUANT_STORE_LOCK_WAIT``, which appears nowhere else in the
    repository: following the advice changed nothing, and the operator's next
    move was to conclude the limit was not configurable. The real override goes
    through ``settings`` (``QUANT__STORAGE__LOCK_WAIT_SECONDS``), so the message
    quotes that and the value currently in force.
    """
    return (
        f"partition lock busy after {waited:.0f}s: {path.name} "
        f"(another process is writing it; this wait comes from "
        f"storage.lock_wait_seconds={limit:.0f}, override it with "
        f"QUANT__STORAGE__LOCK_WAIT_SECONDS if that write is legitimately long)"
    )


@contextmanager
def _partition_lock(path: Path, *, wait: float | None = None) -> Iterator[None]:
    """Serialize writers of one logical partition across threads and processes.

    Windows needs care here. ``msvcrt.LK_LOCK`` looks blocking but is not: the
    CRT retries ten times, one second apart, then raises ``OSError`` with
    "Resource deadlock avoided" — nine seconds, no matter how long the holder
    legitimately needs. Merging one daily partition takes minutes, so the
    scheduled job and a manual backfill that overlapped by a few seconds made
    one of them abort with an error that says "deadlock" and means "busy".

    The wait is therefore written out explicitly: non-blocking attempt, short
    sleep, repeat until the deadline. Both platforms take the same path, so a
    POSIX run and a Windows run block for the same amount of time.
    """
    limit = LOCK_WAIT_SECONDS if wait is None else float(wait)
    lock_path = path.with_name(f".{path.stem}.lock")
    with _thread_lock(str(lock_path)), lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            def try_acquire() -> bool:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    return True
                except OSError:
                    return False
        else:
            import fcntl

            def try_acquire() -> bool:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except OSError:
                    return False

        started = time.monotonic()
        waited = 0.0
        while not try_acquire():
            waited = time.monotonic() - started
            if waited >= limit:
                raise StoreBusyError(_lock_busy_message(path, waited, limit))
            time.sleep(0.2)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def as_yyyymmdd(value: object) -> str | None:
    """Normalise a date-ish value to ``YYYYMMDD``, or None when it is not one.

    Public because the API layer has to canonicalise a query parameter *before*
    comparing it to a stored date: ``"2026-10-30" <= "20261026"`` is False, so an
    un-normalised parameter silently marks a stale dataset as current.
    """
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    digits = text.replace("-", "").replace("/", "")[:8]
    return digits if digits.isdigit() else text


# Kept as an alias: the module reads as private inside and as public outside.
_as_yyyymmdd = as_yyyymmdd


def _partition_overlaps(rec, start: str | None, end: str | None) -> bool:
    """Conservatively decide whether a partition can overlap the requested window."""
    cov_start = _as_yyyymmdd(rec.cover_start) if rec.cover_start else None
    cov_end = _as_yyyymmdd(rec.cover_end) if rec.cover_end else None
    if not cov_start or not cov_end:
        return True
    if len(cov_start) != 8 or len(cov_end) != 8:
        return True
    if not (cov_start.isdigit() and cov_end.isdigit()):
        return True
    want_start = _as_yyyymmdd(start) if start else None
    want_end = _as_yyyymmdd(end) if end else None
    if want_start and len(want_start) == 8 and want_start.isdigit() and cov_end < want_start:
        return False
    return not (want_end and len(want_end) == 8 and want_end.isdigit() and cov_start > want_end)


def _filter_groups(filters) -> list[list[tuple]]:
    """Normalise pyarrow's conjunction / disjunction filter forms."""
    if not filters:
        return []
    if isinstance(filters, tuple) and len(filters) == 3 and isinstance(filters[0], str):
        return [[filters]]
    items = list(filters)
    if not items:
        return []
    if isinstance(items[0], tuple):
        return [items]
    return [list(group) for group in items]


def _read_filters(date_column: str, start: str | None, end: str | None,
                  filters) -> list | None:
    groups = _filter_groups(filters)
    if not groups:
        groups = [[]]
    if start:
        for group in groups:
            group.append((date_column, ">=", start))
    if end:
        for group in groups:
            group.append((date_column, "<=", end))
    if not any(groups):
        return None
    return groups[0] if len(groups) == 1 else groups


def _replace_retrying(src: Path, dst: Path, attempts: int = 12,
                      wait: float = 0.25) -> None:
    """`os.replace`, but patient with transient Windows file handles."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:      # WinError 5 / 32: file in use
            last = exc
            time.sleep(wait * (1 + attempt))
    raise last  # type: ignore[misc]


def normalize_frame(df: pd.DataFrame, spec: DatasetSpec) -> pd.DataFrame:
    """Stable dtypes + stable column order, so hashes are reproducible."""
    out = df.copy()
    missing = [c for c in spec.required if c not in out.columns]
    if missing:
        raise SchemaError(f"{spec.name}: missing required columns {missing}")

    for col in out.columns:
        if col in DATE_COLUMNS or col == spec.date_column:
            out[col] = out[col].map(_as_yyyymmdd)
        elif out[col].dtype == bool:
            out[col] = out[col].astype("int8")
        elif out[col].dtype == object and infer_dtype(out[col], skipna=True) == "boolean":
            # An object column of True/False/None is written as parquet BOOLEAN
            # and read back as ``bool`` — not as object. Hashing the pre-write
            # frame therefore disagrees with every later read, and the staging
            # check refuses the write. Worse, it only bites on sessions where the
            # column is fully populated: the all-null days round-trip as object
            # and publish fine, which is why 29 of 32 ths_ladder partitions
            # carried a checksum their own file could never reproduce.
            out[col] = out[col].astype("float64")
        elif is_numeric_dtype(out[col]):
            out[col] = out[col].astype("float64")

    out = out.reindex(sorted(out.columns), axis=1)
    return out.reset_index(drop=True)


def content_checksum(df: pd.DataFrame) -> str:
    """Hash of values only (column order normalised, index ignored)."""
    digest = hashlib.sha256()
    digest.update(f"rows={len(df)};cols={len(df.columns)};".encode())
    for col in sorted(df.columns):
        series = df[col]
        digest.update(f"{col}|{series.dtype}|".encode())
        hashed = pd.util.hash_pandas_object(series, index=False).to_numpy(dtype="uint64")
        digest.update(hashed.tobytes())
    return digest.hexdigest()


def schema_hash(df: pd.DataFrame) -> str:
    payload = "|".join(f"{c}:{df[c].dtype}" for c in sorted(df.columns))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def make_snapshot_id(dataset: str, partition: str, checksum: str, as_of: str) -> str:
    raw = f"{dataset}|{partition}|{checksum}|{as_of}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _cover(df: pd.DataFrame, spec: DatasetSpec) -> tuple[str | None, str | None]:
    if spec.date_column not in df.columns or df.empty:
        return None, None
    series = df[spec.date_column].dropna()
    if series.empty:
        return None, None
    return str(series.min()), str(series.max())


class DatasetStore:
    def __init__(self, root: str | Path, registry: Registry, *, retain_previous: int = 1):
        if retain_previous < 0:
            raise ValueError("retain_previous must be non-negative")
        self.root = Path(root)
        self.registry = registry
        self.retain_previous = retain_previous
        self.parquet_root = self.root / "parquet"
        self.parquet_root.mkdir(parents=True, exist_ok=True)
        self._validated: dict[tuple[str, str], tuple[int, int, str]] = {}
        # Paths a previous prune could not delete (Windows keeps a handle open while
        # a reader sits mid-file). Retried on the next prune instead of being dropped
        # on the floor: a silent failure leaves a stale flat parquet in the live
        # directory, which is a second answer to "what is this partition".
        self._pending_cleanup: set[str] = set()

    # ------------------------------------------------------------------ paths
    def _legacy_path(self, dataset: str, partition: str) -> Path:
        return self.parquet_root / dataset / f"{partition}.parquet"

    def _version_dir(self, dataset: str, partition: str) -> Path:
        return self.parquet_root / dataset / ".versions" / partition

    def path_for(self, dataset: str, partition: str) -> Path:
        """Return the live version, or the stable legacy path when unpublished.

        Manifests record an **absolute** path, and that path is how the whole
        store finds its data. It works until the tree moves: copy `data/` to
        another drive and every read raises `FileNotFoundError`, which the reader
        reports as `StoreCorruptionError` — a corruption verdict for what is only
        a different root. So a path that no longer exists is re-rooted at this
        store's own `parquet/` directory using the tail of the recorded path.
        """
        rec = self.registry.get_manifest(dataset, partition)
        if rec is None:
            return self._legacy_path(dataset, partition)
        path = Path(rec.path)
        if path.exists():
            return path
        parts = path.parts
        if "parquet" in parts:
            tail = parts[len(parts) - parts[::-1].index("parquet"):]
            candidate = self.parquet_root.joinpath(*tail)
            if candidate.exists():
                return candidate
        return path

    # ------------------------------------------------------------------ write
    def write(
        self,
        dataset: str,
        partition: str,
        df: pd.DataFrame,
        *,
        as_of: str,
        mode: str = "replace",
        status: str | None = None,
        note: str = "",
        source: str | None = None,
        trust_tier: str | None = None,
        allow_repair: bool = False,
    ) -> ManifestRecord | None:
        """Stage and verify an immutable version, then atomically publish its manifest.

        ``allow_repair`` exists for one case: the caller has already read the
        values of a partition whose manifest no longer matches its bytes, and is
        re-publishing those values. The staging check still proves the *new* file
        matches the *new* manifest; only the pre-write check of the broken
        predecessor is waived. Without it a corrupt manifest could never be
        repaired through the writer, which is how 29 ths_ladder partitions stayed
        unreadable instead of being fixed.

        ``status`` defaults to ``None``, which means "no opinion": a new partition
        becomes ``ok``, and an existing ``quarantine`` is carried forward. A
        quality verdict is not something a backfill should overturn by omission —
        clearing it takes ``status="ok"``, said out loud, after looking at why.
        """
        spec = get_spec(dataset)
        if df is None or df.empty:
            return None
        # What this write supplied, before merge and dedup. Recorded so a
        # one-session merge is distinguishable from a full recompute after the
        # fact; the checksum cannot tell them apart because the result may be
        # identical either way.
        input_rows = len(df)
        frame = normalize_frame(df, spec)
        legacy_path = self._legacy_path(dataset, partition)
        legacy_path.parent.mkdir(parents=True, exist_ok=True)

        with _partition_lock(legacy_path):
            existing = self.registry.get_manifest(dataset, partition)
            if existing is not None:
                old_path = Path(existing.path)
                # A manifest whose file is gone is the worst case, and it is the
                # one `allow_repair` used to miss: it only waived the checksum
                # check, so a partition whose bytes had already been deleted
                # could never be rewritten through the writer. It happened on
                # 2026-11-01 (a concurrent prune took the live version), and the
                # only way out was editing the registry by hand. Repair covers it
                # now, and because there is nothing to merge onto, the write is
                # forced to a full replace.
                missing_old = not old_path.exists()
                if missing_old and not allow_repair:
                    raise StoreCorruptionError(
                        f"current manifest has no parquet: {dataset}[{partition}]"
                    )
                if not missing_old:
                    try:
                        self._validate_record(existing)
                    except StoreCorruptionError:
                        if not allow_repair:
                            raise
                if mode == "merge" and missing_old:
                    mode = "replace"
                if mode == "merge":
                    # Re-normalise the *merged* frame, not just the new rows.
                    # `content_checksum` hashes each column's dtype, and a
                    # partition written by an older writer can carry object
                    # columns that parquet reads back as `bool`. The staging
                    # check then compared two different dtype sets and refused
                    # the write with "staging parquet failed verification",
                    # which points at the file instead of at the merge.
                    frame = normalize_frame(
                        pd.concat([pd.read_parquet(old_path), frame], ignore_index=True),
                        spec,
                    )
                if status is None and existing.status == "quarantine":
                    # A quarantine is a *verdict about the data*, and a writer
                    # must not overturn it by accident. `status` used to default
                    # to "ok", so any ordinary merge — a backfill, a re-run of
                    # the daily job — silently put a quarantined partition back
                    # into `read()`. Clearing it is a decision, so it now has to
                    # be said out loud: pass status="ok" (usually together with
                    # allow_repair) after looking at why it was quarantined.
                    status = "quarantine"

            if spec.pk:
                frame = frame.drop_duplicates(subset=list(spec.pk), keep="last")
                frame = frame.sort_values(list(spec.pk), kind="mergesort")
            frame = frame.reset_index(drop=True)

            # "ok" is the default for a *new* partition; inheriting a verdict
            # only applies to a partition that already had one.
            effective_status = status or "ok"
            checksum = content_checksum(frame)
            snap = make_snapshot_id(dataset, partition, checksum, as_of)
            cover_start, cover_end = _cover(frame, spec)
            version_dir = self._version_dir(dataset, partition)
            version_dir.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            path = version_dir / f"{partition}.{snap}.{token}.parquet"
            tmp = version_dir / f".{path.name}.tmp"
            meta_path = path.with_suffix(".meta.json")
            rec = ManifestRecord(
                dataset=dataset,
                partition=partition,
                source=source or spec.source,
                trust_tier=trust_tier or spec.trust_tier,
                as_of=as_of,
                ingested_at=datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
                rows=len(frame),
                snapshot_id=snap,
                checksum=checksum,
                schema_hash=schema_hash(frame),
                cover_start=cover_start,
                cover_end=cover_end,
                status=effective_status,
                path=str(path),
                note=note,
                input_rows=input_rows,
            )
            meta = {
                "format_version": 2,
                "dataset": dataset,
                "partition": partition,
                "rows": len(frame),
                "snapshot_id": snap,
                "checksum": checksum,
                "schema_hash": rec.schema_hash,
                "as_of": as_of,
                "cover_start": cover_start,
                "cover_end": cover_end,
                "columns": list(frame.columns),
                "dtypes": {c: str(t) for c, t in frame.dtypes.items()},
                "file_sha256": "",
                "previous_path": str(existing.path) if existing is not None else None,
                "previous_snapshot_id": existing.snapshot_id if existing is not None else None,
            }

            try:
                frame.to_parquet(tmp, index=False, compression="zstd")
                _fsync_file(tmp)
                staged = pd.read_parquet(tmp)
                if (len(staged) != rec.rows or content_checksum(staged) != checksum
                        or schema_hash(staged) != rec.schema_hash):
                    raise StoreCorruptionError(f"staging parquet failed verification: {tmp}")
                meta["file_sha256"] = _file_sha256(tmp)

                # The destination is unique to this write. Replacing a staging file
                # therefore never mutates bytes visible through an existing manifest.
                _replace_retrying(tmp, path)
                _fsync_dir(version_dir)
                _atomic_json(meta_path, meta)
                published_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if (published_meta.get("snapshot_id") != snap
                        or published_meta.get("file_sha256") != meta["file_sha256"]):
                    raise StoreCorruptionError(f"metadata mismatch for {path}")
            except Exception:
                self._drop(tmp)
                self._drop(path)
                self._drop(meta_path)
                raise

            # Do not remove the new version if SQLite raises: its transaction may
            # have committed before the error reached this layer. A retry is safe
            # because merge de-duplicates on the natural key and every write gets
            # a fresh immutable path.
            self.registry.upsert_manifest(rec)
            self._link_canonical(dataset, partition, Path(rec.path))
            self._prune_versions(dataset, partition, rec)
            return rec

    def _link_canonical(self, dataset: str, partition: str, version: Path) -> None:
        """Expose the live version at `parquet/<dataset>/<partition>.parquet`.

        Appending writes an immutable file under `.versions/` and points the
        manifest at it; the flat path is only materialised when a year closes.
        That left `data/parquet/daily/2026.parquet` **missing** for the whole
        current year, so any backup that copies the flat directory silently lost
        twelve months of data. A hard link costs no extra bytes (same volume) and
        makes the directory tell the truth. Best effort: a filesystem that
        refuses links must not fail the write.
        """
        target = self.parquet_root / dataset / f"{partition}.parquet"
        # Never unlink the target when it *is* the source: for a partition whose
        # manifest still points at the flat file, `unlink` would delete the only
        # copy and the link would then fail. Compare resolved paths first.
        try:
            if target.resolve() == version.resolve():
                return
        except OSError:
            return
        try:
            if target.exists() or target.is_symlink():
                target.unlink()
            os.link(version, target)
        except (OSError, NotImplementedError):
            try:
                shutil.copy2(version, target)
            except OSError:
                pass

    # --------------------------------------------------------------- retention
    def _drop(self, path: Path) -> None:
        """Delete a file, remembering failures so a later prune can retry."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            self._pending_cleanup.add(str(path))

    def _retry_cleanup(self) -> None:
        """Delete what a previous prune could not, and only immutable versions.

        A pending entry can outlive its relevance: the path it names may have
        been re-linked as the live mirror since it failed. Version files are
        immutable and never reused, so they are always safe to delete; the flat
        path is rebuilt on every write and deleting it is how backups lose a
        year. Anything outside `.versions/` is dropped from the queue instead.
        """
        for name in list(self._pending_cleanup):
            path = Path(name)
            if ".versions" not in path.parts:
                self._pending_cleanup.discard(name)
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            self._pending_cleanup.discard(name)

    def _prune_versions(self, dataset: str, partition: str, current: ManifestRecord) -> None:
        """Keep the live version and a short chain of committed predecessors.

        Starts by retrying whatever the previous prune could not delete.
        """
        self._retry_cleanup()
        keep: set[str] = set()
        path = Path(current.path)
        for _ in range(self.retain_previous + 1):
            keep.add(str(path))
            try:
                meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                break
            previous = meta.get("previous_path")
            if not previous:
                break
            path = Path(previous)

        # The registry is the authority on which file is live, not this call's
        # `current`. Two writers on the same partition (the service serving a
        # request while the daily job merges the session) each prune against
        # their own chain, and whichever upsert committed second decides what the
        # manifest points at. Without this re-read, the slower writer deletes the
        # version the manifest actually names: measured 2026-11-01, three writes
        # to stk_mins[20261030] in ten minutes left the manifest pointing at a
        # file that no longer existed, and every later read raised
        # "current manifest has no parquet".
        # A registry that cannot answer must stop the whole prune, not produce a
        # `live=None` that reads exactly like "nothing is live". The difference
        # matters: `None` means every candidate below is fair game, so a
        # transient "database is locked" during the 17:30 merge would delete the
        # version another writer had just published — and `keep` here is *this*
        # call's chain, which by definition does not contain it. Whether the read
        # failed or returned nothing is now decided before anything is deleted.
        try:
            live = self.registry.get_manifest(dataset, partition)
        except Exception as exc:  # cleanup is best-effort; a write is not
            log.warning("prune skipped for %s[%s]: registry unreadable (%s: %s)",
                        dataset, partition, type(exc).__name__, exc, exc_info=True)
            return
        if live is not None:
            keep.add(str(Path(live.path)))

        directory = self._version_dir(dataset, partition)
        try:
            candidates = list(directory.glob("*.parquet"))
            staging = list(directory.glob(".*.tmp"))
        except OSError:
            return
        for candidate in candidates:
            if str(candidate) in keep:
                continue
            # Re-read the pointer before each delete. The set above was computed
            # once, and a concurrent writer can move the manifest in between:
            # deleting the version it now names leaves the store pointing at a
            # file that does not exist, and every later read of that partition
            # raises "current manifest has no readable parquet/metadata".
            try:
                live = self.registry.get_manifest(dataset, partition)
            except Exception as exc:  # see the note above
                log.warning("prune stopped for %s[%s] after %s: registry unreadable"
                            " (%s: %s)", dataset, partition, candidate.name,
                            type(exc).__name__, exc, exc_info=True)
                return
            if live is not None and str(candidate) == str(Path(live.path)):
                continue
            self._drop(candidate)
            self._drop(candidate.with_suffix(".meta.json"))
        for candidate in staging:
            self._drop(candidate)

        # The flat path is the *mirror* of the live version, not a previous one:
        # `_link_canonical` hard-links it on every write so that a backup copying
        # `parquet/<dataset>/` sees the current partition. It used to fall into
        # the branch below — never in `keep`, because `keep` holds `.versions`
        # paths — so each write created the mirror and then deleted it one line
        # later. Measured 2026-11-01: 30 stk_mins partitions, 29 flat files, and
        # the missing one was always the most recently written.
        legacy = self._legacy_path(dataset, partition)
        keep.add(str(legacy))
        if str(legacy) not in keep:                      # pragma: no cover
            self._drop(legacy)
            self._drop(legacy.with_suffix(".meta.json"))

        # The same old writers staged beside the stable file, so a crash there left
        # `.{partition}.<hex>.tmp` behind with nothing to ever claim it again. No
        # writer can be using one now: this runs under the partition lock they took.
        # The lock file itself and unrelated names are left alone.
        try:
            abandoned = list(legacy.parent.glob(f".{partition}.*.tmp"))
        except OSError:
            abandoned = []
        for candidate in abandoned:
            self._drop(candidate)

        # Version paths are immutable and never reused, so pruned cache entries
        # can never become valid again. Do not let a long backfill grow this dict.
        owned = {str(candidate) for candidate in candidates} | {str(legacy)}
        for key in list(self._validated):
            if key[0] in owned and key[0] not in keep:
                self._validated.pop(key, None)

    # ------------------------------------------------------------------- read
    def _validate_record(self, rec: ManifestRecord) -> None:
        """Bind a manifest to its metadata and immutable bytes before using it."""
        path = Path(rec.path)
        if not path.exists():
            raise FileNotFoundError(path)
        try:
            meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise StoreCorruptionError(f"missing/invalid metadata for {path}") from exc
        expected = {
            "snapshot_id": rec.snapshot_id, "checksum": rec.checksum, "rows": rec.rows,
        }
        if (any(meta.get(key) != value for key, value in expected.items())
                or ("schema_hash" in meta and meta["schema_hash"] != rec.schema_hash)):
            raise StoreCorruptionError(f"manifest/metadata mismatch for {path}")

        stat = path.stat()
        # Stamped manifests carry a real hash; a legacy one carries "", which is
        # falsy. Treating "" as "a hash that must equal the cached value" made
        # the comparison fail forever, so every unstamped partition was re-read
        # and re-hashed on every single access — the cache could never hit.
        published_hash = meta.get("file_sha256") or None
        key = (str(path), rec.snapshot_id)
        cached = self._validated.get(key)
        if (cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns
                and (not published_hash or cached[2] == published_hash)):
            return
        if published_hash:
            if _file_sha256(path) != published_hash:
                raise StoreCorruptionError(f"parquet checksum mismatch for {path}")
        else:
            # Pre-versioning manifests have no byte hash. Validate their actual
            # contents once instead of treating a path as proof of a commit.
            frame = pd.read_parquet(path)
            if (len(frame) != rec.rows or content_checksum(frame) != rec.checksum
                    or schema_hash(frame) != rec.schema_hash):
                raise StoreCorruptionError(f"legacy parquet mismatch for {path}")
            published_hash = _file_sha256(path)
        self._validated[key] = (stat.st_size, stat.st_mtime_ns, published_hash)
        if len(self._validated) > VALIDATED_CACHE_LIMIT:
            # Paths are immutable, so an evicted entry only costs a re-hash of
            # one file. `_prune_versions` cannot do this alone: it can only see
            # the versions present in the directory *now*, so entries belonging
            # to versions pruned in an earlier round were never removed.
            for stale in list(self._validated)[:len(self._validated) // 4]:
                self._validated.pop(stale, None)

    def _refresh_after_missing(self, current: ManifestRecord, exc: FileNotFoundError,
                               status: tuple[str, ...] | None) -> ManifestRecord:
        latest = self.registry.get_manifest(current.dataset, current.partition)
        if latest is None:
            raise StoreCorruptionError(
                f"partition disappeared while reading: {current.dataset}[{current.partition}]"
            ) from exc
        if latest.snapshot_id == current.snapshot_id and latest.path == current.path:
            # The pointer has not moved. That is either real corruption (the
            # fail-closed test deletes a live file) or a writer mid-publish.
            # Only a bounded retry can tell them apart, so hand it back to the
            # loop instead of deciding here.
            raise _TransientManifestGap(str(exc)) from exc
        if status is not None and latest.status not in status:
            raise StoreCorruptionError(
                f"refreshed manifest is not readable: {current.dataset}[{current.partition}]"
            ) from exc
        return latest

    @staticmethod
    def _retry_sleep(attempt: int) -> None:
        """Back off between manifest re-checks; injectable in tests."""
        if attempt < len(MANIFEST_RETRY_DELAYS):
            time.sleep(MANIFEST_RETRY_DELAYS[attempt])

    def _validate_record_with_retry(self, rec: ManifestRecord) -> ManifestRecord:
        current = rec
        for attempt in range(len(MANIFEST_RETRY_DELAYS) + 1):
            try:
                self._validate_record(current)
                return current
            except FileNotFoundError as exc:
                try:
                    current = self._refresh_after_missing(current, exc, READABLE_STATUS)
                except _TransientManifestGap:
                    self._retry_sleep(attempt)
        raise StoreCorruptionError(
            f"current manifest has no readable parquet/metadata: "
            f"{rec.dataset}[{rec.partition}]"
        )

    def _read_record(self, rec: ManifestRecord, *, columns=None, filters=None,
                     status: tuple[str, ...] | None = None) -> pd.DataFrame:
        """Read one manifest version, refreshing only when its file was cleaned.

        A concurrent writer can leave a version half-pruned for a few
        milliseconds. Back off and re-read the pointer rather than declaring
        corruption immediately; if nothing publishes, fail closed as before.
        """
        current = rec
        for attempt in range(len(MANIFEST_RETRY_DELAYS) + 1):
            path = Path(current.path)
            try:
                if not path.exists():
                    raise FileNotFoundError(path)
                self._validate_record(current)
                return pd.read_parquet(path, columns=columns, filters=filters)
            except FileNotFoundError as exc:
                try:
                    current = self._refresh_after_missing(current, exc, status)
                except _TransientManifestGap:
                    self._retry_sleep(attempt)
        raise StoreCorruptionError(
            f"current manifest has no readable parquet/metadata: "
            f"{rec.dataset}[{rec.partition}]"
        )

    def read(
        self,
        dataset: str,
        start: str | None = None,
        end: str | None = None,
        *,
        columns: list[str] | None = None,
        status: tuple[str, ...] = READABLE_STATUS,
        filters=None,
    ) -> pd.DataFrame:
        spec = get_spec(dataset)
        start = _as_yyyymmdd(start) if start else None
        end = _as_yyyymmdd(end) if end else None
        pushed = _read_filters(spec.date_column, start, end, filters)
        filter_columns = {p[0] for group in _filter_groups(pushed) for p in group}
        fetch = columns
        if columns:
            fetch = sorted(set(columns) | {spec.date_column} | set(spec.pk) | filter_columns)
        records = [r for r in self.registry.list_manifest(dataset) if r.status in status]
        frames = []
        for rec in records:
            if not _partition_overlaps(rec, start, end):
                continue
            frames.append(self._read_record(rec, columns=fetch, filters=pushed, status=status))
        if not frames:
            return pd.DataFrame(columns=list(columns) if columns else None)

        df = pd.concat(frames, ignore_index=True)
        col = spec.date_column
        if col in df.columns and (start or end):
            s = df[col].astype(str)
            if start:
                df = df[s >= start]
            if end:
                df = df[df[col].astype(str) <= end]
        if spec.pk:
            keys = [c for c in spec.pk if c in df.columns]
            if keys:
                df = df.sort_values(keys, kind="mergesort").reset_index(drop=True)
        if columns:
            keep = [c for c in columns if c in df.columns]
            df = df[keep].reset_index(drop=True)
        return df

    def read_partition(self, dataset: str, partition: str,
                       columns: list[str] | None = None,
                       status: tuple[str, ...] = READABLE_STATUS) -> pd.DataFrame:
        rec = self.registry.get_manifest(dataset, partition)
        if rec is None or rec.status not in status:
            return pd.DataFrame()
        return self._read_record(rec, columns=columns, status=status)

    def partitions(self, dataset: str) -> list[ManifestRecord]:
        return self.registry.list_manifest(dataset)

    def snapshot_for(self, dataset: str, as_of: str) -> str | None:
        """Snapshot id of the partition that would have served ``as_of``.

        ``as_of`` reaches here from a URL, so it arrives in whatever shape the
        caller typed. Comparing ``"2026-10-30"`` against stored partition names
        missed the exact match, and ``"2026-10-30"[:4]`` is ``"2026-"`` — which
        matches nothing, silently. A lineage receipt then recorded the newest
        partition instead of the one that actually served the decision.
        """
        recs = [r for r in self.registry.list_manifest(dataset) if r.status in READABLE_STATUS]
        if not recs:
            return None
        day = _as_yyyymmdd(as_of) or str(as_of)

        for key in (day, day[:4]):
            for rec in recs:
                if rec.partition == key:
                    return self._validate_record_with_retry(rec).snapshot_id

        covering = [
            r for r in recs
            if (r.cover_start or "") <= day <= (r.cover_end or "99999999")
        ]
        if covering:
            pool = covering
        else:
            # A lineage receipt may bind the latest state that was already
            # available (for example a weekend, or a day after the last bar),
            # but it must never bind a snapshot from the future.  Falling back
            # to all records here made /data/lineage/20200101 attach the 2026
            # limit-up pools and still report complete=true.
            pool = [r for r in recs if r.cover_end and r.cover_end <= day]
            if not pool:
                return None
        best = max(pool, key=lambda r: (r.cover_end or "", r.ingested_at))
        return self._validate_record_with_retry(best).snapshot_id

    # -------------------------------------------------------------- lineage
    def drop_partition(self, dataset: str, partition: str) -> None:
        """Delete lineage first, then all current, retained and legacy files."""
        self._retry_cleanup()
        legacy = self._legacy_path(dataset, partition)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        with _partition_lock(legacy):
            self.registry.drop_partition(dataset, partition)
            directory = self._version_dir(dataset, partition)
            owned = {str(legacy)}
            if directory.exists():
                owned.update(str(path) for path in directory.glob("*.parquet"))
                for candidate in directory.iterdir():
                    if candidate.is_file() or candidate.is_symlink():
                        candidate.unlink(missing_ok=True)
                try:
                    directory.rmdir()
                except OSError:
                    pass
            legacy.unlink(missing_ok=True)
            legacy.with_suffix(".meta.json").unlink(missing_ok=True)
            for key in list(self._validated):
                if key[0] in owned:
                    self._validated.pop(key, None)

    def fingerprint(self, datasets: list[str] | None = None) -> dict[str, list[dict[str, str]]]:
        """Snapshot ids per dataset, attached to every trading decision."""
        names = datasets or sorted({r.dataset for r in self.registry.list_manifest()})
        out: dict[str, list[dict[str, str]]] = {}
        for name in names:
            recs = self.registry.list_manifest(name)
            if recs:
                out[name] = [
                    {"partition": r.partition, "snapshot_id": r.snapshot_id, "as_of": r.as_of}
                    for r in recs
                ]
        return out
