"""D-23: the pre-registration is frozen. Tests pin the goalposts, not the result."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from service.trading import preregistration as pre

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "session_seasonality_preregistration_20261107.md"
MODULE = ROOT / "service" / "trading" / "preregistration.py"


def test_contract_values_are_frozen():
    c = pre.V1
    assert c.version == "v1"
    assert c.contaminated_days == ("20261104", "20261105", "20261106")
    assert c.first_eligible_day == "20261109"
    assert c.min_confirmatory_sessions == 60
    assert c.holdout_fraction == 0.20
    assert c.min_frames_per_bucket == 20
    assert c.min_bucket_n == 30
    assert c.min_regimes == 3
    assert c.metric == "break_rate_proxy"
    assert c.primary_window == "09:30-09:59"
    assert c.reference_window == "10:30-13:59"
    assert c.max_out_of_sample_decay == 0.50
    assert c.max_confirmatory_tests == 1
    with pytest.raises(FrozenInstanceError):
        c.min_confirmatory_sessions = 10


def test_readiness_gate_requires_every_condition():
    assert pre.readiness(0, 0, 0)["allowed_to_test"] is False
    assert pre.readiness(59, 12, 3)["allowed_to_test"] is False
    assert pre.readiness(60, 11, 3)["allowed_to_test"] is False
    assert pre.readiness(60, 12, 2)["allowed_to_test"] is False
    assert pre.readiness(60, 12, 3)["allowed_to_test"] is True
    gate = pre.readiness(60, 12, 3)
    assert "不构成任何改配置" in gate["note"]


def test_readiness_is_a_gate_not_a_conclusion():
    """Passing the sample gate must never be phrased as research success."""
    gate = pre.readiness(120, 24, 4)
    assert gate["allowed_to_test"] is True
    assert "不是研究结论" in gate["note"]
    assert "success" not in gate
    assert "verdict" not in gate


def test_forbidden_list_covers_the_known_failure_modes():
    text = " ".join(pre.V1.forbidden)
    for phrase in ("自动修改", "U 型", "OBI_5=1.0", "跨表连接", "机械比较",
                   "被污染", "净值曲线", "lockbox"):
        assert phrase in text
    assert any("净值" in item for item in pre.V1.success_is_not)


def test_module_has_no_io_at_all():
    """A frozen contract must not be able to read data or write config."""
    source = MODULE.read_text(encoding="utf-8")
    for word in ("read_parquet", "open(", "requests", "subprocess",
                 "write_text", "save_ticket", "to_parquet"):
        assert word not in source, f"{word} would make the contract impure"


def test_document_matches_the_machine_readable_contract():
    doc = DOC.read_text(encoding="utf-8")
    for phrase in ("v1，冻结", "D-23", "20261104", "20261105", "20261106",
                   "2026-11-09", "60", "20%", "lockbox",
                   pre.V1.primary_window, pre.V1.reference_window,
                   "衰减 ≤ 50%", "preregistration_status.py"):
        assert phrase in doc, f"missing {phrase!r}"
    assert "09:30–09:59" not in doc and "10:30–13:59" not in doc, \
        "窗口格式必须与机器契约一致，用 ASCII 连字符"
    assert "已经被看过" in doc, "the contamination must be admitted explicitly"
    assert "不以净值曲线变好作为成功标准" in doc
    assert "通过 ≠ 改配置授权" in doc


def test_no_config_was_changed_by_the_pre_registration():
    params = (ROOT / "configs" / "params.yaml").read_text(encoding="utf-8")
    for line in ("break_rate_gate: 0.38", "break_rate_gate_vendor: 0.35",
                 "hard_stop_pct: 0.05", "stop_proximity: 0.02",
                 "min_bucket_n: 30"):
        assert line in params, f"{line} was changed"
