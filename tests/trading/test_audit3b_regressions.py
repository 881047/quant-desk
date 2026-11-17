"""Regressions for the second pass of the third audit (2026-10-31, later).

Split from ``test_audit3_regressions.py`` only because that file reached a size
where the diff stopped being readable. Same rule: one test per defect, and the
docstring names the failure rather than the function.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from service.kernel.models import Bar, D, Instrument, Order, OrderType, Side

DAY = "20260105"


def _match(order: Order, bar: Bar, inst: Instrument):
    from service.kernel.calendar import TradingCalendar
    from service.kernel.execution import BarExecutionModel

    return BarExecutionModel().match(order, bar, inst, bar.ts, TradingCalendar.always())


def _bar(**kwargs) -> Bar:
    base = {
        "symbol": "600000.SH",
        "ts": pd.Timestamp(f"{DAY} 09:30:00").to_pydatetime(),
        "open": D("10.00"), "high": D("10.40"), "low": D("9.80"),
        "close": D("10.00"), "pre_close": D("10.00"),
        "volume": 1_000_000, "amount": D("1e7"),
        "up_limit": D("11.00"), "down_limit": D("9.00"),
        "prior_volume": 10_000_000,
    }
    base.update(kwargs)
    return Bar(**base)


# --------------------------------------------------------------- rule labels
def test_lot_rounding_and_liquidity_are_two_different_reasons():
    """``rule_id`` is an attribution field, not a label.

    Both used to be reported as ``+VOLUME_CAP``, so "rounded 250 down to a legal
    200" read as "the book could only absorb 200" in every TCA review — which
    inflated the share of fills blamed on liquidity.
    """
    inst = Instrument(symbol="600000.SH")

    rounded = Order(order_id="O1", client_order_id="c1", symbol="600000.SH",
                    side=Side.BUY, qty=250, order_type=OrderType.MARKET)
    result = _match(rounded, _bar(), inst)
    assert result.filled and result.fill.qty == 200
    assert result.fill.rule_id.endswith("+LOT_ROUND")

    # 1,000 shares against 5,000 x 10% = 500 of capacity: the cap bit, and the
    # lot was already legal, so this one really is liquidity.
    capped = Order(order_id="O2", client_order_id="c2", symbol="600000.SH",
                   side=Side.BUY, qty=1_000, order_type=OrderType.MARKET)
    result = _match(capped, _bar(prior_volume=5_000), inst)
    assert result.filled and result.fill.qty == 500
    assert result.fill.rule_id.endswith("+VOLUME_CAP")


# --------------------------------------------------------------- data gate
def test_a_missing_calendar_is_not_a_freshness_pass():
    """With no calendar every lag is zero, so a stale dataset read as current."""
    from service.adapters.brief import build_brief
    from service.adapters.pit import TradingCalendarStore

    cal = TradingCalendarStore.from_frame(pd.DataFrame({"cal_date": []}))
    out = build_brief(
        summary=[{"dataset": "daily", "cover_end": "20200102", "rows": 10},
                 {"dataset": "stk_limit", "cover_end": "20200102", "rows": 10}],
        calendar=cal, today="20260105", rt_age_seconds=None, sentiment=None,
    )
    assert out["core"]["usable"] is False
    assert any("trade_cal" in note for note in out["notes"])


# --------------------------------------------------------------- pool compare
def test_a_partially_ingested_peer_is_not_a_zero_delta():
    """One pool fetched, two missing: a delta would be a fabricated gap."""
    from types import SimpleNamespace

    from service.trading.pool_cross import compare

    class _Rec:
        def __init__(self, partition: str) -> None:
            self.partition = partition
            self.snapshot_id = "s"
            self.status = "ok"

    class _Registry:
        def __init__(self, present: set[str]) -> None:
            self.present = present

        def get_manifest(self, name, partition):
            return _Rec(partition) if name in self.present else None

    class _Data:
        def __init__(self, present: set[str]) -> None:
            self.registry = _Registry(present)

        def read(self, name, start=None, end=None):
            n = 49 if name == "ths_limit_up" else 0
            return pd.DataFrame({"x": range(n)})

    desk = SimpleNamespace(data=_Data({"ths_limit_up"}))
    out = compare(desk, "20261030", {"limit_up": 47, "broken": 31, "limit_down": 82},
                  source="eastmoney")
    assert out["ok"] is False
    assert out["skipped"] == "partial"
    assert set(out["missing"]) == {"broken", "limit_down"}


# --------------------------------------------------------------- wording
def test_a_trailing_stop_is_not_compiled_into_a_fixed_stop():
    """``移动止损`` contains ``止损``; dict order decided which rule was tested."""
    from service.research.claim import compile_claim

    claim = compile_claim("移动止损 5% 之后，涨停首板次日开盘买入更好")
    assert claim.spec["rule"] == "trailing_stop"
    fixed = compile_claim("止损 5% 之后，涨停首板次日开盘买入更好")
    assert fixed.spec["rule"] == "stop_loss"


# --------------------------------------------------------------- atomic files
def test_an_atomic_write_never_leaves_a_partial_file(tmp_path):
    from service.adapters.jsonio import write_json_atomic, write_text_atomic

    target = tmp_path / "a" / "b.json"
    write_json_atomic(target, {"k": "值"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"k": "值"}
    assert not list(target.parent.glob(".*.tmp"))

    text = tmp_path / "c.md"
    write_text_atomic(text, "hello")
    assert text.read_text(encoding="utf-8") == "hello"
    assert not list(tmp_path.glob("*.tmp"))


# --------------------------------------------------------------- date params
@pytest.mark.parametrize("raw", ["../../configs/data", "2026-13-45", "2026103", "abc"])
def test_a_bad_date_parameter_is_refused(raw):
    from fastapi import HTTPException

    from service.api.params import day_param

    with pytest.raises(HTTPException) as excinfo:
        day_param(raw, "as_of")
    assert excinfo.value.status_code == 422


def test_a_date_parameter_accepts_the_shapes_a_url_carries():
    from service.api.params import day_param

    assert day_param("2026-10-30") == "20261030"
    assert day_param("20261030") == "20261030"
    assert day_param(None) is None
