"""Paper book: one persistent account, executed through the deterministic kernel.

Three fill bases, and the label matters as much as the price:

``next_open`` (default)
    An approved ticket waits for the next session's open. A decision made after
    the close cannot be filled at that close - the price was already known when
    the decision was written - so the next open is the earliest honest price.
``close``
    Same-session fill at the closing price, kept for scenario tests. It names one
    session's close, so a ticket written before that close exists (pre-open,
    intraday, or before the 17:30 bar) is queued for that session rather than
    rejected: a missing print is not a market decision.
``intraday``
    Filled at the live quote, now. Only usable during a session and only against
    a real-time source; outside those conditions the ticket is queued for the
    next open instead of being refused, so a model woken at 20:00 can still act.

Intraday fills are *not* reproducible and are labelled as such: every record
carries ``fill_basis="intraday"`` plus its ``price_source`` and quote age, so a
later reader can tell which part of the track record a replay could not recover.

T+1 falls out of the kernel: a buy increases ``total_qty`` but not
``available_qty``; ``rollover()`` on the next trading day calls ``settle()``.
"""

from __future__ import annotations

import os
import threading
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from service import settings
from service.kernel.calendar import TradingCalendar
from service.kernel.clock import SHANGHAI
from service.kernel.costs import CostModel
from service.kernel.execution import BarExecutionModel, derive_fill_ratio
from service.kernel.ledger import Ledger
from service.kernel.models import (
    Bar,
    D,
    Fill,
    Intent,
    OrderStatus,
    OrderType,
    Session,
    Side,
    money,
)
from service.kernel.oms import _session_key

from .market import MarketRepository, Snapshot
from .state import TERMINAL_TICKET_STATUSES, PaperStore, StateConflict, utc_now

#: Queue kinds that bet on **one specific session's print**, and the fill basis
#: each records. All expire with the session they named, so all must be settled
#: before ``settle_pending`` is allowed to call them stale.
#:
#: ``close`` is here for the same reason as the other two: it names one session's
#: closing price, so it can only be settled after that session's bar exists.
SAME_SESSION_QUEUES = {"ceiling": "ceiling_queue", "open": "open_auction",
                       "close": "close"}

#: Returned by ``_ceiling_ratio`` when the data needed to judge the queue has
#: not landed. Distinct from ``None`` ("this order cannot queue at all"): one is
#: "wait for the next settle", the other is a terminal ``OPEN_AT_LIMIT`` cancel.
CEILING_WAITING = object()


def _fill_error(exc: ValueError) -> str:
    """``CODE: sentence`` for a rejected fill, ``str`` for anything else.

    ``FillRejected`` carries a stable code, and the ticket is where the reason
    lives; writing only the sentence made "why did this bounce" a parsing job.
    A plain ``ValueError`` (a bad ``t_plus``, a reused client id) has no code and
    keeps its message.
    """
    code = getattr(exc, "code", None)
    detail = getattr(exc, "detail", None)
    return f"{code}: {detail}" if code and detail else str(exc)

#: How many *closed* orders / historical fills the account payload carries.
#:
#: The snapshot is rewritten whole on every commit, so its size is a per-write
#: cost that grows with the book's lifetime: 300 closed orders measured 230 KB
#: and 0.9 ms of JSON per commit, on a path every proposal and every settlement
#: takes. Live orders are always kept in full — ``Ledger.from_snapshot``
#: reconciles reservations against them — and everything older stays in the
#: ``orders`` / ``paper_fills`` tables, which are the durable record.
HISTORY_KEEP = 200


def _state_mark(state: dict) -> dict:
    """The parts of ``state`` a fill/queue transition can change, by reference.

    A ``deepcopy`` here cost 6.2 ms at 300 orders and ran twice per settlement
    candidate — under the book lock, on every ``/portfolio``. Every writer in
    this class *rebinds* these keys or appends to the two lists, so capturing
    the references plus a shallow ``list``/``dict`` copy is an exact rollback
    and costs O(n) pointers instead of O(n) objects.

    A key that was **absent** stays absent: restoring an empty dict where there
    was no key at all changes the stored payload and would make "rolled back"
    and "never happened" two different books.

    Keep this list in step with ``_record_fill`` / ``queue_ticket`` /
    ``_track_case``: they are the only writers inside a rollback window.
    """
    mark: dict = {}
    for key in ("pending", "account", "fills", "sleeves", "strategy_of",
                "opens", "updated_at"):
        if key not in state:
            continue
        value = state[key]
        if isinstance(value, list):
            mark[key] = list(value)
        elif isinstance(value, dict):
            mark[key] = dict(value)
        else:
            mark[key] = value
    return mark


def _state_restore(state: dict, mark: dict) -> None:
    for key in ("pending", "account", "fills", "sleeves", "strategy_of",
                "opens", "updated_at"):
        if key in mark:
            state[key] = mark[key]
        else:
            state.pop(key, None)


def _reference_price(ticket: dict) -> Decimal | None:
    """The price the decision was made against, when the ticket recorded one.

    A queued ticket is written after the close and filled at the next open, so
    the prior close is the only honest benchmark for implementation shortfall.
    Missing or malformed values return None rather than a guess: a made-up
    reference would manufacture a cost that never happened.
    """
    raw = ticket.get("price_ref")
    if raw in (None, ""):
        return None
    try:
        value = D(str(raw))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return value if value.is_finite() and value > 0 else None


@dataclass
class ExecutionResult:
    ok: bool
    status: str
    reason: str = ""
    order_id: str = ""
    fill_id: str = ""
    qty: int = 0
    price: Decimal = D("0")
    fees: Decimal = D("0")
    cash_after: Decimal = D("0")
    equity_after: Decimal = D("0")


class _ReadOnlyStore:
    """Reads delegate to the durable store; every write is disabled.

    Audit records are deliberately dropped: a valuation preview did not perform
    the corporate action it is simulating, so writing one would be a false audit
    trail. State/execution writes fail loudly instead of pretending to succeed.
    """

    def __init__(self, store: PaperStore):
        self._store = store

    def log_decision(self, *_args, **_kwargs) -> None:
        return None

    def __getattr__(self, name: str):
        if name in {"save_state", "commit_ticket_state", "commit_execution"}:
            raise RuntimeError(f"read-only book view cannot call store.{name}")
        return getattr(self._store, name)


