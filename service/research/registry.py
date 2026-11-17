"""Every trial, counted once, in one place.

The referee deflates a Sharpe ratio by "the number of trials". Until this module
it used ``len(grid)`` — the variants inside one ``run()`` call. Nine hypotheses
run on the same 2023-2026 panel therefore deflated themselves by 8 each, i.e.
72 searches were booked as 8. The Deflated Sharpe ratio and any false-discovery
statement are only as honest as that denominator.

This registry is the denominator:

- one row per ``run()``, keyed by a hash of the *economic* spec, so re-running
  the same idea is counted as a second **look**, not as a new idea;
- a family is the set of trials that share a data panel; BH-FDR is computed
  inside a family, because that is the population a false discovery rate is
  defined over;
- ``p_adjusted`` is Benjamini-Hochberg, the step-up procedure that controls the
  expected proportion of false discoveries rather than the chance of any
  (Romano-Wolf step-down is stricter; BH is the right default when the goal is
  "how many of these are worth a second look", not "which one is certainly real").

Numbers are stored, not remembered: a hypothesis that was run before this module
existed can be backfilled with ``source="backfill"`` and immediately joins the
denominator.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .spec import EventStudy

DEFAULT_FAMILY = "ashare_daily"

DDL = """
CREATE TABLE IF NOT EXISTS trials (
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
    data_fingerprint TEXT,
    code_fingerprint TEXT,
    config_fingerprint TEXT,
    data_inputs TEXT,
    source TEXT NOT NULL DEFAULT 'run'
);
CREATE INDEX IF NOT EXISTS idx_trials_family ON trials(family);
CREATE INDEX IF NOT EXISTS idx_trials_hash ON trials(spec_hash);

-- Supersession lives *beside* the denominator, never inside it. The `trials`
-- table is what a Deflated Sharpe divides by, so nothing in it may be edited or
-- removed: a look that happened still happened. What changes is only the label
-- on it, and a label gets its own append-only log.
--
--   superseded_by NOT NULL  -> this trial was overturned by that one
--   superseded_by NULL      -> the previous claim was withdrawn; live again
--
-- Current state = the latest row per trial_id. An overturned trial keeps
-- counting; it just stops being a candidate.
CREATE TABLE IF NOT EXISTS supersessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    family TEXT NOT NULL,
    trial_id INTEGER NOT NULL,
    superseded_by INTEGER,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_supersessions_trial ON supersessions(trial_id);
"""

# Search dimensions only. Cost and slippage are measurement assumptions, not
# choices the researcher made; re-running the same idea at a different cost is
# the same hypothesis, and must count as a new look rather than a new trial.
HASH_FIELDS = (
    "pool", "boards", "entry", "hold", "exit_at", "start", "end",
    "gap_threshold", "link_pool", "max_per_day", "rank_by", "rank_dir",
    "min_amount", "exclude_st", "exclude_bj", "lot",
)


def spec_hash(spec: EventStudy) -> str:
    payload = {k: getattr(spec, k) for k in HASH_FIELDS}
    if isinstance(payload.get("boards"), tuple):
        payload["boards"] = list(payload["boards"])
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def look_hash(payload: dict) -> str:
    """Identity of any single look at the data, not just a full event study.

    A bucket test and an overlay comparison are searches too. Counting only
    ``run()`` calls as trials would leave the two cheapest — and therefore most
    heavily used — ways to look outside the denominator, which is exactly
    backwards: those are the ones that get run fifty times in an afternoon.
    """
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def bucket_hash(bucket: str, horizon: int, window: tuple[str, str] | list[str]) -> str:
    return look_hash({"kind": "bucket", "bucket": bucket, "horizon": int(horizon),
                      "window": list(window)})


def overlay_hash(base: EventStudy, rule: str, params: dict) -> str:
    """Same rule on a different book is a different hypothesis, so the base is in."""
    payload = {k: getattr(base, k) for k in HASH_FIELDS}
    if isinstance(payload.get("boards"), tuple):
        payload["boards"] = list(payload["boards"])
    return look_hash({"kind": "overlay", "base": payload, "rule": rule,
                      "params": {k: params[k] for k in sorted(params)}})


def _finite_optional(value: float | None, name: str, *, probability: bool = False) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数或 None") from exc
    if not math.isfinite(number) or (probability and not 0.0 <= number <= 1.0):
        interval = " [0, 1] 内" if probability else ""
        raise ValueError(f"{name} 必须是{interval}有限数或 None")
    return number


def bh_adjust(p_values: list[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values, aligned with the input.

    ``adj_(i) = min_{k >= i} p_(k) * n / k`` on the sorted p-values. Returning
    them aligned (not sorted) is deliberate: the caller has to say *which* trial
    survived, and sorting would lose that.
    """
    n = len(p_values)
    if n == 0:
        return []
    p_values = [_finite_optional(p, "p_value", probability=True) for p in p_values]
    order = sorted(range(n), key=lambda i: p_values[i])
    adjusted = [1.0] * n
    running = 1.0
    for rank in range(n, 0, -1):
        idx = order[rank - 1]
        value = min(1.0, p_values[idx] * n / rank)
        running = min(running, value)
        adjusted[idx] = round(running, 6)
    return adjusted


