"""Derived T0 pools must match the exchange limit rules exactly."""

from __future__ import annotations

import pandas as pd
import pytest

from scripts.derive_pools import _warmup_incomplete
from service.adapters.derive import derive_pools, pool_counts, zt_next_day_stats


def _bars(rows):
    return pd.DataFrame(rows, columns=[
        "ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount",
    ])


def _limits(rows):
    return pd.DataFrame(rows, columns=["ts_code", "trade_date", "up_limit", "down_limit"])


DAILY = _bars([
    # A: two consecutive limit-ups, then a normal day
    ("600001.SH", "20240102", 10.5, 11.00, 10.4, 11.00, 10.00, 1000, 1e6),
    ("600001.SH", "20240103", 11.5, 12.10, 11.3, 12.10, 11.00, 1200, 1e6),
    ("600001.SH", "20240104", 12.2, 12.90, 12.0, 12.50, 12.10, 900, 1e6),
    # B: touched the limit but closed below -> broken board
    ("600002.SH", "20240102", 10.2, 11.00, 10.1, 10.50, 10.00, 800, 1e6),
    # C: closed at the lower limit
    ("600003.SH", "20240102", 9.3, 9.4, 9.00, 9.00, 10.00, 700, 1e6),
])

LIMITS = _limits([
    ("600001.SH", "20240102", 11.00, 9.00),
    ("600001.SH", "20240103", 12.10, 9.90),
    ("600001.SH", "20240104", 13.31, 10.89),
    ("600002.SH", "20240102", 11.00, 9.00),
    ("600003.SH", "20240102", 11.00, 9.00),
])


def test_pools_are_derived_from_limit_prices():
    pools = derive_pools(DAILY, LIMITS)
    assert pool_counts(pools) == {"limit_up": 2, "broken_board": 1, "limit_down": 1}

    a = pools[pools.code == "600001.SH"].sort_values("trade_date")
    assert a["pool"].tolist() == ["limit_up", "limit_up"]
    assert a["boards"].tolist() == [1, 2]

    b = pools[pools.code == "600002.SH"].iloc[0]
    assert b["pool"] == "broken_board"
    assert b["boards"] == 0

    c = pools[pools.code == "600003.SH"].iloc[0]
    assert c["pool"] == "limit_down"
    assert c["limit_price"] == 9.0


def test_normal_day_produces_no_rows():
    quiet = _bars([("600001.SH", "20240102", 10.0, 10.2, 9.9, 10.1, 10.0, 1000, 1e6)])
    limits = _limits([("600001.SH", "20240102", 11.0, 9.0)])
    assert derive_pools(quiet, limits).empty


def test_boards_reset_after_a_normal_day():
    bars = _bars([
        ("600001.SH", "20240102", 10.5, 11.0, 10.4, 11.0, 10.0, 1, 1),
        ("600001.SH", "20240103", 11.2, 12.1, 11.0, 12.1, 11.0, 1, 1),
        ("600001.SH", "20240104", 12.3, 12.5, 12.0, 12.2, 12.1, 1, 1),
        ("600001.SH", "20240105", 12.3, 13.42, 12.2, 13.42, 12.2, 1, 1),
    ])
    limits = _limits([
        ("600001.SH", "20240102", 11.0, 9.0),
        ("600001.SH", "20240103", 12.1, 9.9),
        ("600001.SH", "20240104", 13.31, 10.89),
        ("600001.SH", "20240105", 13.42, 10.98),
    ])
    pools = derive_pools(bars, limits)
    hits = pools[pools.pool == "limit_up"].sort_values("trade_date")
    assert hits["boards"].tolist() == [1, 2, 1]


def test_a_broken_board_still_knows_which_rung_it_failed_on():
    """炸板行的 ``boards`` 是 0，但"几进几"不是 0。

    游资看身位看的是"3 进 4 失败"还是"首板没封住"，两者的次日分布不同。没有
    ``prev_boards``，这张表算不出来——而它正是打板唯一能优化的东西。
    不复用 ``boards`` 存这个数：同一个字段两种含义，读的人迟早会读错。
    """
    bars = _bars([
        ("600004.SH", "20240102", 10.5, 11.0, 10.4, 11.0, 10.0, 1, 1),   # 1 板
        ("600004.SH", "20240103", 11.6, 12.1, 11.5, 12.1, 11.0, 1, 1),   # 2 板
        ("600004.SH", "20240104", 12.5, 13.31, 12.2, 12.6, 12.1, 1, 1),  # 摸板未封
    ])
    limits = _limits([
        ("600004.SH", "20240102", 11.0, 9.0),
        ("600004.SH", "20240103", 12.1, 9.9),
        ("600004.SH", "20240104", 13.31, 10.89),
    ])
    pools = derive_pools(bars, limits).sort_values(["trade_date", "pool"])
    broken = pools[pools.pool == "broken_board"].iloc[0]
    assert broken["boards"] == 0, "没有完成的连板"
    assert broken["prev_boards"] == 2, "今天是 3 进 4，失败在第 4 板"

    up = pools[pools.pool == "limit_up"]
    assert up["prev_boards"].tolist() == [0, 1]
    # 封住的行必须满足 boards = prev_boards + 1；这是这两列唯一的对账关系。
    assert (up["boards"] == up["prev_boards"] + 1).all()


def test_next_day_premium_uses_next_available_bar():
    pools = derive_pools(DAILY, LIMITS)
    stats = zt_next_day_stats(DAILY, pools)
    first = stats[(stats.code == "600001.SH") & (stats.trade_date == "20240102")].iloc[0]
    assert first["next_open"] == 11.5
    assert first["open_premium"] == 11.5 / 11.0 - 1


