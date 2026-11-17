"""The trial log is append-only history; a schema change may never rebuild it.

`research.sqlite` holds every look the desk has taken at the panel, and the
Deflated Sharpe divides by that count. Dropping the table to add a column would
reset the denominator and make every future claim look better than it is — the
exact failure the table exists to prevent. So the migration is an in-place
`ALTER TABLE`, exercised here against a table built to the old shape.
"""

from __future__ import annotations

import sqlite3

from service.research.registry import TrialRegistry

OLD_DDL = """
CREATE TABLE trials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    family TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    pool TEXT,
    n_variants INTEGER NOT NULL DEFAULT 0,
    n_looks INTEGER NOT NULL DEFAULT 1,
    n_trades INTEGER,
    p_value REAL,
    effect REAL,
    verdict TEXT,
    window_start TEXT,
    window_end TEXT,
    source TEXT NOT NULL DEFAULT 'run'
);
"""


def _old_registry(path):
    con = sqlite3.connect(path)
    con.executescript(OLD_DDL)
    con.execute("INSERT INTO trials(created_at, family, spec_hash, spec_json, n_trades)"
                " VALUES ('2026-10-26T10:00:00+00:00','ashare_daily','deadbeef','{}',915)")
    con.commit()
    con.close()


def test_an_old_log_gains_the_column_without_losing_a_row(tmp_path):
    db = tmp_path / "research.sqlite"
    _old_registry(db)

    TrialRegistry(db)   # opening it is the migration

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    columns = {r["name"] for r in con.execute("PRAGMA table_info(trials)")}
    rows = con.execute("SELECT spec_hash, n_trades, data_fingerprint, "
                       "code_fingerprint, config_fingerprint, data_inputs FROM trials").fetchall()
    con.close()

    assert {"data_fingerprint", "code_fingerprint", "config_fingerprint", "data_inputs"} <= columns
    assert len(rows) == 1                       # history intact
    assert rows[0]["n_trades"] == 915
    assert rows[0]["data_fingerprint"] is None  # unknown, not invented
    assert rows[0]["code_fingerprint"] is None
    assert rows[0]["config_fingerprint"] is None
    assert rows[0]["data_inputs"] is None


def test_opening_twice_is_idempotent(tmp_path):
    db = tmp_path / "research.sqlite"
    _old_registry(db)

    TrialRegistry(db)
    TrialRegistry(db)

    con = sqlite3.connect(db)
    columns = [r[1] for r in con.execute("PRAGMA table_info(trials)")]
    con.close()
    assert columns.count("data_fingerprint") == 1


def test_a_row_without_a_fingerprint_is_not_counted_as_an_identity(tmp_path):
    """Old rows are "unknown provenance", not "one more panel"."""
    db = tmp_path / "research.sqlite"
    _old_registry(db)
    registry = TrialRegistry(db)

    assert registry.fingerprint_history("deadbeef") == []


def test_two_panels_for_one_spec_are_visible_side_by_side(tmp_path):
    db = tmp_path / "research.sqlite"
    registry = TrialRegistry(db)
    base = {"spec_hash_value": "abc", "spec_json": {}, "pool": "broken_board",
            "window": ("20230101", "20261023"), "family": "ashare_daily"}
    registry.record_look(**base, n_trades=915, effect=-0.0096, data_fingerprint="old")
    registry.record_look(**base, n_trades=914, effect=-0.0150, data_fingerprint="new")

    history = registry.fingerprint_history("abc")

    assert [h["fingerprint"] for h in history] == ["new", "old"]
    assert history[0]["trades_min"] == history[0]["trades_max"] == 914
