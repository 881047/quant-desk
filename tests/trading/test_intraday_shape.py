"""Session-shape metrics: reproducible, completeness-aware and signal-free."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from service.trading import intraday, profile


def _session() -> pd.DataFrame:
    morning = pd.date_range("2026-11-13 09:35", "2026-11-13 11:30", freq="5min")
    afternoon = pd.date_range("2026-11-13 13:05", "2026-11-13 15:00", freq="5min")
    stamps = morning.append(afternoon)
    close = [10.0] * 47 + [11.0]
    return pd.DataFrame({
        "ts_code": ["000001.SZ"] * 48,
        "trade_date": ["20261113"] * 48,
        "trade_time": stamps.strftime("%Y-%m-%d %H:%M"),
        "open": [10.0] * 48, "close": close,
        "high": [12.0] * 48, "low": [9.0] * 48,
        "vol": [100.0] * 48,
        "amount": [100_000.0] * 48,
        "source": ["baostock"] * 48,
    })


def test_full_session_shape_uses_real_bar_end_times():
    out = intraday.session_metrics(_session())

    assert out["bars"] == 48 and out["expected_bars"] == 48
    assert out["bar_coverage"] == 1.0 and out["complete_session"] is True
    assert out["afternoon_volume_share"] == pytest.approx(0.5)
    assert out["last_30min_volume_share"] == pytest.approx(6 / 48)
    assert out["afternoon_return_pct"] == pytest.approx(10.0)
    assert out["close_position_in_range"] == pytest.approx(2 / 3)
    assert out["close_vs_session_high_pct"] == pytest.approx((11 / 12 - 1) * 100)


def test_partial_capture_and_zero_volume_stay_explicit():
    partial = _session().head(6)
    out = intraday.session_metrics(partial)
    assert out["complete_session"] is False
    assert out["bar_coverage"] == pytest.approx(6 / 48)
    assert out["afternoon_return_pct"] is None

    zero = intraday.session_metrics(_session().assign(vol=0.0))
    assert zero["afternoon_volume_share"] is None
    assert zero["last_30min_volume_share"] is None


def test_profile_reuses_existing_vwap_and_exposes_the_new_shape(monkeypatch):
    frame = _session()
    monkeypatch.setattr(intraday, "collect", lambda *_args, **_kwargs: (frame, "stored"))

    out = profile._intraday(SimpleNamespace(market=object()), "000001.SZ", "20261113")

    assert out["available"] is True and out["complete_session"] is True
    assert out["vwap"] == 10.0, "the existing amount/(hands*100) VWAP stays authoritative"
    assert out["first_30min_volume_share"] == pytest.approx(6 / 48, abs=0.001)
    assert out["last_30min_volume_share"] == pytest.approx(6 / 48, abs=0.001)
    assert out["afternoon_return_pct"] == 10.0
    assert "通常" not in out["note"], "no unsupported heuristic percentages"