def test_next_day_premium_uses_exchange_pre_close_on_ex_dividend_day():
    daily = _bars([
        ("600001.SH", "20240102", 10.5, 11.0, 10.0, 11.0, 10.0, 1000, 1e6),
        ("600001.SH", "20240103", 10.5, 11.0, 10.0, 10.8, 10.0, 1000, 1e6),
    ])
    pools = pd.DataFrame({"code": ["600001.SH"], "trade_date": ["20240102"],
                          "pool": ["limit_up"], "close": [11.0]})
    row = zt_next_day_stats(daily, pools).iloc[0]
    assert row["next_pre_close"] == 10.0
    assert row["open_premium"] == pytest.approx(0.05), (
        "raw 11.0 close would invent an ex-dividend loss"
    )


def test_bar_after_a_suspension_is_not_called_the_next_trading_day():
    daily = _bars([
        ("600001.SH", "20240102", 10.5, 11.0, 10.0, 11.0, 10.0, 1000, 1e6),
        ("600001.SH", "20240104", 12.0, 12.2, 11.8, 12.1, 11.5, 1000, 1e6),
        ("600002.SH", "20240102", 20.0, 20.2, 19.8, 20.1, 20.0, 1000, 1e6),
        ("600002.SH", "20240103", 20.1, 20.3, 19.9, 20.2, 20.1, 1000, 1e6),
    ])
    pools = pd.DataFrame({"code": ["600001.SH"], "trade_date": ["20240102"],
                          "pool": ["limit_up"], "close": [11.0]})
    row = zt_next_day_stats(daily, pools).iloc[0]
    assert row["next_trade_date"] == "20240104"
    assert row["expected_next_trade_date"] == "20240103"
    assert pd.isna(row["next_open"])
    assert pd.isna(row["open_premium"])
    assert pd.isna(row["open_to_close"])


def test_warmup_must_extend_until_the_streak_has_a_reset():
    assert _warmup_incomplete(DAILY, LIMITS, "20240103")

    with_reset = pd.concat([
        _bars([("600001.SH", "20240101", 9.8, 10.0, 9.7, 9.9, 9.8, 1, 1)]),
        DAILY,
    ], ignore_index=True)
    limits_with_reset = pd.concat([
        _limits([("600001.SH", "20240101", 10.89, 8.91)]),
        LIMITS,
    ], ignore_index=True)
    assert not _warmup_incomplete(with_reset, limits_with_reset, "20240103")


def test_a_suspension_breaks_the_streak_instead_of_joining_it():
    """停牌不是"没有行"，是"没有交易日"——复牌那天是首板。

    002309.SZ 2024-11-04..11-08 是 5 个板，停牌两个月，2025-01-09 复牌又收在涨停价上。
    按行数连板会把它算成第 6 板，再连到第 8 板；而它是一张新的首板。
    这条错误会被 max_boards、晋级率、情绪标签三处同时读走。
    """
    bars = _bars([
        ("002309.SZ", "20241104", 1.9, 2.00, 1.9, 2.00, 1.82, 100, 1e6),
        ("002309.SZ", "20241105", 2.0, 2.10, 2.0, 2.10, 2.00, 100, 1e6),
        ("002309.SZ", "20250109", 2.4, 2.56, 2.4, 2.56, 2.33, 100, 1e6),
        ("002309.SZ", "20250110", 2.6, 2.69, 2.6, 2.69, 2.56, 100, 1e6),
    ])
    limits = _limits([
        ("002309.SZ", "20241104", 2.00, 1.64),
        ("002309.SZ", "20241105", 2.10, 1.80),
        ("002309.SZ", "20250109", 2.56, 2.10),
        ("002309.SZ", "20250110", 2.69, 2.30),
    ])
    # 中间那些交易日市场开着，只是这只票没有行。
    calendar = ["20241104", "20241105", "20241106", "20241107", "20241108",
                "20250106", "20250107", "20250108", "20250109", "20250110"]

    pools = derive_pools(bars, limits, calendar_days=calendar)
    up = pools[pools["pool"] == "limit_up"].set_index("trade_date")["boards"]

    assert up["20241105"] == 2
    assert up["20250109"] == 1, "复牌首板，不是第 6 板"
    assert up["20250110"] == 2


def test_without_a_calendar_the_gap_is_still_caught_from_the_frame():
    """不传日历时也能发现断层——只要帧里还有别的票作参照。

    生产里传的是整市场日线，所以这条是默认行为；单票调用才需要显式日历。
    """
    bars = _bars([
        ("002309.SZ", "20241104", 1.9, 2.00, 1.9, 2.00, 1.82, 100, 1e6),
        ("002309.SZ", "20241105", 2.0, 2.10, 2.0, 2.10, 2.00, 100, 1e6),
        ("002309.SZ", "20250109", 2.4, 2.56, 2.4, 2.56, 2.33, 100, 1e6),
        # 另一只票在中间每天都有行，于是"20250109 的上一交易日"不是 20241105
        ("600001.SH", "20250108", 10.0, 10.1, 9.9, 10.0, 10.0, 100, 1e6),
        ("600001.SH", "20250109", 10.0, 10.1, 9.9, 10.0, 10.0, 100, 1e6),
    ])
    limits = _limits([
        ("002309.SZ", "20241104", 2.00, 1.64),
        ("002309.SZ", "20241105", 2.10, 1.80),
        ("002309.SZ", "20250109", 2.56, 2.10),
        ("600001.SH", "20250108", 11.00, 9.00),
        ("600001.SH", "20250109", 11.00, 9.00),
    ])

    up = derive_pools(bars, limits)
    up = up[up["pool"] == "limit_up"].set_index("trade_date")["boards"]

    assert up["20250109"] == 1
