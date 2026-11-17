"""Synthetic regressions for profile, universe and screen; no real data store."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from service.trading import profile
from service.trading.screen import ScreenConfig, screen
from service.trading.universe import browse


class FrameStore:
    def __init__(self, daily, basic=None, daily_basic=None):
        self.daily = daily
        self.basic = pd.DataFrame() if basic is None else basic
        self.daily_basic = pd.DataFrame() if daily_basic is None else daily_basic

    def read(self, dataset, start=None, end=None, **kwargs):
        frame = {
            "daily": self.daily,
            "stock_basic": self.basic,
            "daily_basic": self.daily_basic,
        }.get(dataset, pd.DataFrame()).copy()
        if frame.empty:
            return frame
        if dataset == "daily":
            if start:
                frame = frame[frame["trade_date"].astype(str) >= str(start)]
            if end:
                frame = frame[frame["trade_date"].astype(str) <= str(end)]
        return frame


def universe_store() -> FrameStore:
    days = ["20260105", "20260106", "20260107"]
    daily = pd.DataFrame([
        {"ts_code": code, "trade_date": day, "close": close,
         "pre_close": close, "amount": 100_000}
        for code, close in (("600001.SH", 10.0), ("600002.SH", 20.0))
        for day in days
    ])
    basic = pd.DataFrame([
        {"ts_code": "600001.SH", "name": "甲股份", "industry": "银行"},
        {"ts_code": "600002.SH", "name": "乙股份", "industry": "软件"},
    ])
    return FrameStore(daily, basic)


def test_browse_honors_a_zero_price_upper_bound():
    out = browse(universe_store(), "20260107", max_price=0)
    assert out["universe_size"] == 0
    assert out["rows"] == []


def test_browse_returns_use_the_exchange_reference_price_on_an_ex_date():
    daily = pd.DataFrame([
        {"ts_code": "600001.SH", "trade_date": "20260106", "close": 10.0,
         "pre_close": 10.0, "amount": 100_000},
        {"ts_code": "600001.SH", "trade_date": "20260107", "close": 9.0,
         "pre_close": 9.0, "amount": 100_000},
    ])
    out = browse(FrameStore(daily), "20260107", exclude_st=False)
    assert out["rows"][0]["ret_1d"] == 0.0


def test_browse_does_not_need_stock_basic_to_keep_the_daily_universe():
    store = universe_store()
    store.basic = pd.DataFrame()
    out = browse(store, "20260107", exclude_st=False)
    assert out["universe_size"] == 2
    assert {row["symbol"] for row in out["rows"]} == {"600001.SH", "600002.SH"}
    assert all(row["name"] is None for row in out["rows"])


class ScreenMarket:
    def __init__(self, list_date="20200101"):
        self.codes = ["600001.SH", "688001.SH", "689001.SH"]
        self.list_date = list_date
        self.universe = SimpleNamespace(st_map=lambda codes, day: {code: False for code in codes})

    def daily(self, date):
        return pd.DataFrame([
            {"ts_code": code, "trade_date": date, "close": 10.0,
             "pre_close": 9.9, "vol": 10_000, "amount": 100_000}
            for code in self.codes
        ])

    def limits(self, date):
        return pd.DataFrame(columns=["ts_code", "up_limit", "down_limit"])

    def basic(self):
        return pd.DataFrame([
            {"ts_code": code, "name": code, "list_date": self.list_date}
            for code in self.codes
        ])

    def adv_map(self, date, days):
        return pd.Series(100_000_000.0, index=self.codes)

    def panel(self, date, days):
        return pd.DataFrame([
            {"ts_code": code, "trade_date": day, "close": close, "adj_close": close}
            for code in self.codes
            for day, close in (("20260105", 9.0), (date, 10.0))
        ])


def test_screen_excludes_star_market_prefixes_not_just_suffixes():
    rows = screen(ScreenMarket(), "20260106")
    assert [row["symbol"] for row in rows] == ["600001.SH"]


@pytest.mark.parametrize("list_date", ["", None])
def test_screen_does_not_treat_a_blank_listing_date_as_an_old_stock(list_date):
    market = ScreenMarket(list_date=list_date)
    market.codes = ["600001.SH"]
    assert screen(market, "20260106") == []


def test_fundamentals_use_the_latest_announced_restatement_not_physical_row_order():
    frame = pd.DataFrame([
        {"ts_code": "600000.SH", "ann_date": "20260515", "end_date": "20260331", "roe": 99.0},
        {"ts_code": "600000.SH", "ann_date": "20260428", "end_date": "20260331", "roe": 10.0},
    ])
    desk = SimpleNamespace(data=SimpleNamespace(read=lambda dataset: frame))
    out = profile._fundamentals(desk, "600000.SH", "20260601")
    assert out["announced_on"] == "20260515"
    assert out["metrics"]["roe"] == 99.0


def test_identity_uses_the_name_known_on_the_requested_day():
    changes = pd.DataFrame([
        {"ts_code": "600000.SH", "name": "历史名", "start_date": "20250101", "change_reason": "更名"},
        {"ts_code": "600000.SH", "name": "未来名", "start_date": "20260301", "change_reason": "更名"},
    ])
    market = SimpleNamespace(
        basic=lambda: pd.DataFrame([{"ts_code": "600000.SH", "name": "当前名", "list_date": "20000101"}]),
        instrument=lambda symbol, day: SimpleNamespace(
            board=SimpleNamespace(value="MAIN"), is_etf=False, is_st=False, name="历史名"),
        names=lambda: {"600000.SH": "当前名"},
        namechange=lambda: changes,
        calendar=SimpleNamespace(between=lambda start, end: []),
        sw_of=lambda symbol: {},
        industry_peers=lambda symbol: {"available": False},
        industry_of=lambda symbol: "银行",
    )
    out = profile._identity(SimpleNamespace(market=market), "600000.SH", "20260110")
    assert out["name"] == "历史名"
    assert all(row["from"] <= "20260110" for row in out["former_names"])


class EventData:
    def read(self, dataset, **kwargs):
        if dataset == "dividend":
            return pd.DataFrame([
                {"ts_code": "600000.SH", "div_proc": "实施", "ann_date": "20260101",
                 "ex_date": "20260105", "cash_div": 1.0, "stk_div": 0.0, "pay_date": ""},
                {"ts_code": "600000.SH", "div_proc": "实施", "ann_date": "20260301",
                 "ex_date": "20260106", "cash_div": 9.0, "stk_div": 0.0, "pay_date": ""},
            ])
        raise KeyError(dataset)


def test_corporate_actions_do_not_use_an_announcement_from_the_future():
    market = SimpleNamespace(
        names=lambda: {"600000.SH": "测试"},
        industry_of=lambda symbol: "银行",
        announcements=lambda symbol, day, limit: [],
    )
    out = profile._events(SimpleNamespace(data=EventData(), market=market), "600000.SH", "20260110")
    assert len(out["corporate_actions"]) == 1
    assert out["corporate_actions"][0]["cash_per_share"] == 1.0
    assert out["recent_news"] == []
    assert out["news_status"] == "error"
    assert "news 读取失败" in out["news_error"]


class OwnershipData:
    def __init__(self, top10=None, share_float=None):
        self.top10 = pd.DataFrame() if top10 is None else top10
        self.share_float = pd.DataFrame() if share_float is None else share_float

    def read(self, dataset):
        return {
            "top10_floatholders": self.top10,
            "share_float": self.share_float,
        }.get(dataset, pd.DataFrame()).copy()


def ownership_desk(data):
    market = SimpleNamespace(_cached=lambda key, build: build(), basic_row=lambda symbol, day: {})
    return SimpleNamespace(data=data, market=market)


def test_ownership_filters_disclosures_and_unlock_announcements_by_as_of():
    top10 = pd.DataFrame([{
        "ts_code": "600000.SH", "end_date": "20251231", "ann_date": "20260430",
        "hold_float_ratio": 20.0, "hold_change": 1.0, "holder_type": "机构",
        "holder_name": "未来披露股东",
    }])
    share_float = pd.DataFrame([{
        "ts_code": "600000.SH", "ann_date": "20260430", "float_date": "20261231",
        "float_ratio": 10.0, "float_share": 100.0, "holder_name": "未来公告股东",
    }])
    out = profile._ownership(ownership_desk(OwnershipData(top10, share_float)), "600000.SH", "20260131")
    assert out["top10_float"]["available"] is False
    assert out["unlocks"]["available"] is False


def test_unlocks_keep_the_reason_when_every_future_row_is_suspect():
    share_float = pd.DataFrame([{
        "ts_code": "600000.SH", "ann_date": "20260101", "float_date": "20261231",
        "float_ratio": 150.0, "float_share": 100.0, "holder_name": "异常行",
    }])
    out = profile._ownership(ownership_desk(OwnershipData(share_float=share_float)),
                             "600000.SH", "20260131")
    assert out["unlocks"]["available"] is False
    assert out["unlocks"]["excluded_suspect_rows"] == 1
    assert "比例超过 100%" in out["unlocks"]["reason"]


def test_missing_market_caps_stay_missing_instead_of_becoming_zero():
    history = pd.DataFrame([{"ts_code": "600000.SH", "trade_date": "20260107"}])
    today = {"total_mv": None, "circ_mv": None}
    market = SimpleNamespace(basic_history=lambda *args: history, basic_row=lambda *args: today)
    out = profile._valuation(SimpleNamespace(market=market), "600000.SH", "20260107", 60)
    assert out["current"]["total_mv_cny"] is None
    assert out["current"]["circ_mv_cny"] is None


def test_negative_current_pe_has_no_cross_sectional_rank():
    symbols = [f"60000{i}.SH" for i in range(5)]
    frame = pd.DataFrame([
        {"ts_code": code, "pe_ttm": -5.0 if i == 0 else float(10 + i),
         "pb": 1.0, "total_mv": 100.0 + i, "turnover_rate": 2.0}
        for i, code in enumerate(symbols)
    ])
    daily = pd.DataFrame([{"ts_code": code, "close": 10.0, "pre_close": 9.0} for code in symbols])
    market = SimpleNamespace(
        basic_day=lambda day: frame,
        industry_peers=lambda symbol: {"available": True, "members": symbols,
                                       "level": "L3", "name": "测试行业", "skipped": []},
        names=lambda: {code: code for code in symbols},
        daily=lambda day: daily,
    )
    out = profile._peers(SimpleNamespace(market=market), symbols[0], "20260107")
    assert out["mine"]["pe_ttm"] == -5.0
    assert out["rank_pct"]["pe_ttm"] is None


def test_a_candidate_states_the_band_it_trades_in():
    """候选行必须自带涨跌幅：600 开头和 300 开头不是同一笔交易。

    同一个"涨 8%"在主板上离涨停还有 2 个点，在创业板上还有 12 个点。
    原先候选没有 `board`，读者只能从前缀推；ST 的 5% 更让这条捷径失效。
    """
    market = ScreenMarket()
    market.codes = ["600001.SH", "300001.SZ"]

    rows = {row["symbol"]: row for row in screen(market, "20260106")}

    assert rows["600001.SH"]["board"] == "MAIN"
    assert rows["600001.SH"]["band"] == 0.10
    assert rows["300001.SZ"]["board"] == "CHINEXT"
    assert rows["300001.SZ"]["band"] == 0.20


def test_a_st_name_keeps_its_five_percent_band_even_when_kept():
    """`exclude_st=False` 时表里就有 ST，而 ST 的涨跌幅是 5%。

    `band` 不能从板块推，只能来自同一个 ST 判定——否则读者会在一个"5% 天花板"
    的标的上按 10% 算止损距离。
    """
    market = ScreenMarket()
    market.codes = ["600001.SH"]
    market.universe = SimpleNamespace(st_map=lambda codes, day: {code: True for code in codes})

    rows = screen(market, "20260106", ScreenConfig(exclude_st=False))

    assert rows[0]["board"] == "MAIN"
    assert rows[0]["band"] == 0.05
