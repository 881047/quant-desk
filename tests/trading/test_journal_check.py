"""复盘里的数字要能在当天的 facts 里找到。

`facts` 存在的理由是一条具体的失败（2026-10-29 的报告写"可买 6 只"，实际 0 只）。
但那条链只管收盘报告、而且事后跑；复盘日志走另一条路——逐字入库、零核对，
而它会被以后的会话读回来。**错的数字在这里会变成记忆，然后被下一次引用。**

所以这里只做一件事：把文本里的数字和当天 facts 对一遍，**不拦、只报**。
"""

from __future__ import annotations

from service.trading import journal_check

FACTS = {
    "day": "20261030",
    "breadth": {"advancers": 893, "decliners": 4602, "equal_weight_return": -0.030363},
    "pools": {"limit_up": 49, "limit_down": 93},
    "account": {"equity": "5007.51", "positions": 0},
}


def test_a_stock_code_is_not_a_fact():
    """600519 是代码不是数字。每次都报它会训练读者忽略这条提示。"""
    assert journal_check.numbers_in("明天看 600519 和 000858") == []


def test_numbers_are_deduplicated_in_order():
    assert journal_check.numbers_in("49 家涨停，3 只候选，又是 49 家") == ["49", "3"]


def test_a_percentage_matches_the_stored_fraction():
    """文本写 3.04%，facts 存 -0.030363——同一个事实，两种写法。"""
    out = journal_check.check("等权跌 3.04%，893 家上涨。", FACTS)

    assert out["matched"] == 2
    assert out["unmatched"] == []


def test_a_number_that_only_matches_in_magnitude_is_flagged_as_such():
    """跌 3.04% 对 -0.030363：方向在文字里，不在符号里。数对上了，符号没核。"""
    out = journal_check.check("等权跌 3.04%。", FACTS)

    assert out["unmatched"] == []
    assert out["sign_from_words"] == ["3.04"]


def test_a_thousands_separator_is_not_a_different_number():
    out = journal_check.check("账户 5,007.51 元，涨停 49 家。", FACTS)

    assert out["matched"] == 2
    assert out["unmatched"] == []


def test_a_number_that_is_not_in_the_session_is_reported_not_rejected():
    out = journal_check.check("今天 912 家上涨。", FACTS)

    assert out["unmatched"] == ["912"]
    assert "不等于编的" in out["note"], "提示要说清它对不上的可能原因"
    # 不抛、不拦——写入照常发生，这是设计
    assert "ok" not in out or out.get("ok") is not False


def test_no_facts_is_skipped_not_passed():
    """没有快照时"没法核对"，不能说成"核对通过"。"""
    out = journal_check.check("今天 893 家上涨。", None)

    assert out["skipped"] is True
    assert out["checked"] == 0
    assert "不是通过" in out["note"]


def test_every_branch_returns_the_same_keys():
    """三个分支少一个键，调用方就得写 if 才能取 counts——而它们都不是异常。"""
    keys = {"checked", "matched", "unmatched"}
    for out in (
        journal_check.check("", FACTS),
        journal_check.check("明天看 600519。", FACTS),
        journal_check.check("今天 893 家。", None),
        journal_check.check("今天 893 家。", FACTS),
    ):
        assert keys <= set(out), out


def test_twenty_numbers_is_the_cap_but_the_rest_are_counted():
    # 五位数：六位数会被当成股票代码跳过（见上一条测试）。
    text = "，".join(str(10000 + i) for i in range(30))
    out = journal_check.check(text, FACTS)

    assert len(out["unmatched"]) == 20
    assert out["unmatched_count"] == 30, "20 是列表上限，不是计数上限"


def test_times_and_dates_are_labels_not_facts():
    """19:30 / 11-01 / 10月30日 是标签。它们各自会贡献两三个对不上的 token，
    把真正的编造淹掉——实测一篇真实复盘 53 个 token 里 35 个是这类噪声。"""
    assert journal_check.numbers_in("19:30 收盘复盘（11-01 复查，10月30日）涨停 49 家") == ["49"]


def test_the_days_own_prices_count_as_reproducible():
    """facts 里没有收盘价，而复盘一定会引用它。只对 facts 会让正确的句子天天报错。"""
    import pandas as pd

    class Market:
        def daily(self, day):
            return pd.DataFrame({"close": [1392.05, 11.64], "pre_close": [1380.0, 11.5],
                                 "amount": [1.5e9, 2.0e9]})

    desk = type("D", (), {"market": Market()})()
    extra = journal_check.market_numbers(desk, "20261030")

    assert 1392.05 in extra and 11.64 in extra
    out = journal_check.check("600519 收 1392.05，平安 11.64。", FACTS, extra)
    assert out["unmatched"] == []


# --------------------------------------------------------- 判不了就说判不了
#
# `verify` 的结论原来只有"匹配了几个数"。而盘中写的笔记，facts 里根本没有当天
# 的日线——它是某一时刻的快照，笔记叙述的是一条时间线。2026-11-04 实测：13:30
# 那条巡检 14 个数里 12 个"未匹配"，模型为此又写了一条 journal 去解释。
# 一天四条盘中笔记，就要写四条解释。

def _stub_desk(latest: str):
    from types import SimpleNamespace
    return SimpleNamespace(market=SimpleNamespace(latest_bar_date=lambda: latest))


def test_a_note_written_before_the_bar_lands_says_it_is_provisional(monkeypatch):
    """当天日线还没落库时，结论要自报「盘中」并说清什么时候能判。

    `unmatched` 在盘中是结构性的，不是写错了——把它当成错误，模型要么反复解释，
    要么学会不再写数字。两种结果都比"核对不完"更糟。
    """
    from service.trading import journal_check

    monkeypatch.setattr(journal_check, "market_numbers", lambda _d, _day: set())

    verdict = journal_check.verify(
        _stub_desk("20261103"), "涨停 36 家，封板率 0.4038", "20261104",
        facts={"counts": {"limit_up": 31}}, compute=False)

    assert verdict["basis"] == "intraday"
    assert "20261103" in verdict["basis_note"], "要说出最近一根日线是哪天"
    assert "不需要为它补写解释" in verdict["basis_note"]
    # 该错的仍然标出来：这不是"盘中就全放过"
    assert "36" in verdict["unmatched"]


def test_a_note_written_after_the_bar_lands_is_checked_against_the_close(monkeypatch):
    """收盘后的笔记没有这个豁免——那是可以真判的。"""
    from service.trading import journal_check

    monkeypatch.setattr(journal_check, "market_numbers", lambda _d, _day: set())

    verdict = journal_check.verify(
        _stub_desk("20261104"), "涨停 31 家", "20261104",
        facts={"counts": {"limit_up": 31}}, compute=False)

    assert verdict["basis"] == "close"
    assert "basis_note" not in verdict
