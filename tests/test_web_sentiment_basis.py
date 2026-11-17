"""同一交易日的两个涨停数，页面上必须有名字。

2026-11-08 实测：总览显示 32 / 14 / 15（东财收盘），盘面显示 33 / 17 / 15
（派生池），而盘面三柱旁边的来源标签写着"东方财富"——标签跟着三池明细的
``source`` 走，数字跟着 ``counts`` 走。读者看到的是同一天两个涨停数，且其
中一个被贴上了另一个来源的名字。

修法分两层：``zh.basis()`` 把 ``break_rate_basis`` 译成中文口径说明；两个
页面各自把口径写在数字旁边，盘面再把东财的对照数摆出来。

这里只钉住"翻译函数真的返回中文"和"两个页面确实调了它"。前者是行为断言
（node 里直接调用），后者是绊线：删掉芯片时它会红，但它不能证明渲染正确。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "web" / "assets" / "js" / "pages"


def test_basis_is_translated_not_passed_through():
    """``vendor`` 不许原样出现在中文界面上。"""
    script = r"""
import { zh } from './web/assets/js/zh.js';
console.log(JSON.stringify({
  vendor: zh.basis('vendor'),
  derived: zh.basis('touched: 盘中触及涨停价即算炸板'),
  missing: zh.basis(null),
}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if result.returncode != 0 and "not found" in result.stderr.lower():
        pytest.skip("node unavailable")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["vendor"] == "东财收盘口径"
    assert out["derived"].startswith("派生池口径")
    assert "vendor" not in out["vendor"] and "touched" not in out["derived"]
    assert out["missing"] == "—"


def test_both_pages_name_the_basis_next_to_the_numbers():
    today = (PAGES / "today.js").read_text(encoding="utf-8")
    market = (PAGES / "market.js").read_text(encoding="utf-8")
    assert "zh.basis(s.break_rate_basis)" in today
    # 盘面：口径标签必须取自 counts（数字的来源），不是三池明细的 vendor
    assert "const basis = counts.source || s.counts_source" in market
    assert "zh.vendor(basis)" in market
    assert "counts.eastmoney" in market, "东财对照数必须摆出来，否则两个数还是打架"


def test_intraday_weakness_is_split_by_recoverability_in_the_production_page():
    today = (PAGES / "today.js").read_text(encoding="utf-8")
    assert "item.components || []" in today
    assert "可回补真实历史" in today
    assert "不可回补，只能逐日冷存" in today
    assert "part.depth" in today and "part.min_history" in today
    assert "part.universe_coverage" in today and "股票覆盖" in today


def test_desktop_modules_follow_the_running_backend_build():
    app = (ROOT / "web" / "assets" / "js" / "app.js").read_text(encoding="utf-8")
    assert "state.health?.build?.code_fingerprint" in app
    assert "?v=${moduleVersion}" in app
    assert "report_error" in app and "技术细节" in app
