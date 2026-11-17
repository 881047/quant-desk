"""The real account, kept strictly apart from the paper book.

Two reasons it cannot live in ``paper.sqlite``:

* the paper book is a *measurement*. Twenty closed trades decide whether the
  simulated strategy has an edge; dropping a real fill into it would corrupt the
  sample and the resulting number would answer neither question.
* this module is read-only on purpose. Reading a broker's statement is
  bookkeeping. Generating orders from a script is 程序化交易, and since the
  2026-08 接入规范 that channel is institutional-only — so nothing here writes
  an order, and nothing here holds a credential.

What it does: take trades and position snapshots from whatever the broker
exports, dedupe them by content, and answer three questions — what do I hold,
what is it worth, which discipline level has been hit.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from service.kernel.clock import SHANGHAI

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_key TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    trade_time TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    amount REAL,
    fee REAL DEFAULT 0,
    broker_id TEXT,
    source TEXT,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trade_date);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

CREATE TABLE IF NOT EXISTS position_snapshot (
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty INTEGER NOT NULL,
    available INTEGER,
    cost REAL,
    market_value REAL,
    source TEXT,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (as_of, symbol)
);

-- The snapshot's own record, including the days when it has no rows at all.
-- "Sold everything" and "never synced" both leave position_snapshot empty, and
-- reading the second as the first puts sold-out positions back on the panel.
CREATE TABLE IF NOT EXISTS snapshot_meta (
    as_of TEXT PRIMARY KEY,
    rows INTEGER DEFAULT 0,
    source TEXT DEFAULT '',
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS day (
    as_of TEXT PRIMARY KEY,
    cash REAL,
    market_value REAL,
    equity REAL,
    source TEXT,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    dedupe_key TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    rule TEXT NOT NULL,
    symbol TEXT,
    severity TEXT,
    title TEXT,
    claimed_at TEXT NOT NULL,
    sent_at TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL,
    file_sha TEXT NOT NULL,
    profile TEXT,
    rows INTEGER,
    inserted INTEGER,
    duplicates INTEGER,
    status TEXT,
    note TEXT,
    imported_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def normalize_symbol(raw: Any) -> str:
    """`000700` / `SZ000700` / `000700.SZ` → `000700.SZ`; anything else → ''."""
    text = str(raw or "").strip().upper().replace(" ", "")
    if not text:
        return ""
    for prefix in ("SH", "SZ", "BJ"):
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            text = text[len(prefix):] + "." + prefix
            break
    if "." in text:
        code, _, market = text.partition(".")
        if len(code) == 6 and code.isdigit() and market in ("SH", "SZ", "BJ"):
            return f"{code}.{market}"
        return ""
    if len(text) == 6 and text.isdigit():
        from service.adapters.text import to_ts_code

        return to_ts_code(text)
    return ""


def normalize_date(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def normalize_side(raw: Any) -> str:
    """``BUY`` / ``SELL`` from a broker's wording, or ``""`` when ambiguous.

    Order matters: an unambiguous marker wins over a single letter. The old
    version tested ``"B" in text`` together with ``"BUY"``, so a statement row
    reading ``SELL B`` (B-share sale, or a column that puts the board after the
    side) came back as a **buy** — the position doubled and the cash moved the
    other way. A bare letter is now only read as a side when it is a standalone
    token.
    """
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if "卖" in text or "SELL" in text or "SALE" in text:
        return "SELL"
    if "买" in text or "BUY" in text or "PURCHASE" in text:
        return "BUY"
    tokens = {token for token in re.split(r"[^A-Z]+", text) if token}
    if "B" in tokens and "S" not in tokens:
        return "BUY"
    if "S" in tokens and "B" not in tokens:
        return "SELL"
    return ""


def trade_key(row: dict) -> str:
    """Identity of one fill, independent of the file it arrived in.

    Brokers disagree on everything except the fill itself, so the key is built
    from the numbers a re-export cannot change: date, symbol, side, qty, price
    and the broker's own id when it has one. Re-importing yesterday's statement
    must be a no-op, otherwise every import doubles the position.
    """
    parts = [
        str(row.get("trade_date") or ""),
        str(row.get("trade_time") or ""),
        str(row.get("symbol") or ""),
        str(row.get("side") or ""),
        str(int(row.get("qty") or 0)),
        f"{float(row.get('price') or 0):.4f}",
        str(row.get("broker_id") or ""),
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


@dataclass
class Holding:
    symbol: str
    qty: int = 0
    cost_total: float = 0.0     # price × qty + fees: what the position cost
    entry_total: float = 0.0    # price × qty only: where the market has to go
    realized: float = 0.0
    fees: float = 0.0

    @property
    def cost(self) -> float:
        """Fee-inclusive break-even per share."""
        return self.cost_total / self.qty if self.qty else 0.0

    @property
    def avg_price(self) -> float:
        """Raw average entry. Stops anchor here, not on the fee-inclusive cost:
        a stop is a price the market trades at, and making it depend on your
        commission schedule means two accounts hold the same stock with two
        different stops."""
        return self.entry_total / self.qty if self.qty else 0.0


@dataclass
class ExternalStore:
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write lock and one commit for a multi-table ledger operation."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def _read_conn(self, conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if conn is not None:
            yield conn
            return
        with self._conn() as own:
            yield own

    @contextmanager
    def _write_conn(self, conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if conn is not None:
            yield conn
            return
        with self._lock, self._conn() as own:
            yield own

    # ---------------------------------------------------------------- trades
    def add_trades(self, rows: Iterable[dict], *, source: str = "",
                   conn: sqlite3.Connection | None = None) -> dict:
        inserted = duplicates = 0
        stamp = _now()
        with self._write_conn(conn) as db:
            for row in rows:
                key = row.get("trade_key") or trade_key(row)
                cur = db.execute(
                    """INSERT OR IGNORE INTO trades
                       (trade_key, trade_date, trade_time, symbol, side, qty, price,
                        amount, fee, broker_id, source, imported_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, row["trade_date"], row.get("trade_time") or "", row["symbol"],
                     row["side"], int(row["qty"]), float(row["price"]),
                     float(row.get("amount") or 0.0), float(row.get("fee") or 0.0),
                     row.get("broker_id") or "", source, stamp),
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    duplicates += 1
        return {"inserted": inserted, "duplicates": duplicates}

    def forget(self, *, source: str) -> int:
        """Delete fills that came from one source.

        A hand-entered fill and the same fill arriving later in a broker export
        have different broker ids, so the content hash does not catch them and
        the position doubles. The manual rows have to be retired explicitly
        before the first real statement is imported.
        """
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM trades WHERE source = ?", (source,))
        return int(cur.rowcount)

    def trades(self, since: str | None = None, *, until: str | None = None,
               conn: sqlite3.Connection | None = None) -> list[dict]:
        sql = "SELECT * FROM trades"
        clauses: list[str] = []
        args: list[str] = []
        if since:
            clauses.append("trade_date >= ?")
            args.append(since)
        if until:
            clauses.append("trade_date <= ?")
            args.append(until)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY trade_date, trade_time, rowid"
        with self._read_conn(conn) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def holdings(self, *, until: str | None = None,
                 conn: sqlite3.Connection | None = None) -> dict[str, Holding]:
        """Replay every fill into a weighted-average cost book.

        A-share 摊薄成本: buys add price×qty plus fees; sells remove a
        proportional slice of the cost and book the rest as realised. Sells
        beyond the holding are ignored (a short position is not something this
        account can hold) but counted, so a broken statement shows up.
        """
        out: dict[str, Holding] = {}
        for row in self.trades(until=until, conn=conn):
            held = out.setdefault(row["symbol"], Holding(symbol=row["symbol"]))
            qty = int(row["qty"])
            price = float(row["price"])
            fee = float(row.get("fee") or 0.0)
            held.fees += fee
            if row["side"] == "BUY":
                held.qty += qty
                held.cost_total += price * qty + fee
                held.entry_total += price * qty
            else:
                qty = min(qty, held.qty)  # never book a short
                if qty <= 0:
                    continue
                unit = held.cost_total / held.qty if held.qty else price
                raw = held.entry_total / held.qty if held.qty else price
                held.realized += (price - unit) * qty - fee
                held.qty -= qty
                held.cost_total -= unit * qty
                held.entry_total -= raw * qty
        return {k: v for k, v in out.items() if v.qty or v.realized}

    def holding_book(self, *, conn: sqlite3.Connection | None = None) -> dict[str, Holding]:
        """What the account holds, from the manual snapshot when there is one.

        The snapshot is the book the user maintains by hand; the ledger is the
        history of fills they chose to record. Those two agree only if every fill
        is entered, and the monitor should watch what the person believes they
        own — not a replay that silently misses the trades nobody typed in.
        """
        day = self.snapshot_day(conn=conn)
        if not day:
            return {k: v for k, v in self.holdings(conn=conn).items() if v.qty > 0}
        out: dict[str, Holding] = {}
        for symbol, row in self.snapshot(day, conn=conn).items():
            qty = int(row["qty"])
            if qty <= 0:
                continue
            cost = float(row.get("cost") or 0.0)
            out[symbol] = Holding(symbol=symbol, qty=qty,
                                  cost_total=cost * qty, entry_total=cost * qty)
        return out

    # -------------------------------------------------------------- snapshot
    def save_snapshot(self, as_of: str, rows: Iterable[dict], *, source: str = "") -> int:
        stamp = _now()
        count = 0
        rows = list(rows)
        with self._lock, self._conn() as conn:
            for row in rows:
                conn.execute(
                    """INSERT OR REPLACE INTO position_snapshot
                       (as_of, symbol, qty, available, cost, market_value, source, imported_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (as_of, row["symbol"], int(row["qty"]),
                     int(row["qty"] if row.get("available") is None else row["available"]),
                     float(row.get("cost") or 0.0),
                     float(row.get("market_value") or 0.0), source, stamp),
                )
                count += 1
            conn.execute(
                """INSERT OR REPLACE INTO snapshot_meta (as_of, rows, source, imported_at)
                   VALUES (?,?,?,?)""",
                (as_of, count, source, stamp))
        return count

    def replace_snapshot(self, as_of: str, rows: Iterable[dict], *,
                         source: str = "", conn: sqlite3.Connection | None = None) -> int:
        """Write today's holdings *as a whole*, replacing whatever was there.

        An upsert cannot express "I no longer hold this": after a full sell the
        row simply stops being written, so the old quantity stays in the table
        and the panel keeps showing a position that was closed. The snapshot is
        a statement about one moment, not an accumulation. Every row is converted
        before the delete, and the delete, inserts and metadata share one commit.
        """
        stamp = _now()
        values: list[tuple] = []
        seen: set[str] = set()
        for row in rows:
            symbol = str(row["symbol"])
            if not symbol or symbol in seen:
                raise ValueError(f"持仓快照包含空代码或重复代码：{symbol!r}")
            seen.add(symbol)
            values.append(
                (as_of, symbol, int(row["qty"]),
                 int(row["qty"] if row.get("available") is None else row["available"]),
                 float(row.get("cost") or 0.0),
                 float(row.get("market_value") or 0.0), source, stamp),
            )
        with self._write_conn(conn) as db:
            db.execute("DELETE FROM position_snapshot WHERE as_of = ?", (as_of,))
            db.executemany(
                """INSERT INTO position_snapshot
                   (as_of, symbol, qty, available, cost, market_value, source, imported_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                values,
            )
            db.execute(
                """INSERT OR REPLACE INTO snapshot_meta (as_of, rows, source, imported_at)
                   VALUES (?,?,?,?)""",
                (as_of, len(values), source, stamp))
        return len(values)

    def snapshot_day(self, as_of: str | None = None, *,
                     conn: sqlite3.Connection | None = None) -> str | None:
        """Which day the snapshot describes, or None when none was ever written.

        Both tables have to be consulted: ``snapshot_meta`` knows about a day
        whose holdings are empty (sold everything), ``position_snapshot`` knows
        about days written before that table existed. Asking only one of them
        made the panel and the reconciler disagree about which snapshot is
        "current" — the panel showed no holdings while the reconciler reported
        a 200-share mismatch against a different day's rows.
        """
        with self._read_conn(conn) as db:
            if as_of:
                if db.execute("SELECT 1 FROM snapshot_meta WHERE as_of = ?",
                              (as_of,)).fetchone():
                    return as_of
                legacy = db.execute(
                    "SELECT 1 FROM position_snapshot WHERE as_of = ? LIMIT 1",
                    (as_of,)).fetchone()
                return as_of if legacy else None
            row = db.execute(
                """SELECT MAX(d) AS d FROM (
                       SELECT MAX(as_of) AS d FROM snapshot_meta
                       UNION ALL
                       SELECT MAX(as_of) AS d FROM position_snapshot)""").fetchone()
            return (row["d"] if row and row["d"] else None) or None

    def snapshot(self, as_of: str | None = None, *,
                 conn: sqlite3.Connection | None = None) -> dict[str, dict]:
        day = as_of or self.snapshot_day(conn=conn)
        if not day:
            return {}
        with self._read_conn(conn) as db:
            return {r["symbol"]: dict(r) for r in db.execute(
                "SELECT * FROM position_snapshot WHERE as_of = ?", (day,))}

    def save_day(self, as_of: str, *, cash: float, market_value: float,
                 equity: float, source: str = "",
                 conn: sqlite3.Connection | None = None) -> None:
        with self._write_conn(conn) as db:
            db.execute(
                """INSERT OR REPLACE INTO day (as_of, cash, market_value, equity, source, imported_at)
                   VALUES (?,?,?,?,?,?)""",
                (as_of, cash, market_value, equity, source, _now()))

    def days(self, limit: int = 30, *,
             conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._read_conn(conn) as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM day ORDER BY as_of DESC LIMIT ?", (limit,))]

    # ---------------------------------------------------------------- import
    def log_import(self, *, file_name: str, file_sha: str, profile: str, rows: int,
                   inserted: int, duplicates: int, status: str, note: str = "",
                   conn: sqlite3.Connection | None = None) -> None:
        with self._write_conn(conn) as db:
            db.execute(
                """INSERT INTO imports
                   (file_name, file_sha, profile, rows, inserted, duplicates, status,
                    note, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (file_name, file_sha, profile, rows, inserted, duplicates, status,
                 note, _now()))

    def seen_sha(self, file_sha: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM imports WHERE file_sha = ? AND status = 'ok' LIMIT 1",
                (file_sha,)).fetchone()
        return bool(row)

    # ---------------------------------------------------------------- alerts
    def claim_alert(self, *, key: str, day: str, rule: str, symbol: str,
                    severity: str, title: str) -> bool:
        """Take ownership of one alert for one day.

        `INSERT OR IGNORE` on the dedupe key is what stops a monitor that runs
        every five minutes from sending the same breach six times an hour. The
        first run that inserts wins; every later run is told to stay quiet.
        """
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO alerts
                   (dedupe_key, day, rule, symbol, severity, title, claimed_at, status)
                   VALUES (?,?,?,?,?,?,?, 'claimed')""",
                (key, day, rule, symbol, severity, title, _now()))
        return bool(cur.rowcount)

    def mark_alert_sent(self, keys: list[str]) -> None:
        if not keys:
            return
        with self._lock, self._conn() as conn:
            conn.executemany(
                "UPDATE alerts SET sent_at = ?, status = 'sent' WHERE dedupe_key = ?",
                [(_now(), key) for key in keys])

    def release_alerts(self, keys: list[str], note: str = "") -> int:
        """Delete only unsent claims so their unique keys can be claimed again.

        A sent row is permanent evidence and is never released, even if a caller
        accidentally includes its key after a delivery result races a retry.
        ``note`` remains in the signature for callers of the old status-update
        API; the failed claim itself is removed rather than left to block retries.
        """
        if not keys:
            return 0
        with self._lock, self._conn() as conn:
            cur = conn.executemany(
                """DELETE FROM alerts
                   WHERE dedupe_key = ? AND sent_at IS NULL
                     AND status IN ('claimed', 'failed')""",
                [(key,) for key in keys])
        return int(cur.rowcount)

    def alerts(self, day: str | None = None, limit: int = 30) -> list[dict]:
        sql = "SELECT * FROM alerts"
        args: tuple = ()
        if day:
            sql += " WHERE day = ?"
            args = (day,)
        sql += " ORDER BY claimed_at DESC LIMIT ?"
        args = args + (limit,)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def imports(self, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM imports ORDER BY id DESC LIMIT ?", (limit,))]


# --------------------------------------------------------------------- levels
def limit_pct(symbol: str, *, st: bool = False, name: str | None = None) -> float:
    """The daily band, from the one implementation in ``adapters.symbols``.

    This used to be a fourth copy of the rule and it had drifted: the 920xxx
    Beijing range was missing (a 30% band was read as 10%) and ``st=True``
    returned 5% for ChiNext/STAR names, which keep 20%. Both errors turn into
    false "跌停" alerts in the monitor.

    ``name`` carries the short name because the band depends on one thing the
    code alone cannot say: 未完成股改 (S-prefixed) listings are capped at 5%
    whatever else they are, even after their ST label is gone.
    """
    from service.adapters.symbols import limit_pct as _band

    return float(_band(symbol, is_st=st, name=name))


def levels(*, symbol: str, qty: int, cost: float, prev_close: float,
           last: float | None = None, st: bool = False, entry: float | None = None,
           opened_today: bool = False, auction_anchor: float | None = None,
           auction_drop: float = 0.02, stop_drop: float = 0.05,
           name: str | None = None, stop_override: float | None = None) -> dict:
    """The lines a human will act on, computed the same way every day.

    Two different anchors on purpose: the auction line is measured against the
    previous close (a gap is a gap), the stop against the entry cost (a loss is a
    loss). Using one anchor for both is how "I'm only down 2% today" and "I'm
    down 9% on the trade" end up in the same sentence.
    """
    pct = limit_pct(symbol, st=st, name=name)
    settle = float(last if last is not None else prev_close)
    entry_price = float(entry if entry is not None else cost)
    up_limit = round(prev_close * (1 + pct) + 1e-9, 2)
    down_limit = round(prev_close * (1 - pct) + 1e-9, 2)
    if stop_override is None:
        # configs/stops.json 是人钉下的价位，优先于 -5% 规则。/stops 与
        # scripts/monitor.py 都已按这个顺序取值，只有这里还在跑纯规则：
        # 于是同一只票在同一晚，dashboard 说 14.17/13.61，账本与告警说
        # 9.90/12.50。人是读 dashboard 的那一方，两条线里错的偏偏是这条。
        try:
            from service.trading.external_stops import load_overrides
            stop_override = (load_overrides().get(symbol) or {}).get("stop")
        except Exception:  # noqa: BLE001 - 读不到位价就退回规则，不影响其余字段
            stop_override = None
    stop = (round(float(stop_override) + 1e-9, 2)
            if stop_override is not None
            else round(entry_price * (1 - stop_drop) + 1e-9, 2))
    # The auction line is measured against the **latest close**, not the previous
    # one: tomorrow's gap is a gap from tonight's close. Anchoring on prev_close
    # produced 11.64 instead of the 12.81 the trading plan said, for the same
    # stock on the same evening — two components disagreeing about one level.
    anchor = float(auction_anchor if auction_anchor is not None else settle)
    auction = round(anchor * (1 - auction_drop) + 1e-9, 2)
    return {
        "symbol": symbol,
        "qty": int(qty),
        "cost": round(cost, 4),
        "prev_close": round(prev_close, 2),
        "last": round(settle, 2),
        "up_limit": up_limit,
        "down_limit": down_limit,
        "auction_line": auction,
        "stop": stop,
        "market_value": round(settle * qty, 2),
        # A position opened today cannot have earned the gap from yesterday's
        # close — that move happened before the fill. Measuring it from the
        # previous close would report a profit on the day of purchase, which is
        # the single most common way a daily P&L lies.
        "pnl_today": round((settle - (cost if opened_today else prev_close)) * qty, 2),
        "breakeven": round(cost, 4),
        "entry": round(entry_price, 4),
        "pnl_total": round((settle - cost) * qty, 2),
        "pnl_total_pct": round((settle / cost - 1) * 100, 2) if cost else None,
        "to_auction_line_pct": round((settle / auction - 1) * 100, 2) if auction else None,
        "to_stop_pct": round((settle / stop - 1) * 100, 2) if stop else None,
        "breach_auction": settle <= auction,
        "breach_stop": settle <= stop,
    }


def reconcile(store: ExternalStore, as_of: str | None = None) -> dict:
    """Fills vs the broker's own position snapshot.

    Dividends, 送股, 转股 and 配股 change a position without producing a fill, so a
    trade-only ledger drifts. The snapshot is the authority; this reports the
    gap instead of silently trusting either side.
    """
    # "No snapshot" and "a snapshot that holds nothing" are different facts.
    # Reading the empty one as missing hid a real 100-share disagreement: the
    # account was in cash, the ledger still carried the stock it had sold.
    day = as_of or store.snapshot_day()
    snap = store.snapshot(day) if day else {}
    held = store.holdings(until=day) if day else store.holdings()
    if not day:
        return {"ok": True, "as_of": None, "snapshot_missing": True, "mismatches": [],
                "note": ("还没有持仓快照，只核了成交流水。导出一次持仓表再 import，"
                         "才能发现分红送股/转股这类不产生成交的变动。")}
    rows = []
    for symbol in sorted(set(snap) | set(held)):
        broker_qty = int((snap.get(symbol) or {}).get("qty") or 0)
        ledger_qty = int(held.get(symbol).qty if held.get(symbol) else 0)
        if broker_qty != ledger_qty:
            rows.append({"symbol": symbol, "broker_qty": broker_qty,
                         "ledger_qty": ledger_qty, "delta": broker_qty - ledger_qty})
    return {"ok": not rows,
            "as_of": day,
            "snapshot_missing": False,
            "mismatches": rows,
            "note": ("成交流水不含分红送股/转股/配股；持仓快照才是权威。"
                     "有差异时以快照为准，并检查是否漏导了非交易过户。")}
