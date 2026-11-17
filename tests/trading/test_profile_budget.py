"""档案装不下的时候：整块让位，而不是让宿主把字符串剪短。

600519 在 2026-11-01 长到 16,572 B——原因不是代码改了什么，是**一条公告的正文
下载完了**：`excerpt` 从 60 字节的「未下载」变成 320 字节的散文。这个变量是内容，
不是代码，所以削字节的修法永远追不上。宿主超过 16 KB 的处理是逐级 shrink
（字符串 240/120/60、数组 12/6/2），读者拿到的是**每一块都被剪短**的档案。

于是改成按块让位：装不下的块整块不给，列进 `omitted_sections` 并写 `size_note`。
一块缺失是一次 `sections=` 调用就能补回来的；一段被剪到 60 字的正文看起来只是结论短。
"""

from __future__ import annotations

import json

from service.trading import profile

#: 宿主的硬上限。留 236 B 是因为 formatJsonOutput 之后才是真正送出的字节。
HOST_CEILING = 16_000


def _size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def test_no_symbol_ever_produces_a_body_the_host_would_cut(desk):
    """五只不同板块的票，默认档案都必须落在上限之内。"""
    for symbol in ("600519.SH", "300750.SZ", "000001.SZ", "688981.SH", "920592.BJ"):
        out = profile.build(desk, symbol)
        assert out["ok"] is True
        assert _size(out) <= HOST_CEILING, f"{symbol} 冒出 {_size(out)} B"


def test_a_dropped_block_is_named_and_one_call_away(desk):
    out = profile.build(desk, "600519.SH")

    omitted = out.get("omitted_sections") or []
    if not omitted:
        return                      # 装得下就什么都不该说
    assert "sections=" in out["size_note"]
    for name in omitted:
        assert name not in out, "说省了又还在，就是没省"
        alone = profile.build(desk, "600519.SH", sections=[name])
        assert alone["ok"] is True and name in alone


def test_a_block_is_byte_identical_whether_kept_or_requested_alone(desk):
    """让位机制不许改内容：拿回来的那一块，与整套里的一字不差。"""
    full = profile.build(desk, "600519.SH")
    for name in [n for n in profile.BLOCK_ORDER if n in full]:
        alone = profile.build(desk, "600519.SH", sections=[name])
        assert alone[name] == full[name], name


def test_the_cheap_duplicate_goes_before_the_unique_block(desk):
    """`sentiment` 与 quant_pools/quant_lhb 重复，`ownership` 没有第二个出口。

    第一版按"重要性"排，把 2.6 KB 的股东户数/北向/解禁让给了 1.3 KB 的池子与龙虎榜——
    那是把只有内核能给的数字，换成两个工具里都有的数字。
    """
    order = profile.KEEP_PRIORITY
    assert order.index("ownership") < order.index("sentiment")
    assert order.index("ownership") < order.index("peers")
    # 身份、可交易性、仓位是底线：任何情况下都排在最前
    assert set(order[:3]) == {"identity", "tradability", "position"}


def test_the_floor_is_four_blocks_even_when_the_budget_is_absurd(desk, monkeypatch):
    """预算调到 1 字节也要留四块：没有价格没有仓位的"档案"是另一份文档。"""
    monkeypatch.setattr(profile, "BLOCK_BUDGET", 1)

    out = profile.build(desk, "600519.SH")

    assert len([n for n in profile.BLOCK_ORDER if n in out]) == 4


def test_an_explicit_request_for_everything_is_budgeted_too(desk):
    """一次要十三块等于没要——它同样是 16.7 KB，同样会被宿主剪。"""
    out = profile.build(desk, "600519.SH", sections=list(profile.BLOCK_ORDER))

    assert _size(out) <= HOST_CEILING
    assert out.get("omitted_sections"), "全要也装不下，必须让位并说明"
