"""B1-b / B1-c / B1-a#5 regressions: one fee schedule, computed not backed out.

Three separate fixes live here because they all touch "how a fill is priced":

* B1-b — ``market._cost_rate`` used to swallow a missing/malformed rate and fall
  back to a literal (``0.0005`` / ``0.00001``), so a broken config was silently
  booked at a guessed fee. It now fails fast, like ``kernel.costs._rate``.
* B1-c — ``fees.compute`` derived stamp duty by subtraction
  (``total - commission - transfer``), which attributes any fourth cost line to
  stamp duty, and accepted ``config`` + ``instrument`` together, silently
  letting one override the other. It now reads the rate directly and refuses the
  ambiguous pair.
* B1-a #5 — ``tca.summary`` must report the queue penalty under ``impact_bps``,
  not ``fee_bps``, without changing ``total_bps``.

Red on HEAD ``000aba3``: the fallback returned a number instead of raising, the
pair did not raise, subtraction produced 1.42 for stamp duty, and ``tca`` had no
``impact_bps`` bucket.
"""

from __future__ import annotations

import pytest

from service import settings
from service.kernel.costs import CostModel
from service.kernel.models import Board, D, Instrument, money
from service.trading import fees
from service.trading.market import _cost_rate
from service.trading.tca import summary
from service.settings import MissingParameter

# ---------------------------------------------------------------------------
# B1-a #5 · tca buckets the penalty as impact, not as a fee
# ---------------------------------------------------------------------------
class _Store:
    def __init__(self, fills):
        self._fills = fills

    def recent_fills(self, limit: int = 500):
        return self._fills[:limit]


class _Desk:
    def __init__(self, fills):
        self.paper = _Store(fills)


def _record(**overrides) -> dict:
    base = {
        "ticket_id": "t1", "symbol": "600000.SH", "side": "BUY", "qty": 100,
        "price": "11.00", "ref_price": "11.00", "fees": "5.01",
        "limit_up_penalty": "2.20", "fill_basis": "next_open",
    }
    base.update(overrides)
    return base


def test_tca_buckets_penalty_as_impact():
    new = summary(_Desk([_record()]))["fills"][0]
    # 5.01 / 1100 = 45.5 bps — the three CostModel lines only.
    assert new["fee_bps"] == 45.55
    # 2.20 / 1100 = 20.0 bps — the queue penalty, on its own line.
    assert new["impact_bps"] == 20.0
    assert new["total_bps"] == 65.55

    # The same money, reclassified: a pre-fix record (penalty already inside
    # `fees`, no penalty key) yields the identical total through the new code.
    old = summary(_Desk([_record(fees="7.21", limit_up_penalty=None)]))["fills"][0]
    assert old["fee_bps"] == 65.55
    assert old["impact_bps"] == 0.0
    assert old["total_bps"] == 65.55


# ---------------------------------------------------------------------------
# B1-b · the fee rate is read or it fails; there is no silent fallback
# ---------------------------------------------------------------------------
def _stock(desk, day: str) -> str:
    """A real non-ETF, non-CB symbol: those zero their own rates by branch."""
    for symbol in _picked(desk, 8):
        inst = desk.market.instrument(symbol, day)
        if inst.board not in (Board.ETF, Board.CB) and not inst.is_etf:
            return symbol
    pytest.skip("no plain stock in the fixture universe")


def _picked(desk, n: int) -> list[str]:
    from tests.trading.helpers import pick_symbols

    return pick_symbols(desk, n)


def test_cost_rate_fails_fast_when_missing(desk, monkeypatch):
    day = desk.market.latest_bar_date()
    symbol = _stock(desk, day)

    def missing(key: str):
        raise MissingParameter(key)

    # The old `except (KeyError, ValueError, ArithmeticError)` swallowed exactly
    # this and returned the hardcoded 0.0005, so a broken schedule was booked at
    # a guessed rate. Both the reader and its caller must now propagate.
    monkeypatch.setattr(settings, "decimal", missing)

    with pytest.raises(MissingParameter):
        _cost_rate("stamp_duty_sell")
    with pytest.raises(MissingParameter):
        desk.market.instrument(symbol, day)


def test_cost_rate_single_source_values(desk):
    """Regression guard: deleting the fallback must not change the numbers."""
    day = desk.market.latest_bar_date()
    symbol = _stock(desk, day)

    inst = desk.market.instrument(symbol, day)
    assert inst.stamp_duty == D("0.0005")
    assert inst.transfer_fee == D("0.00001")
    # Same reader the CostModel uses — one schedule, one answer.
    assert settings.decimal("costs.stamp_duty_sell") == D("0.0005")
    assert settings.decimal("costs.transfer_fee") == D("0.00001")


