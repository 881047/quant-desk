"""11 份自动化提示词必须和内核说同一件事。

这些文件是模型的作业指导书，在宿主里没有版本历史（整段覆盖、不可回滚），
所以正本放在 `docs/automations/`。它们最容易坏的两种方式是：

* **点名了一个不存在的字段**（`current.sleeve_state`、`data.core_ok`、
  `quant_facts.recent` 都真的发生过）——这类错误不会报错，只会让模型读到
  None 并据此做出相反的决定；
* **把内核里的数字抄了一份**（仓位上限、样本目标、流动性门槛）——改了配置
  而不改提示词，提示词就开始说谎。

字段路径由 `scripts/audit_prompts.py --live` 对着真实响应核对；这里钉住的是
不连服务也能判的部分：文件齐全、时段与文件名一致、数字与配置一致。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "docs" / "automations"

sys.path.insert(0, str(ROOT / "scripts"))

from service import settings


def _body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path.name} 少了 front-matter"
    return text.split("---", 2)[2]


def _all() -> dict[str, str]:
    return {p.name: _body(p) for p in sorted(PROMPTS.glob("*.md"))}


def _section(text: str, title: str) -> str:
    match = re.search(rf"^## {title}\n(.*?)(?=^## |\Z)", text, re.DOTALL | re.MULTILINE)
    assert match, f"找不到「{title}」这一节"
    return match.group(1)


def test_every_window_has_one_file_and_a_schedule_that_matches_its_name():
    """文件名前缀是时段，front-matter 的 rrule 必须说同一件事。

    十一份提示词是互相抄出来的，改时间只改了其中一处的事故（09:05 的规则
    抄进 09:26）靠读是看不出来的，靠算可以。
    """
    bodies = _all()
    assert len(bodies) == 11, sorted(bodies)

    for name in bodies:
        text = (PROMPTS / name).read_text(encoding="utf-8")
        hour = int(re.search(r"^rrule:.*BYHOUR=(\d+)", text, re.MULTILINE).group(1))
        minute = int(re.search(r"^rrule:.*BYMINUTE=(\d+)", text, re.MULTILINE).group(1))
        assert f"{hour:02d}{minute:02d}" == name[:4], (
            f"{name} 的文件名前缀与 rrule {hour:02d}:{minute:02d} 不一致")
        # 正文要么说出这个时刻，要么明确「时刻由 session.now 给」。v2 起后者是常态：
        # 8/9/10/14 点档有约 20 分钟抖动，写死分钟反而是错的信息。
        assert (f"{hour:02d}:{minute:02d}" in bodies[name]
                or f"{hour}:{minute:02d}" in bodies[name]
                or "session.now" in bodies[name]), name


def test_the_position_caps_are_read_from_config_not_copied_into_prompts():
    """v2 起提示词不再抄「30% / 40% / 50%」，而是指定从 `risk.limits` 读。

    抄一份的下场已经量过：2026-11-05/06 用 CORE 的 30% 算价格上限，把可买池
    缩小约 40%。所以这里钉两件事：指向正确字段，且旧字面量不许回来。
    """
    caps = ("max_core_single", "max_core_etf_single", "max_aggressive_single")
    for name in ("0926-open-decision.md", "1430-close-entry.md"):
        body = _all()[name]
        assert "risk.limits" in body, name
        for key in caps:
            assert key in body, (name, key)
        assert "risk.limits.max_positions" in body, name
        assert "股票 100 股 / 30%" not in body, f"{name} 又抄了一份旧上限"


def test_the_liquidity_floor_matches_the_screen_config():
    """5000 万是 `screen.min_adv_cny`，不是拍出来的。"""
    floor = settings.num("screen.min_adv_cny")
    assert floor == 50_000_000, "配置变了，提示词里的「5000 万」也要改"
    assert "20 日均额 ≥ 5000 万" in _all()["0935-samples.md"]


def test_both_ordering_windows_describe_the_same_three_steps():
    """下单是三步，不是「两步」。

    两份提示词都写过「下单是两步，不能省」然后列出三个调用——数一遍就知道
    对不上。v2 统一成「三步」，这里钉住调用序列与词本身，防止只改一份。
    """
    body = _all()
    for name in ("0926-open-decision.md", "1430-close-entry.md"):
        assert "三步" in body[name], name
        assert "两步" not in body[name], f"{name} 又写回「两步」了"
        for call in ('action="propose"', 'resource="ticket"', 'action="approve"'):
            assert call in body[name], (name, call)


def test_no_prompt_promises_a_field_name_that_the_kernel_does_not_own():
    """静态半边：每条反引号路径都必须在 `audit_prompts` 的映射表里有归属。

    连服务的逐条解析是 `--live`；这里只保证"没有被漏登记的路径"——否则
    新写一条路径就等于绕过检查。
    """
    import audit_prompts

    assert audit_prompts.main([]) == 0, "有路径没登记归属，或解析器退化"


@pytest.mark.parametrize("name", ["0905-persona-calibration.md", "1930-reconcile.md"])
def test_the_holiday_gate_reads_a_field_that_exists(name):
    """开盘前的日历判断只能来自 `session.market_open_today`。

    两份提示词都用它决定"今天要不要干活"。它一旦改名（或写成 `data.*`），
    节假日就会照常跑完一整轮——不报错，只是把休市写成数据故障。
    """
    assert "market_open_today=false" in _all()[name]


def test_a_bare_field_in_a_list_is_checked_against_its_anchor(tmp_path, monkeypatch):
    """`opened_intraday` 是 2026-11-13 唯一漏过去的字段：它不带点。

    `PATH_RE` 只认 `a.b`，所以「`quant_facts.affordable` 的 `in_cap` /
    `opened_intraday` / `buyable_at_close`」里只有第一个前缀被检查过。旧名字在
    8ddff76 就拆成了 `one_word` + `board_opens.opened`，模型读到 None 后会拿
    `in_cap` 顶替第三个口径——正是这类"读不到也不报错"的错误最贵。
    """
    import audit_prompts

    (tmp_path / "0926-open-decision.md").write_text(
        "---\nid: x\n---\n"
        "不许互相替代：`quant_facts.affordable` 的 "
        "`in_cap` / `opened_intraday` / `buyable_at_close`；\n",
        encoding="utf-8")
    monkeypatch.setattr(audit_prompts, "PROMPTS", tmp_path)

    found = audit_prompts.bare_fields()
    assert ("quant_facts.affordable", "opened_intraday") in found
    assert ("quant_facts.affordable", "in_cap") in found

    endpoint, key = audit_prompts.field_key("quant_facts.affordable", "opened_intraday")
    assert (endpoint, key) == ("/facts", "affordable.opened_intraday")
    payload = {"affordable": {"in_cap": 25, "buyable_at_close": 0}}
    with pytest.raises(KeyError):
        audit_prompts.resolve(payload, key)


def test_a_list_wrapped_onto_the_next_line_still_finds_its_anchor(tmp_path, monkeypatch):
    """提示词是硬折行的：锚点在上一行末尾、字段在下一行行首，是常态。

    1430 的档位三值就是这样写的（`quant_limit_structure` 的 / 换行 /
    `counts.limit_up` / `seal_rate` / `max_streak`）。只在同一行里找锚点，
    这一整类都会被当成"没有锚点"跳过——看起来查过了，其实一个都没查。
    """
    import audit_prompts

    (tmp_path / "1430-close-entry.md").write_text(
        "---\nid: x\n---\n"
        "8. **档位在本窗口判**：当日三值读 `quant_limit_structure` 的\n"
        "   `counts.limit_up` / `seal_rate` / `max_streak`；三项任一为 null 就写 undetermined。\n",
        encoding="utf-8")
    monkeypatch.setattr(audit_prompts, "PROMPTS", tmp_path)

    found = audit_prompts.bare_fields()
    assert ("quant_limit_structure", "seal_rate") in found
    assert ("quant_limit_structure", "max_streak") in found

    endpoint, key = audit_prompts.field_key("quant_limit_structure", "seal_rate")
    assert (endpoint, key) == ("/knowledge/limit_structure", "seal_rate")


def test_a_prompt_naming_a_tool_that_does_not_exist_is_caught(tmp_path, monkeypatch):
    """工具名写错时，`ToolSearch` 直接载不到，模型只能改用手搓 HTTP 或记忆里的数。

    `quant_information` 之前既没进审计的映射表、也没进过任何检查——它这次是
    在补裸字段时顺带发现的。工具名按插件源码 `define('quant_x', …)` 取证。
    """
    import audit_prompts

    (tmp_path / "0926-open-decision.md").write_text(
        "---\nid: x\n---\n3. `quant_teleport` 先读，再 `quant_facts`。\n",
        encoding="utf-8")
    monkeypatch.setattr(audit_prompts, "PROMPTS", tmp_path)

    ghosts = audit_prompts.ghost_tools()
    assert ghosts == {"quant_teleport": {"0926-open-decision.md"}}
    assert "quant_facts" not in ghosts


def test_a_list_without_an_anchor_is_not_guessed(tmp_path, monkeypatch):
    """没有锚点的裸名可以属于任何一个端点，不猜。

    猜一个去查，会把"查不到"写成"提示词写错了"；下一次真写错时反而没人信。
    这类列表（`max_streak`、`tiers`、`promotion`）只做人工核对。
    """
    import audit_prompts

    (tmp_path / "1850-market-review.md").write_text(
        "---\nid: x\n---\n"
        "   不是当日派生值；炸板率、`max_streak`、`tiers`、`promotion`（分母为 0 写 null）。\n",
        encoding="utf-8")
    monkeypatch.setattr(audit_prompts, "PROMPTS", tmp_path)

    assert audit_prompts.bare_fields() == {}


def test_a_wrapped_value_list_is_not_read_as_fields(tmp_path, monkeypatch):
    """`warning`：换行后是 `pre_open` / `opening_window` / `missing` 三个取值。

    跨行找锚点如果连冒号也认，就会把这三个枚举值当成 `warning` 的字段去查，
    凭空造出三条悬空——然后真悬空的那天，没人再信这份报告。
    """
    import audit_prompts

    (tmp_path / "0905-persona-calibration.md").write_text(
        "---\nid: x\n---\n"
        "- `quant_pools` 先看 `pool_state.phase` 与它自带的 `warning`：\n"
        "  `pre_open` / `opening_window` / `missing` 三档的 `counts` 都不是终值，\n",
        encoding="utf-8")
    monkeypatch.setattr(audit_prompts, "PROMPTS", tmp_path)

    assert audit_prompts.bare_fields() == {}


def test_a_list_broken_across_lines_keeps_going(tmp_path, monkeypatch):
    """硬折行会把列表断在任意一个 `/` 后面：`promotion` / 换行 / `first_seal`。

    逐行扫描时，断行之后的字段会被当成"没有锚点的列表"跳过——看起来查过了，
    其实只查了前半截。1850 的七个结构字段正是这样，前三个进了审计、后四个没有。
    """
    import audit_prompts

    (tmp_path / "1850-market-review.md").write_text(
        "---\nid: x\n---\n"
        "3. 当日结构读 `quant_limit_structure` 的 `max_streak` / `tiers` /\n"
        "   `first_seal` / `trend`；\n",
        encoding="utf-8")
    monkeypatch.setattr(audit_prompts, "PROMPTS", tmp_path)

    found = audit_prompts.bare_fields()
    assert ("quant_limit_structure", "max_streak") in found
    assert ("quant_limit_structure", "first_seal") in found, "换行后的字段被丢了"
    assert ("quant_limit_structure", "trend") in found


def test_no_prompt_still_points_at_the_retired_window_times():
    """收盘下单 14:35 → 14:30 前移后，两处提示词还留着旧时刻。

    1335 写「14:35 那次自己判」，1525 写「17:35 收盘摘要之前」——后者那个任务
    根本不存在（日更在 17:30）。模型读到的窗口名与实际排程对不上时，会去解释
    一个不存在的窗口，或者等一个不会来的时刻。
    """
    bodies = _all()
    assert "14:35" not in bodies["1335-noon-check.md"], "收盘窗口已前移到 14:30"
    assert "14:30" in bodies["1335-noon-check.md"]
    assert "17:35" not in bodies["1525-alert-handoff.md"], "17:35 没有这个任务"
    assert "17:30" in bodies["1525-alert-handoff.md"]


def test_the_active_playbook_names_the_window_the_scheduler_runs():
    """14:30 的窗口读到写着「14:35 的开仓」的规则书，会以为开仓在另一个窗口。

    提示词把优先级写成「playbook > 本文件」，所以规则书的窗口名必须和排程一致；
    v3 在尾盘前移后还留着四处 14:35（§7 人格、§7.5 报价、§9.2 开仓），
    2026-11-14 统一成 14:30 并在 meta 里记了 amend_note。
    """
    import json

    meta = json.loads((ROOT / "playbook" / "meta.json").read_text(encoding="utf-8"))
    entry = next(v for v in meta["versions"] if v["version"] == meta["active"])
    text = (ROOT / "playbook" / entry["file"]).read_text(encoding="utf-8")

    assert "14:35" not in text, f"{entry['file']} 仍把收盘窗口叫 14:35"
    assert "14:30" in text


def test_short_alpha_paper_tasks_keep_model_account_and_ticket_boundaries():
    bodies = _all()
    for name in ("0924-short-alpha-entry.md", "1428-short-alpha-exit.md",
                 "1910-short-alpha-judgment.md"):
        body = bodies[name]
        assert "DeepSeek-V4.1-Flash" in body
        assert "data/external.sqlite" in body
        for call in ('action="propose"', 'resource="ticket"', 'action="approve"'):
            assert call in body, (name, call)
        assert "strategy=short_alpha" in body
    assert "short-alpha:{signal_day}:entry:{symbol}" in bodies["0924-short-alpha-entry.md"]
    assert "target_weight=原冻结值" in bodies["0924-short-alpha-entry.md"]
    assert "thesis=原冻结thesis" in bodies["0924-short-alpha-entry.md"]
    assert "禁止下单、禁止修改模拟或实盘账本" not in bodies["1910-short-alpha-judgment.md"]
    assert "最迟 T+2" in bodies["1428-short-alpha-exit.md"]


def test_existing_account_tasks_do_not_double_manage_short_alpha():
    for name in ("0926-open-decision.md", "1335-noon-check.md", "1430-close-entry.md"):
        body = _all()[name]
        assert "短线α来源隔离（2026-11-15）" in body
        assert 'resource="short_alpha_execution"' in body
        assert "不重复管理" in body
        assert "硬止损" in body
