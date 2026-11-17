"""The per-name dossier: every block present, every gap stated.

The failure mode this file guards against is not a crash — it is a plausible
number. A percentile of 0.0 reads as "cheapest in three years" and is what a
NaN produces if it reaches `(series <= value).mean()`. A fundamentals block
that silently uses a report announced after the decision date is a look-ahead
that no other test in the suite would catch.
"""

import math

import pandas as pd
import pytest

from service.trading import profile
from service.trading.profile import _pct_rank


def test_percentile_treats_a_missing_value_as_missing():
    series = pd.Series([1.0, 2.0, 3.0] * 40)
    assert _pct_rank(series, 2.0) == pytest.approx(0.667, abs=0.01)
    assert _pct_rank(series, float("nan")) is None, "NaN must not become 0.0"
    assert _pct_rank(series, None) is None


def test_percentile_needs_a_window_to_mean_anything():
    assert _pct_rank(pd.Series([1.0, 2.0, 3.0]), 2.0) is None


def test_every_block_is_either_given_or_explicitly_omitted(desk):
    """十三块加起来 16.7 KB，装不进 16 KB 的响应里——所以默认会给一部分。

    契约不是"每块都在"，而是"每一块要么给你、要么明确说没给"：`omitted_sections`
    与响应里的块合起来必须是全体，否则读者会把"没给"读成"没有"。
    """
    symbol = desk.market.daily(desk.market.latest_bar_date())["ts_code"].iloc[0]
    out = profile.build(desk, symbol)

    assert out["ok"] is True
    given = [name for name in profile.BLOCK_ORDER if name in out]
    omitted = out.get("omitted_sections") or []
    assert sorted(given + omitted) == sorted(profile.BLOCK_ORDER)
    assert set(given) & set(omitted) == set()
    assert len(given) >= 4, "至少要有身份、可交易性、仓位这些底线块"
    if omitted:
        assert "sections=" in out["size_note"]

    # Anything declared missing must actually say why. A block that reports
    # `available: false` with no reason is indistinguishable from a bug, and a
    # nested one (`ownership.unlocks`) is the easiest place to hide that.
    for name in out["missing"]:
        node = out
        for part in name.split("."):
            node = node[part]
        assert node.get("reason"), f"{name} is missing but gives no reason"


def test_an_unknown_symbol_is_an_error_not_an_empty_profile(desk):
    out = profile.build(desk, "999999.XX")
    assert out["ok"] is False and out["error"]


def test_the_profile_reports_the_same_return_the_market_does(desk):
    """Two implementations of "the 250-day return" must not disagree."""
    symbol = desk.market.daily(desk.market.latest_bar_date())["ts_code"].iloc[0]
    out = profile.build(desk, symbol)
    if not out["technicals"].get("available"):
        pytest.skip("not enough history for this symbol")

    day = out["as_of"]
    block = out["technicals"]["return_pct"]
    for window in ("20d", "60d"):
        if window not in block:
            continue
        expected = desk.market.pct_change(symbol, day, int(window[:-1]))
        assert block[window] == pytest.approx(expected * 100, abs=0.02), window


def test_fundamentals_do_not_read_a_report_before_it_was_announced(desk, monkeypatch):
    """Q2 published on 2026-08-28 is unknown on 2026-07-15."""
    frame = pd.DataFrame({
        "ts_code": ["600000.SH", "600000.SH"],
        "ann_date": ["20260428", "20260828"],
        "end_date": ["20260331", "20260630"],
        "roe": [10.0, 99.0],
        "or_yoy": [1.0, 2.0],
        "netprofit_yoy": [1.0, 2.0],
    })
    monkeypatch.setattr(desk.data, "read", lambda *a, **k: frame)

    early = profile._fundamentals(desk, "600000.SH", "20260715")
    late = profile._fundamentals(desk, "600000.SH", "20260901")

    assert early["available"] is True
    assert early["latest_period"] == "20260331"
    assert early["metrics"]["roe"] == 10.0
    assert late["latest_period"] == "20260630"
    assert late["metrics"]["roe"] == 99.0