# ---------------------------------------------------------------------------
# B1-c · stamp duty is computed from the rate, not backed out of the total
# ---------------------------------------------------------------------------
def test_stamp_duty_is_computed_directly():
    out = fees.compute("SELL", 8.30, 100)
    # 830 notional: 5.00 commission (floor) + 0.42 stamp + 0.01 transfer.
    assert out["commission"] == pytest.approx(5.00)
    assert out["stamp_duty"] == pytest.approx(0.42)
    assert out["transfer_fee"] == pytest.approx(0.01)
    assert out["total"] == pytest.approx(5.43)
    # The itemised lines must still add up to the total the kernel charged.
    assert (out["commission"] + out["stamp_duty"] + out["transfer_fee"]) == \
        pytest.approx(out["total"])


def test_config_and_instrument_together_is_refused():
    # `instrument or _instrument(cfg)` let a supplied instrument silently drop
    # the config's stamp/transfer keys while `rate_note` still quoted the
    # config's commission — two halves of two schedules. Now it is an error.
    with pytest.raises(ValueError, match="not both"):
        fees.compute("BUY", 10.0, 100, config=fees.schedule(),
                     instrument=Instrument("600000.SH"))


def test_stamp_duty_survives_extra_cost_component(monkeypatch):
    """A fourth sell-side line must not be re-labelled as stamp duty."""
    inst = Instrument("600000.SH")
    original = CostModel.sell_fees

    def sell_fees_with_extra(self, amount, instrument):
        return money(original(self, amount, instrument) + D("1.00"))

    monkeypatch.setattr(CostModel, "sell_fees", sell_fees_with_extra)

    out = fees.compute("SELL", 8.30, 100, instrument=inst)
    # total grew by the injected 1.00 (5.43 -> 6.43), but stamp duty did not:
    assert out["total"] == pytest.approx(6.43)
    assert out["stamp_duty"] == pytest.approx(0.42)
    # Subtraction would have produced 6.43 - 5.00 - 0.01 = 1.42 here.
    assert out["stamp_duty"] != pytest.approx(1.42)
    # And it still equals notional x the instrument's own rate.
    assert out["stamp_duty"] == pytest.approx(float(money(D("830") * inst.stamp_duty)))


# ---------------------------------------------------------------------------
# MAJOR-2 (fix round) · a missing costs key surfaces as a config error
# ---------------------------------------------------------------------------
def test_stop_gate_raises_when_cost_config_missing(desk, monkeypatch):
    """A missing costs key must surface as a config error, not as 'unknown price'.

    ``MissingParameter`` subclasses ``KeyError`` (``settings.py``), so the generic
    ``except (ValueError, KeyError, RuntimeError)`` in ``stop_gate`` used to
    swallow it and return ``unknown=[...]``: every BUY was then refused with a
    message blaming the quote feed, and nobody learned that ``configs/costs.yaml``
    was short a key. B1-b made ``market._cost_rate`` fail fast, which put a
    ``MissingParameter`` onto this path — so ``stop_gate`` has to let it through.

    Actual chain (confirmed against the tree, matches Ray's report):
    ``stop_gate`` -> ``open_stops`` (``stops.py:181``) -> ``market.snapshot``
    -> ``market.instrument`` (``market.py:1247``) -> ``_cost_rate``
    -> ``settings.decimal("costs.*")``. Only ``costs.*`` is redirected, so every
    other ``settings.decimal`` call still runs and the desk stays usable.
    """
    from service.trading import observation
    from tests.trading.test_autostop import _held
    from tests.trading.test_live_fill import _quote

    day = desk.market.latest_bar_date()
    # Build the held position and its quote BEFORE patching: both read bars via
    # market.snapshot, which (through instrument -> _cost_rate) would raise.
    symbol, entry, stop = _held(desk)
    quote = _quote(desk, symbol, age_seconds=1.5, is_realtime=True)

    real_decimal = settings.decimal

    def missing_costs(key):
        if str(key).startswith("costs."):
            raise MissingParameter(key)
        return real_decimal(key)

    monkeypatch.setattr(settings, "decimal", missing_costs)

    # It must raise. It must NOT return {"unknown": [...], ...}.
    with pytest.raises(MissingParameter):
        observation.stop_gate(desk, day, quotes={symbol: quote})


