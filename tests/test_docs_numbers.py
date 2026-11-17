"""文档里的工具数必须等于代码里的工具数。

``AGENTS.md`` 从 2026-11-02 起就写着"``tests/test_docs_numbers.py`` 会盯着这份
文档里的数字对不对"，而它一直不存在。于是同一个数字长出了四个版本：README 说
50 个工具、``plugin/README.md`` 说 64 个、``lib/index.js`` 的 docstring 说 57
个、runbook 说 37 个——真值 65。写死数字本身没错，**没人复算**才是错。

只盯"现在时"的断言。带日期的历史快照（README 里"2026-10-26 之后 46 个 DSH
工具，390 passed"、AGENTS.md 里"2026-11-02 是 1,938 个"）不进来：把历史改成
今天的数字，比数字过期更糟——它会让"当时是什么样"永久消失。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 每一处"现在时"的工具数声明。句式必须恰好命中一次：命中 0 次说明句子被
#: 改写过了，这条断言就静默地不再核对任何东西——那比数字过期更难发现。
CLAIMS = [
    ("README.md", r"(\d+) 个公开 `quant_\*` 工具只有一份定义"),
    ("README.md", r"DSH 插件（(\d+) 个公开 `quant_\*` 工具"),
    ("plugin/README.md", r"(\d+)\s+public façade tools"),
    ("plugin/README.md", r"The same (\d+) public tools"),
    ("plugin/lib/index.js", r"through (\d+) public tools"),
    ("plugin/mcp/package.json", r"the same (\d+) public tools"),
    ("docs/03-runbook.md", r"serves only the (\d+) public tools"),
]

INTERNAL_CLAIMS = [
    ("README.md", r"内部 handler (\d+) 个"),
    ("plugin/README.md", r"defines (\d+) internal handlers"),
    ("plugin/lib/index.js", r"\* (\d+) internal handlers"),
    ("plugin/mcp/package.json", r"(\d+) internal handlers"),
]


def tool_names() -> set[str]:
    """Every ``quant_*`` tool the plugin actually defines.

    Taken from the ``define(`` call, not from "any quoted ``quant_*`` in the
    file": a description that names a tool which does not exist used to inflate
    the count, which is the one direction this file must never fail in. The
    definition goes through a factory, so counting call sites gives 1 — the
    names are what the hosts see, and what ``tools.test.mjs`` asserts against.
    """
    source = (ROOT / "plugin/lib/index.js").read_text(encoding="utf-8")
    return set(re.findall(r"define\(\s*'(quant_[a-z0-9_]+)'", source))


def public_tool_names() -> set[str]:
    """The four names in PUBLIC_TOOL_NAMES, not the internal compatibility table."""
    source = (ROOT / "plugin/lib/facade.js").read_text(encoding="utf-8")
    block = re.search(r"PUBLIC_TOOL_NAMES\s*=\s*Object\.freeze\(\[([\s\S]*?)\]\)", source)
    assert block, "找不到 PUBLIC_TOOL_NAMES"
    return set(re.findall(r"'(quant_[a-z0-9_]+)'", block.group(1)))


def test_the_tool_list_can_be_counted_at_all():
    """先证明"数得出来"，再谈"数得对不对"。

    换一次定义格式，正则就会数出 0 个——那时下面每条断言都会以
    "0 != 65"的形式变红，看着像文档写错了，实际是本文件失去了取数能力。
    """
    names = tool_names()
    assert len(names) >= 60, f"只数出 {len(names)} 个内部工具名，正则可能已失效"
    assert all(name.startswith("quant_") for name in names)
    public = public_tool_names()
    assert public == {"quant_query", "quant_order", "quant_workflow", "quant_alert"}


def test_no_description_names_a_tool_that_does_not_exist():
    """描述里点名的 ``quant_*`` 必须真的能调。

    ``quant_dashboard`` 的描述一度写着"今天的 API 计数在 ``quant_usage``、
    可选数据集明细在 ``quant_data_brief``"——两个工具都不存在。模型照着描述
    去调，得到的是 unknown tool；更糟的是它会把"没有这个数"记成人没问。
    描述是工具契约的一部分，指向不存在的名字和写错参数一样是缺陷。
    """
    source = (ROOT / "plugin/lib/index.js").read_text(encoding="utf-8")
    defined = tool_names()
    mentioned = set(re.findall(r"[`'\"](quant_[a-z0-9_]+)[`'\"]", source))
    assert mentioned >= defined, "取数正则认不出已定义的工具，先修它"
    ghosts = sorted(mentioned - defined)
    assert not ghosts, f"描述里引用但从未定义的工具：{ghosts}"


@pytest.mark.parametrize(("path", "pattern"), CLAIMS)
def test_documented_tool_count_matches_the_code(path, pattern):
    text = (ROOT / path).read_text(encoding="utf-8")
    hits = re.findall(pattern, text)
    assert len(hits) == 1, (
        f"{path} 里这个句式命中 {len(hits)} 次，不是 1 次：{pattern}\n"
        "句子改写后这条断言就不再核对任何数字——请同步更新本文件的 CLAIMS。"
    )
    actual = len(public_tool_names())
    assert int(hits[0]) == actual, (
        f"{path} 写 {hits[0]} 个公开工具，代码里是 {actual} 个（{pattern}）"
    )


@pytest.mark.parametrize(("path", "pattern"), INTERNAL_CLAIMS)
def test_documented_internal_handler_count_matches_the_code(path, pattern):
    text = (ROOT / path).read_text(encoding="utf-8")
    hits = re.findall(pattern, text)
    assert len(hits) == 1, f"{path} 内部 handler 句式命中 {len(hits)} 次：{pattern}"
    actual = len(tool_names())
    assert int(hits[0]) == actual, f"{path} 写 {hits[0]} 个内部 handler，代码里是 {actual} 个"


def test_agents_md_promise_names_this_file():
    """承诺与实现在同一个名字上。删掉本文件时，这句话也要一起改。"""
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "tests/test_docs_numbers.py" in text
    assert Path(__file__).name == "test_docs_numbers.py"
