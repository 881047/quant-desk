"""对比：两列必须来自同一段代码，而不是两次调用后靠人对齐。

最容易错的地方不是取数，是**两边算的不是同一件事**：估值分位窗口不同、财报季度不同、
行业强弱比的是不同层级。这份测试钉的就是这三件事被显式说出来。
"""

from __future__ import annotations

import pytest

from service.trading import compare


def test_two_names_produce_one_row_per_metric(desk):
    out = compare.build(desk, ["600519.SH", "000858.SZ"])

    assert out["ok"] is True
    assert out["symbols"] == ["600519.SH", "000858.SZ"]
    metrics = [row["metric"] for row in out["rows"]]
    assert "pe_ttm_pctile" in metrics and "vs_industry_pp_20d" in metrics
    assert len(metrics) == len(compare.METRICS) + len(compare.FINANCIAL)
    for row in out["rows"]:
        assert set(row["values"]) == set(out["symbols"])


def test_both_columns_share_one_valuation_window(desk):
    """窗口是请求级的，不是每只票各算各的——这正是"同源"的含义。"""
    out = compare.build(desk, ["600519.SH", "300750.SZ"], valuation_window=500)

    assert out["valuation_window"] == 500
    for row in out["rows"]:
        if row["metric"].endswith("_pctile"):
            assert row["comparable"] is True


def test_a_reporting_period_mismatch_disables_the_financial_rows(desk):
    """不同季度的 ROE 并排，比出来的是季度差不是公司差。"""
    out = compare.build(desk, ["600519.SH", "000858.SZ"])

    periods = out["report_period"]
    mismatch = len(set(periods.values())) > 1
    assert out["period_match"] is (not mismatch)
    for row in out["rows"]:
        if row["metric"] in {name for name, _, _ in compare.FINANCIAL}:
            assert row["comparable"] is (not mismatch)
            if mismatch:
                assert "报告期不同" in row["why"]


def test_industries_are_named_because_the_reference_differs(desk):
    """`vs_industry_pp` 是各自对自己行业，读者必须知道参照物不是同一个。"""
    out = compare.build(desk, ["600519.SH", "000001.SZ"])

    assert set(out["industries"]) == {"600519.SH", "000001.SZ"}
    row = next(r for r in out["rows"] if r["metric"] == "vs_industry_pp_20d")
    # 银行和白酒比"相对行业强弱"是有意义的，但必须附上各自的行业名
    assert row["comparable"] is True
    assert "各自对自己行业" in out["note"]


def test_one_symbol_is_refused_with_a_usable_example(desk):
    out = compare.build(desk, ["600519.SH"])

    assert out["ok"] is False
    assert out["example"]["symbols"]


def test_more_than_three_is_refused(desk):
    out = compare.build(desk, ["600519.SH", "000858.SZ", "300750.SZ", "000001.SZ"])

    assert out["ok"] is False
    assert "最多三个" in out["error"]


def test_an_unknown_symbol_is_reported_not_silently_dropped(desk):
    out = compare.build(desk, ["600519.SH", "000858.SZ", "999999.SZ"])

    assert out["ok"] is True
    assert out["missing"] == ["999999.SZ"]
    assert "999999.SZ" not in out["symbols"]


def test_a_null_cell_says_whose_null_it_is(desk):
    """一行两列、一边缺数时，`comparable` 变 false 但不说原因，读者只能猜。

    实测 2026-10-30：000001 与 300750 的 2026Q3 `roe_waa` 在 fina_indicator 里
    都是空的，600519 的 2026Q2 是 16.75。"空"意味着厂商没填，不是 0，也不是
    这家公司没有这个指标——三种读法的下一步完全不同。
    """
    out = compare.build(desk, ["000001.SZ", "300750.SZ"])

    nulls = [row for row in out["rows"] if row.get("null_for")]
    if not nulls:
        pytest.skip("这两只票当期指标齐全，构造不出空值")
    for row in nulls:
        for symbol in row["null_for"]:
            assert row["values"][symbol] is None
        assert row["comparable"] is False, "少一列还能比，是另一种错"
    assert "fina_indicator" in out["null_note"]


def test_a_clean_pair_carries_no_null_disclaimer(desk):
    """一句常驻的免责声明会被读成套话，只在真有 null 时出现。"""
    out = compare.build(desk, ["600519.SH", "000858.SZ"])

    if any(row.get("null_for") for row in out["rows"]):
        pytest.skip("这一对也有空值")
    assert "null_note" not in out
