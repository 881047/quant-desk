"""巨潮资讯网 (cninfo) — the CSRC-designated disclosure platform, no key needed.

Why this source rather than another vendor: every listed company must publish
here by law, so the file is complete by construction, the PDFs are the originals,
and there is no token, quota or permission tier to lose.

Two things about the interface are worth writing down, because the obvious
version of each is wrong.

**The per-symbol endpoint is broken.** `POST /new/hisAnnouncement/query` with
`stock=000001,szse` returns `totalAnnouncement: 0` — verified on 2026-10-28, and
reported broken since 2026-05. What works is the full-text search endpoint
`/new/fulltextSearch/full` queried by the six-digit code, then filtered on
`secCode` locally.

**`isfulltext=false` silently loses about 40% of the file.** For 平安银行 over
2026-07-01..10-28 the SZSE official API returns 19 announcements; cninfo with
`isfulltext=false` returns 11, with `isfulltext=true` returns 19 — the same 19.
Title-only search misses everything whose title does not carry the search terms,
which is exactly the routine filings a decision needs.

The PDF links (`static.cninfo.com.cn/finalpage/...`) still resolve — 200,
`application/pdf`, `%PDF-` — despite a 2026-05 claim that they had all died.
Checked rather than assumed; `tests/data/test_cninfo.py` re-checks the shape.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import ClassVar

import pandas as pd

from service import settings
from service.adapters.eastmoney_adapter import (
    RetryBudget,
    _close_response,
    _finite_timeout,
    _retryable_http_error,
)

# Keywords → labels. Three questions are kept apart on purpose:
#
#   category — what the filing is about        (回购 / 减持 / 诉讼 …)
#   kind     — what kind of document it is     (决议 / 意见 / 通知 …)
#   stage    — where the thing stands          (计划 / 进展 / 完成 / 终止 …)
#
# Folding them into one bucket loses the answer that matters: 「回购进展」 and
# 「回购方案」 are both 回购, and whether the buyback is proposed or half done is
# the whole point. `paperwork` is a fourth, orthogonal fact: the document
# carries no new event, it schedules, formalises or duplicates one.
#
# Every rule is a literal substring of the title, and `labels()` returns the word
# that produced each label. A label nobody can audit is a label nobody trusts —
# the 2023 公司法 rename of 股东大会 → 股东会 is what happens when the vocabulary
# is assumed instead of measured (see P46: 48.3% → 23.1%, then P47 → 10.1%).
#
# Order matters: the first match wins, and specific beats generic.
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("定期报告", ("年度报告", "半年度报告", "季度报告", "年报", "中报", "季报")),
    ("业绩", ("业绩预告", "业绩快报", "业绩说明", "业绩预增", "业绩预减", "业绩预亏",
             "业绩预盈", "业绩修正")),
    # 计提减值 was 1,834 titles sitting in 其他; it is a P&L event, not paperwork.
    ("减值", ("减值", "计提", "核销")),
    ("减持", ("减持", "拟减持", "减持计划", "减持进展")),
    ("增持", ("增持", "增持计划", "增持进展")),
    ("回购", ("回购",)),
    # 期权 alone is another 1,538 titles: 「股票期权激励计划」 never says 股权激励.
    ("股权激励", ("股权激励", "限制性股票", "员工持股", "期权")),
    ("质押", ("质押", "解除质押", "解押")),
    ("解禁", ("限售", "解禁", "上市流通")),
    # 债券 lines are financing and behave like it: a forced conversion or a
    # redemption moves the share count. 1,306 titles mention 转债 alone, and
    # 融资券 / 中期票据 are the same shelf.
    ("再融资", ("向特定对象发行", "非公开发行", "可转换公司债券", "配股", "定增",
               "转债", "转股", "债券", "融资券", "中期票据", "票据", "优先股")),
    ("并购重组", ("重大资产重组", "发行股份购买", "购买资产", "收购", "吸收合并",
                 "重大资产")),
    # 会计 sits after 并购重组 so 「会计师事务所关于重大资产重组的专项说明」 stays
    # a deal document; on its own it is about the books (政策变更/差错/审计机构).
    ("会计", ("会计政策", "会计估计", "会计师事务所", "审计机构")),
    ("分红", ("权益分派", "利润分配", "分红", "派息")),
    ("监管", ("问询", "关注函", "监管函", "纪律处分", "立案", "处罚", "警示函")),
    # 异动 and 澄清 are exchange-facing, not company events: a 异常波动公告 exists
    # because the tape moved 20%, and reading it as "news" overstates it.
    ("异动", ("异常波动", "澄清", "风险提示")),
    ("诉讼", ("诉讼", "仲裁", "破产", "刑事", "法院", "裁定", "重整")),
    ("担保", ("担保",)),
    ("关联交易", ("关联交易", "关联资金")),
    # 募集资金管理 (现金管理/补流/专户) is 5% of the file and says nothing about
    # the business — separate from 再融资, which is where the money came from.
    ("募集资金", ("募集资金", "募投", "闲置自有资金", "现金管理", "补充流动资金", "超募")),
    ("权益变动", ("权益变动", "协议转让", "询价转让", "控制权", "要约收购", "股份转让",
                 "无偿划转", "表决权委托", "一致行动人")),
    ("对外投资", ("对外投资", "参与投资", "认购", "私募基金", "产业基金",
                 "合伙企业", "增资", "设立子公司")),
    ("经营", ("中标", "合同", "订单", "框架协议", "经营数据", "产销", "专利", "资质",
             "注册证", "注册批准", "药品注册", "认证")),
    ("停复牌", ("停牌", "复牌", "风险警示", "退市")),
    # 人事 verbs come before 会议: 「补选董事会非独立董事」 mentions the board but is
    # an appointment, and 4,544 titles were read as meetings because of it.
    ("人事", ("辞职", "聘任", "任职", "换届", "选举", "补选", "改选", "离任")),
    # 公司治理 before 会议 for the same reason inverted: 「董事会薪酬与考核委员会
    # 工作细则」 is a charter document, not a meeting, and 董事会 is in both.
    ("公司治理", ("章程", "议事规则", "管理制度", "治理制度", "制度", "办法", "细则",
                 "薪酬")),
    # 股东会, not 股东大会: the 2023 公司法 revision renamed it, and the titles
    # followed. Matching only the old form left 3,000+ meeting notices in 其他.
    ("会议", ("股东大会", "股东会", "董事会", "监事会", "会议决议", "会议材料",
             "会议资料", "会议通知", "法律意见书")),
    ("人事", ("董事长", "总经理", "财务总监", "独立董事", "董事", "证券事务代表")),
)

# The document, not the subject. 49% fall through to 公告, which is an honest
# answer: the title literally is 「关于……的公告」 with no other form marker.
KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("函件", ("问询函", "关注函", "监管函", "警示函", "告知书")),
    ("决议", ("决议", "审议通过", "表决")),
    ("意见", ("意见", "核查", "保荐", "督导", "跟踪报告", "审计报告", "评估报告")),
    ("说明", ("说明", "解释", "澄清", "回复")),
    ("通知", ("通知", "提示性", "提示")),
    ("报告", ("报告", "月报表", "汇总表")),
)
KIND_DEFAULT = "公告"

# Where the thing stands. `None` is the common and correct answer (79%): most
# titles do not say. Only unambiguous markers are listed — 「实施」 and 「拟」 were
# in the first draft and pushed coverage to 100%, which is the same as saying
# nothing.
STAGE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("更正", ("更正",)),
    # 调整 is not a correction: 「调整回购价格上限」 changes the terms on purpose —
    # 640 of the 7,708 buyback filings say it.
    ("调整", ("调整", "修正")),
    ("终止", ("终止", "取消", "中止")),
    ("完成", ("完成", "完毕", "实施结果", "实施公告", "结果", "过户", "兑付")),
    ("进展", ("进展", "阶段性")),
    ("计划", ("预案", "草案", "计划书")),
)

# 「方案」 only means a proposal in some categories. In 分红 it is the payout
# plan; in an 行动方案 it is a slogan. Applying it per category keeps the label
# from spreading to documents where it says nothing.
STAGE_EXTRA: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "回购": (("计划", ("方案", "报告书")),),
    "分红": (("计划", ("方案",)),),
    "再融资": (("计划", ("方案",)),),
    "并购重组": (("计划", ("方案",)),),
    # 「减持计划」 is the announcement that starts the clock; without this the
    # label was null on the one filing a reader most needs to place in time.
    "减持": (("计划", ("计划",)),),
    "增持": (("计划", ("计划",)),),
    "股权激励": (("完成", ("授予", "归属", "行权")), ("终止", ("注销",))),
}

# Documents about documents: an agenda, a charter, an opinion letter, a summary
# of a report that was filed separately. They announce no new event, and 22% of
# the file is made of them — the single cheapest thing a reader can skip.
PAPERWORK_WORDS: tuple[str, ...] = (
    "会议通知", "会议材料", "会议资料", "会议议程", "议事规则", "章程",
    "管理制度", "管理办法", "实施细则", "工作制度", "声明与承诺", "月报表",
    # 报告摘要, not 摘要: 「募集说明书摘要」 is the readable half of a prospectus and
    # must not be filed under "no new fact".
    "报告摘要", "英文版", "汇总表", "法律意见书",
)

_TAG = re.compile(r"</?em>")
_NEGATIONS = "不未"


def _clean_title(text: str) -> str:
    return _TAG.sub("", str(text or "")).strip()


def _find(text: str, word: str) -> int:
    """Index of ``word``, skipping occurrences that are negated (不/未)."""
    start = 0
    while True:
        i = text.find(word, start)
        if i < 0:
            return -1
        if i == 0 or text[i - 1] not in _NEGATIONS:
            return i
        start = i + 1


def _hit(text: str, rules: tuple[tuple[str, tuple[str, ...]], ...],
         default: str | None = None) -> tuple[str | None, str]:
    for name, words in rules:
        for word in words:
            if _find(text, word) >= 0:
                return name, word
    return default, ""


def classify(title: str) -> str:
    """Coarse category from the title. Derived, not authoritative."""
    return _hit(_clean_title(title), CATEGORY_RULES, "其他")[0]


def labels(title: str) -> dict:
    """Every derived label for one title, plus the word behind each of them."""
    clean = _clean_title(title)
    category, cat_word = _hit(clean, CATEGORY_RULES, "其他")
    kind, kind_word = _hit(clean, KIND_RULES, KIND_DEFAULT)
    stage, stage_word = _hit(clean, STAGE_RULES)
    if stage is None:
        for extra_stage, words in STAGE_EXTRA.get(category, ()):
            for word in words:
                if _find(clean, word) >= 0:
                    stage, stage_word = extra_stage, word
                    break
            if stage is not None:
                break
    paper_word = next((w for w in PAPERWORK_WORDS if _find(clean, w) >= 0), "")
    # A resolution is a decision, never paperwork — even when its subject is the
    # charter. 「股东大会关于变更公司章程的决议」 carries the vote that changed it.
    if kind == "决议":
        paper_word = ""
    # A meeting notice schedules a decision that has not been made yet. The
    # words alone miss it — the title says 「股东会的通知」, not 「会议通知」 — so the
    # two labels compose the fact instead.
    if not paper_word and category == "会议" and kind == "通知":
        paper_word = "会议+通知"
    return {
        "category": category,
        "kind": kind,
        "stage": stage,
        "paperwork": bool(paper_word),
        "basis": {"category": cat_word, "kind": kind_word,
                  "stage": stage_word, "paperwork": paper_word},
    }


def reclassify(frame: pd.DataFrame) -> pd.DataFrame:
    """Recompute the derived columns from `title` on a stored frame.

    The labels are derived, so a better rule table must not require 5,919 more
    requests to apply — that is how the 48% 其他 was cut after the fact, and how
    the three new columns reach 218k rows that were fetched before they existed.
    """
    if frame is None or frame.empty or "title" not in frame.columns:
        return frame
    out = frame.copy()
    parsed = out["title"].map(labels)
    out["category"] = [item["category"] for item in parsed]
    out["kind"] = [item["kind"] for item in parsed]
    out["stage"] = [item["stage"] for item in parsed]
    out["paperwork"] = [item["paperwork"] for item in parsed]
    return out


PDF_HOST = "static.cninfo.com.cn"


def _validate_pdf_url(url: str) -> None:
    """Allow only the public cninfo file host, including every redirect target."""
    try:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid cninfo PDF URL: {url}") from exc
    default_port = {"http": 80, "https": 443}.get(scheme)
    if (default_port is None or parts.hostname != PDF_HOST
            or parts.username is not None or parts.password is not None
            or (port is not None and port != default_port)):
        raise ValueError(f"not a cninfo static URL: {url}")


class _CninfoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_pdf_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class CninfoAdapter:
    SEARCH = "http://www.cninfo.com.cn/new/fulltextSearch/full"
    STATIC = "http://static.cninfo.com.cn/"
    HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Referer": "http://www.cninfo.com.cn/new/disclosure",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    def __init__(self, *, timeout: float | None = None, retries: int | None = None,
                 min_interval: float | None = None, max_seconds: float | None = None,
                 max_pdf_bytes: int | None = None):
        """Defaults come from configs/params.yaml (source.cninfo.*).

        A quarter-second between calls: this is a public service, not a data
        feed, and being throttled here costs a whole day of announcements.
        """
        timeout = settings.num("source.cninfo.timeout") if timeout is None else timeout
        retries = settings.integer("source.cninfo.retries") if retries is None else retries
        if min_interval is None:
            min_interval = settings.num("source.cninfo.min_interval")
        if max_seconds is None:
            max_seconds = settings.num("source.cninfo.max_seconds")
        if max_pdf_bytes is None:
            max_pdf_bytes = settings.integer("source.cninfo.max_pdf_bytes")
        if isinstance(max_pdf_bytes, bool) or not isinstance(max_pdf_bytes, int) or max_pdf_bytes <= 0:
            raise ValueError("max_pdf_bytes must be a positive integer")
        self.timeout = _finite_timeout(timeout)
        self.retries = retries
        self.min_interval = min_interval
        self.max_seconds = max_seconds
        self.max_pdf_bytes = max_pdf_bytes
        self._last = 0.0
        self.calls = 0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _post(self, url: str, payload: dict) -> dict:
        body = urllib.parse.urlencode(payload).encode()
        headers = {**self.HEADERS,
                   "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        last: Exception | None = None
        budget = RetryBudget(self.max_seconds)
        for attempt in range(self.retries + 1):
            response = None
            self._throttle()
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                response = urllib.request.urlopen(req, timeout=self.timeout)
                self.calls += 1
                return json.loads(response.read().decode("utf-8", "ignore"))
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last = exc
                if isinstance(exc, urllib.error.HTTPError):
                    _close_response(exc)
                retry_source = response if response is not None else exc
                if (attempt >= self.retries
                        or not _retryable_http_error(exc, response)
                        or not budget.wait(attempt, retry_source)):
                    break
            finally:
                _close_response(response)
        raise last  # type: ignore[misc]

    def search(self, symbol: str, start: str, end: str, *,
               page_size: int = 30, max_pages: int = 12) -> pd.DataFrame:
        """Every announcement filed by ``symbol`` between two dates (inclusive).

        ``start`` / ``end`` are YYYYMMDD; the endpoint wants YYYY-MM-DD.
        Results are filtered on an exact `secCode` match: searching by code is
        precise (10/10 in a spot check), searching by company name is not — it
        returns the parent, the H-share line and unrelated names that merely
        appear in the title.
        """
        code = symbol.split(".")[0]
        rows: list[dict] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            payload = {
                "searchkey": code,
                "sdate": f"{start[:4]}-{start[4:6]}-{start[6:]}",
                "edate": f"{end[:4]}-{end[4:6]}-{end[6:]}",
                # See the module docstring: the title-only mode drops ~40%.
                "isfulltext": "true",
                "sortName": "pubdate",
                "sortType": "desc",
                "pageNum": str(page),
                "pageSize": str(page_size),
            }
            data = self._post(self.SEARCH, payload)
            batch = data.get("announcements") or []
            if not batch:
                break
            for item in batch:
                if str(item.get("secCode")) != code:
                    continue
                ann_id = str(item.get("announcementId") or "")
                if not ann_id or ann_id in seen:
                    continue
                stamp = item.get("announcementTime")
                try:
                    if stamp in (None, ""):
                        continue
                    pub = time.strftime("%Y%m%d", time.localtime(int(stamp) / 1000))
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
                seen.add(ann_id)
                adjunct = str(item.get("adjunctUrl") or "")
                size = item.get("adjunctSize")
                title = _clean_title(item.get("announcementTitle"))
                tag = labels(title)
                rows.append({
                    "announcement_id": ann_id,
                    "ts_code": symbol,
                    "name": str(item.get("secName") or ""),
                    "title": title,
                    "category": tag["category"],
                    "kind": tag["kind"],
                    "stage": tag["stage"],
                    "paperwork": tag["paperwork"],
                    "pub_date": pub,
                    "pdf_url": (self.STATIC + adjunct) if adjunct else "",
                    "adjunct_type": str(item.get("adjunctType") or ""),
                    "size_kb": float(size) if size not in (None, "") else None,
                })
            if not data.get("hasMore", False) or page * page_size >= int(
                    data.get("totalRecordNum") or 0):
                break
        if not rows:
            return pd.DataFrame()
        out = pd.DataFrame(rows)
        return out.sort_values(["pub_date", "announcement_id"],
                               ascending=[False, True]).reset_index(drop=True)

    def _read_pdf(self, response, url: str, deadline: float) -> bytes:
        geturl = getattr(response, "geturl", lambda: url)
        _validate_pdf_url(geturl())
        headers = getattr(response, "headers", {}) or {}
        length = headers.get("Content-Length")
        if length not in (None, ""):
            try:
                if int(length) > self.max_pdf_bytes:
                    raise ValueError(f"PDF exceeds {self.max_pdf_bytes} bytes: {url}")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid or excessive PDF Content-Length: {length!r}") from exc

        blob = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"PDF download exceeded {self.max_seconds}s: {url}")
            chunk = response.read(min(1024 * 1024, self.max_pdf_bytes + 1 - len(blob)))
            if not chunk:
                break
            blob.extend(chunk)
            if len(blob) > self.max_pdf_bytes:
                raise ValueError(f"PDF exceeds {self.max_pdf_bytes} bytes: {url}")
        return bytes(blob)

    def download_pdf(self, url: str) -> bytes:
        """Read a bounded PDF from cninfo, validating every redirect target."""
        _validate_pdf_url(url)
        last: Exception | None = None
        budget = RetryBudget(self.max_seconds)
        deadline = budget.started + self.max_seconds
        opener = urllib.request.build_opener(_CninfoRedirectHandler())
        for attempt in range(self.retries + 1):
            response = None
            self._throttle()
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"PDF download exceeded {self.max_seconds}s: {url}")
                req = urllib.request.Request(
                    url, headers={**self.HEADERS, "Referer": self.STATIC})
                response = opener.open(req, timeout=min(self.timeout, remaining))
                self.calls += 1
                blob = self._read_pdf(response, url, deadline)
                if not blob.startswith(b"%PDF"):
                    raise ValueError(f"not a PDF: {url} -> {blob[:8]!r}")
                return blob
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if isinstance(exc, urllib.error.HTTPError):
                    _close_response(exc)
                retry_source = response if response is not None else exc
                if (attempt >= self.retries
                        or not _retryable_http_error(exc, response)
                        or not budget.wait(attempt, retry_source)):
                    break
            finally:
                _close_response(response)
        raise last  # type: ignore[misc]


def _page_text(page) -> str:
    """Text of one page, or empty. A single unreadable page must not lose the rest."""
    try:
        return page.extract_text() or ""
    except Exception:  # noqa: BLE001
        return ""


def pdf_to_text(blob: bytes, max_pages: int = 12) -> tuple[str, int, int, int]:
    """Extract text from the first ``max_pages`` pages.

    Returns ``(text, pages_read, pages_empty, pages_total)``. The truncation is
    deliberate: an annual report runs 200+ pages and the model does not need the
    accounting notes to judge what the filing is. Callers record ``pages_read``
    so a short excerpt is never mistaken for the whole document; ``pages_empty``
    counts pages that yielded no text (scans, charts), which is a different fact
    from "not read". ``pages_total`` is what makes the first two readable:
    "12 of 187" says the rest exists, "12 of 12" says it was read whole.
    """
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(blob))
    total = len(reader.pages)
    pages = list(reader.pages[:max_pages])
    chunks = [_page_text(page) for page in pages]
    empty = sum(1 for chunk in chunks if not chunk.strip())
    text = "\n".join(chunks)
    text = re.sub(r"[ 	]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), len(pages), empty, total
