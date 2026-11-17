"""The gate must be able to say yes before its "no" means anything.

These tests run the *real* referee (`runner.run`) against panels whose truth is
known. They are deterministic: the world is seeded, so a change in any of these
assertions means the gate changed, not the dice.
"""

from __future__ import annotations

import pytest

from service.research import controls
from service.research.controls import PanelConfig, World, build_world, calibrate, run_world

SMALL = PanelConfig(symbols=120, days=140)


# ------------------------------------------------------------------- worlds
def test_world_has_the_columns_the_research_layer_reads():
    frames = build_world(SMALL, World("null", 0.0, "null"), seed=1)
    daily = frames["daily"]
    for col in ("ts_code", "trade_date", "open", "close", "pre_close", "amount"):
        assert col in daily.columns
    assert daily["trade_date"].nunique() == SMALL.days
    assert daily["ts_code"].nunique() == SMALL.symbols
    # Liquidity floor in scan_pool is CNY 50m; Tushare's amount is thousand CNY.
    assert (daily["amount"] * 1000).min() >= 5e7


def test_null_world_cannot_carry_alpha():
    with pytest.raises(ValueError):
        World("bad", 0.005, "null")
    with pytest.raises(ValueError):
        World("bad", 0.0, "nonsense")


def test_signal_raises_forward_opens_only():
    """The drift must land after the signal, never inside it."""
    frames = build_world(SMALL, World("s", 0.01, "signal"), seed=1)
    daily = frames["daily"]
    first_day = daily.groupby("ts_code")["open"].first()
    assert first_day.between(9.5, 10.5).all(), "day-0 open must stay at price0"


# --------------------------------------------------------------------- gate
def test_pure_noise_is_rejected():
    """FDR at alpha=0. An admission here is a false discovery by construction."""
    row = run_world(SMALL, World("null", 0.0, "null"), seed=1)
    assert row["admitted"] is False
    assert row["verdict"] == "rejected"
    assert row["excess"] < 0, "a zero-edge structure is negative after costs"


def test_edge_below_cost_is_rejected():
    """+0.20%/trade does not clear the 20bps round trip the spec charges."""
    row = run_world(SMALL, World("weak", 0.002, "signal"), seed=1)
    assert row["admitted"] is False


def test_real_edge_is_admitted():
    """The positive control: without this, every 'rejected' in the repo is unreadable.

    The edge is 2.5%/trade, not 2.0%. ``capital.simulate`` used to multiply only
    slippage by ``cost_mult`` while ``spec.effective_cost_bps`` multiplied fees
    *and* slippage, so the "2x cost" gate was really about 1.3x on this account —
    where the 5-yuan minimum commission, not slippage, is the dominant term. With
    the multiplier applied to both, 2.0% no longer survives and 2.5% does. The
    control moved because the gate got stricter, which is the only direction a
    control is allowed to move.
    """
    row = run_world(SMALL, World("strong", 0.025, "signal"), seed=1)
    assert row["admitted"] is True
    assert row["verdict"] == "promoted"
    assert row["excess"] > 0.005
    assert row["p_value"] < 0.01


def test_placebo_is_not_admitted():
    """Same drift, random stock-days: an admission would mean the gate sees the future."""
    row = run_world(SMALL, World("placebo", 0.010, "placebo"), seed=1)
    assert row["admitted"] is False
    assert row["excess"] < 0.001


# ------------------------------------------------------------------- report
def test_calibration_reports_tpr_and_lift():
    worlds = (World("null", 0.0, "null"), World("strong", 0.025, "signal"))
    report = calibrate(SMALL, worlds=worlds, seeds=(1,))

    assert set(report["worlds"]) == {"null", "strong"}
    assert report["worlds"]["null"]["admitted"] == 0
    assert report["worlds"]["strong"]["admitted"] == 1
    pooled = report["pooled"]
    assert pooled["random_tpr"] == pytest.approx(pooled["adoption_rate"])
    assert pooled["lift"] == pytest.approx(pooled["tpr"] - pooled["adoption_rate"])
    assert pooled["carries_information"] is True


def test_default_worlds_cover_both_directions():
    names = {w.name for w in controls.DEFAULT_WORLDS}
    assert {"null", "placebo"} <= names
    assert any(w.alpha >= 0.02 and w.mode == "signal" for w in controls.DEFAULT_WORLDS)
