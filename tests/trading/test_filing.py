"""读一份公告正文：分段、按字节收口、并且说清楚它看到的是多少。

这个模块存在的理由是三条互相矛盾的要求：正文必须能读到、单次响应不能超过 16 KB、
而 14,000 字的正文装不进去。分页解决容量，句读收口解决可读性，`coverage` 解决诚实——
一条只抽取了 12 页的年报，不能读成\"年报里没有这一项\"。
"""

from __future__ import annotations

import pytest

from service.trading import filing


class FakeMarket:
    """只提供 `filing.read` 真正用到的那三样东西。"""

    def __init__(self, texts: dict[str, dict], rows: dict[str, dict] | None = None):
        self._texts = texts
        self._rows = rows or {}
        self.store = type("Store", (), {"root": "/tmp/nonexistent-root"})()

    def announcement(self, announcement_id: str) -> dict | None:
        return self._rows.get(announcement_id)

    def announcement_text(self, announcement_id: str) -> dict | None:
        return self._texts.get(announcement_id)


class FakeDesk:
    def __init__(self, market: FakeMarket):
        self.market = market


def _doc(chars: int, *, body: str = "本公司董事会保证公告内容真实、准确、完整。") -> str:
    """A document of roughly ``chars`` characters built from real-looking sentences."""
    out = []
    size = 0
    while size < chars:
        out.append(body)
        size += len(body) + 1
    return "\n".join(out)


def test_a_window_ends_on_a_break_not_mid_sentence():
    """窗口必须在句读处收口——否则读者读到的是半句话，而它看起来像完整的。"""
    text = "\n".join(["第一段内容完整。" * 20, "第二段内容完整。" * 20, "第三段内容完整。" * 20])
    desk = FakeDesk(FakeMarket({"1": {"text": text, "chars": len(text)}}))

    out = filing.read(desk, "1", chars=400)

    assert out["ok"] is True
    assert out["text"].endswith(("。", "\n")), out["text"][-20:]
    assert out["returned"] < 400, "在句读处收口，长度必然略小于请求值"


def test_the_pages_concatenate_back_to_the_exact_document():
    """分页是可逆的：逐页取完必须一字不差地还原原文。

    这条恒等式是分页唯一的正确性判据。差一个字符就说明切点算错了，
    而错位在长文档里会累积成\"某一段永远读不到\"。
    """
    text = _doc(9000)
    desk = FakeDesk(FakeMarket({"1": {"text": text, "chars": len(text)}}))

    pieces, offset, pages = [], 0, 0
    while True:
        out = filing.read(desk, "1", offset=offset)
        pieces.append(out["text"])
        pages += 1
        if out["done"]:
            break
        offset = out["next_offset"]
        assert pages < 20

    assert "".join(pieces) == text
    assert pages == out["pages_at_this_size"] or pages <= out["pages_at_this_size"] + 1


def test_the_byte_budget_binds_before_the_character_count():
    """同一句 6,000 字，中文是 18 KB、数字是 11 KB——上限是字节，就该按字节切。

    实测过：`chars=6000` 在一份三季报上产出 16,627 B 的响应，超过宿主 16 KB
    上限，会被**整包丢弃**。那比短一点糟得多。
    """
    chinese = "公司" * 3000            # 6,000 字，18,000 字节
    desk = FakeDesk(FakeMarket({"1": {"text": chinese, "chars": len(chinese)}}))

    out = filing.read(desk, "1", chars=6000)

    assert out["returned"] < 6000, "字节预算必须先把窗口压下来"
    assert out["bytes"] <= filing.BYTE_BUDGET
    assert out["bytes_limited"]["served_chars"] == out["returned"]
    assert out["next_offset"] == out["returned"]


def test_next_offset_is_the_only_way_forward():
    """`offset + chars` 会漂移，因为窗口在句读处收口；只有 next_offset 是准的。"""
    text = "\n".join(["一句话。" * 30] * 30)
    desk = FakeDesk(FakeMarket({"1": {"text": text, "chars": len(text)}}))

    out = filing.read(desk, "1", chars=400)
    nxt = filing.read(desk, "1", offset=out["next_offset"])

    assert out["next_offset"] == out["offset"] + out["returned"]
    assert nxt["text"][0] == text[out["next_offset"]]
    assert "next_offset" in out["note"]


