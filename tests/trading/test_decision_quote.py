"""P0-A：票据的意图字段一律全可选、默认 null，不碰 price_ref。

这些字段在 arm() 时才被冻结。在两阶段 approve 落地之前，propose 出来的
票必须和改造前**逐字同形**——否则「写了条件却还没有兑现分支」的票会
沿 approve 的普通路径立刻成交。
"""

from __future__ import annotations

from service import settings
from tests.trading.helpers import pick_symbols, proposal

INTENT_FIELDS = (
    "decision_ts", "decision_quote", "decision_frame_id",
    "decision_quote_basis", "contingency",
)


def test_proposed_ticket_carries_all_five_null_fields(desk):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol), actor="agent")
    for key in INTENT_FIELDS:
        assert key in ticket, key
        assert ticket[key] is None, f"{key} 在 propose 阶段必须为空"
    # 估值口径没被新基准顶替：price_ref 仍是日线 snap.last。
    assert "price_ref" in ticket and ticket["price_ref"]
    assert "status" in ticket


def test_trigger_keys_are_exactly_the_two_declared():
    """v4 #16 原本守「P0 只增 trigger.enabled 一个总开关，ttl/confirm/上限复用既有键」。

    2026-11-09 刻意拆成 observe（影子求值、零资金）与 enabled（兑现出钱）两级，
    所以基线从 1 个键变成 2 个。仍用**精确集合**断言而不是放宽成 >=1：
    这道墙的价值就是不让第三个来路不明的 trigger 开关悄悄混进来。
    """
    assert [k for k in settings.SPECS if k.startswith("trigger.")] == [
        "trigger.observe", "trigger.enabled"]
