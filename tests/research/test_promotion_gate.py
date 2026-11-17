"""The money switch has a written, numeric bar; these tests keep it honest.

The thresholds live in configs/params.yaml, not in code. These tests pass an
explicit cfg so they cannot silently drift with a config edit: if someone
loosens a threshold, they have to come back and say why.
"""

from __future__ import annotations

import pytest

from scripts.promotion_gate import evaluate

CFG = {
    "min_memo_n": 20,
    "min_bss": 0.0,
    "min_accepted_hypotheses": 1,
    "atr_multiple": 2.5,
    "max_trade_risk": 0.04,
}


def _facts(**overrides) -> dict:
    facts = {
        "arena": {"n": 20, "min_decisions": 20, "ci_low": 0.002},
        "memo": {"n": 20, "bss": 0.12},
        "hypotheses": {"accepted": 1, "total": 8},
        "account": {"equity": 10000.0, "lot": 100},
        "benchmark": {"symbol": "X.SZ", "last": 10.0, "atr20_pct": 3.0},
        "flags": {"observe": True, "enabled": False},
        "session": {"in_session": True, "label": "连续竞价"},
    }
    facts.update(overrides)
    return facts


def test_all_gates_pass_is_ready_and_a_closed_switch_is_compliant():
    out = evaluate(_facts(), CFG)
    assert out["gate"] == "ready"
    assert out["eligible"] == out["total"] == 5
    assert out["blockers"] == []
    assert out["compliance"] == "ok"
    assert out["decision"] == "保持 trigger.enabled=false"


@pytest.mark.parametrize("patch,blocker", [
    ({"arena": {"n": 19, "min_decisions": 20, "ci_low": 0.002}}, "decisions"),
    ({"arena": {"n": 20, "min_decisions": 20, "ci_low": -0.01}}, "decisions"),
    ({"memo": {"n": 19, "bss": 0.12}}, "calibration"),
    ({"memo": {"n": 20, "bss": -0.01}}, "calibration"),
    ({"hypotheses": {"accepted": 0, "total": 8}}, "hypotheses"),
    ({"account": {"equity": 1000.0, "lot": 100}}, "sizing"),
    ({"flags": {"observe": False, "enabled": False}}, "observe"),
])
def test_each_unmet_gate_blocks(patch, blocker):
    out = evaluate(_facts(**patch), CFG)
    assert out["gate"] == "blocked"
    assert blocker in out["blockers"]


def test_confidence_interval_crossing_zero_blocks_even_with_enough_trades():
    """n>=20 alone is not evidence; the interval must clear zero."""
    out = evaluate(_facts(arena={"n": 25, "min_decisions": 20,
                                 "ci_low": -0.003, "ci_high": 0.01}), CFG)
    assert "decisions" in out["blockers"]


def test_missing_data_fails_closed_instead_of_being_read_as_zero():
    out = evaluate(_facts(arena={"n": None, "min_decisions": 20, "ci_low": None},
                          memo={"n": None, "bss": None},
                          hypotheses={"accepted": None, "total": None},
                          benchmark={"symbol": "X.SZ", "last": None,
                                     "atr20_pct": None}), CFG)
    assert out["gate"] == "blocked"
    assert set(out["blockers"]) == {"decisions", "calibration",
                                    "hypotheses", "sizing"}


def test_enabled_while_blocked_is_a_violation_subject_to_mechanical_demotion():
    out = evaluate(_facts(flags={"observe": True, "enabled": True},
                          arena={"n": 0, "min_decisions": 20, "ci_low": None}), CFG)
    assert out["compliance"] == "violation"
    assert "关回" in out["decision"]


def test_enabled_while_ready_is_compliant():
    out = evaluate(_facts(flags={"observe": True, "enabled": True}), CFG)
    assert out["gate"] == "ready"
    assert out["compliance"] == "ok"


def test_session_timing_is_runtime_not_an_eligibility_gate():
    out = evaluate(_facts(session={"in_session": False, "label": "盘前/盘后"}), CFG)
    assert out["gate"] == "ready"
    assert out["runtime"]["in_session"] is False
    assert "decisions" not in out["blockers"]


def test_sizing_threshold_is_recomputed_from_atr_budget_and_lot():
    """2.5 * 6.6% * 14.61 * 100 / 4% = 6026.62 CNY, not a hardcoded number."""
    facts = _facts(account={"equity": 6026.61, "lot": 100},
                   benchmark={"symbol": "002185.SZ", "last": 14.61,
                              "atr20_pct": 6.6})
    out = evaluate(facts, CFG)
    sizing = next(c for c in out["checks"] if c["name"] == "sizing")
    assert "6026.62" in sizing["detail"]
    assert "sizing" in out["blockers"]

    facts["account"]["equity"] = 6026.63
    assert "sizing" not in evaluate(facts, CFG)["blockers"]


def test_thresholds_are_explicit_config_not_code_literals():
    """A stricter config must change the verdict; nothing is baked into evaluate."""
    stricter = {**CFG, "min_memo_n": 21}
    assert "calibration" in evaluate(_facts(), stricter)["blockers"]
    looser = {**CFG, "min_memo_n": 7, "min_accepted_hypotheses": 0}
    assert evaluate(_facts(memo={"n": 7, "bss": 0.01},
                           hypotheses={"accepted": 0, "total": 7}),
                    looser)["gate"] == "ready"
