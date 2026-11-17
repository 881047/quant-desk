"""Quality checks must be sensitive (catch real defects) and specific (few false alarms)."""

from __future__ import annotations

import pandas as pd
import pytest

from service.adapters.catalog import get_spec
from service.adapters.quality import evaluate, repair
from tests.data.helpers import make_adj, make_daily


def _eval(dataset, df, calendar=None):
    return evaluate(dataset, "2024", df, calendar_days=calendar)


def test_clean_panel_is_ok():
    days = ["20240102", "20240103", "20240104"]
    q = _eval("daily", make_daily(days), set(days))
    assert q.status == "ok"
    assert q.issues == []


def test_a_zero_limit_is_info_not_a_warning():
    """First-day/delisting rows have no band; that is a count, not a defect.

    A `warn` here would make every stk_limit partition with one IPO look
    unhealthy, which is exactly the "always-on alert" this file warns about.
    """
    df = pd.DataFrame({
        "ts_code": ["920047.BJ", "600610.SH"],
        "trade_date": ["20260709", "20260709"],
        "up_limit": [99999.99, 6.85],
        "down_limit": [0.0, 6.85],
        "close": [1.0, 6.85],
    })
    q = evaluate("stk_limit", "2026", df)

    issue = next(i for i in q.issues if i.code == "limit_band_absent")
    assert issue.severity == "info"
    assert issue.count == 1
    assert q.status == "ok"


def test_duplicate_key_quarantines():
    df = make_daily(["20240102", "20240103"])
    df = pd.concat([df, df.iloc[[0]]], ignore_index=True)
    q = _eval("daily", df, {"20240102", "20240103"})
    codes = {i.code for i in q.issues}
    assert "duplicate_key" in codes
    assert q.status == "quarantine"


def test_non_positive_price_is_error():
    df = make_daily(["20240102", "20240103"])
    df.loc[0, "close"] = 0.0
    q = _eval("daily", df, {"20240102", "20240103"})
    assert any(i.code == "non_positive_price" and i.severity == "error" for i in q.issues)
    assert q.status == "quarantine"


@pytest.mark.parametrize(("column", "value"), [
    ("close", float("nan")),
    ("high", float("inf")),
    ("open", float("-inf")),
])
def test_missing_or_non_finite_price_is_error(column, value):
    df = make_daily(["20240102"])
    df.loc[0, column] = value
    q = _eval("daily", df)
    issue = next(i for i in q.issues if i.code == "non_positive_price")
    assert issue.severity == "error"
    assert issue.count == 1
    assert q.status == "quarantine"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0])
def test_missing_or_non_finite_volume_is_error(value):
    df = make_daily(["20240102"])
    df.loc[0, "vol"] = value
    q = _eval("daily", df)
    issue = next(i for i in q.issues if i.code == "volume_invalid")
    assert issue.severity == "error"
    assert issue.count == 1
    assert q.status == "quarantine"


def test_ohlc_contradiction_is_error():
    df = make_daily(["20240102"])
    df.loc[0, "high"] = 1.0
    df.loc[0, "low"] = 5.0
    q = _eval("daily", df, {"20240102"})
    assert any(i.code == "ohlc_inconsistent" for i in q.issues)


def test_zero_volume_with_price_move_is_warning_only():
    days = ["20240102", "20240103", "20240104"]
    df = make_daily(days)
    df.loc[1, "vol"] = 0.0
    q = _eval("daily", df, set(days))
    assert any(i.code == "zero_volume_price_move" and i.severity == "warn" for i in q.issues)
    assert q.status == "warn"


def test_limit_breach_is_detected():
    days = ["20240102", "20240103"]
    df = make_daily(days)
    df.loc[1, "close"] = float(df.loc[1, "up_limit"]) * 1.20
    q = _eval("daily", df, set(days))
    assert any(i.code == "limit_breach" for i in q.issues)


def test_adj_factor_decrease_is_detected():
    days = ["20240102", "20240103"]
    df = make_adj(days, start=2.0, step=-0.5)
    q = _eval("adj_factor", df, set(days))
    assert any(i.code == "adj_factor_decrease" for i in q.issues)


