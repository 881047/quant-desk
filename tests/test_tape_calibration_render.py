"""The calibration report must not imply a full window it did not have."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _render_module():
    spec = importlib.util.spec_from_file_location(
        "tape_calibration_script", ROOT / "scripts" / "tape_calibration.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _result(**overrides):
    result = {"ok": True, "window": 120, "min_periods": 120, "full_window": True,
              "effective_window": 120}
    result.update(overrides)
    return result


def test_full_window_header_states_the_window_only():
    module = _render_module()
    text = module.render(_result())
    assert text.splitlines()[0].startswith("窗口 120 日（")
    assert "实际最短历史" not in text


def test_short_history_is_named_in_the_header():
    module = _render_module()
    text = module.render(_result(full_window=False, effective_window=40, min_periods=40))
    header = text.splitlines()[0]
    assert "实际最短历史只有 40 日" in header
    assert "不能与满窗读数直接比" in header


def test_failed_calibration_reports_the_error_only():
    module = _render_module()
    text = module.render({"ok": False, "error": "empty series"})
    assert text == "[校准失败] empty series"
