"""An overlay is the same trades run twice, and the difference is a measurement.

Everything here is about the two places this kind of analysis lies: counting a
trigger that did not change anything as if it had, and reporting only the
portfolio-level difference, which dilutes a rule that fires on 3% of the book.

No test asserts that a rule is *good*. That is not this module's job.
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.research import overlay

DAYS = [f"202401{d:02d}" for d in range(2, 12)]
COST = 0.004


def trades_from(paths: list[list[float]], cost: float = COST) -> pd.DataFrame:
    """Build a trade table plus its daily legs from explicit close paths.

    Written this way so ``gross`` is a fact of the path rather than something the
    test recomputes: if the legs and the recorded return ever disagree,
    ``reconstruction_error`` should catch it, and a test that built both from the
    same formula could not.
    """
    trades, legs = [], []
    for tid, closes in enumerate(paths):
        entry = closes[0]
        n = len(closes)
        legs.append({"trade_id": tid, "date": DAYS[0], "leg": 0,
                     "ret": closes[0] / entry - 1 - cost, "kind": "entry"})
        for k in range(1, n):
            legs.append({"trade_id": tid, "date": DAYS[k], "leg": k,
                         "ret": closes[k] / closes[k - 1] - 1, "kind": "day"})
        gross = closes[-1] / entry - 1
        trades.append({
            "trade_id": tid, "ts_code": f"60000{tid}.SH",
            "entry_date": DAYS[0], "exit_date": DAYS[n - 1],
            "entry_price": entry, "exit_price": closes[-1],
            "cost": cost, "gross": gross, "net": gross - cost,
        })
    frame = pd.DataFrame(trades)
    frame.attrs["daily_rows"] = pd.DataFrame(legs)
    frame.attrs["calendar"] = DAYS
    return frame


FALLING = [[10.0, 9.0, 8.0, 7.5]]          # -25% over three sessions
FLAT = [[10.0, 10.1, 10.0, 10.2]]          # never near a 5% stop
# 五个交易日：移动止损必须在最后一腿之前触发，否则出场日本来就是那天，
# 规则什么也没改变（这一条由 crossed_on_last_leg 单独覆盖）。
RISE_THEN_FALL = [[10.0, 11.0, 12.0, 10.5, 10.4]]


# ------------------------------------------------------------------ the path
def test_the_rebuilt_path_reproduces_the_recorded_gross():
    """If the legs cannot rebuild `gross`, every stop level is measured wrong."""
    trades = trades_from([*FALLING, *FLAT, *RISE_THEN_FALL])
    result = overlay.compare(trades, "stop_loss", threshold=0.05)
    assert result["reconstruction_error"] < 1e-12
    assert result["n_trades"] == 3


def test_missing_legs_is_an_error_not_a_silent_pass():
    frame = pd.DataFrame([{"trade_id": 0, "entry_price": 10.0, "cost": 0.004,
                           "gross": -0.2, "net": -0.204}])
    with pytest.raises(ValueError, match="daily_rows"):
        overlay.compare(frame, "stop_loss", threshold=0.05)


# ------------------------------------------------------------------ stop loss
def test_a_stop_moves_the_exit_to_the_session_it_fired():
    trades = trades_from(FALLING)
    result = overlay.compare(trades, "stop_loss", threshold=0.05)
    treated, detail = overlay.apply_rule(trades, "stop_loss", threshold=0.05)

    assert result["n_fired"] == 1
    assert detail["days_saved"]["median"] == 2
    row = treated.iloc[0]
    assert row["exit_date"] == DAYS[1], "9.0 那天累计 -10%，已破 5%，出场应在这一天"
    assert row["gross"] == pytest.approx(-0.10)
    assert row["exit_price"] == pytest.approx(9.0)


def test_cost_does_not_shrink_when_the_trade_does():
    """Both legs still happen, so the round trip is still paid in full."""
    trades = trades_from(FALLING)
    treated, _ = overlay.apply_rule(trades, "stop_loss", threshold=0.05)
    assert treated.iloc[0]["cost"] == pytest.approx(COST)
    assert treated.iloc[0]["net"] == pytest.approx(-0.10 - COST)


def test_a_level_crossed_on_the_exit_session_is_not_a_fired_trade():
    """The exit was already that day; nothing changed, so nothing is counted."""
    # 10 -> 10.1 -> 9.2: the 5% threshold is crossed on the last session only.
    trades = trades_from([[10.0, 10.1, 9.2]])
    result = overlay.compare(trades, "stop_loss", threshold=0.05)
    assert result["n_fired"] == 0
    assert result["crossed_on_last_leg"] == 1
    assert result["arms"]["with"]["mean"] == pytest.approx(result["arms"]["without"]["mean"])


def test_a_tighter_stop_fires_on_more_trades():
    trades = trades_from([*FALLING, *FLAT, *RISE_THEN_FALL])
    loose = overlay.compare(trades, "stop_loss", threshold=0.15)
    tight = overlay.compare(trades, "stop_loss", threshold=0.05)
    assert tight["n_fired"] >= loose["n_fired"]


def test_a_trailing_stop_fires_where_a_plain_stop_would_not():
    """A trade that is still up 5% has not hit a -5% stop, but it has given back
    8 points from its peak."""
    trades = trades_from(RISE_THEN_FALL)
    plain = overlay.compare(trades, "stop_loss", threshold=0.05)
    trail = overlay.compare(trades, "trailing_stop", threshold=0.08)
    assert plain["n_fired"] == 0
    assert trail["n_fired"] == 1
    treated, _ = overlay.apply_rule(trades, "trailing_stop", threshold=0.08)
    assert treated.iloc[0]["gross"] == pytest.approx(0.05, abs=1e-9)


def test_take_profit_is_the_mirror():
    trades = trades_from(RISE_THEN_FALL)
    treated, _ = overlay.apply_rule(trades, "take_profit", threshold=0.15)
    row = treated.iloc[0]
    assert row["exit_date"] == DAYS[2]
    assert row["gross"] == pytest.approx(0.20)


# --------------------------------------------------------------- measurement
def test_the_paired_difference_covers_the_whole_book():
    """The portfolio effect and the per-affected-trade effect are different numbers."""
    trades = trades_from([*FALLING, *FLAT, *FLAT, *FLAT])
    result = overlay.compare(trades, "stop_loss", threshold=0.05)
    assert result["n_trades"] == 4 and result["n_fired"] == 1
    assert result["paired_diff"]["n"] == 4
    assert result["affected_diff"]["n"] == 1

    affected_gain = result["affected_diff"]["mean"]
    assert result["paired_diff"]["mean"] == pytest.approx(affected_gain / 4, rel=0.02)


def test_untouched_trades_contribute_exactly_zero():
    trades = trades_from([*FALLING, *FLAT])
    treated, _ = overlay.apply_rule(trades, "stop_loss", threshold=0.05)
    untouched = treated[~treated["overlay_fired"]]
    base = trades[~treated["overlay_fired"].to_numpy()]
    assert (untouched["net"].to_numpy() == base["net"].to_numpy()).all()


def test_the_result_carries_no_verdict():
    """Reading the two arms is the caller's job, so nothing here may summarise."""
    trades = trades_from([*FALLING, *FLAT])
    result = overlay.compare(trades, "stop_loss", threshold=0.05)
    for forbidden in ("verdict", "recommendation", "better", "should"):
        assert forbidden not in result
    assert "要不要采用由调用方决定" in " ".join(result["caveats"])