def test_a_non_numeric_id_is_refused_with_a_pasteable_example():
    out = filing.read(FakeDesk(FakeMarket({})), "600519.SH")

    assert out["ok"] is False
    assert out["example"]["announcement_id"].isdigit()
    assert "600519.SH" in out["error"]


def test_an_id_the_table_does_not_have_is_an_answer_not_a_500():
    """`ok:false` 带 need，而不是 404——\"没有这个 id\"是文档的事实，不是服务坏了。"""
    out = filing.read(FakeDesk(FakeMarket({}, {})), "9999999999")

    assert out["ok"] is False
    assert out["status"] == "unknown_id"
    assert out["need"]


def test_an_undownloaded_body_says_which_step_to_run(monkeypatch):
    row = {"announcement_id": "1", "ts_code": "600519.SH", "pdf_url": "http://x/y.pdf",
           "size_kb": 100, "title": "公告", "pub_date": "20261030"}
    desk = FakeDesk(FakeMarket({}, {"1": row}))

    out = filing.read(desk, "1", fetch=False)

    assert out["ok"] is False
    assert out["status"] == "not_downloaded"
    assert "fetch=true" in out["need"] or "fetch_anns_text" in out["need"]


def test_a_scan_says_it_is_a_scan_instead_of_retrying():
    """扫描件没有文字层：再抓一百次也一样，下一个动作是去看 pdf_url。"""
    row = {"announcement_id": "1", "ts_code": "600519.SH", "pdf_url": "http://x/y.pdf",
           "size_kb": 100, "title": "公告", "pub_date": "20261030"}
    desk = FakeDesk(FakeMarket({}, {"1": row}))

    from service.adapters import anns_text

    monkeypatch_real = anns_text.fetch_one
    try:
        anns_text.fetch_one = lambda *a, **k: {"id": "1", "status": "no_text_layer", "chars": 0}
        out = filing.read(desk, "1", fetch=True)
    finally:
        anns_text.fetch_one = monkeypatch_real

    assert out["ok"] is False
    assert out["status"] == "no_text_layer"
    assert "扫描件" in out["need"]


def test_twelve_pages_is_reported_as_a_slice_not_the_document():
    """抽取上限 12 页。说成\"没有这一项\"是最坏的一种错。"""
    text = _doc(500)
    desk = FakeDesk(FakeMarket({"1": {"text": text, "chars": len(text),
                                      "pages_read": 12, "pages_total": 187}}))

    out = filing.read(desk, "1")

    assert "12" in out["coverage"] and "187" in out["coverage"]
    assert out["pages_total"] == 187


def test_an_old_cache_entry_without_a_page_total_is_still_read_honestly():
    text = _doc(500)
    desk = FakeDesk(FakeMarket({"1": {"text": text, "chars": len(text), "pages_read": 12}}))

    out = filing.read(desk, "1")

    assert "上限" in out["coverage"], "12 页就是抽取上限，它意味着可能更长"


def test_an_offset_past_the_end_is_an_empty_page_not_an_error():
    text = _doc(100)
    desk = FakeDesk(FakeMarket({"1": {"text": text, "chars": len(text)}}))

    out = filing.read(desk, "1", offset=10**6)

    assert out["ok"] is True
    assert out["text"] == "" and out["done"] is True
    assert "超过正文长度" in out["note"]


@pytest.mark.parametrize("chars,expected_cap", [(100, True), (99999, True), (4000, False)])
def test_a_window_size_outside_the_range_is_clamped_and_admitted(chars, expected_cap):
    text = _doc(2000)
    desk = FakeDesk(FakeMarket({"1": {"text": text, "chars": len(text)}}))

    out = filing.read(desk, "1", chars=chars)

    assert ("window_capped" in out) is expected_cap
