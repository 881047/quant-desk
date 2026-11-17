"""P3 memory: searchable journal, case library, hypotheses, versioned playbook."""

from __future__ import annotations

import pytest

from service.trading import playbook as pb
from tests.trading.helpers import pick_symbols, proposal


def test_journal_search_finds_chinese_substring(desk):
    desk.paper.add_journal("今天没有追高，等回踩十日线再考虑", tags="discipline")
    hits = desk.paper.search_journal("回踩十日线")
    assert hits and "回踩十日线" in hits[0]["text"]


def test_memory_search_returns_journal_and_cases(desk):
    desk.paper.add_journal("游资仓只在情绪修复时做", tags="aggressive")
    result = desk.paper.search_journal("情绪修复")
    assert result
    # no cases yet
    assert desk.paper.list_cases(limit=5) == []


def test_case_is_recorded_after_a_round_trip(desk):
    symbol = pick_symbols(desk, 1)[0]
    buy = desk.decisions.propose(proposal(symbol))
    assert buy["status"] == "proposed"
    assert desk.decisions.approve(buy["ticket_id"])["status"] == "filled"

    # Simulate the next trading day: shares become sellable.
    pos = desk.book.ledger.account.position(symbol)
    pos.available_qty = pos.total_qty
    pos.locked_qty = 0
    sell = desk.decisions.propose(proposal(symbol, side="SELL"))
    assert sell["status"] == "proposed", sell.get("reasons")
    assert desk.decisions.approve(sell["ticket_id"])["status"] == "filled"

    cases = desk.paper.list_cases(limit=5)
    assert len(cases) == 1
    case = cases[0]
    assert case["symbol"] == symbol
    assert case["qty"] == buy["qty"]
    assert case["pnl"] < 0  # two commissions + stamp duty at the same price
    assert case["thesis"]


def test_hypothesis_lifecycle(desk):
    hyp_id = desk.paper.add_hypothesis(
        "首板次日开盘买入的日内收益为负",
        "2026 年样本已显示负期望",
        "screen: first_board, hold 1d",
    )
    hyp = desk.paper.get_hypothesis(hyp_id)
    assert hyp["status"] == "draft"

    updated = desk.paper.update_hypothesis(
        hyp_id, "testing", {"metrics": {"mean_ret": -0.0014}}
    )
    assert updated["status"] == "testing"

    promoted = desk.paper.update_hypothesis(
        hyp_id, "promoted", {"metrics": {"mean_ret": -0.0014}, "notes": "confirmed"}
    )
    assert promoted["status"] == "promoted"
    assert promoted["result"]["metrics"]["mean_ret"] == -0.0014


def test_hypothesis_rejects_unknown_status(desk):
    hyp_id = desk.paper.add_hypothesis("x" * 10)
    with pytest.raises(ValueError):
        desk.paper.update_hypothesis(hyp_id, "maybe")


def test_playbook_versioning(tmp_path):
    folder = tmp_path / "playbook"
    folder.mkdir()
    (folder / "v1.md").write_text("# v1\n" + "旧规则。" * 20, encoding="utf-8")

    current = pb.current(tmp_path)
    assert current["version"] == "v1"

    draft = pb.propose(tmp_path, "# v2\n" + "新规则：单笔风险降到 3%。" * 5, note="tighten")
    assert draft["draft_id"].startswith("pb-")
    assert pb.current(tmp_path)["version"] == "v1"  # drafts are not live

    with pytest.raises(pb.PlaybookError):
        pb.activate(tmp_path, draft["draft_id"], challenge="ACTIVATE")

    issued = pb.issue_challenge(tmp_path, draft["draft_id"])
    entry = pb.activate(tmp_path, draft["draft_id"], challenge=issued["challenge"])
    assert entry["version"] == "v2"
    assert pb.current(tmp_path)["version"] == "v2"
    assert "单笔风险降到 3%" in pb.current(tmp_path)["text"]
    assert len(pb.history(tmp_path)["versions"]) == 2

    with pytest.raises(pb.PlaybookError, match="unknown|already"):
        pb.activate(tmp_path, draft["draft_id"], challenge=issued["challenge"])