def test_an_adj_factor_decrease_says_how_many_codes_and_whether_they_are_bj():
    """2026-11-14 查过：全库 515 行、22 个年度分区，绝大多数是 .BJ。

    上游自己就是翻的（2017-04-26 的 920627.BJ 返回 1.0、04-27 返回 1.9987），
    不是本台合并出错。修不了，但告警要能告诉人"这是谁"，否则每次都得重新查。
    """
    days = ["20240102", "20240103"]
    df = make_adj(days, code="920627.BJ", start=2.0, step=-0.5)

    q = _eval("adj_factor", df, set(days))

    issue = next(i for i in q.issues if i.code == "adj_factor_decrease")
    assert "只代码" in issue.detail
    assert ".BJ" in issue.detail
    assert "供应商历史因子按日翻转" in issue.detail


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_adj_factor_is_error_and_rejected(value):
    days = pd.bdate_range("2024-01-02", periods=100).strftime("%Y%m%d").tolist()
    df = make_adj(days)
    df.loc[0, "adj_factor"] = value
    q = _eval("adj_factor", df)
    issue = next(i for i in q.issues if i.code == "adj_factor_invalid")
    assert issue.severity == "error"
    assert issue.count == 1
    clean, rejected = repair(df, get_spec("adj_factor"))
    assert len(clean) == 99
    assert len(rejected) == 1


def test_adjusted_price_jump_is_flagged():
    days = [f"202401{d:02d}" for d in range(1, 26)]
    daily = make_daily(days)
    adj = make_adj(days, start=1.0, step=0.0)
    adj.loc[adj.index[-1], "adj_factor"] = 1.9
    df = daily.merge(adj, on=["ts_code", "trade_date"])
    q = _eval("adj_factor", df, set(days))
    assert any(i.code == "adjusted_price_jump" for i in q.issues)


def test_cross_frame_adjustment_check():
    from service.adapters.quality import check_adjustment_continuity

    days = [f"202401{d:02d}" for d in range(1, 26)]
    daily = make_daily(days)
    adj = make_adj(days, start=1.0, step=0.0)
    assert check_adjustment_continuity(daily, adj) == []

    adj.loc[adj.index[-1], "adj_factor"] = 1.9
    issues = check_adjustment_continuity(daily, adj)
    assert issues and issues[0].code == "adjusted_price_jump"
    assert issues[0].severity == "warn"


def test_missing_trading_days_is_warned():
    calendar = [f"2024010{d}" for d in range(2, 10)]  # 8 days
    df = pd.concat(
        [make_daily(["20240102", "20240103"]), make_daily(["20240108", "20240109"])],
        ignore_index=True,
    )
    q = _eval("daily", df, set(calendar))
    issue = next(i for i in q.issues if i.code == "missing_trading_days")
    assert issue.severity == "warn"
    assert issue.count == 1
    assert issue.sample[0]["missing"] == 4


def test_partition_edge_gaps_are_not_hidden_by_per_symbol_bounds():
    calendar = {f"2024010{d}" for d in range(2, 10)}
    df = make_daily(["20240104", "20240105", "20240106"])
    q = _eval("daily", df, calendar)
    issue = next(i for i in q.issues if i.code == "missing_calendar_days")
    assert issue.count == 5
    assert issue.sample[0] == {"trade_date": "20240102", "position": "leading"}
    assert issue.sample[-1] == {"trade_date": "20240109", "position": "trailing"}


def test_suspensions_outside_the_symbols_gap_cannot_hide_missing_days():
    calendar = {f"2024010{d}" for d in range(2, 10)}
    df = pd.concat(
        [make_daily(["20240102", "20240103"]), make_daily(["20240108", "20240109"])],
        ignore_index=True,
    )
    suspensions = {"600000.SH": {f"1999010{d}" for d in range(1, 10)} | {"20240104"}}
    q = evaluate("daily", "2024", df, calendar_days=calendar, suspensions=suspensions)
    issue = next(i for i in q.issues if i.code == "missing_trading_days")
    assert issue.sample[0]["missing"] == 3, "only 20240104 lies inside this panel"


