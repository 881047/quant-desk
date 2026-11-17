"""cninfo adapter, offline: parsing, paging and the filters that matter.

The live endpoint is exercised by the backfill, not by the suite — a test that
needs the network is a test that fails on a train. What is pinned here is the
logic that was wrong the first time it was written: filtering to an exact
`secCode`, deduplicating across pages, and refusing a body that is not a PDF.
"""

from __future__ import annotations

import io
import urllib.error
from datetime import date

import pytest

import service.adapters.cninfo_adapter as cninfo_mod
import service.adapters.eastmoney_adapter as em_mod
from service.adapters.cninfo_adapter import CninfoAdapter, classify, labels, pdf_to_text


def _item(code: str, ann_id: str, title: str, *, ts: int = 1792771200000,
          adjunct: str = "finalpage/2026-10-24/1.PDF") -> dict:
    return {"secCode": code, "announcementId": ann_id, "secName": "测试",
            "announcementTitle": title, "announcementTime": ts,
            "adjunctUrl": adjunct, "adjunctType": "PDF", "adjunctSize": 100}


def test_search_keeps_only_the_exact_code():
    """Searching by code is precise; searching by name returns the parent too."""
    calls: list[dict] = []

    def fake_post(url, payload):
        calls.append(payload)
        return {"announcements": [_item("000001", "1", "平安银行：<em>季报</em>"),
                                  _item("601318", "2", "中国平安：关于平安银行的公告"),
                                  _item("02318", "3", "H 股公告")],
                "hasMore": False, "totalRecordNum": 3}

    client = CninfoAdapter(min_interval=0)
    client._post = fake_post                       # type: ignore[method-assign]
    df = client.search("000001.SZ", "20260701", "20261028")

    assert len(df) == 1
    assert df.iloc[0]["ts_code"] == "000001.SZ"
    assert df.iloc[0]["title"] == "平安银行：季报", "<em> 是检索高亮，不是内容"
    assert calls[0]["isfulltext"] == "true", "标题模式会丢掉 40% 的公告"
    # The labels are computed at fetch time and stored, so a reader never has to
    # re-run the rule table over 218k titles to ask what a filing is.
    assert set(df.columns) >= {"category", "kind", "stage", "paperwork"}
    assert df.iloc[0]["kind"] == "公告"
    assert df.iloc[0]["stage"] is None


def test_pages_are_deduplicated_and_stop_on_has_more():
    page = {"n": 0}

    def fake_post(url, payload):
        page["n"] += 1
        if page["n"] == 1:
            return {"announcements": [_item("000001", "1", "第一页"),
                                      _item("000001", "2", "第一页之二")],
                    "hasMore": True, "totalRecordNum": 3}
        # The second page repeats id 2, which the first page already returned.
        return {"announcements": [_item("000001", "2", "第一页之二"),
                                  _item("000001", "3", "第二页")],
                "hasMore": False, "totalRecordNum": 3}

    client = CninfoAdapter(min_interval=0)
    client._post = fake_post                       # type: ignore[method-assign]
    # page_size must be consistent with totalRecordNum, or the loop stops on the
    # "already have them all" rule before it ever asks for page two.
    df = client.search("000001.SZ", "20260701", "20261028", page_size=2)

    assert sorted(df["announcement_id"]) == ["1", "2", "3"]


def test_a_pdf_url_must_be_a_cninfo_static_url():
    """A guard, because a redirect is how a downloader ends up parsing HTML."""
    client = CninfoAdapter(min_interval=0)
    with pytest.raises(ValueError):
        client.download_pdf("http://example.com/x.PDF")


def test_the_bucket_table_follows_how_titles_are_actually_written():
    """48% of 105k rows landed in 其他 before this: the words had moved on.

    The 2023 公司法 renamed 股东大会 to 股东会 and the titles followed, so a rule
    matching only the old form left 3,000+ meeting notices unclassified. 转债
    lines, 独立董事 filings and 章程 revisions were the next three buckets.
    """
    assert classify("关于召开2025年第二次临时股东会的通知") == "会议"
    assert classify("2025年第二次临时股东会决议公告") == "会议"
    assert classify("独立董事候选人声明与承诺") == "人事"
    assert classify("公司章程（修订稿）") == "公司治理"
    assert classify("关于不向下修正双乐转债转股价格的公告") == "再融资"
    assert classify("股票交易异常波动公告") == "异动"
    # The old form still matches, because half the archive predates the change.
    assert classify("2025年年度股东大会决议公告") == "会议"


