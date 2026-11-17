"""P1-a: signal-book and account-implementable alpha must be shown separately."""

from __future__ import annotations

from service.research.alpha_reading import alpha_reading, slim_reading

RESULT = {
    "gate_reading": "rejected",
    "reasons": ["2x costs kill it: Sharpe -4.00", "deflated Sharpe 0.000",
                "capacity: needs CNY 52,603"],
    "best": {
        "metrics": {"sharpe": 7.0439, "total_return": 16.55, "hit_rate": 0.688},
        "trades": {"n": 44446, "mean_excess": 0.007547, "p_value": 0.0005},
    },
    "cpcv": {"oos_sharpe_mean": 7.4106},
    "pbo": {"pbo": 0.0},
    "capacity": {"implementable": False, "account_equity": 5000.0,
                 "capital_one_lot_each": 52603.0,
                 "median_signals_per_day": 41.0, "max_names_affordable": 3},
    "stress": {
        "2x": {"sharpe": -3.9974, "hit_rate": 0.1598},
        "3x": {"sharpe": -3.6589},
    },
}


def test_two_readings_are_labelled_differently():
    out = alpha_reading(RESULT)
    assert out["status"] == "measured" and out["read_only"] is True
    signal = out["signal_book"]
    account = out["account_implementable"]
    assert signal["sharpe"] == 7.0439
    assert signal["mean_excess_per_trade"] == 0.007547
    assert "等权组合" in signal["basis"]
    assert account["implementable"] is False
    assert account["capital_one_lot_each_cny"] == 52603.0
    assert account["account_equity_cny"] == 5000.0
    assert account["sharpe_at_2x_cost"] == -3.9974
    assert account["sharpe_at_3x_cost"] == -3.6589
    assert out["cost_sensitivity"]["collapses_between_1x_and_2x"] is True
    assert "rejected" in out["reading"]


def test_old_format_fails_closed_without_fabricating_numbers():
    for empty in (None, {}, {"metrics": {"sharpe": 1.0}, "notes": "old row"}):
        out = alpha_reading(empty)
        assert out["status"] == "not_stored"
        assert "signal_book" not in out
        assert out["read_only"] is True


def test_slim_reading_keeps_numbers_drops_repeated_prose():
    slim = slim_reading(alpha_reading(RESULT))
    assert slim["signal_sharpe"] == 7.0439
    assert slim["sharpe_at_2x_cost"] == -3.9974
    assert slim["collapses_between_1x_and_2x"] is True
    assert "basis" not in slim and "warning" not in slim
    assert slim_reading({"status": "not_stored"}) == {"status": "not_stored"}


def test_no_verdict_or_trade_fields():
    out = alpha_reading(RESULT)
    for forbidden in ("action", "verdict", "signal", "score",
                      "ranking", "target_price", "recommendation"):
        assert forbidden not in out