def test_repair_drops_only_unusable_rows():
    days = pd.bdate_range("2024-01-02", periods=100).strftime("%Y%m%d").tolist()
    df = make_daily(days)
    df.loc[1, "close"] = -1.0
    clean, rejected = repair(df, get_spec("daily"))
    assert len(clean) == 99
    assert len(rejected) == 1
    assert days[1] not in set(clean["trade_date"])


def test_repair_preserves_structural_bad_ratio_for_quarantine():
    df = make_daily(["20240102", "20240103", "20240104"])
    df.loc[1, "close"] = -1.0
    preserved, rejected = repair(df, get_spec("daily"))
    assert len(preserved) == 3, "deleting 1/3 would hide the structural defect"
    assert len(rejected) == 1
    q = evaluate("daily", "2024", preserved)
    assert q.status == "quarantine"


def test_duplicate_group_uses_the_same_structural_ratio_as_evaluate():
    days = pd.bdate_range("2024-01-02", periods=100).strftime("%Y%m%d").tolist()
    df = make_daily(days)
    df.loc[[1, 2], :] = df.loc[[0, 0], :].to_numpy()
    preserved, rejected = repair(df, get_spec("daily"))
    assert len(preserved) == 100, "3 duplicate rows are structural even if repair drops only 2"
    assert len(rejected) == 2
    assert evaluate("daily", "2024", preserved).status == "quarantine"


@pytest.mark.parametrize(("column", "value"), [
    ("close", float("nan")),
    ("vol", float("inf")),
])
def test_repair_drops_missing_and_non_finite_values(column, value):
    days = pd.bdate_range("2024-01-02", periods=100).strftime("%Y%m%d").tolist()
    df = make_daily(days)
    df.loc[0, column] = value
    clean, rejected = repair(df, get_spec("daily"))
    assert len(clean) == 99
    assert rejected["trade_date"].tolist() == [days[0]]


def test_pool_coverage_gap_is_reported():
    df = pd.DataFrame(
        {
            "trade_date": ["20240102"] * 3,
            "code": ["600000", "600001", "600002"],
            "name": ["a", "b", "c"],
            "pct_chg": [10.0, 9.9, 10.0],
        }
    )
    q = evaluate("limit_up_pool", "20240102", df, expected_limit_ups=100)
    issue = next(i for i in q.issues if i.code == "pool_coverage_gap")
    assert issue.count == 3
    assert issue.ratio == pytest.approx(0.97, abs=0.01)


def test_empty_pool_is_warning_not_failure():
    q = evaluate("limit_up_pool", "20240102", pd.DataFrame())
    assert q.status == "warn"
    assert q.issues[0].code == "pool_empty"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_quote_price_is_error(value):
    df = pd.DataFrame({"code": ["600000"], "last": [value]})
    q = evaluate("rt_quote", "20240102", df)
    issue = next(i for i in q.issues if i.code == "invalid_last")
    assert issue.severity == "error"
    assert issue.count == 1


def test_stale_quote_is_flagged():
    now = 1_700_000_000
    df = pd.DataFrame(
        {
            "code": ["600000", "600001"],
            "last": [10.0, 11.0],
            "quote_ts": [now - 600, now - 10],
        }
    )
    q = evaluate("rt_quote", "20240102", df, now_epoch=now)
    assert any(i.code == "stale_quote" for i in q.issues)


def test_a_snapshot_read_later_is_not_a_stale_snapshot():
    """The rule compared to the wall clock, so after the close *every* row failed.

    `verify_data` runs at 17:30 over a file whose newest quote is 14:55. All four
    `rt_quote` partitions therefore sat at `warn` with `ratio=1.0` and a detail
    reading "max quote age 34305s" — true, meaningless, and identical every day.
    An alarm that always fires hides the one case worth seeing. Age has to mean
    "behind its own batch", which is what a snapshot actually is.
    """
    stamp = "2023-11-14T14:55:00+08:00"
    capture = pd.Timestamp(stamp).timestamp()
    df = pd.DataFrame({
        "code": ["600000", "600001"],
        "last": [10.0, 11.0],
        "quote_ts": [capture - 5, capture - 10],
        "snapshot_ts": [stamp, stamp],
        "trade_date": ["20231114", "20231114"],
    })

    q = evaluate("rt_quote", "20231114", df)

    assert not any(i.code == "stale_quote" for i in q.issues), \
        "整批一起变旧不是缺陷，判据不该在收盘后必然触发"