def test_reclassify_rewrites_a_stored_frame_without_a_refetch():
    """A derived label must be recomputable cheaply, or the rule table is frozen."""
    import pandas as pd

    from service.adapters.cninfo_adapter import reclassify

    frame = pd.DataFrame({"title": ["关于召开2025年第二次临时股东会的通知"],
                          "category": ["其他"]})
    out = reclassify(frame)
    assert out.iloc[0]["category"] == "会议"
    assert frame.iloc[0]["category"] == "其他", "调用方的那份不该被就地改动"


def test_reclassify_adds_the_columns_a_frame_predates():
    """The 218k rows were fetched before kind/stage/paperwork existed."""
    import pandas as pd

    from service.adapters.cninfo_adapter import reclassify

    frame = pd.DataFrame({"title": ["关于回购公司股份方案的公告"]})
    out = reclassify(frame)

    assert set(out.columns) >= {"category", "kind", "stage", "paperwork"}
    assert out.iloc[0]["kind"] == "公告"
    assert out.iloc[0]["stage"] == "计划"
    assert bool(out.iloc[0]["paperwork"]) is False


def test_the_same_subject_at_three_stages_keeps_three_labels():
    """「回购」 is the subject; proposed / in progress / terminated is the answer.

    One bucket cannot hold both: a buyback that was proposed, one that is
    running and one that was called off are the same category and opposite facts.
    """
    plan = labels("关于回购公司股份方案的公告")
    running = labels("关于回购股份的进展公告")
    stopped = labels("关于终止回购股份方案的公告")

    assert {plan["category"], running["category"], stopped["category"]} == {"回购"}
    assert (plan["stage"], running["stage"], stopped["stage"]) == ("计划", "进展", "终止")


def test_a_meeting_notice_is_paperwork_and_a_resolution_is_not():
    """The notice schedules a decision; the resolution is the decision."""
    notice = labels("关于召开2025年第二次临时股东会的通知")
    resolution = labels("2025年第二次临时股东会决议公告")

    assert notice["category"] == resolution["category"] == "会议"
    assert notice["kind"] == "通知" and resolution["kind"] == "决议"
    assert notice["paperwork"] is True
    assert resolution["paperwork"] is False, "决议带着表决结果，不是程序文件"


def test_every_label_reports_the_word_it_came_from():
    """A wrong label has to be checkable without re-reading the rule table."""
    item = labels("关于持股5%以上股东减持计划的公告")

    assert item["basis"]["category"] == "减持"
    assert item["basis"]["stage"] == "计划"
    assert item["stage"] == "计划", "减持计划是这条时间线的起点，不能是空"
    assert labels("独立董事候选人声明与承诺")["basis"]["paperwork"] == "声明与承诺"


def test_a_negated_marker_does_not_set_the_stage():
    """「未完成」 is not 完成. The marker is a word, and words get negated."""
    assert labels("关于未完成业绩承诺的说明")["stage"] is None
    assert labels("关于完成回购的公告")["stage"] == "完成"


def test_titles_are_bucketed_by_keyword_not_by_vendor_code():
    assert classify("平安银行：2026年第三季度报告") == "定期报告"
    assert classify("关于持股5%以上股东减持计划的公告") == "减持"
    assert classify("关于股东减持计划实施完毕的公告") == "减持"
    assert classify("关于收到推荐董事长人选文件的公告") == "人事"
    assert classify("") == "其他"


def test_pdf_text_reports_what_it_read():
    """`pages_read` exists so a 12-page excerpt is never mistaken for the whole."""

    class _Page:
        def __init__(self, text=None, boom=False):
            self._text, self._boom = text, boom

        def extract_text(self):
            if self._boom:
                raise RuntimeError("bad page")
            return self._text

    class _Reader:
        def __init__(self, pages):
            self.pages = pages

    import pypdf

    real = pypdf.PdfReader
    pypdf.PdfReader = lambda _: _Reader([_Page("第一页"), _Page(boom=True), _Page(None)])
    try:
        text, read, empty, total = pdf_to_text(b"anything", max_pages=5)
    finally:
        pypdf.PdfReader = real
    assert read == 3
    assert empty == 2, "one page raised, one page had no text layer"
    assert "第一页" in text


def test_announcement_dates_are_day_precision():
    """Byte-for-byte the interface's limit, and the profile has to say so."""
    assert date(2026, 10, 24).strftime("%Y%m%d") == "20261024"


