"""Tape history uses clean sessions and carries a content receipt."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from service.trading import tape


class FakeData:
    def __init__(self, frames):
        self.frames = frames

    def read(self, name, start=None, end=None, columns=None):
        frame = self.frames.get(name, pd.DataFrame()).copy()
        return frame[columns] if columns and not frame.empty else frame


def _desk():
    frames = {
        "ths_limit_up": pd.DataFrame({"trade_date": [
            "20240101", "20240101", "20240102", "not-a-date", None]}),
        "ths_limit_break": pd.DataFrame({"trade_date": ["20240101"]}),
        "ths_limit_stat": pd.DataFrame({
            "trade_date": ["20240101", "20240101"],
            "seal_rate": [float("inf"), float("nan")],
        }),
    }
    return SimpleNamespace(data=FakeData(frames)), frames


def test_history_cleans_dates_and_non_finite_rates_and_adds_a_digest():
    desk, _ = _desk()
    result = tape.limit_history(desk, "20240102", back=5)

    assert result["ok"] is True
    assert result["window"] == {"start": "20240101", "end": "20240102"}
    assert result["seal_rate"]["derived_days"] == 1
    assert result["seal_rate"]["p50"] == pytest.approx(2 / 3, abs=0.001)
    digest = result["digest"]
    assert digest["rows"] == 6
    assert digest["datasets"] == {
        "ths_limit_up": 3, "ths_limit_break": 1, "ths_limit_stat": 2}
    assert digest["first"] == "20240101"
    assert digest["last"] == "20240102"
    assert len(digest["sha256"]) == 64


def test_history_digest_changes_when_an_old_row_changes():
    desk, frames = _desk()
    before = tape.limit_history(desk, "20240102", back=5)["digest"]["sha256"]
    frames["ths_limit_up"] = pd.concat([
        frames["ths_limit_up"], pd.DataFrame({"trade_date": ["20240101"]})],
        ignore_index=True)
    after = tape.limit_history(desk, "20240102", back=5)["digest"]["sha256"]
    assert after != before


def test_the_vendor_count_says_which_vendor(desk):
    """`counts.vendor` 与 `facts.vendor_eastmoney` 是两个厂商，名字必须分开。

    同一天（2026-10-30）：东财跌停 82、同花顺 86、派生池 93。两个字段都叫
    "vendor" 时，读者只会得出"有一个算错了"，而真相是三家抓取深浅不同。
    """
    out = tape.limit_structure(desk)

    counts = out.get("counts") or {}
    if "vendor_ths" not in counts:
        pytest.skip("派生池没就绪，没有可对照的厂商值")
    assert "vendor" not in counts, "厂商计数必须点名，否则与东财那版撞名"
    assert set(counts["vendor_ths"]) == {"limit_up", "broken", "limit_down"}
