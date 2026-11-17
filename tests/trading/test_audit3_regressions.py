"""Regressions for the third audit pass (2026-10-31).

Same rule as the first two files: one test per defect found by reading, each
docstring naming the failure it prevents. The themes this round are

* a reservation that is released in part must not be zeroed in whole;
* asking a question must not create state (``peek`` vs ``position``);
* a percentile whose denominator is 1,400 daily partitions must not re-read
  all 1,400 on every call, and must not go stale when one of them changes;
* a validated-partition cache that can never hit is not a cache.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from service.kernel.costs import CostModel
from service.kernel.ledger import Ledger
from service.kernel.models import (
    Account,
    Bar,
    D,
    Instrument,
    Intent,
    OrderStatus,
    Position,
    Side,
)

DAY = "20260105"


def _bar(symbol: str = "600000.SH", close: str = "10.00") -> Bar:
    return Bar(
        symbol=symbol,
        ts=pd.Timestamp(f"{DAY} 09:30:00").to_pydatetime(),
        open=D("10.00"), high=D("10.50"), low=D("9.50"),
        close=D(close), pre_close=D("10.00"),
        volume=1_000_000, amount=D("1e7"),
        up_limit=D("11.00"), down_limit=D("9.00"),
    )


class _AlwaysCalendar:
    def session_at(self, ts):
        from service.kernel.models import Session

        return Session.CONTINUOUS

    def can_submit(self, ts):
        return True

    def can_cancel(self, ts):
        return True


# ------------------------------------------------------- reservation release
def test_a_partial_release_leaves_the_remainder_on_the_order():
    """Cancelling must return what it returned, not erase the whole reservation.

    ``_release`` clamps with ``min(order.frozen_cash, account.cash_frozen)`` and
    then sets the reservation to zero. The clamp exists because the two sides
    can disagree; zeroing while releasing less makes the difference vanish from
    both. A second cancel of the same order then releases it twice.
    """
    account = Account(cash_available=D("0"), cash_frozen=D("100"),
                      cash_withdrawable=D("0"))
    # ``frozen_withdrawable`` is part of the reservation now: the release has to
    # know which part of the freeze came out of the withdrawable pool, or it
    # hands T+1-restricted cash back as if it were withdrawable.
    order = SimpleNamespace(side=Side.BUY, frozen_cash=D("400"),
                            frozen_withdrawable=D("0"),
                            frozen_shares=0, symbol="600000.SH")
    from service.kernel.oms import OrderManager

    OrderManager._release(order, account)

    assert account.cash_frozen == D("0"), "everything frozen was returned"
    assert account.cash_available == D("100")
    assert order.frozen_cash == D("300"), (
        "the 300 that could not be released must stay on the order, not "
        "disappear from both the account and the reservation")


def test_a_sell_release_without_a_position_does_not_invent_one():
    """Releasing shares for a symbol the account never held must not create a row.

    ``account.position()`` is a factory. Used from a release path it produced an
    empty Position, which was then written into every snapshot and made
    ``Ledger.reconcile`` report an unexpected position for a trade that never
    happened.
    """
    account = Account()
    order = SimpleNamespace(side=Side.SELL, frozen_cash=D("0"),
                            frozen_shares=100, symbol="600000.SH")
    from service.kernel.oms import OrderManager

    OrderManager._release(order, account)

    assert "600000.SH" not in account.positions
    assert order.frozen_shares == 100, "nothing was released, so nothing is owed"


def test_a_rejected_sell_leaves_no_phantom_position():
    """Proposing a sell in a symbol you do not own is a rejection, not a holding.

    The guard runs on every proposal, so this fired on every symbol the model
    ever asked about. Empty positions are invisible in ``positions()`` but are
    persisted by ``snapshot()`` and flagged by ``reconcile()``.
    """
    ledger = Ledger.fresh("5000")
    order = ledger.oms.submit(
        Intent(symbol="600000.SH", side=Side.SELL, qty=100),
        pd.Timestamp(f"{DAY} 10:00:00").to_pydatetime(),
        ledger.account,
        Instrument(symbol="600000.SH"),
        _bar(),
        _AlwaysCalendar(),
    )
    assert order.status == OrderStatus.REJECTED
    assert "600000.SH" not in ledger.account.positions


def test_a_proposal_check_does_not_conjure_a_holding():
    """A buy check for an unheld symbol must read zero without creating a row."""
    from service.kernel.risk import RiskEngine

    account = Account(cash_available=D("5000"))
    decision = RiskEngine().check(
        Intent(symbol="600000.SH", side=Side.BUY, qty=100),
        account,
        Instrument(symbol="600000.SH"),
        _bar(),
        kill_switch=False,
        safe_mode=False,
    )
    assert decision.allowed
    assert "600000.SH" not in account.positions, (
        "a check is a question; only a fill may create a position")


# ------------------------------------------------------------ tape caching
class _Rec:
    def __init__(self, partition: str, snapshot_id: str, status: str = "ok") -> None:
        self.partition = partition
        self.snapshot_id = snapshot_id
        self.status = status


class _FakeStore:
    def __init__(self) -> None:
        self.versions = {"ths_limit_up": "v1", "ths_limit_break": "v1",
                         "ths_limit_stat": "v1"}
        self.calls: list[tuple] = []
        self.rows = {"ths_limit_up": 3, "ths_limit_break": 2, "ths_limit_stat": 1}

    def list_manifest(self, name):
        return [_Rec("20260105", self.versions[name])]

    def read(self, name, start=None, end=None, columns=None):
        self.calls.append((name, start, end, tuple(columns or ())))
        n = self.rows[name]
        data = {"trade_date": [f"2026010{i}" for i in range(n)]}
        for column in columns or ():
            if column != "trade_date":
                data[column] = [0.5] * n
        return pd.DataFrame(data)


def _desk():
    from service.trading.tape import _POOL_CACHE

    _POOL_CACHE.clear()
    store = _FakeStore()
    return SimpleNamespace(data=SimpleNamespace(registry=store, read=store.read)), store


def test_the_percentile_baseline_is_not_reread_on_every_question():
    """1,684 daily partitions per call was 2.4 s on the first endpoint the
    scheduler hits. The rows cannot change between ingestions, so the manifest
    set is the version and one registry query replaces the whole sweep.
    """
    from service.trading import tape

    desk, store = _desk()
    tape.limit_history(desk, "20260105")
    first = len(store.calls)
    assert first == 3, "one read per pool"
    tape.limit_history(desk, "20260105")
    tape.limit_history(desk, "20260104")
    assert len(store.calls) == first, (
        "a second question, even about a different day, must not re-read")


def test_the_baseline_cache_is_keyed_on_content_not_on_the_clock():
    """A re-ingested pool must invalidate the answer even within one process.

    ``snapshot_id`` is derived from the content checksum, so an edited partition
    produces a new id. Caching on anything weaker would serve yesterday's
    percentile from today's pool.
    """
    from service.trading import tape

    desk, store = _desk()
    tape.limit_history(desk, "20260105")
    before = len(store.calls)
    store.versions["ths_limit_up"] = "v2"
    tape.limit_history(desk, "20260105")
    assert len(store.calls) > before, "a changed pool must be re-read"


def test_the_window_is_still_applied_after_the_cache():
    """Reading unbounded is only safe because the caller still filters by day.

    Without this the cache would be a look-ahead: tomorrow's pool would sit in
    today's percentile.
    """
    from service.trading import tape

    desk, store = _desk()
    store.rows["ths_limit_up"] = 3
    history = tape.limit_history(desk, "20260101")
    assert history["window"]["end"] == "20260101", (
        "rows after as_of must not enter the baseline")


# --------------------------------------------------- validated-partition cache
def test_an_unstamped_partition_is_hashed_once_not_every_access(tmp_path):
    """``file_sha256`` of "" is falsy, and the cache compared it to a real hash.

    The comparison could never be true, so every legacy partition was re-read
    and re-hashed on every single access. The cache existed and never hit.
    """
    from pathlib import Path

    import service.adapters.parquet_store as ps
    from service.adapters.sqlite_store import Registry

    store = ps.DatasetStore(tmp_path / "data", Registry(tmp_path / "registry.sqlite"))
    frame = pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20260105"],
                          "close": [10.0], "open": [10.0], "high": [10.0],
                          "low": [10.0], "pre_close": [10.0], "vol": [100.0],
                          "amount": [1000.0]})
    rec = store.write("daily", "20260105", frame, as_of="20260105")

    # Reproduce a pre-versioning manifest: stamped with an empty hash.
    meta_path = Path(rec.path).with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["file_sha256"] = ""
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                         encoding="utf-8")

    store._validated.clear()
    calls = {"n": 0}
    real = ps.pd.read_parquet

    def counted(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    ps.pd.read_parquet = counted
    try:
        store._validate_record(rec)   # first access: prove the bytes
        assert calls["n"] == 1
        store._validate_record(rec)   # second and third: trust size + mtime
        store._validate_record(rec)
    finally:
        ps.pd.read_parquet = real

    assert calls["n"] == 1, (
        "an unstamped partition must be proven once, not on every read")


# ---------------------------------------------------------------- breadth
def test_a_suspended_name_has_no_moving_average():
    """Breadth counts sessions, not rows.

    A stock that missed three of the last twenty sessions has no MA20. Counting
    rows instead of dates would average the seventeen it does have, which reads
    as participation that was never there.
    """
    from service.research.discover import _breadth

    dates = [f"202601{d:02d}" for d in range(1, 21)]
    rows = [{"ts_code": "A", "trade_date": d, "close": float(i + 1)}
            for i, d in enumerate(dates)]
    # B trades the first seventeen sessions, then halts: no MA20 exists for it.
    rows += [{"ts_code": "B", "trade_date": d, "close": 1.0} for d in dates[:17]]
    frame = pd.DataFrame(rows)

    # A is above its own MA20; B is not counted as above, and is not in the
    # denominator either because it has no close on the last session.
    assert _breadth(frame, 20) == pytest.approx(1.0)

    # A window B does fill: over five sessions both count.
    short = pd.DataFrame(rows[-5 * 2:])
    assert 0.0 <= _breadth(short, 5) <= 1.0


# ------------------------------------------------------- drawdown ordering
def test_drawdown_is_compounded_in_the_order_the_trades_happened():
    """A drawdown is a property of the sequence, not of the list order.

    ``list_cases`` is newest-first. Compounding in that order walks the equity
    curve backwards, so a strategy that fell early and recovered reported a deep
    drawdown — and the allocator halves capital on `dd <= -8%`.
    """
    from service.trading.allocator import stats_from_cases

    cases = [
        {"strategy": "s", "pnl_pct": 0.10, "closed_at": "2026-01-03T10:00:00"},
        {"strategy": "s", "pnl_pct": -0.20, "closed_at": "2026-01-02T10:00:00"},
        {"strategy": "s", "pnl_pct": 0.05, "closed_at": "2026-01-01T10:00:00"},
    ]
    stats = stats_from_cases(cases)["s"]
    # 1.05 -> 0.84 -> 0.924; peak 1.05, so the trough is -20%, not -16%.
    assert stats.max_drawdown == pytest.approx(-0.20, abs=1e-9), (
        "the trough must be measured from the running peak of the real sequence")


# ------------------------------------------------------------ stop ownership
def test_a_reopened_position_without_a_stop_has_no_stop():
    """The stop belongs to the current entry, not to any entry that had one.

    Taking the newest BUY *that carries a stop* kept a level written three
    round-trips ago: the reopened position inherited it, ``duty`` asked for an
    exit every morning, and ``propose`` 409'd on a number nobody chose.
    """
    from service.trading import stops

    tickets = [
        {"symbol": "600000.SH", "side": "BUY", "stop_price": None,
         "ticket_id": "new"},                       # newest: no stop
        {"symbol": "600000.SH", "side": "BUY", "stop_price": "8.00",
         "ticket_id": "old"},                       # three round-trips ago
    ]
    desk = SimpleNamespace(paper=SimpleNamespace(list_tickets=lambda **_: tickets))
    assert stops.stop_map(desk) == {}

    tickets[0]["stop_price"] = "9.50"
    assert stops.stop_map(desk)["600000.SH"]["ticket_id"] == "new"


# --------------------------------------------------------------- impact floor
def test_impact_refuses_a_missing_adv_instead_of_inventing_one_share():
    """``max(adv/price, 1.0)`` turned "unknown ADV" into "ADV is one share".

    A 20,000-share order then needed 66,667 sessions, and the schedule loop ran
    that many times — an endpoint that hangs rather than saying it cannot answer.
    """
    from service.trading.impact import estimate

    with pytest.raises(ValueError, match="adv_cny"):
        estimate(20_000, 10.0, 0.0, 0.2)

    # A tiny but real ADV stays bounded rather than scheduling tens of thousands
    # of days.
    result = estimate(1_000_000_000, 1.0, 1.0, 0.2)
    assert result.days_needed <= 250
    assert len(result.schedule) <= 250


# ------------------------------------------------------------ benchmark noise
def test_a_zero_previous_close_does_not_poison_the_benchmark():
    """One ``pre_close == 0`` row made the whole market series inf.

    ``groupby.mean`` skips NaN but not inf, and ``cumprod`` then carries it
    forever: every excess becomes NaN, and NaN is not valid JSON.
    """
    from service.trading.arena import _market_returns

    frame = pd.DataFrame({
        "trade_date": ["20260105", "20260105", "20260106", "20260106"],
        "close": [10.0, 11.0, 11.0, 12.1],
        "pre_close": [0.0, 10.0, 11.0, 11.0],
    })
    store = SimpleNamespace(read=lambda *a, **k: frame)
    series = _market_returns(store)
    assert series.notna().all()
    assert series.map(lambda v: abs(v) != float("inf")).all()
    # Two names that day: 11/11-1 = 0 and 12.1/11-1 = 10%, so the equal-weight
    # mean is 5%. The point is that it is a number, not inf.
    assert series.loc["20260106"] == pytest.approx(0.05, abs=1e-9)


# ------------------------------------------------------------ attribution gate
def test_unmeasurable_residual_alpha_blocks_promotion():
    """``x or 0.0`` does not catch NaN — NaN is truthy.

    An attribution whose residuals were uncomputable therefore passed the one
    gate that exists to stop a candidate from being promoted on exposures alone.
    """
    from service.research.attribution import blocks_promotion

    summary = {"n": 200, "coverage": 0.9, "mean_matched_excess": float("nan"),
               "t_stat": 3.0}
    blocked, reason = blocks_promotion(summary)
    assert blocked is True
    assert "alpha" in reason


# ------------------------------------------------------ partial fill quantity
def test_a_volume_capped_partial_sell_rounds_down_instead_of_cancelling():
    """The cap decides *how much* fills, not whether the order is legal.

    ``sell_lot`` answers "may this be submitted", and 150 of 300 really is
    illegal. Asked for a partial fill it returned 0, so a volume-capped sell was
    cancelled outright while the same buy filled.
    """
    from datetime import datetime

    from service.kernel.calendar import TradingCalendar
    from service.kernel.execution import BarExecutionModel
    from service.kernel.models import Bar, Order, OrderType

    inst = Instrument(symbol="600000.SH")
    bar = Bar(symbol="600000.SH", ts=datetime(2026, 1, 5, 9, 30),
              open=D("10.00"), high=D("10.40"), low=D("9.80"), close=D("10.00"),
              pre_close=D("10.00"), volume=1_000, amount=D("10000"),
              up_limit=D("11.00"), down_limit=D("9.00"), prior_volume=2_500)
    order = Order(order_id="O1", client_order_id="c1", symbol="600000.SH",
                  side=Side.SELL, qty=300, order_type=OrderType.MARKET)

    result = BarExecutionModel().match(order, bar, inst, bar.ts, TradingCalendar.always())

    assert result.filled, result.detail
    # 2,500 x 10% = 250 shares of capacity, rounded down to the 100-share step.
    assert result.fill.qty == 200


# --------------------------------------------------------- weight-sized sells
def test_a_weight_sized_sell_is_not_rejected_for_having_no_quantity():
    """The qty is *derived* from the weight; re-reading ``intent.qty`` threw it away."""
    ledger = Ledger.fresh("5000")
    ledger.account.positions["600000.SH"] = Position(
        symbol="600000.SH", total_qty=1000, available_qty=1000, avg_cost=D("10"))

    order = ledger.oms.submit(
        # Equity is 5,000 cash + 1,000 x 10 = 15,000. A **target** weight of
        # 20% is 3,000 of value, so the sell is 10,000 - 3,000 = 7,000 at 10.00
        # = 700 shares, which leaves the position where the caller asked for it.
        # (Before 2026-11-13 this meant "sell 20% of equity" = 300 shares: the
        # same field meant "target" on a buy and "order size" on a sell.)
        # The point of the case is that the qty is *derived*, not that any
        # weight is legal.
        Intent(symbol="600000.SH", side=Side.SELL, target_weight=D("0.2")),
        pd.Timestamp(f"{DAY} 10:00:00").to_pydatetime(),
        ledger.account,
        Instrument(symbol="600000.SH"),
        _bar(),
        _AlwaysCalendar(),
    )
    assert order.reason != "BAD_QTY", order.reason
    assert order.status != OrderStatus.REJECTED, order.reason
    assert order.qty == 700


# ------------------------------------------------------------ cancel windows
def test_the_opening_auction_gap_accepts_orders_but_not_cancellations():
    """09:25-09:30 is not a cancellable pre-open: the auction has already cleared."""
    from datetime import datetime

    from service.kernel.calendar import TradingCalendar

    calendar = TradingCalendar.always()
    assert not calendar.can_cancel(datetime(2026, 1, 5, 9, 26))
    assert calendar.can_submit(datetime(2026, 1, 5, 9, 26))
    assert calendar.can_cancel(datetime(2026, 1, 5, 9, 19))
    assert calendar.can_cancel(datetime(2026, 1, 5, 10, 0))


# -------------------------------------------------------------- sleeve default
def test_an_unassigned_position_spends_from_core():
    """``Intent``, ``Order`` and ``RiskLimits`` all default to CORE.

    ``sleeve_of`` used to return AGGRESSIVE for every non-ETF, so a position with
    no sleeve entry was counted against the *tighter* 60% cap while every other
    layer called it CORE — one fact, two budgets.
    """
    from service.trading.book import PaperBook

    book = object.__new__(PaperBook)
    book.state = {}
    book.ledger = SimpleNamespace(account=SimpleNamespace(peek=lambda symbol: None))

    assert book.sleeve_of("600000.SH") == "CORE"
    assert book.sleeve_of("510300.SH") == "CORE"

    book.state = {"sleeves": {"600000.SH": "AGGRESSIVE"}}
    assert book.sleeve_of("600000.SH") == "AGGRESSIVE"


# ------------------------------------------------------------ cost stress scale
def test_the_cost_multiplier_applies_to_commission_not_only_slippage():
    """``spec.effective_cost_bps`` doubles fees *and* slippage; the account curve doubled one.

    On a 5,000 CNY account the 5-yuan minimum commission is the dominant term
    (50bps on a 1,000 CNY ticket against 20bps of slippage), so a stress test
    that skipped it was running at roughly 1.3x and calling it 2x.
    """
    from service.kernel.models import Instrument
    from service.research.capital import _fees

    model = CostModel()
    inst = Instrument(symbol="600000.SH")
    base = _fees(model, 1_000, inst, "buy")
    stressed = _fees(model, 1_000, inst, "buy", 2.0)
    # 5.00 minimum commission + 0.01 transfer fee on a 1,000 CNY ticket.
    assert base == pytest.approx(5.01), "the minimum commission dominates at this size"
    assert stressed == pytest.approx(2 * base)
