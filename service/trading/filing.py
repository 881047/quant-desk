"""Reading one filing: paged, bounded, and honest about what it is showing.

Titles tell you a filing exists; the body tells you whether it matters.
「减持计划」 and 「减持计划实施完毕」 are the same category and opposite facts, and
only the text separates them. But an annual report is 12,000 characters of prose
after PDF extraction and sometimes 10× that, and the host drops any single tool
result past 16 KB rather than shortening it. So the reader pages: one request,
one contiguous window, `next_offset` as the only authority for what follows.

Three things this module refuses to do:

* **Split mid-sentence when a boundary is available.** A window that starts with
  "…公司拟以自有资金" and ends with "回购价格不超过" reads like a complete
  thought and is not. The cut backs off to the nearest paragraph or sentence
  break within the last 40% of the window.
* **Present twelve pages as the document.** PDF extraction stops at
  ``MAX_PAGES``; the payload says "12 / 187 页" so a number that only appears on
  page 40 is visibly absent rather than silently missing.
* **Fail silently when the text was never downloaded.** On demand it fetches
  once and caches; when that fails it says which failure it was and what to run,
  because "正文未下载" and "PDF 是扫描件" call for different next steps.
"""

from __future__ import annotations

#: The extractor's page ceiling, from the module that owns the cache. Used to
#: read old cache entries honestly: "12 pages" is the ceiling, so it means
#: "possibly longer", while "3" means "that was the whole document".
from service.adapters.anns_text import MAX_PAGES

#: A window of 4,000 characters is ~8.5 KB of UTF-8 for cninfo PDFs (they run
#: ~2.1 bytes a character: lots of digits and ASCII). Measured 2026-11-01 on the
#: largest cached filing: 4,000 chars → 8,410 B, + JSON escaping ≈ 9.3 KB, which
#: leaves room for the metadata and stays clear of the 16 KB ceiling.
DEFAULT_CHARS = 4000
MAX_CHARS = 6000
MIN_CHARS = 400

#: The ceiling that actually binds is bytes, not characters: the same 6,000
#: characters are 11 KB of ASCII-heavy quarterly report and 18 KB of pure
#: Chinese prose. Measured 2026-11-01: `chars=6000` produced a 16,627 B response —
#: past the host ceiling, where the body is dropped whole rather than shortened.
#: So the window is cut to a byte budget, and `returned` reports what survived.
BYTE_BUDGET = 9000

#: Cut preference, strongest first. A paragraph break reads as a decision; a hard
#: cut reads as a truncation even when it isn't one.
_BREAKS = ("\n\n", "\n", "。", "；", "！", "？", "；", ". ")


def _cut(text: str, offset: int, size: int) -> int:
    """End index for a window starting at ``offset``, on the nearest break.

    Never returns past the text, always returns past ``offset``. The back-off
    floor is 60% of the window: searching for a break across the whole window
    would let one early newline shrink a 4,000-character page to 400.
    """
    end = min(len(text), offset + size)
    if end >= len(text):
        return len(text)
    window = text[offset:end]
    floor = max(1, int(size * 0.6))
    for token in _BREAKS:
        at = window.rfind(token)
        if at >= floor:
            return offset + at + len(token)
    return end


def _fit_bytes(text: str, offset: int, size: int, budget: int = BYTE_BUDGET) -> int:
    """End index for a window that is both ``size`` characters and ``budget`` bytes.

    Characters are not the constraint — bytes are. One 6,000-character window was
    11 KB on an ASCII-heavy quarterly report and would have been 18 KB on pure
    Chinese prose, and the host drops anything past 16 KB rather than shortening
    it. So ask in characters, cut to bytes, and let ``returned`` say what fit.

    Binary search on the character count rather than a proportional step: the
    step overshot downwards (it cut a 6,000-character request to 3,400 when
    3,900 fit), and the cost of measuring is one encode of a ≤9 KB slice.
    """
    end = _cut(text, offset, size)
    # No shortcut for "the rest of the document fits in `size`": a 6,000-character
    # remainder that fits the character request can still be 18 KB of Chinese.
    if len(text[offset:end].encode("utf-8")) <= budget:
        return end
    low, high, best = 1, size, 1
    while low <= high:
        mid = (low + high) // 2
        if len(text[offset:offset + mid].encode("utf-8")) <= budget:
            best, low = mid, mid + 1
        else:
            high = mid - 1
    return _cut(text, offset, best)


def _clamp_chars(chars) -> tuple[int, bool]:
    try:
        asked = int(chars)
    except (TypeError, ValueError):
        return DEFAULT_CHARS, False
    if asked < MIN_CHARS:
        return MIN_CHARS, True
    if asked > MAX_CHARS:
        return MAX_CHARS, True
    return asked, False


