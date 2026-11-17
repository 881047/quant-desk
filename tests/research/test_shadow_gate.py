"""The shadow gate's thresholds must be the configured ones, not literals.

``shadow_gate`` used to compare against ``0.05`` and ``0.30`` written into its own
body, while ``research.shadow_gate`` sat in ``params.yaml`` unread and the referee
read ``pbo_promote: 0.30`` — one number with two homes and one number with none.
The tests below read the ceiling from config and probe either side of it, so they
keep testing the gate (not the number) after someone tunes the yaml.
"""

from __future__ import annotations

import pytest

from service import settings
from service.research import runner


def _case(*, p_value: float, pbo: float) -> tuple[dict, dict, dict, dict, dict]:
    best = {
        "trades": {
            "n": runner.MIN_TRADES,
            "mean_excess": 0.004,
            "p_value": p_value,
        }
    }
    pbo_block = {"ok": True, "pbo": pbo}
    cpcv = {"oos_sharpe_p05": 0.2}
    stress = {runner.PRIMARY_STRESS: {"metrics": {"sharpe": 0.4}}}
    cap = {"implementable": True}
    return best, pbo_block, cpcv, stress, cap


def _advance(p_value: float, pbo: float) -> tuple[bool, list[str]]:
    best, pbo_block, cpcv, stress, cap = _case(p_value=p_value, pbo=pbo)
    return runner.shadow_gate(best, {}, pbo_block, cpcv, stress, cap)


def test_the_gate_reads_its_thresholds_from_config():
    assert runner.SHADOW_P_MAX == settings.num("research.shadow_p_max")
    assert runner.SHADOW_PBO_MAX == settings.num("research.shadow_gate")


def test_the_shadow_gate_is_not_stricter_than_capital_promotion():
    """One ordering that must hold: watching a strategy is cheaper than funding it."""
    assert settings.num("research.shadow_gate") >= settings.num(
        "research.referee.pbo_promote")


def test_the_shadow_p_value_ceiling_is_the_configured_one():
    ceiling = runner.SHADOW_P_MAX
    assert _advance(ceiling - 0.001, 0.10)[0] is True
    ok, reasons = _advance(ceiling + 0.001, 0.10)
    assert ok is False
    assert any("p>" in r for r in reasons), reasons


def test_the_shadow_pbo_ceiling_is_the_configured_one():
    ceiling = runner.SHADOW_PBO_MAX
    assert _advance(0.01, ceiling - 0.01)[0] is True
    ok, reasons = _advance(0.01, ceiling + 0.01)
    assert ok is False
    assert any("pbo>" in r for r in reasons), reasons


def test_a_missing_pbo_is_not_treated_as_zero():
    """``pbo-unavailable`` must block: "not measured" is not "no selection risk"."""
    best, _, cpcv, stress, cap = _case(p_value=0.01, pbo=0.0)
    ok, reasons = runner.shadow_gate(best, {}, {"ok": False, "pbo": None}, cpcv,
                                     stress, cap)
    assert ok is False
    assert "pbo-unavailable" in reasons


def test_the_reason_names_the_threshold_that_failed():
    """A verdict the reader can check: the sentence must quote the configured number."""
    ok, reasons = _advance(runner.SHADOW_P_MAX + 0.01, 0.10)
    assert not ok
    assert f"p>{runner.SHADOW_P_MAX:g}" in reasons


@pytest.mark.parametrize("bad", [-0.01, 0.0])
def test_a_non_positive_edge_never_advances(bad):
    best, pbo_block, cpcv, stress, cap = _case(p_value=0.01, pbo=0.01)
    best["trades"]["mean_excess"] = bad
    ok, reasons = runner.shadow_gate(best, {}, pbo_block, cpcv, stress, cap)
    assert ok is False
    assert "excess<=0" in reasons