def test_fundamentals_before_any_announcement_are_absent_not_zero(desk, monkeypatch):
    frame = pd.DataFrame({
        "ts_code": ["600000.SH"], "ann_date": ["20260428"], "end_date": ["20260331"],
        "roe": [10.0], "or_yoy": [1.0], "netprofit_yoy": [1.0],
    })
    monkeypatch.setattr(desk.data, "read", lambda *a, **k: frame)

    out = profile._fundamentals(desk, "600000.SH", "20260101")
    assert out["available"] is False
    assert "20260428" in out["reason"], "say when the first report arrives"
    assert "metrics" not in out


@pytest.mark.parametrize(("section", "dataset"), [
    ("fundamentals", "fina_indicator"),
    ("events", "dividend"),
])
def test_profile_read_failures_are_errors_not_empty_results(desk, monkeypatch, section, dataset):
    symbol = desk.market.daily(desk.market.latest_bar_date())["ts_code"].iloc[0]
    real = desk.data.read

    def broken(name, *args, **kwargs):
        if name == dataset:
            raise ValueError(f"{dataset} parquet unavailable")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(desk.data, "read", broken)
    out = profile.build(desk, symbol, sections=[section])

    block = out[section]
    assert block["available"] is False
    assert block["status"] == "error"
    assert f"{dataset} parquet unavailable" in block["reason"]
    assert section in out["missing"]
    assert out["errors"][section] == block["reason"]
    assert out["status"] == "degraded"


def test_fundamental_empty_table_is_empty_not_an_error(desk, monkeypatch):
    monkeypatch.setattr(desk.data, "read", lambda *_args, **_kwargs: pd.DataFrame())

    out = profile._fundamentals(desk, "600000.SH", "20261112")

    assert out["available"] is False
    assert out["status"] == "empty"
    assert out["reason"] == "fina_indicator 为空"


def test_the_profile_carries_no_verdict(desk):
    """No score, no rating, no advice — the desk describes, the model decides."""
    symbol = desk.market.daily(desk.market.latest_bar_date())["ts_code"].iloc[0]
    out = profile.build(desk, symbol)

    banned = {"score", "rating", "recommendation", "advice", "verdict",
              "target_price", "conviction", "signal", "buy", "sell"}
    seen: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                seen.add(str(key).lower())
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(out)
    assert not (seen & banned), f"profile invented a verdict field: {seen & banned}"


def test_technicals_use_adjusted_prices(desk):
    """A 250-day return computed on raw close is wrong for 45% of the market."""
    symbol = desk.market.daily(desk.market.latest_bar_date())["ts_code"].iloc[0]
    out = profile.build(desk, symbol)
    if not out["technicals"].get("available"):
        pytest.skip("no history")
    hist = desk.market.history(symbol, out["as_of"], 251)
    raw = float(hist["close"].iloc[-1] / hist["close"].iloc[0] - 1)
    adj = float(hist["adj_close"].iloc[-1] / hist["adj_close"].iloc[0] - 1)
    reported = out["technicals"]["return_pct"].get("250d")
    if reported is None:
        pytest.skip("less than 250 sessions")
    # The reported figure tracks the adjusted series, never the raw one. If the
    # name had no corporate action in the window the two agree, so a name with a
    # dividend is the only case that proves anything — the assertion is written
    # against the adjusted series either way.
    assert math.isclose(reported / 100, adj, abs_tol=2e-3), (raw, adj)


def test_every_reported_fundamental_field_exists_in_the_table():
    """A field name that silently misses turns into a null nobody questions.

    The first version asked for `ocf_to_or`; it is not a column in
    `fina_indicator`, so the cash-flow line read null for every name for every
    quarter and looked like a data gap rather than a typo.
    """
    from pathlib import Path

    import pandas as pd

    path = Path(__file__).resolve().parents[2] / "data" / "parquet" / "fina_indicator" / "all.parquet"
    if not path.exists():
        pytest.skip("fina_indicator not backfilled in this checkout")
    columns = set(pd.read_parquet(path).columns)

    missing = [key for key, _ in profile.FINA_FIELDS if key not in columns]
    assert not missing, f"profile 报了表里没有的字段：{missing}"


