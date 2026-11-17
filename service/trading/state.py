"""Persistent state for the paper trading desk.

One SQLite file holds everything a decision audit needs:

- ``paper_state``  account snapshot + day/peak/kill-switch (single row)
- ``tickets``      proposals and their computed execution parameters
- ``orders``       what was actually sent to the kernel
- ``decisions``    append-only log (propose / approve / reject / risk veto)
- ``journal``      the agent's own notes, searchable

The store never decides anything; it only remembers. All rules live in
``guard.py`` and ``decisions.py``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from service import vocabulary as vocab
from service.kernel.clock import SHANGHAI

DDL = """
CREATE TABLE IF NOT EXISTS paper_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    sleeve TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

-- Execution is committed once per decision ticket.  The JSON account snapshot
-- remains the read model, but this table is the database-enforced idempotency
-- boundary: a retry cannot book the same ticket twice, even from two processes.
CREATE TABLE IF NOT EXISTS proposal_claims (
    fingerprint TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
-- The caller's own idempotency handle: a tool times out at 60 s but the service
-- has already committed, so the retry must be able to say "this is the same
-- request" without depending on the model wording its thesis the same way
-- twice. Kept after the ticket reaches a terminal state — it is a promise about
-- the *request* ("same id, same answer"), not pending-intent deduplication.
CREATE TABLE IF NOT EXISTS proposal_requests (
    client_request_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_fills (
    ticket_id TEXT PRIMARY KEY,
    fill_id TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

-- A durable counterpart of state.pending.  Besides making queue recovery
-- explicit, the primary key prevents duplicate pending rows for one ticket.
CREATE TABLE IF NOT EXISTS pending_tickets (
    ticket_id TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    ticket_id TEXT,
    actor TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    text TEXT NOT NULL,
    tags TEXT,
    snapshot TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    opened_at TEXT,
    closed_at TEXT NOT NULL,
    qty INTEGER NOT NULL,
    entry REAL NOT NULL,
    exit REAL NOT NULL,
    pnl REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    thesis TEXT,
    invalidation TEXT,
    ticket_ids TEXT,
    note TEXT,
    strategy TEXT DEFAULT 'llm',
    persona TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_symbol ON cases(symbol);

-- What the desk looked at and did not buy. The fourth scoreable judgement.
--
-- Proposals are settled by arena (excess vs the equal-weight market), persona
-- switches by the scorecard, hypotheses by the referee. "I looked and decided
-- not to act" had nowhere to land except prose, so restraint was never scored:
-- 2026-11-01..03 produced zero trades and zero records, which is three days of
-- judgement with no evidence about whether it was any good.
--
-- Prices are deliberately absent from the write path. The caller names symbols
-- and a horizon; the settlement reads the sessions and the closes itself, so a
-- declined record cannot be scored against a price the caller chose.
CREATE TABLE IF NOT EXISTS declined (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    symbols TEXT NOT NULL,
    reason TEXT NOT NULL,
    invalidation TEXT NOT NULL DEFAULT '',
    horizon_days INTEGER NOT NULL,
    settled_at TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_declined_day ON declined(day);

-- Which decision style is running, and every change of mind about it.
--
-- A persona is not a label on a chart: it decides which sleeve may be spent,
-- which horizon applies and which data is a prerequisite. Keeping the current
-- row separate from the switch log means "what am I now" is one lookup and
-- "why did I become it" keeps its full history, including the expectation that
-- was written down *before* the outcome was known.
CREATE TABLE IF NOT EXISTS persona_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current TEXT NOT NULL,
    since TEXT NOT NULL,
    day TEXT NOT NULL,
    switches_today INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_switch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    from_key TEXT,
    to_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence TEXT,
    expected_edge_bps REAL,
    invalidation TEXT,
    horizon_days INTEGER,
    regime TEXT,
    readiness TEXT,
    nav_at_switch REAL,
    actor TEXT
);
CREATE INDEX IF NOT EXISTS idx_persona_switch_day ON persona_switch(day);

CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    statement TEXT NOT NULL,
    rationale TEXT,
    test_spec TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    result TEXT
);

-- Which parts of the desk the agent actually uses. Without this, "the model is
-- under-utilised" is an opinion; with it, it is a number.
--
-- `calls` alone was misleading: on 2026-10-26 it read 241 proposals while the
-- book held zero tickets, because every retry of a rejected request counted the
-- same as a decision. `ok_calls` / `fail_calls` split that out by status code.
-- Rows written before the split leave `calls - ok_calls - fail_calls > 0`, which
-- `usage_summary` reports as `unclassified` rather than guessing a verdict.
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    method TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    ok_calls INTEGER NOT NULL DEFAULT 0,
    fail_calls INTEGER NOT NULL DEFAULT 0,
    last_ts TEXT,
    PRIMARY KEY (day, endpoint, method)
);

-- Core sleeve: a slow, weight-driven ETF book that runs every trading day.
CREATE TABLE IF NOT EXISTS core_nav (
    trade_date TEXT PRIMARY KEY,
    nav TEXT NOT NULL,
    cash TEXT NOT NULL,
    invested TEXT NOT NULL,
    weights TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS core_trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_core_trade_date ON core_trade(trade_date);

-- Memos are the model's own reasoning record: claims with probabilities and a
-- falsifier, scored later so "deep thinking" is measurable rather than claimed.
CREATE TABLE IF NOT EXISTS memo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    topic TEXT NOT NULL,
    thesis TEXT NOT NULL,
    mechanism TEXT,
    falsifier TEXT,
    confidence REAL,
    predictions TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    score TEXT
);

"""

FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(
    text, tags, symbol,
    content='journal', content_rowid='id', tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS journal_ai AFTER INSERT ON journal BEGIN
  INSERT INTO journal_fts(rowid, text, tags, symbol)
  VALUES (new.id, new.text, new.tags, new.symbol);
END;

CREATE TRIGGER IF NOT EXISTS journal_ad AFTER DELETE ON journal BEGIN
  INSERT INTO journal_fts(journal_fts, rowid, text, tags, symbol)
  VALUES ('delete', old.id, old.text, old.tags, old.symbol);
END;

CREATE TRIGGER IF NOT EXISTS journal_au AFTER UPDATE ON journal BEGIN
  INSERT INTO journal_fts(journal_fts, rowid, text, tags, symbol)
  VALUES ('delete', old.id, old.text, old.tags, old.symbol);
  INSERT INTO journal_fts(rowid, text, tags, symbol)
  VALUES (new.id, new.text, new.tags, new.symbol);
END;
"""

#: Every status a ticket can hold. The API's ``status`` filter is typed with the
#: same list (``service/api/trading.py``); a test keeps the two in step, because a
#: typo in a status filter returns an empty list, and "no tickets" is
#: indistinguishable from "you spelled it wrong" — which is the whole reason the
#: enumeration tool exists.
TICKET_STATUSES = ("proposed", "armed", "queued", "filled", "rejected",
                   "cancelled", "expired")

#: The four statuses a ticket can never leave. A ticket that is none of these is
#: still somebody's outstanding intent, which is why "open" is a thing callers
#: ask for — see ``list_tickets``.
TERMINAL_TICKET_STATUSES = frozenset({"filled", "rejected", "cancelled", "expired"})

#: Pseudo-status for "not terminal yet". Kept distinct from any real status so a
#: future ``open`` state cannot silently change the meaning of a saved query.
OPEN_TICKET_STATUS = "open"


def utc_now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def journal_day(value: str) -> str:
    """``20261103`` / ``2026-11-03`` → ``2026-11-03``. Anything else is refused.

    Silently ignoring an unparseable date is the worse failure: the caller gets
    an answer to a different question and has no way to tell. A date that cannot
    be read is a 400, not an empty result.
    """
    digits = str(value).strip().replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"日期要写成 YYYYMMDD 或 YYYY-MM-DD，收到 {value!r}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


def _journal_filter(*, kind: str | None = None, since: str | None = None,
                    until: str | None = None,
                    row_id: int | None = None) -> tuple[str, list]:
    """SQL fragment starting with ``AND``, plus its parameters.

    Returns ``(clause, params)`` so every query above can append it to whatever
    its own WHERE already constrains. Returning a bare condition list and letting
    each caller join it is how the first draft of this ended up emitting two
    ``WHERE`` keywords in the FTS branch.
    """
    parts: list[str] = []
    params: list = []
    if row_id is not None:
        parts.append("id = ?")
        params.append(int(row_id))
    if kind:
        parts.append("kind = ?")
        params.append(str(kind).strip())
    if since:
        parts.append("substr(ts, 1, 10) >= ?")
        params.append(journal_day(since))
    if until:
        parts.append("substr(ts, 1, 10) <= ?")
        params.append(journal_day(until))
    return ("".join(f" AND {p}" for p in parts), params)


class StateConflict(RuntimeError):
    """The account changed after the caller loaded it; replay from the new snapshot."""


class StoreBusy(StateConflict):
    """Another process held the write lock past ``PaperStore.BUSY_TIMEOUT``.

    A subclass, because every existing ``except StateConflict`` in the book is
    already right about what to do — retry from a fresh snapshot. It exists as
    its own type so the API's 409 can say "nothing was written, retry" instead of
    claiming the ledger was reloaded, which is what ``STATE_CONFLICT`` says.
    """


class ExecutionExists(RuntimeError):
    """Another process committed this ticket first."""


class PaperStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.fts = False
        # Deliberately *not* ``write=True``: ``executescript`` commits any open
        # transaction before running, so a BEGIN IMMEDIATE here would be released
        # by the first statement and would only look like protection. The two
        # schema scripts below do the same thing again. The DDL is
        # ``IF NOT EXISTS`` and the column checks re-run on every start, so a
        # process that loses a migration race fixes it on the next boot — which
        # is not true of any other write path in this class, and is exactly why
        # those do take the write lock.
        with self._conn() as conn:
            # WAL is a property of the *file*, not of a connection: set it once,
            # on the connection that also runs the DDL. Re-issuing it per request
            # (this store opens a connection per call) was a write-shaped pragma
            # on the read path and one more thing to contend with under load.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(DDL)
            state_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_state)")}
            if "revision" not in state_cols:
                conn.execute(
                    "ALTER TABLE paper_state ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            self._migrate_json_rows(conn)
            # Attribution needs to know which strategy opened a trade. SQLite has
            # no ADD COLUMN IF NOT EXISTS, so check first.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)")}
            if "strategy" not in cols:
                conn.execute("ALTER TABLE cases ADD COLUMN strategy TEXT DEFAULT 'llm'")
            # Persona attribution arrived in P26. Rows written before it keep
            # NULL on purpose: back-filling them with the current persona would
            # invent a label the decision never carried.
            if "persona" not in cols:
                conn.execute("ALTER TABLE cases ADD COLUMN persona TEXT")
            # Same reason: older usage tables only counted calls, and a rejected
            # request counted the same as an accepted one.
            usage_cols = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
            for column in ("ok_calls", "fail_calls"):
                if column not in usage_cols:
                    conn.execute(
                        f"ALTER TABLE usage ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            try:
                conn.executescript(FTS_DDL)
                conn.execute("INSERT INTO journal_fts(journal_fts) VALUES('rebuild')")
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False  # SQLite without FTS5: LIKE fallback below

    @staticmethod
    def _migrate_json_rows(conn: sqlite3.Connection) -> None:
        """Index legacy JSON fills/queue entries without changing their contents."""
        row = conn.execute("SELECT payload FROM paper_state WHERE id=1").fetchone()
        if row is None:
            return
        payload = json.loads(row["payload"])
        now = utc_now()
        for fill in payload.get("fills", []):
            ticket_id = fill.get("ticket_id")
            fill_id = fill.get("fill_id")
            if not ticket_id or not fill_id:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO paper_fills
                   (ticket_id, fill_id, order_id, created_at, payload)
                   VALUES (?,?,?,?,?)""",
                (ticket_id, fill_id, fill.get("order_id", ""),
                 fill.get("ts") or now, json.dumps(fill, ensure_ascii=False)),
            )
        conn.execute("DELETE FROM pending_tickets")
        for entry in payload.get("pending", []):
            ticket_id = entry.get("ticket_id")
            if not ticket_id:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO pending_tickets(ticket_id, updated_at, payload)
                   VALUES (?,?,?)""",
                (ticket_id, now, json.dumps(entry, ensure_ascii=False)),
            )

    #: How long a writer waits for the other process's lock before giving up.
    #: Handed to ``sqlite3.connect``; also quoted in the error text so a reader
    #: can tell "waited 30 s and lost" from "the file is corrupt".
    BUSY_TIMEOUT = 30.0

    @contextmanager
    def _conn(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=self.BUSY_TIMEOUT)
        conn.row_factory = sqlite3.Row
        try:
            # ``synchronous`` is per connection and belongs here. The journal
            # mode is *not*: it is persisted in the file and is set once by
            # ``__init__``. Issuing ``journal_mode=WAL`` on every connection made
            # every read look like a write to SQLite's locking layer.
            conn.execute("PRAGMA synchronous=NORMAL")
            # Not because this schema has foreign keys — it has none — but so a
            # connection behaves like ``adapters/sqlite_store``'s, and so that
            # adding one later cannot silently go unenforced on this path.
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                # Take the write lock *before* the first SELECT. Python's default
                # is a deferred transaction: it reads first and only upgrades on
                # the first write. Between those two moments another process can
                # commit, and the upgrade then fails with SQLITE_BUSY_SNAPSHOT —
                # which busy_timeout does **not** retry, because no amount of
                # waiting makes the stale snapshot valid again. Every
                # SELECT-then-INSERT in this class (claim a proposal, commit an
                # execution, settle a declined name) is that shape, and the API
                # and the 5-minute monitor are two processes.
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException as exc:
            # Explicit, not implicit. ``close()`` on an uncommitted connection
            # discards the transaction too, but a reader of this function should
            # not have to know that: every multi-statement write in this class
            # (fill + account + queue + audit) is atomic because of this line.
            # A rollback that itself fails must not hide the original error.
            with suppress(sqlite3.Error):
                conn.rollback()
            # "database is locked" after 30 s is contention, not a defect: the
            # request wrote nothing, so retrying the same request is correct.
            # Surfaced as StoreBusy (a StateConflict) so the route answers 409
            # "retry" rather than 500 "the server is broken".
            if isinstance(exc, sqlite3.OperationalError):
                text = str(exc).lower()
                if "locked" in text or "busy" in text:
                    raise StoreBusy(
                        f"state database busy for {self.BUSY_TIMEOUT:.0f}s ({exc});"
                        f" another writer holds the lock") from exc
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ state
    @staticmethod
    def _save_state_conn(conn: sqlite3.Connection, payload: dict[str, Any],
                         expected_revision: int | None) -> int:
        encoded = json.dumps(payload, ensure_ascii=False)
        now = utc_now()
        if expected_revision is None:
            # One statement, not SELECT-then-UPSERT. The two-step version read
            # `revision` outside a transaction, so two processes bootstrapping at
            # the same moment both computed N+1 and the second silently
            # overwrote the first — the one lost update this class cannot detect
            # afterwards. `RETURNING` makes the read and the bump atomic.
            row = conn.execute(
                """INSERT INTO paper_state(id, payload, updated_at, revision)
                   VALUES (1,?,?,1)
                   ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                       updated_at=excluded.updated_at,
                       revision=paper_state.revision + 1
                   RETURNING revision""",
                (encoded, now),
            ).fetchone()
            return int(row["revision"])

        revision = int(expected_revision) + 1
        cur = conn.execute(
            """UPDATE paper_state SET payload=?, updated_at=?, revision=?
               WHERE id=1 AND revision=?""",
            (encoded, now, revision, int(expected_revision)),
        )
        if cur.rowcount != 1:
            raise StateConflict(
                f"paper_state revision {expected_revision} is stale")
        return revision

    def save_state(self, payload: dict[str, Any],
                   expected_revision: int | None = None) -> int:
        with self._lock, self._conn(write=True) as conn:
            return self._save_state_conn(conn, payload, expected_revision)

    def load_state_with_revision(self) -> tuple[dict[str, Any] | None, int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload, revision FROM paper_state WHERE id=1").fetchone()
        if row is None:
            return None, 0
        return json.loads(row["payload"]), int(row["revision"])

    def load_state(self) -> dict[str, Any] | None:
        payload, _revision = self.load_state_with_revision()
        return payload

    # ---------------------------------------------------------------- tickets
    @staticmethod
    def _save_ticket_conn(conn: sqlite3.Connection, ticket: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO tickets(ticket_id, created_at, expires_at, status, symbol, side, sleeve, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket_id) DO UPDATE SET
                status=excluded.status, expires_at=excluded.expires_at,
                payload=excluded.payload
            """,
            (
                ticket["ticket_id"], ticket["created_at"], ticket.get("expires_at"),
                ticket["status"], ticket["symbol"], ticket["side"], ticket["sleeve"],
                json.dumps(ticket, ensure_ascii=False),
            ),
        )
        if ticket["status"] in TERMINAL_TICKET_STATUSES:
            conn.execute("DELETE FROM proposal_claims WHERE ticket_id=?", (ticket["ticket_id"],))

    def save_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        """Save a lifecycle transition and return the durable ticket.

        A terminal *transition* is immutable. Approval, expiry and guard rejection
        can race in different processes; each may hold a payload read while the
        ticket was still ``proposed``. ``BEGIN IMMEDIATE`` makes this check and
        the subsequent write one serialization point, so a stale writer cannot
        replace a fill (and its provenance) with an older rejection. A same-state
        write may maintain metadata such as a stop anchor, but the first fill or
        rejection's core fields still win.
        """
        with self._lock, self._conn(write=True) as conn:
            row = conn.execute(
                "SELECT payload FROM tickets WHERE ticket_id=?",
                (ticket["ticket_id"],),
            ).fetchone()
            if row is not None:
                current = json.loads(row["payload"])
                status = current.get("status")
                if status in TERMINAL_TICKET_STATUSES:
                    if ticket.get("status") != status:
                        return current
                    protected = {
                        "filled": ("fill", "filled_at"),
                        "rejected": ("reasons", "checks", "rejected_at"),
                        "cancelled": ("reasons", "checks", "cancelled_at",
                                      "cancel_reason"),
                        "expired": ("reasons", "expired_at"),
                    }[status]
                    ticket = dict(ticket)
                    for key in protected:
                        if key in current:
                            ticket[key] = current[key]
            self._save_ticket_conn(conn, ticket)
            return ticket

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def proposal_for_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT t.payload FROM proposal_claims c
                   JOIN tickets t ON t.ticket_id=c.ticket_id
                   WHERE c.fingerprint=? AND t.status IN ('proposed','queued','armed')""",
                (fingerprint,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def release_proposal_claim(self, ticket_id: str) -> int:
        """D-18: free the fingerprint slot, **not** the client-request promise.

        ``proposal_claims`` is pending-intent deduplication ("the same thesis
        must not become a second ticket"); ``proposal_requests`` is the caller's
        permanent idempotency handle — its DDL comment says it is kept after a
        terminal state, because "same id, same answer" outlives the intent. Only
        the former is deleted here. An ``expired_untriggered`` intent can never
        fire again, so holding its slot would hand a model re-submitting the
        same condition a dead ticket until the next-day ``expired`` sweep.

        Does not touch ``tickets.status``: the state machine still rules there.
        Idempotent; returns the number of rows deleted.
        """
        with self._lock, self._conn(write=True) as conn:
            cur = conn.execute(
                "DELETE FROM proposal_claims WHERE ticket_id=?", (ticket_id,))
            return cur.rowcount

    def proposal_for_request_id(self, request_id: str) -> dict[str, Any] | None:
        """The ticket this caller already created — whatever its status is now.

        Deliberately not filtered to pending: a retry after a fill must get the
        fill, not a second proposal. That is the whole difference between a
        fingerprinted intent and a request handle.
        """
        if not request_id:
            return None
        with self._conn() as conn:
            row = conn.execute(
                """SELECT t.payload FROM proposal_requests r
                   JOIN tickets t ON t.ticket_id=r.ticket_id
                   WHERE r.client_request_id=?""",
                (request_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def claim_request(self, request_id: str, ticket_id: str) -> dict[str, Any] | None:
        """Bind a caller id to a ticket; return the ticket that already had it.

        Public because the rejection path does not go through ``save_proposal``:
        a guard refusal is still an answer, and a retry should get that answer
        rather than a second refusal with a new id.
        """
        if not request_id:
            return None
        with self._lock, self._conn(write=True) as conn:
            return self._claim_request_conn(conn, request_id, ticket_id)

    @staticmethod
    def _claim_request_conn(conn: sqlite3.Connection, request_id: str,
                            ticket_id: str) -> dict[str, Any] | None:
        """INSERT the handle; on conflict return the ticket that owns it."""
        try:
            conn.execute(
                "INSERT INTO proposal_requests(client_request_id,ticket_id,created_at) "
                "VALUES (?,?,?)", (request_id, ticket_id, utc_now()))
            return None
        except sqlite3.IntegrityError:
            row = conn.execute(
                """SELECT t.payload FROM proposal_requests r
                   JOIN tickets t ON t.ticket_id=r.ticket_id
                   WHERE r.client_request_id=?""", (request_id,)).fetchone()
            if row is None:
                raise
            return json.loads(row["payload"])

    def save_proposal(self, ticket: dict[str, Any], fingerprint: str,
                      client_request_id: str | None = None) -> tuple[dict, bool]:
        """Atomically claim one normalized intent and persist its first ticket."""
        with self._lock, self._conn(write=True) as conn:
            # Claim the caller's handle before writing anything else. Two
            # processes retrying the same request then meet at this unique key,
            # and the loser returns the winner's ticket instead of creating a
            # second one.
            if client_request_id:
                winner = self._claim_request_conn(
                    conn, client_request_id, ticket["ticket_id"])
                if winner is not None:
                    return winner, False
            row = conn.execute(
                "SELECT ticket_id FROM proposal_claims WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if row is not None:
                existing = conn.execute(
                    "SELECT payload FROM tickets WHERE ticket_id=?", (row["ticket_id"],),
                ).fetchone()
                if existing is None:
                    raise StateConflict(f"proposal claim {row['ticket_id']} has no ticket")
                current = json.loads(existing["payload"])
                if current.get("status") in {"proposed", "queued"}:
                    if client_request_id:
                        # The intent already had a ticket; point this handle at
                        # it so a later retry gets the same answer.
                        conn.execute(
                            "UPDATE proposal_requests SET ticket_id=? WHERE client_request_id=?",
                            (current["ticket_id"], client_request_id))
                    return current, False
                conn.execute(
                    "DELETE FROM proposal_claims WHERE fingerprint=?", (fingerprint,),
                )
            try:
                conn.execute(
                    "INSERT INTO proposal_claims(fingerprint,ticket_id,created_at) VALUES (?,?,?)",
                    (fingerprint, ticket["ticket_id"], utc_now()),
                )
            except sqlite3.IntegrityError:
                # Another process claimed the same intent after our first read.
                # The unique key is the arbiter; a retry gets that winner, not a 500.
                winner = conn.execute(
                    """SELECT t.payload FROM proposal_claims c
                       JOIN tickets t ON t.ticket_id=c.ticket_id
                       WHERE c.fingerprint=?""",
                    (fingerprint,),
                ).fetchone()
                if winner is not None:
                    payload = json.loads(winner["payload"])
                    if client_request_id:
                        conn.execute(
                            "UPDATE proposal_requests SET ticket_id=? WHERE client_request_id=?",
                            (payload["ticket_id"], client_request_id))
                    return payload, False
                raise
            self._save_ticket_conn(conn, ticket)
            return ticket, True

    def list_tickets(self, limit: int = 50, status: str | None = None) -> list[dict]:
        """Newest first. ``status="open"`` means "not terminal".

        ``open`` is a pseudo-status, not a stored value: a ticket that has not
        reached ``filled/rejected/cancelled/expired`` is somebody's outstanding
        intent, and those are exactly the ones a caller cannot find anywhere
        else — ``/orders`` only has what was sent to the kernel and
        ``/portfolio.pending`` only has the opening-print queue.
        """
        sql = "SELECT payload FROM tickets"
        params: list[Any] = []
        if status == OPEN_TICKET_STATUS:
            placeholders = ",".join("?" * len(TERMINAL_TICKET_STATUSES))
            sql += f" WHERE status NOT IN ({placeholders})"
            params.extend(sorted(TERMINAL_TICKET_STATUSES))
            # B1 (2026-11-08 deep audit): an armed intent that expired without
            # ever triggering is factually retired. watch._record_intent_terminals
            # deliberately does NOT mutate status — shadow evaluation is an
            # observation, and the approve guard reads intent_terminal_state to
            # refuse any later fill. Mutating status here would invent a new
            # execution-layer transition; filtering the *view* keeps `open`
            # honest while the full record stays auditable by status=armed.
            # `denied` stays visible: a missing quote may recover next frame.
            sql += (" AND COALESCE(json_extract(payload, "
                    "'$.intent_terminal_state'), '') <> 'expired_untriggered'")
        elif status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def stop_tickets(self, symbols: list[str]) -> list[dict]:
        """Filled BUY tickets for these symbols, newest first.

        The stop belongs to the current entry, so callers need the newest BUY
        per *held* symbol — not the newest 500 tickets overall. Two reasons this
        is a query and not a filter over ``list_tickets``:

        * the window was a silent correctness bug. A position opened more than
          500 tickets ago simply had no stop any more, and "no stop" is a state
          the desk treats as legitimate rather than as missing data;
        * ``stop_gate`` runs on every proposal and used to call the scan twice —
          measured 4.1 ms per scan at 500 tickets, 8.2 ms per proposal.

        Filtering on the indexed ``symbol`` column is not enough on its own: a
        name that has been round-tripped twenty times still has twenty filled
        BUY tickets, and decoding them all is the cost that matters. The window
        function keeps exactly the newest row per symbol, so the caller gets
        ``max_positions`` rows instead of a history.
        """
        if not symbols:
            return []
        placeholders = ",".join("?" * len(symbols))
        sql = (
            "SELECT payload FROM ("
            "  SELECT payload, ROW_NUMBER() OVER ("
            "    PARTITION BY symbol ORDER BY created_at DESC, ticket_id DESC"
            "  ) AS rn"
            "  FROM tickets"
            f"  WHERE status='filled' AND side='BUY' AND symbol IN ({placeholders})"
            ") WHERE rn=1"
        )
        with self._conn() as conn:
            rows = conn.execute(sql, list(symbols)).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    # ----------------------------------------------------------------- orders
    @staticmethod
    def _save_order_conn(conn: sqlite3.Connection, order: dict[str, Any]) -> str:
        row = conn.execute(
            "SELECT payload FROM orders WHERE order_id=?", (order["order_id"],)
        ).fetchone()
        stored_as = order["order_id"]
        payload = dict(order)
        if row is not None:
            existing = json.loads(row["payload"])
            if existing.get("ticket_id") != order.get("ticket_id"):
                suffix = str(order.get("ticket_id") or "dup")[-8:]
                stored_as = f"{order['order_id']}-{suffix}"
                payload["collision_with"] = {
                    "order_id": order["order_id"],
                    "ticket_id": existing.get("ticket_id"),
                    "ts": existing.get("ts"),
                }
                payload["stored_as"] = stored_as
        payload["order_id"] = stored_as
        conn.execute(
            """
            INSERT INTO orders(order_id, ts, symbol, status, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                ts=excluded.ts, status=excluded.status, payload=excluded.payload
            """,
            (stored_as, order["ts"], order["symbol"], order["status"],
             json.dumps(payload, ensure_ascii=False)),
        )
        return stored_as

    def save_order(self, order: dict[str, Any]) -> str:
        """Persist one order without allowing a different ticket to overwrite it."""
        with self._lock, self._conn(write=True) as conn:
            return self._save_order_conn(conn, order)

    def list_orders(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM orders ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def get_fill(self, ticket_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM paper_fills WHERE ticket_id=?", (ticket_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def recent_fills(self, limit: int = 500) -> list[dict[str, Any]]:
        """Newest fills first, for execution-quality measurement."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM paper_fills ORDER BY rowid DESC LIMIT ?",
                (max(1, int(limit)),)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def count_fills(self) -> int:
        """How many fills this book has ever booked.

        ``state["fills"]`` is a rolling window (``PaperBook.HISTORY_KEEP``), so
        counting it would freeze the reported total at the window size. The
        table is the full record; one ``COUNT(*)`` on a primary-key table is
        cheaper than the JSON it replaces.
        """
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM paper_fills").fetchone()
        return int(row["n"])

    def pending_ticket_ids(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT ticket_id FROM pending_tickets ORDER BY ticket_id").fetchall()
        return [str(row["ticket_id"]) for row in rows]

    @staticmethod
    def _sync_pending_conn(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        conn.execute("DELETE FROM pending_tickets")
        now = utc_now()
        for entry in payload.get("pending", []):
            conn.execute(
                "INSERT INTO pending_tickets(ticket_id, updated_at, payload) VALUES (?,?,?)",
                (entry["ticket_id"], now, json.dumps(entry, ensure_ascii=False)),
            )

    def commit_execution(self, payload: dict[str, Any], expected_revision: int,
                         *, order: dict[str, Any], ticket: dict[str, Any],
                         fill: dict[str, Any], case: dict[str, Any] | None,
                         actor: str) -> int:
        """Atomically book fill, account, queue removal, ticket and audit rows."""
        with self._lock, self._conn(write=True) as conn:
            existing = conn.execute(
                "SELECT ticket_id FROM paper_fills WHERE ticket_id=?",
                (fill["ticket_id"],),
            ).fetchone()
            if existing is not None:
                raise ExecutionExists(f"ticket {fill['ticket_id']} already has a fill")
            try:
                conn.execute(
                    """INSERT INTO paper_fills(ticket_id, fill_id, order_id, created_at, payload)
                       VALUES (?,?,?,?,?)""",
                    (fill["ticket_id"], fill["fill_id"], fill["order_id"],
                     utc_now(), json.dumps(fill, ensure_ascii=False)),
                )
            except sqlite3.IntegrityError as exc:
                # The SELECT above is advisory: two processes can both find the
                # ticket unfilled and race to insert. The primary key is what
                # actually serialises them, so its complaint has to become the
                # same answer the check would have given — otherwise the loser
                # of the race gets a 500 on a fill that *did* happen.
                raise ExecutionExists(
                    f"ticket {fill['ticket_id']} already has a fill") from exc
            revision = self._save_state_conn(conn, payload, expected_revision)
            stored_as = self._save_order_conn(conn, order)
            self._save_ticket_conn(conn, ticket)
            if case is not None:
                self._add_case_conn(conn, case)
            self._log_decision_conn(conn, "fill", fill, fill["ticket_id"], actor)
            if stored_as != order["order_id"]:
                self._log_decision_conn(conn, "order_id_collision", {
                    "order_id": order["order_id"], "stored_as": stored_as,
                    "ticket_id": fill["ticket_id"],
                }, fill["ticket_id"], "system")
            self._sync_pending_conn(conn, payload)
            return revision

    def commit_ticket_state(self, payload: dict[str, Any], expected_revision: int,
                            *, ticket: dict[str, Any], kind: str,
                            decision: dict[str, Any], actor: str) -> int:
        """Atomically change queue/account state and its ticket's lifecycle."""
        with self._lock, self._conn(write=True) as conn:
            revision = self._save_state_conn(conn, payload, expected_revision)
            self._save_ticket_conn(conn, ticket)
            self._log_decision_conn(conn, kind, decision, ticket["ticket_id"], actor)
            self._sync_pending_conn(conn, payload)
            return revision

    def open_order_records(self) -> dict[str, dict[str, Any]]:
        """Recover durable non-terminal OMS orders for a ledger restart."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM orders WHERE status IN "
                "('PENDING_NEW','NEW','PARTIALLY_FILLED','PENDING_CANCEL')"
            ).fetchall()
        out = {}
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("order_id"):
                out[str(payload["order_id"])] = payload
        return out

    def max_order_seq(self) -> int:
        """Highest order number already written to the audit table.

        The in-memory counter is persisted with the account, but a crash between
        the fill and the save can leave an order row behind without the counter.
        Loading the max of both makes the id monotonic across either failure.
        """
        with self._conn() as conn:
            rows = conn.execute("SELECT order_id FROM orders").fetchall()
        best = 0
        for row in rows:
            oid = str(row["order_id"] or "")
            if oid[:1] == "O" and oid[1:].isdigit():
                best = max(best, int(oid[1:]))
        return best

    # -------------------------------------------------------------- decisions
    @staticmethod
    def _log_decision_conn(conn: sqlite3.Connection, kind: str, payload: dict[str, Any],
                           ticket_id: str | None, actor: str) -> None:
        # 所有写入（含 commit_ticket_state）都过这一个口子，值域校验放这里单点
        # 生效。软校验：登记未声明的值，不拒绝一条已经走完风控的审计记录。
        vocab.check_decision(kind, actor)
        conn.execute(
            "INSERT INTO decisions(ts, kind, ticket_id, actor, payload) VALUES (?,?,?,?,?)",
            (utc_now(), kind, ticket_id, actor, json.dumps(payload, ensure_ascii=False)),
        )

    def log_decision(self, kind: str, payload: dict[str, Any], *,
                     ticket_id: str | None = None, actor: str = "agent") -> None:
        with self._lock, self._conn(write=True) as conn:
            self._log_decision_conn(conn, kind, payload, ticket_id, actor)

    def list_decisions(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    # --------------------------------------------------------------- declined
    def add_declined(self, day: str, symbols: list[str], reason: str, *,
                     invalidation: str = "", horizon_days: int = 3) -> int:
        """Record what the desk looked at and did not buy.

        No prices. The settlement reads them, so a declined basket cannot be
        scored against a price the caller picked after the fact.
        """
        with self._lock, self._conn(write=True) as conn:
            cur = conn.execute(
                "INSERT INTO declined(ts, day, symbols, reason, invalidation,"
                " horizon_days) VALUES (?,?,?,?,?,?)",
                (utc_now(), str(day), json.dumps(list(symbols), ensure_ascii=False),
                 str(reason), str(invalidation or ""), int(horizon_days)),
            )
            return int(cur.lastrowid or 0)

    def list_declined(self, limit: int = 100, *, settled: bool | None = None) -> list[dict]:
        sql = "SELECT * FROM declined"
        if settled is True:
            sql += " WHERE settled_at IS NOT NULL"
        elif settled is False:
            sql += " WHERE settled_at IS NULL"
        sql += " ORDER BY id DESC LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["symbols"] = json.loads(item.get("symbols") or "[]")
            item["result"] = json.loads(item["result"]) if item.get("result") else None
            out.append(item)
        return out

    def settle_declined(self, declined_id: int, result: dict) -> None:
        with self._lock, self._conn(write=True) as conn:
            conn.execute(
                "UPDATE declined SET settled_at=?, result=? WHERE id=?",
                (utc_now(), json.dumps(result, ensure_ascii=False), int(declined_id)),
            )

    # ---------------------------------------------------------------- journal
    def add_journal(self, text: str, *, kind: str = "note", symbol: str | None = None,
                    tags: str = "", snapshot: dict | None = None) -> int:
        with self._lock, self._conn(write=True) as conn:
            cur = conn.execute(
                "INSERT INTO journal(ts, kind, symbol, text, tags, snapshot) VALUES (?,?,?,?,?,?)",
                (utc_now(), kind, symbol, text, tags,
                 json.dumps(snapshot or {}, ensure_ascii=False)),
            )
            return int(cur.lastrowid or 0)

    def set_journal_snapshot(self, row_id: int, snapshot: dict) -> None:
        """Replace one entry's snapshot — used by the verification backfill.

        The journal is append-only for its text; the snapshot is metadata *about*
        the text, and the entries written before the check existed never got one.
        Rewriting it is the only way to make those entries readable with the
        same field the new ones carry. It touches no prose.
        """
        with self._lock, self._conn(write=True) as conn:
            conn.execute("UPDATE journal SET snapshot=? WHERE id=?",
                         (json.dumps(snapshot or {}, ensure_ascii=False), int(row_id)))

    def search_journal(self, query: str = "", limit: int = 20, *,
                       kind: str | None = None, since: str | None = None,
                       until: str | None = None,
                       row_id: int | None = None) -> list[dict]:
        """Journal rows, newest first, with the filters pushed into SQL.

        ``kind`` / ``until`` / ``since`` exist because "the last review" and "the
        last entry" are different questions with different answers. Filtering a
        20-row window in Python — which is what ``duty.py`` used to do — works
        only while reviews outnumber notes; notes are written ~4x as often, so
        that window silently stops containing any review at all, and a check that
        reads "no review today" starts firing on days that have one. A filter
        that can only be answered by scanning a fixed window is not a filter.

        ``row_id`` opens exactly one entry. Without it the only way to read a
        long entry's full text was ``full=true``, which returns every row in the
        page at full length (5x, capped at 8 rows) — so reading one 700-character
        review cost the whole page.

        Dates are ``YYYYMMDD`` or ``YYYY-MM-DD``; ``ts`` is stored ISO with an
        offset, so the comparison is on the first 10 characters.
        """
        clause, params = _journal_filter(kind=kind, since=since, until=until,
                                         row_id=row_id)
        text = query.strip()
        by_id = row_id is not None
        if text and self.fts and len(text) >= 3 and not by_id:
            try:
                with self._conn() as conn:
                    rows = conn.execute(
                        "SELECT j.* FROM journal_fts f JOIN journal j ON j.id = f.rowid "
                        f"WHERE journal_fts MATCH ?{clause} ORDER BY rank LIMIT ?",
                        (f'"{text}"', *params, limit),
                    ).fetchall()
                return [self._journal_row(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        with self._conn() as conn:
            if text and not by_id:
                rows = conn.execute(
                    "SELECT * FROM journal WHERE (text LIKE ? OR tags LIKE ? "
                    f"OR symbol LIKE ?){clause} ORDER BY id DESC LIMIT ?",
                    (f"%{text}%", f"%{text}%", f"%{text}%", *params, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM journal WHERE 1=1{clause} ORDER BY id DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
        return [self._journal_row(r) for r in rows]

    @staticmethod
    def _journal_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["snapshot"] = json.loads(item.get("snapshot") or "{}")
        return item

    # ------------------------------------------------------------------ cases
    @staticmethod
    def _add_case_conn(conn: sqlite3.Connection, case: dict[str, Any]) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO cases
               (case_id, symbol, opened_at, closed_at, qty, entry, exit, pnl, pnl_pct,
                thesis, invalidation, ticket_ids, note, strategy, persona)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case["case_id"], case["symbol"], case.get("opened_at"),
                case["closed_at"], int(case["qty"]), float(case["entry"]),
                float(case["exit"]), float(case["pnl"]), float(case["pnl_pct"]),
                case.get("thesis", ""), case.get("invalidation", ""),
                json.dumps(case.get("ticket_ids", []), ensure_ascii=False),
                case.get("note", ""), case.get("strategy", "llm"), case.get("persona"),
            ),
        )

    def add_case(self, case: dict[str, Any]) -> None:
        with self._lock, self._conn(write=True) as conn:
            self._add_case_conn(conn, case)

    @staticmethod
    def _case_row(row: sqlite3.Row) -> dict:
        """One closed case, with money at cents and percentages at 4 places.

        `pnl_pct` came out of the arithmetic as `-0.008406256954365008` — sixteen
        decimals of float noise in a field a reader compares against other
        percentages. It surfaced on 2026-11-02 when a sample trade closed and the
        quality audit found it. The database keeps the raw value; the read path
        is where rounding belongs, otherwise every export has to remember.
        """
        item = dict(row)
        item["ticket_ids"] = json.loads(item.get("ticket_ids") or "[]")
        for key in ("entry", "exit", "pnl"):
            if isinstance(item.get(key), float):
                item[key] = round(item[key], 2)
        if isinstance(item.get("pnl_pct"), float):
            item["pnl_pct"] = round(item["pnl_pct"], 4)
        return item

    def list_cases(self, limit: int = 20, symbol: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM cases WHERE symbol=? ORDER BY closed_at DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cases ORDER BY closed_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._case_row(row) for row in rows]

    def search_cases(self, query: str, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM cases
                   WHERE symbol LIKE ? OR thesis LIKE ? OR note LIKE ?
                   ORDER BY closed_at DESC LIMIT ?""",
                (f"%{query}%", f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        return [self._case_row(row) for row in rows]

    # ------------------------------------------------------------- hypotheses
    def add_hypothesis(self, statement: str, rationale: str = "",
                       test_spec: str = "") -> int:
        now = utc_now()
        with self._lock, self._conn(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO hypotheses(created_at, updated_at, statement, rationale,
                   test_spec, status, result) VALUES (?,?,?,?,?,'draft','{}')""",
                (now, now, statement, rationale, test_spec),
            )
            return int(cur.lastrowid or 0)

    def list_hypotheses(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM hypotheses ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["result"] = json.loads(item.get("result") or "{}")
            out.append(item)
        return out

    def get_hypothesis(self, hyp_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM hypotheses WHERE id=?", (hyp_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["result"] = json.loads(item.get("result") or "{}")
        return item

    def set_hypothesis_spec(self, hyp_id: int, test_spec: str) -> None:
        """Fix a malformed spec without discarding the hypothesis."""
        now = utc_now()
        with self._lock, self._conn(write=True) as conn:
            conn.execute(
                "UPDATE hypotheses SET test_spec=?, updated_at=? WHERE id=?",
                (test_spec, now, hyp_id),
            )

    def update_hypothesis(self, hyp_id: int, status: str, result: dict | None = None) -> dict | None:
        # `underpowered` 也是裁判的合法读数：它说的是"这个设计看不见"，
        # 不是"没跑过"。漏掉它会让 run_research 在**报告已经落盘之后**抛
        # ValueError，账本停在旧状态——一种安静的不一致。
        if status not in ("draft", "testing", "promoted", "rejected", "underpowered"):
            raise ValueError(f"bad hypothesis status: {status}")
        now = utc_now()
        with self._lock, self._conn(write=True) as conn:
            conn.execute(
                "UPDATE hypotheses SET status=?, result=?, updated_at=? WHERE id=?",
                (status, json.dumps(result or {}, ensure_ascii=False), now, hyp_id),
            )
        return self.get_hypothesis(hyp_id)

    # --------------------------------------------------------------- persona
    def persona_current(self, default: str = "retail") -> dict:
        """The active decision style, plus how long it has been running.

        ``days_in_persona`` is calendar days, not trading days: it answers "has
        this style had time to be judged yet", which is a wall-clock question.
        """
        today = datetime.now(SHANGHAI).strftime("%Y%m%d")
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM persona_state WHERE id=1").fetchone()
        # 配额按 `persona_switch.day` 现算，不能读 `persona_state.switches_today`：
        # 那一列只在切换时写一次，跨日不会归零。2026-11-09 实读仍是 11-05 的
        # 1，而当天一次都没切——任何读到它的窗口都会以为日限 2 次已用掉一半。
        used = self.persona_switch_count(today)
        if row is None:
            return {"current": default, "since": None, "day": None,
                    "switches_today": int(used or 0), "days_in_persona": 0}
        item = dict(row)
        since = str(item.get("since") or "")
        days = 0
        if since:
            try:
                days = max(0, (datetime.now() - datetime.strptime(since, "%Y%m%d")).days)
            except ValueError:
                days = 0
        return {"current": str(item["current"]), "since": since,
                "day": str(item.get("day") or ""),
                "switches_today": int(used or 0),
                "days_in_persona": days}

    def persona_set(self, current: str, *, since: str, day: str,
                    switches_today: int = 0) -> None:
        with self._lock, self._conn(write=True) as conn:
            conn.execute(
                """INSERT INTO persona_state(id, current, since, day, switches_today,
                                             updated_at)
                   VALUES (1,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     current=excluded.current, since=excluded.since, day=excluded.day,
                     switches_today=excluded.switches_today,
                     updated_at=excluded.updated_at""",
                (current, since, day, int(switches_today), utc_now()),
            )

    def persona_log_switch(self, *, day: str, from_key: str | None, to_key: str,
                           reason: str, evidence: list[str],
                           expected_edge_bps: float | None, invalidation: str,
                           horizon_days: int | None = None, regime: str = "",
                           actor: str = "agent", readiness: str = "ready",
                           nav: float | None = None) -> dict:
        ts = utc_now()
        with self._lock, self._conn(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO persona_switch(ts, day, from_key, to_key, reason, evidence,
                     expected_edge_bps, invalidation, horizon_days, regime, readiness,
                     nav_at_switch, actor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, day, from_key, to_key, reason,
                 json.dumps(evidence, ensure_ascii=False), expected_edge_bps,
                 invalidation, horizon_days, regime, readiness, nav, actor),
            )
            new_id = cur.lastrowid
        return {"id": new_id, "ts": ts, "day": day}

    def persona_switches(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM persona_switch ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["evidence"] = json.loads(item.get("evidence") or "[]")
            out.append(item)
        return out

    def persona_switch_count(self, day: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM persona_switch WHERE day=?", (day,)
            ).fetchone()
        return int(row[0] or 0)

    # --------------------------------------------------------------- core nav
    def core_record_nav(self, trade_date: str, nav: str, cash: str, invested: str,
                        weights: dict) -> None:
        with self._lock, self._conn(write=True) as conn:
            conn.execute(
                """INSERT INTO core_nav(trade_date, nav, cash, invested, weights)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(trade_date) DO UPDATE SET
                     nav=excluded.nav, cash=excluded.cash,
                     invested=excluded.invested, weights=excluded.weights""",
                (trade_date, str(nav), str(cash), str(invested),
                 json.dumps(weights, ensure_ascii=False, sort_keys=True)),
            )

    def core_nav_history(self, limit: int = 400) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM core_nav ORDER BY trade_date DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["weights"] = json.loads(item.get("weights") or "{}")
            out.append(item)
        return list(reversed(out))

    def core_record_trade(self, trade_date: str, symbol: str, side: str, qty: int,
                          price: str, fee: str, reason: str = "") -> None:
        with self._lock, self._conn(write=True) as conn:
            conn.execute(
                """INSERT INTO core_trade(trade_date, symbol, side, qty, price, fee, reason)
                   VALUES (?,?,?,?,?,?,?)""",
                (trade_date, symbol, side, int(qty), str(price), str(fee), reason),
            )

    def core_trades(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM core_trade ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ memos
    def add_memo(self, topic: str, thesis: str, mechanism: str = "", falsifier: str = "",
                 confidence: float = 0.5, predictions: list[dict] | None = None) -> int:
        now = utc_now()
        with self._lock, self._conn(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO memo(created_at, topic, thesis, mechanism, falsifier,
                   confidence, predictions, status, score) VALUES (?,?,?,?,?,?,?, 'open', '{}')""",
                (now, topic, thesis, mechanism, falsifier, float(confidence),
                 json.dumps(predictions or [], ensure_ascii=False)),
            )
            return int(cur.lastrowid or 0)

    def list_memos(self, limit: int = 50, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM memo"
        params: list[object] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["predictions"] = json.loads(item.get("predictions") or "[]")
            item["score"] = json.loads(item.get("score") or "{}")
            out.append(item)
        return out

    def close_memo(self, memo_id: int, score: dict) -> None:
        with self._lock, self._conn(write=True) as conn:
            conn.execute(
                "UPDATE memo SET status='scored', score=? WHERE id=?",
                (json.dumps(score, ensure_ascii=False), memo_id),
            )

    def calibration(self) -> dict:
        rows = self.list_memos(limit=1000, status="scored")
        pairs: list[tuple[float, int]] = []
        for memo in rows:
            for item in memo.get("score", {}).get("predictions", []):
                if "probability" in item and "outcome" in item:
                    pairs.append((float(item["probability"]), int(item["outcome"])))
        if not pairs:
            return {"n": 0, "brier": None, "base_rate": None,
                    "brier_climatology": None, "bss": None, "buckets": []}
        brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)

        # 一个 Brier 数单独看没有意义，但有参照的方式只有一种是对的：
        # 气候基准（climatology）—— 永远报这个样本的发生率 s，得 B_c = s(1-s)，
        # 技巧分 BSS = 1 - B/B_c。BSS=0 表示和"只会报基础率"一样好，为负表示更差。
        #
        # 这里原来报的是 `skill_vs_constant = 0.25 - brier`。它有两个毛病：
        #   1. 基准取了常数 0.5，那是 s=0.5 的特例，也是最弱的一条基准。样本不
        #      均衡时（本账 4/7 次发生）它会让预测显得比实际好。
        #   2. 更严重的是它算的是**差值**，而字段名 `skill_vs_constant` 听起来是
        #      **技巧分**。同一组数据：差值是 −0.05（"略有不如"），BSS 是 −0.23
        #      （"比什么都不知道差 23%"）。名字承诺了后者，给的是前者。
        base_rate = sum(y for _, y in pairs) / len(pairs)
        brier_climatology = base_rate * (1.0 - base_rate)
        bss = (1.0 - brier / brier_climatology) if brier_climatology > 0 else None
        buckets = []
        for lo, hi in ((0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.01)):
            chunk = [(p, y) for p, y in pairs if lo <= p < hi]
            if chunk:
                buckets.append({
                    "range": f"{lo:.1f}-{min(hi, 1.0):.1f}",
                    "n": len(chunk),
                    "predicted": round(sum(p for p, _ in chunk) / len(chunk), 3),
                    "realised": round(sum(y for _, y in chunk) / len(chunk), 3),
                })
        return {"n": len(pairs), "brier": round(brier, 4),
                "base_rate": round(base_rate, 4),
                "brier_climatology": round(brier_climatology, 4),
                "bss": None if bss is None else round(bss, 4),
                "buckets": buckets,
                "reading": ("bss 是唯一该看的技巧分：1 − brier/brier_climatology，"
                            "0 = 和「只会报基础率」一样好，负数 = 更差，1 = 完美。"
                            "brier_climatology 是 s(1−s)，s 见 base_rate——样本均衡时它"
                            "约等于 0.25，不均衡时会明显低于 0.25，那时拿 0.25 当参照会"
                            "让预测显得比实际好。bss 为 null 表示样本全同一边（s 为 0 或 1），"
                            "此时技巧分没有定义，不是零。"
                            "buckets 里 predicted 与 realised 的差距说明偏在哪一边。")}

    # ------------------------------------------------------------------ usage
    def orphan_tables(self) -> dict[str, int]:
        """Tables in the account file that no DDL in this module declares.

        Usually a deleted feature whose table was never dropped. `advice` is the
        example: P14 created it, P15 (commit 13333ee, 2026-10-26 10:57) removed
        the module, the DDL, the four store methods and the routes — but a
        ``DROP TABLE`` never ran, so the last row (written 10:24 that morning)
        outlived its schema. Nothing reads it, nothing expires it, no guard
        applies, and for exactly that reason it must stay visible: a pending
        suggestion that no code can retire is worse than no suggestion at all.
        """
        declared = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", DDL))
        with self._conn() as conn:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            found: dict[str, int] = {}
            for name in names:
                if name in declared or name.startswith(("sqlite_", "journal_fts")):
                    continue
                found[name] = int(conn.execute(
                    f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        return found

    @staticmethod
    def _record_usage_conn(conn: sqlite3.Connection, endpoint: str, method: str,
                           status: int, day: str | None = None) -> None:
        """One counter increment. The statement lives here so every writer
        (the per-request store, its cached-connection subclass, scripts) uses
        the same upsert and the same ok/fail split.

        ``day`` is the **exchange's** date, not the host's: ``utc_now()`` is
        local time, so a box in another timezone filed the same session under
        two different days and `/usage` disagreed with ``journal.day``.
        """
        day = day or datetime.now(SHANGHAI).strftime("%Y%m%d")
        ok = 0 if int(status) >= 400 else 1
        fail = 1 - ok
        conn.execute(
            """INSERT INTO usage(day, endpoint, method, calls, ok_calls, fail_calls, last_ts)
               VALUES (?,?,?,1,?,?,?)
               ON CONFLICT(day, endpoint, method) DO UPDATE SET
                 calls = calls + 1,
                 ok_calls = ok_calls + excluded.ok_calls,
                 fail_calls = fail_calls + excluded.fail_calls,
                 last_ts = excluded.last_ts""",
            (day, endpoint, method.upper(), ok, fail, utc_now()),
        )

    def record_usage(self, endpoint: str, method: str, status: int = 200,
                     day: str | None = None) -> None:
        """Count one served request, split by whether the caller got an answer.

        A 4xx is the caller's mistake (wrong path, wrong schema, failed guard),
        not work done. Keeping the two in one counter is what made 241 rejected
        proposals read as 241 decisions.
        """
        with self._lock, self._conn(write=True) as conn:
            self._record_usage_conn(conn, endpoint, method, status, day)

    def usage_summary(self, days: int = 7) -> dict:
        """Request counts over the last ``days`` days — all of them, or none.

        ``days`` used to trim nothing but the *display* of ``by_day``: the SQL
        had no ``WHERE``, so ``days=1`` came back with one entry under
        ``by_day`` and ``total`` = every call ever recorded (19,398 of them on
        2026-11-05, across 11 days). The desktop window and `/dashboard` both
        ask for ``days=1`` and show it as "today", which made a quiet morning
        look like the busiest day on record. One response may not mean two
        different windows.

        The cutoff matches the bucket `record_usage` writes: ``utc_now()[:10]``
        — local, despite the name. Deriving it from a different clock would
        drop today's rows between midnight and 08:00.
        """
        days = max(1, int(days))
        since = ((datetime.now().astimezone() - timedelta(days=days - 1))
                 .strftime("%Y%m%d"))
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT day, endpoint, method, calls, ok_calls, fail_calls, last_ts
                   FROM usage WHERE day >= ? ORDER BY day DESC, calls DESC""",
                (since,),
            ).fetchall()
        by_endpoint: dict[str, int] = {}
        by_day: dict[str, int] = {}
        last_seen: dict[str, str] = {}
        failures: dict[str, int] = {}
        unclassified: dict[str, int] = {}
        for r in rows:
            key = f"{r['method']} {r['endpoint']}"
            calls = int(r["calls"])
            ok = int(r["ok_calls"] or 0)
            fail = int(r["fail_calls"] or 0)
            by_endpoint[key] = by_endpoint.get(key, 0) + calls
            by_day[r["day"]] = by_day.get(r["day"], 0) + calls
            if fail:
                failures[key] = failures.get(key, 0) + fail
            legacy = calls - ok - fail
            if legacy:
                unclassified[key] = unclassified.get(key, 0) + legacy
            if r["last_ts"] and (key not in last_seen or r["last_ts"] > last_seen[key]):
                last_seen[key] = r["last_ts"]
        total_ok = sum(int(r["ok_calls"] or 0) for r in rows)
        total_fail = sum(int(r["fail_calls"] or 0) for r in rows)
        total_calls = sum(by_endpoint.values())
        return {
            "days": days,
            # 窗口的起点，和数字放在一起。`days` 是相对量，读者算不出边界；
            # 而"总数到底盖了哪几天"正是这个接口以前答错的那个问题。
            "since": since,
            "by_endpoint": dict(sorted(by_endpoint.items(), key=lambda kv: -kv[1])),
            # 不再切片：窗口已经由 SQL 保证，切片只会把"WHERE 被删掉"这种
            # 回退伪装成正常（by_day 照旧是 days 条，total 却是历史累计）。
            "by_day": dict(sorted(by_day.items(), reverse=True)),
            "failures": dict(sorted(failures.items(), key=lambda kv: -kv[1])),
            "ok": total_ok,
            "fail": total_fail,
            "unclassified": total_calls - total_ok - total_fail,
            "last_seen": last_seen,
            "total": total_calls,
        }

    # ---------------------------------------------------------------- summary
    def counts(self) -> dict[str, int]:
        with self._conn() as conn:
            tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            journal = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
            decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        return {"tickets": tickets, "orders": orders, "journal": journal,
                "decisions": decisions}
