from __future__ import annotations

import pandas as pd
import pytest

from service.research import runner
from service.research.spec import EventStudy, SpecError, parse_spec, rank_metric


def test_rank_metric_prefers_the_name_closest_to_the_limit():
    frame = pd.DataFrame({
        "close": [10.90, 10.50],
        "limit_price": [11.0, 11.0],
        "boards": [0, 0],
        "amount": [100.0, 200.0],
        "pct_chg": [9.0, 5.0],
    })
    closest = rank_metric(frame, "close_to_limit")
    assert closest.iloc[0] == pytest.approx(10.9 / 11.0 - 1)
    assert closest.idxmax() == 0
    assert rank_metric(frame, "amount").idxmax() == 1
    assert rank_metric(frame, "pct_chg").idxmax() == 0
    with pytest.raises(SpecError):
        rank_metric(frame, "market_cap")


def test_direct_event_study_cannot_bypass_t_plus_one_validation():
    with pytest.raises(SpecError, match=r"T\+1"):
        runner.run(None, EventStudy(pool="limit_up", entry="next_open", hold=0))


def test_valid_variant_errors_are_not_silently_skipped(monkeypatch):
    monkeypatch.setattr(runner, "load_inputs", lambda store, base: (None, None, None))
    monkeypatch.setattr(runner, "_grid", lambda base, **kwargs: [base])

    def fail(*args, **kwargs):
        raise SpecError("broken executable variant")

    monkeypatch.setattr(runner, "_run_one", fail)
    with pytest.raises(SpecError, match="broken executable variant"):
        runner.run(object(), EventStudy(pool="limit_up", hold=1))


def test_winners_and_losers_respect_the_base_liquidity_floor(store):
    base = EventStudy(pool="winners", hold=1, min_amount=1e15,
                      start="20240101", end="20240131")
    with pytest.raises(SpecError, match="no pool events"):
        runner.load_inputs(store, base)


def test_capacity_flags_a_book_this_account_cannot_hold():
    trades = pd.DataFrame({
        "entry_date": ["20240103"] * 25,
        "entry_price": [11.4] * 25,
    })
    cap = runner.capacity(trades, 5000)
    assert cap["median_signals_per_day"] == 25
    assert cap["capital_one_lot_each"] == pytest.approx(28500)
    assert cap["max_names_affordable"] == 4
    assert cap["implementable"] is False


def test_capacity_accepts_a_single_name_book():
    trades = pd.DataFrame({"entry_date": ["20240103"], "entry_price": [9.5]})
    cap = runner.capacity(trades, 5000)
    assert cap["capital_one_lot_each"] == pytest.approx(950)
    assert cap["implementable"] is True


def test_capacity_reports_valid_price_and_limit_samples():
    trades = pd.DataFrame({
        "entry_date": ["20240103"] * 3,
        "entry_price": [10.0, float("nan"), float("inf")],
        "limit_data": [True, True, False],
    })
    cap = runner.capacity(trades, 5000)
    assert cap["input_trades"] == 3
    assert cap["valid_trades"] == 1
    assert cap["limit_data_valid"] == 1
    assert cap["limit_data_coverage"] == 1.0
    assert cap["median_price"] == 10.0


def _best(excess: float, p_value: float, n: int = 500) -> dict:
    return {"trades": {"n": n, "mean_excess": excess, "p_value": p_value}}


def _stress(sharpe_2x: float = 1.0) -> dict:
    return {"2x": {"metrics": {"sharpe": sharpe_2x}}, "3x": {"metrics": {"sharpe": 0.2}}}


def _cap(ok: bool = True) -> dict:
    return {"implementable": ok, "median_signals_per_day": 1, "median_price": 10.0,
            "capital_one_lot_each": 1000.0, "max_names_affordable": 4, "account_equity": 5000}


def test_verdict_rejects_negative_excess():
    verdict, reasons, _ = runner._verdict(
        _best(-0.001, 1.0), {"dsr": 0.99, "trials": 5}, {"pbo": 0.0},
        {"oos_sharpe_p05": 1.0, "positive": 1.0}, _stress(), _cap())
    assert verdict == "rejected"
    assert any("excess" in r for r in reasons)


def test_verdict_rejects_when_selection_is_noise():
    verdict, _, _ = runner._verdict(
        _best(0.002, 0.001), {"dsr": 0.99, "trials": 5}, {"pbo": 0.7},
        {"oos_sharpe_p05": 1.0, "positive": 1.0}, _stress(), _cap())
    assert verdict == "rejected"


def test_invalid_pbo_fails_closed_instead_of_becoming_zero():
    invalid = {"ok": False, "pbo": None, "note": "sample too short"}
    verdict, reasons, record = runner._verdict(
        _best(0.003, 0.001), {"dsr": 0.99, "trials": 5}, invalid,
        {"oos_sharpe_p05": 1.0, "positive": 1.0}, _stress(), _cap())
    assert verdict == "testing"
    assert any("PBO unavailable" in reason for reason in reasons)
    assert not record["promotion"]["all_passed"]
    advance, blockers = runner.shadow_gate(
        _best(0.003, 0.001), {"dsr": 0.99, "trials": 5}, invalid,
        {"oos_sharpe_p05": 1.0, "positive": 1.0}, _stress(), _cap())
    assert not advance and "pbo-unavailable" in blockers


