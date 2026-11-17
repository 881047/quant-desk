"""P2-2：时段条件分布。样本门槛、区间、不给信号。"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from service.trading import intraday_buckets as buckets


def _features(n: int = 40, shape: str = "limit_up_like",
              bucket: str = "0940") -> pd.DataFrame:
    return pd.DataFrame({
        "shape": [shape] * n,
        "bucket_5min": [bucket] * n,
        "forward_to_close_pct": [float(i % 11) - 5.0 for i in range(n)],
        "frame_return_pct": [float(i % 7) - 3.0 for i in range(n)],
    })


def test_each_bucket_reports_n_and_an_interval():
    out = buckets.conditional_distribution(_features(), sentiment_state="主升")
    assert out["ok"] is True
    assert out["bucket_count"] == 1
    row = out["buckets"][0]
    assert row["status"] == "ok" and row["n"] == 40
    assert row["p25"] <= row["median"] <= row["p75"]
    assert len(row["interval"]) == 2 and row["interval"][0] <= row["interval"][1]
    assert row["sentiment_state"] == "主升"


def test_a_bucket_below_the_sample_floor_is_insufficient_not_interpolated():
    out = buckets.conditional_distribution(_features(n=29), sentiment_state="分歧")
    row = out["buckets"][0]
    assert row["status"] == "insufficient" and row["n"] == 29
    assert row["mean"] is None and row["interval"] is None
    assert "不插值" in out["note"]


def test_the_floor_is_configurable_and_the_units_are_stated():
    out = buckets.conditional_distribution(_features(n=8), min_n=5)
    assert out["buckets"][0]["status"] == "ok"
    assert out["min_bucket_n"] == 5
    assert "pct" in out["units"]


def test_shape_bucket_and_sentiment_are_three_separate_group_keys():
    frame = pd.concat([
        _features(n=35, shape="limit_up_like", bucket="0940"),
        _features(n=35, shape="limit_up_like", bucket="1000"),
        _features(n=35, shape="weak", bucket="0940"),
    ])
    out = buckets.conditional_distribution(frame, sentiment_state="分歧")
    assert out["bucket_count"] == 3
    assert {(r["shape"], r["bucket_5min"]) for r in out["buckets"]} == {
        ("limit_up_like", "0940"), ("limit_up_like", "1000"), ("weak", "0940")}
    assert all(r["sentiment_state"] == "分歧" for r in out["buckets"])


def test_the_answer_contains_no_signal_score_or_action():
    out = buckets.conditional_distribution(_features(), sentiment_state="主升")
    blob = json.dumps(out, ensure_ascii=False).lower()
    for word in ('"signal"', '"score"', '"ranking"', '"action"', '"advice"'):
        assert word not in blob
    assert "U 型" in out["note"] and "不是信号" in out["note"]


def test_invalid_value_columns_and_empty_input_are_refused():
    with pytest.raises(ValueError):
        buckets.conditional_distribution(_features(), value_col="close")
    with pytest.raises(ValueError):
        buckets.conditional_distribution(_features().drop(columns=["shape"]))
    assert buckets.conditional_distribution(pd.DataFrame())["status"] == "no_features"