def test_the_fundamental_labels_match_the_columns_they_describe():
    """`roe` is the diluted figure and `roe_waa` is the weighted one."""
    labels = dict(profile.FINA_FIELDS)
    assert "加权" in labels["roe_waa"]
    assert "摊薄" in labels["roe"]


def test_news_is_labelled_with_what_it_is_actually_about(desk):
    """`ts_code` on the news table is a tag, not an attribution.

    Ten rows are tagged to 002966.SZ and their titles are about the banking
    sector, fund flows and one about 中国石油. Called `recent_filings`, a model
    reads them as the company's announcements and attributes sector flow to one
    bank. Every row now carries the scope it was judged to have.
    """
    symbol = desk.market.daily(desk.market.latest_bar_date())["ts_code"].iloc[0]
    out = profile.build(desk, symbol)
    events = out["events"]

    assert "recent_news" in events and "recent_filings" not in events
    name = desk.market.names().get(symbol, "")
    for item in events["recent_news"]:
        assert item["scope"] in ("company", "industry", "market", "tagged_only")
        if name and name not in item["title"]:
            assert item["scope"] != "company", item["title"]
    assert "不是公告归属" in events["news_note"]


def test_news_scope_names_the_word_it_judged_by():
    """Two labels were wrong in the first rule: 银行间 is not a bank, 沪指 is news.

    `scope` alone cannot be checked — a reader who sees `industry` on a funding
    market story has no way to tell a bad label from a bad title. The basis word
    makes the error visible and the rule testable.
    """
    from service.trading.profile import news_scope

    assert news_scope("银行间市场资金面转松", "平安银行", "银行") == ("tagged_only", "")
    assert news_scope("沪指站上 3600 点，两市成交 1.2 万亿", "", "")[0] == "market"
    assert news_scope("平安银行获批发行 300 亿资本债", "平安银行", "银行") == (
        "company", "平安银行")
    # 万科A is stored with the share-class letter; the headline drops it.
    assert news_scope("万科前 10 月销售金额同比转正", "万科A", "房地产") == ("company", "万科")


def test_fundamentals_state_the_year_to_date_basis(desk):
    """A YTD series resets in Q1; without the label the trend reads as a collapse."""
    symbol = desk.market.daily(desk.market.latest_bar_date())["ts_code"].iloc[0]
    fundamentals = profile.build(desk, symbol)["fundamentals"]
    if not fundamentals.get("available"):
        pytest.skip("no fundamentals for this symbol")

    assert "YTD" in fundamentals["basis"] or "累计" in fundamentals["basis"]
    assert isinstance(fundamentals["is_year_end"], bool)
    assert fundamentals["missing_metrics"] == [
        k for k, v in fundamentals["metrics"].items() if v is None
    ]
    for row in fundamentals["trend"]:
        assert "is_year_end" in row and "roe_ytd" in row


def test_valuation_percentiles_carry_a_direction(desk):
    """A high PE percentile is expensive; a high yield percentile is cheap.

    Reporting both as a bare percentile invites reading a 3rd-percentile yield as
    "cheap" when it means the opposite.
    """
    symbol = desk.market.daily(desk.market.latest_bar_date())["ts_code"].iloc[0]
    valuation = profile.build(desk, symbol)["valuation"]
    if not valuation.get("available"):
        pytest.skip("no valuation series")

    direction = valuation["direction"]
    assert "贵" in direction["pe_ttm"]
    assert "便宜" in direction["dv_ttm"], "股息率的方向和 PE 相反，必须写出来"


