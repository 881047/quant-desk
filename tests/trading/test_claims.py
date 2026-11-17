"""写下来的数，是不是贴在了它该在的标签上。

`journal_check` 只回答"这个数在当天 facts 里找不找得到"。2026-10-29 的收盘报告写
"可买的 6 只"，当日实际是上限内 23 只、收盘可买 0 只——"6" 可能出现在 facts 的某个
角落，存在性检查放它过去，但这句话是错的：错在把 6 贴到了"可买"这个标签上。

所以这里测的是另一半：标签。以及**不该判的那一堆**——会误报的检查比没有检查更糟，
而中文散文里"同一个词、不同的量"至少有一打。
"""

from __future__ import annotations

import json

import pytest

from scripts import verify_report
from service.trading import claims

FACTS = {
    "day": "20261030",
    "readiness": {"ok": True, "coverage": 1.0002},
    "breadth": {"advancers": 893, "decliners": 4602, "flat": 76,
                "equal_weight_return": -0.0265, "median_return": -0.026},
    "pools": {"limit_up": 49, "limit_down": 93, "broken": 26,
              "vendor_eastmoney": {"limit_up": 47, "broken": 31}},
    "affordable": {"in_cap": 26, "one_word": 11, "buyable_at_close": 0},
    "account": {"positions": 0, "equity": 5000.0},
}


# ------------------------------------------------------------------ 认得出标签
def test_both_word_orders_are_the_same_claim():
    """`893 家上涨` 和 `涨停 49 家` 是两种语序，不能只认一种。"""
    out = claims.scan("今天 893 家上涨，涨停 49 家。", FACTS)

    assert out["labelled"] == 2
    assert out["ok"] == 2


def test_a_population_label_with_two_definitions_reports_both():
    """裸"可买"既可能是上限内、也可能是收盘还能买——10-29 就是把 23 和 0 写成了 6。"""
    out = claims.scan("可买的 6 只全部封在涨停价。", FACTS)

    item = out["unbacked"][0]
    assert item["label"] == "可买"
    assert item["should_be"] == {"affordable.in_cap": 26,
                                 "affordable.buyable_at_close": 0}


def test_the_longer_label_wins_over_the_shorter_one():
    """`收盘可买 0 只` 里的 `收盘可买` 不能被裸 `可买` 抢走，否则一句正确的话会被判错。"""
    out = claims.scan("收盘可买 0 只，上限内 26 只。", FACTS)

    assert out["labelled"] == 2
    assert out["ok"] == 2


def test_a_number_with_no_label_is_left_alone():
    """`我买了 3 只` 里的 3 没有标签，不该进判定——否则每篇复盘都有一堆噪声。"""
    assert claims.scan("我买了 3 只，连涨 3 天。", FACTS)["labelled"] == 0


# ------------------------------------------------------------------ 三种错
def test_a_real_number_under_the_wrong_label_is_named_and_placed():
    """893 是真的，只是它是上涨家数。报告要指出它本来属于哪儿。"""
    out = claims.scan("今天 893 家跌停。", FACTS)

    item = out["misplaced"][0]
    assert item["label"] == "跌停"
    assert item["should_be"] == {"pools.limit_down": 93}
    assert item["but_that_is"] == {"path": "breadth.advancers", "value": 893}
    assert item["span"] == "今天 893 家跌停"


def test_a_number_no_fact_has_is_unbacked_not_misplaced():
    out = claims.scan("涨停 137 家。", FACTS)

    assert out["unbacked"][0]["wrote"] == "137"
    assert out["misplaced"] == []


def test_an_integral_count_never_matches_a_coverage_ratio():
    """`跌停 1 家` 曾经匹配上 readiness.coverage=1.0002，于是"数是真的"指向了一个比率。"""
    out = claims.scan("昨日跌停 1 家。", FACTS)

    assert out["misplaced"] == []


# ------------------------------------------------------------------ 不该判的
@pytest.mark.parametrize(("text", "why"), [
    ("跌停 14 家（11:30）→ 62 家（14:58）。", "intraday"),
    ("午报可买 13 只。", "quoted"),
    ("09:26 自列可买 5 只等权 +0.67%。", "sample"),
    ("午后新增 7 只炸板。", "delta"),
    ("20 日等权 -9.87%。", "window"),
    ("5 日中位数 -0.81%。", "window"),
])
def test_the_seven_kinds_of_not_this_claim_are_reported_but_not_judged(text, why):
    """盘中/引用/样本/增量/窗口——都是"同一个词、不同的量"。照报，但不判。"""
    out = claims.scan(text, FACTS)

    assert [item["why"] for item in out["provisional"]] == [why] or \
        out["provisional"][0]["why"] == why
    assert out["misplaced"] == [] and out["unbacked"] == []


