"""AKShare 离线校准集：三道闸、整批 quarantine、用途隔离。全程不联网。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from scripts import fetch_akshare_calibration as cal
from service.adapters.catalog import OFFLINE_CALIBRATION_ONLY, SPECS
from service.trading import parity


class FakeStore:
    def __init__(self, daily_ready: bool = True):
        self.daily_ready = daily_ready
        self.written: list = []

    def read(self, dataset, start, end, **kwargs):
        if dataset == "daily":
            return (pd.DataFrame({"trade_date": [start]}) if self.daily_ready
                    else pd.DataFrame())
        return pd.DataFrame()

    def write(self, dataset, day, frame, **kwargs):
        self.written.append((dataset, day, len(frame), kwargs))


def _frame(**overrides):
    row = {"trade_date": "20261109", "symbol": "600000",
           "quote_ts": "10:00:00", "last": 10.5,
           "bid1_volume": 100.0, "ask1_volume": 50.0,
           "schema_version": cal.SCHEMA_VERSION}
    row.update(overrides)
    return pd.DataFrame([row])


def _collect(tmp_path, frame=None, **kwargs):
    store = FakeStore()
    fetcher = lambda symbol, day: frame if frame is not None else _frame(symbol=symbol, day=day)
    return store, cal.collect(
        "20261109", ["600000"], store=store, fetcher=fetcher,
        lock_path=tmp_path / "lock", budget_path=tmp_path / "budget.json",
        now=datetime(2026, 11, 9, 18, 0), **kwargs)


def test_a_valid_batch_is_written_with_the_schema_version(tmp_path):
    store, out = _collect(tmp_path)
    assert out["ok"] is True and out["status"] == "ok" and out["rows"] == 1
    assert store.written[0][0] == "akshare_calibration"
    assert store.written[0][3]["status"] == "ok"
    assert out["calibration_degraded"] is False


@pytest.mark.parametrize("frame,reason", [
    (_frame(last=-1.0), "last_must_be_positive"),
    (_frame(bid1_volume=-5.0), "bid1_volume_must_be_nonnegative"),
    (_frame(quote_ts="12:00:00"), "quote_ts_outside_session"),
    (_frame(schema_version="v0"), "schema_version_mismatch"),
    (_frame(trade_date="20261106"), "trade_date_mismatch"),
])
def test_any_bad_row_quarantines_the_whole_batch(tmp_path, frame, reason):
    store, out = _collect(tmp_path, frame=frame)
    assert out["status"] == "quarantine"
    assert any(reason in r for r in out["reasons"])
    assert store.written == []                      # 不落库
    assert out["calibration_degraded"] is True
    assert out["fallback"] == "stk_mins"
    assert out["promotion_allowed"] is False


def test_missing_columns_are_never_silently_cast(tmp_path):
    ok, reasons = cal.validate_frame(_frame().drop(columns=["bid1_volume"]), "20261109")
    assert ok is False and "missing_columns" in reasons[0]


def test_vendor_failure_is_a_degraded_state_not_a_crash(tmp_path):
    def broken(*_a, **_k):
        raise RuntimeError("rate limited")

    store = FakeStore()
    out = cal.collect("20261109", ["600000"], store=store, fetcher=broken,
                      lock_path=tmp_path / "lock",
                      budget_path=tmp_path / "budget.json",
                      now=datetime(2026, 11, 9, 18, 0))
    assert out["status"] == "degraded" and out["calibration_degraded"] is True
    assert out["promotion_allowed"] is False and store.written == []


def test_three_gates(tmp_path):
    lock, budget = tmp_path / "lock", tmp_path / "budget.json"
    now = datetime(2026, 11, 9, 18, 0)
    assert cal.acquire_lock(lock, now=now) is True
    assert cal.acquire_lock(lock, now=now) is False          # 并发被挡
    assert cal.acquire_lock(lock, now=now + timedelta(hours=3)) is True  # stale 过期
    cal.release_lock(lock)
    assert cal.allow_budget(budget, 100, now=now)[0] is True
    ledger = json.loads(budget.read_text())
    assert ledger["20261109"] == 100
    assert cal.allow_budget(budget, 10_000, now=now)[0] is False
    assert cal.session_ready(FakeStore(daily_ready=False), "20261109") is False
    assert cal.session_ready(FakeStore(), "20261109") is True


def test_a_day_without_published_bars_is_skipped(tmp_path):
    store = FakeStore(daily_ready=False)
    out = cal.collect("20261109", ["600000"], store=store,
                      fetcher=lambda s, d: _frame(symbol=s, day=d),
                      lock_path=tmp_path / "lock",
                      budget_path=tmp_path / "budget.json",
                      now=datetime(2026, 11, 9, 18, 0))
    assert out["status"] == "not_ready" and store.written == []


def test_usage_isolation_is_a_contract_not_a_comment():
    spec = SPECS["akshare_calibration"]
    assert "akshare_calibration" in OFFLINE_CALIBRATION_ONLY
    assert spec.trust_tier == "T1" and spec.on_demand is True
    assert "offline_calibration" in spec.tags and "snapshot" not in spec.tags
    realtime = (Path(__file__).resolve().parents[1]
                / "service" / "trading" / "realtime.py").read_text(encoding="utf-8")
    assert "akshare_calibration" not in realtime
    assert cal.VERIFIED_REAL_FETCHER is False
    with pytest.raises(RuntimeError):
        cal._default_fetcher("600000", "20261109")


def test_degraded_calibration_blocks_promotion_without_loosening_the_gate():
    comparison = parity.compare(
        [{"ticket_id": f"td-{i}", "symbol": "600000.SH", "side": "BUY", "qty": 100,
          "status": "filled", "fill_basis": "close", "price": 10.0,
          "fees": 5.0, "cash_after": 4000.0} for i in range(120)],
        [{"ticket_id": f"td-{i}", "symbol": "600000.SH", "side": "BUY", "qty": 100,
          "status": "filled", "fill_basis": "close", "price": 10.0,
          "fees": 5.0, "cash_after": 4000.0} for i in range(120)])
    out = parity.gate(comparison, trading_days=10,
                      regime_counts={"a": 30, "b": 30, "c": 30},
                      human_activated=True, calibration_degraded=True)
    assert out["status"] == "calibration_degraded"
    assert out["gates"]["intent_parity_g5"]["promotion_allowed"] is False
    assert out["gates"]["intent_parity_g5"]["price_gap_gate_bps"] == 10.0
    assert out["activated"] is False
