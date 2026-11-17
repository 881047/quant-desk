"""申万一级行业榜：31 行，两个涨跌幅口径，资金流带日期。

2026-11-02 的实盘审计里，"传媒 +6.69% / +29.99 亿、非银 +3.02% / +27.94 亿"
这类判断全部来自外部检索——内核没有行业榜，而它是"哪条线有资金"的唯一入口。
"""

from __future__ import annotations

from service.trading.sector import sectors


def test_it_returns_all_sw_level_one_industries(desk):
    out = sectors(desk, "20261030")

    assert out["ok"] is True
    assert out["count"] == 31, "申万 2021 一级是 31 个"
    assert len(out["industries"]) == 31
    row = out["industries"][0]
    for key in ("sw_l1", "members", "pct_chg", "pct_chg_eq", "amount_cny"):
        assert key in row, key


def test_the_two_weightings_are_both_reported(desk):
    """市值加权像指数，等权像"随手买一只"。只给一个数就是把口径藏起来。"""
    out = sectors(desk, "20261030")
    differing = [row for row in out["industries"]
                 if row["pct_chg"] is not None and row["pct_chg_eq"] is not None
                 and abs(row["pct_chg"] - row["pct_chg_eq"]) > 0.5]

    assert differing, "两个口径应该在某些行业上明显不同，否则说明有一个算错了"


def test_it_says_which_day_the_money_came_from(desk):
    out = sectors(desk, "20261030")

    assert "flow_as_of" in out
    assert out["flow_as_of"] in ("20261030", None)
    for row in out["industries"]:
        if row["main_net_inflow_cny"] is None:
            assert out["flow_missing"] > 0
        else:
            assert row["flow_as_of"] == out["flow_as_of"]


def test_the_note_separates_institutional_money_from_hot_money(desk):
    """P2-3：机构口径不能用来判断游资行为，这句话必须写在响应里。"""
    out = sectors(desk, "20261030")

    assert "不含营业部席位" in out["note"]
    assert "游资" in out["note"]


def test_a_day_without_data_is_an_error_not_an_empty_list(desk):
    out = sectors(desk, "19900102")

    assert out["ok"] is False
    assert "error" in out


def test_today_before_the_evening_ingest_says_so(desk):
    """16:03 调用时今天的日线还没写。

    2026-11-03 起有两种可接受的回答，取决于当天有没有留下盘中快照：

    * **有快照**：给出榜，但必须带 ``price_source="intraday_snapshot"`` 并在
      ``basis`` 里说明"未收盘、会继续变"——盘中要的是"现在哪条线强"，而把
      盘中数冒充收盘数才是真正的问题；
    * **没有快照**：说"还没入库、先看哪一天"，而不是"表缺了"。

    这条测试原来只允许第二种，因为当时 ``_industry_frame`` 只认 `daily`，
    盘中必然为空。行为变了，断言跟着变，但两条约束都还在。
    """
    latest = desk.market.latest_bar_date()
    ahead = str(int(latest) + 1) if latest else "20991231"

    out = sectors(desk, ahead)

    if out["ok"]:
        assert out.get("price_source") == "intraday_snapshot", \
            "盘中可以给榜，但必须标明这不是收盘数"
        assert "盘中快照" in out["basis"]
        assert out["as_of"] == ahead
    else:
        assert "还没入库" in out["error"]
        assert out["latest_bar"] == latest


def test_a_future_day_with_no_snapshot_is_still_an_error(desk):
    """远期日期既没有日线也没有快照，必须报错而不是给一份空的榜。"""
    out = sectors(desk, "20991231")

    assert out["ok"] is False
    assert "error" in out