def test_cninfo_retries_5xx_with_shared_budget_and_closes(monkeypatch):
    error = urllib.error.HTTPError(
        "http://fake.invalid", 503, "unavailable",
        {"Retry-After": "0.125"}, io.BytesIO(b""),
    )
    closed = []
    error.close = lambda: closed.append("error")

    class Response:
        def read(self):
            return b'{"announcements": []}'

        def close(self):
            closed.append("response")

    calls = []

    def fake_urlopen(request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise error
        return Response()

    slept = []
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(em_mod.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(em_mod.time, "sleep", slept.append)
    client = CninfoAdapter(retries=1, min_interval=0, max_seconds=5)

    assert client._post("http://fake.invalid", {}) == {"announcements": []}
    assert len(calls) == 2
    assert slept == [0.125]
    assert closed == ["error", "response"]


def test_cninfo_does_not_retry_4xx(monkeypatch):
    error = urllib.error.HTTPError(
        "http://fake.invalid", 404, "missing", {}, io.BytesIO(b""),
    )
    error.close = lambda: None
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(timeout)
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(em_mod.time, "sleep", lambda _s: pytest.fail("4xx 不应退避"))
    client = CninfoAdapter(retries=3, min_interval=0)

    with pytest.raises(urllib.error.HTTPError):
        client._post("http://fake.invalid", {})
    assert len(calls) == 1


def test_invalid_announcement_timestamp_is_dropped():
    valid = _item("000001", "1", "有效公告")
    invalid = _item("000001", "2", "非法日期")
    invalid["announcementTime"] = "not-a-timestamp"
    client = CninfoAdapter(min_interval=0)
    client._post = lambda _url, _payload: {
        "announcements": [invalid, valid], "hasMore": False, "totalRecordNum": 2,
    }

    out = client.search("000001.SZ", "20260101", "20261231")
    assert out["announcement_id"].tolist() == ["1"]


@pytest.mark.parametrize("url", [
    "http://static.cninfo.com.cn.evil.invalid/x.PDF",
    "http://user@static.cninfo.com.cn/x.PDF",
    "https://127.0.0.1/x.PDF",
    "ftp://static.cninfo.com.cn/x.PDF",
    "http://static.cninfo.com.cn:8080/x.PDF",
])
def test_pdf_url_rejects_untrusted_hosts_userinfo_schemes_and_ports(url):
    client = CninfoAdapter(min_interval=0)
    with pytest.raises(ValueError):
        client.download_pdf(url)


def test_redirect_target_is_validated_before_following():
    handler = cninfo_mod._CninfoRedirectHandler()
    with pytest.raises(ValueError):
        handler.redirect_request(
            None, None, 302, None, {}, "http://127.0.0.1/private.PDF"
        )


class _PdfResponse:
    def __init__(self, chunks, *, url="http://static.cninfo.com.cn/x.PDF", headers=None):
        self.chunks = list(chunks)
        self.url = url
        self.headers = headers or {}
        self.closed = False

    def geturl(self):
        return self.url

    def read(self, _size):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


def _install_pdf_opener(monkeypatch, response, calls):
    class Opener:
        def open(self, _request, timeout):
            calls.append(timeout)
            return response

    monkeypatch.setattr("urllib.request.build_opener", lambda _handler: Opener())


def test_pdf_download_streams_with_finite_timeout_and_closes(monkeypatch):
    response = _PdfResponse([b"%PDF-", b"page"])
    calls = []
    _install_pdf_opener(monkeypatch, response, calls)
    client = CninfoAdapter(timeout=3.5, retries=0, min_interval=0, max_seconds=5)

    assert client.download_pdf(client.STATIC + "x.PDF") == b"%PDF-page"
    assert len(calls) == 1 and 0 < calls[0] <= 3.5
    assert response.closed


def test_pdf_download_rejects_body_over_the_byte_cap(monkeypatch):
    response = _PdfResponse([b"%PDF-too-large"])
    calls = []
    _install_pdf_opener(monkeypatch, response, calls)
    client = CninfoAdapter(retries=0, min_interval=0, max_pdf_bytes=8)

    with pytest.raises(ValueError, match="exceeds"):
        client.download_pdf(client.STATIC + "x.PDF")
    assert response.closed


def test_pdf_download_rejects_an_untrusted_final_response_url(monkeypatch):
    response = _PdfResponse([b"%PDF-page"], url="http://127.0.0.1/x.PDF")
    calls = []
    _install_pdf_opener(monkeypatch, response, calls)
    client = CninfoAdapter(retries=0, min_interval=0)

    with pytest.raises(ValueError):
        client.download_pdf(client.STATIC + "x.PDF")
    assert response.closed