def test_an_entry_rule_is_refused_by_the_paired_primitive():
    trades = trades_from(FLAT)
    with pytest.raises(ValueError, match="入场过滤"):
        overlay.compare(trades, "regime_filter")


# ------------------------------------------------------------------- filters
def test_a_filter_is_reported_as_unpaired_with_its_overlap():
    trades = trades_from([*FALLING, *FALLING, *RISE_THEN_FALL])
    keep = pd.Series([False, False, True])
    result = overlay.compare_filter(trades, keep, label="只做上涨的")
    assert result["n_trades"] == 3 and result["n_kept"] == 1
    assert result["kept_share"] == pytest.approx(1 / 3, abs=1e-3)
    assert result["overlap"]["share_of_with_arm"] == 1.0
    assert "选择效应" in result["overlap"]["note"]
    assert "paired" not in result


def test_reconstruction_mismatch_is_rejected_not_reported_as_a_valid_overlay():
    trades = trades_from(FALLING)
    trades.loc[0, "gross"] += 0.01
    with pytest.raises(ValueError, match="reconstruction_error"):
        overlay.compare(trades, "stop_loss", threshold=0.05)


def test_few_independent_dates_return_insufficient_not_trade_iid():
    trades = trades_from([*FALLING, *FLAT, *FLAT])
    out = overlay.compare(trades, "stop_loss", threshold=0.05)
    paired = out["paired_diff"]
    assert paired["status"] == "insufficient"
    assert paired["ok"] is False
    assert paired["independent_days"] == 1
    assert paired["p_basis"] != "trade-iid"
