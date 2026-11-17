"""两个来源的涨停家数差得明显时，要说清楚差在哪。

``tape.count_gap_warn``（默认 0.05）是这个台子里唯一一个**声明了但测试套件
从没读过的配置键**——``scripts/audit_config_keys.py`` 报告"168 声明 / 167 读 /
1 未读"，指的就是它。

那个脚本的语义是"读了没有"，不是"代码里写没写"：``service/trading/tape.py``
确实在用它，但读它的那条分支不在任何测试的覆盖路径上，所以探针没看见。
后果是：**一个专门用来解释两家口径不一致的规则，从没有被验证过它真的会说话。**

这个文件把那条分支跑出来。

    python scripts/audit_config_keys.py   # 跑完整套件之后，这一项应该变成"已读"
"""

from __future__ import annotations

import pytest

from service.trading import tape


@pytest.fixture
def desk_with_pools(desk):
    """A desk whose pools are present, so limit_structure has something to say."""
    return desk


def _vendor_limit_up(desk, day: str) -> int | None:
    """The vendor's own count for the day, read the same way the endpoint does."""
    frame = tape._read(desk, "ths_limit_up", day, day)
    if frame is None or frame.empty:
        return None
    frame = frame[frame["trade_date"].astype(str) == day]
    return len(frame) if len(frame) else None


def test_the_gap_note_is_written_when_two_sources_disagree(desk_with_pools,
                                                           monkeypatch):
    """派生池与同花顺差得超过阈值时，答案里必须有一句解释。"""
    desk = desk_with_pools
    day = desk.market.latest_bar_date()
    vendor = _vendor_limit_up(desk, day)
    if not vendor:
        pytest.skip("本地没有同花顺涨停池，跳过对账分支")

    # 造一个差得足够远的派生池：把涨停家数改成同花顺的十倍。
    real_breadth = tape.breadth(desk.data, day)
    assert real_breadth is not None, "派生池没有数据，跳过"
    skewed = dict(real_breadth)
    skewed["limit_up"] = max(vendor * 10, vendor + 10)
    monkeypatch.setattr(tape, "breadth", lambda store, d: skewed)

    result = tape.limit_structure(desk, day)
    notes = result.get("notes") or []
    gap_notes = [note for note in notes if "两个来源差" in note]
    assert gap_notes, (
        f"两个来源差了 {abs(skewed['limit_up'] - vendor) / max(skewed['limit_up'], vendor):.0%}，"
        f"却没有写明：{notes}")


def test_the_gap_note_is_not_written_when_the_sources_agree(desk_with_pools,
                                                            monkeypatch):
    """两家一致时不要多嘴——否则每条答案都挂着一句"我们核对过，没差"。"""
    desk = desk_with_pools
    day = desk.market.latest_bar_date()
    vendor = _vendor_limit_up(desk, day)
    if not vendor:
        pytest.skip("本地没有同花顺涨停池，跳过对账分支")

    real_breadth = tape.breadth(desk.data, day)
    if real_breadth is None:
        pytest.skip("派生池没有数据，跳过")
    aligned = dict(real_breadth)
    aligned["limit_up"] = vendor
    monkeypatch.setattr(tape, "breadth", lambda store, d: aligned)

    result = tape.limit_structure(desk, day)
    notes = result.get("notes") or []
    assert not [note for note in notes if "两个来源差" in note], (
        f"两家一致却写了差异说明：{notes}")
