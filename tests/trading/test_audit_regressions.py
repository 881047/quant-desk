"""Regressions for the 2026-10 audit.

One test per defect that was found by reading the code rather than by a failing
assertion, because that is the only kind that stays fixed: a bug with no test is
a bug on a timer. Each docstring states the failure it prevents, since "why is
this assertion here" is the question the next reader will actually have.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

DAY = "20260105"


# --------------------------------------------------------------- price bands
def test_the_band_rounds_half_up_like_the_exchange():
    """9.95 x 1.10 is 10.945; the exchange prints 10.95, Decimal defaults to 10.94.

    Half-even is the default for ``quantize``, so every derived ceiling was one
    fen low whenever the previous close ended in 5 — about 5% of the market. A
    band one fen low is not cosmetic: ``fill_price_at_open`` clamps a buy to
    ``up_limit``, so a legal fill was priced below the price it printed at, and
    ``blocked_at_open`` accepts a limit-down fill one fen above the real floor.
    """
    from service.kernel.models import D, Instrument
    from service.trading.realtime import QuoteService

    inst = Instrument(symbol="600000.SH", limit_pct=D("0.10"))
    up, down = QuoteService._limits(None, "600000.SH", D("9.95"), inst)
    assert up == D("10.95"), "round half up, not half to even"
    assert down == D("8.96")

    up, down = QuoteService._limits(None, "600000.SH", D("4.05"), Instrument(
        symbol="600000.SH", limit_pct=D("0.10")))
    assert up == D("4.46")
    assert down == D("3.65")


# ------------------------------------------------------------------ fee model
def test_a_partial_fee_schedule_falls_back_to_the_defaults():
    """Only-overriding-commission is the normal case, not an error.

    ``config[key]`` was a hard index, so "my broker charges 0.02%" raised
    ``KeyError: min_commission`` inside the booking path — and a booking that
    fails is worse than a fee that is slightly off.
    """
    from service.trading.fees import KEYS, compute, schedule

    result = compute("BUY", 10.0, 100, config={"commission_rate": 0.0002})
    assert result["commission"] == pytest.approx(max(5.0, 1000.0 * 0.0002))
    assert result["stamp_duty"] == 0.0
    assert result["total"] > 0
    # The missing keys come from configs/costs.yaml now, not from a literal dict
    # in the module: there is no second copy of the schedule to compare against.
    assert set(schedule()) == set(KEYS)


# ----------------------------------------------------------------- side text
@pytest.mark.parametrize("raw,expected", [
    ("SELL B", "SELL"),        # was BUY: the bare "B" was tested before "SELL"
    ("卖出B股", "SELL"),        # was "": an unexplained rejection
    ("B股买入", "BUY"),
    ("B", "BUY"),
    ("S", "SELL"),
    ("证券买入", "BUY"),
    ("卖出", "SELL"),
    ("", ""),
    ("???", ""),
])
def test_a_bare_letter_never_beats_an_explicit_side(raw, expected):
    """A B-share sale recorded as a buy doubles the position and reverses cash."""
    from service.trading.external import normalize_side

    assert normalize_side(raw) == expected


# ------------------------------------------------------------------- JSON NaN
def test_a_non_finite_number_becomes_null_instead_of_a_500():
    """Starlette renders with ``allow_nan=False``; one NaN killed the endpoint.

    A suspended stock's ``close`` arrives as ``"-"`` and is coerced to NaN, so
    ``/market/bars`` and ``/market/pools`` returned 500 on any day with one dirty
    row — and the 4xx/5xx breaker then disabled the route for a minute, turning a
    data gap into "the desk cannot be read".
    """
    import json

    from service.api.responses import SafeJSONResponse

    payload = {"ok": True, "bars": [{"close": 9.9}, {"close": math.nan}],
               "nested": {"mean": math.inf, "ok": 1.5}}
    rendered = SafeJSONResponse(content=payload).body
    decoded = json.loads(rendered)
    assert decoded["bars"][1]["close"] is None
    assert decoded["nested"]["mean"] is None
    assert decoded["nested"]["ok"] == 1.5


def test_a_finite_payload_is_rendered_exactly_as_before():
    """The sanitiser must stay off the hot path: no re-serialisation, no reformat."""
    from fastapi.responses import JSONResponse

    from service.api.responses import SafeJSONResponse

    payload = {"a": 1, "b": [1.5, 2.5], "c": "x"}
    assert SafeJSONResponse(content=payload).body == JSONResponse(content=payload).body


# --------------------------------------------------------------- date inputs
@pytest.mark.parametrize("bad", ["../../etc/passwd", "2026", "", "abc", "2026/13/45x"])
def test_a_date_parameter_that_is_not_a_session_is_refused(bad):
    """Either it becomes a session date, or it is refused. Never passed through.

    ``"20261026" <= "2026-10-30"`` is False, so an un-normalised parameter made
    ``/data/brief`` report a month-stale dataset as current and stamped a 14:45
    ``/market/pools`` snapshot ``settled``. The same value reached
    ``reports/facts/../../x.json`` as a real write path.
    """
    from service.api.trading import _day

    with pytest.raises(HTTPException) as err:
        _day(bad)
    assert err.value.status_code == 422


def test_a_hyphenated_date_is_normalised_rather_than_refused():
    """People write 2026-10-30. Accepting it is fine; comparing it raw is not."""
    from service.api.data import _day as data_day
    from service.api.trading import _day

    assert _day("2026-10-30") == "20261030"
    assert _day("2026/10/30") == "20261030"
    assert _day("20261030") == "20261030"
    assert _day(None) is None
    assert data_day("2026-10-30") == "20261030"
    assert data_day(None) is None


# ------------------------------------------------------------ arena benchmark
def test_a_book_with_no_closed_case_does_not_read_the_market_history(desk, monkeypatch):
    """Zero closed trades meant reading twenty years of daily bars — for nothing.

    ``market_for_cases`` falls through to a full read when the case list is
    empty, and both consumers of that series (per-case excess, the market NAV)
    need a case to exist. Every dashboard call paid ~8 seconds for it.
    """
    from service.trading import arena

    calls: list[str] = []
    original = desk.data.read

    def counting(dataset, *args, **kwargs):
        calls.append(dataset)
        return original(dataset, *args, **kwargs)

    monkeypatch.setattr(desk.data, "read", counting)
    monkeypatch.setattr(desk.paper, "list_cases", lambda *a, **k: [])
    board = arena.scoreboard(desk)
    assert "daily" not in calls, "no case, no holding window, no benchmark series"
    assert board["ci_position"] == "sample_below_20"
    assert board["market_window"] is None
    assert "verdict" not in board, "arena 不再替读者下结论"


# -------------------------------------------------------------- stop gate
def test_unknown_stop_and_missing_stop_are_not_the_same_answer(desk, monkeypatch):
    """``unknown`` blocks every buy; a position with no stop must not land there."""
    from service.trading import observation

    monkeypatch.setattr("service.trading.stops.open_stops", lambda *a, **k: [])
    monkeypatch.setattr("service.trading.stops.stopped_symbols", lambda *a, **k: set())
    gate = observation.stop_gate(desk, desk.market.latest_bar_date(), {})
    assert gate["unknown"] == []
    assert gate["breached"] == []
    assert isinstance(gate["no_stop"], list)


# --------------------------------------------------------------- api breaker
def test_a_ticket_id_does_not_get_its_own_failure_bucket():
    """The breaker has to see the *route*, or it never fires where retries happen.

    Keyed on the literal path, `POST /decision/td-...-aaaa/approve` and
    `.../bbbb/approve` counted separately, so a loop that invented a new ticket
    id each time was invisible to the one control meant to stop it.
    """
    from service.api.app import _breaker_key

    first = _breaker_key("POST", "/decision/td-20261030-ab12cd34/approve")
    second = _breaker_key("POST", "/decision/td-20261030-ffffeeee/approve")
    assert first == second == "POST /decision/*/approve"
    assert _breaker_key("GET", "/market/bars") == "GET /market/bars"


@pytest.mark.parametrize("status", [409, 422, 401, 403, 429])
def test_a_legitimate_refusal_does_not_trip_the_breaker(status):
    """409 is this desk's normal answer: stop breached, ticket expired, T+1.

    Counting it meant a correctly-behaving agent could disable /decision/propose
    by asking while a position sat below its stop — the breaker punishing the
    behaviour it exists to protect.
    """
    from service.api.app import _counts_toward_breaker

    assert _counts_toward_breaker(status) is False


@pytest.mark.parametrize("status", [500, 503])
def test_a_crash_still_trips_the_routes_breaker(status):
    from service.api.app import _counts_toward_breaker

    assert _counts_toward_breaker(status) is True


@pytest.mark.parametrize("status", [404, 405])
def test_a_wrong_route_is_counted_but_against_itself(status):
    """404 describes a path that does not exist, and 404s need no token.

    Counting them in the *matched route's* bucket meant any local process could
    send 30 requests to a misspelled path that normalises to the same shape and
    stop a real endpoint for a minute. They are still throttled — just in a bucket
    keyed on the path that missed, so a wrong path can only silence itself.
    """
    from service.api.app import _UNROUTED_FAILURES, _counts_toward_breaker

    assert status in _UNROUTED_FAILURES
    assert _counts_toward_breaker(status) is False


# --------------------------------------------------------------- cache bound
def test_the_market_cache_evicts_instead_of_growing_forever():
    """TTL bounds age, not size: an unread entry never expires.

    Every symbol ever asked about kept its frame for the life of the process, so
    a month of profiles and scans accumulated hundreds of megabytes.
    """
    from service.trading.market import MarketRepository

    class _Store:
        registry = None

    market = MarketRepository(_Store(), ttl_seconds=3600.0, max_entries=32)
    for i in range(400):
        market._cached(f"hist:{i}", lambda i=i: i)
    assert len(market._cache) <= 32
    assert len(market._versions) == len(market._cache)


# ------------------------------------------------------- research state cache
def test_a_latest_state_read_is_answered_without_touching_the_rows():
    """`cached_state` promised "costs nothing here" and hashed 16M rows instead.

    The manifest is content-addressed — the writer derives each checksum from the
    frame it stored — so manifest + window is an *exact* key for a read of the
    store's own data over that window. Hashing the frame re-derived information
    the writer had already computed, at 2.6 seconds a call. A caller that hands in
    its own frame is still hashed in full: that frame has no manifest of its own,
    and a wrong hit would report a state built from other data.
    """
    import json

    import pandas as pd

    from service.research import state as state_mod

    class _Row:
        def __init__(self, i):
            self.dataset, self.partition = "daily", str(i)
            self.checksum, self.schema_hash, self.snapshot_id = f"c{i}", "s", f"snap{i}"
            self.cover_start = self.cover_end = "20260102"
            self.status, self.ingested_at = "ok", "2026-01-02T00:00:00"

    class _Store:
        registry = type("R", (), {"list_manifest": lambda self, dataset=None:
                                  [_Row(1), _Row(2)]})()
        reads = 0

        def read(self, *args, **kwargs):
            type(self).reads += 1
            raise AssertionError("a manifest-keyed hit must not read the rows")

    store = _Store()
    cache = Path(tempfile.mkdtemp(prefix="state-cache-"))
    start, end = "20050101", "20260102"
    key = state_mod._window_digest(state_mod._manifest_digest(store), start, end)

    cached = pd.DataFrame({"bucket": ["calm"]}, index=pd.Index([end], name="trade_date"))
    cached.to_parquet(cache / f"state_{start}.parquet")
    (cache / f"state_{start}.meta.json").write_text(json.dumps({
        "last_date": end, "start": start, "schema": state_mod.SCHEMA,
        "source_digest": key, "sessions": 1}), encoding="utf-8")

    out = state_mod.cached_state(store, last=end, as_of=end, cache_dir=cache)
    assert list(out.index) == [end]
    assert _Store.reads == 0


def test_a_changed_manifest_invalidates_the_state_cache():
    """The cheap key has to change when the data does — that is the whole bet."""
    from service.research import state as state_mod

    base = state_mod._window_digest("manifest-a", "20050101", "20260102")
    same = state_mod._window_digest("manifest-a", "20050101", "20260102")
    other_data = state_mod._window_digest("manifest-b", "20050101", "20260102")
    other_window = state_mod._window_digest("manifest-a", "20050101", "20260103")
    assert base == same
    assert base != other_data
    assert base != other_window
