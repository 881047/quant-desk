"""行业横截面：把四张表按成员对齐之后，"哪个行业强"才有交易意义。

单看 5 日涨跌幅会把两件事读成一件事：一个行业 42 只票齐涨 2%，和一只票涨停、
其余 41 只不动，均值都是 +2.4%。所以这张表同时给宽度、涨停家数和主力净流入——
只有内核能算，因为它得把日线、申万成员、涨停价、资金流四张表按 ts_code 对齐。
"""

from __future__ import annotations

import pandas as pd

from service.trading.industry import panel


class FakeStore:
    def __init__(self, flow: pd.DataFrame):
        self.flow = flow

    def read(self, name, start=None, end=None, columns=None):
        if name == "moneyflow":
            return self.flow
        raise KeyError(name)


class FakeMarket:
    """6 个交易日 × 5 只票，两个申万一级行业，一只涨停，一只不映射。"""

    DATES = ("20261023", "20261026", "20261027", "20261028", "20261029", "20261030")

    def __init__(self):
        self.store = FakeStore(pd.DataFrame([
            {"ts_code": "600001.SH", "net_mf_amount": 100.0},   # 万元 -> 1,000,000 元
            {"ts_code": "600002.SH", "net_mf_amount": -50.0},
        ]))

    def daily(self, day):
        return pd.DataFrame([
            # 银行：6 天涨 10%，最后一天齐涨
            {"ts_code": "600001.SH", "close": 11.0, "pre_close": 10.9, "amount": 1000},
            {"ts_code": "600002.SH", "close": 11.0, "pre_close": 10.9, "amount": 1000},
            # 电子：一只封涨停（close == up_limit），一只横盘，一只跌
            {"ts_code": "300001.SZ", "close": 11.0, "pre_close": 10.0, "amount": 500},
            {"ts_code": "300002.SZ", "close": 10.0, "pre_close": 10.0, "amount": 500},
            {"ts_code": "300003.SZ", "close": 9.0, "pre_close": 10.0, "amount": 500},
            # 没有申万分类的票
            {"ts_code": "999999.SZ", "close": 10.0, "pre_close": 10.0, "amount": 500},
        ])

    def panel(self, day, days):
        rows = []
        for ts, start, end in (("600001.SH", 10.0, 11.0), ("600002.SH", 10.0, 11.0),
                               ("300001.SZ", 10.0, 11.0), ("300002.SZ", 10.0, 10.0),
                               ("300003.SZ", 10.0, 9.0), ("999999.SZ", 10.0, 10.0)):
            for i, d in enumerate(self.DATES):
                px = start + (end - start) * i / (len(self.DATES) - 1)
                rows.append({"trade_date": d, "ts_code": ts, "close": px, "adj_close": px})
        return pd.DataFrame(rows)

    def limits(self, day):
        return pd.DataFrame([
            {"ts_code": "600001.SH", "up_limit": 12.1, "down_limit": 9.9},
            {"ts_code": "600002.SH", "up_limit": 12.1, "down_limit": 9.9},
            {"ts_code": "300001.SZ", "up_limit": 11.0, "down_limit": 9.0},
            {"ts_code": "300002.SZ", "up_limit": 11.0, "down_limit": 9.0},
            {"ts_code": "300003.SZ", "up_limit": 11.0, "down_limit": 9.0},
        ])

    def sw_table(self):
        return pd.DataFrame([
            {"ts_code": "600001.SH", "l1_name": "银行"},
            {"ts_code": "600002.SH", "l1_name": "银行"},
            {"ts_code": "300001.SZ", "l1_name": "电子"},
            {"ts_code": "300002.SZ", "l1_name": "电子"},
            {"ts_code": "300003.SZ", "l1_name": "电子"},
        ])


def rows_by_name(out):
    return {row["l1"]: row for row in out["rows"]}


def test_returns_are_equal_weight_over_members():
    """行业收益是成员等权平均——不是市值加权，也不是"最大的那只"。

    银行两只都 +10%，电子一只 +10%、一只 0、一只 -10%，等权是 0。
    """
    out = panel(FakeMarket(), "20261030")
    rows = rows_by_name(out)

    assert rows["银行"]["members"] == 2
    assert rows["银行"]["ret_5d"] == 0.10
    assert rows["电子"]["members"] == 3
    assert abs(rows["电子"]["ret_5d"]) < 0.001


def test_breadth_separates_a_broad_move_from_one_name():
    """宽度和涨停家数分开给：电子的均值被一只涨停撑住，宽度说明其余在跌。"""
    rows = rows_by_name(panel(FakeMarket(), "20261030"))

    assert rows["电子"]["advancers"] == 1
    assert rows["电子"]["decliners"] == 1
    assert rows["电子"]["limit_up"] == 1
    assert rows["银行"]["advancers"] == 2 and rows["银行"]["limit_up"] == 0


def test_unmapped_names_are_counted_not_silently_dropped():
    """没有申万分类的票必须报数：把它们悄悄丢掉，行业均值会变。"""
    out = panel(FakeMarket(), "20261030")

    assert out["unmapped"] == 1
    assert out["members_covered"] == 5
    assert "999999.SZ" not in {row["l1"] for row in out["rows"]}


def test_money_flow_is_summed_over_members_and_converted_from_wan_yuan():
    """moneyflow 是万元；行业净流入是成员求和再换成元。

    银行两只：+100 万、-50 万 → 净 50 万元 = 500,000 元。少了求和会得到某一只的钱，
    少了 ×1e4 会差四个数量级。
    """
    rows = rows_by_name(panel(FakeMarket(), "20261030"))

    assert rows["银行"]["net_mf_cny"] == 500_000.0


def test_ranking_is_by_five_day_return_and_the_weak_end_is_kept():
    """排序按 5 日；最弱的一端必须留在表里——轮动问题里它和最强的一样重要。"""
    out = panel(FakeMarket(), "20261030")

    assert out["rank_by"] == "ret_5d"
    assert out["rows"][0]["l1"] == "银行" and out["rows"][0]["rank"] == 1
    assert [row["rank"] for row in out["rows"]] == [1, 2]
    assert out["spread_top_minus_bottom"] is not None


def test_a_missing_taxonomy_says_so_instead_of_returning_an_empty_table():
    class NoTable(FakeMarket):
        def sw_table(self):
            return pd.DataFrame()

    out = panel(NoTable(), "20261030")

    assert out["rows"] == []
    assert "申万" in out["note"]
