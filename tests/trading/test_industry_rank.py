"""行业内的相对位置：当日排名，不是累计超额。

和 `profile.relative_strength` 的分工最容易混：那个是 20/60 日累计超额，
这个是**当日**在同行里排第几。2026-11-02 的分众 20 日超额 +17pp、当日排名
25/28，两个都对——它们回答的不是同一个问题。
"""

from service.trading.industry_rank import in_industry


def test_rank_lands_inside_its_own_industry(desk):
    out = in_industry(desk, "002027.SZ", "20261102")

    assert out["ok"] is True
    assert out["industry"]["group"] == "广告营销"
    rank = out["rank_pct"]
    assert 1 <= rank["rank"] <= rank["total"]
    # 排名分母不能超过该行业的成员数
    assert rank["total"] <= out["industry"]["members"]


def test_rank_and_percentile_agree(desk):
    out = in_industry(desk, "600892.SH", "20261102")

    assert out["ok"] is True
    rank = out["rank_pct"]
    # 第 1 名对应最高百分位，最后一名对应最低
    expected = 1 - (rank["rank"] - 1) / rank["total"]
    assert abs(rank["percentile"] - expected) < 0.05


def test_excess_is_against_the_industry_median(desk):
    out = in_industry(desk, "002027.SZ", "20261102")

    assert out["ok"] is True
    expected = out["pct_chg"] - out["industry_median_pct"]
    assert abs(out["vs_industry_pp"] - expected) < 0.02


def test_unknown_symbol_is_an_error_not_a_zero_rank(desk):
    out = in_industry(desk, "999999.SH", "20261102")

    assert out["ok"] is False
    assert "error" in out