# ---------------------------------------------------------------------------
# Round 2 · a missing costs key must not degrade to "data unavailable"
#
# `MissingParameter` subclasses `KeyError`, so a generic
# `except (ValueError, KeyError, RuntimeError)` turns a broken fee schedule into
# "price unknown" / "no constraints today" — the reader is told the market data
# is missing when the truth is `configs/costs.yaml` is short a key. Each site
# below now re-raises it ahead of the generic clause.
# ---------------------------------------------------------------------------
def _break_costs(monkeypatch):
    """Make only ``costs.*`` raise MissingParameter; every other key still works."""
    real_decimal = settings.decimal

    def missing_costs(key):
        if str(key).startswith("costs."):
            raise MissingParameter(key)
        return real_decimal(key)

    monkeypatch.setattr(settings, "decimal", missing_costs)


def test_tripwire_raises_when_cost_config_missing(desk, monkeypatch):
    """``tripwire._stops`` prices the next-open exit, so it reads the fee schedule.

    Old code: the generic ``except (ValueError, KeyError, RuntimeError)`` in
    ``_stops`` turned ``MissingParameter`` into a ``stop_unmeasured`` finding —
    "止损距离无法计算", which blames the market data. It must raise instead.
    """
    from service.trading import tripwire
    from tests.trading.test_autostop import _held

    day = desk.market.latest_bar_date()
    _held(desk)              # a position with a stop, so open_stops prices it
    _break_costs(monkeypatch)

    # It must raise — NOT return a ["stop_unmeasured", ...] findings list.
    with pytest.raises(MissingParameter):
        tripwire.evaluate(desk, day=day, include_tape=False)


def test_duty_raises_when_cost_config_missing(desk, monkeypatch):
    """``duty.build`` must not report a broken schedule as "no constraints today".

    **Where the raise actually comes from (measured, not assumed):** NOT the new
    clause at ``duty.py:178`` (which wraps ``observe``). The traceback is
    ``duty.build:227 -> stops.open_stops:181 -> market.snapshot:1247 ->
    market.instrument:1229 -> _cost_rate`` — a *second*, unguarded ``open_stops``
    call later in ``build``. So this case pins the end-to-end contract; it does
    **not** exercise the new clause, and it is green on the pre-fix code too
    (reported as a finding — the ``:178`` clause is unreachable for cost errors,
    because ``observe`` is ``observation.snapshot``, which folds
    ``(ValueError, KeyError)`` into an error *constraint* instead of raising).
    """
    from service.trading import duty
    from tests.trading.test_autostop import _held

    day = desk.market.latest_bar_date()
    _held(desk)
    _break_costs(monkeypatch)

    # It must raise — NOT return an empty constraints list.
    with pytest.raises(MissingParameter):
        duty.build(desk, day=day)


def test_context_pack_raises_when_cost_config_missing(desk, monkeypatch, tmp_path):
    """A broken schedule must not produce a zeroed pack.

    **Measured origin:** ``build_context_pack -> book.read_view ->
    view.mark_prices -> market.snapshot -> market.instrument -> _cost_rate`` — an
    *unguarded* valuation of the private preview, reached before ``_observations``
    is called. So the clause in ``context_pack._observations`` is never exercised
    here, and this case is green on the pre-fix code too (reported as a finding:
    that clause is unreachable for cost errors because ``snapshot`` folds them
    into an error constraint).
    """
    from service.trading import context_pack
    from tests.trading.test_autostop import _held

    day = desk.market.latest_bar_date()
    _held(desk)
    (tmp_path / "data").mkdir(exist_ok=True)
    _break_costs(monkeypatch)

    # It must raise — NOT return {"constraints": 0, ...}.
    with pytest.raises(MissingParameter):
        context_pack.build_context_pack(desk, root=tmp_path)


def test_snapshot_raises_when_cost_config_missing(desk, monkeypatch):
    """``observation.snapshot`` must not fold a config error into an error constraint.

    This is the ONLY case that distinguishes the facade change: before it,
    ``snapshot`` wrapped every builder in ``except (ValueError, KeyError)`` and
    turned a missing rate into ``{"id": "error:stop_constraints", "kind":
    "error", ...}`` with ``ok=True`` — the caller read "observation unavailable"
    and went looking at the quote feed. With the re-raise it propagates
    ``MissingParameter`` instead.

    The other three guards cannot see this: ``tripwire`` calls ``open_stops``
    directly (bypassing ``snapshot``), and ``duty`` / ``context_pack`` fail even
    earlier on their own unguarded reads (``duty.build`` /
    ``build_context_pack``'s read-only valuation).

    A held position with a stop is required: without one, ``stop_constraints``
    never prices an exit and therefore never reads the fee schedule at all.
    """
    from service.trading import observation
    from tests.trading.test_autostop import _held

    day = desk.market.latest_bar_date()
    _held(desk)
    _break_costs(monkeypatch)

    # It must raise — NOT return {"ok": True, ..., "kind": "error"}.
    with pytest.raises(MissingParameter):
        observation.snapshot(desk, day)


