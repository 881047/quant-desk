"""SQLite registry: dataset manifest, ingest ledger, quality reports.

The registry is the single source of truth for:
- which partition exists, with what checksum and snapshot id (lineage)
- how far a backfill got (resume cursor)
- the latest quality verdict per partition (quarantine gate)

Everything is written with explicit transactions; readers open short-lived
connections so the API and CLI can run concurrently (WAL mode).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_manifest (
    dataset       TEXT NOT NULL,
    partition     TEXT NOT NULL,
    source        TEXT NOT NULL,
    trust_tier    TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    rows          INTEGER NOT NULL,
    snapshot_id   TEXT NOT NULL,
    checksum      TEXT NOT NULL,
    schema_hash   TEXT NOT NULL,
    cover_start   TEXT,
    cover_end     TEXT,
    status        TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL,
    -- Rows this write *supplied*, not the size of the result. A merge that adds
    -- one session shows 1 here and 28,273 in `rows`; a full recompute shows the
    -- same number in both. Without this, "the partition was rewritten" and "the
    -- partition gained one row" are indistinguishable after the fact, and the
    -- 2026-10-29 verdict drift could not be attributed either way.
    --
    -- NULL means "written before this column existed". 0 is not a valid value —
    -- the writer returns early on an empty frame — so the two cannot be
    -- confused, and a default of 0 would have made every historical row claim a
    -- write that never happened.
    input_rows    INTEGER,
    PRIMARY KEY (dataset, partition)
);

CREATE TABLE IF NOT EXISTS ingest_ledger (
    job         TEXT NOT NULL,
    dataset     TEXT NOT NULL,
    partition   TEXT NOT NULL,
    status      TEXT NOT NULL,
    cursor      TEXT,
    rows        INTEGER,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (job, dataset, partition)
);

CREATE TABLE IF NOT EXISTS quality_report (
    dataset     TEXT NOT NULL,
    partition   TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    rows        INTEGER NOT NULL,
    issues_json TEXT NOT NULL,
    PRIMARY KEY (dataset, partition)
);

CREATE INDEX IF NOT EXISTS idx_manifest_dataset ON dataset_manifest(dataset);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON ingest_ledger(job, status);
"""


@dataclass(frozen=True)
class ManifestRecord:
    dataset: str
    partition: str
    source: str
    trust_tier: str
    as_of: str
    ingested_at: str
    rows: int
    snapshot_id: str
    checksum: str
    schema_hash: str
    cover_start: str | None
    cover_end: str | None
    status: str
    path: str
    note: str = ""
    input_rows: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "partition": self.partition,
            "source": self.source,
            "trust_tier": self.trust_tier,
            "as_of": self.as_of,
            "ingested_at": self.ingested_at,
            "rows": self.rows,
            "snapshot_id": self.snapshot_id,
            "checksum": self.checksum,
            "schema_hash": self.schema_hash,
            "cover_start": self.cover_start,
            "cover_end": self.cover_end,
            "status": self.status,
            "input_rows": self.input_rows,
            "note": self.note,
            "path": self.path,
        }


# Columns added after a table shipped, keyed by table.
_LATE_COLUMNS = {"dataset_manifest": {"input_rows": "INTEGER"}}