def test_sentiment_says_how_much_it_could_read(desk):
    """"Not on the list" and "the list was never fetched" are different answers."""
    symbol = desk.market.daily(desk.market.latest_bar_date())["ts_code"].iloc[0]
    sentiment = profile.build(desk, symbol, sections=["sentiment"])["sentiment"]

    checked = sentiment["checked"]
    assert checked["pool_day"]
    assert 0 <= checked["pools_read"] <= 3
    assert checked["lhb_sessions_asked"] >= 0
    assert checked["lhb_sessions_read"] <= checked["lhb_sessions_asked"]
    if checked["lhb_sessions_read"] and sentiment["lhb_20d"] is None:
        assert "未上榜" in sentiment["lhb_note"]


def test_relative_strength_decomposes_the_return(desk):
    """+15% in 60 sessions means nothing until you know what the sector did."""
    symbol = desk.market.daily(desk.market.latest_bar_date())["ts_code"].iloc[0]
    out = profile.build(desk, symbol)
    rs = out["relative_strength"]
    if not rs.get("available"):
        pytest.skip(rs.get("reason", "no cross-section"))

    for window, row in rs["window"].items():
        assert row["vs_industry_pp"] == pytest.approx(
            row["stock_pct"] - row["industry_pct"], abs=0.02), window
        assert row["vs_market_pp"] == pytest.approx(
            row["stock_pct"] - row["market_pct"], abs=0.02), window


def test_a_limit_up_name_is_found_in_the_pool(tmp_path, desk):
    """The pool tables key on a bare code; every other table uses a ts_code.

    Comparing them directly meant `sentiment.pools` was empty for *every* symbol:
    three partitions were read, nothing matched, and the block looked like a stock
    that simply was not limit-up. The check ran and could never fire.
    """
    day = desk.market.latest_bar_date()
    try:
        up = desk.data.read_partition("limit_up_pool", day)
    except (KeyError, FileNotFoundError, ValueError):
        pytest.skip("no limit-up pool for today")
    if up is None or up.empty:
        pytest.skip("empty limit-up pool")

    code = str(up.iloc[0]["code"])
    basic = desk.market.basic()
    hit = basic[basic["symbol"].astype(str) == code]
    if hit.empty:
        pytest.skip(f"{code} not in stock_basic")
    symbol = str(hit.iloc[0]["ts_code"])

    out = profile.build(desk, symbol, sections=["sentiment", "tradability"])
    pools = out["sentiment"]["pools"]
    assert "limit_up" in pools, f"{symbol} 在涨停池里，但档案没有报告"
    assert out["tradability"]["to_up_limit_pct"] is not None
    assert abs(out["tradability"]["to_up_limit_pct"]) < 1.0, "封住的票距涨停应接近 0"


def test_ownership_reports_who_holds_the_float(desk):
    """Every other block describes the price; this one describes the holders."""
    basic = desk.market.basic()
    daily = desk.market.daily(desk.market.latest_bar_date())
    symbol = daily["ts_code"].iloc[0]
    out = profile.build(desk, symbol)
    own = out["ownership"]
    assert {"holders", "top10_float", "unlocks", "pledge", "insider"} <= set(own)

    top = own["top10_float"]
    if not top.get("available"):
        pytest.skip(top.get("reason", "no top10 data"))
    assert 0 < top["combined_float_pct"] <= 100
    assert top["raised"] + top["cut"] <= top["holders_reported"]
    if top.get("northbound_holder"):
        assert top["northbound_holder"]["float_pct"] > 0
    assert basic is not None


def test_unlocks_are_dated_in_the_future(desk):
    """The calendar has to run on 解禁日, not on the announcement date."""
    daily = desk.market.daily(desk.market.latest_bar_date())
    day = desk.market.latest_bar_date()
    found = False
    for symbol in daily["ts_code"].head(40):
        own = profile.build(desk, symbol)["ownership"]
        unlocks = own["unlocks"]
        if not unlocks.get("available"):
            continue
        found = True
        assert unlocks["next_float_date"] > day, "解禁日必须在今天之后"
        assert unlocks["days_until"] >= 0
        assert unlocks["within_180d_events"] >= 1
        assert unlocks["next_float_pct_of_total"] is not None
        break
    if not found:
        pytest.skip("no future unlock in the first 40 names")


