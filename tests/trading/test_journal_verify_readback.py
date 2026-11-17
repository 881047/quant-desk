"""核对结论要能被下一个人读到。

P83 给写入加了闸门：`POST /journal` 把文本里的数字和当天 facts 对一遍。但结论只
随响应返回，条目本身只存了一段文字——**第二次引用这条记忆的人，看不到它当初被核过
没有**。记忆的可信度不能只存在于写它的那个会话里。

所以这里测三件事：
1. 存下来的结论是证据，不是建议（`note` 不留，`sign_from_words` 留）；
2. 读回来时它在 `verified` 上，两个出口说的是同一件事；
3. 没核过的条目是 `null`，不是 `{}`——"没核"与"核过没问题"必须能分开。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app
from service.trading import journal_check

TOKEN = {"X-Quant-Token": "change-me-local-only"}
FACTS = {"day": "20261030", "breadth": {"advancers": 893, "equal_weight_return": -0.030363},
         "pools": {"limit_up": 49}}


@pytest.fixture
def client(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------ 存储形状
def test_the_stored_verdict_is_evidence_not_advice():
    """`note` 是同一段话，十条就是 1.1 KB 的重复建议；`sign_from_words` 是证据，留。"""
    verdict = journal_check.check("等权跌 3.04%，今天 912 家上涨。", FACTS)
    stored = journal_check.stored(verdict, "20261030")

    assert stored["day"] == "20261030"
    numbers = stored["numbers"]
    assert "note" not in numbers, "建议属于页面，不属于每一条"
    assert numbers["checked"] == verdict["checked"]
    assert numbers["unmatched_count"] == 1
    assert numbers["sign_from_words"] == ["3.04"]


def test_an_entry_without_numbers_still_records_that_it_was_checked():
    """`checked: 0` 与"没有 numbers 键"是两件事：核过了没数 / 从没核过。"""
    stored = journal_check.stored(journal_check.check("今日不追高，等回踩", FACTS), "20261030")

    assert stored["numbers"]["checked"] == 0
    assert stored["numbers"] is not None


def test_a_day_without_facts_is_stored_as_skipped_not_as_passed():
    stored = journal_check.stored(journal_check.check("今天 893 家上涨。", None), "20261030")

    assert stored["numbers"]["skipped"] is True
    assert stored["numbers"]["checked"] == 0


def test_the_cap_is_a_cap_and_the_count_survives_it():
    """20 是列表上限，不是计数上限；截断之后 must 仍能还原总数。"""
    text = "，".join(str(10000 + i) for i in range(30))
    out = journal_check.check(text, FACTS)

    assert len(out["unmatched"]) == 20
    assert out["unmatched_count"] == 30
    assert out["matched"] + out["unmatched_count"] == out["checked"]


def test_a_past_day_is_never_recomputed_into_a_snapshot(tmp_path, monkeypatch):
    """回填只能读盘。账户那一半读的是"现在"，补算会把今天的余额写进那天的文件。"""
    from service.trading import facts as facts_mod

    def explode(*_args, **_kwargs):
        raise AssertionError("回填不该走计算路径")

    monkeypatch.setattr(facts_mod, "build_facts", explode)

    assert facts_mod.facts_for(object(), "20261023", root=tmp_path, compute=False) is None


# ------------------------------------------------------------------ 读回来
def test_a_written_note_comes_back_with_the_verdict_it_earned(client, tmp_path, monkeypatch):
    monkeypatch.setattr("service.api.trading.ROOT", tmp_path)
    monkeypatch.setattr("service.trading.journal_check.market_numbers", lambda desk, day: set())
    day = client.get("/facts", headers=TOKEN).json()["day"]
    (tmp_path / "reports" / "facts").mkdir(parents=True)
    (tmp_path / "reports" / "facts" / f"{day}.json").write_text(
        f'{{"day": "{day}", "breadth": {{"advancers": 893}}}}', encoding="utf-8")

    posted = client.post("/journal", json={"text": "今天 912 家上涨"}, headers=TOKEN).json()
    entry = client.get("/journal", params={"limit": 1}, headers=TOKEN).json()["entries"][0]

    assert entry["id"] == posted["id"]
    assert entry["verified"]["unmatched_count"] == 1
    assert entry["verified"]["unmatched"] == ["912"]
    assert entry["verified"]["checked"] == posted["verification"]["checked"]


def test_the_two_exits_say_the_same_thing_about_the_same_entry(client, desk):
    """一致性审计的断言，在这里先测一遍：同一个 id 的两个出口必须给同一个 verified。"""
    for text in ("今天 912 家上涨", "跌停 137 家"):
        verdict = journal_check.check(text, FACTS)
        desk.paper.add_journal(text, snapshot=journal_check.stored(verdict, "20261030"))

    by_journal = {row["id"]: row for row in
                  client.get("/journal", params={"limit": 10, "recent": 0},
                             headers=TOKEN).json()["entries"]}
    by_search = {row["id"]: row for row in
                 client.get("/memory/search", params={"limit": 10, "recent": 0},
                            headers=TOKEN).json()["journal"]}

    shared = set(by_journal) & set(by_search)
    assert len(shared) == 2
    for row_id in shared:
        assert by_journal[row_id]["verified"] == by_search[row_id]["verified"]
        assert by_journal[row_id]["verified"]["unmatched_count"] == 1


def test_an_entry_written_before_the_check_reads_as_never_checked(client, desk):
    """老条目没有 numbers 键。`null` 说的是"没核过"，不是"核过没问题"。"""
    row_id = desk.paper.add_journal("旧的一条：今天 912 家上涨", snapshot={"day": "20261023"})

    entries = client.get("/journal", params={"limit": 50, "recent": 0},
                         headers=TOKEN).json()
    row = next(item for item in entries["entries"] if item["id"] == row_id)

    assert row["verified"] is None
    assert entries["unchecked"] >= 1, "一页里混着没核过的条目，页级要报数"
    assert "没核过" in entries["unchecked_note"]


def test_a_fully_checked_page_says_nothing_about_unchecked(client, tmp_path, monkeypatch):
    """全核过了就别挂 `unchecked` 键——覆盖数据，不覆盖代码。"""
    monkeypatch.setattr("service.api.trading.ROOT", tmp_path)
    monkeypatch.setattr("service.trading.journal_check.market_numbers", lambda desk, day: set())
    day = client.get("/facts", headers=TOKEN).json()["day"]
    (tmp_path / "reports" / "facts").mkdir(parents=True)
    (tmp_path / "reports" / "facts" / f"{day}.json").write_text(
        f'{{"day": "{day}"}}', encoding="utf-8")

    client.post("/journal", json={"text": "今天 893 家上涨"}, headers=TOKEN)
    body = client.get("/journal", params={"limit": 1}, headers=TOKEN).json()

    assert body["entries"][0]["verified"] is not None
    assert "unchecked" not in body


def test_the_read_path_does_not_ship_the_whole_fix_list(client, tmp_path, monkeypatch):
    """读的人不会去改一条已经落定的复盘；三个 token 够看出味道，计数保留。"""
    monkeypatch.setattr("service.api.trading.ROOT", tmp_path)
    monkeypatch.setattr("service.trading.journal_check.market_numbers", lambda desk, day: set())
    day = client.get("/facts", headers=TOKEN).json()["day"]
    (tmp_path / "reports" / "facts").mkdir(parents=True)
    (tmp_path / "reports" / "facts" / f"{day}.json").write_text(
        f'{{"day": "{day}"}}', encoding="utf-8")

    text = "，".join(str(10000 + i) for i in range(8))
    client.post("/journal", json={"text": text}, headers=TOKEN)
    row = client.get("/journal", params={"limit": 1}, headers=TOKEN).json()["entries"][0]

    assert len(row["verified"]["unmatched"]) == 3
    assert row["verified"]["unmatched_count"] == 8


# ------------------------------------------------- 只能核一半的日子（P86）
def test_market_facts_drops_the_blocks_that_read_today(desk):
    """过去的日子只能补回行情那半边：账户余额是"现在"的，写进那天就是造假。"""
    from service.trading.facts import market_facts

    day = desk.market.latest_bar_date()
    market = market_facts(desk, day)

    assert set(market) == {"day", "readiness", "breadth", "pools"}
    for dropped in ("account", "affordable", "tickets_today"):
        assert dropped not in market


def test_a_past_day_checks_labels_but_never_claims_the_numbers(desk):
    """`verify_labels` 只给标签结论；它不能顺手说"数字也核过了"。"""
    day = desk.market.latest_bar_date()
    out = journal_check.verify_labels(desk, "涨停 49 家，跌停 93 家。", day)

    assert "checked" not in out
    assert out["labelled"] >= 1


def test_the_stored_scope_travels_with_the_verdict():
    stored = journal_check.stored(
        {"claims": journal_check.claims.scan("我买了 3 只。", {"day": "x"})},
        "20261023", scope="market_only")

    assert stored["claims"]["scope"] == "market_only"
    assert stored["claims"]["labelled"] == 0, "「看过了没标签」也要存，不能留空"
    assert "numbers" not in stored


def test_a_label_only_row_still_counts_as_unchecked(client, desk):
    """只核了标签的行，读回来必须仍然算「数字没核」——否则 11 条会假装已核。"""
    verdict = journal_check.claims.scan("涨停 49 家。", {"pools": {"limit_up": 49}})
    desk.paper.add_journal("涨停 49 家。", snapshot=journal_check.stored(
        {"claims": verdict}, "20261023", scope="market_only"))

    body = client.get("/journal", params={"limit": 5, "recent": 0}, headers=TOKEN).json()
    row = body["entries"][0]

    assert row["verified"]["claims"]["scope"] == "market_only"
    assert "checked" not in row["verified"]
    assert body["unchecked"] >= 1
    assert "标签那半核过" in body["unchecked_note"]


def test_a_skipped_row_is_unchecked_not_verified(client, desk):
    """有数字但当天没快照：`checked: 0, skipped` 是"没核成"，不是"核过没问题"。"""
    stored = journal_check.stored(
        journal_check.check("今天 893 家。", None), "20261023")
    desk.paper.add_journal("今天 893 家。", snapshot=stored)

    body = client.get("/journal", params={"limit": 5, "recent": 0}, headers=TOKEN).json()

    assert body["entries"][0]["verified"]["skipped"] is True
    assert body["unchecked"] >= 1