def test_verdict_says_testing_when_only_capacity_fails():
    verdict, reasons, _ = runner._verdict(
        _best(0.003, 0.001), {"dsr": 0.99, "trials": 5}, {"pbo": 0.1},
        {"oos_sharpe_p05": 1.0, "positive": 1.0}, _stress(), _cap(ok=False))
    assert verdict == "testing"
    assert any("capacity" in r for r in reasons)


def test_verdict_promotes_only_when_every_gate_passes():
    # desk/v2 tightened dsr_promote to 0.98; this fixture is about "all gates
    # pass => promoted", not about the exact bar, so keep it clearly above it.
    verdict, _, _ = runner._verdict(
        _best(0.003, 0.001), {"dsr": 0.995, "trials": 5}, {"pbo": 0.05},
        {"oos_sharpe_p05": 0.8, "positive": 0.9}, _stress(1.2), _cap())
    assert verdict == "promoted"


def test_verdict_withholds_promotion_when_dsr_is_weak():
    verdict, reasons, _ = runner._verdict(
        _best(0.003, 0.001), {"dsr": 0.40, "trials": 20}, {"pbo": 0.1},
        {"oos_sharpe_p05": 0.8, "positive": 0.9}, _stress(1.2), _cap())
    assert verdict == "testing"
    assert any("deflated" in r for r in reasons)


def test_shadow_gate_advances_an_implementable_edge_even_with_low_dsr():
    ok, blockers = runner.shadow_gate(
        _best(0.003, 0.001), {"dsr": 0.2, "trials": 20}, {"pbo": 0.0},
        {"oos_sharpe_p05": 1.0, "positive": 1.0}, _stress(1.0), _cap())
    assert ok and blockers == []


def test_shadow_gate_blocks_when_costs_or_capacity_fail():
    ok, blockers = runner.shadow_gate(
        _best(0.003, 0.001), {"dsr": 0.2, "trials": 20}, {"pbo": 0.0},
        {"oos_sharpe_p05": 1.0, "positive": 1.0}, _stress(-0.5), _cap(ok=False))
    assert ok is False
    assert "2x-cost" in blockers and "capacity" in blockers


def test_verdict_rejects_when_costs_kill_it():
    verdict, reasons, _ = runner._verdict(
        _best(0.001, 0.01), {"dsr": 0.99, "trials": 5}, {"pbo": 0.1},
        {"oos_sharpe_p05": 1.0, "positive": 1.0}, _stress(-1.0), _cap())
    assert verdict == "rejected"
    assert any("2x costs" in r for r in reasons)


def test_capacity_uses_the_instrument_lot():
    """Convertible bonds trade in 10-unit lots, not 100-share lots."""
    trades = pd.DataFrame({"entry_date": ["20240103"], "entry_price": [163.37]})
    stock = runner.capacity(trades, 5000, lot=100)
    bond = runner.capacity(trades, 5000, lot=10)
    assert stock["capital_one_lot_each"] == pytest.approx(16337)
    assert bond["capital_one_lot_each"] == pytest.approx(1634)
    assert bond["implementable"] is True and stock["implementable"] is False


def test_trade_stats_resample_days_not_trades():
    rows = [{"trade_date": f"d{d:03d}", "net": 0.001, "excess": 0.001 + (d % 3) * 0.0001}
            for d in range(40) for _ in range(20)]
    out = runner._trade_stats(pd.DataFrame(rows))
    assert out["p_basis"].startswith("day-block")
    assert out["days"] == 40
    assert out["n"] == 800


def test_trade_stats_exclude_unresolved_marks_from_realised_sample():
    trades = pd.DataFrame({
        "trade_date": ["20240102", "20240103"],
        "net": [0.01, float("nan")],
        "excess": [0.005, float("nan")],
    })
    out = runner._trade_stats(trades)
    assert out["n"] == 1
    assert out["mean"] == pytest.approx(0.01)
    assert out["mean_excess"] == pytest.approx(0.005)


def test_label_spans_cover_actual_multiday_holding_paths():
    trades = pd.DataFrame({"trade_id": [0]})
    trades.attrs["daily_rows"] = pd.DataFrame({
        "trade_id": [0, 0], "date": ["20240103", "20240104"]})
    spans = runner._label_spans([{"trade_frame": trades}],
                                pd.Index(["20240102", "20240103", "20240104", "20240105"]))
    assert spans.tolist() == [1, 3, 3, 4]


