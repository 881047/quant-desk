"""P1-3：PIT 回放器。零前视、双时钟分离、取不到价不许假装。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from service.trading import replay
from tests.trading.test_frame import DAY

_NAMES = ["093000_000001.parquet", "100000_000001.parquet",
          "103000_000001.parquet", "104000_000001.parquet"]
_DECISION = "2026-11-05T10:00:00+08:00"


def _write(path: Path, price: float, code: str = "600000",
           source_ts: str = "2026-11-05T09:30:00+08:00") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"code": code, "last": price, "snapshot_ts": source_ts}]).to_parquet(
        path, index=False)


def _root(tmp_path: Path, names=_NAMES, cold=(), price: float = 10.5) -> Path:
    root = tmp_path / "data"
    for _i, name in enumerate(names):
        _write(root / "rt_snapshots" / DAY / name, price)
    for _i, name in enumerate(cold):
        _write(root / "rt_archive" / DAY / name, price - 1)
    return root


def test_visible_frames_drops_every_frame_after_the_decision(tmp_path):
    root = _root(tmp_path)
    view = replay.visible_frames(root, DAY, _DECISION)
    assert [f.frame_id for f in view["frames"]] == ["093000_000001", "100000_000001"]
    assert view["dropped_future"] == ["103000_000001.parquet", "104000_000001.parquet"]
    assert view["total_frames"] == 4
    assert view["cutoff"].endswith(".999999+08:00")


def test_cold_frames_are_visible_to_replay(tmp_path):
    """冷存补上被 SNAPSHOT_KEEP 裁掉的早盘帧。"""
    root = _root(tmp_path, names=_NAMES[2:], cold=_NAMES[:2])
    view = replay.visible_frames(root, DAY, _DECISION)
    assert [f.frame_id for f in view["frames"]] == ["093000_000001", "100000_000001"]
    assert view["frames"][0].path.parent.parent.name == "rt_archive"


def test_quote_at_keeps_source_and_acquisition_clocks_separate(tmp_path):
    root = _root(tmp_path)
    fact = replay.quote_at(root, DAY, _DECISION, "600000.SH")
    assert fact.basis == "frame" and fact.price == 10.5
    assert fact.frame_id == "100000_000001"
    assert fact.source_ts == "2026-11-05T09:30:00+08:00"
    assert fact.acquisition_ts.startswith("2026-11-05T10:00:00")
    assert fact.source_ts != fact.acquisition_ts  # 两个时钟不许合并


def test_zero_price_and_missing_symbol_are_unavailable_not_a_fake_price(tmp_path):
    root = _root(tmp_path)
    zero = replay.quote_at(root, DAY, _DECISION, "600000.SH")
    assert zero.basis == "frame"        # 本帧有价，另测零价帧
    _write(root / "rt_snapshots" / DAY / _NAMES[1], 0.0)
    assert replay.quote_at(root, DAY, _DECISION, "600000.SH").basis == "unavailable"
    assert replay.quote_at(root, DAY, _DECISION, "999999.SH").reason == "symbol_not_in_frame"


def test_a_decision_before_any_frame_is_unavailable(tmp_path):
    root = _root(tmp_path)
    fact = replay.quote_at(root, DAY, "2026-11-05T09:00:00+08:00", "600000.SH")
    assert fact.price is None and fact.basis == "unavailable"
    assert fact.reason == "no_frame_at_or_before_decision_ts"
    assert fact.frame_id is None


def test_backfill_fields_are_read_only_and_only_frame_basis_when_priced(tmp_path):
    root = _root(tmp_path)
    ticket = {"decision_ts": _DECISION, "trade_date": DAY, "symbol": "600000.SH",
              "decision_quote": None, "price_ref": "10.00"}
    fields = replay.backfill_fields(ticket, root)
    assert fields["decision_quote_basis"] == "frame"
    assert fields["decision_quote"] == "10.5"
    assert fields["decision_frame_id"] == "100000_000001"
    # 只读：票据本身一个字都没被改，price_ref 也没被动过。
    assert ticket["decision_quote"] is None and ticket["price_ref"] == "10.00"

    missing = replay.backfill_fields({"trade_date": DAY, "symbol": "600000.SH"}, root)
    assert missing["decision_quote_basis"] == "unavailable"
    assert missing["reason"] == "ticket_missing_decision_keys"


def test_non_periodic_frames_are_judged_by_real_clock(tmp_path):
    names = ["094257_999355.parquet", "100000_000001.parquet",
             "100005_000002.parquet"]
    root = _root(tmp_path, names=names)
    fact = replay.quote_at(root, DAY, "2026-11-05T10:00:02+08:00", "600000.SH")
    assert fact.frame_id == "100000_000001"


def test_cross_check_reports_the_gap_without_overwriting_either_source(tmp_path):
    root = _root(tmp_path)
    fact = replay.quote_at(root, DAY, _DECISION, "600000.SH")
    same = replay.cross_check_shadow(10.5, fact)
    assert same["comparable"] is True and same["gap_bps"] == 0.0
    different = replay.cross_check_shadow(10.6, fact)
    assert different["comparable"] is True and different["gap_bps"] > 0
    assert fact.price == 10.5  # 比对不改回放事实
    assert replay.cross_check_shadow(None, fact)["comparable"] is False


def test_bad_decision_ts_is_refused_not_silently_defaulted(tmp_path):
    with pytest.raises(ValueError):
        replay.visible_frames(_root(tmp_path), DAY, "sometime")
