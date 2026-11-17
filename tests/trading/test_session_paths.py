"""只在盘中才会走到的分支。

收盘后跑一遍全量测试，绿灯只说明"收盘状态下没问题"。真正花钱的那几个小时里，
代码走的是另一组分支：开盘三十分钟三池还在增量加载、盘中快照会过期、涨停判定
要用实时价而日线还是昨天的、T+1 让卖不掉。

2026-11-02 的事故全部发生在这些分支里（10:30 的快照在 13:18 被当当日终值引用、
实盘仓位在盘中没有告警），所以它们必须有测试，而不是靠第二天再看一眼。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from service.api.trading import POOL_DERIVED_AFTER, POOL_STALE_AFTER, _pool_phase
from service.kernel.clock import SHANGHAI

DAY = "20261102"


def _frame(loaded_at: str):
    """最小可用的池子帧：`_pool_phase` 只读 `ingested_at` 与行数。"""
    import pandas as pd

    return pd.DataFrame({"ingested_at": [loaded_at], "code": ["000001"]})


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 2, hour, minute, tzinfo=SHANGHAI) + timedelta(seconds=second)


def test_opening_thirty_minutes_are_not_a_verdict():
    """09:41 的涨停 0 家不是"今天没有涨停"，是还没加载。"""
    frame = _frame(_at(9, 41).isoformat())

    phase, state = _pool_phase(DAY, frame, frame, frame, now=_at(9, 45))

    assert phase == "opening_window"
    assert state["settled"] is False
    assert "未成形" in state["warning"]


def test_an_intraday_snapshot_that_is_ten_minutes_old_is_stale():
    frame = _frame(_at(10, 30).isoformat())

    phase, state = _pool_phase(DAY, frame, frame, frame, now=_at(10, 41))

    assert phase == "intraday"
    assert state["stale"] is True
    assert state["age_seconds"] == 660
    assert state["derived_from_stale"] is False


def test_the_1102_case_reports_derived_ratios_as_stale():
    """10:30 的快照，13:18 还在算封单/成交比——差 2.8 小时。"""
    frame = _frame(_at(10, 30, 12).isoformat())

    phase, state = _pool_phase(DAY, frame, frame, frame, now=_at(13, 18, 39))

    assert phase == "intraday"
    assert state["age_seconds"] == pytest.approx(2 * 3600 + 48 * 60 + 27, abs=2)
    assert state["age_seconds"] > POOL_DERIVED_AFTER
    assert state["derived_from_stale"] is True


def test_a_fresh_snapshot_is_not_flagged():
    frame = _frame(_at(13, 30, 0).isoformat())

    _, state = _pool_phase(DAY, frame, frame, frame, now=_at(13, 30, 90))

    assert state["stale"] is False
    assert state["age_seconds"] == 90


def test_the_thresholds_are_what_the_docstring_says():
    assert POOL_STALE_AFTER == 600
    assert POOL_DERIVED_AFTER == 1800


def test_after_the_close_the_last_snapshot_is_still_not_settled():
    """15:30：收盘了，但日更 17:30 才写终值。此刻的数是 14:45 快照。"""
    frame = _frame(_at(14, 45).isoformat())

    phase, state = _pool_phase(DAY, frame, frame, frame, now=_at(15, 30))

    assert phase == "stale_close"
    assert state["settled"] is False
    assert "14:45" in state["warning"]


def test_a_previous_day_is_settled_regardless_of_time():
    frame = _frame(_at(14, 45).isoformat())

    phase, state = _pool_phase("20261030", frame, frame, frame, now=_at(10, 0))

    assert phase == "settled"
    assert state["settled"] is True


def test_a_mid_session_minute_read_does_not_poison_the_partition(desk):
    """盘中查一只票的分时，不能把半天的数据写进分区。

    写了之后，晚间 `intraday_sweep` 的 `needs_fetch` 看到"已有行"就跳过这只票，
    半日数据留下来却像一整天——一次盘中查询足以让 17:30 的全市场补齐漏掉它。
    """
    from datetime import datetime

    from service.kernel.clock import SHANGHAI
    from service.trading.intraday import collect

    today = datetime.now(SHANGHAI).strftime("%Y%m%d")
    frame, how = collect(desk.market, "600519.SH", today)

    if how == "stored":
        pytest.skip("今天的分钟线已经入库，测不到'当日不落库'分支")
    # 开盘前（或非交易日）数据源根本没有当天分时，返回 ``provider_has_no_<day>``
    # 与空表——这一条同样**不写分区**，测试想守的东西在两种情况下都守住了。
    # 原来只认"不落库"四个字，于是每天开盘前跑一次就红一次。
    #
    # 同理 `unavailable` / `failed:*`：取数失败自然没有分区可写。这条测试守的是
    # "盘中不得落库"，不是"取数必须成功"——把网络故障也判成红，等于让这台台子
    # 在断网那天报一个它没犯的错，而真正的失败（fetched 且不带"不落库"）反而
    # 会被噪声盖住。2026-11-04 05:59 的全量跑正是这样红了一次。
    if how in ("unavailable", "not_collected") or how.startswith("failed:"):
        pytest.skip(f"取数没成功（{how}），没有分区可污染，这条测不到")
    assert ("不落库" in how) or how.startswith("provider_has_no_"), how
    assert not frame.empty or how.startswith(("unavailable", "provider_has_no"))


def test_after_the_close_a_stale_close_is_also_flagged_stale():
    """收盘后读到 14:45 的快照：`age_seconds` 五千多秒，`stale` 不能是 false。

    两个字段互相打脸比没有字段更糟——读的人会挑那个顺手的信。
    """
    frame = _frame(_at(14, 45).isoformat())

    phase, state = _pool_phase(DAY, frame, frame, frame, now=_at(16, 30))

    assert phase == "stale_close"
    assert state["stale"] is True
    assert state["age_seconds"] > POOL_DERIVED_AFTER
