from __future__ import annotations

from pathlib import Path

import pytest

from scripts import cleanup_text_fixture

ROOT = Path(__file__).resolve().parents[1]


def test_cleanup_previews_then_deletes_only_the_fixed_allowlist(tmp_path):
    root = tmp_path / "text"
    root.mkdir()
    allowed = [root / name for name in cleanup_text_fixture.ALLOWED_NAMES]
    for path in allowed:
        path.write_text("fixture", encoding="utf-8")
    keep = root / "real-data.sqlite"
    keep.write_text("keep", encoding="utf-8")
    nested = root / "nested" / "fixture.json"
    nested.parent.mkdir()
    nested.write_text("keep", encoding="utf-8")

    preview = cleanup_text_fixture.cleanup(root, dry_run=True)
    assert [path.name for path in preview] == list(cleanup_text_fixture.ALLOWED_NAMES)
    assert all(path.exists() for path in allowed)

    removed = cleanup_text_fixture.cleanup(root, dry_run=False)
    assert removed == preview
    assert all(not path.exists() for path in allowed)
    assert keep.read_text(encoding="utf-8") == "keep"
    assert nested.read_text(encoding="utf-8") == "keep"


def test_cleanup_cli_is_dry_run_unless_apply_is_explicit(monkeypatch, tmp_path):
    root = tmp_path / "text"
    root.mkdir()
    fixture = root / "fixture.json"
    fixture.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(cleanup_text_fixture, "TEXT_DB", root)

    assert cleanup_text_fixture.main([]) == 0
    assert fixture.exists()
    assert cleanup_text_fixture.main(["--dry-run"]) == 0
    assert fixture.exists()
    assert cleanup_text_fixture.main(["--apply"]) == 0
    assert not fixture.exists()


def test_cleanup_rejects_a_whitelisted_symlink_to_an_outside_file(tmp_path):
    root = tmp_path / "text"
    root.mkdir()
    outside = tmp_path / "real-data.json"
    outside.write_text("keep", encoding="utf-8")
    link = root / "fixture.json"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="refusing non-fixture path"):
        cleanup_text_fixture.cleanup(root, dry_run=False)
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cleanup_missing_root_does_not_create_or_scan_anything(tmp_path):
    root = tmp_path / "missing"
    assert cleanup_text_fixture.cleanup(root, dry_run=False) == []
    assert not root.exists()


@pytest.mark.parametrize("name", [
    "install_service.ps1",
    "install_daily_task.ps1",
    "install_external_tasks.ps1",
])
def test_installers_verify_tasks_and_never_prekill_processes(name):
    text = (ROOT / "scripts" / name).read_text(encoding="utf-8-sig")
    assert "Assert-ProjectTask" in text
    assert "Register-ScheduledTask" in text
    for forbidden in ("Stop-Process", "taskkill", "Get-NetTCPConnection"):
        assert forbidden not in text


def test_external_tasks_use_file_mode_instead_of_a_shell_command():
    text = (ROOT / "scripts" / "install_external_tasks.ps1").read_text(encoding="utf-8-sig")
    assert '-File `"$Wrapper`"' in text
    assert "-Command" not in text


def test_panel_never_uses_an_html_string_sink():
    text = (ROOT / "scripts" / "panel_ui" / "app.js").read_text(encoding="utf-8")
    for forbidden in (".innerHTML", ".insertAdjacentHTML", "document.write", "javascript:"):
        assert forbidden not in text
