"""T+1 限价挂单（低吸）：日线能精确验证成交的那个入场点。

为什么这个入场点值得单独存在
----------------------------
排板要在这个价位**排队**——日线给不了队列信息，所以「打板」的成交概率永远是估的。
限价挂单相反：它坐在下方等价格过来，成交判据只有一个数——当日最低价有没有触及。
日线 OHLC 里有这个数，而且是逐笔核对得了的。

2026-11-03 的分析里，这个区别决定了整个结论的形状：限价单只在价格跌破涨停价时
成交，而一字板（从未跌破）的回封率按构造是 100% 且买不到。把两类混在一起，
"打高位板回封率更高"就成了一个对下单能力不成立的结论。

这里的断言只关心机械正确性：成交条件、成交价、时点、以及折让必须真的起作用。
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


def make_store(t_path, t1_path, t2_path) -> FakeStore:
    """三根日线：T（信号日）、T+1（挂单日）、T+2（出场日）。

    每根给 (open, low, close)。low 是这一层的分水岭——成交与否只看它。
    """
    rows, limits = [], []
    prev = t_path[2]
    for day, (open_px, low_px, close_px) in zip(DAYS, (t_path, t1_path, t2_path)):
        rows.append({"ts_code": "600001.SH", "trade_date": day, "open": open_px,
                     "low": low_px, "close": close_px, "pre_close": prev,
                     "amount": 100000})
        limits.append({"ts_code": "600001.SH", "trade_date": day,
                       "up_limit": round(prev * 1.1, 2),
                       "down_limit": round(prev * 0.9, 2)})
        prev = close_px
    pools = pd.DataFrame([
        {"ts_code": "600001.SH", "trade_date": "20240102", "pool": "limit_up",
         "boards": 1, "name": "甲股份", "amount": 100000, "limit_price": t_path[2]},
    ])
    return FakeStore(pd.DataFrame(rows), pd.DataFrame(limits), pools)


def run(store, **spec_kwargs) -> pd.DataFrame:
    panel = load_panel(store, "20240102", "20240104")
    pools = store.read("limit_pool_t0", "20240102", "20240104")
    spec = EventStudy(pool="limit_up", boards=(1,), **spec_kwargs)
    return build_trades(panel, pools, spec, market_returns(panel))


def test_a_limit_order_fills_when_the_low_touches_it():
    """T 收盘 10.0，折让 3% → 挂 9.70；T+1 最低 9.60 触及 → 按 9.70 成交。

    开盘 10.20 在挂单价之上，所以不是开盘成交——这正是"等价格来找你"的形状。
    """
    store = make_store(t_path=(10.0, 9.9, 10.0), t1_path=(10.2, 9.6, 10.1),
                       t2_path=(10.4, 10.3, 10.5))
    trades = run(store, entry="next_limit", limit_drop_pct=0.03, hold=1, cost_bps=0)

    assert len(trades) == 1
    trade = trades.iloc[0]
    assert trade["entry_price"] == pytest.approx(9.70, abs=1e-6)
    assert str(trade["entry_date"]) == "20240103", "挂单在 T+1"
    assert str(trade["exit_date"]) == "20240104", "T+1 买入最早 T+2 卖"
    assert trade["gross"] == pytest.approx(10.5 / 9.70 - 1, abs=1e-9)


def test_it_does_not_fill_when_the_low_never_reaches_the_order():
    """T+1 最低 9.90 高于挂单价 9.70 → 不成交。没有这个断言，上面那条就只是
    "总会成交"，而低吸的核心恰恰是**大部分时候不成交**。"""
    store = make_store(t_path=(10.0, 9.9, 10.0), t1_path=(10.2, 9.9, 10.1),
                       t2_path=(10.4, 10.3, 10.5))
    trades = run(store, entry="next_limit", limit_drop_pct=0.03, hold=1, cost_bps=0)

    assert trades.empty


def test_a_gap_down_through_the_order_fills_at_the_open_not_the_limit():
    """T+1 开 9.40，已在挂单价 9.70 之下 → 按 9.40 成交（更优）。

    这不是"占便宜"，是真实撮合顺序：集合竞价先成交，限价买单在开盘价低于
    限价时按开盘价成交。
    """
    store = make_store(t_path=(10.0, 9.9, 10.0), t1_path=(9.4, 9.2, 9.6),
                       t2_path=(9.8, 9.7, 9.9))
    trades = run(store, entry="next_limit", limit_drop_pct=0.03, hold=1, cost_bps=0)

    assert len(trades) == 1
    assert trades.iloc[0]["entry_price"] == pytest.approx(9.40, abs=1e-6)


def test_a_deeper_discount_fills_less_often():
    """折让越深，成交越少——这条把"折让参数真的接进了撮合"钉住。

    如果参数被静默忽略（发生过一次：`parse_spec` 没把它传进 EventStudy），
    两档折让会给出完全一样的成交数，而表面上一切正常。
    """
    store = make_store(t_path=(10.0, 9.9, 10.0), t1_path=(10.2, 9.6, 10.1),
                       t2_path=(10.4, 10.3, 10.5))

    shallow = run(store, entry="next_limit", limit_drop_pct=0.03, hold=1, cost_bps=0)
    deep = run(store, entry="next_limit", limit_drop_pct=0.08, hold=1, cost_bps=0)

    assert len(shallow) == 1, "3% → 9.70，最低 9.60 触及"
    assert deep.empty, "8% → 9.20，最低 9.60 没触及"


def test_limit_drop_is_refused_on_other_entries():
    """折让只在 next_limit 下有意义。允许它在别处静默存在，等于让打错的参数
    看起来"已经调过了"。"""
    with pytest.raises(SpecError, match="limit_drop_pct"):
        parse_spec({"pool": "limit_up", "entry": "next_open", "limit_drop_pct": 0.03})


@pytest.mark.parametrize("drop", [0.0, -0.03, 1.0, 1.5])
def test_limit_drop_must_be_a_positive_fraction(drop):
    with pytest.raises(SpecError, match="limit_drop_pct"):
        parse_spec({"pool": "limit_up", "entry": "next_limit", "limit_drop_pct": drop})


def test_two_discounts_are_two_specs_not_one():
    """同名会让 registry 里两条假设互相覆盖，而"覆盖"看起来像"同一条判决"。"""
    shallow = parse_spec({"pool": "limit_up", "entry": "next_limit",
                          "limit_drop_pct": 0.03, "hold": 1})
    deep = parse_spec({"pool": "limit_up", "entry": "next_limit",
                       "limit_drop_pct": 0.05, "hold": 1})

    assert shallow.name() != deep.name()
    assert "d3" in shallow.name() and "d5" in deep.name()


def test_next_limit_without_a_low_column_says_so():
    """数据集没有 low 时，给一句人话，而不是一个 KeyError。

    别的 entry 不该因此受影响：`low_f{k}` 是按存在性造的。
    """
    bars = pd.DataFrame([
        {"ts_code": "600001.SH", "trade_date": day, "open": 10.0, "close": 10.0,
         "pre_close": 10.0, "amount": 100000} for day in DAYS
    ])
    limits = pd.DataFrame([
        {"ts_code": "600001.SH", "trade_date": day, "up_limit": 11.0,
         "down_limit": 9.0} for day in DAYS
    ])
    pools = pd.DataFrame([
        {"ts_code": "600001.SH", "trade_date": "20240102", "pool": "limit_up",
         "boards": 1, "name": "甲股份", "amount": 100000, "limit_price": 10.0},
    ])
    store = FakeStore(bars, limits, pools)

    # 没有 low：next_open 照常工作
    panel = load_panel(store, "20240102", "20240104")
    assert "low_f1" not in panel.columns
    trades = build_trades(panel, store.read("limit_pool_t0", "20240102", "20240104"),
                          EventStudy(pool="limit_up", boards=(1,), entry="next_open",
                                     hold=1, cost_bps=0), market_returns(panel))
    assert len(trades) == 1

    with pytest.raises(SpecError, match="low"):
        build_trades(panel, store.read("limit_pool_t0", "20240102", "20240104"),
                     EventStudy(pool="limit_up", boards=(1,), entry="next_limit",
                                hold=1, cost_bps=0), market_returns(panel))
