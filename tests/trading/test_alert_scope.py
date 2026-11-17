"""The alert-log base/scope contract, pinned so nobody "fixes" it into a bug."""

from __future__ import annotations

from pathlib import Path

from service.notify import alert_log
from service.trading import intent

DAY = "20261107"


def test_explicit_base_is_an_exact_directory_not_a_scope_root(tmp_path):
    """``base`` means *this* directory. Changing this breaks test_monitor's redirect.

    It also means scope is ignored when base is given — that is the documented
    contract, not a defect. The test exists to stop a later reader from
    "correcting" it and silently moving every redirected watcher's files.
    """
    shadow = alert_log.alerts_path(DAY, base=tmp_path, scope="shadow")
    paper = alert_log.alerts_path(DAY, base=tmp_path, scope="paper")
    assert shadow == tmp_path / f"{DAY}_alerts.jsonl"
    assert shadow == paper


def test_scope_selects_the_default_directory_only_when_base_is_omitted():
    shadow = alert_log.alerts_path(DAY, scope="shadow")
    paper = alert_log.alerts_path(DAY, scope="paper")
    assert shadow.parent.name == "shadow"
    assert paper.parent.name == "paper"
    assert shadow.parent != paper.parent


def test_scoped_base_isolates_scopes_under_an_explicit_root(tmp_path):
    shadow = alert_log.scoped_base(tmp_path, "shadow")
    paper = alert_log.scoped_base(tmp_path, "paper")
    assert shadow == tmp_path / "shadow"
    assert paper == tmp_path / "paper"
    assert shadow != paper


def test_scoped_base_is_idempotent(tmp_path):
    once = alert_log.scoped_base(tmp_path, "shadow")
    assert alert_log.scoped_base(once, "shadow") == once
    assert "shadow/shadow" not in str(alert_log.scoped_base(once, "shadow"))


def test_scoped_base_none_matches_the_default_directory():
    assert alert_log.scoped_base(None, "shadow") == alert_log.default_base("shadow")


def test_intent_uses_the_public_scoped_base(tmp_path):
    assert intent._shadow_base(tmp_path) == alert_log.scoped_base(tmp_path, "shadow")
    assert intent._shadow_base(None) is None


def test_shadow_trace_never_writes_into_paper_alerts(tmp_path):
    """End-to-end: one terminal intent under an explicit base stays in shadow."""
    rows = intent.run(
        None, day=DAY, base=tmp_path, frame_row={"last": 9.0},
        session_state={"at": "2026-11-07T09:40:00+08:00"},
        tickets=[{"ticket_id": "td-scope", "symbol": "600000.SH", "qty": 100,
                  "contingency": {"kind": "price", "op": "lte", "value": 10.0}}],
    )
    assert rows[0]["status"] == "triggered"
    shadow = tmp_path / "shadow" / f"{DAY}_alerts.jsonl"
    assert shadow.exists()
    assert "intent_terminal" in shadow.read_text(encoding="utf-8")
    # paper writes to the exact base, so a leak would show up here.
    assert not (tmp_path / f"{DAY}_alerts.jsonl").exists()
    paper_dir = tmp_path / "paper"
    assert not paper_dir.exists() or not list(paper_dir.glob("*.jsonl"))
    assert isinstance(Path(shadow).parent, Path)
