"""Text is data: it needs an id, a source, and a clock."""

from __future__ import annotations

from service.adapters.text import normalize, parse_time, query, to_ts_code


def test_codes_and_times_are_normalised():
    assert to_ts_code("002966") == "002966.SZ"
    assert to_ts_code("600000.SH") == "600000.SH"
    assert to_ts_code("300750") == "300750.SZ"
    assert to_ts_code("688981") == "688981.SH"
    assert to_ts_code("830799") == "830799.BJ"
    assert to_ts_code("") == ""

    assert parse_time("2026-10-23 18:40") == ("20261023", "18:40")
    assert parse_time("2026102310:00") == ("20261023", "10:00")
    assert parse_time("2026/10/23 20:15:00") == ("20261023", "20:15")
    assert parse_time("2026-10-23") == ("20261023", "00:00")
    assert parse_time("不是时间") is None


def test_records_without_a_title_or_a_time_are_rejected():
    assert normalize({"title": "", "pub_time": "2026-10-23 09:00"}) is None
    assert normalize({"title": "有标题但没时间"}) is None
    row = normalize({"ts_code": "002966", "title": "  苏州银行  业绩快报 ",
                     "pub_time": "2026-10-23 18:40", "source": "上交所",
                     "kind": "announcement", "sentiment": 3})
    assert row["title"] == "苏州银行 业绩快报"
    assert row["kind"] == "announcement"
    assert row["sentiment"] == 1.0, "sentiment must be clamped, not trusted"
    assert row["news_id"]


def test_same_story_twice_gets_one_id():
    base = {"ts_code": "002966", "title": "同一件事", "pub_time": "2026-10-23 18:40",
            "source": "上交所", "url": "https://example.invalid/a"}
    assert normalize(base)["news_id"] == normalize(dict(base))["news_id"]


class _Store:
    def __init__(self, frame):
        self._frame = frame

    def read(self, dataset, start=None, end=None, **kwargs):
        return self._frame


def test_pit_cutoff_excludes_text_published_after_the_close():
    import pandas as pd

    frame = pd.DataFrame([
        {"news_id": "a", "pub_date": "20261023", "pub_time": "14:05", "ts_code": "002966.SZ",
         "title": "盘中新闻", "summary": "", "source": "证券时报", "url": "",
         "kind": "news", "tier": "media", "sentiment": None, "tags": ""},
        {"news_id": "b", "pub_date": "20261023", "pub_time": "18:40", "ts_code": "002966.SZ",
         "title": "收盘后公告", "summary": "", "source": "上交所", "url": "",
         "kind": "announcement", "tier": "official", "sentiment": None, "tags": ""},
    ])
    store = _Store(frame)

    same_day = query(store, day="20261023", symbol="002966.SZ")
    titles = [i["title"] for i in same_day["items"]]
    assert titles == ["盘中新闻"], "an 18:40 announcement cannot inform a decision at 15:00"

    next_day = query(store, day="20261026", symbol="002966.SZ")
    assert set(titles := [i["title"] for i in next_day["items"]]) == {"盘中新闻", "收盘后公告"}


def test_empty_store_says_so_instead_of_returning_nothing():
    import pandas as pd

    result = query(_Store(pd.DataFrame()), day="20261026", symbol="002966.SZ")
    assert result["ok"] is False
    assert "文本库为空" in result["error"]
    assert result["coverage"]["items"] == 0


def test_one_headline_stored_under_three_symbols_is_returned_once():
    """`news_id` includes the code, so a market-wide story is one row per symbol.

    The vendor serves the same 午评 to every symbol it is asked about. Returning
    it three times reads as three independent reports of one fact, and repetition
    is the cheapest thing to mistake for corroboration.
    """
    import pandas as pd

    def row(code: str, news_id: str) -> dict:
        return {"news_id": news_id, "pub_date": "20261026", "pub_time": "11:52",
                "ts_code": code, "title": "10月26日午评：创业板指半日收跌1.65%",
                "summary": "", "source": "东财", "url": "", "kind": "news",
                "tier": "media", "sentiment": None, "tags": ""}

    frame = pd.DataFrame([row("002966.SZ", "a"), row("600000.SH", "b"),
                          row("300750.SZ", "c")])
    result = query(_Store(frame), day="20261026")

    assert len(result["items"]) == 1
    assert result["coverage"]["items"] == 3, "items stays the raw window count"
    assert result["coverage"]["distinct_items"] == 1
    assert result["coverage"]["duplicate_rows"] == 2


def test_symbol_filter_runs_before_shared_headline_deduplication():
    import pandas as pd

    def row(code: str, news_id: str) -> dict:
        return {"news_id": news_id, "pub_date": "20261026", "pub_time": "11:52",
                "ts_code": code, "title": "同一则市场新闻", "summary": "",
                "source": "东财", "url": "", "kind": "news", "tier": "media",
                "sentiment": None, "tags": ""}

    frame = pd.DataFrame([row("600000.SH", "a"), row("002966.SZ", "b")])
    result = query(_Store(frame), day="20261026", symbol="002966.SZ",
                   include_market=False)
    assert len(result["items"]) == 1
    assert result["items"][0]["ts_code"] == "002966.SZ"
    assert result["coverage"]["distinct_items"] == 1, "coverage remains market-wide"