def test_holder_count_is_reported_with_its_own_date(desk):
    """Ten-day snapshots exist, so a fixed "last quarter" comparison would be wrong."""
    daily = desk.market.daily(desk.market.latest_bar_date())
    day = desk.market.latest_bar_date()
    for symbol in daily["ts_code"].head(40):
        holders = profile.build(desk, symbol)["ownership"]["holders"]
        if not holders.get("available"):
            continue
        assert holders["as_of"] <= day
        assert holders["announced_on"] <= day
        assert holders["holder_count"] > 0
        if holders.get("previous"):
            assert holders["previous"] > 0
            assert holders["change_pct"] is not None
        break


def test_pledge_is_compared_with_the_same_day_market(desk):
    """A pledge ratio means nothing without the cross-section it sits in."""
    frame = profile._read_cached(desk, "pledge_stat")
    if frame is None or frame.empty:
        pytest.skip("pledge_stat not backfilled")
    symbol = str(frame["ts_code"].iloc[0])
    pledge = profile.build(desk, symbol)["ownership"]["pledge"]
    if not pledge.get("available"):
        pytest.skip(pledge.get("reason", "no pledge"))

    assert 0 <= pledge["pledge_ratio_pct"] <= 100
    assert pledge["peers_reporting"] > 0
    assert pledge["market_median_same_day_pct"] is not None
    assert pledge["as_of"] <= desk.market.latest_bar_date()


def test_insider_net_shares_match_the_direction(desk):
    """净减持 with a positive net is a sign error that reads as good news."""
    frame = profile._read_cached(desk, "stk_holdertrade")
    if frame is None or frame.empty:
        pytest.skip("stk_holdertrade not backfilled")
    day = desk.market.latest_bar_date()
    frame = frame.assign(ann_date=frame["ann_date"].astype(str))
    horizon = (pd.Timestamp(day) - pd.Timedelta(days=180)).strftime("%Y%m%d")
    recent = frame[(frame["ann_date"] <= day) & (frame["ann_date"] >= horizon)]
    if recent.empty:
        pytest.skip("no insider trades in the window")

    symbol = str(recent["ts_code"].iloc[0])
    insider = profile.build(desk, symbol)["ownership"]["insider"]
    if not insider.get("available"):
        pytest.skip(insider.get("reason", "no insider block"))

    assert insider["buy_events"] + insider["sell_events"] == insider["events"]
    if insider["net_shares"] > 0:
        assert insider["direction"] == "净增持"
    elif insider["net_shares"] < 0:
        assert insider["direction"] == "净减持"


def test_a_block_transfer_is_not_reported_as_market_selling(desk):
    """31% of a company is a 协议转让; calling it supply overstates it by 10x."""
    frame = profile._read_cached(desk, "stk_holdertrade")
    if frame is None or frame.empty:
        pytest.skip("stk_holdertrade not backfilled")
    big = frame[pd.to_numeric(frame["change_ratio"], errors="coerce") >= 5]
    if big.empty:
        pytest.skip("no block transfer in the table")

    found = False
    for symbol in big["ts_code"].unique()[:10]:
        insider = profile.build(desk, str(symbol))["ownership"]["insider"]
        if not insider.get("available"):
            continue
        if (insider.get("largest_single_pct_of_total") or 0) >= 5:
            found = True
            assert insider.get("caveat"), "大额变动必须附说明"
            assert "不是二级市场" in insider["caveat"]
            break
    assert found, "表里有 >=5% 的变动，但档案没报出来"


# ------------------------------------------------------------------ 申万口径
def test_every_listed_name_carries_a_sw_classification(desk):
    """The taxonomy is one file and one call; a hole in it is a name with no peers."""
    table = desk.market.sw_table()
    if table.empty:
        pytest.skip("index_member not backfilled")

    basic = desk.market.basic()
    listed = set(basic[basic["list_status"] == "L"]["ts_code"].astype(str))
    covered = set(table["ts_code"].astype(str))
    missing = listed - covered
    assert not missing, f"{len(missing)} 只上市股票没有申万分类，例如 {sorted(missing)[:5]}"
    assert table["l1_name"].notna().all()
    assert table["l3_name"].notna().all()


