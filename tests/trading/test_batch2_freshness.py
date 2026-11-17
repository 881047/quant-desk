"""B2 regressions: the 90/120/360 quote-age layering is auditable, not implicit.

Three numbers answer three different questions and are deliberately NOT merged:

* 90 s  (``live_fill.max_quote_age``) — may this quote move cash? Irreversible,
  so the strictest gate.
* 120 s (``realtime.stale_after``)    — may this quote answer "is a stop
  breached"? State judgement has an ``unknown`` / fail-closed branch.
* 360 s (``frame.FRAME_STALE_AFTER``) — is the capture loop itself alive?

An age in (90, 120] is therefore refused for a fill while still used to judge a
breach. That is a *cross-stage* statement (propose gate vs approve fill, watch
breach-judgement vs autostop execution) — it is not two branches inside one
``_execute_live`` call, and the cases below never pretend it is.

Red on HEAD ``000aba3``: ``quote_freshness`` did not exist, ``Refusal`` carried
no ``freshness`` structure, and neither ``stop_gate`` nor ``tripwire.evaluate``
reported ``freshness_basis`` / ``threshold_seconds``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from service.kernel.clock import SHANGHAI
from service.kernel.models import Instrument
from service.trading import live_fill, tripwire
from tests.trading.helpers import pick_symbols


def _seed_stopped_position(desk, symbol: str, day: str, stop: str) -> None:
    """A held position plus the filled BUY ticket that carries its stop.

    Seeded directly, without propose/approve, so this case depends only on
    ``observation.stop_gate`` — the unit under test — and not on the decision
    path. (It also keeps the RED control honest: reverting ``observation.py``
    alone isolates the batch-2 change.)
    """
    snap = desk.market.snapshot(symbol, day)
    position = desk.book.ledger.account.position(symbol)
    position.total_qty = 100
    position.available_qty = 100
    position.avg_cost = snap.last
    desk.paper.save_ticket({
        "ticket_id": "td-batch2-stop",
        "created_at": "2026-11-05T09:26:00+08:00",
        "expires_at": f"{day}T23:59:59+08:00",
        "status": "filled",
        "symbol": symbol,
        "side": "BUY",
        "sleeve": "CORE",
        "stop_price": stop,
        "stop_price_final": stop,
        "qty": 100,
    })


def _stub_quote(*, age_seconds: float, quote_ts: str) -> SimpleNamespace:
    """Just enough of ``realtime.Quote`` for the freshness path (no vendor)."""
    return SimpleNamespace(
        symbol="600000.SH", is_realtime=True, source="ths",
        quote_ts=quote_ts, age_seconds=age_seconds,
        # Full numeric set so the stub can also clear the stale gate and reach
        # the bar-building path (the loosen case below needs that).
        last=11.0, pre_close=10.0, high=11.0, low=10.0,
        up_limit=11.0, down_limit=9.0, volume=100_000, amount=1_100_000.0,
        suspended=False,
    )


# ---------------------------------------------------------------------------
# B2 #11 · the shared pure function states the layering explicitly
# ---------------------------------------------------------------------------
def test_quote_freshness_layers_are_explicit():
    from service.trading.realtime import quote_freshness

    info = quote_freshness(105.0)
    assert info["tradable"] is False          # 105 > 90: may not move cash
    assert info["measurable"] is True         # 105 <= 120: may judge a breach
    assert info["max_quote_age"] == 90.0
    assert info["stale_after"] == 120.0
    assert info["age_seconds"] == 105.0
    # The basis names all three thresholds and says they are not merged.
    basis = info["basis"]
    for threshold in ("90", "120", "360"):
        assert threshold in basis, basis
    assert ("不可合并" in basis) or ("分层" in basis), basis

    # A missing age is neither tradable nor measurable.
    none = quote_freshness(None)
    assert none["tradable"] is False and none["measurable"] is False


# ---------------------------------------------------------------------------
# B2 #12 · a stale-quote refusal carries both thresholds on the wire
# ---------------------------------------------------------------------------
def test_stale_quote_refusal_carries_both_thresholds():
    now = datetime(2026, 11, 5, 10, 30, tzinfo=SHANGHAI)
    quote = _stub_quote(age_seconds=105.0,
                        quote_ts=(now - timedelta(seconds=105)).isoformat())
    market = SimpleNamespace(calendar=SimpleNamespace(is_trading_day=lambda day: True))

    live, refusal = live_fill.build(market, "600000.SH", quote, now=now)

    assert live is None
    assert refusal is not None and refusal.code == "STALE_QUOTE"
    # Refused for a fill (>90s) while still measurable for state judgement
    # (<=120s): the deliberate cross-stage split B2 makes explicit.
    assert "有意分层" in refusal.message
    freshness = refusal.as_dict()["freshness"]
    assert freshness["max_quote_age"] == 90.0
    assert freshness["stale_after"] == 120.0
    assert freshness["tradable"] is False
    assert freshness["measurable"] is True


# ---------------------------------------------------------------------------
# B2 #13 · the propose-side gate annotates ONLY the (90, 120] divergence window
# ---------------------------------------------------------------------------
def test_stop_gate_marks_basis_in_divergence_window(desk):
    from service.trading import observation
    from tests.trading.test_live_fill import _quote

    day = desk.market.latest_bar_date()
    symbol = pick_symbols(desk, 1)[0]
    _seed_stopped_position(desk, symbol, day, stop="0.01")

    # 105s: measurable (breach can be judged) but not tradable (fill would be
    # refused). This is exactly the window B2 must make auditable.
    divergent = _quote(desk, symbol, age_seconds=105.0, is_realtime=True)
    gate = observation.stop_gate(desk, day, quotes={symbol: divergent})
    assert symbol not in gate["unknown"], "105s 在 120s 状态窗口内，应判可测"
    assert gate["threshold_seconds"] == 120.0
    assert "freshness_basis" in gate
    assert "90" in gate["freshness_basis"] and "120" in gate["freshness_basis"]

    # 150s is outside both windows: an ordinary answer keeps the exact three-key
    # shape that existing consumers assert with a whole-dict equality.
    outside = _quote(desk, symbol, age_seconds=150.0, is_realtime=True)
    plain = observation.stop_gate(desk, day, quotes={symbol: outside})
    assert set(plain) == {"breached", "unknown", "no_stop"}


# ---------------------------------------------------------------------------
# B2 #14 · tripwire always reports which freshness basis it judged on
# ---------------------------------------------------------------------------
def test_tripwire_carries_freshness_basis(desk):
    day = desk.market.latest_bar_date()
    # A flat book needs no quote; the structure is attached unconditionally.
    result = tripwire.evaluate(desk, day=day, include_tape=False)

    assert "freshness_basis" in result
    assert result["threshold_seconds"] == 120.0
    basis = result["freshness_basis"]
    for threshold in ("90", "120"):
        assert threshold in basis, basis
    assert "measurable" in basis
    # The state-side window is the one that decides findings here.
    assert result["checked"]["live_quotes"] == 0


# ---------------------------------------------------------------------------
# MINOR-9 · the public ``max_age`` override must actually bind, both ways
# ---------------------------------------------------------------------------
def test_max_age_override_can_loosen_and_tighten():
    """``build(..., max_age=...)`` is a published parameter that went untested.

    Because nobody passed it, B2 broke it silently: the decision OR-ed in the
    helper's config-derived ``tradable`` (fixed at 90), so ``max_age`` could only
    tighten, never loosen — ``max_age=200`` still refused a 105s quote while the
    message said "limit 200". And ``tradable`` was computed from the quote's
    self-reported ``age`` rather than ``effective_age = max(age, actual_age)``, so
    a quote that said "88s" but was actually 118s old read as tradable.

    ``LiveBar`` carries no freshness dict (there is no refusal to attach one to),
    so the effective bound is asserted on a *same-override refusal* below — the
    only place ``build`` exposes ``effective_max_age``.
    """
    now = datetime(2026, 11, 5, 10, 30, tzinfo=SHANGHAI)
    market = SimpleNamespace(
        calendar=SimpleNamespace(is_trading_day=lambda day: True),
        instrument=lambda symbol, day: Instrument("600000.SH"),
    )

    # ---- 1. Loosen: 105s sits inside an explicit 200s bound, so it must pass.
    at_105 = _stub_quote(age_seconds=105.0,
                         quote_ts=(now - timedelta(seconds=105)).isoformat())
    live, refusal = live_fill.build(market, "600000.SH", at_105, now=now, max_age=200)
    assert refusal is None, refusal
    assert live is not None
    assert live.age_seconds == 105.0

    # The override does not rewrite the configured default, and the effective
    # bound really is 200: a quote past 200 is refused with both numbers pinned.
    at_245 = _stub_quote(age_seconds=245.0,
                         quote_ts=(now - timedelta(seconds=245)).isoformat())
    _, beyond = live_fill.build(market, "600000.SH", at_245, now=now, max_age=200)
    assert beyond is not None and beyond.code == "STALE_QUOTE"
    assert beyond.freshness["effective_max_age"] == 200.0
    assert beyond.freshness["max_quote_age"] == 90.0

    # ---- 2. Tighten: the same 105s quote is refused inside an explicit 60s bound.
    _, tight = live_fill.build(market, "600000.SH", at_105, now=now, max_age=60)
    assert tight is not None and tight.code == "STALE_QUOTE"
    assert "60" in tight.message
    assert tight.freshness["effective_max_age"] == 60.0

    # ---- 3. MINOR-8 guard: judge on effective_age, not the self-reported age.
    # "88s" looks fresh against a 90s limit, but quote_ts proves it is 118s old.
    stale = _stub_quote(age_seconds=88.0,
                        quote_ts=(now - timedelta(seconds=118)).isoformat())
    _, inconsistent = live_fill.build(market, "600000.SH", stale, now=now,
                                      max_age=90)
    assert inconsistent is not None and inconsistent.code == "STALE_QUOTE"
    assert inconsistent.freshness["tradable"] is False
    assert inconsistent.freshness["age_seconds"] == 118.0
    # The annotation must agree with the decision it annotates: this call applied
    # the default 90s bound, and the message says so.
    assert inconsistent.freshness["effective_max_age"] == 90.0
    assert "上限 90 秒" in inconsistent.message


def test_effective_age_within_max_age_is_tradable():
    """MINOR-8, positive side: a quote whose two ages agree is not falsely refused.

    ``tradable`` must follow ``effective_age = max(age, actual_age)``. When the
    self-reported age and the wall-clock age are the same 88s, both are inside a
    90s limit and the fill is allowed (no over-correction into always-refuse).
    """
    now = datetime(2026, 11, 5, 10, 30, tzinfo=SHANGHAI)
    market = SimpleNamespace(
        calendar=SimpleNamespace(is_trading_day=lambda day: True),
        instrument=lambda symbol, day: Instrument("600000.SH"),
    )
    consistent = _stub_quote(age_seconds=88.0,
                             quote_ts=(now - timedelta(seconds=88)).isoformat())

    live, refusal = live_fill.build(market, "600000.SH", consistent, now=now,
                                    max_age=90)

    assert refusal is None, refusal
    assert live is not None
    from service.trading.realtime import quote_freshness

    assert quote_freshness(88.0)["tradable"] is True


def test_a_live_bar_states_its_capacity_basis():
    """Capacity is judged on a printed session, not on today's partial volume.

    ``Bar`` defaults ``prior_volume`` to 0, which lets the matcher fall back to
    today's volume — a number that grows with the clock. The live builder now
    states the previous session's turnover, so the same order cannot absorb more
    at 14:00 than it could at 09:30.
    """
    now = datetime(2026, 11, 5, 10, 30, tzinfo=SHANGHAI)
    quote = _stub_quote(age_seconds=1.0,
                        quote_ts=(now - timedelta(seconds=1)).isoformat())
    market = SimpleNamespace(
        calendar=SimpleNamespace(is_trading_day=lambda day: True,
                                 prev=lambda day: "20261104"),
        instrument=lambda symbol, day: Instrument("600000.SH"),
        traded_volume=lambda symbol, day: 7_000_000,
    )

    live, refusal = live_fill.build(market, "600000.SH", quote, now=now)

    assert refusal is None, refusal
    assert live.bar.priced_at == "live"
    assert live.bar.prior_volume == 7_000_000