def test_power_sample_removes_overlapping_events_per_symbol():
    trades = pd.DataFrame({
        "trade_id": [0, 1, 2], "ts_code": ["600001.SH"] * 3,
        "entry_date": ["20240103", "20240104", "20240105"],
        "exit_date": ["20240104", "20240105", "20240108"],
    })
    independent = runner._non_overlapping_trades(trades)
    assert independent["trade_id"].tolist() == [0, 2]


def test_underpowered_is_not_the_same_message_as_rejected():
    weak = {"underpowered": True, "days": 40, "mde_bps": 300, "floor_bps": 170,
            "required_days": 900, "required_trades": 4000}
    common = ({"dsr": 0.5, "trials": 8}, {"pbo": 0.2},
              {"oos_sharpe_p05": 0.1, "positive": 0.8}, _stress(), _cap())

    verdict, reasons, _ = runner._verdict(_best(0.001, 0.30), *common, power=weak)
    assert verdict == "underpowered"
    assert any("cannot detect" in r for r in reasons)

    verdict, _, _ = runner._verdict(_best(0.001, 0.30), *common,
                                 power={"underpowered": False})
    assert verdict == "rejected"

    # A negative point estimate is rejected either way: more data would not make
    # it tradeable, and calling it "underpowered" would invite waiting for nothing.
    verdict, _, _ = runner._verdict(_best(-0.001, 0.30), *common, power=weak)
    assert verdict == "rejected"


# ------------------------------------------------------- the audit trail
def test_every_gate_records_what_it_saw():
    """A verdict that cannot be checked is not a verdict, it is an announcement."""
    verdict, _, record = runner._verdict(
        _best(-0.001, 1.0), {"dsr": 0.99, "trials": 5}, {"pbo": 0.0},
        {"oos_sharpe_p05": 1.0, "positive": 1.0}, _stress(), _cap())

    # Read the live policy name: pinning a version string here made a legit
    # threshold tightening look like a regression (2026-11-12, desk/v2).
    assert record["policy"]["name"] == runner.REFEREE_POLICY["name"]
    assert record["gates"]
    for gate in record["gates"]:
        assert set(gate) == {"gate", "value", "op", "threshold", "passed",
                             "fails_as", "detail"}
        if not gate["passed"]:
            assert gate["fails_as"] in {"rejected", "testing", "underpowered"}

    failing = {g["gate"] for g in record["gates"] if not g["passed"]}
    assert failing == {"mean_excess"}, "只有这一条该失败，其余闸门都在记录里"


def test_the_failing_gate_explains_the_verdict():
    verdict, _, record = runner._verdict(
        _best(0.003, 0.001), {"dsr": 0.99, "trials": 5}, {"pbo": 0.1},
        {"oos_sharpe_p05": 1.0, "positive": 1.0}, _stress(), _cap(ok=False))
    assert verdict == "testing"
    caught = [g for g in record["gates"] if not g["passed"]]
    assert [g["gate"] for g in caught] == ["capacity"]
    assert caught[0]["fails_as"] == "testing"


def test_parse_best_roundtrips_a_next_limit_discount_suffix():
    """name() appends @d<pct>; parse_best() must undo it.

    Found 2026-11-09: hypothesis 7 (entry=next_limit, 3% discount) ran for
    ~4 minutes and then died in the 2x-cost stress leg with the nonsensical
    "entry must be one of ['next_close', 'next_limit', ...]" — the value was
    literally `next_limit@d3`. The stress leg re-parses the best spec, so a
    name that cannot be parsed back is a latent failure at the very last step.
    """
    base = parse_spec({"pool": "limit_up", "boards": [1],
                       "entry": "next_limit", "limit_drop_pct": 0.03, "hold": 1})
    for name in ("limit_up:b1:next_limit@d3:h1c",
                 "limit_up:b1:next_limit@d8:h2x",
                 "limit_up:b1:next_open:h1c"):
        recovered = runner.parse_best({"spec": name}, base)
        assert recovered.name() == name
        # The stress leg calls build_trades -> parse_spec on this object.
        assert parse_spec(recovered).entry == recovered.entry

    discounted = runner.parse_best({"spec": "limit_up:b1:next_limit@d8:h1c"}, base)
    assert discounted.entry == "next_limit"
    assert discounted.limit_drop_pct == pytest.approx(0.08)
    assert discounted.limit_drop_pct != base.limit_drop_pct


def test_promotion_gates_are_shown_even_when_the_verdict_is_rejected():
    """The model should see why it *would not* have been promoted, not guess."""
    _, _, record = runner._verdict(
        _best(-0.001, 1.0), {"dsr": 0.4, "trials": 5}, {"pbo": 0.6},
        {"oos_sharpe_p05": -0.2, "positive": 0.3}, _stress(0.4), _cap())
    promotion = record["promotion"]
    assert promotion["all_passed"] is False
    blocked = {g["gate"] for g in promotion["gates"] if not g["passed"]}
    assert {"dsr", "pbo", "cpcv_p05", "cpcv_positive"} <= blocked