def test_the_comparison_group_is_the_finest_level_with_enough_names(desk):
    """`城商行Ⅲ` (17) is a better benchmark than `银行` (42) — when it exists."""
    table = desk.market.sw_table()
    if table.empty:
        pytest.skip("index_member not backfilled")

    group = desk.market.industry_peers("002966.SZ")
    if not group.get("available"):
        pytest.skip(group.get("reason", "no classification"))
    assert group["level"] == "L3"
    assert group["skipped"] == [], "选中的就是最细的一级，不该有跳过的"
    assert 8 <= group["size"] <= 60
    assert "002966.SZ" in group["members"]


def test_a_too_small_bucket_is_skipped_and_the_reason_is_kept(desk):
    """工商银行 is one of six state-owned banks; six is not a median."""
    table = desk.market.sw_table()
    if table.empty:
        pytest.skip("index_member not backfilled")

    group = desk.market.industry_peers("601398.SH")
    if not group.get("available"):
        pytest.skip(group.get("reason", "no classification"))
    if group["level"] == "L3":
        pytest.skip("国有大型银行Ⅲ is large enough in this dataset")

    assert group["skipped"], "跳过了更细的一级，却没有说明原因"
    assert group["skipped"][0]["why"] == "太小"
    assert group["skipped"][0]["size"] < 8


def test_peers_reports_the_bucket_it_used(desk):
    """A median is meaningless without the set it was taken over."""
    out = profile.build(desk, "600519.SH", sections=["peers", "identity"])
    peers = out["peers"]
    if not peers.get("available"):
        pytest.skip(peers.get("reason", "no peers"))

    assert peers["taxonomy"] in ("申万2021", "stock_basic.industry")
    assert peers["level"] in ("L1", "L2", "L3", "vendor")
    assert peers["peer_count"] <= out["identity"]["comparison_group"]["size"]
    if peers["taxonomy"] == "申万2021":
        assert peers["industry"].endswith(("Ⅰ", "Ⅱ", "Ⅲ")) or peers["level"] == "L1"


def test_without_the_sw_classification_the_block_widens_to_the_vendor_label(desk, monkeypatch):
    """No classification table is a reason to widen, not to return nothing."""
    import pandas as pd

    monkeypatch.setattr(desk.market, "sw_table", lambda: pd.DataFrame())
    out = profile.build(desk, "600519.SH", sections=["peers", "identity"])
    peers = out["peers"]
    if not peers.get("available"):
        pytest.skip(peers.get("reason", "vendor industry missing too"))

    assert peers["taxonomy"] == "stock_basic.industry"
    assert peers["level"] == "vendor"
    assert peers["peer_count"] > 0
    assert out["identity"]["comparison_group"] is None


def test_universe_can_filter_on_the_same_taxonomy_the_profile_uses(desk):
    """"城商行" must mean the same 17 names in both places, or none of it lines up."""
    from service.trading.universe import browse

    day = desk.market.latest_bar_date()
    group = desk.market.industry_peers("002966.SZ")
    if not group.get("available") or group.get("level") != "L3":
        pytest.skip("no 申万 L3 group for this name")

    out = browse(desk.data, day, industry="城商行", limit=100)
    if out["universe_size"] == 0:
        pytest.skip("index_member not backfilled")
    assert out["universe_size"] == group["size"], (
        f"筛选得到 {out['universe_size']} 只，档案的比较组是 {group['size']} 只")
    for row in out["rows"][:3]:
        assert row["sw_l3"] == group["name"]
        assert row["sw_l1"]


def test_margin_falls_back_to_the_last_published_session(desk):
    """Balance is a stock, not a flow: yesterday's number still answers today.

    The exchanges publish margin detail after the close, so a same-day lookup
    returns nothing for hours. That was rendered as `available: false`
    ("not a margin target") for every name until publication — a data-timing
    fact reported as a fact about the instrument.
    """
    day = desk.market.latest_bar_date()
    row = desk.market.margin_row("601398.SH", day)
    if not row:
        pytest.skip("margin_detail not backfilled")

    assert str(row["trade_date"]) <= day
    assert row.get("_lag_sessions") is not None
    assert float(row["rzye"]) > 0

    margin = profile.build(desk, "601398.SH")["flow"]["margin"]
    assert margin["available"] is True
    assert margin["as_of"] <= day
    assert margin["lag_sessions"] >= 0
    assert "存量" in margin["note"]