def test_an_intraday_marker_does_not_excuse_a_population_claim():
    """`可买的 6 只（14:57）` 不能因为带时刻就放过：可买集合当天任何时刻都该是同一个。"""
    out = claims.scan("14:57:17 时上限内可买的 6 只全部封住。", FACTS)

    assert out["ok"] == 0
    assert [item["wrote"] for item in out["unbacked"]] == ["6"]


def test_a_disclosed_vendor_variant_is_not_an_error():
    """`47 家涨停…eastmoney 口径` 是同一个数的另一个来源，写明了就不是错。"""
    out = claims.scan("47 家涨停（eastmoney 全量口径）。", FACTS)

    assert out["ok"] == 1
    assert not out.get("variant")


def test_an_undisclosed_vendor_variant_is_called_a_variant():
    out = claims.scan("47 家涨停，31 家炸板（未注明来源）。", FACTS)

    assert out["variant"][0]["but_that_is"]["path"] == "pools.vendor_eastmoney.limit_up"


# ------------------------------------------------------------------ 形状
def test_no_facts_is_skipped_not_passed():
    out = claims.scan("涨停 49 家。", None)

    assert out["skipped"] is True
    assert "不是通过" in out["note"]


def test_the_counts_add_up():
    """`labelled` 必须等于四类之和——读的人用它判断"这篇有几个标签数"。"""
    out = claims.scan("893 家跌停，涨停 137 家，涨停 49 家，跌停 62 家（14:58）。", FACTS)

    parts = sum(len(out.get(key) or []) for key in
                ("misplaced", "variant", "unbacked", "provisional"))
    assert out["labelled"] == out["ok"] + parts


# ------------------------------------------------------------------ 报告核对
def test_a_close_report_with_a_mislabelled_count_warns(tmp_path, monkeypatch):
    """把 10-29 那类句子放进一份日记账，核对脚本必须报出来，而不是"数字存在"就过。"""
    monkeypatch.setattr(verify_report, "ROOT", tmp_path)
    (tmp_path / "reports" / "facts").mkdir(parents=True)
    (tmp_path / "reports" / "close").mkdir(parents=True)
    (tmp_path / "reports" / "facts" / "20261029.json").write_text(
        json.dumps({"day": "20261029", "readiness": {"ok": True}, **{
            k: v for k, v in FACTS.items() if k != "readiness"}}, ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "reports" / "close" / "20261029.md").write_text(
        "可买的 6 只全部封在涨停价。\n", encoding="utf-8")

    problems = verify_report.check_day("20261029", strict=False)

    assert any(level == "WARN" and "可买" in message for level, message in problems), problems


def test_an_intraday_quote_in_a_good_report_is_info_not_a_warning(tmp_path, monkeypatch):
    """一份好报告里的"跌停 62 家（14:58）"不该被算成错误——否则没人会看这些告警。"""
    monkeypatch.setattr(verify_report, "ROOT", tmp_path)
    (tmp_path / "reports" / "facts").mkdir(parents=True)
    (tmp_path / "reports" / "close").mkdir(parents=True)
    (tmp_path / "reports" / "facts" / "20261030.json").write_text(
        json.dumps(FACTS, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "reports" / "close" / "20261030.md").write_text(
        "盘中跌停 62 家（14:58），收盘 93 家。\n", encoding="utf-8")

    problems = verify_report.check_day("20261030", strict=False)

    assert not [p for p in problems if p[0] in ("WARN", "FAIL")], problems


def test_a_timestamped_sealed_count_is_not_a_missing_number(tmp_path, monkeypatch):
    """2026-11-13 的收盘报告：「cap 内 7 只涨停，14:42:25 全部 sealed」。

    收盘 facts 的 `in_cap` 是 25——14:42 还没到收盘，7 是那一刻的数。`SEALED` 抓到了
    7，存在性检查说"事实包里没有"，而 `claims` 同时把同一句判成 intraday INFO：
    一句话两个结论，WARN 那条还排在前面。对着正确的话报警，结果是没人再看告警。
    """
    monkeypatch.setattr(verify_report, "ROOT", tmp_path)
    (tmp_path / "reports" / "facts").mkdir(parents=True)
    (tmp_path / "reports" / "close").mkdir(parents=True)
    (tmp_path / "reports" / "facts" / "20261030.json").write_text(
        json.dumps(FACTS, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "reports" / "close" / "20261030.md").write_text(
        "cap 内 7 只涨停，14:42:25 全部 sealed。\n", encoding="utf-8")

    problems = verify_report.check_day("20261030", strict=False)

    assert not [p for p in problems if p[0] in ("WARN", "FAIL")], problems
    assert any(level == "INFO" and "盘中" in message
               for level, message in problems), problems