def read(desk, announcement_id: str, *, offset: int = 0, chars: int = DEFAULT_CHARS,
         fetch: bool = True) -> dict:
    """One window of one filing's text, with everything needed to read the next."""
    ann = str(announcement_id or "").strip()
    if not ann.isdigit():
        return {
            "ok": False,
            "error": f"{announcement_id!r} 不是公告 id——它是纯数字，不是股票代码",
            "example": {"announcement_id": "1225475864"},
            "where": "profile 的 events.announcements[].announcement_id，或 knowledge(topic=filings)",
        }

    size, clamped = _clamp_chars(chars)
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0

    meta = desk.market.announcement(ann)
    cached = desk.market.announcement_text(ann)
    source = "cache"
    status = "cached" if cached is not None else "not_downloaded"

    if cached is None:
        if meta is None:
            return {
                "ok": False, "announcement_id": ann, "status": "unknown_id",
                "error": "公告表（218,556 行）里没有这个 id",
                "need": "确认 id 是从 profile / knowledge(topic=filings) 拿到的；"
                        "若那只票的公告还没入库，先跑 scripts/backfill.py --datasets anns",
            }
        if fetch:
            from service.adapters import anns_text
            from service.adapters.cninfo_adapter import CninfoAdapter

            result = anns_text.fetch_one(CninfoAdapter(), meta, desk.market.store.root)
            status = str(result.get("status") or "unknown")
            if status == "ok":
                cached = desk.market.announcement_text(ann)
                source = "downloaded"
        if cached is None:
            # Each status has a different next step, so they get different words.
            need = {
                "not_downloaded": f"用 fetch=true 重试，或跑 scripts/fetch_anns_text.py "
                                  f"--symbol {meta.get('ts_code')} --limit 5",
                "no_text_layer": "这份 PDF 是扫描件，没有文字层——只能看原文 pdf_url",
                "too_large": "文件超过 8 MB，脚本会跳过；只能看原文 pdf_url",
                "no_url": "公告表里这条没有 pdf_url，无法下载",
            }.get(status.split(":")[0], "下载失败，可重试；仍失败就只看 pdf_url")
            return {
                "ok": False, "announcement_id": ann, "status": status,
                "title": meta.get("title"), "pub_date": meta.get("pub_date"),
                "pdf_url": meta.get("pdf_url"),
                "error": "正文没有拿到", "need": need,
            }

    text = str(cached.get("text") or "")
    total = len(text)
    end = _fit_bytes(text, start, size) if start < total else start
    window = text[start:end] if start < total else ""
    done = end >= total
    window_bytes = len(window.encode("utf-8"))

    pages_read = cached.get("pages_read")
    pages_total = cached.get("pages_total")
    page_note = None
    if isinstance(pages_read, int) and isinstance(pages_total, int):
        if pages_read < pages_total:
            page_note = (f"原文 {pages_total} 页，抽取了前 {pages_read} 页——"
                         "只出现在后面的数字（附注、明细表）这里没有")
        elif pages_read == pages_total:
            page_note = f"原文 {pages_total} 页，全部抽取"
    elif isinstance(pages_read, int):
        # Old cache entries (fetched before `pages_total` was recorded) can still
        # be read honestly: 12 pages is the extractor's ceiling, so "12" means
        # "possibly longer" while "3" means "that was all of it".
        if pages_read >= MAX_PAGES:
            page_note = (f"抽取了前 {pages_read} 页（脚本上限），原文可能更长——"
                         "只出现在后面的内容这里没有")
        else:
            page_note = f"抽取了 {pages_read} 页（旧缓存没记总页数）"

    out = {
        "ok": True,
        "announcement_id": ann,
        "ts_code": (meta or {}).get("ts_code") or cached.get("ts_code"),
        "title": (meta or {}).get("title") or cached.get("title"),
        "pub_date": (meta or {}).get("pub_date") or cached.get("pub_date"),
        # Same value as `pub_date`, under the name every other response uses for
        # "when this was true". A filing's answer is true as of the day it was
        # published — and without it a page of prose has no date at all.
        "as_of": (meta or {}).get("pub_date") or cached.get("pub_date"),
        "category": (meta or {}).get("category") or cached.get("category"),
        "kind": (meta or {}).get("kind"),
        "pdf_url": (meta or {}).get("pdf_url") or cached.get("pdf_url"),
        "chars": total,
        "offset": start,
        "returned": len(window),
        "bytes": window_bytes,
        "text": window,
        "done": done,
        "next_offset": None if done else end,
        "remaining": max(0, total - end),
        # How many calls the whole document costs at this window size — the
        # reader can decide to stop at page one instead of discovering it later.
        "pages_at_this_size": max(1, -(-total // size)) if size else 1,
        "pages_read": pages_read,
        "pages_total": pages_total,
        # 这两个是"覆盖到哪"的三个数之一。pages_total 为 null 时，读者无从判断
        # "12 页"是全文还是抓取上限——写清楚，否则这个 null 会被读成"没有页数"。
        "pages_total_note": (None if isinstance(pages_total, int) else
                             f"旧缓存没记总页数；{MAX_PAGES} 页是抓取上限，不是全文"),
        "pages_empty": cached.get("pages_empty"),
        "coverage": page_note,
        "fetched": source,
        "basis": ("正文是从巨潮 PDF 抽取的纯文本：段落顺序对，但表格会塌成一行行的数字，"
                  "金额、股数、比例这类需要对齐看的数字要回原文 pdf_url 核。"
                  "日期只有日级精度（巨潮对多数公司返回午夜）。"),
        "note": ("offset 是本响应的唯一续读依据：下一段用 next_offset，不要自己加 chars——"
                 "窗口在句读处收口，长度并不固定。"),
    }
    if clamped:
        out["window_capped"] = {"asked_for": chars, "served": size,
                                "why": "单次 400-6,000 字；14,000 字的正文一次装不进 16 KB"}
    if not done and len(window) < size:
        # Short window, document not finished: the byte budget cut it, not the
        # text. Say so, because "4,000 字" promised and 3,400 delivered looks
        # like a bug or, worse, like the paragraph ended there.
        out["bytes_limited"] = {
            "asked_chars": size, "served_chars": len(window),
            "bytes": window_bytes, "budget": BYTE_BUDGET,
            "why": f"这次窗口按字节收口：{window_bytes} B ≤ {BYTE_BUDGET} B 预算。"
                   "中文多的一页装得少，数字多的一页装得多，续读只看 next_offset",
        }
    if not window and start >= total:
        out["note"] = f"offset {start} 已超过正文长度 {total}，这一段是空的。"
    return out
