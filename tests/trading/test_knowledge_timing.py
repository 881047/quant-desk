"""The conditional-distribution topic is a measurement, never a signal."""

from __future__ import annotations

import pandas as pd

from service.trading import knowledge
from service.trading.intraday_buckets import MIN_BUCKET_N


def _desk(frame):
    class Store:
        def read(self, name):
            assert name == "intraday_features"
            return frame

    class Market:
        store = Store()

    class Desk:
        market = Market()

    return Desk()


def _frame(n: int) -> pd.DataFrame:
    return pd.DataFrame({
        "code": ["002185"] * n,
        "day": ["20261109"] * n,
        "shape": ["up"] * n,
        "bucket_5min": ["09:35"] * n,
        "forward_to_close_pct": [float(i % 7) / 10 for i in range(n)],
    })


def test_thin_bucket_is_insufficient_and_has_no_statistics():
    out = knowledge.timing_distribution(_desk(_frame(MIN_BUCKET_N - 1)), "002185.SZ")
    assert out["ok"] is True and out["read_only"] is True
    assert out["n_frames"] == MIN_BUCKET_N - 1
    assert out["buckets"][0]["status"] == "insufficient"
    assert out["buckets"][0]["mean"] is None
    assert out["buckets"][0]["interval"] is None


def test_full_bucket_reports_a_distribution_without_an_action():
    out = knowledge.timing_distribution(_desk(_frame(MIN_BUCKET_N + 5)), "002185.SZ")
    bucket = out["buckets"][0]
    assert bucket["status"] == "ok"
    assert bucket["n"] == MIN_BUCKET_N + 5
    assert bucket["p25"] is not None and bucket["interval"] is not None
    for forbidden in ("action", "verdict", "signal", "target_price"):
        assert forbidden not in out and forbidden not in bucket
    assert out["provenance"]["datasets"] == ["intraday_features"]


def test_missing_symbol_is_explained_not_reported_as_no_capability():
    out = knowledge.answer(_desk(_frame(5)), "timing_distribution")
    assert out["need"] == "symbol"
    assert "不是没有这个能力" in out["error"]


def test_missing_partition_degrades_to_a_readable_answer():
    class Broken:
        market = type("M", (), {"store": None})()

    out = knowledge.timing_distribution(Broken(), "002185.SZ")
    assert out["ok"] is False and out["status"] == "no_features"
    assert out["buckets"] == []


def test_as_of_filters_frames_before_bucket_statistics_and_route_forwards_it():
    frame = pd.concat([
        _frame(2),
        _frame(3).assign(day="20261110"),
        _frame(100).assign(day="20261111", forward_to_close_pct=9.9),
    ], ignore_index=True)
    desk = _desk(frame)
    out = knowledge.answer(desk, "timing_distribution", symbol="002185.SZ",
                           as_of="20261110")
    assert out["ok"] is True
    assert out["days"] == ["20261109", "20261110"]
    assert out["n_frames"] == 5
    assert out["data_as_of"] == "20261110"
    assert out["pit_safe"] is True
    assert out["as_of_basis"] == "end_of_day"
    assert frame.loc[frame["day"] <= out["data_as_of"], "day"].max() == "20261110"
    assert out["buckets"][0]["n"] != 105


def test_as_of_before_all_history_does_not_substitute_future_frames():
    frame = _frame(5).assign(day="20261111")
    out = knowledge.timing_distribution(_desk(frame), "002185.SZ", as_of="2026-11-10")
    assert out["ok"] is False
    assert out["status"] == "no_frames_before_as_of"
    assert out["n_frames"] == 0