def test_a_name_with_no_margin_record_says_so_without_claiming_a_lag(desk):
    """No row at all is a different statement from a stale row."""
    day = desk.market.latest_bar_date()
    row = desk.market.margin_row("000001.SZ", day)
    if not row:
        pytest.skip("margin_detail not backfilled")
    # A symbol that has never appeared must return nothing, not the previous
    # symbol's row: the fallback walks back by date, not by another key.
    assert desk.market.margin_row("999999.XX", day) == {}


# --------------------------------------------------------------- 公告（巨潮）
def test_announcements_carry_a_category_and_a_day_precision_note(desk):
    """Titles alone cannot separate 「减持计划」 from 「减持实施完毕」."""
    out = profile.build(desk, "000001.SZ")
    events = out["events"]
    if "announcements" not in events:
        pytest.skip("anns not backfilled")

    assert "正文" in events["announcements_note"]
    assert "日级精度" in events["announcements_note"]
    for item in events["announcements"]:
        assert item["announcement_id"].isdigit()
        assert len(item["pub_date"]) == 8
        assert item["category"]
        assert item["kind"] in ("公告", "决议", "意见", "通知", "报告", "说明", "函件")
        assert item["stage"] in (None, "计划", "进展", "完成", "终止", "调整", "更正")
        assert isinstance(item["paperwork"], bool)
        # basis 只留真正命中过词的那几项：四个标签都没命中时整个键不出现
        # （六条公告里这是常态，而四个空串是每行 60 字节的占位）。
        assert set(item.get("basis", {})) <= {"category", "kind", "stage", "paperwork"}
        assert all(isinstance(v, str) and v for v in item.get("basis", {}).values())
        assert item["pdf_url"].startswith("http://static.cninfo.com.cn/")
        assert set(item["text"]) >= {"available"}
    # The label is derived from the title, so at least one filing must be able to
    # say which word it came from — otherwise the whole `basis` block is a stub.
    assert any(i["basis"]["category"] for i in events["announcements"])


def test_announcement_text_is_empty_until_the_pdf_is_downloaded(desk):
    """No cache is 'not downloaded', never 'empty filing'."""
    out = profile.build(desk, "600519.SH")
    items = out["events"].get("announcements") or []
    if not items:
        pytest.skip("no announcements for this symbol")

    uncached = [i for i in items if not i["text"]["available"]]
    for item in uncached:
        assert "未下载" in item["text"]["reason"]


def test_a_cached_filing_contributes_an_excerpt_and_its_length(desk, monkeypatch):
    """The excerpt is truncated on purpose; `chars` keeps the real size visible."""

    def fake_text(announcement_id):
        return {"chars": 9000, "pages_read": 9, "pages_empty": 1,
                "text": "证券代码：000001\n\n平安银行股份有限公司 2026 年第三季度报告\n" + "正文" * 400}

    monkeypatch.setattr(desk.market, "announcement_text", fake_text)
    items = profile.build(desk, "000001.SZ")["events"]["announcements"]
    if not items:
        pytest.skip("no announcements for this symbol")

    first = items[0]["text"]
    assert first["available"] is True
    assert first["chars"] == 9000
    assert first["pages_read"] == 9
    assert first["pages_empty"] == 1
    assert first["truncated"] is True
    assert len(first["excerpt"]) <= 420
    assert "\n" not in first["excerpt"], "换行在 payload 里只是噪声"


