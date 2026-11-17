"""P3-2：时机反事实只给分布，不给点估计、不下 verdict。"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from service.trading import intraday_buckets, timing_counterfactual as tc

_FRAMES = [("093000_000001", "09:30:00", 10.0),
           ("094500_000001", "09:45:00", 10.5),
           ("100000_000001", "10:00:00", 11.0)]


def _features(codes: int = 35) -> pd.DataFrame:
    rows = []
    for i in range(codes):
        code = f"{600000 + i}"
        for fid, at, last in _FRAMES:
            rows.append({
                "code": code, "frame_id": fid, "frame_at": at, "last": last,
                "shape": "limit_up_like", "bucket_5min": "0930",
                "forward_to_close_pct": (11.0 / last - 1) * 100,
                "frame_return_pct": 5.0,
            })
    return pd.DataFrame(rows)


def _events():
    return [{"ticket_id": "td-1", "symbol": "600000.SH", "state": "expired_untriggered",
             "shape": "limit_up_like", "bucket_5min": "0930"},
            {"ticket_id": "td-2", "symbol": "600001.SH", "state": "denied",
             "shape": "limit_up_like", "bucket_5min": "0930"},
            {"ticket_id": "td-3", "symbol": "600002.SH", "state": "pending",
             "shape": "limit_up_like", "bucket_5min": "0930"}]


def test_forward_horizon_uses_the_last_real_frame_within_the_window():
    out = tc.with_forward_horizon(_features(codes=1), 30)
    first = out[out["frame_id"] == "093000_000001"].iloc[0]
    assert round(first["forward_30min_pct"], 4) == 10.0   # 10.00 → 11.00
    last = out[out["frame_id"] == "100000_000001"].iloc[0]
    assert pd.isna(last["forward_30min_pct"])          # 没有更晚帧 ≠ 0%


def test_cross_day_prices_are_never_joined_into_an_intraday_return():
    frame = _features(codes=1).iloc[[0]].copy()
    other = frame.copy()
    frame["day"] = "20261112"
    other["day"] = "20261113"
    other["last"] = 20.0
    out = tc.with_forward_horizon(pd.concat([frame, other], ignore_index=True), 30)
    assert out["forward_30min_pct"].isna().all()


def test_lunch_gap_does_not_invent_a_frame():
    frame = _features(codes=1)
    extra = frame.iloc[[0, 2]].copy()
    extra["frame_id"] = ["112900_000001", "130000_000001"]
    extra["frame_at"] = ["11:29:00", "13:00:00"]
    merged = pd.concat([frame, extra], ignore_index=True)
    out = tc.with_forward_horizon(merged, 30).set_index("frame_id")
    assert pd.isna(out.loc["112900_000001", "forward_30min_pct"])
    assert pd.isna(out.loc["130000_000001", "forward_30min_pct"])
    assert out.loc["093000_000001", "forward_30min_pct"] == pytest.approx(10.0)
    with pytest.raises(ValueError):
        tc.with_forward_horizon(frame, 0)


def test_counterfactual_returns_distributions_not_point_estimates():
    out = tc.counterfactual(_events(), _features(), sentiment_state="分歧")
    assert out["ok"] is True and out["n_events"] == 2   # pending 被排除
    assert all(e["state"] in tc.TERMINAL_INTENT_STATES for e in out["events"])
    first = out["events"][0]
    assert first["next_window"]["status"] == "ok" and first["next_window"]["n"] >= 30
    assert first["to_close"]["status"] == "ok"
    assert "interval" in first["to_close"]


def test_a_thin_bucket_is_insufficient_not_interpolated():
    out = tc.counterfactual(_events(), _features(codes=2), min_n=30)
    assert out["events"][0]["next_window"]["status"] == "insufficient"
    assert out["events"][0]["next_window"]["interval"] is None


def test_no_verdict_edge_action_or_counterfactual_profit():
    out = tc.counterfactual(_events(), _features())
    blob = json.dumps(out, ensure_ascii=False).lower()
    for word in ('"verdict"', '"edge"', '"action"', '"advice"', '"should_have"'):
        assert word not in blob
    assert any("点估计" in note for note in out["caveats"])
    assert any("not measurable" in note for note in out["caveats"])


def test_denominator_keeps_untriggered_intents_separate_from_fills():
    out = tc.counterfactual(_events(), _features())
    assert out["denominator"]["untriggered_intents"] == 2
    assert "tca.summary" in out["denominator"]["note"]
    assert "partial-fill" in out["denominator"]["note"]


def test_no_features_or_no_events_are_explicit_states():
    assert tc.counterfactual(_events(), pd.DataFrame())["status"] == "no_features"
    assert tc.counterfactual([], _features())["status"] == "no_terminal_events"


def test_horizon_columns_are_accepted_by_the_shared_bucket_stat():
    assert intraday_buckets.valid_value_col("forward_30min_pct")
    assert intraday_buckets.valid_value_col("forward_to_close_pct")
    assert not intraday_buckets.valid_value_col("close")