class PaperBook:
    #: A queued ticket dies after this many sessions without a bar. A suspended
    #: stock resuming three weeks later would otherwise execute a thesis nobody
    #: holds any more, at a limit set against a different price.
    QUEUE_TTL_SESSIONS = 3
    #: How many missed sessions of corporate actions to replay after downtime.
    MAX_CATCHUP_SESSIONS = 30

    def __init__(self, store: PaperStore, market: MarketRepository,
                 initial_cash: str | None = None, fill_mode: str | None = None,
                 quotes=None, guard=None, quotes_cached=None):
        self.store = store
        self.market = market
        self.lock = threading.RLock()
        # The pre-trade guard, injected rather than constructed here so that a
        # queued ticket is re-checked against the *current* account when it fills,
        # not only against the account it was proposed into. ``None`` is allowed
        # for unit tests of the fill path; ``build_desk`` always wires the real one.
        self.guard = guard
        # A callable symbol->Quote mapping. Injected rather than imported so the
        # book keeps no dependency on the quote service, and so tests can hand it
        # a fixed answer without going near the network.
        self.quotes = quotes
        # Cache-only quote reader, used by the fill-time guard. ``None`` keeps
        # the old behaviour (the guard may fetch), which is what unit tests with
        # a stub quote callable expect; production wires it to the quote cache.
        self.quotes_cached = quotes_cached
        # The moment a live fill happens. Injectable so a test can freeze it and
        # so the bar, the session check and the OMS all read the same clock -
        # three different notions of "now" inside one fill is a bug waiting.
        self.clock = lambda: datetime.now(SHANGHAI)
        self.cost = CostModel()
        self.fill_mode = fill_mode or os.getenv("FILL_MODE", "next_open")
        self.matcher = BarExecutionModel(cost_model=self.cost)
        payload, self._revision = store.load_state_with_revision()
        if payload is None:
            # None means "the configured account size", not a literal: the
            # opening balance is a property of the account, not of this class.
            payload = self._default_state(initial_cash or settings.initial_cash_text())
            self._revision = store.save_state(payload)
        self.state = payload
        self._mark_receipts: dict[str, dict] = {}
        account = payload["account"]
        orphaned_cash = D(account.get("cash_frozen", "0"))
        orphaned_shares = sum(
            int(row.get("frozen_qty", 0) or 0)
            for row in account.get("positions", {}).values()
        )
        if (orphaned_cash or orphaned_shares) and not account.get("order_records"):
            durable_orders = self.store.open_order_records()
            if durable_orders:
                account["order_records"] = durable_orders
            else:
                # Legacy snapshots never persisted the order itself. Releasing an
                # unprovable reservation is safer than deleting its cash/shares;
                # emit an audit fact instead of failing every subsequent restart.
                account["cash_available"] = str(D(account["cash_available"]) + orphaned_cash)
                account["cash_frozen"] = "0"
                for row in account.get("positions", {}).values():
                    frozen = int(row.get("frozen_qty", 0) or 0)
                    row["available_qty"] = min(
                        int(row["total_qty"]), int(row.get("available_qty", 0)) + frozen,
                    )
                    row["frozen_qty"] = 0
                self.store.log_decision("orphan_reservation_release", {
                    "cash": str(orphaned_cash), "shares": orphaned_shares,
                    "reason": "legacy snapshot has reservations but no durable order records",
                })
        self.ledger = self._ledger_from_snapshot(account)
        # The counter lives in two places on purpose: the account payload (so a
        # clean restart resumes) and the orders table (so a crash between the
        # fill and the save cannot hand out a number that is already taken).
        self.ledger.oms.seq = max(self.ledger.oms.seq, self.store.max_order_seq())

    # ------------------------------------------------------------------ state
    def _ledger_from_snapshot(self, account: dict) -> Ledger:
        ledger = Ledger.from_snapshot(account, self.cost)
        if account.get("settled_session") is None and self.state.get("day"):
            # Legacy snapshots did not persist the marker. The book's committed
            # day is written only after rollover settles, so it is the one safe
            # recovery source; without it the first restart could unlock today's
            # purchases once more.
            ledger.oms._settled_session = _session_key(self.state["day"])
        return ledger

    def _rollback_ledger(self, mark: dict) -> None:
        """Rebuild the ledger from the last committed account payload.

        The old rollback kept a ``deepcopy(ledger)`` per settlement candidate.
        The account payload in ``state`` *is* the last committed snapshot, so
        rebuilding from it is the same answer, costs nothing on the success path
        (which is every path), and cannot drift from what is on disk.

        ``from_snapshot`` also restores ``order_seq`` from the payload — the
        failed attempt's number was never written to the ``orders`` table, so
        reusing it is safe.
        """
        account = mark.get("account")
        if account is not None:
            self.ledger = self._ledger_from_snapshot(account)

    @staticmethod
    def _default_state(initial_cash: str) -> dict:
        return {
            "initial_cash": initial_cash,
            "day": "",
            "day_start_equity": initial_cash,
            "kill_switch": False,
            "safe_mode": False,
            "fills": [],
            # The queue exists from the first write. It used to be created by
            # the first *read* (``setdefault``), so "never queued anything" and
            # "rolled back to the beginning" were two different payloads, and a
            # conservation check that indexes the key failed on a book that was
            # simply new.
            "pending": [],
            "account": {
                "cash_available": initial_cash,
                "cash_frozen": "0",
                "cash_withdrawable": initial_cash,
                "realized_pnl": "0",
                "fees_paid": "0",
                "dividend_income": "0",
                "peak_equity": initial_cash,
                "positions": {},
            },
        }

    def _account_payload(self) -> dict:
        # Keep the production book on the same lossless snapshot as the kernel:
        # open orders, client idempotency keys, fills and peak equity all survive
        # restart instead of leaving an orphaned cash/share reservation behind.
        payload = self.ledger.snapshot()
        payload["positions"] = {
            symbol: {
                **row,
                **({"sleeve": sleeve} if (sleeve := getattr(
                    self.ledger.account.positions[symbol], "sleeve", None,
                )) is not None else {}),
            }
            for symbol, row in payload.get("positions", {}).items()
        }
        return self._bounded(payload)

    @staticmethod
    def _bounded(payload: dict) -> dict:
        """Keep every live order, the last ``HISTORY_KEEP`` closed ones.

        The snapshot is the write unit (``save_state`` rewrites the whole JSON),
        so an unbounded history makes every commit slower for the rest of the
        account's life. Nothing is lost: the ``orders`` / ``paper_fills`` tables
        keep the full record, and ``Ledger.from_snapshot`` only needs live
        reservations to reconcile — closed orders are carried for audit
        convenience and the restart path tolerates their absence.
        """
        open_statuses = {"PENDING_NEW", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}
        records = payload.get("order_records") or []
        live = [row for row in records if row.get("status") in open_statuses]
        closed = [row for row in records if row.get("status") not in open_statuses]
        kept = live + closed[-HISTORY_KEEP:]
        payload["order_records"] = kept
        # The legacy id→status map mirrors what is carried, so a reader cannot
        # see an order id in one field and not the other.
        payload["orders"] = {row["order_id"]: row["status"] for row in kept}
        fills = payload.get("fills") or []
        if len(fills) > HISTORY_KEEP:
            payload["fills"] = fills[-HISTORY_KEEP:]
        return payload

    def persist(self) -> None:
        with self.lock:
            self.state["account"] = self._account_payload()
            self.state["updated_at"] = utc_now()
            try:
                self._revision = self.store.save_state(self.state, self._revision)
            except StateConflict:
                # Another process (the 09:26 session prep, the 5-minute monitor,
                # the 17:30 daily job, a manual backfill) committed first. Our
                # in-memory copy is the stale one, and nothing in this class used
                # to re-read it: `_revision` only ever moved forward on a
                # successful write, so the next write conflicted too, forever,
                # until the service was restarted. Re-reading is the whole fix;
                # the caller still sees one 409 and its retry now succeeds.
                self.reload()
                raise

    def reload(self) -> int:
        """Re-read the durable book after another process committed.

        The rebuild mirrors ``__init__`` on purpose: a partially refreshed
        ledger (new account, old open orders) is worse than either state.
        """
        payload, revision = self.store.load_state_with_revision()
        if payload is None:
            return self._revision
        self.state = payload
        self._revision = revision
        self.ledger = Ledger.from_snapshot(payload["account"], self.cost)
        self.ledger.oms.seq = max(self.ledger.oms.seq, self.store.max_order_seq())
        self._mark_receipts = {}
        return revision

    # ------------------------------------------------------------------- day
    def read_view(self, today: str, *, live: bool = False) -> PaperBook:
        """Return a valued copy without advancing or writing the real account.

        Generic information reads still need current T+1 availability, corporate
        actions and price marks. They may not obtain those by calling the real
        ``rollover()``: that persists state, writes audit events and can settle
        queued orders into executions. This view previews only account
        maintenance. Pending orders stay pending because a read has no authority
        to turn them into fills. All store writes are disabled on the copy.
        """
        if not today:
            raise ValueError("read_view requires a trading date")
        with self.lock:
            view = copy(self)
            view.state = deepcopy(self.state)
            view.ledger = Ledger.from_snapshot(deepcopy(self._account_payload()), self.cost)
            view._revision = self._revision
        view.lock = threading.RLock()
        view.store = _ReadOnlyStore(self.store)
        view._mark_receipts = {}
        # _apply_corporate_actions calls persist/log_decision. The former is a
        # no-op on this private copy and the latter is dropped by _ReadOnlyStore.
        view.persist = lambda: None

        day = str(view.state.get("day") or "")
        if not day:
            view.state["day"] = str(today)
            view.state["day_start_equity"] = str(view.equity())
            view.state["actions_through"] = str(today)
        elif str(today) > day:
            view.ledger.settle(str(today))
            view.state["day"] = str(today)
            view.state["day_start_equity"] = str(view.equity())
            view.apply_corporate_actions(str(today))
        view.mark_prices(str(today), live=live)
        return view

    def rollover(self, today: str | None = None) -> str:
        """Settle the previous session once the trading day advances."""
        with self.lock:
            return self._rollover(today)

    def _rollover(self, today: str | None) -> str:
        today = today or self.market.last_trading_day()
        if not today:
            return self.state.get("day", "")
        if not self.state.get("day"):
            self.state["day"] = today
            self.state["day_start_equity"] = str(self.equity())
            self.persist()
            self.apply_corporate_actions(today)   # seeds the watermark only
            return today
        if today > self.state["day"]:
            self.ledger.settle(today)
            self.state["day"] = today
            # Yesterday's close is the right denominator for today's P&L, so it
            # is captured *before* the ex-date cash arrives: a dividend is part
            # of today's return, not a restatement of yesterday's.
            self.state["day_start_equity"] = str(self.equity())
            self.persist()
            self.apply_corporate_actions(today)
        # Order matters. A ceiling ticket carries ``expires_day = created_day``
        # because it is a bet on *that* session's board, and ``settle_pending``
        # treats anything past its expiry as dead. But the seal and open-count
        # that decide a ceiling fill are only published after 17:30, so the
        # settlement can only happen on the next rollover — which is exactly when
        # ``settle_pending`` would cancel it. Running the ceiling pass first gives
        # it its one chance; whatever it cannot settle (no bar, no pool) is then
        # expired by ``settle_pending`` as intended, instead of silently killing
        # every ceiling ticket before the only code that can fill it ever runs.
        self.settle_same_session_due(today)
        # Always try the queue, not only when the date moves: a bar that arrives
        # late in the day must still fill yesterday's tickets today, otherwise
        # they sit idle until the next rollover.
        self.settle_pending(today)
        return self.state["day"]

    def apply_corporate_actions(self, day: str) -> list[dict]:
        with self.lock:
            return self._apply_corporate_actions(day)

    def _apply_corporate_actions(self, day: str) -> list[dict]:
        """Credit dividends and bonus shares for every session since the last run.

        The stored bars are unadjusted, so on an ex-date the price simply drops by
        the payout. Without this, a position held across one shows a loss that
        never happened — and the stop, the drawdown breaker and the arena benchmark
        all believe it.

        ``actions_through`` is the idempotency key: one pass per session, and the
        *first* pass on an existing book starts counting from that day instead of
        retroactively paying dividends for positions opened before the feature
        existed.
        """
        done = str(self.state.get("actions_through") or "")
        if done and str(day) <= done:
            return []
        if not done:
            self.state["actions_through"] = str(day)
            self.persist()
            return []
        calendar = getattr(self.market, "calendar", None)
        between = getattr(calendar, "between", None)
        try:
            sessions = between(done, day) if between else [str(day)]
        except (TypeError, ValueError):
            sessions = [str(day)]
        # Bound one pass without dropping history. Taking the *last* N sessions
        # and then advancing the watermark to ``day`` permanently skipped every
        # earlier action. Take the oldest unprocessed sessions instead and leave
        # the watermark on the last one actually processed; the next call resumes
        # exactly after it, even when the requested day has not changed.
        pending_sessions = [d for d in sessions if done < d <= str(day)]
        sessions = pending_sessions[:self.MAX_CATCHUP_SESSIONS]
        if not sessions:
            return []
        processed_through = sessions[-1]
        applied: list[dict] = []
        for session in sessions:
            try:
                actions = self.market.actions(session)
            except (KeyError, FileNotFoundError, ValueError):
                actions = []
            for action in actions:
                pos = self.ledger.account.positions.get(action.symbol)
                if pos is None or pos.total_qty <= 0:
                    continue
                resolved = self._resolve_action(action, session)
                if resolved is None:
                    self.store.log_decision(
                        "corporate_action_mismatch",
                        {"symbol": action.symbol, "day": session,
                         "cash_div": str(action.cash_div_per_share),
                         "stock_div": str(action.stock_div_per_share + action.transfer_per_share),
                         "reason": "the tape did not drop by what the plan says"},
                        actor="system")
                    continue
                cash, ratio = resolved
                plan = replace(action, cash_div_per_share=cash,
                               stock_div_per_share=ratio, transfer_per_share=D("0"))
                row = plan.apply(self.ledger.account, oms=self.ledger.oms)
                if row.get("applied"):
                    row["day"] = session
                    applied.append(row)
            # A bond that reaches its last session stops existing. Without this
            # the position stays marked at a price nobody can trade, and every
            # downstream number (equity, drawdown, stops) inherits the fiction.
            # `getattr` because a stub market in a test (and any minimal
            # implementation) need not know about bonds at all.
            lookup = getattr(self.market, "cb_maturities", None)
            try:
                maturities = lookup(session) if lookup else []
            except (KeyError, FileNotFoundError, ValueError):
                maturities = []
            for event in maturities:
                # No position does not mean no lifecycle work: an open buy
                # freezes cash and must still expire on the delisting session.
                # Redemption happens after the last session, so any order still
                # working on the bond dies first. Without this the position is
                # zeroed while a sell order keeps its frozen shares, and the
                # snapshot written one line later fails its own consistency
                # check on the next boot.
                try:
                    close_ts = datetime.strptime(session, "%Y%m%d").replace(
                        hour=15, tzinfo=SHANGHAI)
                except ValueError:
                    close_ts = None
                row = event.apply(self.ledger.account, oms=self.ledger.oms,
                                  ts=close_ts)
                if row.get("applied"):
                    row["day"] = session
                    applied.append(row)
                elif row.get("reason") in {"OPEN_SELL_ORDERS",
                                           "FROZEN_SHARES_REMAIN"}:
                    # Nothing was zeroed; say so instead of losing the event.
                    self.store.log_decision(
                        "cb_maturity_deferred",
                        {"symbol": event.symbol, "day": session, **row},
                        actor="system")

            # Interest is a different event from redemption and runs on its own
            # schedule: a bond pays a coupon every year of its life, and only
            # then returns par. Skipping this made a held bond look like it paid
            # nothing between purchase and delisting.
            coupon_lookup = getattr(self.market, "cb_coupons", None)
            try:
                coupons = coupon_lookup(session) if coupon_lookup else []
            except (KeyError, FileNotFoundError, ValueError):
                coupons = []
            for coupon in coupons:
                row = coupon.apply(self.ledger.account)
                if row.get("applied"):
                    row["day"] = session
                    applied.append(row)
        self.state["actions_through"] = processed_through
        self.persist()
        if len(pending_sessions) > len(sessions):
            self.store.log_decision("corporate_action_catchup_pending", {
                "through": processed_through,
                "requested_day": str(day),
                "remaining_sessions": len(pending_sessions) - len(sessions),
            }, actor="system")
        for row in applied:
            self.store.log_decision("corporate_action", row, actor="system")
        return applied

    def _resolve_action(self, action, session: str):
        """Cash and share ratio that the *quote* supports, or ``None`` to skip.

        The dividend table and the unadjusted price series are two witnesses to
        the same event, and they disagree on about 3% of rows (a stale plan, a
        moved ex-date, a rights issue priced in). Paying a payout the price never
        dropped for would mint money; adding shares the price never diluted for
        would double count. So the tape arbitrates: it supplies the total
        adjustment and the plan only splits it into cash versus shares.
        """
        cash = action.cash_div_per_share
        ratio = action.stock_div_per_share + action.transfer_per_share
        pos = self.ledger.account.positions.get(action.symbol)
        prev = pos.last_price if pos is not None else D("0")
        snap = self.market.snapshot(action.symbol, session)
        if prev <= 0 or snap is None or snap.pre_close <= 0:
            return cash, ratio                     # no second opinion: trust the plan
        total = D("1") - snap.pre_close / prev     # share of value the event removed
        if total < D("-0.005") or cash / prev > total + D("0.005"):
            return None                            # payout larger than the drop
        implied = (prev - cash) / snap.pre_close - D("1")
        if implied > D("3"):
            return None                            # 10 送 30+ is not a real plan
        # A cash-only plan still leaves a rounding residue of a few ten-thousandths
        # in the implied ratio; on 1,000 shares that is a free share. Real bonus
        # plans start at 每股送 0.1, so anything under 0.02 is noise.
        return cash, (D("0") if implied < D("0.02") else implied)

    # ---------------------------------------------------------------- pricing
    def mark_prices(self, date: str | None = None, *, live: bool = False) -> Decimal:
        """Re-value the book and return equity.

        ``live=True`` asks the quote service for anything the stored bar cannot
        answer — during a session today's bar does not exist yet, and a stop
        distance computed from yesterday's close is not a risk measurement. The
        circuit breakers read ``last_price``, so whoever is about to decide
        whether the account may still take risk has to refresh it first.
        """
        date = date or self.state.get("day") or self.market.last_trading_day()
        prices: dict[str, Decimal] = {}
        missing: list[str] = []
        # The stale-price scan reads parquet and mutates `last_price`; the live
        # branch below calls the network. They are locked separately on purpose:
        # holding the book lock across an 8-second quote timeout would block every
        # other request on the desk (the same reason `_cached` builds outside its
        # condition variable).
        with self.lock:
            # Recorded so `equity()` can say which session its marks belong to.
            # The value is persisted with the rest of `state`, so a restart does
            # not make yesterday's prices look like today's.
            self.state["marked_day"] = date
            self._mark_receipts = {}
            for symbol, pos in self.ledger.account.positions.items():
                if pos.total_qty == 0:
                    continue
                snap = self.market.snapshot(symbol, date)
                if snap is not None and snap.last > 0:
                    prices[symbol] = snap.last
                    pos.last_price = snap.last
                    self._mark_receipts[symbol] = {
                        "price_source": "daily",
                        "price_as_of": getattr(snap, "trade_date", None),
                        "price_age_seconds": None,
                        "is_realtime": False,
                    }
                else:
                    missing.append(symbol)

        got: dict = {}
        if live and missing and self.quotes is not None:
            try:
                got = self.quotes(missing) or {}
            except (ValueError, KeyError, RuntimeError):
                got = {}

        with self.lock:
            for symbol in missing:
                quote = got.get(symbol)
                if quote is not None and quote.last > 0:
                    prices[symbol] = quote.last
                    self.ledger.account.position(symbol).last_price = quote.last
                    self._mark_receipts[symbol] = {
                        "price_source": getattr(quote, "source", "unknown"),
                        "price_as_of": getattr(quote, "quote_ts", None),
                        "price_age_seconds": getattr(quote, "age_seconds", None),
                        "is_realtime": getattr(quote, "is_realtime", None),
                    }
            return self.ledger.account.update_peak(prices)

    @property
    def marks_as_of(self) -> str:
        """The session the stored prices belong to, or "" when never marked."""
        return str(self.state.get("marked_day") or "")

    def mark_receipt(self, symbol: str) -> dict:
        """Provenance of the last mark, or an explicit unknown after restart."""
        return dict(self._mark_receipts.get(symbol, {
            "price_source": "book_mark_unknown",
            "price_as_of": None,
            "price_age_seconds": None,
            "is_realtime": None,
        }))

    def equity(self) -> Decimal:
        """Account equity at the most recent marks. A read, not a write.

        There used to be a ``date`` parameter here that was read and then never
        used: every caller believed it was asking "equity as of this session",
        and every answer was "equity at whatever ``mark_prices`` last wrote".
        Rather than keep a parameter that lies, the marks' own day is exposed as
        :attr:`marks_as_of` so a caller can see how old the valuation is.

        It also used to ratchet ``peak_equity`` — a read that quietly moved the
        drawdown baseline, and moved it only in memory: the new peak reached the
        database at the next ``persist()``, so a restart between a poll and a
        write put the peak back. Now ``mark_prices`` owns that write (it calls
        ``update_peak``), which means the high-water mark is sampled at the
        moments the desk deliberately looks at prices, not whenever someone
        happens to call a getter.
        """
        prices = {}
        for symbol, pos in tuple(self.ledger.account.positions.items()):
            if pos.total_qty == 0:
                continue
            if pos.last_price > 0:
                prices[symbol] = pos.last_price
        return self.ledger.account.equity(prices)

    def _fill_count(self) -> int:
        """Total fills ever booked, from the table once the window is full.

        ``state["fills"]`` is capped at ``HISTORY_KEEP`` so the snapshot stays
        bounded; reporting its length after that would quietly stop counting.
        """
        window = len(self.state.get("fills", []))
        if window < HISTORY_KEEP:
            return window
        try:
            return max(window, self.store.count_fills())
        except Exception:  # noqa: BLE001 - a count must not break the summary
            return window

    def actions_on(self, day: str) -> int:
        """How many fills were booked on a session, both sides.

        Counted from fills rather than tickets: a cancelled queue entry is not an
        action, and an approval that never reached the market should not consume
        the day's budget.
        """
        return sum(1 for f in self.state.get("fills", [])
                   if str(f.get("trade_date")) == str(day))

    def daily_pnl_pct(self) -> float:
        start = D(self.state.get("day_start_equity", "0"))
        if start <= 0:
            return 0.0
        return float(self.equity() / start - 1)

    def drawdown(self) -> float:
        acct = self.ledger.account
        if acct.peak_equity <= 0:
            return 0.0
        return float(acct.equity() / acct.peak_equity - 1)

    # -------------------------------------------------------------- execution
    def _fill_exists(self, ticket_id: str) -> dict | None:
        # `paper_fills.ticket_id` is a primary key; `state["fills"]` is an
        # unbounded list that every approval used to scan linearly before
        # touching the index. Ask the index first, fall back to the list for
        # fills that exist only in memory (a test double, or a snapshot written
        # before the table existed).
        stored = self.store.get_fill(ticket_id)
        if stored is not None:
            return stored
        for fill in self.state.get("fills", []):
            if fill.get("ticket_id") == ticket_id:
                return fill
        return None

    def _pending_list(self) -> list[dict]:
        return self.state.setdefault("pending", [])

    def _pending_ticket(self, ticket_id: str) -> dict | None:
        # Read-only: a lookup must not create the ``pending`` key. It did, and
        # that made "rolled back to before the call" differ from "never called"
        # by one empty list in the persisted payload.
        for entry in self.state.get("pending") or ():
            if entry.get("ticket_id") == ticket_id:
                return entry
        return None

    def queued_buy_commitment(self, *, exclude: str | None = None) -> Decimal:
        """Cash already promised to queued buys that have not settled yet.

        A queued ticket has no ledger order, so nothing is frozen: last night's
        19:30 buy — or this morning's 09:26 one — sits in ``pending`` while
        ``cash_available`` still shows the whole balance. The guard reads that
        number, so without this the 14:35 window can promise the same money a
        second time, and the *earlier* ticket is the one killed at settlement —
        after the model was told it had been placed.

        Fees are included, because the cash check adds them for the ticket under
        review; leaving them out would let a ticket through that fails on the
        minimum commission alone.

        Deliberately conservative for ``ceiling`` tickets: they may fill only a
        fraction of ``qty``, and the full notional is reserved here. Over-
        reserving costs an opportunity; under-reserving is what this method
        exists to stop. ``exclude`` is the ticket currently being checked — at
        settlement it is still in ``pending``, and counting it against itself
        would reject every queued buy.
        """
        total = D("0")
        for entry in self.state.get("pending") or ():
            if exclude is not None and entry.get("ticket_id") == exclude:
                continue
            if str(entry.get("side", "")).upper() != "BUY":
                continue
            try:
                value = money(D(entry["qty"]) * D(entry["limit_price"]))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
            total += value
            # 费用取不到就只算本金：少留 5 元可以接受，因为一个查不到的
            # instrument 把整笔占用丢掉，才是这条方法存在的意义所在。
            instrument = None
            try:
                day = entry.get("created_day") or self.state.get("day") or ""
                instrument = self.market.instrument(entry["symbol"], day)
            except Exception:  # noqa: BLE001 - a stub market may not resolve one
                instrument = None
            if instrument is not None:
                total += self.cost.buy_fees(value, instrument)
        return money(total)

    def queue_ticket(self, ticket: dict, actor: str = "agent",
                     *, queue_kind: str = "next_open") -> ExecutionResult:
        """Park a ticket for the next session's open instead of filling it now.

        A decision made after the close cannot be executed at that close: the
        price was already known when the decision was written. Filling at the
        next open is the earliest honest price, and it is also what the research
        path assumes.

        The existence check that used to live here (``market.bar(...) is None``
        -> NO_BAR) is gone on purpose. During a session today's daily bar cannot
        exist, so the check rejected exactly the orders that now need queueing;
        and it was never the real guard anyway - ``propose`` will not write a
        ticket without a price, and ``settle_pending`` re-checks the bar before
        it fills anything.
        """
        if int(ticket.get("qty") or 0) <= 0:
            return ExecutionResult(False, "rejected", "BAD_QTY")
        with self.lock:
            if self._pending_ticket(ticket["ticket_id"]):
                return ExecutionResult(ok=True, status="queued", qty=int(ticket["qty"]),
                                       price=D(ticket["limit_price"]))
            entry = {
                "ticket_id": ticket["ticket_id"],
                "symbol": ticket["symbol"],
                "side": ticket["side"],
                "qty": int(ticket["qty"]),
                "limit_price": str(ticket["limit_price"]),
                "sleeve": ticket.get("sleeve", "CORE"),
                "strategy": ticket.get("strategy", "llm"),
                "persona": ticket.get("persona"),
                "intent_type": ticket.get("intent_type", ""),
                "thesis": ticket.get("thesis", ""),
                "invalidation": ticket.get("invalidation", ""),
                "stop_price": ticket.get("stop_price"),
                "created_day": ticket["trade_date"],
                # 同日队列都只赌"某一个交易日的那个价"，不是长期有效的挂单：
                # ``ceiling`` 赌当天的板（封单/开板次数是当天的事实），``open``
                # 赌当天 09:30 的开盘价，``close`` 赌当天的收盘价。所以
                # ``expires_day`` 就是创建日，结算由
                # ``settle_same_session_due`` 在当天 bar 落库后完成，而
                # ``_rollover`` 必须**先结算、后过期**——反了就是在结算之前把它
                # 当过期的单取消掉（2026-11-04 修过一次）。次日开盘单用
                # QUEUE_TTL_SESSIONS 的窗口。
                "expires_day": (ticket["trade_date"]
                                if queue_kind in SAME_SESSION_QUEUES
                                else self._queue_expiry(ticket["trade_date"])),
                # 这张单有权用哪一个交易日的价格。同日队列就是创建日；
                # 次日开盘单是决策日的下一个交易日。在这里钉一次，因为
                # ``settle_pending`` 每次 rollover 都会跑，而它当天能看到的 bar
                # 未必是这张单赌的那一根：日更跳过一天，"次日开盘"就变成了"再下
                # 一个开盘"，成交在一根决策者从没见过的价格上。钉住之后，bar 迟到
                # 仍按它自己那一天定价；那一天真的没有 bar，就取消而不是改价。
                "target_day": (ticket["trade_date"]
                               if queue_kind in SAME_SESSION_QUEUES
                               else self._next_session(ticket["trade_date"])),
                **({"queue_kind": queue_kind} if queue_kind != "next_open" else {}),
            }
            mark = _state_mark(self.state)
            self._pending_list().append(entry)
            stored = dict(self.store.get_ticket(ticket["ticket_id"]) or ticket)
            stored.update(status="queued", queued_at=utc_now(), queue_kind=queue_kind,
                          fill_basis=SAME_SESSION_QUEUES.get(queue_kind, "next_open"))
            self.state["account"] = self._account_payload()
            self.state["updated_at"] = utc_now()
            try:
                self._revision = self.store.commit_ticket_state(
                    self.state, self._revision, ticket=stored, kind="queue",
                    decision={"qty": entry["qty"], "limit": entry["limit_price"]},
                    actor=actor)
            except StateConflict:
                # Someone else committed; our snapshot is the stale copy. Reload
                # so the next call works, then let the caller see one 409.
                self.reload()
                raise
            except Exception:
                _state_restore(self.state, mark)
                raise
            return ExecutionResult(ok=True, status="queued", qty=entry["qty"],
                                   price=D(entry["limit_price"]))

    def _bar_missing_for_good(self, symbol: str, target: str) -> bool:
        """True when a *later* session has a bar but ``target`` has none.

        "The bar is late" and "this session never printed" look identical on the
        target date alone: at 09:26 the daily job has not run yet, and cancelling
        there would kill a ticket that would have filled correctly eight hours
        later. A later session that does have a bar is the proof the pipeline
        moved past ``target`` — so that bar is genuinely absent (suspension, a
        hole in the partition), and no honest price is left.
        """
        later = self._next_session(target)
        if not later or later == target:
            return False
        return self.market.bar(symbol, later) is not None

    def _queue_expiry(self, created_day: str) -> str:
        """The last session a queued ticket may still fill on."""
        calendar = getattr(self.market, "calendar", None)
        shift = getattr(calendar, "shift", None)
        if shift is None:
            return ""          # stub market: no calendar, no expiry
        try:
            return shift(created_day, self.QUEUE_TTL_SESSIONS) or ""
        except (TypeError, ValueError):
            return ""

    def _next_session(self, day: str) -> str:
        """The session after ``day``, or ``""`` if the calendar cannot say.

        ``next`` on the calendar store is the exact answer; ``shift(day, 1)`` is
        the fallback for the test doubles, and it is only used there — a calendar
        day is not a session on a Friday. No calendar at all means no pinning,
        which restores the old "whatever bar is available" behaviour rather than
        inventing a date.
        """
        calendar = getattr(self.market, "calendar", None)
        step = getattr(calendar, "next", None)
        if step is not None:
            try:
                return step(day) or ""
            except (TypeError, ValueError):
                return ""
        shift = getattr(calendar, "shift", None)
        if shift is None:
            return ""
        try:
            return shift(day, 1) or ""
        except (TypeError, ValueError):
            return ""

    def settle_pending(self, day: str, actor: str = "system") -> list[dict]:
        """Execute only tickets whose durable lifecycle is still ``queued``."""
        results: list[dict] = []
        with self.lock:
            for entry in list(self._pending_list()):
                ticket_id = entry["ticket_id"]
                existing = self._fill_exists(ticket_id)
                if existing is not None:
                    # Legacy crash recovery: the old writer could leave both a
                    # fill and a pending row.  Never apply that fill twice.
                    if not any(row.get("ticket_id") == ticket_id
                               for row in self.state.get("fills", [])):
                        self.state.setdefault("fills", []).append(existing)
                    self.state["pending"] = [
                        row for row in self._pending_list()
                        if row.get("ticket_id") != ticket_id
                    ]
                    stored = self._finalize_ticket(entry, existing, Side(entry["side"]))
                    self.state["account"] = self._account_payload()
                    self.state["updated_at"] = utc_now()
                    self._revision = self.store.commit_ticket_state(
                        self.state, self._revision, ticket=stored, kind="recover_fill",
                        decision={"ticket_id": ticket_id, "status": "already_filled"},
                        actor=actor)
                    results.append({"ticket_id": ticket_id, "status": "already_filled",
                                    "symbol": entry["symbol"], "day": day,
                                    "qty": existing.get("qty", 0)})
                    continue
                stored = self.store.get_ticket(ticket_id)
                if stored is None or stored.get("status") != "queued":
                    outcome = {"ticket_id": ticket_id, "status": "cancelled",
                               "symbol": entry["symbol"], "day": day,
                               "reason": "TICKET_NOT_QUEUED"}
                    self._cancel_queued(entry, outcome, actor, status="cancelled")
                    results.append(outcome)
                    continue
                # A ticket written on the session it would trade in cannot use
                # that session's opening price: the open already happened.
                created = str(entry.get("created_day") or "")
                kind = entry.get("queue_kind")
                if kind in SAME_SESSION_QUEUES:
                    # ``open`` and ``ceiling`` are priced by
                    # ``settle_same_session_due`` on the session they named, and
                    # that pass runs first. Filling one here would price the same
                    # ticket off a *later* session's bar — a board bought a day
                    # after the board. When the day they named is gone, the only
                    # honest outcome left is the expiry below.
                    expires = str(entry.get("expires_day") or "")
                    if expires and str(day) > expires:
                        outcome = {"ticket_id": ticket_id, "status": "cancelled",
                                   "symbol": entry["symbol"], "day": day,
                                   "reason": f"QUEUE_EXPIRED:{created}->{day}"}
                        self._cancel_queued(entry, outcome, actor, status="cancelled")
                        results.append(outcome)
                    continue
                target = str(entry.get("target_day") or "")
                if target:
                    if str(day) < target:
                        continue          # the session it bet on has not opened yet
                elif created >= str(day):
                    continue              # legacy row: no pinned session
                expires = str(entry.get("expires_day") or "")
                if expires and str(day) > expires:
                    outcome = {"ticket_id": ticket_id, "status": "cancelled",
                               "symbol": entry["symbol"], "day": day,
                               "reason": f"QUEUE_EXPIRED:{created}->{day}"}
                    self._cancel_queued(entry, outcome, actor, status="cancelled")
                    results.append(outcome)
                    continue
                # Price at the pinned session, never at whatever bar happens to
                # exist today. ``day`` here is only the clock.
                price_day = target or str(day)
                mark = _state_mark(self.state)
                try:
                    outcome = self._try_fill(entry, price_day, actor)
                    if outcome is None:
                        if (target and str(day) > target
                                and self._bar_missing_for_good(entry["symbol"], target)):
                            # The session this ticket was entitled to has passed
                            # and its bar is genuinely absent — a later session's
                            # bar proves the pipeline moved past it. There is no
                            # honest price left: expire it by name rather than
                            # book a later open.
                            outcome = {"ticket_id": ticket_id, "status": "cancelled",
                                       "symbol": entry["symbol"], "day": price_day,
                                       "reason": f"NO_BAR:{price_day}"}
                            self._cancel_queued(entry, outcome, actor, status="cancelled")
                            results.append(outcome)
                        continue
                    if outcome.get("status") == "cancelled":
                        self._cancel_queued(entry, outcome, actor, status="cancelled")
                    results.append(outcome)
                except StateConflict:
                    self.reload()
                    raise
                except Exception:
                    _state_restore(self.state, mark)
                    self._rollback_ledger(mark)
                    raise
        return results

    def settle_same_session_due(self, today: str, actor: str = "system") -> list[dict]:
        """结算所有"赌某一天那一个价"的挂单：天花板单与盘前单。

        两类单的 ``expires_day`` 都是创建日，所以 ``settle_pending`` 会把它们当
        过期单取消——而唯一能结算它们的代码在下面。``_rollover`` 因此必须先跑
        这一步（第三轮修过一次顺序，这里把 ``open`` 并进同一趟）。

        按 ``created_day`` 分组，而不是拿 ``today`` 去套：一只挂了两天的单必须用
        它自己那天的 bar 与池子定价。``<= today`` 而不是 ``< today``，因为日更
        17:30 落库之后，当天 18:50 / 19:30 的调用就应该把当天的单结掉；没有 bar
        时 ``_try_fill`` 返回 ``None``，不会凭空成交。

        但"按它自己那一天定价"只在那一两天里成立。这三个队列的 ``expires_day``
        都是创建日，``settle_pending`` 会先把它们当过期的单取消——唯一能结算它们
        的代码是这一趟，所以那一趟永远轮不到。没有上限时，停摆一周再起来，一周
        前赌收盘的票仍会在那天的收盘价上补出一笔：账户里凭空多出一周前建仓的持
        仓，当天的面板上没有一个字提过。窗口用 ``QUEUE_TTL_SESSIONS``，与次日开
        盘单同一个数、同一个理由串（``QUEUE_EXPIRED``）。
        """
        days = sorted({str(entry.get("created_day") or "")
                       for entry in self._pending_list()
                       if entry.get("queue_kind") in SAME_SESSION_QUEUES
                       and str(entry.get("created_day") or "") <= str(today)})
        results: list[dict] = []
        for day in days:
            if day:
                results.extend(self._settle_same_session(day, actor, today=str(today)))
        return results

    def settle_ceiling(self, day: str, actor: str = "system") -> list[dict]:
        """结算当日以涨停价挂出的单（只结算 ceiling，保留的窄入口）。"""
        return self._settle_same_session(day, actor, kinds={"ceiling"})

    def _settle_same_session(self, day: str, actor: str, *,
                             kinds: set[str] | None = None,
                             today: str | None = None) -> list[dict]:
        """One pass over the tickets that named ``day``.

        ``_try_fill`` prices from that day's own bar, so a ticket that waited a
        day is still settled at the print it actually bet on — never at a later
        session's price. ``today`` is the clock, and it is only used to decide
        whether the wait has become too long: past ``QUEUE_TTL_SESSIONS`` the
        ticket is expired by name instead of being priced from a bar the desk
        was not running for. ``settle_ceiling`` passes no clock and therefore
        keeps its "settle whatever day you asked me about" contract.
        """
        deadline = self._queue_expiry(day) if today else ""
        wanted = SAME_SESSION_QUEUES if kinds is None else {k: SAME_SESSION_QUEUES[k] for k in kinds}
        results: list[dict] = []
        with self.lock:
            for entry in list(self._pending_list()):
                basis = wanted.get(entry.get("queue_kind"))
                if basis is None:
                    continue
                if str(entry.get("created_day") or "") != str(day):
                    continue
                if self._fill_exists(entry["ticket_id"]) is not None:
                    continue
                if deadline and today and str(today) > deadline:
                    outcome = {"ticket_id": entry["ticket_id"], "status": "cancelled",
                               "symbol": entry["symbol"], "day": day,
                               "reason": f"QUEUE_EXPIRED:{day}->{today}"}
                    self._cancel_queued(entry, outcome, actor, status="cancelled")
                    results.append(outcome)
                    continue
                before_state = _state_mark(self.state)
                try:
                    outcome = self._try_fill(entry, day, actor, basis=basis)
                    if outcome is None:
                        # 没有 bar 或数据没到：留着，下一轮再看，绝不猜。
                        continue
                    if outcome.get("status") == "cancelled":
                        self._cancel_queued(entry, outcome, actor, status="cancelled")
                    results.append(outcome)
                except StateConflict:
                    self.reload()
                    raise
                except Exception:
                    _state_restore(self.state, before_state)
                    self._rollback_ledger(before_state)
                    raise
        return results

    def _cancel_queued(self, entry: dict, outcome: dict, actor: str,
                       *, status: str) -> None:
        """Remove one pending row and finalize its ticket in the same transaction.

        The in-memory queue is rolled back if the commit fails. Four callers
        (``settle_pending`` ×3, ``_settle_same_session``) invoke this *outside*
        the per-ticket mark/rollback their neighbours have, so a
        ``StateConflict``/``StoreBusy`` — which this desk treats as routine
        multi-process contention — used to leave the row deleted in memory while
        the database still had it. The next successful commit then wrote the
        shortened queue as the truth, and the ticket stayed ``queued`` forever
        with nothing left to settle it.

        The three values are *rebound* by the code below, never mutated, so
        restoring the references is an exact rollback and costs no deepcopy.
        """
        before_pending = self.state.get("pending")
        before_account = self.state.get("account")
        before_updated = self.state.get("updated_at")
        try:
            self.state["pending"] = [
                row for row in self._pending_list()
                if row.get("ticket_id") != entry["ticket_id"]
            ]
            stored = dict(self.store.get_ticket(entry["ticket_id"]) or entry)
            stored.setdefault("created_at", utc_now())
            stored.setdefault("trade_date", entry.get("created_day", ""))
            stored.setdefault("sleeve", entry.get("sleeve", "CORE"))
            stored.update(status=status, cancelled_at=utc_now(),
                          reasons=[outcome.get("reason", "cancelled")])
            self.state["account"] = self._account_payload()
            self.state["updated_at"] = utc_now()
            self._revision = self.store.commit_ticket_state(
                self.state, self._revision, ticket=stored, kind="cancel",
                decision=outcome, actor=actor)
        except Exception:
            self.state["pending"] = before_pending if before_pending is not None else []
            if before_account is not None:
                self.state["account"] = before_account
            if before_updated is not None:
                self.state["updated_at"] = before_updated
            raise

    def cancel_ticket(self, ticket: dict, reason: str, actor: str) -> dict:
        """Manual rejection is also a queue deletion, not just a status label."""
        with self.lock:
            before = deepcopy(self.state)
            try:
                self.state["pending"] = [
                    row for row in self._pending_list()
                    if row.get("ticket_id") != ticket["ticket_id"]
                ]
                stored = dict(ticket)
                stored.update(status="rejected", reasons=[reason or "rejected by operator"],
                              rejected_at=utc_now())
                self.state["account"] = self._account_payload()
                self.state["updated_at"] = utc_now()
                self._revision = self.store.commit_ticket_state(
                    self.state, self._revision, ticket=stored, kind="manual_reject",
                    decision={"reason": reason}, actor=actor)
                return stored
            except StateConflict:
                # Someone else committed; our snapshot is the stale copy. Reload
                # so the next call works, then let the caller see one 409.
                self.reload()
                raise
            except Exception:
                self.state = before
                raise

    def cancel_armed_ticket(self, ticket: dict, reason: str, actor: str) -> dict:
        """Withdraw an *armed intent*. No order exists, so nothing is released.

        ``armed`` is not in ``pending`` and never touched the ledger: the first
        approve deliberately stopped before guard/execution (P0-D). Withdrawing
        it is therefore a ticket-status write, not an order cancellation. It is
        ``cancelled``, not ``rejected`` — a rejection says the proposal was
        refused, while this proposal was approved and then called off.

        Goes through ``commit_ticket_state`` like ``cancel_ticket`` so the
        optimistic revision check still applies; an exception rolls the in-memory
        snapshot back.

        D-19: ``self.state`` is submitted **unchanged**. There is no order to
        drop and no cash to release, so recomputing ``state["account"]`` or
        bumping ``updated_at`` would make a no-op withdrawal look like a ledger
        event and needlessly move the revision other writers contend on. The
        state argument exists only to carry the revision check.
        """
        with self.lock:
            # Re-read inside the lock: the caller may hold a stale ``view="full"``
            # copy while another thread just promoted this intent to ``filled``.
            # Overwriting that would erase a real fill.
            current = self.store.get_ticket(ticket["ticket_id"]) or ticket
            status = str(current.get("status") or "")
            if status in TERMINAL_TICKET_STATUSES:
                return current  # idempotent: a terminal state is the answer
            if status != "armed":
                raise ValueError("cancel_armed_ticket requires an armed ticket")
            before = deepcopy(self.state)
            try:
                stored = dict(current)
                stored.update(status="cancelled",
                              reasons=[reason or "armed intent withdrawn"],
                              cancelled_at=utc_now())
                self._revision = self.store.commit_ticket_state(
                    self.state, self._revision, ticket=stored,
                    kind="intent_cancel", decision={"reason": reason}, actor=actor)
                return stored
            except StateConflict:
                self.reload()
                raise
            except Exception:
                self.state = before
                raise

    def cancel_order(self, ticket_id: str, *, actor: str = "agent",
                     reason: str = "") -> dict:
        """Withdraw a ticket that is queued but not yet executed.

        This is the *order* exit, not the *proposal* exit: ``reject`` rewrites a
        ticket to ``rejected`` (the thesis never traded), whereas a queued ticket
        has no ledger order yet — cancelling it is just dropping its pending row.
        ``queued_buy_commitment`` is recomputed from what is left, so there is
        nothing to "release" and nothing to double-release.

        Idempotent, because "cancel" is a request to withdraw, not a claim that
        it is still withdrawable: a second call on a terminal ticket returns its
        status with ``cancelled: False`` instead of raising. A ``ceiling`` order
        rests at the exchange today, so the exchange's cancellation window
        applies; ``open`` / ``next_open`` entries are exempt — withdrawing a
        next-open intent after hours is normal. A ``next_open`` entry has no
        ``queue_kind`` key at all, hence ``.get``.
        """
        with self.lock:
            stored = self.store.get_ticket(ticket_id)
            if stored is None:
                return {"ticket_id": ticket_id, "status": "not_found",
                        "cancelled": False}
            status = str(stored.get("status") or "")
            if status in TERMINAL_TICKET_STATUSES:
                return {"ticket_id": ticket_id, "status": status,
                        "cancelled": False, "released_qty": 0}
            entry = self._pending_ticket(ticket_id)
            if entry is None:
                return {"ticket_id": ticket_id, "status": "not_queued",
                        "cancelled": False, "released_qty": 0}
            queue_kind = entry.get("queue_kind")
            if queue_kind == "ceiling" and not (
                    self.market.calendar.calendar().can_cancel(self.clock())):
                return {"ticket_id": ticket_id, "status": "blocked",
                        "cancelled": False, "released_qty": 0,
                        "reasons": ["CANNOT_CANCEL_IN_AUCTION"]}
            released = int(entry.get("qty") or 0)
            # ``_cancel_queued`` drops the pending row from ``state`` *before* the
            # commit; if the commit raises, the in-memory queue would be missing a
            # ticket the database still has. Roll the snapshot back — the same
            # pattern as ``cancel_ticket``. State only: ``_cancel_queued`` never
            # touches the ledger, so there is no ``before_ledger`` to restore.
            before = deepcopy(self.state)
            try:
                self._cancel_queued(
                    entry, {"reason": reason or "manual_cancel"}, actor,
                    status="cancelled")
            except StateConflict:
                # Someone else committed; our snapshot is stale. Reload so the
                # next call works, then let the caller see one 409. This clause
                # must stay *before* ``except Exception``, or the reload is undone.
                self.reload()
                raise
            except Exception:
                self.state = before
                raise
            return {"ticket_id": ticket_id, "status": "cancelled",
                    "cancelled": True, "released_qty": released,
                    "queue_kind": queue_kind,
                    "ticket": self.store.get_ticket(ticket_id) or stored}

    def _retire_order(self, order, ts: datetime, reason: str) -> None:
        """Release every remaining cash/share reservation after a submit.

        The reason travels with the cancel too. It used to be passed only to the
        ``expire`` fallback, so an order retired inside the cancel window recorded
        ``CANCELLED`` — the mechanism but not the reason — and only an order
        retired *outside* the window kept the sentence that explained it.
        """
        if not order.is_open:
            return
        result = self.ledger.cancel(order.order_id, ts, self.market.calendar.calendar(),
                                    reason=reason)
        if not result.success:
            result = self.ledger.oms.expire(
                order.order_id, ts, self.ledger.account, reason=reason)
        if not result.success:
            raise RuntimeError(
                f"cannot retire {order.order_id}: {result.reason.value}:{result.detail}")

    def _reject_execution(self, ticket: dict, reason: str, actor: str) -> ExecutionResult:
        """Persist a terminal rejection together with any OMS sequence changes."""
        stored = dict(self.store.get_ticket(ticket["ticket_id"]) or ticket)
        stored.update(status="rejected", reasons=[reason], rejected_at=utc_now())
        self.state["account"] = self._account_payload()
        self.state["updated_at"] = utc_now()
        self._revision = self.store.commit_ticket_state(
            self.state, self._revision, ticket=stored, kind="execution_reject",
            decision={"reason": reason}, actor=actor)
        return ExecutionResult(False, "rejected", reason)

    def expected_ceiling_fill(self, symbol: str, day: str) -> float | None:
        """这只票今天以涨停价挂单，大概能成交多少比例。

        给 ``propose`` 用。没有它，模型会按 ``qty`` 算仓位与风险，而实际只成交
        一部分——风险预算会整体偏大一倍以上。返回 ``None`` 表示"不知道"：
        没有封单数据、或全天没开板，都是不知道（后者应读作"买不到"）。
        """
        built = self.market.bar(symbol, day)
        bar = built[0] if built is not None else None
        ratio = self._ceiling_ratio({"symbol": symbol, "ticket_id": ""}, day, bar=bar)
        return None if ratio is CEILING_WAITING else ratio

    def _ceiling_pool(self, day: str) -> tuple[bool, object]:
        """``(可读?, 当日涨停池)``。第一个元素 ``False`` = 还没落库。

        必须与"池子里没有这只票"分开：缺数据要等，缺这只票才是"它今天没封板"。
        混成一个 ``None`` 的下场是——日线先到、池子后到的那几分钟里，每一张打板
        单都被判成"排不上队"并**终态取消**，而池子五分钟后就到了。
        """
        store = getattr(self.market, "store", None)
        if store is None:
            return True, None            # 桩行情：读不到池子，沿用旧逻辑
        try:
            pool = store.read("limit_up_pool", day, day)
        except (KeyError, ValueError, OSError):
            return False, None
        if pool is None or pool.empty or "code" not in pool.columns:
            return False, None
        return True, pool

    def _ceiling_ratio(self, entry: dict, day: str, pool=None, bar=None):
        """涨停价买单当天能成交的比例，按当日封单与成交额倒推。

        ``execution.derive_fill_ratio`` 早就写好了这个式子（成交额 ÷（成交额 +
        封单额），并假设你的单排在队尾），但那个模块的注释写着"没有任何 builder
        提供过这个比例，所以 ``LIMIT_UP_QUEUE`` 分支从来没被走过"——涨停开板一律
        回 ``QUEUE_NO_PRIORITY``。打板在这个台子上因此根本成交不了。

        这里给它供数，并且只在**当日单、收盘后结算**时供：
        用收盘封单去定 09:30 的成交是前视；用来定"这个交易日里一张挂着的涨停价
        单最终能成交多少"不是——那件事在 15:00 就已经确定，只是到收盘才知道。
        这个区别写在 fill 的 ``fill_basis`` 上，读的人能看见。

        ``open_times == 0``（全天没开板）返回 ``None``：排队买不到就是买不到，
        让撮合器走它原本的 ``OPEN_AT_LIMIT`` 拒绝，不编一个"排到了"。
        """
        if pool is None:
            ready, pool = self._ceiling_pool(day)
            if not ready or pool is None:
                return None
        code = str(entry["symbol"]).split(".")[0]
        hit = pool[pool["code"].astype(str) == code]
        if hit.empty:
            return None
        row = hit.iloc[0]
        try:
            opens = float(row.get("open_times"))
        except (TypeError, ValueError):
            return None

        # 先看开盘价在不在涨停上。**"全天没开板"和"买不到"不是一回事**：
        # 000995.SZ 2026-11-02 的 open_times=0，但它是 open 15.53 → 收盘 15.60
        # 的盘中封板，封板之前随便买。把它读成"买不到"会让 93% 的样本被误杀。
        readable, at_limit = self._opening_vs_limit(
            entry["symbol"], day, bar=bar)
        if not readable:
            # 判不出来 ≠ 买不到。开盘涨停池还没落库时返回"等"，而不是让撮合器
            # 按 ``OPEN_AT_LIMIT`` 落一个**终态**取消——池子五分钟后再到，那张单
            # 已经死了。11.3 把这条规则用在了 ``limit_up_pool`` 上，这里是同一个
            # 形状的第二份数据源。
            return CEILING_WAITING
        if at_limit is False:
            # 开盘低于涨停价：挂涨停价单会在开盘就以市价成交（限价高于市价），
            # 不需要排队。成交价是开盘价，**不是涨停价**——这一点必须让调用方
            # 看见，因为两者的期望差了一个数量级（+1.87% vs 开盘买的份额）。
            return 1.0
        if opens <= 0:
            return None

        seal, traded = row.get("seal_amount"), row.get("amount")
        if seal is None or traded is None:
            return None
        try:
            raw = derive_fill_ratio(Decimal(str(seal)), Decimal(str(traded)))
        except (ArithmeticError, ValueError):
            return None
        if raw is None:
            return None
        # 不打折。这个比例是「成交额 ÷（成交额 + 封单额）」，用的是**收盘**封单，
        # 对开过板的票系统性偏乐观（2026-11-02 的 83 只开板票，中位 94.9%）——
        # 因为被砸开过的票收盘封单本来就薄。要不要打折、打多少，是读的人的决定：
        # 内核给原始计算与口径，不替他做这个假设。
        return raw

    def _opening_vs_limit(self, symbol: str, day: str, *, bar=None) -> tuple[bool, bool | None]:
        """``(判得出来?, 开盘价是否在涨停上)``。

        第一个元素把"数据还没到"与"这只票开盘不在涨停上"分开：前者要等，后者
        说明挂涨停价的单会在开盘成交。混在一起时，``limit_pool_t0`` 晚到几分钟
        就会把一张还在排队的单判成"买不到"，而那是**终态**。

        优先用当日 K 线自己的 ``open`` 与 ``up_limit``：这两个数在日线落库时就
        已经确定，不依赖第二份数据集什么时候到。只有 K 线缺涨跌停价（``0`` 表示
        未知）时才回退到开盘涨停池。
        """
        if bar is not None and bar.up_limit > 0:
            return True, bar.open >= bar.up_limit * D("0.999")
        store = getattr(self.market, "store", None)
        if store is None:
            # 桩行情：沿用旧语义，不因为读不到池子就拒绝成交。
            return True, None
        try:
            frame = store.read("limit_pool_t0", day, day,
                               columns=["code", "pool", "open", "limit_price"])
        except (KeyError, ValueError, OSError):
            return False, None
        if frame is None or frame.empty or "pool" not in frame.columns:
            return False, None
        hit = frame[(frame["code"].astype(str) == str(symbol))
                    & (frame["pool"].astype(str) == "limit_up")]
        if hit.empty:
            return True, None
        row = hit.iloc[0]
        try:
            return True, float(row["open"]) >= float(row["limit_price"]) * 0.999
        except (TypeError, ValueError):
            return True, None

    @staticmethod
    def _clamp_to_band(limit: Decimal, side: Side, bar: Bar) -> Decimal:
        """Keep a ticket's limit inside the session's band, when there is one.

        Three fill paths wrote `min(limit, bar.up_limit)` by hand. That is only
        meaningful when the band exists: a bar whose limit column was empty
        carries ``up_limit == 0``, so the clamp rewrote a perfectly good limit to
        zero and ``submit`` rejected it as PRICE_LIMIT — a missing column wearing
        the costume of an illegal price. A band of zero now means "unknown", and
        an unknown band leaves the trader's price alone; the OMS decides whether
        it can be honoured against a derived band.
        """
        if side is Side.BUY and bar.up_limit > 0:
            return min(limit, bar.up_limit)
        if side is Side.SELL and bar.down_limit > 0:
            return max(limit, bar.down_limit)
        return limit

    def _try_fill(self, entry: dict, day: str, actor: str, *,
                  basis: str = "next_open") -> dict | None:
        built = self.market.bar(entry["symbol"], day)
        if built is None:
            return None  # bar not published yet: keep waiting, never guess
        bar, instrument = built
        side = Side(entry["side"])
        limit = D(entry["limit_price"])
        limit = self._clamp_to_band(limit, side, bar)

        if entry.get("queue_kind") == "open":
            # A pre-open order gets the opening print or nothing. The day's later
            # range is not evidence: the order was not resting yet when the price
            # traded there, so a gap through the limit is a miss, not a fill at
            # the limit. Without this check the matcher's "touched the day's low"
            # path would book a price this trader could not have got.
            if side is Side.BUY and bar.open > limit:
                return {"ticket_id": entry["ticket_id"], "status": "cancelled",
                        "symbol": entry["symbol"], "day": day,
                        "reason": f"OPEN_ABOVE_LIMIT:{bar.open}>{limit}"}
            if side is Side.SELL and bar.open < limit:
                return {"ticket_id": entry["ticket_id"], "status": "cancelled",
                        "symbol": entry["symbol"], "day": day,
                        "reason": f"OPEN_BELOW_LIMIT:{bar.open}<{limit}"}

        if basis == "close":
            # 收盘结算只认当日收盘价。限价站在收盘价的错误一侧就是"没成交"，
            # 不是"按限价成交"——把 10.00 的买单记成收盘 10.50 那天的成交，
            # 会凭空造出一笔市场没有印过的价格（``_execute_at_close`` 同一条
            # 规则，2026-11-03 修过它自己的那一份）。
            if side is Side.BUY and bar.close > limit:
                return {"ticket_id": entry["ticket_id"], "status": "cancelled",
                        "symbol": entry["symbol"], "day": day,
                        "reason": f"PRICE_LIMIT:close {bar.close} above buy limit {limit}"}
            if side is Side.SELL and bar.close < limit:
                return {"ticket_id": entry["ticket_id"], "status": "cancelled",
                        "symbol": entry["symbol"], "day": day,
                        "reason": f"PRICE_LIMIT:close {bar.close} below sell limit {limit}"}

        # 天花板单：把当天的排队比例塞进 bar，撮合器据此按比例成交或拒绝。
        # 其它路径的 entry 没有 ``queue_kind``，这一支对它们完全不可见。
        if side == Side.BUY and entry.get("queue_kind") == "ceiling":
            ready, pool = self._ceiling_pool(day)
            if not ready:
                # 池子还没落库。17:30 的任务按 dataset 逐个写，日线和池子不是
                # 同一秒到；缺数据不等于"没开板"，而"排不上队"是**终态**取消——
                # 数据晚到五分钟就再也补不回来。等下一轮。
                return None
            ratio = self._ceiling_ratio(entry, day, pool=pool, bar=bar)
            if ratio is CEILING_WAITING:
                # 开盘涨停池还没到：不知道开没开板，等下一轮，别落终态。
                return None
            if ratio is not None and ratio < 1.0:
                bar = replace(bar, fill_ratio=ratio)

        veto = self._preflight(entry, day, limit, side)
        if veto is not None:
            return veto
        intent = Intent(
            symbol=entry["symbol"],
            side=side,
            qty=int(entry["qty"]),
            order_type=OrderType.LIMIT,
            limit_price=limit,
            reason=entry.get("intent_type", "queued"),
            sleeve=entry.get("sleeve", "CORE"),
            client_order_id=entry["ticket_id"],
        )
        if basis == "close":
            ts, calendar = self._close_execution_point(day)
        else:
            ts = datetime.strptime(day, "%Y%m%d").replace(hour=9, minute=30, tzinfo=SHANGHAI)
            calendar = None
        before_account = self.state.get("account")
        order = self.ledger.submit(intent, ts, instrument, bar,
                                   calendar or self.market.calendar.calendar())
        if order.status == OrderStatus.REJECTED:
            return {"ticket_id": entry["ticket_id"], "status": "cancelled",
                    "symbol": entry["symbol"], "day": day, "reason": order.reason}

        result = self.matcher.match(order, bar, instrument, ts, calendar,
                                    ref_price=_reference_price(entry))
        if not result.filled:
            self._retire_order(order, ts, "QUEUED_NO_FILL")
            return {"ticket_id": entry["ticket_id"], "status": "cancelled",
                    "symbol": entry["symbol"], "day": day,
                    "reason": f"{result.reject_reason}:{result.detail}"}

        fill = result.fill
        assert fill is not None
        # C2-a: a sealed-ceiling fill is priced from the 17:30 limit-up pool — how
        # much of the day's turnover was sealed is a *close-of-day* fact — so its
        # honest timestamp is the closing call auction (14:58), not the 09:30 open
        # the ticket was matched at. Stamping it 09:30 while ``fill_basis`` says
        # ``ceiling_queue`` is the look-ahead this fixes: pricing a 09:30 print
        # with 15:00+ data.
        #
        # ``rule_id`` is the discriminator: only ``_match_continuous``'s
        # sealed-ceiling branch sets ``LIMIT_UP_QUEUE``, so a ceiling ticket that
        # opened *below* the limit (an ordinary fill at the open) is untouched,
        # as are every non-ceiling path. Only ``ts`` moves — price / qty / fees /
        # limit_up_penalty / fill_id are the matcher's and stay identical.
        #
        # Nothing upstream changes: the order is still submitted and matched at
        # 09:30 (that branch needs a CONTINUOUS session), so
        # ``fill.ts`` 14:58 >= ``order.created_ts`` 09:30 keeps ``oms.apply_fill``
        # happy, and T+1 accounting keys on ``trade_date`` (a string), not on
        # ``ts.date()``. ``_close_execution_point`` keeps one definition of 14:58
        # shared with the close path.
        if fill.rule_id.startswith("LIMIT_UP_QUEUE"):
            fill = replace(fill, ts=self._close_execution_point(day)[0])
        # Ticket-scoped, for the same reason as the close path: the matcher's
        # ``order_id + filled_qty`` restarts at O00000001 in every process, so two
        # processes over one database collide on ``paper_fills.fill_id`` before the
        # revision check can report the conflict.
        fill = replace(fill, fill_id=f"fill-{entry['ticket_id']}")
        entry_price = D("0")
        if side == Side.SELL:
            held = self.ledger.account.peek(entry["symbol"])
            entry_price = held.avg_cost if held is not None else D("0")
        try:
            self.ledger.apply_fill(order, fill)
        except ValueError as exc:
            if before_account is not None:
                self.ledger = Ledger.from_snapshot(before_account, self.cost)
            return {"ticket_id": entry["ticket_id"], "status": "cancelled",
                    "symbol": entry["symbol"], "day": day, "reason": f"FILL_ERROR:{_fill_error(exc)}"}
        if side is Side.BUY:
            # A fill dated before today is *already* one session old. ``_rollover``
            # settles T+1 before it runs either settlement pass, so a ticket that
            # settles a session late is credited after the unlock and would stay
            # locked until the following rollover — T+2, and a stop that fires the
            # next morning cannot sell. Move just this fill's shares; calling
            # ``ledger.settle()`` here would also unlock shares bought today.
            self._release_t_plus_one(entry["symbol"], fill.qty, day)
        leftover = order.remaining_qty
        if leftover > 0:
            # One ticket has one terminal outcome; a partial fill never leaves a
            # live remainder holding cash or shares after this transaction.
            # ``ts`` is the settlement session start (09:30); a LIMIT_UP_QUEUE
            # fill had its ``fill.ts`` moved to 14:58 above. ``oms.cancel`` writes
            # ``order.updated_ts = ts``, so a ceiling partial fill legitimately
            # leaves ``order.updated_ts`` earlier than ``fill.ts``. The order is
            # already terminal and cannot fill again, so this is an audit-order
            # quirk, not a state bug. Retiring at 14:58 instead would hit
            # ``can_cancel(14:58) == False`` and flip the terminal label to
            # EXPIRED — a different decision, not a fix.
            self._retire_order(order, ts, "QUEUED_PARTIAL_REMAINDER")

        ticket = dict(entry, trade_date=day)
        self._record_fill(
            ticket, order, fill, side, fill.qty, entry_price, ts, actor,
            basis=basis,
            # The ticket already says ``ceiling_queue``; the fill record said
            # ``next_open`` for the same trade, so TCA grouped a same-session
            # ceiling fill with next-open entries and the decay study excluded it
            # from neither. One label, taken from the caller that knows which
            # settlement this is.
            extra={"price_source": "daily", "quote_ts": bar.ts.isoformat()})
        return {"ticket_id": entry["ticket_id"], "status": "filled",
                "symbol": entry["symbol"], "day": day, "qty": fill.qty,
                "price": str(fill.price), "rule": fill.rule_id,
                "unfilled": leftover}

    def _release_t_plus_one(self, symbol: str, qty: int, fill_day: str) -> None:
        """Unlock a fill that belongs to an earlier session than the book's day.

        ``oms.apply_fill`` parks a T+1 purchase in ``locked_qty`` and leaves the
        unlock to ``settle()`` at the next session boundary. That contract holds
        for an intraday fill (same session, unlock tomorrow) but not for a queue
        settlement that happens *after* the boundary: the shares were bought on
        ``fill_day``, the book has already rolled to a later day, so they are
        deliverable now. Without this the position sat locked for one extra
        session and a stop that fired that morning was refused as T+1.

        Only this fill's shares move. ``settle()`` cannot be used: it clears
        every lock, including shares genuinely bought today.
        """
        if qty <= 0 or str(fill_day) >= str(self.state.get("day") or fill_day):
            return
        pos = self.ledger.account.peek(symbol)
        if pos is None:
            return
        move = min(int(pos.locked_qty), int(qty))
        if move <= 0:
            return
        pos.locked_qty -= move
        pos.available_qty += move

    def _finalize_ticket(self, ticket: dict, record: dict, side: Side) -> dict:
        """Build the terminal ticket from the same provenance as the fill."""
        stored = dict(self.store.get_ticket(ticket["ticket_id"]) or ticket)
        stored.setdefault("created_at", utc_now())
        stored.setdefault("sleeve", ticket.get("sleeve", "CORE"))
        stored["status"] = "filled"
        stored["filled_at"] = utc_now()
        stored["fill"] = {
            "order_id": record["order_id"],
            "fill_id": record["fill_id"],
            "qty": record["qty"],
            "price": record["price"],
            "fees": record["fees"],
            "cash_after": str(self.ledger.account.cash_available),
            "equity_after": str(self.equity()),
            "basis": record["fill_basis"],
        }
        for key in ("price_source", "quote_ts", "quote_age_seconds"):
            if key in record:
                stored["fill"][key] = record[key]
        if side == Side.BUY and stored.get("stop_pct"):
            pct = D(str(stored["stop_pct"]))
            final = (D(record["price"]) * (D("1") - pct)).quantize(D("0.01"))
            stored["stop_price_final"] = str(final)
            stored["stop_basis"] = "fill"
        return stored

    @staticmethod
    def _live_snapshot(symbol: str, bar, live) -> Snapshot:
        """Shape a live bar like a stored snapshot for the guard.

        The guard, the band rule and the sizing code all take a snapshot; none of
        them should care whether it came from a daily partition or a quote. What
        a reader must be able to see is ``source``, which reads ``live:<vendor>``.
        """
        return Snapshot(
            symbol=symbol,
            trade_date=bar.ts.strftime("%Y%m%d"),
            last=bar.close, pre_close=bar.pre_close, open=bar.open,
            high=bar.high, low=bar.low,
            up_limit=bar.up_limit, down_limit=bar.down_limit,
            volume=float(bar.volume), amount=float(bar.amount),
            suspended=bar.suspended, source=f"live:{live.source}",
        )

    def _preflight(self, entry: dict, day: str, limit: Decimal, side: Side,
                   snapshot=None) -> dict | None:
        """Re-run the hard guard at fill time, or return the cancellation row.

        Every aggregate limit — position count, industry, sleeve, cash floor,
        actions per day — is a property of the *account*, and the account can
        change between the approval and the open: another queued ticket may
        have filled first, or the position may have been marked down. Checking
        only at proposal time let four individually legal tickets open four
        positions under a three-position cap.

        ``snapshot`` is the live quote when the fill happens intraday: today's
        stored bar does not exist yet, and validating an intraday order against
        yesterday's close rejects it for a band that no longer applies.
        """
        if self.guard is None:
            return None
        snap = snapshot if snapshot is not None else self.market.snapshot(entry["symbol"], day)
        # Cache-only quotes, never a vendor call: this runs with ``self.lock``
        # held, and ``guard.preflight`` asks the stop gate about *every* holding.
        # Passing ``None`` would let that question go to the network for each
        # held name, freezing every reader of the account behind three source
        # timeouts. ``{}`` would be a lie ("no quotes"), so the parameter is
        # omitted entirely when no cache reader was wired (unit tests keep the
        # old path, including their stub quote callables).
        quotes = None
        if self.quotes_cached is not None:
            held = [p.symbol for p in tuple(self.ledger.account.positions.values())
                    if p.total_qty > 0]
            quotes = self.quotes_cached(held) if held else {}
        result = self.guard.preflight(
            self, symbol=entry["symbol"], side=side.value,
            sleeve=entry.get("sleeve", "CORE"), qty=int(entry["qty"]),
            price=limit, date=day, strategy=entry.get("strategy"), snapshot=snap,
            # 结算时这张票自己还在 ``pending`` 里，不排除就会拿它自己的占用去
            # 抵它自己的钱，每一张排队的买单都会被"现金不足"退掉。
            exclude_ticket=entry["ticket_id"],
            quotes=quotes,
        )
        if result.ok:
            return None
        return {"ticket_id": entry["ticket_id"], "status": "cancelled",
                "symbol": entry["symbol"], "day": day,
                "reason": "GUARD:" + "; ".join(result.failures),
                "checks": result.as_dict()}

    def _record_fill(self, ticket: dict, order, fill: Fill, side: Side, qty: int,
                     entry_price: Decimal, ts: datetime, actor: str, *,
                     basis: str | None = None, extra: dict | None = None) -> None:
        record = {
            "ticket_id": ticket["ticket_id"],
            "fill_id": fill.fill_id,
            "order_id": order.order_id,
            "symbol": fill.symbol,
            "side": side.value,
            "qty": qty,
            "price": str(fill.price),
            "fees": str(fill.fees),
            # Queue-position penalty travels as its own amount, never inside
            # ``fees``. Defaulted so a synthetic fill without the new field
            # still records 0. ``record`` is what lands in state["fills"] and
            # paper_fills.payload, and all three settlement paths go through
            # this function — so the amount stays auditable everywhere.
            "limit_up_penalty": str(getattr(fill, "limit_up_penalty", D("0"))),
            "ts": fill.ts.isoformat(),
            "ref_price": None if fill.ref_price is None else str(fill.ref_price),
            # Which matching rule produced this fill (``LIMIT_UP_QUEUE`` /
            # ``+TOUCH_DAY`` / ``+VOLUME_CAP`` …). Without it, a ceiling-queue
            # ticket that actually filled at the open and one that queued at the
            # ceiling look identical in ``paper_fills``, and TCA grouped them
            # together. ``fill_basis`` answers "which settlement", this answers
            # "which print".
            "rule_id": fill.rule_id,
            "trade_date": ticket["trade_date"],
            "sleeve": ticket.get("sleeve", "CORE"),
            "fill_basis": basis or ("next_open" if self.fill_mode == "next_open"
                                    else "close"),
        }
        if extra:
            record.update(extra)
        fills = self.state.setdefault("fills", [])
        fills.append(record)
        # The in-memory list is a rolling window; the durable row is in
        # ``paper_fills`` and is what a restart reads. ``summary()`` counts the
        # table once this window is full, so the number never under-reports.
        if len(fills) > HISTORY_KEEP:
            del fills[:-HISTORY_KEEP]
        self.state["pending"] = [
            row for row in self._pending_list()
            if row.get("ticket_id") != ticket["ticket_id"]
        ]
        self.state.setdefault("sleeves", {})[fill.symbol] = ticket.get("sleeve", "CORE")
        self.state.setdefault("strategy_of", {})[fill.symbol] = ticket.get("strategy", "llm")
        case = self._track_case(ticket, fill.symbol, side, qty, fill.price, fill.fees,
                                entry_price, ts)
        stored = self._finalize_ticket(ticket, record, side)
        order_row = {
            "order_id": order.order_id,
            "ts": ts.isoformat(),
            "symbol": fill.symbol,
            "status": order.status.value,
            "side": side.value,
            "qty": qty,
            "price": str(fill.price),
            "fees": str(fill.fees),
            "ticket_id": ticket["ticket_id"],
            "sleeve": ticket.get("sleeve", "CORE"),
            "fill_basis": record["fill_basis"],
        }
        for key in ("price_source", "quote_ts", "quote_age_seconds"):
            if key in record:
                order_row[key] = record[key]
        self.state["account"] = self._account_payload()
        self.state["updated_at"] = utc_now()
        self._revision = self.store.commit_execution(
            self.state, self._revision, order=order_row, ticket=stored,
            fill=record, case=case, actor=actor)

    def execute_ticket(self, ticket: dict, actor: str = "agent") -> ExecutionResult:
        with self.lock:
            existing = self._fill_exists(ticket["ticket_id"])
            if existing:
                return ExecutionResult(
                    ok=True, status="already_filled", order_id=existing.get("order_id", ""),
                    fill_id=existing.get("fill_id", ""), qty=int(existing.get("qty", 0)),
                    price=D(existing.get("price", "0")), fees=D(existing.get("fees", "0")),
                    cash_after=self.ledger.account.cash_available,
                    equity_after=self.equity(),
                )
            if self._pending_ticket(ticket["ticket_id"]):
                return ExecutionResult(ok=True, status="queued",
                                       qty=int(ticket["qty"]), price=D(ticket["limit_price"]))
            stored = self.store.get_ticket(ticket["ticket_id"])
            if stored and stored.get("status") in {"rejected", "expired", "cancelled"}:
                return ExecutionResult(False, "rejected", "TICKET_NOT_EXECUTABLE")
            before_state = _state_mark(self.state)
            try:
                # 09:15–09:30 the exchange accepts orders but has not printed an
                # open yet. AUCTION_OPEN (09:15–09:25) collects the opening
                # auction, PRE_OPEN (09:25–09:30) is the gap before the first
                # print; the kernel refuses to *match* in either — there is no
                # trade price to match against — so an intraday ticket created
                # there used to die with `MARKET_CLOSED` (and, unlike the 15:01
                # case, `_reject_execution` made that a *terminal* rejection,
                # not a recoverable queue). The honest model is the exchange's
                # own: the order joins this session's opening print. It is queued
                # as ``open`` (same-day, `fill_basis="open_auction"`) and settled
                # once today's daily bar exists, at ``bar.open``, with every
                # gap-through cancelled rather than filled later in the day.
                if self.fill_mode in {"intraday", "next_open"}:
                    session = self.market.calendar.calendar().session_at(self.clock())
                    if session in {Session.PRE_OPEN, Session.AUCTION_OPEN}:
                        return self.queue_ticket(ticket, actor, queue_kind="open")
                if self.fill_mode == "limit_queue":
                    # 当日以涨停价挂单，当天收盘后按"开没开板 + 封单/成交额"结算。
                    # 这是打板唯一说得通的执行方式：`next_open` 会在次日开盘买，
                    # 而那正是被证伪的那个动作；`intraday` 会在挂单那一刻就判
                    # "封死、排队未知"而拒绝。
                    #
                    # 这里**不判断该不该挂**。挂什么价、挂哪只、什么时候挂，是
                    # 调用方的决定；内核只负责按限价撮合并如实报告。如果挂单时
                    # 市价低于限价，撮合会以市价成交（300556.SZ 2026-11-02 就是
                    # 挂 19.51、成交 16.38），成交记录里带着成交价，读的人自己
                    # 看得出那不是打板。
                    #
                    # 只有 BUY 进这个队列：ceiling 的定义是"按涨停封单排队买"，
                    # 卖出没有封单可排。把卖单塞进来只会让它带着
                    # `fill_basis=ceiling_queue` 的标签按开盘价成交，TCA 的
                    # by_basis 分组从此混入两类完全不同的成交。
                    if str(ticket.get("side", "")).upper() == "BUY":
                        return self.queue_ticket(ticket, actor, queue_kind="ceiling")
                    return self.queue_ticket(ticket, actor)
                if self.fill_mode == "next_open":
                    return self.queue_ticket(ticket, actor)
                if self.fill_mode == "intraday":
                    live = self._execute_live(ticket, actor)
                    if live is not None:
                        return live
                    # No live path right now: carry the order to the next open.
                    queued = self.queue_ticket(ticket, actor)
                    if queued.ok:
                        queued.reason = "FALLBACK_TO_OPEN"
                    return queued
                return self._execute_at_close(ticket, actor)
            except StateConflict:
                self.reload()
                raise
            except Exception:
                _state_restore(self.state, before_state)
                self._rollback_ledger(before_state)
                raise

    def _execute_live(self, ticket: dict, actor: str) -> ExecutionResult | None:
        """Fill against the current quote, or return None if that cannot be done.

        Returning None means "not now, try the queue" - never "pretend". The two
        refusals that matter (a stored close posing as live, a quote older than
        90 seconds) are produced by ``live_fill.build`` and travel into the
        decision log, so an unexplained fallback is impossible.
        """
        from .live_fill import build as build_live

        if self.quotes is None:
            return None

        # Outside the session a vendor still answers with the last traded price.
        # Filling there would stamp a 15:00 price as 20:00 - the same lie as
        # using a stored close, just harder to notice.
        now = self.clock()
        # Judge the *matching* window, not "is a quote still flowing". The
        # snapshot layer deliberately keeps 15:00–15:05 inside ``SESSIONS`` so a
        # frame captured at 15:01 still belongs to today's session — the right
        # answer for /market/status, the wrong answer for a fill. Two bugs lived
        # in the gap between that 15:05 window and ``can_submit``'s 15:00 edge:
        #   * 15:00–15:05 the order reached ``oms.submit``, which refused it as
        #     AFTER_HOURS, and ``_reject_execution`` then wrote a **terminal**
        #     rejection — a tail-end decision that could never be recovered;
        #   * 14:57–15:00 AUCTION_CLOSE matched at ``bar.close``, which for a live
        #     bar is the 14:58 tape price used as the 15:00 close, with no
        #     slippage.
        # The single choke point for "may this fill off a live quote": only
        # CONTINUOUS may. Everything else returns None and ``execute_ticket``
        # decides which queue it joins — the two opening-auction windows go to
        # the same-day ``open`` queue, AUCTION_CLOSE / AFTER_HOURS / CLOSED (and
        # non-trading days, which are CLOSED here) to the next open. This is a
        # whitelist, not a blacklist of the four sessions seen today: a session
        # value added later cannot silently reopen the terminal-reject hole
        # (``matcher.match`` refuses any non-CONTINUOUS session with
        # MARKET_CLOSED, and that used to become a *terminal* rejection here).
        session = self.market.calendar.calendar().session_at(now)
        if session is not Session.CONTINUOUS:
            self.store.log_decision(
                "live_refused",
                {"ticket_id": ticket["ticket_id"],
                 "reason": {"code": "MARKET_CLOSED", "message": session.value}},
                ticket_id=ticket["ticket_id"], actor=actor,
            )
            return None

        symbol = ticket["symbol"]
        try:
            quote = self.quotes([symbol]).get(symbol)
        except (ValueError, KeyError, RuntimeError):
            quote = None

        live, refusal = build_live(self.market, symbol, quote, now=now)
        if live is None:
            self.store.log_decision(
                "live_refused",
                {"ticket_id": ticket["ticket_id"], "reason": refusal.as_dict()
                 if refusal else None},
                ticket_id=ticket["ticket_id"], actor=actor,
            )
            return None

        side = Side(ticket["side"])
        qty = int(ticket["qty"])
        limit = D(ticket["limit_price"])
        bar = live.bar
        limit = self._clamp_to_band(limit, side, bar)

        # The same aggregate guard the queue and the close path run. It used to
        # be skipped here, which meant the *fastest* route to a fill was also the
        # only one that never re-checked position count, industry weight, the
        # cash floor or the daily action cap — exactly the limits that a burst of
        # intraday orders violates first.
        day = bar.ts.strftime("%Y%m%d")
        veto = self._preflight(ticket, day, limit, side,
                              snapshot=self._live_snapshot(symbol, bar, live))
        if veto is not None:
            self.store.log_decision(
                "live_refused",
                {"ticket_id": ticket["ticket_id"],
                 "reason": {"code": "GUARD", "message": veto["reason"]},
                 "checks": veto.get("checks")},
                ticket_id=ticket["ticket_id"], actor=actor,
            )
            # Not a rejection: the ticket keeps its life and waits for the open,
            # where the guard is asked again against the same rules.
            return None

        intent = Intent(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=limit,
            reason=ticket.get("intent_type", "ticket"),
            sleeve=ticket.get("sleeve", "CORE"),
            client_order_id=ticket["ticket_id"],
        )
        before_account = self.state.get("account")
        order = self.ledger.submit(intent, bar.ts, live.instrument, bar,
                                   self.market.calendar.calendar())
        if order.status == OrderStatus.REJECTED:
            return self._reject_execution(ticket, order.reason, actor)

        # The decision price, not the fill price. Without it `ref_price`
        # collapsed to `bar.open` — which here *is* the quote that just
        # filled — so implementation shortfall recorded only slippage and
        # the delay between deciding and filling was always zero.
        result = self.matcher.match(order, bar, live.instrument, bar.ts,
                                    ref_price=_reference_price(ticket))
        if not result.filled:
            self._retire_order(order, bar.ts, "LIVE_NO_FILL")
            return self._reject_execution(
                ticket, f"{result.reject_reason}:{result.detail}", actor)

        fill = result.fill
        assert fill is not None
        # Ticket-scoped, as the close and next-open paths already are: the
        # matcher's ``order_id + filled_qty`` restarts at O00000001 in every
        # process, so two processes over one database collide on
        # ``paper_fills.fill_id`` before the revision check can report it.
        fill = replace(fill, fill_id=f"fill-{ticket['ticket_id']}")
        entry_price = D("0")
        if side == Side.SELL:
            held = self.ledger.account.peek(symbol)
            entry_price = held.avg_cost if held is not None else D("0")
        try:
            self.ledger.apply_fill(order, fill)
        except ValueError as exc:
            if before_account is not None:
                self.ledger = Ledger.from_snapshot(before_account, self.cost)
            return self._reject_execution(ticket, f"FILL_ERROR:{_fill_error(exc)}", actor)
        if order.remaining_qty > 0:
            self._retire_order(order, bar.ts, "LIVE_PARTIAL_REMAINDER")

        self._record_fill(ticket, order, fill, side, fill.qty, entry_price, bar.ts,
                          actor, basis="intraday",
                          extra={"price_source": live.source,
                                 "quote_ts": live.quote_ts,
                                 "quote_age_seconds": None if live.age_seconds is None
                                 else round(live.age_seconds, 1)})
        return ExecutionResult(
            ok=True, status="filled", order_id=order.order_id, fill_id=fill.fill_id,
            qty=fill.qty, price=fill.price, fees=fill.fees,
            cash_after=self.ledger.account.cash_available, equity_after=self.equity(),
        )

    def _close_execution_point(self, day: str) -> tuple[datetime, TradingCalendar]:
        """收盘结算的时点与日历，三处调用共用一处定义。

        ``_execute_at_close``（立即成交）、``_try_fill(basis="close")``（排队
        结算）与打板成交（``LIMIT_UP_QUEUE``，用 14:58 作为**结算时点**）必须
        给出同一个 14:58 与同一把日历。写成两份的话，同一张票"当时能执行"和
        "等 bar 到了再执行"会走不同的撮合规则，而差异只在数据迟到时出现——最
        不容易被发现的那一侧。

        注意：对打板成交，14:58 是**结算时点**而不是成交瞬间——真实成交时刻与
        ``+TOUCH_DAY`` 一样不可观测（见 ``tca._caveats``）。这里只是把"用收盘后
        的事实回填一个更早的时刻"改成"记在事实可观测的那一端"。
        """
        ts = datetime.strptime(day, "%Y%m%d").replace(hour=14, minute=58, tzinfo=SHANGHAI)
        calendar = self.market.calendar.calendar()
        if calendar.session_at(ts) is not Session.AUCTION_CLOSE:
            # A date the stored calendar does not cover (a synthetic session in a
            # test, or a date before trade_cal begins). The fill is still a close
            # fill, so use the calendar that only knows the clock rather than
            # turning a valid ticket into MARKET_CLOSED.
            calendar = TradingCalendar.always()
        return ts, calendar

    def _execute_at_close(self, ticket: dict, actor: str) -> ExecutionResult:
        symbol = ticket["symbol"]
        date = ticket["trade_date"]
        built = self.market.bar(symbol, date)
        if built is None:
            # Today's bar is not published yet — pre-open, intraday, or a 17:30
            # job that has not finished. That is a missing print, not a market
            # rejection: queue for *this* session's close and let
            # ``settle_same_session_due`` price it once the bar lands. The old
            # branch returned a terminal ``NO_BAR`` here, so a ticket written at
            # 09:26 could never be filled at the close it named — the one gap
            # the 09:25–09:30 fix left in ``close`` mode.
            return self.queue_ticket(ticket, actor, queue_kind="close")
        bar, instrument = built

        side = Side(ticket["side"])
        qty = int(ticket["qty"])
        limit_price = D(ticket["limit_price"])
        limit_price = self._clamp_to_band(limit_price, side, bar)
        # A limit order fills at the close only when the close is on the right
        # side of the limit. The old code clamped instead — `min(close, limit)`
        # for a buy — which booked a fill at a price the market never printed:
        # a 10.00 limit on a stock that closed at 10.50 was recorded as a 10.00
        # entry, and every downstream number (cost basis, P&L, the stop anchored
        # to the fill) inherited a trade that could not have happened.
        if side == Side.BUY and bar.close > limit_price:
            return self._reject_execution(
                ticket, f"PRICE_LIMIT:close {bar.close} above buy limit {limit_price}", actor)
        if side == Side.SELL and bar.close < limit_price:
            return self._reject_execution(
                ticket, f"PRICE_LIMIT:close {bar.close} below sell limit {limit_price}", actor)
        veto = self._preflight(ticket, date, limit_price, side)
        if veto is not None:
            return ExecutionResult(False, "rejected", veto["reason"])

        intent = Intent(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=limit_price,
            reason=ticket.get("intent_type", "ticket"),
            sleeve=ticket.get("sleeve", "CORE"),
            client_order_id=ticket["ticket_id"],
        )
        # 14:58 is inside the closing call auction, so the shared matcher prices
        # this as a close fill — the same rule the open path uses, applied to the
        # session that is actually happening.
        ts, calendar = self._close_execution_point(date)

        before_account = self.state.get("account")
        order = self.ledger.submit(intent, ts, instrument, bar, calendar)
        if order.status == OrderStatus.REJECTED:
            return self._reject_execution(ticket, order.reason, actor)

        # Through the matcher, not by hand. The hand-built Fill used the *ticket*
        # quantity while ``submit`` had already rounded the order to a legal lot,
        # so a 250-share ticket produced an order of 200 and a fill of 250 —
        # ``apply_fill`` then raised "fill qty exceeds remaining" and the ticket
        # was rejected with FILL_ERROR. It also skipped the volume cap, the queue
        # ratio and the incremental fee model that every other fill path uses.
        result = self.matcher.match(order, bar, instrument, ts, calendar,
                                    ref_price=_reference_price(ticket))
        if not result.filled:
            self._retire_order(order, ts, "CLOSE_NO_FILL")
            return self._reject_execution(
                ticket, f"{result.reject_reason}:{result.detail}", actor)

        fill = result.fill
        assert fill is not None
        # Ticket-scoped, as this path always was. The matcher derives its id from
        # ``order_id + filled_qty``, and an order id restarts at O00000001 in every
        # process — so a second process working the same database produced a
        # duplicate ``fill_id`` and died on the UNIQUE index before the revision
        # check could report the (correct) StateConflict. The ticket is the
        # idempotency unit here; ``paper_fills.ticket_id`` is what guards replay.
        fill = replace(fill, fill_id=f"fill-{ticket['ticket_id']}")
        qty = fill.qty
        entry_price = D("0")
        if side == Side.SELL:
            held = self.ledger.account.peek(symbol)
            entry_price = held.avg_cost if held is not None else D("0")
        try:
            self.ledger.apply_fill(order, fill)
        except ValueError as exc:
            if before_account is not None:
                self.ledger = Ledger.from_snapshot(before_account, self.cost)
            return self._reject_execution(ticket, f"FILL_ERROR:{_fill_error(exc)}", actor)
        if order.remaining_qty > 0:
            self._retire_order(order, ts, "CLOSE_PARTIAL_REMAINDER")

        self._record_fill(
            ticket, order, fill, side, qty, entry_price, ts, actor, basis="close",
            extra={"price_source": "daily", "quote_ts": bar.ts.isoformat()})
        return ExecutionResult(
            ok=True, status="filled", order_id=order.order_id, fill_id=fill.fill_id,
            qty=qty, price=fill.price, fees=fill.fees,
            cash_after=self.ledger.account.cash_available, equity_after=self.equity(),
        )

    # ---------------------------------------------------------------- cases
    def _track_case(self, ticket: dict, symbol: str, side: Side, qty: int,
                    price: Decimal, fees: Decimal, entry_price: Decimal,
                    ts: datetime) -> dict | None:
        """Turn a fill into a reviewable case (buy opens, sell closes)."""
        opens = self.state.setdefault("opens", {})
        if side == Side.BUY:
            if symbol not in opens:
                opens[symbol] = {
                    "strategy": ticket.get("strategy", "llm"),
                    "persona": ticket.get("persona"),
                    "opened_at": ts.isoformat(),
                    "price": str(price),
                    "qty": qty,
                    "ticket_id": ticket["ticket_id"],
                    "thesis": ticket.get("thesis", ""),
                    "invalidation": ticket.get("invalidation", ""),
                }
            return None

        open_rec = opens.get(symbol)
        if open_rec is None or entry_price <= 0:
            return None
        pnl = money((price - entry_price) * D(qty) - fees)
        basis = money(entry_price * D(qty))
        pnl_pct = float(pnl / basis) if basis > 0 else 0.0
        opened_at = open_rec.get("opened_at")
        held_days = 0
        try:
            held_days = (ts - datetime.fromisoformat(opened_at)).days
        except (TypeError, ValueError):
            pass

        case = {
            "case_id": f"case-{ticket['ticket_id']}",
            "symbol": symbol,
            "opened_at": opened_at,
            "closed_at": ts.isoformat(),
            "qty": qty,
            "entry": float(entry_price),
            "exit": float(price),
            "pnl": float(pnl),
            "pnl_pct": pnl_pct,
            "thesis": open_rec.get("thesis", ""),
            "invalidation": open_rec.get("invalidation", ""),
            "ticket_ids": [open_rec.get("ticket_id"), ticket["ticket_id"]],
            "note": f"held {held_days}d",
            "strategy": open_rec.get("strategy", "llm"),
            "persona": open_rec.get("persona"),
        }
        held = self.ledger.account.peek(symbol)
        if held is None or held.total_qty == 0:
            opens.pop(symbol, None)
        return case

    # ---------------------------------------------------------------- control
    def sleeve_of(self, symbol: str) -> str:
        """Which budget this position spends from.

        Explicit assignment wins, then the sleeve the position was opened with,
        then ``CORE`` — the same default as ``Intent``, ``Order`` and
        ``RiskLimits``. The old fallback was ``CORE`` for an ETF and
        ``AGGRESSIVE`` for everything else, which inverted the default for every
        stock: a position with no sleeve entry was counted against the *tighter*
        60% sleeve cap while every other layer called it CORE.
        """
        sleeves = self.state.setdefault("sleeves", {})
        if symbol in sleeves:
            return str(sleeves[symbol])
        pos = self.ledger.account.peek(symbol)
        if pos is not None and pos.sleeve:
            return str(pos.sleeve).upper()
        return "CORE"

    def sleeve_value(self, sleeve: str) -> Decimal:
        total = D("0")
        # A snapshot of the pairs, not a live view: this runs on the proposal
        # thread while another thread's fill may add a position, and iterating a
        # dict that grows raises RuntimeError.
        for pos in tuple(self.ledger.account.positions.values()):
            if pos.total_qty <= 0:
                continue
            if self.sleeve_of(pos.symbol).upper() != sleeve.upper():
                continue
            total += money(D(pos.total_qty) * pos.last_price)
        return total

    def set_kill_switch(self, value: bool) -> None:
        with self.lock:
            self.state["kill_switch"] = bool(value)
            self.persist()

    def set_safe_mode(self, value: bool) -> None:
        with self.lock:
            self.state["safe_mode"] = bool(value)
            self.persist()

    def positions(self) -> list[dict]:
        out = []
        equity = self.equity()
        for symbol, pos in self.ledger.account.positions.items():
            if pos.total_qty == 0:
                continue
            value = money(D(pos.total_qty) * pos.last_price)
            out.append({
                "symbol": symbol,
                "name": self.market.names().get(symbol, ""),
                # ``qty`` and ``total_qty`` are the same number under two names:
                # the summary has always said qty, the /account route has always
                # said total_qty, and a reader comparing the two routes had no way
                # to tell whether they disagreed. Both are emitted until callers
                # have moved.
                "qty": pos.total_qty,
                "total_qty": pos.total_qty,
                "available_qty": pos.available_qty,
                "frozen_qty": pos.frozen_qty,
                "sleeve": self.sleeve_of(symbol),
                "avg_cost": str(pos.avg_cost),
                "last_price": str(pos.last_price),
                "market_value": str(value),
                "weight": float(value / equity) if equity > 0 else 0.0,
                "unrealized_pnl": str(pos.unrealized_pnl),
                "realized_pnl": str(pos.realized_pnl),
            })
        return sorted(out, key=lambda x: x["market_value"], reverse=True)

    def summary(self) -> dict:
        """One consistent read of the account, under the book lock.

        ``apply_fill`` writes cash, eight position fields and the order status in
        sequence; a reader that takes them one at a time can report an equity
        that never existed (a position counted while its cash is still frozen).
        ``lock`` is an ``RLock``, so the internal callers that already hold it —
        ``execute_ticket``, ``_rollover``, ``persist`` — are unaffected.
        """
        with self.lock:
            return self._summary()

    def _summary(self) -> dict:
        equity = self.equity()
        acct = self.ledger.account
        # Counted once and reused: it is the same list the caller is about to
        # read, and "how many positions do I hold" is the first question a risk
        # check asks.
        positions = self.positions()
        # Cash promised to queued buys is not frozen in the ledger (there is no
        # order yet), so it has to be reported separately — otherwise
        # ``cash_available`` reads as spendable and the next window spends it
        # twice. Same computation the guard uses, one place.
        committed = self.queued_buy_commitment()
        return {
            "as_of": utc_now(),
            "day": self.state.get("day"),
            "initial_cash": self.state.get("initial_cash"),
            "equity": str(money(equity)),
            # 金额一律走 money()：`Decimal("0")` 与 `Decimal("0.00")` 打印出来是
            # "0" 与 "0.00"，同一个余额两个出口就成了两个值。串在 /account 与
            # /portfolio 之间比一次就会被读成不一致，而它只是没量化。
            "cash_available": str(money(acct.cash_available)),
            "cash_frozen": str(money(acct.cash_frozen)),
            # 已排队、未结算的买单占用的钱。``cash_available`` 是账本里的余额，
            # 但**能花的是 ``cash_free``**：09:26 或昨晚 19:30 排进去的买单在结算
            # 之前不冻结任何现金，只看 cash_available 会把同一笔钱承诺两次。
            "cash_committed": str(committed),
            "cash_free": str(money(acct.cash_available - committed)),
            "market_value": str(money(equity - acct.cash_total())),
            "realized_pnl": str(money(acct.realized_pnl)),
            "unrealized_pnl": str(money(sum((p.unrealized_pnl for p in acct.positions.values()),
                                            D("0")))),
            "fees_paid": str(money(acct.fees_paid)),
            "peak_equity": str(money(acct.peak_equity)),
            "drawdown": round(self.drawdown(), 4),
            "daily_pnl_pct": round(self.daily_pnl_pct(), 4),
            "total_return": round(float(equity / D(self.state["initial_cash"]) - 1), 4),
            "position_count": len(positions),
            "positions": positions,
            "kill_switch": self.state.get("kill_switch", False),
            "safe_mode": self.state.get("safe_mode", False),
            "fills": self._fill_count(),
            "fill_mode": self.fill_mode,
            "pending": [
                {"ticket_id": e["ticket_id"], "symbol": e["symbol"], "side": e["side"],
                 "qty": e["qty"], "limit": e["limit_price"], "created": e.get("created_day"),
                 # ``kind`` 与 ``target`` 让读的人分得清"今天的开盘单"和"等明天
                 # 开盘的单"：14:35 窗口看到 09:26 的买单还挂在 pending 里，那是
                 # 正常（收盘后才结算），不是"没买"；没有这两个字段时，两张单在
                 # 输出里长得一样，模型很容易再提一张。
                 "kind": e.get("queue_kind", "next_open"),
                 "target_day": e.get("target_day") or e.get("created_day")}
                for e in self._pending_list()
            ],
        }
