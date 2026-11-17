"""Write transactions in ``PaperStore``: when they start, and what a loser sees.

The API, the 5-minute monitor and a manual backfill are three processes on one
SQLite file. Python's default is a deferred transaction: it reads first and only
takes the write lock at the first INSERT. Between those two moments another
process can commit, and the upgrade then fails with SQLITE_BUSY_SNAPSHOT — which
``busy_timeout`` cannot retry, because waiting does not make a stale read
snapshot valid again. The fix is to take the write lock before the first SELECT.

These tests pin the three things that make that real:

* a write connection is already in a transaction when it is handed out
  (deferred would report ``in_transaction`` False until the first DML);
* a writer that cannot get the lock raises ``StoreBusy`` — a ``StateConflict``,
  which the API answers 409 with "retry", not a 500;
* a reader is not blocked by an open write transaction (WAL), so narrowing the
  lock only serialises writers.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from service.trading.state import PaperStore, StateConflict, StoreBusy


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "paper.sqlite"
    PaperStore(path)          # creates the schema
    return path


def _raw_ticket(conn, ticket_id: str) -> None:
    """Insert straight into the table so the caller can hold the transaction."""
    conn.execute(
        "INSERT INTO tickets(ticket_id,created_at,status,symbol,side,sleeve,payload)"
        " VALUES (?,?,?,?,?,?,?)",
        (ticket_id, "2026-11-04T09:00:00+08:00", "proposed", "600000.SH",
         "BUY", "CORE", json.dumps({"ticket_id": ticket_id})))


def _ticket(ticket_id: str) -> dict:
    return {"ticket_id": ticket_id, "symbol": "600000.SH", "side": "BUY",
            "qty": 100, "status": "proposed", "created_at": "2026-11-04T09:26:00+08:00"}


def test_a_stale_writer_cannot_replace_a_terminal_ticket(db):
    """The first terminal answer wins, even when the loser read ``proposed``."""
    store = PaperStore(db)
    proposed = {**_ticket("t-race"), "sleeve": "CORE"}
    store.save_ticket(proposed)
    store.save_ticket({**proposed, "status": "filled",
                       "fill": {"qty": 100, "price": "10.00"}})

    answer = store.save_ticket({**proposed, "status": "rejected",
                                "reasons": ["stale guard"]})

    assert store.get_ticket("t-race")["status"] == "filled"
    assert answer["status"] == "filled"
    assert store.get_ticket("t-race")["fill"] == {"qty": 100, "price": "10.00"}

    # Same-state metadata maintenance is legal; the first fill is still immutable.
    answer = store.save_ticket({**answer, "stop_price": "9.50",
                                "fill": {"qty": 0, "price": "0"}})
    assert answer["stop_price"] == "9.50"
    assert answer["fill"] == {"qty": 100, "price": "10.00"}


def test_a_write_connection_takes_the_lock_before_the_first_statement(db):
    store = PaperStore(db)
    with store._conn(write=True) as conn:
        # Deferred transactions start empty; IMMEDIATE has already begun. This
        # is the whole fix, so assert it directly rather than through a timing
        # test that would only fail on some machines.
        assert conn.in_transaction is True
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_read_connection_does_not_open_a_transaction(db):
    store = PaperStore(db)
    with store._conn() as conn:
        assert conn.in_transaction is False
        assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_a_blocked_writer_raises_store_busy_not_a_raw_sqlite_error(db, monkeypatch):
    """The loser of a cross-process race must get a retryable answer.

    Two ``PaperStore`` instances over one file stand in for two processes: they
    share no lock, which is exactly the situation ``_lock`` cannot cover.
    """
    holder, loser = PaperStore(db), PaperStore(db)
    monkeypatch.setattr(PaperStore, "BUSY_TIMEOUT", 0.2, raising=False)

    with holder._conn(write=True) as conn:
        _raw_ticket(conn, "t-holder")
        with pytest.raises(StoreBusy) as caught:
            loser.save_ticket(_ticket("t-loser"))
    # StoreBusy is a StateConflict, so every existing `except StateConflict`
    # in the book retries it without knowing this type exists.
    assert isinstance(caught.value, StateConflict)
    assert "busy" in str(caught.value)
    # And the loser really wrote nothing.
    assert PaperStore(db).get_ticket("t-loser") is None


def test_a_reader_is_not_blocked_by_an_open_write_transaction(db):
    holder, reader = PaperStore(db), PaperStore(db)
    with holder._conn(write=True) as conn:
        _raw_ticket(conn, "t-open")
        # WAL: this must answer, not wait for the holder to commit. A reader
        # sees the pre-commit state, which is why the fix only narrows writers.
        assert reader.list_tickets() == []
    assert [t["ticket_id"] for t in reader.list_tickets()] == ["t-open"]


def test_a_busy_database_is_not_mistaken_for_a_broken_one(db, monkeypatch):
    """Only locked/busy become StoreBusy; a real defect must stay a defect."""
    store = PaperStore(db)
    monkeypatch.setattr(PaperStore, "BUSY_TIMEOUT", 0.2, raising=False)
    with (pytest.raises(sqlite3.OperationalError) as caught,
          store._conn(write=True) as conn):
        conn.execute("SELECT * FROM no_such_table")
    assert not isinstance(caught.value, StoreBusy)
    assert "no such table" in str(caught.value)


#: SQL that mutates. Deliberately not "CREATE": the schema is built once in
#: ``__init__`` through ``executescript``, and matching the word would flag a
#: read method whose docstring explains a table's history.
_MUTATING = ("INSERT INTO", "DELETE FROM", "ALTER TABLE", "executescript(")


def _store_methods() -> dict[str, str]:
    import re
    from itertools import pairwise
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "service" / "trading" / "state.py"
    lines = source.read_text(encoding="utf-8").splitlines()
    heads = [(i, m.group(1)) for i, line in enumerate(lines)
             for m in [re.match(r"    def (\w+)\(", line)] if m]
    heads.append((len(lines), "<eof>"))
    return {name: "\n".join(lines[i:j]) for (i, name), (j, _) in pairwise(heads)}


def test_every_mutating_store_method_takes_the_write_lock():
    """The rule, checked structurally — not one call site at a time.

    Converting the 20-odd write paths by hand is how one of them gets missed,
    and a missed one is invisible until two processes collide in production. A
    new write method that opens ``_conn()`` (deferred) fails here instead.
    """
    import re

    methods = _store_methods()
    # ``__init__`` is exempt on purpose: ``executescript`` commits a pending
    # transaction, so an IMMEDIATE there is released by the first statement and
    # would only look like protection. It is the one write path whose failure
    # mode self-heals — the column checks re-run on the next start.
    exempt = {"__init__"}
    mutating = {
        name: body for name, body in methods.items() if name not in exempt
        and (any(token in body for token in _MUTATING)
             or re.search(r"UPDATE\s+\w+\s+SET", body)
             or re.search(r"self\._\w+_conn\(conn", body))
    }
    # A parse that silently matched nothing would make this test a no-op.
    assert len(mutating) >= 20, f"only {len(mutating)} mutating methods found"

    offenders = sorted(
        name for name, body in mutating.items()
        if "self._conn(" in body and "self._conn(write=True)" not in body
    )
    assert offenders == [], f"these write without BEGIN IMMEDIATE: {offenders}"


def test_the_busy_answer_is_a_409_that_says_retry_not_reloaded():
    from service.api.app import app

    handlers = app.exception_handlers
    # Starlette resolves by MRO, so the specific handler has to exist for
    # StoreBusy or the generic "the ledger was reloaded" text would be used.
    assert StoreBusy in handlers and StateConflict in handlers
    response = handlers[StoreBusy](None, StoreBusy("state database busy for 30s"))
    body = json.loads(response.body)
    assert response.status_code == 409
    assert body["code"] == "STORE_BUSY"
    assert "没有产生任何写入" in body["next"]
    assert "重载" not in body["next"]
    assert response.headers["retry-after"] == "2"