def test_a_quote_that_lags_its_own_batch_is_still_flagged():
    """The signal the wall-clock rule was reaching for, kept.

    Measured 2026-10-23: 336 codes carried a price stamped 07:15 in a batch taken
    at 16:15 — PT金田A / 国华退 / ST星源, delisted names whose last trade is
    years old. A frozen price that still looks valid is exactly what a staleness
    check is for.
    """
    stamp = "2023-11-14T14:55:00+08:00"
    capture = pd.Timestamp(stamp).timestamp()
    df = pd.DataFrame({
        "code": ["600000", "000003"],
        "last": [10.0, 2.71],
        "quote_ts": [capture - 5, capture - 26_100],
        "snapshot_ts": [stamp, stamp],
        "trade_date": ["20231114", "20231114"],
    })

    q = evaluate("rt_quote", "20231114", df)

    issue = next(i for i in q.issues if i.code == "stale_quote")
    assert issue.count == 1


def test_a_partition_holding_another_sessions_tape_is_an_error():
    """The failure the age test could not express: right file, wrong day."""
    stamp = "2023-11-13T14:55:00+08:00"
    capture = pd.Timestamp(stamp).timestamp()
    df = pd.DataFrame({
        "code": ["600000"],
        "last": [10.0],
        "quote_ts": [capture],
        "snapshot_ts": [stamp],
        "trade_date": ["20231114"],
    })

    q = evaluate("rt_quote", "20231114", df)

    issue = next(i for i in q.issues if i.code == "quote_not_from_session")
    assert issue.severity == "error"


# ------------------------------------------------ factor vs exchange pre_close
def test_a_factor_that_restarts_breaks_the_pre_close_identity():
    """A 920xxx migration resets ``adj_factor`` to 1.0 while the price does not move.

    ``pre_close`` is the exchange's adjusted previous close, so
    ``pre_close / 前收 == 前因子 / 今因子`` must hold every session. This is the
    shape of 2026-10-26, where eleven Beijing names went from a factor of 1073 to
    1.14 overnight: priced off the factor they lost 99.9% on a day they moved
    0.1%. The jump test only fires above a 35% move, and a factor error of 6% is
    invisible to it — the identity has no such blind spot.
    """
    from service.adapters.quality import check_adjustment_continuity

    days = ["20261020", "20261021", "20261022", "20261023", "20261026"]
    flat = 47.96
    daily = pd.DataFrame({
        "ts_code": "920227.BJ", "trade_date": days,
        "open": flat, "high": flat, "low": flat, "close": flat, "pre_close": flat,
        "vol": 1000.0, "amount": 1e6,
        "up_limit": flat * 1.3, "down_limit": flat * 0.7,
    })
    adj = pd.DataFrame({
        "ts_code": "920227.BJ", "trade_date": days,
        "adj_factor": [1073.1522, 1073.1522, 1073.1522, 1073.1522, 1.1441],
    })

    issues = check_adjustment_continuity(daily, adj)
    conflict = next(i for i in issues if i.code == "adj_factor_conflict")
    assert conflict.count == 1
    assert conflict.severity == "warn"
    assert conflict.sample[0]["rel_error"] > 0.9


def test_a_real_ex_date_satisfies_the_identity():
    """A 10-for-10 split moves ``pre_close`` and the factor by the same ratio.

    The identity is not a "factor must never change" rule — it is "the factor must
    change exactly when, and as much as, the exchange re-bases the price".
    """
    from service.adapters.quality import check_adjustment_continuity

    days = ["20260105", "20260106", "20260107"]
    daily = pd.DataFrame({
        "ts_code": "600000.SH", "trade_date": days,
        "open": [20.0, 10.0, 10.2], "high": [20.2, 10.4, 10.5],
        "low": [19.8, 9.9, 10.1], "close": [20.0, 10.2, 10.4],
        "pre_close": [20.0, 10.0, 10.2], "vol": 1000.0, "amount": 1e6,
        "up_limit": [22.0, 11.0, 11.22], "down_limit": [18.0, 9.0, 9.18],
    })
    adj = pd.DataFrame({"ts_code": "600000.SH", "trade_date": days,
                        "adj_factor": [1.0, 2.0, 2.0]})

    codes = {i.code for i in check_adjustment_continuity(daily, adj)}
    assert "adj_factor_conflict" not in codes


