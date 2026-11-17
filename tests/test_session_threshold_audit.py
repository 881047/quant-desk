"""D-12：时段复核只描述、不推断、不推荐、不改配置。"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from scripts import session_threshold_audit as sta


def _frame(day: str, stamps: list[str], pools: list[str]) -> pd.DataFrame:
    rows = []
    for i, (stamp, pool) in enumerate(zip(stamps, pools)):
        rows.append({
            "trade_date": day, "snapshot_ts": f"{day}T{stamp}+08:00",
            "pool": pool, "code": f"60000{i}",
            "bid1_volume": 100.0 + i * 10, "bid_volume_5": 300.0 + i * 10,
            "ask_volume_5": 100.0, "ask1_volume": 50.0,
            "open_times": 1.0 if i % 2 else 0.0,
            "last": 10.0, "pct_chg": 10.0,
        })
    return pd.DataFrame(rows)


def _sample():
    stamps = ["09:25:17", "09:42:57", "10:00:01", "11:29:00", "13:00:10", "14:55:00"]
    pools = ["limit_up", "broken_board", "limit_up", "limit_down",
             "broken_board", "limit_up"]
    return [_frame(day, stamps, pools)
            for day in ("20261104", "20261105", "20261106")]


@pytest.mark.parametrize("text,expected", [
    ("2026-11-04T09:25:17+08:00", "0925"),
    ("2026-11-04T09:42:57+08:00", "0940"),
    ("2026-11-04T10:00:01+08:00", "1000"),
    ("2026-11-04T11:29:00+08:00", "1125"),
    ("2026-11-04T14:59:59+08:00", "1455"),
    ("2026-11-04T16:00:00+08:00", None),
    ("garbage", None),
])
def test_bucket_uses_real_clock_and_stays_inside_the_session(text, expected):
    assert sta.bucket_of(text) == expected


def test_audit_reports_n_and_experiential_intervals_per_bucket():
    out = sta.audit(_sample(), days=["20261104", "20261105", "20261106"],
                    thresholds={"sentiment.break_rate_gate": 0.38})
    assert out["ok"] is True and out["totals"]["rows"] == 18
    assert out["totals"]["frames"] == 18 and out["totals"]["buckets"] == 6
    row = next(r for r in out["buckets"] if r["bucket"] == "0940")
    assert row["n_frames"] == 3 and row["n_rows"] == 3
    assert row["broken_board_rows"] == 3
    assert row["break_rate_proxy"] == 1.0
    assert row["bid1_volume_p25_p75"][0] <= row["bid1_volume_median"]
    assert -1.0 <= row["obi_5_median"] <= 1.0


def test_small_frame_counts_are_descriptive_only():
    out = sta.audit(_sample(), days=["20261104", "20261105", "20261106"])
    assert all(r["status"] == "descriptive_only" for r in out["buckets"])


def test_no_inference_no_recommendation_no_signal():
    out = sta.audit(_sample(), days=["20261104", "20261105", "20261106"])
    assert out["inference_allowed"] is False
    assert out["threshold_recommendations"] == []
    assert "≥60" in out["inference_note"] and "U 型" in out["inference_note"]
    assert "口径不同" in out["break_rate_caveat"]
    blob = json.dumps(out, ensure_ascii=False).lower()
    for word in ('"signal"', '"action"', '"advice"', '"recommendation_value"'):
        assert word not in blob


def test_empty_inputs_are_explicit_states():
    assert sta.audit([])["status"] == "no_data"
    bad = pd.DataFrame({"snapshot_ts": ["2026-11-04T16:00:00+08:00"],
                        "pool": ["limit_up"]})
    assert sta.audit([bad])["status"] == "no_session_rows"


def test_markdown_report_states_it_does_not_change_config(tmp_path):
    out = sta.audit(_sample(), days=["20261104", "20261105", "20261106"],
                    thresholds={"sentiment.break_rate_gate": 0.38})
    md = sta.render_md(out)
    assert "| 桶 | 帧数 n |" in md
    assert "inference_allowed = false" in md
    assert "不推断、不推荐、不改配置" in md
    assert "`sentiment.break_rate_gate` | 0.38" in md
    # D-23: 描述性报告必须指向冻结的预注册，且明确自己不会升级成推断工具。
    assert "session_seasonality_preregistration_20261107.md" in md
    assert "污染的探索期" in md
    assert "不会因样本变多" in md
    target = tmp_path / "audit.md"
    target.write_text(md, encoding="utf-8")
    assert "U 型" in target.read_text(encoding="utf-8")


def test_main_writes_a_report_from_a_synthetic_root(tmp_path):
    root = tmp_path / "data" / "parquet" / "pool_tape"
    root.mkdir(parents=True)
    for day, frame in zip(("20261104", "20261105", "20261106"), _sample()):
        frame.to_parquet(root / f"{day}.parquet", index=False)
    out = tmp_path / "report.md"
    assert sta.main(["--root", str(tmp_path / "data"),
                     "--days", "20261104,20261105,20261106",
                     "--out", str(out)]) == 0
    assert out.is_file() and "逐桶描述" in out.read_text(encoding="utf-8")
