"""Only a verdict-critical step may turn the desk red.

2026-11-12: `intraday_features` (0.094 on the information scorecard) failed and
the whole close run was marked `not_ready`, even though the close facts were
READY with `in_cap=26`. A supplementary gap must be visible without vetoing a
day whose facts are complete.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import daily_job


def test_a_supplementary_failure_is_visible_but_does_not_block():
    gate, critical, supplementary = daily_job._gate(
        0, 0, {"derived_pools": 0, "intraday_features": 1, "text": 1})

    assert gate == "ready"
    assert critical == []
    assert supplementary == ["intraday_features", "text"]


def test_the_pool_rebuild_is_verdict_critical():
    gate, critical, supplementary = daily_job._gate(
        0, 0, {"derived_pools": 1, "intraday_features": 1})

    assert gate == "not_ready"
    assert critical == ["derived_pools"]
    assert supplementary == ["intraday_features"]


def test_backfill_or_verify_failure_blocks_with_no_step_at_all():
    assert daily_job._gate(1, 0, {})[0] == "not_ready"
    assert daily_job._gate(0, 1, {})[0] == "not_ready"


def _alert(root: Path) -> Path:
    daily_job.write_alert("20261112", ("20261109", "20261112"), "not_ready",
                          rc_backfill=0, rc_verify=0,
                          failed_steps=["intraday_features", "text"], root=root)
    path = root / "data" / "ALERT.txt"
    assert path.exists()
    return path


def test_a_rebuild_resolves_only_its_own_step(tmp_path):
    (tmp_path / "data").mkdir()
    path = _alert(tmp_path)

    assert daily_job.resolve_alert_step("intraday_features", "20261112",
                                        tmp_path) is True
    text = path.read_text(encoding="utf-8")
    assert "failed steps: text" in text
    assert "intraday_features" not in text


def test_resolving_the_last_step_clears_the_alert(tmp_path):
    (tmp_path / "data").mkdir()
    daily_job.write_alert("20261112", ("20261112", "20261112"), "not_ready",
                          rc_backfill=0, rc_verify=0,
                          failed_steps=["intraday_features"], root=tmp_path)

    assert daily_job.resolve_alert_step("intraday_features", "20261112",
                                        tmp_path) is True
    assert not (tmp_path / "data" / "ALERT.txt").exists()


def test_a_rebuild_for_another_day_does_not_touch_the_alert(tmp_path):
    (tmp_path / "data").mkdir()
    path = _alert(tmp_path)
    before = path.read_text(encoding="utf-8")

    assert daily_job.resolve_alert_step("intraday_features", "20261111",
                                        tmp_path) is False
    assert path.read_text(encoding="utf-8") == before


def test_pipeline_state_names_supplementary_steps_without_flipping_state(tmp_path):
    from datetime import datetime

    from service.adapters.pipeline_state import status, write_marker
    from service.kernel.clock import SHANGHAI

    write_marker(tmp_path, "20261112", "close", {
        "gate": "ready",
        "failed_steps": [],
        "supplementary_failed": ["intraday_features"],
        "window": ["20261109", "20261112"],
    })
    state = status(tmp_path, today="20261112",
                   now=datetime(2026, 11, 12, 18, 30, tzinfo=SHANGHAI))

    assert state["state"] == "done"
    assert state["failed_steps"] == []
    assert state["supplementary_failed"] == ["intraday_features"]
    assert "intraday_features" in state["note"]