def test_a_resumption_after_a_long_halt_is_not_a_factor_error():
    """``pre_close`` keeps the pre-halt price; the factor absorbed the interim.

    000010.SZ resumed after a multi-year halt: the factor had moved
    4x while the exchange's previous close stayed at the last traded price. That
    is a legitimate gap, not a bad factor, so only consecutive sessions are
    compared.
    """
    from service.adapters.quality import check_adjustment_continuity

    daily = pd.DataFrame({
        "ts_code": "000010.SZ", "trade_date": ["20130718", "20140901"],
        "open": [23.87, 7.0], "high": [23.87, 7.0], "low": [23.87, 7.0],
        "close": [23.87, 7.0], "pre_close": [23.87, 23.87],
        "vol": 1000.0, "amount": 1e6,
        "up_limit": [26.26, 26.26], "down_limit": [21.48, 21.48],
    })
    adj = pd.DataFrame({"ts_code": "000010.SZ", "trade_date": ["20130718", "20140901"],
                        "adj_factor": [2.694, 10.775]})

    codes = {i.code for i in check_adjustment_continuity(daily, adj)}
    assert "adj_factor_conflict" not in codes


def _snapshot(rows: int, halted: int, *, last: float = 10.0) -> pd.DataFrame:
    import pandas as pd

    return pd.DataFrame({
        "code": [f"{i:06d}" for i in range(rows)],
        "last": [last] * (rows - halted) + [0.0] * halted,
        "volume": [1000] * (rows - halted) + [0] * halted,
        "open": [9.9] * (rows - halted) + [0] * halted,
    })


def test_a_halted_stock_is_not_a_bad_quote():
    """停牌的 last 是 0，那是"今天没成交"，不是坏价格。

    这条曾经让每一帧快照都判 quarantine：A 股每天停牌 100–400 只，占全市场 6%
    上下，而 QUARANTINE_RATIO 是 2%。一个每天都响的告警，等于把真正的喂价故障
    藏在里面（stale_quote 当初也是这么坏的）。实测 2026-11-02：5,560 行里 343 只
    last<=0，全部只出现一次、从未再更新——停牌。
    """
    q = evaluate("rt_quote", "20261102", _snapshot(5560, 343))
    assert not any(i.code == "invalid_last" for i in q.issues)
    assert q.status == "ok"


def test_a_quote_feed_that_died_is_not_mistaken_for_a_market_wide_halt():
    """豁免必须带上限：全市场不会有五分之一同时停牌。"""
    q = evaluate("rt_quote", "20261102", _snapshot(100, 100))
    issue = next(i for i in q.issues if i.code == "suspended_share")
    assert issue.severity == "error"
    assert issue.count == 100
    assert q.status == "quarantine"


def test_a_row_with_volume_but_no_price_is_still_broken():
    """分辨停牌与坏行的依据是"有没有成交"，不是"价格是不是 0"。"""
    import pandas as pd

    q = evaluate("rt_quote", "20261102", pd.DataFrame({
        "code": ["600000", "600001"],
        "last": [0.0, 10.0],
        "volume": [5000, 100],
        "open": [9.0, 9.9],
    }))
    issue = next(i for i in q.issues if i.code == "invalid_last")
    assert issue.count == 1
    assert q.status == "quarantine"


def test_without_volume_or_open_every_zero_price_is_broken():
    """缺列时不许把坏数据认成停牌——宁可误报。"""
    import pandas as pd

    q = evaluate("rt_quote", "20261102", pd.DataFrame({
        "code": ["600000", "600001"], "last": [0.0, 10.0],
    }))
    assert any(i.code == "invalid_last" for i in q.issues)


