"""A sell at the lower limit did not happen; the trade kept falling.

``build_trades`` used to price every exit at the planned session's close, even
when that close was at the down limit and nobody was bidding. 3.2% of 涨停次日
exits land on a limit-down close and those trades average -8.7% gross, so this is
not a rounding detail — it was the last optimistic assumption in the trade table.

The rule now: if the planned sell cannot clear, the position rolls to the first
tradable session. If no fill is available it stays open with a separate mark.
Every holding day must appear in the daily series, otherwise the equity curve
and the trade table disagree about the same trade.
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.research.spec import EventStudy, build_trades, load_panel, market_returns
from tests.research.conftest import FakeStore

DAYS = ["20240102", "20240103", "20240104", "20240105", "20240108"]

# (open, close) per session. D1 is the entry open, D2 the planned exit.
PRICES = {
    "600001.SH": [(10.0, 10.0), (10.0, 10.5), (10.5, 10.8), (10.8, 10.9), (10.9, 11.0)],
    "600002.SH": [(10.0, 10.0), (10.0, 10.5), (10.5, 9.45), (9.45, 9.80), (9.80, 9.90)],
    "600003.SH": [(10.0, 10.0), (10.0, 10.5), (10.5, 9.45), (9.45, 8.50), (8.50, 9.00)],
}


def make_store() -> FakeStore:
    rows, limits = [], []
    for code, path in PRICES.items():
        prev = path[0][1]
        for day, (open_px, close_px) in zip(DAYS, path):
            rows.append({"ts_code": code, "trade_date": day, "open": open_px,
                         "close": close_px, "pre_close": prev, "amount": 100000})
            limits.append({"ts_code": code, "trade_date": day,
                           "up_limit": round(prev * 1.1, 2),
                           "down_limit": round(prev * 0.9, 2)})
            prev = close_px
    pools = pd.DataFrame([
        {"ts_code": code, "trade_date": "20240102", "pool": "limit_up", "boards": 1,
         "name": "甲股份", "amount": 100000}
        for code in PRICES
    ])
    return FakeStore(pd.DataFrame(rows), pd.DataFrame(limits), pools)


def run(store) -> pd.DataFrame:
    panel = load_panel(store, "20240102", "20240108")
    pools = store.read("limit_pool_t0", "20240102", "20240108")
    spec = EventStudy(pool="limit_up", boards=(1,), entry="next_open", hold=1)
    return build_trades(panel, pools, spec, market_returns(panel))


def test_a_blocked_exit_rolls_one_session_forward():
    trades = run(make_store()).set_index("ts_code")

    healthy = trades.loc["600001.SH"]
    assert not healthy["rolled"]
    assert healthy["exit_date"] == "20240104"
    assert healthy["gross"] == pytest.approx(0.08, abs=1e-9)

    rolled = trades.loc["600002.SH"]
    assert rolled["rolled"]
    assert rolled["exit_date"] == "20240105", "跌停卖不掉，顺延到下一交易日"
    # 9.80 / 10.00 - 1：两根 K 线的合并收益，不是跌停价上的假成交
    assert rolled["gross"] == pytest.approx(-0.02, abs=1e-9)


def test_two_blocked_sessions_roll_to_the_first_tradable_day():
    trades = run(make_store()).set_index("ts_code")
    trade = trades.loc["600003.SH"]
    assert trade["rolled"] and not trade["stuck"]
    assert not trade["open_position"]
    assert trade["exit_date"] == "20240108"
    assert trade["exit_price"] == pytest.approx(9.00)
    assert trade["gross"] == pytest.approx(-0.10)


def test_the_gate_reports_what_it_did():
    trades = run(make_store())
    gate = trades.attrs["sell_gate"]
    assert gate["blocked"] == 2
    assert gate["rolled"] == 2
    assert gate["stuck"] == 0


def test_unresolved_limit_downs_are_marks_not_realised_exits():
    store = make_store()
    daily = store.data["daily"]
    hit = (daily["ts_code"] == "600003.SH") & (daily["trade_date"] == "20240108")
    daily.loc[hit, ["open", "close"]] = 7.65
    limits = store.data["stk_limit"]
    hit = (limits["ts_code"] == "600003.SH") & (limits["trade_date"] == "20240108")
    limits.loc[hit, "down_limit"] = 7.65

    trade = run(store).set_index("ts_code").loc["600003.SH"]
    assert trade["stuck"] and trade["open_position"]
    assert trade["exit_date"] == "" and pd.isna(trade["exit_price"])
    assert pd.isna(trade["gross"]) and pd.isna(trade["net"])
    assert trade["mark_date"] == "20240108"
    assert trade["mark_price"] == pytest.approx(7.65)
    assert trade["mark_gross"] == pytest.approx(-0.235)


def test_stuck_mark_and_benchmark_cover_the_same_full_holding_path():
    store = make_store()
    daily = store.data["daily"]
    hit = (daily["ts_code"] == "600003.SH") & (daily["trade_date"] == "20240108")
    daily.loc[hit, ["open", "close"]] = 7.65
    limits = store.data["stk_limit"]
    hit = (limits["ts_code"] == "600003.SH") & (limits["trade_date"] == "20240108")
    limits.loc[hit, "down_limit"] = 7.65

    trades = run(store)
    trade = trades.set_index("ts_code").loc["600003.SH"]
    legs = trades.attrs["daily_rows"]
    legs = legs[legs["trade_id"] == trade["trade_id"]].sort_values("leg")
    assert list(legs["date"]) == DAYS[1:]
    assert legs.iloc[-1]["kind"] == "day", "未成交的 open exit 当天也必须持有到收盘估值"
    entry = float(legs.iloc[0]["ret"]) + float(trade["cost"])
    compounded = (1 + entry) * float((1 + legs.iloc[1:]["ret"]).prod()) - 1
    assert compounded == pytest.approx(float(trade["mark_gross"]), abs=1e-9)
    assert trade["mark_excess"] == pytest.approx(
        (1 + trade["mark_net"]) / (1 + trade["benchmark"]) - 1)


def test_the_extra_holding_day_is_in_the_daily_series():
    trades = run(make_store())
    rows = trades.attrs["daily_rows"]
    rolled = rows[rows["trade_id"] == int(
        trades.set_index("ts_code").loc["600002.SH", "trade_id"])]
    assert "20240104" in set(rolled["date"].astype(str)), "顺延那天也要有收益行"


def test_the_legs_compound_to_the_trade_gross():
    """A trade and its daily legs must describe the same money.

    The entry leg carries the cost as a subtraction, so it is added back before
    comparing: with that one adjustment the legs must compound to ``gross``
    exactly, including the extra day a rolled exit spent holding.
    """
    trades = run(make_store())
    rows = trades.attrs["daily_rows"]
    for _, trade in trades.iterrows():
        legs = rows[rows["trade_id"] == trade["trade_id"]].sort_values("leg")
        entry = float(legs.iloc[0]["ret"]) + float(trade["cost"])
        rest = legs.iloc[1:]
        compounded = (1 + entry) * float((1 + rest["ret"]).prod()) - 1
        assert compounded == pytest.approx(float(trade["gross"]), abs=1e-9)


def test_suspended_holding_day_is_forward_filled_for_position_and_benchmark():
    store = make_store()
    daily = store.data["daily"]
    store.data["daily"] = daily[~((daily["ts_code"] == "600001.SH")
                                  & (daily["trade_date"] == "20240104"))]
    trades = run(store)
    trade = trades.set_index("ts_code").loc["600001.SH"]
    legs = trades.attrs["daily_rows"]
    legs = legs[legs["trade_id"] == trade["trade_id"]].sort_values("leg")
    assert list(legs["date"]) == ["20240103", "20240104", "20240105"]
    assert legs.loc[legs["date"] == "20240104", "ret"].iloc[0] == 0.0
    assert trade["exit_date"] == "20240105"
    assert trade["gross"] == pytest.approx(0.09)
    market = market_returns(load_panel(store, "20240102", "20240108"))
    benchmark_legs = []
    for _, leg in legs.iterrows():
        if leg["kind"] == "entry":
            benchmark_legs.append(market.at[leg["date"], "intraday"])
        else:
            benchmark_legs.append((1 + market.at[leg["date"], "overnight"])
                                  * (1 + market.at[leg["date"], "intraday"]) - 1)
    assert trade["benchmark"] == pytest.approx((1 + pd.Series(benchmark_legs)).prod() - 1)


def test_a_healthy_exit_is_left_alone():
    trades = run(make_store())
    assert trades["rolled"].sum() == 2
    assert len(trades) == 3


def test_an_open_exit_rolls_to_the_next_open():
    """开盘即跌停同样卖不出；顺延后拿到的是次日开盘价，不是当日开盘价。"""
    rows, limits = [], []
    path = [(10.0, 10.0), (10.0, 10.5), (9.45, 9.60), (9.70, 9.80), (9.80, 9.90)]
    prev = path[0][1]
    for day, (open_px, close_px) in zip(DAYS, path):
        rows.append({"ts_code": "600002.SH", "trade_date": day, "open": open_px,
                     "close": close_px, "pre_close": prev, "amount": 100000})
        limits.append({"ts_code": "600002.SH", "trade_date": day,
                       "up_limit": round(prev * 1.1, 2),
                       "down_limit": round(prev * 0.9, 2)})
        prev = close_px
    pools = pd.DataFrame([{"ts_code": "600002.SH", "trade_date": "20240102",
                           "pool": "limit_up", "boards": 1, "name": "乙股份",
                           "amount": 100000}])
    store = FakeStore(pd.DataFrame(rows), pd.DataFrame(limits), pools)
    panel = load_panel(store, "20240102", "20240108")
    spec = EventStudy(pool="limit_up", boards=(1,), entry="next_open", hold=1,
                      exit_at="open")
    trades = build_trades(panel, store.read("limit_pool_t0", "20240102", "20240108"),
                          spec, market_returns(panel))

    trade = trades.iloc[0]
    assert trade["rolled"]
    assert trade["exit_date"] == "20240105"
    # 9.70 / 10.00 - 1，而不是跌停的 9.45
    assert trade["gross"] == pytest.approx(-0.03, abs=1e-9)


def test_exit_walks_beyond_the_prefixed_forward_columns():
    days = ["20240102", "20240103", "20240104", "20240105",
            "20240108", "20240109", "20240110"]
    path = [(10.0, 10.0), (10.0, 10.5), (10.5, 9.45), (9.45, 8.50),
            (8.50, 7.65), (7.65, 6.89), (6.89, 7.00)]
    rows, limits = [], []
    previous = path[0][1]
    for day, (open_px, close_px) in zip(days, path, strict=True):
        rows.append({"ts_code": "600003.SH", "trade_date": day, "open": open_px,
                     "close": close_px, "pre_close": previous, "amount": 100000})
        limits.append({"ts_code": "600003.SH", "trade_date": day,
                       "up_limit": round(previous * 1.1, 2),
                       "down_limit": round(previous * 0.9, 2)})
        previous = close_px
    pools = pd.DataFrame([{"ts_code": "600003.SH", "trade_date": days[0],
                           "pool": "limit_up", "boards": 1, "name": "丙股份",
                           "amount": 100000}])
    store = FakeStore(pd.DataFrame(rows), pd.DataFrame(limits), pools)
    panel = load_panel(store, days[0], days[-1])
    trades = build_trades(panel, pools, EventStudy(pool="limit_up", hold=1),
                          market_returns(panel))
    trade = trades.iloc[0]
    assert trade["exit_date"] == "20240110", "f5/f6 也要继续找真实可卖日"
    assert not trade["open_position"] and not trade["stuck"]
    assert trade["gross"] == pytest.approx(-0.30)


def test_a_holding_that_crosses_an_ex_date_is_flagged():
    """The old guard compared an adjustment factor that was always NaN.

    It promised to refuse an unadjusted holding that spanned a corporate action
    and in fact never fired, because ``daily`` ships neither ``adj_close`` nor
    ``adj_factor``. The exact detector is ``pre_close != previous close``: the
    exchange re-bases the price overnight. A hold of one session that spans an
    ex-date is now marked, so its return can be re-measured from the
    pre_close-chain series rather than quoted raw.
    """
    store = make_store()
    daily = store.data["daily"]
    # A 2-for-1 split on the exit day: pre_close is halved with the price.
    hit = (daily["ts_code"] == "600001.SH") & (daily["trade_date"] == "20240104")
    daily.loc[hit, "pre_close"] = 5.5
    daily.loc[hit, ["open", "close"]] = [5.5, 5.775]

    trades = run(store).set_index("ts_code")
    assert trades.loc["600001.SH", "corporate_action"]
    assert not trades.loc["600002.SH", "corporate_action"]
