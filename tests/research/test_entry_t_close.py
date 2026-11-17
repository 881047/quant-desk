"""T 日收盘买入：这套语言原先表达不了的那个入场点。

在此之前的入场只有 ``next_open`` 与 ``next_close``——都落在 T+1 及之后。
于是所有"打板"假设实际测的都是"次日开盘接力"，而溢价在隔夜段：
2023-01~2026-10 的首板，T 收→T+1 收 +1.75%，其中隔夜 +2.00%、T+1 日内 −0.22%。
测错了那一段，结论就会写成"打板不赚钱"，而那笔钱本来就不在次日日内。

这里的断言只关心两件事：价格与时点对不对，以及日收益序列有没有把同一段算两遍。
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.research.spec import (
    EventStudy,
    SpecError,
    build_trades,
    load_panel,
    market_returns,
    parse_spec,
)
from tests.research.conftest import FakeStore

DAYS = ["20240102", "20240103", "20240104"]

# (open, close) per session. D0 is T, the session we buy at the close of.
PRICES = {
    "600001.SH": [(10.0, 10.0), (10.5, 11.0), (11.5, 12.0)],
}


def make_store(prices: dict | None = None) -> FakeStore:
    rows, limits = [], []
    for code, path in (prices or PRICES).items():
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
         "name": "甲股份", "amount": 100000, "limit_price": path[0][1]}
        for code, path in (prices or PRICES).items()
    ])
    return FakeStore(pd.DataFrame(rows), pd.DataFrame(limits), pools)


def run(store, **spec_kwargs) -> pd.DataFrame:
    panel = load_panel(store, "20240102", "20240104")
    pools = store.read("limit_pool_t0", "20240102", "20240104")
    spec = EventStudy(pool="limit_up", boards=(1,), **spec_kwargs)
    return build_trades(panel, pools, spec, market_returns(panel))


def test_t_close_buys_the_close_of_the_signal_day():
    trade = run(make_store(), entry="t_close", hold=1).set_index("ts_code").loc["600001.SH"]
    assert trade["entry_date"] == "20240102", "入场是 T 本身，不是 T+1"
    assert trade["entry_price"] == pytest.approx(10.0)
    assert trade["exit_date"] == "20240103"
    assert trade["exit_price"] == pytest.approx(11.0)
    assert trade["gross"] == pytest.approx(0.10, abs=1e-9)


def test_t_close_with_an_open_exit_is_the_overnight_leg():
    """hold=1 + exit_at=open 是"只吃隔夜"：T 收 → T+1 开。

    这是打板溢价真正所在的那一段，也是原先完全测不到的一段。
    """
    trade = run(make_store(), entry="t_close", hold=1,
                exit_at="open").set_index("ts_code").loc["600001.SH"]
    assert trade["entry_date"] == "20240102"
    assert trade["exit_date"] == "20240103"
    assert trade["exit_price"] == pytest.approx(10.5)
    assert trade["gross"] == pytest.approx(0.05, abs=1e-9)


def test_next_open_still_starts_one_session_later():
    """新入场点不能顺手改掉旧语义：next_open 的入场仍在 T+1。"""
    trade = run(make_store(), entry="next_open", hold=1).set_index("ts_code").loc["600001.SH"]
    assert trade["entry_date"] == "20240103"
    assert trade["entry_price"] == pytest.approx(10.5)
    assert trade["exit_date"] == "20240104"
    assert trade["gross"] == pytest.approx(12.0 / 10.5 - 1, abs=1e-9)


def test_the_daily_series_does_not_count_the_first_session_twice():
    """入场腿对 t_close 是零收益，T 收→T+1 的收益只由 path 的第一根承担。

    不置零的话，`close_f1 / entry_price` 与 path 里 T+1 那根会把同一段算两遍，
    复利后的曲线比真实收益还好一些——正好是那种"回测好看、实盘对不上"的错。
    """
    trades = run(make_store(), entry="t_close", hold=1)
    trade = trades.set_index("ts_code").loc["600001.SH"]
    legs = trades.attrs["daily_rows"]
    legs = legs[legs["trade_id"] == trade["trade_id"]]
    compounded = (1 + legs["ret"]).prod() - 1
    # `net` 是减法口径（gross − cost），日收益序列是乘法口径，两者差 r×cost。
    # 要验的是"没有重复计一段"，所以按乘法口径算期望值。
    expected = (1 + trade["gross"]) * (1 - trade["cost"]) - 1
    assert compounded == pytest.approx(expected, abs=1e-9)
    assert len(legs) == 2, "T 一根 + T+1 一根"


def test_a_sealed_close_is_still_filled_and_the_optimism_is_documented():
    """收盘封在涨停价时，日线会说"买到了"。这是排队的假设，不是事实。

    这条测试锁的是**行为**：不能用"封住"把样本剔掉——剔掉之后剩下的全是
    炸板，均值会从"含排板"变成"只含炸板"，反向偏成另一个谎。
    真实成交概率归 fill_model，不归价格层。
    """
    sealed = {"600001.SH": [(10.0, 11.0), (11.0, 12.1), (12.1, 13.0)]}
    store = make_store(sealed)
    trade = run(store, entry="t_close", hold=1).set_index("ts_code").loc["600001.SH"]
    assert trade["entry_price"] == pytest.approx(11.0)
    assert trade["gross"] == pytest.approx(0.10, abs=1e-9)


def test_t_close_with_hold_zero_is_refused():
    with pytest.raises(SpecError, match="zero-length"):
        parse_spec({"pool": "limit_up", "boards": [1], "entry": "t_close", "hold": 0})


def test_t_close_parses_and_names_itself():
    spec = parse_spec({"pool": "limit_up", "boards": [1], "entry": "t_close", "hold": 1})
    assert spec.entry == "t_close"
    assert "t_close" in spec.name(), "网格去重靠 name()，入场点必须在名字里"