def test_playbook_refuses_a_patch_draft(tmp_path):
    """只写新增条款的草案不能激活——它会把规则书覆盖成碎片。

    2026-11-09/10 两份待激活草案就是这种形状（4.4 KB / 3.7 KB，各含一章），
    而现行 v2 是 12.9 KB、十章。若当时点了激活，§0 账户事实、§2 仓位上限、
    §8 跳空门、§9.3 赔率门会一起消失，之后每个窗口都在引用不存在的条款。
    """
    folder = tmp_path / "playbook"
    folder.mkdir()
    body = "# 规则\n\n" + "\n\n".join(f"## {i}. 第 {i} 章\n内容。" for i in range(4))
    (folder / "v1.md").write_text(body, encoding="utf-8")

    patch = pb.propose(
        tmp_path, "# 草案\n## 9.7 新条款\n" + "只改一处，不放宽任何硬门。" * 4, note="patch"
    )
    assert patch["review"]["is_fragment"] is True
    assert patch["review"]["missing_sections"] == ["0", "1", "2", "3"]

    issued = pb.issue_challenge(tmp_path, patch["draft_id"])
    with pytest.raises(pb.PlaybookError, match="补丁"):
        pb.activate(tmp_path, patch["draft_id"], challenge=issued["challenge"])
    # 拒绝激活不能顺手把版本改掉，也不能烧掉那次挑战——再试一次仍报「补丁」，
    # 而不是「挑战无效」。
    assert pb.current(tmp_path)["version"] == "v1"
    with pytest.raises(pb.PlaybookError, match="补丁"):
        pb.activate(tmp_path, patch["draft_id"], challenge=issued["challenge"])

    full = pb.propose(tmp_path, body + "\n\n## 9.7 新条款\n只改一处。", note="full")
    assert full["review"]["is_fragment"] is False
    issued = pb.issue_challenge(tmp_path, full["draft_id"])
    entry = pb.activate(tmp_path, full["draft_id"], challenge=issued["challenge"])
    assert entry["version"] == "v2"
    assert "## 3. 第 3 章" in pb.current(tmp_path)["text"]


def test_playbook_sections_are_indexed_and_sliced():
    """正文按章切，不按字符截。

    这条守的是 2026-11-13 查出来的传输事故：`/playbook` 整体 16,301 B，
    超过宿主 16 KB 上限后被 shrink ladder 压到 3,658 B，`text` 只剩 241 字，
    而 `truncated` 仍是 false。切成整章之后，少给的部分一定会被 `next_section`
    说出来——半条规则比没有规则更危险，因为它会被照着执行。
    """
    text = (
        "# 规则\n开头。\n\n"
        "## 0. 账户\n" + "甲" * 100 + "\n\n"
        "## 1. 仓位\n" + "乙" * 100 + "\n\n"
        "## 9. 门\n" + "丙" * 100
    )
    sections = pb.index_sections(text)
    assert [row["n"] for row in sections] == ["pre", "0", "1", "9"]

    whole = pb.select_sections(sections)
    assert whole["text"] == text.strip()
    assert whole["next_section"] is None

    cut = pb.select_sections(sections, budget_bytes=len("甲".encode()) * 100 + 60)
    assert cut["included"] == ["pre", "0"]
    assert cut["next_section"] == "1"

    only = pb.select_sections(sections, names={"9"})
    assert "## 9. 门" in only["text"]
    assert "## 0." not in only["text"]


def test_playbook_challenge_is_bound_to_one_draft(tmp_path):
    first = pb.propose(tmp_path, "# first\n" + "第一版规则。" * 10)
    second = pb.propose(tmp_path, "# second\n" + "第二版规则。" * 10)
    issued = pb.issue_challenge(tmp_path, first["draft_id"])

    with pytest.raises(pb.PlaybookError):
        pb.activate(tmp_path, second["draft_id"], challenge=issued["challenge"])

    assert pb.current(tmp_path)["version"] is None
    assert {row["draft_id"] for row in pb.history(tmp_path)["drafts"]} == {
        first["draft_id"], second["draft_id"],
    }


def test_playbook_rejects_too_short_text(tmp_path):
    (tmp_path / "playbook").mkdir()
    with pytest.raises(pb.PlaybookError):
        pb.propose(tmp_path, "太短")


def test_supersede_clears_a_draft_whose_text_landed_in_a_later_version(tmp_path):
    """草案是累积快照：激活最新一份后，旧几份的内容已生效却仍在待批队列。

    `/playbook` 的 `draft_count` 长期 >0 会让人以为「还有 2 份等着批」，而批了
    不会改变任何条款——这是「草案根本看不见」的镜像错误。supersede 是显式声明：
    文件留着当证据，元数据记下凭哪个版本吸收、凭什么这么判断。
    """
    folder = tmp_path / "playbook"
    folder.mkdir()
    (folder / "v1.md").write_text("# v1\n" + "旧规则。" * 20, encoding="utf-8")
    first = pb.propose(tmp_path, "# a\n" + "含 §7。" * 20, note="§7")
    second = pb.propose(tmp_path, "# b\n" + "含 §7 与 §8。" * 20, note="§8")
    issued = pb.issue_challenge(tmp_path, second["draft_id"])
    pb.activate(tmp_path, second["draft_id"], challenge=issued["challenge"])

    assert [d["draft_id"] for d in pb.history(tmp_path)["drafts"]] == [first["draft_id"]]

    with pytest.raises(pb.PlaybookError, match="unknown version"):
        pb.supersede(tmp_path, first["draft_id"], "v9")

    record = pb.supersede(tmp_path, first["draft_id"], "v2", note="11-06 草案写明纳入 §7")
    assert record["by_version"] == "v2"
    assert record["supersede_note"] == "11-06 草案写明纳入 §7"
    assert pb.history(tmp_path)["drafts"] == []
    assert (folder / first["file"]).exists(), "被吸收的草案文件是证据，不删"

    with pytest.raises(pb.PlaybookError, match="unknown draft"):
        pb.supersede(tmp_path, first["draft_id"], "v2")
