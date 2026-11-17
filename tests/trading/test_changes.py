"""变了什么：两天的可引用数字相减。

叫醒场景的第一问是"和上次比，什么变了"。`/watch` 原来只回答"有没有越过阈值"，
`/facts` 只回答"今天有什么"，中间那一步——差值——一直没人算。
"""

from __future__ import annotations

import pytest

from service.trading import changes


def _two_days(desk):
    day = desk.market.latest_bar_date()
    recent = desk.market.calendar.last_n(day, 6) or []
    assert len(recent) >= 2, "测试需要一个有历史的市场"
    return recent[-2], day


def test_a_diff_only_contains_what_moved(desk):
    since, until = _two_days(desk)
    out = changes.diff(desk, since, until)

    assert out["ok"] is True
    assert (out["from"], out["to"]) == (since, until)
    for row in out["changes"]:
        assert set(row) == {"key", "from", "to", "delta", "material"}
        assert row["delta"] == pytest.approx(row["to"] - row["from"], abs=1e-6)
        # 没变的键不进列表——它们只在 unchanged 里计数。
        assert row["from"] != row["to"]


def test_material_follows_the_stated_threshold(desk):
    """`material` 不是感觉，是表里的阈值；这张表变了，标记必须跟着变。"""
    since, until = _two_days(desk)
    out = changes.diff(desk, since, until)

    for row in out["changes"]:
        limit = changes.MATERIAL.get(row["key"])
        if limit is None or row["key"] == "account.equity":
            continue
        assert row["material"] is (abs(row["delta"]) >= limit)


def test_same_day_is_an_empty_diff_not_an_error(desk):
    day = desk.market.latest_bar_date()
    out = changes.diff(desk, day, day)

    assert out["ok"] is True
    assert out["changes"] == [] and out["sessions"] == 0


def test_no_start_date_asks_for_one_instead_of_guessing(desk):
    out = changes.diff(desk, "")

    assert out["ok"] is False
    assert out["example"], "错误要给出可照抄的调用，否则读者只能猜"


def test_a_day_without_a_snapshot_says_the_account_layer_is_unreliable(desk):
    """过去那天没有快照时，账户层是用今天的账本渲染的。

    这不是小瑕疵：把它当成"昨日的账户"会让读者得出"权益涨了"的结论，
    而那个数字其实来自今天。所以 basis 必须自己说出来。
    """
    since, until = _two_days(desk)
    out = changes.diff(desk, since, until)

    if "快照" in out["basis"]:
        assert "现算" not in out["basis"]
    else:
        assert "不要用" in out["basis"]
