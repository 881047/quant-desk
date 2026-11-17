"""Batch 4 regressions: a ceiling fill is stamped when it was observable.

A ``queue_kind="ceiling"`` fill is priced from the **17:30 limit-up pool**: the
queue ratio (``seal_amount / amount``) only exists after the close. But the fill
used to carry ``ts = 09:30`` — the opening auction — while
``fill_basis="ceiling_queue"``. So a number computed from a 15:00 fact was
stamped as a 09:30 execution: at least eight hours of look-ahead baked into the
record, and the only path in the desk that did it.

The fix moves only ``fill.ts`` for ``rule_id`` starting ``LIMIT_UP_QUEUE`` to
14:58 (``_close_execution_point``, the close-auction open). price / qty / fees /
limit_up_penalty / fill_id are untouched.

``fill_ratio < 1`` is reachable only under ``FILL_MODE=limit_queue`` (or a
synthetic ``Bar(fill_ratio<1)``), so these cases build the book the way
``test_ceiling_queue.py`` does instead of relying on the default mode.

Why three cases and not one: ``plan.md`` §3.5 asks for one ``ts == 14:58`` case,
but §3.3 also requires the non-ceiling paths to keep their timestamps, and C2-b
is a ``tca`` caveat with no other test. Leaving those unverified is exactly the
"changed but untested" pattern the team has been burned by twice.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from service.kernel.clock import SHANGHAI
from service.kernel.models import Bar, D
from tests.trading.test_book_execution_safety import (
    DAY1,
    DAY2,
    SYMBOL,
    StubMarket,
    make_bar,
    make_book,
    quote,
    ticket,
)
from tests.trading.test_ceiling_queue import _PoolStore, _book, _market

DAY1_CLOSE_AUCTION = "2026-01-05T14:58:00+08:00"
DAY1_OPEN = "2026-01-05T09:30:00+08:00"
DAY2_OPEN = "2026-01-06T09:30:00+08:00"
LIVE_CLOCK = "2026-01-06T10:30:00+08:00"


# ---------------------------------------------------------------------------
# C2-a · the ceiling fill is stamped at the close auction, and only ts moves
# ---------------------------------------------------------------------------
def test_ceiling_fill_is_timestamped_at_the_close_auction(tmp_path):
    market = _market(open_times=2, seal=1e6, amount=2e7, low="10.60")
    book = _book(tmp_path / "p.sqlite", market)

    assert book.execute_ticket(ticket(limit="11.00", qty=400)).status == "queued"
    results = book.settle_ceiling(DAY1)
    assert [row["status"] for row in results] == ["filled"], results

    record = book.state["fills"][0]
    # The queue ratio is a 17:30 fact; 09:30 was the look-ahead this batch removes.
    assert record["ts"] == DAY1_CLOSE_AUCTION
    assert record["fill_basis"] == "ceiling_queue"

    # Money must not move with the timestamp: 300 x 11.00 = 3,300 notional,
    # 5.00 commission + 0.03 transfer = 5.03 fees, 20bps penalty = 6.60.
    assert record["price"] == "11.00"
    assert record["qty"] == 300
    assert record["fees"] == "5.03"
    assert record["limit_up_penalty"] == "6.60"
    assert book.ledger.account.peek(SYMBOL).total_qty == 300

    # The durable paper_fills payload must carry the same ts as state["fills"].
    assert book.store.recent_fills()[0]["ts"] == DAY1_CLOSE_AUCTION


# ---------------------------------------------------------------------------
# Acceptance §3.3 · every non-ceiling path keeps the timestamp it had
# ---------------------------------------------------------------------------
def test_non_ceiling_paths_keep_their_original_timestamps(tmp_path):
    bars = {
        DAY1: make_bar(DAY1, open_="10.10", high="11.00", low="10.00", close="10.20"),
        DAY2: make_bar(DAY2, open_="10.10", high="10.30", low="10.05", close="10.20"),
    }

    # (a) next_open — the *other* caller of `_try_fill`, so the one at real risk
    #     of being over-reached by the new `rule_id` branch. Still 09:30.
    queue_book = make_book(tmp_path / "next.sqlite", StubMarket(bars), "next_open")
    queue_book.rollover(DAY1)
    assert queue_book.execute_ticket(ticket(limit="10.20")).status == "queued"
    queue_book.rollover(DAY2)
    next_open = queue_book.state["fills"][-1]
    assert next_open["fill_basis"] == "next_open"
    assert next_open["ts"] == DAY2_OPEN
    assert not next_open["ts"].endswith("14:58:00+08:00")

    # (b) `_execute_at_close` — was already 14:58, must stay so.
    close_book = make_book(tmp_path / "close.sqlite", StubMarket(bars), "close")
    assert close_book.execute_ticket(ticket(limit="10.20")).status == "filled"
    at_close = close_book.state["fills"][-1]
    assert at_close["ts"] == DAY1_CLOSE_AUCTION

    # (c) `_execute_live` — stamps the quote/test clock (10:30), never 14:58.
    live_book = make_book(tmp_path / "live.sqlite", StubMarket(bars), "intraday")
    live_book.quotes = lambda symbols: {s: quote(live_book) for s in symbols}
    assert live_book.execute_ticket(ticket(limit="10.20")).status == "filled"
    live = live_book.state["fills"][-1]
    assert live["fill_basis"] == "intraday"
    assert live["ts"] == LIVE_CLOCK


# ---------------------------------------------------------------------------
# The discriminator is `rule_id`, not `queue_kind` — this is the guard for it
# ---------------------------------------------------------------------------
def test_a_ceiling_ticket_that_opens_below_the_limit_keeps_the_open_ts(tmp_path):
    """A ceiling *ticket* that opened below the limit is an ordinary open fill.

    ``book._try_fill`` keys the re-stamp on ``rule_id.startswith("LIMIT_UP_QUEUE")``
    — set only when the board is sealed at the open **and** ``fill_ratio < 1``.
    This ticket was queued as ``ceiling``, but the board opened *below* the limit,
    so ``blocked_at_open`` is false: it fills at the ordinary opening price and
    its 09:30 is real. If someone ever changes the discriminator to
    ``queue_kind == "ceiling"``, this is the only case that fails.
    """
    ts = datetime.strptime(DAY1, "%Y%m%d").replace(hour=15, tzinfo=SHANGHAI)
    bar = Bar(symbol=SYMBOL, ts=ts, open=D("16.38"), high=D("19.51"),
              low=D("16.10"), close=D("19.51"), pre_close=D("17.74"),
              volume=5_000_000, amount=D("19.51") * 5_000_000,
              up_limit=D("19.51"), down_limit=D("14.53"))
    market = StubMarket({DAY1: bar})
    market.store = _PoolStore(
        pd.DataFrame({"code": [SYMBOL.split(".")[0]], "open_times": [0.0],
                      "seal_amount": [3e8], "amount": [1e8]}),
        derived=pd.DataFrame({"code": [SYMBOL], "pool": ["limit_up"],
                              "open": [16.38], "limit_price": [19.51]}))

    # `last` is 16.38 — the price now, comfortably below the 19.51 ceiling.
    book = _book(tmp_path / "p.sqlite", market, last="16.38", ceiling="19.51")
    assert book.execute_ticket(ticket(limit="19.51", qty=100)).status == "queued"
    results = book.settle_ceiling(DAY1)
    assert [row["status"] for row in results] == ["filled"], results

    # `state["fills"]` now carries `rule_id` as well; the Fill on the ledger is
    # still the authority for the matcher's own label.
    fill = book.ledger.fills[0]
    assert "LIMIT_UP_QUEUE" not in fill.rule_id
    assert fill.ts.strftime("%H:%M") == "09:30"
    assert fill.ts.isoformat() == DAY1_OPEN

    record = book.state["fills"][0]
    assert record["ts"] == DAY1_OPEN
    assert record["fill_basis"] == "ceiling_queue"
    # Sanity that this really filled near the open, not at the ceiling.
    assert fill.price < D("17.00")
    assert book.ledger.account.peek(SYMBOL).total_qty == 100


# ---------------------------------------------------------------------------
# C2-b · the TOUCH_DAY timing caveat is stated where TCA is read
# ---------------------------------------------------------------------------
def test_tca_caveat_names_the_touch_day_timing_uncertainty():
    """`+TOUCH_DAY` fills are triggered by the day's low/high — a 15:00 fact.

    The behaviour is deliberately unchanged (disabling it would rewrite backtest
    fills and break `test_kernel_audit_20261104.py`); the caveat is what makes
    the unobservable fill time visible to whoever reads the TCA report.
    """

    class _Store:
        def recent_fills(self, limit: int = 500):
            return []

    class _Desk:
        paper = _Store()

    from service.trading.tca import summary

    caveats = summary(_Desk())["caveats"]
    joined = " ".join(caveats)
    assert "+TOUCH_DAY" in joined
    assert "shortfall_bps" in joined
    lowered = joined.lower()
    assert "unobservable" in lowered or "不可观测" in joined
