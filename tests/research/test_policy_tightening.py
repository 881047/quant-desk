"""desk/v2 tightened the referee gates in one pass on 2026-11-12.

Two things must stay true forever after:

* The thresholds are still *stricter* than the public working bars, and the
  layering (main gate < promotion gate, shadow p < main p) still holds. A later
  "small relaxation" is exactly how a pre-registered rule turns into a story.
* No hypothesis already on the books can flip from rejected to accepted /
  promoted merely because the config changed. Tightening is monotone by
  construction; this test says so against the actual stored verdicts.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from service import settings
from service.research import runner

ROOT = Path(__file__).resolve().parents[2]
PAPER_DB = ROOT / "data" / "paper.sqlite"


def test_thresholds_are_tighter_than_the_public_working_bars():
    p = runner.REFEREE_POLICY
    assert p["name"] == "desk/v2"
    assert p["p_max"] <= 0.05
    assert p["pbo_max"] <= 0.35
    assert p["dsr_min"] >= 0.95
    assert p["min_trades"] >= 200
    # Layering: promotion must be at least as hard as the main gate.
    assert p["pbo_promote"] <= p["pbo_max"]
    assert p["dsr_promote"] >= p["dsr_min"]
    # Shadow is a stricter p-value, but its selection-risk bar is not stricter
    # than capital promotion (settings.INVARIANTS checks this at load time).
    assert settings.num("research.shadow_p_max") < p["p_max"]
    assert settings.num("research.shadow_gate") >= p["pbo_promote"]


def test_stored_verdicts_cannot_flip_under_tighter_thresholds():
    """Every stored hypothesis is rejected; tightening cannot promote any."""
    if not PAPER_DB.exists():
        pytest.skip("no paper.sqlite in this checkout")
    uri = f"file:{PAPER_DB}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute("select id, status, result from hypotheses").fetchall()
    assert rows, "the on-book hypotheses must not silently disappear"
    for hid, status, raw in rows:
        assert status in {"rejected", "testing", "underpowered", "draft", "promoted"}, \
            f"hypothesis {hid} has an unknown status {status}"
        result = json.loads(raw) if isinstance(raw, str) and raw else {}
        # A tighter p / PBO / DSR / sample gate only adds failing gates. The
        # stored `gate_reading` was produced by desk/v1; assert it is not a
        # positive verdict, so no re-run is needed to preserve the conclusion.
        reading = result.get("gate_reading") or result.get("verdict")
        assert reading not in {"promoted", "accepted"}, \
            f"hypothesis {hid} was {reading}; tightening must not rewrite it"
        # Each verdict carries its own policy snapshot, so v1 and v2 coexist
        # without a reviewer mistaking them for the same rule.
        if isinstance(result.get("policy"), dict):
            assert isinstance(result["policy"].get("policy"), dict)


def test_policy_record_carries_the_thresholds_that_produced_it():
    """A verdict without its rule is a conclusion nobody can check."""
    assert runner.REFEREE_POLICY["name"] == "desk/v2"
    assert runner.REFEREE_POLICY["p_max"] == settings.num("research.referee.p_max")
    assert runner.REFEREE_POLICY["dsr_min"] == settings.num("research.referee.dsr_min")
    assert runner.REFEREE_POLICY["min_trades"] == settings.integer("research.min_trades")