# Columns added after the first release. SQLite has no `ADD COLUMN IF NOT
# EXISTS`, so presence is checked explicitly; this keeps an existing
# research.sqlite usable instead of demanding a rebuild that would destroy the
# trial history it exists to protect.
_LATE_COLUMNS = {
    "data_fingerprint": "TEXT",
    "code_fingerprint": "TEXT",
    "config_fingerprint": "TEXT",
    "data_inputs": "TEXT",
}


class TrialRegistry:
    """SQLite-backed trial log. One file, one table, no service dependency."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as conn:
            conn.executescript(DDL)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(trials)")}
        for column, kind in _LATE_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE trials ADD COLUMN {column} {kind}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ write
    def record_look(self, *, spec_hash_value: str, spec_json: dict,
                    pool: str | None = None, n_variants: int = 1,
                    n_trades: int | None = None, p_value: float | None = None,
                    effect: float | None = None, verdict: str = "",
                    window: tuple[str, str] | list[str] | None = None,
                    data_fingerprint: str | None = None,
                    code_fingerprint: str | None = None,
                    config_fingerprint: str | None = None,
                    data_inputs: tuple[str, ...] | list[str] | None = None,
                    family: str = DEFAULT_FAMILY, source: str = "look") -> dict:
        """Append one look at the data, whatever form it took.

        ``n_variants`` is the number of searches inside that look: an event
        study contributes its whole grid, a bucket test or an overlay comparison
        contributes one. Everything lands in the same table because the
        denominator that matters is "how many times did this desk compare a
        hypothesis to this panel", not "how many of those were event studies".
        """
        if not str(spec_hash_value).strip() or not isinstance(spec_json, dict):
            raise ValueError("spec_hash_value 与 spec_json 必须标识这次 look")
        if not str(family).strip() or not str(source).strip():
            raise ValueError("family 与 source 不能为空")
        if isinstance(n_variants, bool) or int(n_variants) != n_variants or int(n_variants) <= 0:
            raise ValueError("n_variants 必须是正整数")
        if n_trades is not None and (isinstance(n_trades, bool)
                                     or int(n_trades) != n_trades or int(n_trades) < 0):
            raise ValueError("n_trades 必须是非负整数或 None")
        p_value = _finite_optional(p_value, "p_value", probability=True)
        effect = _finite_optional(effect, "effect")
        if window is not None and len(window) != 2:
            raise ValueError("window 必须恰好包含 start/end")
        encoded_inputs = (json.dumps(list(data_inputs), ensure_ascii=False)
                          if data_inputs is not None else None)
        with self._lock, self._conn() as conn:
            looks = conn.execute(
                "SELECT COUNT(*) FROM trials WHERE family=? AND spec_hash=?",
                (family, spec_hash_value)).fetchone()[0] + 1
            conn.execute(
                """
                INSERT INTO trials(created_at, family, spec_hash, spec_json, pool,
                                   n_variants, n_looks, n_trades, p_value, effect,
                                   verdict, window_start, window_end, data_fingerprint,
                                   code_fingerprint, config_fingerprint, data_inputs, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (datetime.now(UTC).isoformat(timespec="seconds"), family,
                 spec_hash_value,
                 json.dumps(spec_json, ensure_ascii=False, default=str),
                 pool, int(n_variants), int(looks), n_trades, p_value, effect,
                 verdict, *(window or (None, None)), data_fingerprint,
                 code_fingerprint, config_fingerprint, encoded_inputs, source),
            )
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {"id": row_id, "spec_hash": spec_hash_value, "n_looks": looks,
                "family": family,
                **self.program_status(family, hash_value=spec_hash_value)}

    def record(self, spec: EventStudy, *, n_variants: int, n_trades: int | None = None,
               p_value: float | None = None, effect: float | None = None,
               verdict: str = "", family: str = DEFAULT_FAMILY,
               source: str = "run", data_fingerprint: str | None = None,
               code_fingerprint: str | None = None,
               config_fingerprint: str | None = None,
               data_inputs: tuple[str, ...] | list[str] | None = None) -> dict:
        """Append one event study and return the program view *including* it."""
        return self.record_look(
            spec_hash_value=spec_hash(spec),
            spec_json={k: getattr(spec, k) for k in HASH_FIELDS},
            pool=spec.pool, n_variants=n_variants, n_trades=n_trades,
            p_value=p_value, effect=effect, verdict=verdict,
            window=(spec.start, spec.end), data_fingerprint=data_fingerprint,
            code_fingerprint=code_fingerprint, config_fingerprint=config_fingerprint,
            data_inputs=data_inputs, family=family, source=source)

    def fingerprint_history(self, spec_hash_value: str) -> list[dict]:
        """Every distinct data identity this spec has been judged against.

        More than one row means the numbers are not comparable across them, even
        though the spec hash is identical. That is the whole point of the column:
        without it, "the effect changed" is an unfalsifiable claim.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT data_fingerprint AS fingerprint, COUNT(*) AS n,
                       MIN(created_at) AS first, MAX(created_at) AS last,
                       MIN(effect) AS effect_min, MAX(effect) AS effect_max,
                       MIN(n_trades) AS trades_min, MAX(n_trades) AS trades_max
                FROM trials
                WHERE spec_hash=? AND data_fingerprint IS NOT NULL
                GROUP BY data_fingerprint
                -- created_at is second-resolution, so two looks recorded in the
                -- same second would tie. The row id is the real ordering.
                ORDER BY MAX(id) DESC
                """,
                (spec_hash_value,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_bucket(self, *, bucket: str, horizon: int, window: tuple[str, str],
                      p_value: float | None, effect: float | None,
                      n_episodes: int | None = None, verdict: str = "",
                      family: str = DEFAULT_FAMILY,
                      source: str = "bucket") -> dict:
        """A conditional-distribution test is a look, and one of the cheapest."""
        return self.record_look(
            spec_hash_value=bucket_hash(bucket, horizon, window),
            spec_json={"kind": "bucket", "bucket": bucket, "horizon": horizon,
                       "window": list(window), "episodes": n_episodes},
            pool=f"state:{bucket}", n_variants=1, n_trades=n_episodes,
            p_value=p_value, effect=effect, verdict=verdict,
            window=window, family=family, source=source)

    def record_overlay(self, base: EventStudy, *, rule: str, params: dict,
                       p_value: float | None, effect: float | None,
                       n_trades: int | None = None, n_fired: int | None = None,
                       p_one_sided: float | None = None, verdict: str = "",
                       family: str = DEFAULT_FAMILY,
                       source: str = "overlay") -> dict:
        """An overlay comparison is a look on the same book, counted as one search."""
        return self.record_look(
            spec_hash_value=overlay_hash(base, rule, params),
            spec_json={"kind": "overlay", "rule": rule, "params": params,
                       "base_pool": base.pool, "base_window": [base.start, base.end],
                       "n_fired": n_fired, "p_one_sided": p_one_sided},
            pool=f"{base.pool}/{rule}", n_variants=1, n_trades=n_trades,
            p_value=p_value, effect=effect, verdict=verdict,
            window=(base.start, base.end), family=family, source=source)

    # ------------------------------------------------------------------- read
    def trials_total(self, family: str = DEFAULT_FAMILY) -> int:
        with self._conn() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM trials WHERE family=?", (family,)).fetchone()[0])

    def variants_total(self, family: str = DEFAULT_FAMILY) -> int:
        """Searches, not runs: each run's grid is itself many trials.

        This is the number the Deflated Sharpe ratio needs. Nine hypotheses with
        an 8-variant grid each are 72 searches, not 9.
        """
        with self._conn() as conn:
            return int(conn.execute(
                "SELECT COALESCE(SUM(n_variants), 0) FROM trials WHERE family=?",
                (family,)).fetchone()[0])

    def looks(self, spec: EventStudy, family: str = DEFAULT_FAMILY) -> int:
        with self._conn() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM trials WHERE family=? AND spec_hash=?",
                (family, spec_hash(spec))).fetchone()[0])

    def by_source(self, family: str = DEFAULT_FAMILY) -> dict[str, int]:
        """Where the family's looks came from. A family that is 90% bucket
        tests should say so: those are the ones nobody thinks to count."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) AS n FROM trials WHERE family=? "
                "GROUP BY source ORDER BY n DESC", (family,)).fetchall()
        return {row["source"]: int(row["n"]) for row in rows}

    def p_values(self, family: str = DEFAULT_FAMILY) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, spec_hash, p_value, effect, verdict, n_looks, created_at "
                "FROM trials WHERE family=? AND p_value IS NOT NULL ORDER BY id",
                (family,)).fetchall()
        return [dict(r) for r in rows]

    def program_status(self, family: str = DEFAULT_FAMILY,
                       spec: EventStudy | None = None, *,
                       hash_value: str | None = None) -> dict:
        """Family-level FDR view. With ``spec`` or ``hash_value``, reports that
        trial's adjusted p."""
        rows = self.p_values(family)
        pvals = [float(r["p_value"]) for r in rows]
        adjusted = bh_adjust(pvals)
        n = len(pvals)
        min_p = min(pvals) if pvals else None
        min_adjusted = min(adjusted) if adjusted else None
        mine = None
        mine_p = None
        target = hash_value or (spec_hash(spec) if spec is not None else None)
        if target is not None and rows:
            for row, adj in zip(rows, adjusted, strict=True):
                if row["spec_hash"] == target:
                    mine = adj
                    mine_p = float(row["p_value"])
        # Superseded trials stay in every number above — they are looks that
        # happened — but the count of them is the one figure that says whether
        # the desk corrects itself or just accumulates.
        counts = self.supersession_counts(family)
        dead = set(counts["ids"])
        dead_effects = [float(r["effect"]) for r in rows
                        if r["id"] in dead and r["effect"] is not None]
        surviving = [r["id"] for r, a in zip(rows, adjusted, strict=True) if a <= 0.10]
        return {
            "family": family,
            "trials_total": self.trials_total(family),
            "p_values_total": n,
            "variants_total": self.variants_total(family),
            "by_source": self.by_source(family),
            "min_p": None if min_p is None else round(min_p, 6),
            "program_fdr": None if min_adjusted is None else round(min_adjusted, 6),
            # BH at q=0.10 is the desk's "worth a shadow book" line; q=0.05 is
            # the conservative read. Both are reported so neither is implied.
            "survivors_q10": int(sum(1 for a in adjusted if a <= 0.10)),
            "survivors_q05": int(sum(1 for a in adjusted if a <= 0.05)),
            "p_adjusted": mine,
            "this_p": mine_p,
            "superseded": counts["superseded"],
            "live": counts["live"],
            "superseded_share": counts["share"],
            "survivors_live": len([i for i in surviving if i not in dead]),
            "superseded_effect_mean": (round(float(sum(dead_effects) / len(dead_effects)), 6)
                                       if dead_effects else None),
            # Said out loud because the tempting "cleanup" is to drop these rows
            # and shrink the denominator.
            "denominator_includes_superseded": True,
        }

    def snapshot(self, family: str = DEFAULT_FAMILY) -> dict:
        """A JSON-safe picture of the family, for versioning outside the database.

        The live ledger is a gitignored SQLite file, and a ledger that can be
        deleted is one whose denominator resets to zero — the flattering
        direction. Exporting makes the counts survive the file, and makes a
        *shrinking* family visible in a diff, which is the only thing in the
        snapshot worth reading.
        """
        status = self.program_status(family)
        rows = self.list(limit=10_000, family=family)
        labels = self.supersessions(family)
        return {
            "family": family,
            "trials_total": status["trials_total"],
            "variants_total": status["variants_total"],
            "by_source": status["by_source"],
            "min_p": status["min_p"],
            "program_fdr": status["program_fdr"],
            "survivors_q10": status["survivors_q10"],
            "survivors_q05": status["survivors_q05"],
            "superseded": status["superseded"],
            "live": status["live"],
            "superseded_share": status["superseded_share"],
            "superseded_effect_mean": status["superseded_effect_mean"],
            "survivors_live": status["survivors_live"],
            "denominator_includes_superseded": True,
            "trials": [
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "source": r["source"],
                    "pool": r["pool"],
                    "spec_hash": r["spec_hash"],
                    "spec": json.loads(r["spec_json"]),
                    "n_variants": r["n_variants"],
                    "n_looks": r["n_looks"],
                    "n_trades": r["n_trades"],
                    "p_value": r["p_value"],
                    "effect": r["effect"],
                    "verdict": r["verdict"],
                    "window": [r["window_start"], r["window_end"]],
                    "data_fingerprint": r["data_fingerprint"],
                    "code_fingerprint": r["code_fingerprint"],
                    "config_fingerprint": r["config_fingerprint"],
                    "data_inputs": (json.loads(r["data_inputs"])
                                    if r["data_inputs"] is not None else None),
                    "superseded_by": (labels.get(r["id"]) or {}).get("superseded_by"),
                    "superseded_reason": (labels.get(r["id"]) or {}).get("reason"),
                }
                for r in reversed(rows)          # oldest first reads as a history
            ],
        }

    # ------------------------------------------------------------- supersede
    def _trial(self, conn: sqlite3.Connection, trial_id: int, family: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT id, family, spec_hash, p_value, effect, verdict FROM trials "
            "WHERE id=? AND family=?", (int(trial_id), family)).fetchone()
        if row is None:
            raise ValueError(f"trial {trial_id} 不在 family={family} 里")
        return row

    def supersede(self, trial_id: int, *, by: int, reason: str,
                  family: str = DEFAULT_FAMILY) -> dict:
        """Mark one trial as overturned by another. Nothing is deleted.

        Deliberately not a delete and not a filter on the denominator: the look
        happened, so it still counts toward the Deflated Sharpe. What changes is
        that it stops being a candidate. A ledger that drops its falsified rows
        is not a ledger, it is a highlight reel — and the number it would make
        look better is exactly the one it should not.

        ``reason`` is required. A supersession without a reason is a deletion
        with extra steps.
        """
        text = str(reason or "").strip()
        if not text:
            raise ValueError("supersede 需要一个理由：没有理由的回收就是删除")
        with self._lock, self._conn() as conn:
            self._trial(conn, trial_id, family)
            self._trial(conn, by, family)
            if int(by) == int(trial_id):
                raise ValueError("一条 trial 不能推翻自己")
            latest = self._latest(conn, family)
            replacement = latest.get(int(by))
            if replacement is not None and replacement["superseded_by"] is not None:
                raise ValueError("superseded_by 必须是 live trial，不能在回收链上形成环")
            current = latest.get(int(trial_id))
            if current and current["superseded_by"] == int(by):
                return {"trial_id": int(trial_id), "superseded_by": int(by),
                        "changed": False, "reason": current["reason"],
                        **self.program_status(family)}
            conn.execute(
                "INSERT INTO supersessions(created_at, family, trial_id, superseded_by, "
                "reason) VALUES (?,?,?,?,?)",
                (datetime.now(UTC).isoformat(timespec="seconds"), family,
                 int(trial_id), int(by), text))
        return {"trial_id": int(trial_id), "superseded_by": int(by), "changed": True,
                "reason": text, **self.program_status(family)}

    def unsupersede(self, trial_id: int, *, reason: str,
                    family: str = DEFAULT_FAMILY) -> dict:
        """Withdraw a supersession. Appends a row with ``superseded_by = NULL``.

        A reason is required here too: "it is live again" is a claim about the
        evidence, and the next reader needs to know which evidence.
        """
        text = str(reason or "").strip()
        if not text:
            raise ValueError("撤销回收也要理由")
        with self._lock, self._conn() as conn:
            self._trial(conn, trial_id, family)
            current = self._latest(conn, family).get(int(trial_id))
            if current is None or current["superseded_by"] is None:
                return {"trial_id": int(trial_id), "superseded_by": None,
                        "changed": False, "reason": text,
                        **self.program_status(family)}
            conn.execute(
                "INSERT INTO supersessions(created_at, family, trial_id, superseded_by, "
                "reason) VALUES (?,?,?,?,?)",
                (datetime.now(UTC).isoformat(timespec="seconds"), family,
                 int(trial_id), None, text))
        return {"trial_id": int(trial_id), "superseded_by": None, "changed": True,
                "reason": text, **self.program_status(family)}

    def _latest(self, conn: sqlite3.Connection, family: str) -> dict[int, dict]:
        rows = conn.execute(
            "SELECT s.trial_id, s.superseded_by, s.reason, s.created_at "
            "FROM supersessions s JOIN (SELECT trial_id, MAX(id) AS mid FROM "
            "supersessions WHERE family=? GROUP BY trial_id) m ON s.id = m.mid",
            (family,)).fetchall()
        return {int(r["trial_id"]): {"superseded_by": (None if r["superseded_by"] is None
                                                       else int(r["superseded_by"])),
                                     "reason": r["reason"],
                                     "created_at": r["created_at"]} for r in rows}

    def supersessions(self, family: str = DEFAULT_FAMILY) -> dict[int, dict]:
        """Current label per trial: who overturned it, and on what grounds."""
        with self._conn() as conn:
            return self._latest(conn, family)

    def supersession_counts(self, family: str = DEFAULT_FAMILY) -> dict:
        state = self.supersessions(family)
        overturned = sorted(t for t, v in state.items() if v["superseded_by"] is not None)
        total = self.trials_total(family)
        return {
            "total": total,
            "superseded": len(overturned),
            "live": total - len(overturned),
            "share": round(len(overturned) / total, 4) if total else 0.0,
            "ids": overturned,
        }

    def live_trials(self, family: str = DEFAULT_FAMILY) -> list[dict]:
        """Trials still standing. The denominator is *not* this list."""
        dead = set(self.supersession_counts(family)["ids"])
        return [r for r in self.list(limit=10_000, family=family) if r["id"] not in dead]

    def list(self, limit: int = 50, family: str | None = None) -> list[dict]:
        query = "SELECT * FROM trials"
        args: list = []
        if family:
            query += " WHERE family=?"
            args.append(family)
        query += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(query, args).fetchall()]
