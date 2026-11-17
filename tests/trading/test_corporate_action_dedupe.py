"""One ex-date, one event.

Tushare records the same plan twice when it was announced against two reporting
periods. `000908.SZ` on 2026-03-11 carries `end_date` 20250429 and 20260113, both
10 转 10. Two `CorporateAction` objects for one symbol-day is not a rounding
problem: the ledger prices each action from the tape, so the same 1.46x is handed
out twice and the account ends up with 2.1x shares instead of 1.46x.

These tests pin the collapse, not the vendor's row count.
"""

from datetime import date

import pandas as pd

from service.trading.market import MarketRepository


class _DividendOnly(MarketRepository):
    def __init__(self, frame: pd.DataFrame):
        self._frame = frame

    def _dividend_frame(self):
        return self._frame


def _row(symbol, ex_date, cash, stock, end_date="20250331", proc="实施"):
    return {"ts_code": symbol, "ex_date": ex_date, "end_date": end_date,
            "ann_date": "20260130", "div_proc": proc, "cash_div": cash,
            "stk_div": stock, "stk_bo_rate": None, "stk_co_rate": None}


def test_two_rows_for_the_same_ex_date_become_one_action():
    frame = pd.DataFrame([
        _row("000908.SZ", "20260311", None, 1.0, end_date="20250429"),
        _row("000908.SZ", "20260311", 0.0, 1.0, end_date="20260113"),
    ])
    actions = _DividendOnly(frame).actions("20260311")

    assert len(actions) == 1
    assert actions[0].symbol == "000908.SZ"
    assert float(actions[0].stock_div_per_share) == 1.0, "kept once, not summed to 2.0"


def test_the_largest_leg_wins_when_the_rows_disagree():
    """A partially filled duplicate must not shrink the event."""
    frame = pd.DataFrame([
        _row("600000.SH", "20260610", 0.30, 0.0, end_date="20251231"),
        _row("600000.SH", "20260610", 0.36, 0.0, end_date="20250630"),
    ])
    actions = _DividendOnly(frame).actions("20260610")

    assert len(actions) == 1
    assert float(actions[0].cash_div_per_share) == 0.36


def test_two_stocks_on_the_same_day_stay_separate():
    frame = pd.DataFrame([
        _row("600000.SH", "20260610", 0.36, 0.0),
        _row("600519.SH", "20260610", 28.02, 0.0),
    ])
    actions = _DividendOnly(frame).actions("20260610")

    assert {a.symbol for a in actions} == {"600000.SH", "600519.SH"}
    assert len(actions) == 2


def test_a_proposed_plan_is_not_an_action():
    frame = pd.DataFrame([_row("600000.SH", "20260610", 0.36, 0.0, proc="预案")])
    assert _DividendOnly(frame).actions("20260610") == []


def test_an_empty_table_is_not_an_error():
    assert _DividendOnly(pd.DataFrame()).actions("20260610") == []


def test_the_ex_date_is_parsed_from_the_partition_day():
    frame = pd.DataFrame([_row("600000.SH", "20260610", 0.36, 0.0)])
    action = _DividendOnly(frame).actions("20260610")[0]
    assert action.ex_date == date(2026, 6, 10)
