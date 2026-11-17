"""三个涨停来源的逐只审计：谁漏了、漏的是哪一类。

这个脚本存在的理由是一句很容易写的话：「同花顺和东财的家数不一样，两家口径不同，
都对」。2026-09-01~10-30 逐只核对下来不是这样——同花顺 38 天里只有 16 天正确。
所以测试锁的是三件事：代码格式必须归一化（不然交集恒为空）、漏报和误报必须分开
计数（净差会把「漏 16、多认 2」写成「少 14」）、缺数据不能读成 0。
"""

from __future__ import annotations

import pandas as pd
import pytest

from scripts.pool_audit import compare, to_ts_code


def test_the_eastmoney_bare_code_becomes_a_ts_code():
    """东财给的是裸 6 位。不补后缀，两张表的交集就是空集——这个 bug 我犯过一次。"""
    assert to_ts_code("000410") == "000410.SZ"
    assert to_ts_code("600000") == "600000.SH"
    assert to_ts_code("920592") == "920592.BJ"   # 北交所新代码段
    assert to_ts_code("430418") == "430418.BJ"
    assert to_ts_code("830799") == "830799.BJ"
    # 已经带后缀的原样返回，不能补成 000410.SZ.SZ
    assert to_ts_code("000410.SZ") == "000410.SZ"


def test_misses_and_extras_are_counted_separately():
    """净差会把两种错误抵消：漏 16 只、多认 2 只，净差是 14，但那是两件事。"""
    truth = {"20261026": {"A", "B", "C", "D"}}
    sources = {"ths": {"20261026": {"A", "B", "Z"}}}

    table, summary = compare(truth, sources)

    row = table.iloc[0]
    assert row["truth"] == 4
    assert row["ths_n"] == 3
    assert row["ths_miss"] == 2      # C, D
    assert row["ths_extra"] == 1     # Z
    assert summary["ths"]["mean_miss"] == 2
    assert summary["ths"]["mean_extra"] == 1
    assert summary["ths"]["exact_days"] == 0


def test_a_source_that_was_never_read_is_skipped_not_scored_as_perfect():
    """没有数据 ≠ 完全正确。把缺数据算进「一致天数」会让一个源凭空拿满分。"""
    truth = {"20261026": {"A", "B"}}
    sources = {"eastmoney": {}}          # 这一天东财没抓

    table, summary = compare(truth, sources)

    assert pd.isna(table.iloc[0]["eastmoney_n"])
    assert summary["eastmoney"]["days"] == 0


def test_a_source_matching_the_ceiling_exactly_is_reportable_as_such():
    truth = {"20261030": {"A", "B", "C"}}
    sources = {"derived": {"20261030": {"A", "B", "C"}}}

    _, summary = compare(truth, sources)

    assert summary["derived"]["exact_days"] == 1
    assert summary["derived"]["exact_share"] == pytest.approx(1.0)


def test_two_days_with_different_gaps_summarise_by_mean_not_by_last_day():
    truth = {"20261026": {"A", "B", "C"}, "20261027": {"A", "B"}}
    sources = {"ths": {"20261026": {"A"}, "20261027": {"A", "B"}}}

    _, summary = compare(truth, sources)

    assert summary["ths"]["mean_miss"] == pytest.approx(1.0)
    assert summary["ths"]["worst_miss"] == 2