# ------------------------------------------------------------- 分时（免费源）
def test_intraday_describes_the_session_only_when_bars_were_collected(desk):
    """Empty means 'not collected'. Reporting zeros would read as a flat day."""
    out = profile.build(desk, "600519.SH", sections=["intraday"])["intraday"]
    if not out.get("available"):
        pytest.skip(out.get("reason", "no bars"))

    assert out["bars"] > 0
    # 三个源都可能出现：全市场盘后走 baostock，缺的日子按需补东财/腾讯。
    # 断言"必须是某一家"会把这个测试绑死在过去的数据来源上——它要测的是
    # "有柱就说得清形状"，不是"从哪儿抓的"。
    assert out["source"] in ("baostock", "eastmoney", "tencent")
    assert 0.0 <= out["first_30min_volume_share"] <= 1.0
    assert out["worst_drawdown_from_high_pct"] <= 0
    assert out["session_low"] <= out["close"] <= out["session_high"]
    if out["vwap"] is None:
        assert "vwap_note" in out, "没有成交额就要说明为什么 VWAP 是空的"


@pytest.mark.parametrize("how,fragment", [
    ("not_collected", "没有联网抓"),
    ("unavailable:eastmoney; tencent", "都没有返回"),
    ("provider_has_no_20200102", "能力边界"),
])
def test_why_there_are_no_bars_is_part_of_the_answer(desk, monkeypatch, how, fragment):
    """三种"没有"的下一步完全不同：叫一次带 fetch、换个源、还是根本做不到。

    合成一句话"当日没有 5 分钟线"会让读者一直重试一个永远不会成功的请求——
    免费分钟线只回看 32 个交易日，2020 年那天谁也补不回来。
    """
    monkeypatch.setattr("service.trading.intraday.collect",
                        lambda *a, **k: (pd.DataFrame(), how))

    out = profile.build(desk, "600519.SH", sections=["intraday"])["intraday"]

    assert out["available"] is False
    assert fragment in out["reason"]


def test_atr_says_what_unit_it_is_in(desk):
    """`atr20: 25.92` 在 1,392 元的票上是 1.9%，但字段名没说单位。

    读者拿 `last - 2*atr20` 算止损时，若把复权价空间里的 ATR 与真实价相减，
    除权股上会得到错的数。百分比由服务端算好，别让读者自己换算。
    """
    out = profile.build(desk, "600519.SH", sections=["technicals"])["technicals"]
    if not out.get("available"):
        pytest.skip(out.get("reason", "no technicals"))

    # 块内的百分比必须自洽：ma20.price_vs_pct 反推出的收盘价，
    # 应当正好是 atr20 / atr20_pct 用的那个价。
    implied_close = out["ma"]["ma20"]["value"] * (1 + out["ma"]["ma20"]["price_vs_pct"] / 100)
    assert out["atr20"] > 0
    assert out["atr20_pct"] == pytest.approx(out["atr20"] / implied_close * 100, abs=0.05)
    assert 0 < out["atr20_pct"] < 30
    assert "价格单位" in out["note"]


def test_two_reader_implementations_never_share_a_cache_key():
    """A fingerprint must be identity, not a memory address.

    ``id(fn)`` is reused the moment the object is collected: creating and
    dropping 50 lambdas in a loop produced **one** distinct id. The frame cache
    lives on ``desk.market`` and is shared for the whole test session, so a
    replacement reader inherited an entry built by the real dataset — the
    "empty table is empty" case answered ``available: true`` in a full run while
    passing on its own.
    """
    from types import SimpleNamespace

    from service.trading import profile

    desk = SimpleNamespace(data=SimpleNamespace())
    seen = []
    for _ in range(50):
        desk.data.read = lambda *_args, **_kwargs: None
        seen.append(profile._reader_fingerprint(desk, "read"))
        del desk.data.read

    assert len(set(seen)) == len(seen), (
        f"{len(seen) - len(set(seen))} of {len(seen)} distinct readers shared a "
        "fingerprint; a cache keyed on it will serve the wrong frame")

    reader = lambda *_args, **_kwargs: None
    desk.data.read = reader
    assert profile._reader_fingerprint(desk, "read") == profile._reader_fingerprint(
        desk, "read"), "the same reader must keep one fingerprint"