class Registry:
    """Thin, dependency-free wrapper around one SQLite file.

    ``fallback_to`` exists for readers that work off a **copy** of this file.
    A copy is a snapshot: while a writer is publishing (the intraday collector
    does it every 40 seconds for six hours), a copied row keeps naming a version
    the writer has already pruned, and every read of that partition raises
    "current manifest has no readable parquet/metadata" — measured 2026-11-04
    09:41, four tests failed on exactly that. When a row's file is gone, the
    reader asks the live registry instead of declaring the store corrupt. The
    distinction matters: a live registry with a dangling pointer *is* corruption,
    a copied one is merely old.
    """

    def __init__(self, db_path: str | Path, *, fallback_to: str | Path | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # 不再往下递归：fallback 自己不带 fallback。
        self._fallback = Registry(fallback_to) if fallback_to is not None else None
        with self._conn() as conn:
            conn.executescript(DDL)
            self._migrate(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns that shipped after the table did.

        SQLite has no `ADD COLUMN IF NOT EXISTS`, so presence is checked first;
        an existing registry keeps its rows instead of demanding a rebuild that
        would drop the manifest history.
        """
        for table, columns in _LATE_COLUMNS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- manifest
    def upsert_manifest(self, rec: ManifestRecord) -> None:
        """Record one version as the live one — but never move the manifest back.

        The manifest is a pointer to a file on disk. Two writers on the same
        partition (the 10:30 snapshot job, the 15:20 close job and the 17:30
        daily job all write ``limit_up_pool``) each commit their own record, and
        the **last commit wins** — not the newest write. A slow writer that
        started at 10:30 but committed at 17:35 therefore dragged the pointer
        back to its own 10:30 version, which the faster writers' prune had
        already deleted. From then on every read of that partition raised
        "current manifest has no readable parquet/metadata" — measured
        2026-11-02 on ``limit_up_pool[20261102]`` and
        ``limit_down_pool[20261102]``, which made the pool endpoints fail while
        the service was serving them.

        So the update is conditional: a record may only take the pointer if it
        is not older than the one already there. ``ingested_at`` is stamped when
        the record is built — that is, when the write *starts* — so it orders
        the writers by when they began, which is the order we want: the write
        that started last is the one whose content is freshest.
        """
        payload = rec.as_dict()
        cols = ", ".join(payload)
        placeholders = ", ".join(f":{k}" for k in payload)
        updates = ", ".join(f"{k}=excluded.{k}" for k in payload if k not in {"dataset", "partition"})
        with self._lock, self._conn() as conn:
            conn.execute(
                f"INSERT INTO dataset_manifest ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(dataset, partition) DO UPDATE SET {updates} "
                f"WHERE excluded.ingested_at >= dataset_manifest.ingested_at",
                payload,
            )

    def get_manifest(self, dataset: str, partition: str) -> ManifestRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dataset_manifest WHERE dataset=? AND partition=?",
                (dataset, partition),
            ).fetchone()
        if row is not None and self._fallback and not Path(row["path"]).exists():
            # 这份清单是拷贝：行旧了不代表库坏了，问当前的那一份。
            live = self._fallback.get_manifest(dataset, partition)
            if live is not None:
                return live
        return self._to_manifest(row) if row else None

    def list_manifest(
        self, dataset: str | None = None, status: tuple[str, ...] | None = None
    ) -> list[ManifestRecord]:
        sql = "SELECT * FROM dataset_manifest"
        clauses: list[str] = []
        params: list[Any] = []
        if dataset:
            clauses.append("dataset=?")
            params.append(dataset)
        if status:
            clauses.append(f"status IN ({','.join('?' for _ in status)})")
            params.extend(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY dataset, partition"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._to_manifest(r) for r in rows]

    def set_manifest_status(self, dataset: str, partition: str, status: str, note: str = "") -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE dataset_manifest SET status=?, note=? WHERE dataset=? AND partition=?",
                (status, note, dataset, partition),
            )

    def drop_partition(self, dataset: str, partition: str) -> None:
        """Remove a partition from lineage. Used when a bad ingest must be undone."""
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM dataset_manifest WHERE dataset=? AND partition=?",
                (dataset, partition),
            )
            conn.execute(
                "DELETE FROM quality_report WHERE dataset=? AND partition=?",
                (dataset, partition),
            )
            conn.execute(
                "DELETE FROM ingest_ledger WHERE dataset=? AND partition=?",
                (dataset, partition),
            )

    # ------------------------------------------------------------ ingest ledger
    def mark_ingest(
        self,
        job: str,
        dataset: str,
        partition: str,
        status: str,
        *,
        cursor: str | None = None,
        rows: int | None = None,
        error: str | None = None,
    ) -> None:
        from datetime import datetime

        now = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO ingest_ledger(job, dataset, partition, status, cursor, rows, attempts, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(job, dataset, partition) DO UPDATE SET
                    status=excluded.status,
                    cursor=COALESCE(excluded.cursor, ingest_ledger.cursor),
                    rows=COALESCE(excluded.rows, ingest_ledger.rows),
                    attempts=ingest_ledger.attempts + 1,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (job, dataset, partition, status, cursor, rows, error, now),
            )

    def ingest_state(self, job: str, dataset: str, partition: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM ingest_ledger WHERE job=? AND dataset=? AND partition=?",
                (job, dataset, partition),
            ).fetchone()
        return dict(row) if row else None

    def list_ingest(self, job: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if job:
                rows = conn.execute(
                    "SELECT * FROM ingest_ledger WHERE job=? ORDER BY dataset, partition", (job,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM ingest_ledger ORDER BY updated_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ quality
    def record_quality(
        self, dataset: str, partition: str, status: str, rows: int, issues: list[dict[str, Any]]
    ) -> None:
        from datetime import datetime

        now = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO quality_report(dataset, partition, checked_at, status, rows, issues_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset, partition) DO UPDATE SET
                    checked_at=excluded.checked_at,
                    status=excluded.status,
                    rows=excluded.rows,
                    issues_json=excluded.issues_json
                """,
                (dataset, partition, now, status, rows, json.dumps(issues, ensure_ascii=False)),
            )
            # The report and the read gate are two views of one verdict. Updating
            # them separately lets a crash leave a quarantined report beside a
            # still-readable manifest (or the reverse).
            note = ",".join(str(issue.get("code", "")) for issue in issues).strip(",")[:200]
            conn.execute(
                "UPDATE dataset_manifest SET status=?, note=? WHERE dataset=? AND partition=?",
                (status, note, dataset, partition),
            )

    def latest_quality(self, dataset: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM quality_report"
        params: list[Any] = []
        if dataset:
            sql += " WHERE dataset=?"
            params.append(dataset)
        sql += " ORDER BY dataset, partition"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["issues"] = json.loads(item.pop("issues_json"))
            out.append(item)
        return out

    # ------------------------------------------------------------ summary
    def summary(self) -> list[dict[str, Any]]:
        """Per-dataset current coverage; old quarantines stay in the manifest only."""
        sql = """
        SELECT dataset,
               COUNT(*)                        AS partitions,
               SUM(CASE WHEN status IN ('ok', 'warn') THEN rows ELSE 0 END) AS rows,
               MIN(CASE WHEN status IN ('ok', 'warn') THEN cover_start END) AS cover_start,
               MAX(CASE WHEN status IN ('ok', 'warn') THEN cover_end END) AS cover_end,
               MAX(CASE WHEN status IN ('ok', 'warn') THEN ingested_at END) AS last_ingested,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END)         AS ok_partitions,
               SUM(CASE WHEN status='warn' THEN 1 ELSE 0 END)       AS warn_partitions,
               SUM(CASE WHEN status='quarantine' AND (
                             cover_end IS NULL OR cover_end >= COALESCE((
                                 SELECT MAX(mq.cover_end) FROM dataset_manifest mq
                                 WHERE mq.dataset=m.dataset AND mq.status IN ('ok', 'warn')
                             ), '')
                        ) THEN 1 ELSE 0 END)                       AS quarantine_partitions,
               MAX(CASE WHEN status IN ('ok', 'warn') THEN trust_tier END) AS trust_tier,
               MAX(CASE WHEN status IN ('ok', 'warn') THEN source END) AS source,
               (SELECT m2.snapshot_id FROM dataset_manifest m2
                 WHERE m2.dataset = m.dataset AND m2.status IN ('ok', 'warn')
                 ORDER BY COALESCE(m2.cover_end, '') DESC, m2.ingested_at DESC
                 LIMIT 1)                      AS last_snapshot_id,
               -- Issue-level, because partition status is not enough: every
               -- `daily` partition used to read `warn`, so "status != ok" was a
               -- constant and carried no information. Counting `severity=error`
               -- rows gives the reader a number that can actually move.
               --
               -- 这个数是**问题条数**，不是坏行数、也不是分区数：一个分区里同一条
               -- 规则命中一万行也只算 1。前端叫它"错误"时读者会当成"一万行坏了"
               -- 或者"一万个分区"，所以顺带把规则名带出来——数字旁边有名字，才
               -- 能自己去查是哪条规则、影响多大（issue.count 在 quality_report 里）。
               COALESCE((SELECT COUNT(*) FROM quality_report qr,
                                              json_each(qr.issues_json) je
                          WHERE qr.dataset = m.dataset
                            AND json_extract(je.value, '$.severity') = 'error'), 0)
                                              AS error_issues,
               COALESCE((SELECT group_concat(DISTINCT json_extract(je.value, '$.code'))
                           FROM quality_report qr, json_each(qr.issues_json) je
                          WHERE qr.dataset = m.dataset
                            AND json_extract(je.value, '$.severity') = 'error'), '')
                                              AS error_codes,
               COALESCE((SELECT group_concat(DISTINCT json_extract(je.value, '$.code'))
                           FROM quality_report qr, json_each(qr.issues_json) je
                          WHERE qr.dataset = m.dataset
                            AND json_extract(je.value, '$.severity') = 'warn'), '')
                                              AS warn_codes
        FROM dataset_manifest m
        GROUP BY dataset
        ORDER BY dataset
        """
        with self._conn() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _to_manifest(row: sqlite3.Row) -> ManifestRecord:
        d = dict(row)
        return ManifestRecord(**d)
