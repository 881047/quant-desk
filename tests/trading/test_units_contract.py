"""第六轮：数值量纲契约的「单点 + 生成物 + 逐端点」回归。

第五轮复查发现两个真问题：后端把权威声明加进 ``/meta/vocabulary``，
但 web 与 plugin 一个都没读；各模块还各自手写着互相矛盾的中文口径。
这里把修法钉住：

* Python 侧只有 ``service/vocabulary.py`` 一处手写；
* web/plugin 的 ``contract.units.js`` 是确定性生成物，过期即测试红；
* 词表的键是从响应根出发的完整路径，逐端点**精确定位**，不递归猜；
* 插件提示词由生成文本插值，不再手抄规则；
* 前端所有歧义字段走 ``fmtByUnits()``，由 units 决定要不要 ×100。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app
from service import vocabulary as vocab

TOKEN = {"X-Quant-Token": "change-me-local-only"}
ROOT = Path(__file__).resolve().parents[2]
WEB_JS = ROOT / "web/assets/js"
PLUGIN_LIB = ROOT / "plugin/lib"
GENERATED = (WEB_JS / "contract.units.js", PLUGIN_LIB / "contract.units.js")


@pytest.fixture
def client(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    with TestClient(app) as c:
        yield c


def _parse_generated(path: Path):
    text = path.read_text(encoding="utf-8")
    block = text.split("export const UNITS = ", 1)[1].split(
        ";\n\nexport const UNITS_TEXT = ", 1)[0]
    tail = text.split("export const UNITS_TEXT = ", 1)[1].split(
        ";\n\nexport function unitsFor", 1)[0]
    return text, json.loads(block), json.loads(tail)


def test_generated_contract_is_fresh():
    """改了词表却没重跑 --write，这里直接红。"""
    result = subprocess.run(
        [sys.executable, "scripts/export_units.py", "--check"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_contract_is_identical_and_matches_python():
    web_text, web_units, web_text_value = _parse_generated(GENERATED[0])
    plugin_text, plugin_units, plugin_text_value = _parse_generated(GENERATED[1])
    assert web_text == plugin_text, "web 与 plugin 不许各存一份会分叉的常量"
    assert web_units == plugin_units
    assert web_units["by_endpoint"] == vocab.UNIT_CONVENTIONS["by_endpoint"]
    assert web_units["naming_exceptions"] == vocab.UNIT_CONVENTIONS["naming_exceptions"]
    assert web_text_value == vocab.units_text() == plugin_text_value
    assert web_units["units_text"] == vocab.units_text()
    assert "daily_pnl_pct" in web_text_value, "命名例外必须出现在给模型的说明里"


_MISSING = object()
_EMPTY = object()


def _locate(body, path: str):
    """按词表的完整路径精确定位；``[]`` 表示进入数组首行。"""
    node = body
    for part in path.split("."):
        is_list = part.endswith("[]")
        name = part[:-2] if is_list else part
        if not isinstance(node, dict) or name not in node:
            return _MISSING
        node = node[name]
        if is_list:
            if not isinstance(node, list):
                return _MISSING
            if not node:
                return _EMPTY  # 当日没有行，不是契约错误
            node = node[0]
    return node


def test_every_declared_field_exists_at_its_declared_path(client):
    """逐端点验：不许把全部响应的键并成一个"宇宙"来互相顶账。"""
    bodies = {}
    checked = 0
    for endpoint, fields in vocab.UNIT_CONVENTIONS["by_endpoint"].items():
        real = "/market/pools" if endpoint == "/market/pools.sentiment" else endpoint
        if real not in bodies:
            response = client.get(real, headers=TOKEN)
            assert response.status_code == 200, (real, response.text[:120])
            bodies[real] = response.json()
        for path in fields:
            assert _locate(bodies[real], path) is not _MISSING, (endpoint, path)
            checked += 1
    assert checked == sum(len(f) for f in vocab.UNIT_CONVENTIONS["by_endpoint"].values())


def test_no_module_hand_writes_a_second_units_explanation():
    """口径文案只能引用词表；散在各模块的中文句子会与例外分叉。"""
    for rel in ("service/trading/facts.py", "service/trading/frame.py",
                "service/trading/intraday_buckets.py",
                "service/research/discover.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "vocab.note(" in src, rel
        assert "returns 是小数" not in src, rel
        assert "百分数（pct）；interval" not in src, rel
    api_src = (ROOT / "service/api/trading.py").read_text(encoding="utf-8")
    assert 'vocab.note("pools")' in api_src
    assert "百分数字段一律以" not in api_src


def test_plugin_prompt_is_generated_from_the_contract():
    src = (PLUGIN_LIB / "index.js").read_text(encoding="utf-8")
    assert "import { UNITS_TEXT } from './contract.units.js'" in src
    assert "${UNITS_TEXT}" in src, "提示词必须插值生成文本，不能手抄规则"
    for stale in (
        "profile 里带 _pct 的是百分数",
        "facts / discover / industries 是小数",
        "收益率有两种写法",
    ):
        assert stale not in src, stale
    # 生成物里的说明确实提到了两个最容易出错的命名例外
    _, _, text = _parse_generated(GENERATED[1])
    assert "daily_pnl_pct" in text and "day_pnl_pct" in text


def test_frontend_only_formats_fields_declared_in_the_contract():
    """前端读的字段名必须都在词表里，否则 fmtByUnits 会显示破折号。"""
    sources = [p.read_text(encoding="utf-8")
               for p in sorted(WEB_JS.rglob("*.js"))]
    used = set()
    for src in sources:
        used |= set(re.findall(r"fmtByUnits\([^,]+,\s*'([^']+)'", src))
    declared = set().union(*(vocab.units_for(ep) for ep in
                             vocab.UNIT_CONVENTIONS["by_endpoint"]))
    assert used and used <= declared, sorted(used - declared)
    assert {"daily_pnl_pct", "break_rate", "pct_chg"} <= used


def test_node_formatter_displays_the_same_numbers_as_before():
    """跨语言一致性：Node 侧按 units 渲染的结果必须与旧函数逐字相同。"""
    script = r"""
import { fmtByUnits } from './web/assets/js/fmt.js';
import { unitsFor } from './web/assets/js/contract.units.js';
const cases = [
  ['/market/pools', 'seal_ratio_pct', 5.11, 3, false, '5.110%'],
  ['/market/pools', 'pct_of_equity', 10.0, 1, true, '+10.0%'],
  ['/market/pools', 'break_rate', 0.304, 2, false, '30.40%'],
  ['/market/sector', 'pct_chg', 1.4, 2, true, '+1.40%'],
  ['/facts', 'equal_weight_return', -0.018629, 2, true, '−1.86%'],
  ['/regime', 'vol_percentile_1y', 0.92, 0, false, '92%'],
  ['/portfolio', 'daily_pnl_pct', 0.0, 2, true, '0.00%'],
];
const out = {};
for (const [ep, field, value, digits, signed, want] of cases) {
  out[`${ep}:${field}`] = [fmtByUnits(unitsFor(ep), field, value, digits, { signed }), want];
}
out.undeclared = [fmtByUnits({}, 'unknown_field', 1.4, 2), '—'];
console.log(JSON.stringify(out));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if result.returncode != 0 and "not found" in result.stderr.lower():
        pytest.skip("node unavailable")
    assert result.returncode == 0, result.stderr
    for key, (got, want) in json.loads(result.stdout).items():
        assert got == want, f"{key}: {got} != {want}"
