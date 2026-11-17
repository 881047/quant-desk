"""P1-2：跨日帧回放与显式 replay 分支。守的是零前视与「历史不等于现在」。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from service.kernel.clock import SHANGHAI
from service.trading import frame as live_frame
from tests.trading.test_frame import DAY, _at, _row

_NAMES = ["093000_000001.parquet", "100000_000001.parquet",
          "103000_000001.parquet", "104000_000001.parquet"]


def _multi_root(tmp_path: Path, names: list[str]) -> Path:
    root = tmp_path / "data"
    archive = root / "rt_snapshots" / DAY
    archive.mkdir(parents=True)
    rows = [_row("600000", 1, 11.0, 10.0, "浦发银行")]
    for name in names:
        pd.DataFrame(rows).to_parquet(archive / name, index=False)
    return root


def test_replay_frames_merges_cold_and_hot_in_real_frame_time_order(tmp_path):
    root = _multi_root(tmp_path, _NAMES[2:])
    cold = root / "rt_archive" / DAY
    cold.mkdir(parents=True)
    rows = [_row("600000", 1, 10.5, 10.0, "浦发银行")]
    for name in _NAMES[:2]:
        pd.DataFrame(rows).to_parquet(cold / name, index=False)

    paths = live_frame.replay_frames(root, DAY)
    assert [p.name for p in paths] == _NAMES
    assert [p.parent.parent.name for p in paths[:2]] == ["rt_archive"] * 2


def test_a_cold_copy_wins_when_the_same_name_exists_in_both(tmp_path):
    """cold 是 copy2 出来的原始字节，是 ground truth。"""
    root = _multi_root(tmp_path, [_NAMES[0]])
    cold = root / "rt_archive" / DAY
    cold.mkdir(parents=True)
    (cold / _NAMES[0]).write_bytes(b"cold original")
    paths = live_frame.replay_frames(root, DAY)
    assert len(paths) == 1
    assert paths[0].parent == cold


@pytest.mark.parametrize("text,expected", [
    ("2026-11-05T10:00:00+08:00", "2026-11-05T10:00:00+08:00"),
    ("10:00:00", "2026-11-05T10:00:00+08:00"),
    ("100000", "2026-11-05T10:00:00+08:00"),
])
def test_parse_as_of_handles_three_clock_formats(text, expected):
    assert live_frame.parse_as_of(text, DAY).isoformat(timespec="seconds") == expected


def test_parse_as_of_refuses_garbage_instead_of_defaulting_to_now():
    assert live_frame.parse_as_of("yesterday", DAY) is None
    assert live_frame.parse_as_of("999999", DAY) is None


def test_replay_neutralizes_now_and_marks_the_answer_historical(desk, tmp_path):
    root = _multi_root(tmp_path, _NAMES)
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 45),
                           as_of="2026-11-05T10:00:00+08:00")
    assert out["ok"] is True
    assert out["historical"] is True
    assert out["frame_id"] == "100000_000001"
    assert out["frame_age_seconds"] is None
    assert out["frame_is_stale"] is False
    assert out["frames_today"] == 4
    assert out["frames_visible"] == 2       # delta 基线只到回放帧（含）
    assert "当前" in out["note"] and "account" in out["note"]


def test_replay_marks_the_account_as_current_for_machines(desk, tmp_path):
    """The note is prose; a downstream attribution script needs a field.

    A replayed frame still carries *today's* book. Nothing in this codebase
    snapshots historical positions, so ``account_is_historical`` must stay
    False until such a store exists — it is a denial, not a placeholder.
    """
    root = _multi_root(tmp_path, _NAMES)
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 45),
                           as_of="10:00:00")
    assert out["historical"] is True
    assert out["account_is_historical"] is False
    assert out["account_basis"] == "current_book_at_read_time"
    assert "account" in out["note"] and "当前" in out["note"]


def test_default_mode_does_not_gain_the_replay_only_fields(desk, tmp_path):
    root = _multi_root(tmp_path, _NAMES)
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 45))
    assert out["historical"] is False
    assert "account_is_historical" not in out
    assert "account_basis" not in out


def test_replay_delta_visible_set_stops_at_the_selected_frame(desk, tmp_path):
    """零前视：回放 10:00 那一帧，可见序列不能含 10:30 / 10:40。"""
    root = _multi_root(tmp_path, _NAMES)
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 45),
                           as_of="10:00:00", lookback=15)
    assert out["frames_visible"] == 2
    since = out["delta"].get("since_frame_id")
    assert since is None or since <= "100000_000001"


def test_non_periodic_frames_are_selected_by_real_clock_not_by_index(desk, tmp_path):
    """人工 force_refresh 会插入非周期帧；按序号推时刻必然选错。"""
    names = ["094257_999355.parquet", "100000_000001.parquet",
             "100005_000002.parquet"]
    root = _multi_root(tmp_path, names)
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 20),
                           as_of="10:00:02")
    assert out["ok"] is True
    assert out["frame_id"] == "100000_000001"
    assert out["frames_visible"] == 2


def test_frame_id_lookup_and_three_explicit_refusals(desk, tmp_path):
    root = _multi_root(tmp_path, _NAMES)
    hit = live_frame.build(desk, day=DAY, root=root, frame_id="093000_000001")
    assert hit["ok"] is True and hit["frame_id"] == "093000_000001"
    assert hit["historical"] is True

    assert live_frame.build(desk, day=DAY, root=root,
                            frame_id="999999_999999")["error"] == "frame_not_found"
    assert live_frame.build(desk, day=DAY, root=root,
                            as_of="garbage")["error"] == "bad_as_of"
    assert live_frame.build(desk, day=DAY, root=root,
                            as_of="09:00:00")["error"] == "no_frame_before_as_of"


def test_default_mode_is_byte_for_byte_the_old_behaviour(desk, tmp_path):
    root = _multi_root(tmp_path, _NAMES)
    out = live_frame.build(desk, day=DAY, root=root, now=_at(10, 45))
    assert out["ok"] is True
    assert out["historical"] is False
    assert out["frame_id"] == "104000_000001"
    assert out["frame_age_seconds"] == 300
    assert out["frames_visible"] == out["frames_today"] == 4
    assert out["as_of"] is None


def test_http_contract_exposes_the_two_replay_parameters():
    from service.api.app import app

    params = app.openapi()["paths"]["/market/frame"]["get"]["parameters"]
    names = {p["name"] for p in params}
    assert {"as_of", "frame_id", "day", "movers", "lookback"} <= names


def test_api_handler_really_forwards_replay_arguments(desk, monkeypatch):
    """参数出现在 schema 不等于真的传到 build——这条盯的是透传。"""
    from service.api import trading as api

    seen: dict = {}
    monkeypatch.setattr(live_frame, "build",
                        lambda *a, **kw: seen.update(kw)
                        or {"ok": True, "day": DAY, "historical": True})
    monkeypatch.setattr(api, "get_desk", lambda: desk)
    out = api.market_frame(day=DAY, movers=5, lookback=15,
                           as_of="10:00:00", frame_id=None)
    assert out["ok"] is True
    assert seen["as_of"] == "10:00:00"
    assert seen["frame_id"] is None


def test_api_replay_passes_the_frame_cutoff_to_both_alert_scopes(monkeypatch):
    from service.api import trading as api
    from service.notify import alert_log

    seen = []

    def fake_build(desk, day, frame_id=None, as_of=None, **kwargs):
        return {"ok": True, "historical": True,
                "as_of_cutoff": "2026-11-13T10:30:00+08:00"}

    def fake_read(day, **kwargs):
        seen.append((day, kwargs.get("scope"), kwargs.get("as_of")))
        return {"available": False}

    monkeypatch.setattr(live_frame, "build", fake_build)
    monkeypatch.setattr(alert_log, "read", fake_read)
    out = api.market_frame(day=DAY, as_of="2026-11-13 10:30:00")
    assert seen == [
        (DAY, "external", "2026-11-13T10:30:00+08:00"),
        (DAY, "paper", "2026-11-13T10:30:00+08:00"),
    ]
    assert out["alerts_are_historical"] is True
    assert out["book_watch_are_historical"] is True
    assert out["pit_scope"]["account"] == "current_book_at_read_time"
