"""The referee must grade an account, not a signal average.

Two failures this file pins down:

* ``portfolio_series`` used to drop flat sessions, so a half-time strategy had
  its CAGR computed over half the elapsed time (35.96% reported, 16.34% real).
* The daily series averaged every open leg regardless of ticket size, so a
  thirty-name book scored as if a CNY 5,000 account could hold it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.research import capital, runner, stats
from service.research.spec import EventStudy, portfolio_series

DAYS = ["20240102", "20240103", "20240104", "20240105", "20240106"]


def daily_rows(pairs: dict[str, list[float]]) -> pd.DataFrame:
    rows = []
    for date, rets in pairs.items():
        for i, ret in enumerate(rets):
            rows.append({"trade_id": i, "date": date, "ret": ret, "leg": 0, "kind": "entry"})
    return pd.DataFrame(rows)


def trades_frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["trade_id"] = range(len(frame))
    frame.attrs["calendar"] = DAYS
    return frame


def trade(symbol: str, entry: str, exit_: str, epx: float, xpx: float) -> dict:
    return {"ts_code": symbol, "entry_date": entry, "exit_date": exit_,
            "entry_price": epx, "exit_price": xpx}


# --------------------------------------------------------------- exposure
def test_flat_sessions_are_included_in_the_series():
    rows = daily_rows({"20240102": [0.01], "20240104": [0.02]})
    series = portfolio_series(rows, calendar=DAYS)
    assert list(series.index) == DAYS
    assert len(series) == 5
    assert series.tolist() == [0.01, 0.0, 0.02, 0.0, 0.0]


def test_metrics_report_exposure_and_shrink_cagr_when_flat_days_count():
    rows = daily_rows({"20240102": [0.01], "20240104": [0.02]})
    short = stats.metrics(portfolio_series(rows, calendar=["20240102", "20240104"]))
    long = stats.metrics(portfolio_series(rows, calendar=DAYS))
    assert long["n"] == 5
    assert long["exposure"] == pytest.approx(0.4)
    # Same trades, honest denominator: annualising two sessions as if they were
    # the whole sample is what produced the 2.2x CAGR.
    assert long["cagr"] < short["cagr"]
    assert long["sharpe"] < short["sharpe"]


# --------------------------------------------------------------- account sim
def test_empty_trades_do_not_invent_a_curve():
    result = capital.simulate(pd.DataFrame(), EventStudy(pool="limit_up"))
    assert result.trades_taken == 0
    assert len(result.daily) == 0
    assert "no trades" in result.note


def test_slots_limit_how_many_names_the_account_can_hold():
    rows = [trade(f"60000{i}.SH", "20240103", "20240105", 10.0, 10.5) for i in range(5)]
    result = capital.simulate(trades_frame(rows), EventStudy(pool="limit_up", hold=1),
                              equity=5000, max_positions=3)
    assert result.trades_taken == 3
    assert result.skip_reasons.get("slots") == 2
    assert result.peak_positions <= 3


def test_a_stock_that_costs_more_than_one_slot_buys_nothing():
    rows = [trade("600001.SH", "20240103", "20240105", 100.0, 110.0)]
    result = capital.simulate(trades_frame(rows), EventStudy(pool="limit_up", hold=1),
                              equity=5000, max_positions=3)
    # 5000 / 3 = 1666 per slot, one lot costs 10000: not a smaller position, none.
    assert result.trades_taken == 0
    assert result.skip_reasons.get("min_lot") == 1


def test_cash_is_never_negative_and_fees_are_charged():
    rows = [trade("600001.SH", "20240103", "20240105", 10.0, 11.0)]
    result = capital.simulate(trades_frame(rows), EventStudy(pool="limit_up", hold=1),
                              equity=5000, max_positions=3)
    assert result.trades_taken == 1
    assert result.cash_min >= 0
    # 100 shares at 10.01 + CNY 5 commission leaves less than 5000 - 1000.
    assert result.cash_min < 4000
    assert result.metrics["total_return"] > 0


def test_account_curve_is_realised_only():
    """A position carried at cost must not mark up before it exits."""
    rows = [trade("600001.SH", "20240103", "20240105", 10.0, 11.0)]
    result = capital.simulate(trades_frame(rows), EventStudy(pool="limit_up", hold=1),
                              equity=5000, max_positions=3)
    curve = result.daily
    entry_day = curve.loc["20240103"]
    # 建仓当天只扣成本（一手 1000 元的佣金 5 元），不记那 10% 的浮盈
    assert entry_day < 0
    assert entry_day == pytest.approx(-5.01 / 5000, abs=2e-4)
    assert curve.loc["20240105"] > 0


def test_close_proceeds_cannot_fund_an_open_entry_that_morning():
    rows = [trade("600001.SH", "20240102", "20240103", 10.0, 20.0),
            trade("600002.SH", "20240103", "20240104", 10.0, 11.0)]
    spec = EventStudy(pool="limit_up", entry="next_open", hold=1, exit_at="close")
    result = capital.simulate(trades_frame(rows), spec, equity=2005, max_positions=2)
    assert result.trades_taken == 1
    assert result.trades_skipped == 1
    assert result.open_positions == 0


def test_open_proceeds_can_fund_an_open_entry():
    rows = [trade("600001.SH", "20240102", "20240103", 10.0, 20.0),
            trade("600002.SH", "20240103", "20240104", 10.0, 11.0)]
    spec = EventStudy(pool="limit_up", entry="next_open", hold=1, exit_at="open")
    result = capital.simulate(trades_frame(rows), spec, equity=2005, max_positions=2)
    assert result.trades_taken == 2
    assert result.open_positions == 0
    assert result.cash_end > 2005


def test_stuck_price_is_an_open_mark_and_never_sale_cash():
    row = {**trade("600001.SH", "20240102", "20240103", 10.0, 20.0), "stuck": True}
    spec = EventStudy(pool="limit_up", entry="next_open", hold=1, exit_at="close")
    result = capital.simulate(trades_frame([row]), spec, equity=2005, max_positions=2)
    assert result.trades_taken == 1
    assert result.open_positions == 1
    # 买入 100 股后只剩约 999 元；20 元的跌停标记不能增加约 2000 元现金。
    assert result.cash_end == pytest.approx(999.0, abs=0.02)
    assert result.daily.iloc[-1] > 0, "允许体现未平仓估值，但这与 realized cash 不同"


def test_t0_bond_quantity_uses_instrument_rules_and_is_marked_estimation():
    rows = [trade("113001.SH", "20240102", "20240102", 100.0, 101.0)]
    # A stale spec.lot=100 must not override the bond instrument's 10-unit step.
    spec = EventStudy(pool="cb", entry="next_open", hold=0, exit_at="close", lot=100)
    result = capital.simulate(trades_frame(rows), spec, equity=2000, max_positions=1)
    assert result.trades_taken == 1
    assert result.open_positions == 0
    assert result.note.startswith("estimation: T+0")


# --------------------------------------------------------------- verdict gate
def _verdict(account):
    best = {"trades": {"n": 200, "mean_excess": 0.01, "p_value": 0.01}, "metrics": {}}
    return runner._verdict(
        best,
        {"dsr": 0.99, "trials": 10},
        {"pbo": 0.10},
        {"oos_sharpe_p05": 0.5, "positive": 0.9},
        {"2x": {"metrics": {"sharpe": 1.0}}},
        {"implementable": True, "median_signals_per_day": 1.0, "median_price": 10.0,
         "capital_one_lot_each": 1000.0, "max_names_affordable": 5},
        power={"underpowered": False},
        account=account,
    )


def test_account_that_loses_money_is_rejected_even_when_the_signal_wins():
    verdict, reasons, _ = _verdict({"trades_taken": 200, "equity_start": 5000,
                                 "max_positions": 3,
                                 "metrics": {"total_return": -0.08}})
    assert verdict == "rejected"
    assert any("account curve loses money" in r for r in reasons)


def test_account_that_makes_money_does_not_block_promotion():
    verdict, _, _ = _verdict({"trades_taken": 200, "equity_start": 5000,
                           "max_positions": 3,
                           "metrics": {"total_return": 0.12}})
    assert verdict == "promoted"


def test_too_few_fillable_trades_is_testing_not_rejected():
    verdict, reasons, _ = _verdict({"trades_taken": 5, "equity_start": 5000,
                                 "max_positions": 3,
                                 "metrics": {"total_return": 0.5}})
    assert verdict in {"testing", "underpowered"}
    assert any("cannot express" in r for r in reasons)