# ------------------------------------------------- 无成交 / 无观测 vs 坏价
def test_a_no_trade_row_is_info_not_a_bad_price():
    """cb_daily 用 open/high/low=0、vol=0 表示"今天没成交"。

    2026-11-15 实测：cb_daily 的 2023–2026 四个年度分区里，942–1190 行是这种
    形状，close 保留上次成交价。当时它们同时被判成 non_positive_price 和
    ohlc_inconsistent（open=0 必然"high 低于 close"），一个分区报两条 error。
    `repair` 还会按坏行把它们删掉——删掉之后"这一天这只债没成交"就不存在了，
    而停牌天数、复权、换手率都要用这个事实。
    """
    days = ["20240102", "20240103", "20240104"]
    df = make_daily(days)
    df.loc[1, ["open", "high", "low"]] = 0.0
    df.loc[1, "vol"] = 0.0

    q = _eval("daily", df, set(days))

    assert not [i for i in q.issues if i.severity == "error"]
    issue = next(i for i in q.issues if i.code == "untraded_row")
    assert (issue.severity, issue.count) == ("info", 1)
    clean, _rejected = repair(df, get_spec("daily"))
    assert len(clean) == 3, "没成交的行是事实，不是坏行"


def test_a_row_with_no_observation_is_info_not_a_bad_price():
    """整行指标为空不是"价格小于零"。

    daily_basic 停牌日 close/pe/pb 全空（2020-09-18 有 90 行），指数发布首日
    只有基点没有 OHLC（399006.SZ 2010-05-31）。把 NaN 一律当坏价，这两类都会
    在血缘表上永远报错，而它们既不能修、也不影响任何可交易的东西。
    """
    df = pd.DataFrame({
        "ts_code": ["000029.SZ", "000670.SZ"],
        "trade_date": ["20200918", "20200918"],
        "close": [float("nan"), float("nan")],
        "pe": [float("nan"), float("nan")],
    })

    q = evaluate("daily_basic", "2020", df)

    assert not [i for i in q.issues if i.severity == "error"]
    issue = next(i for i in q.issues if i.code == "price_absent")
    assert (issue.severity, issue.count) == ("info", 2)


def test_a_price_that_is_absent_but_volume_is_present_is_still_broken():
    """价全空、量非零不是"没有观测"：那一天有人成交。"""
    df = make_daily(["20240102"])
    df.loc[0, ["open", "high", "low", "close"]] = float("nan")

    q = _eval("daily", df, {"20240102"})

    assert any(i.code == "non_positive_price" and i.severity == "error" for i in q.issues)


# ------------------------------------------------- 复权因子下降的三种结局
def test_a_factor_drop_that_comes_back_is_only_a_warning():
    """供应商短时翻值、几天后又对上：序列可疑，但不是"历史被改写"。"""
    days = ["20240102", "20240103", "20240104"]
    df = make_adj(days, start=2.0, step=0.0)
    df.loc[1, "adj_factor"] = 1.99

    q = _eval("adj_factor", df, set(days))

    issue = next(i for i in q.issues if i.code == "adj_factor_decrease")
    assert issue.severity == "warn"
    assert "自愈" in issue.detail


def test_a_factor_restatement_inside_the_universe_stays_an_error():
    """跌下去再没回来，且代码在可用域内：这段历史的复权被永久改写了。"""
    days = ["20240102", "20240103", "20240104", "20240105"]
    df = make_adj(days, start=2.0, step=0.0)
    df.loc[1:, "adj_factor"] = 1.0

    q = _eval("adj_factor", df, set(days))

    issue = next(i for i in q.issues if i.code == "adj_factor_decrease")
    assert issue.severity == "error"
    assert "可用域内" in issue.detail


def test_a_factor_flip_on_a_beijing_listing_is_not_an_error():
    """北交所不在可交易域，且它的因子翻转是供应商按日交替的已知缺陷。"""
    days = ["20240102", "20240103", "20240104"]
    df = make_adj(days, code="920627.BJ", start=2.0, step=0.0)
    df.loc[1, "adj_factor"] = 1.0

    q = _eval("adj_factor", df, set(days))

    issue = next(i for i in q.issues if i.code == "adj_factor_decrease")
    assert issue.severity == "warn"
    assert "供应商历史因子按日翻转" in issue.detail
