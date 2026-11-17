"""The 2026-11-12 decisions on the four deferred items.

Each test pins one decision so a later "small cleanup" cannot quietly reverse
it. In particular: attribution must default ON for the real promotion CLI,
t_close must stay OUT of the default grid, and impact must never enter the
charged cost.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from service.research import factors, runner
from service.research.spec import parse_spec


def test_default_grid_still_excludes_t_close():
    base = parse_spec('{"pool":"limit_up","entry":"next_open","hold":1,"boards":[1]}')
    default = {str(v.entry) for v in runner._grid(base)}
    assert "t_close" not in default
    widened = {str(v.entry) for v in runner._grid(base, include_t_close=True)}
    assert "t_close" in widened
    assert len(widened) > len(default), "widening must add real variants"


def test_widened_grid_is_recorded_as_a_different_family(store):
    """A verdict from a wider family must say so, not pretend to be comparable.

    Uses the real research store fixture, so it checks the payload the desk
    actually writes rather than grepping the source.
    """
    spec = {"pool": "limit_up", "boards": [1], "hold": 1}
    normal = runner.run(store, spec, record=False)
    wider = runner.run(store, spec, record=False, include_t_close=True)
    assert normal["grid_scope"]["includes_t_close"] is False
    assert "t_close" not in normal["grid_scope"]["entries"]
    assert wider["grid_scope"]["includes_t_close"] is True
    assert "t_close" in wider["grid_scope"]["entries"]
    assert "不可直接比较" in wider["grid_scope"]["note"]
    # The DSR denominator really did change — that is WHY it is not comparable.
    assert wider["trials_effective"] != normal["trials_effective"]


def test_cli_defaults_attribution_on_and_t_close_off():
    """Read the CLI source: the real promotion path must attach attribution."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    cli = (root / "scripts" / "run_research.py").read_text(encoding="utf-8")
    assert "--no-attribution" in cli
    assert "args.attribution = not args.no_attribution" in cli
    assert "include_t_close=args.include_t_close" in cli
    assert '"--include-t-close"' in cli


def test_time_holdout_flags_a_sign_flip():
    """A factor that reverses direction in the second half is not stable."""
    ic = pd.Series(np.r_[np.full(40, 0.02), np.full(40, -0.02)]
                   + np.random.default_rng(3).normal(0, 0.001, 80))
    out = factors._time_holdout(ic)
    assert out["status"] == "measured"
    assert out["n_first"] == 40 and out["n_second"] == 40
    assert out["sign_agrees"] is False
    assert out["first_half"] > 0 > out["second_half"]


def test_time_holdout_fails_closed_on_short_series():
    out = factors._time_holdout(pd.Series([0.01] * 10))
    assert out["status"] == "insufficient"
    assert out["first_half"] is None and out["sign_agrees"] is None


def test_factor_summary_carries_time_holdout():
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "research"))
    from test_factors import _panel, _store
    out = factors.panel(_store(_panel()), start="20260001", end="20260900")
    assert out["ok"] is True
    for row in out["factors"]:
        if row["status"] == "measured":
            assert "time_holdout" in row


def test_impact_is_an_uncharged_premium_not_a_new_cost_basis():
    """The matcher's charged cost must not change when impact is estimated."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    source = (root / "service" / "trading" / "analog.py").read_text(encoding="utf-8")
    assert '"charged": False' in source
    assert "by_horizon_if_impact_charged" in source
    assert '"spread_bps": None' in source
    # net must still be computed from `charged`, never from impact.
    assert "net[label] = _json_float(excess - cost)" in source


def test_gate_makes_the_sample_ledger_gap_visible():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    source = (root / "scripts" / "promotion_gate.py").read_text(encoding="utf-8")
    assert "sample_cases" in source
    assert "物理隔离" in source
    assert "闭环" in source
