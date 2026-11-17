"""docs/14-backend-debt-from-frontend-20261108.md 的契约回归。

债务清单里的字段以前只能靠前端"记住要不要 ×100"、用正则剥前缀、照着英文句子
抓可变参数活着。这里把后端补齐的那部分——单位元信息、拆分后的编码、带坐标的
核对清单、结构化文案与值域表——逐项钉住，防止下一次重构又把它们退回去。

所有新增字段都是**增量**的：旧的裸字段一律保留，既有消费者不必改。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service.adapters import catalog
from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app
from service import vocabulary
from service.trading import board, decay, guard, journal_check

TOKEN = {"X-Quant-Token": "change-me-local-only"}
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- A 组
def test_a1_units_declare_both_scales():
    units = vocabulary.POOL_UNITS
    assert units["seal_ratio"].startswith("fraction")
    assert units["pct_of_equity"].startswith("percent")
    assert units["seal_ratio_pct"].startswith("percent")
    assert trading_api.vocab.POOL_UNITS is units  # 接口与词表同源，不抄第二份


def test_a1_pools_carries_units_and_explicit_percent(client, desk):
    day = desk.market.latest_bar_date()
    body = client.get("/market/pools", headers=TOKEN,
                      params={"date": day, "limit": 5}).json()
    assert body["units"]["seal_ratio"] == "fraction (0..1)"
    assert body["units"]["pct_of_equity"] == "percent (0..100)"
    assert "pct_of_equity" in body["units_note"]
    rows = body.get("pools", {}).get("limit_up") or []
    if not rows:
        pytest.skip("no limit-up rows for this bar")
    equity = float(desk.book.summary()["equity"])
    for row in rows:
        if row.get("seal_ratio") is not None:
            assert row["seal_ratio_pct"] == round(row["seal_ratio"] * 100, 3)
        if row.get("pct_of_equity") is not None:
            # 旧字段一个没动：10.0 还是 10.0，0.0511 还是 0.0511
            assert row["pct_of_equity"] == round(row["lot_cost_cny"] / equity * 100, 1)
        no, text = board.split_seal_window(row.get("seal_window"))
        assert row["seal_window_no"] == no
        assert row["seal_window_text"] == text


def test_a2_split_seal_window_handles_plain_text():
    assert board.split_seal_window("1_开盘09:30-10:00") == (1, "开盘09:30-10:00")
    assert board.split_seal_window("4_尾盘14:00后") == (4, "尾盘14:00后")
    assert board.split_seal_window("开盘09:30-10:00") == (None, "开盘09:30-10:00")
    assert board.split_seal_window(None) == (None, None)


def test_a3_dataset_kind_separates_calendar_from_panel():
    assert catalog.dataset_kind("trade_cal") == "calendar"
    assert catalog.dataset_kind("share_float") == "calendar"
    assert catalog.dataset_kind("stock_basic") == "reference"
    assert catalog.dataset_kind("rt_quote") == "snapshot"
    assert catalog.dataset_kind("daily") == "data"
    assert catalog.dataset_kind("never_seen") == "data"  # 未知按面板处理，宁可误报
    assert not catalog.coverage_boundary("trade_cal")
    assert not catalog.coverage_boundary("share_float")
    assert catalog.coverage_boundary("daily")
    # brief 的滞后判定与 /data/status 的分流必须是同一条规则
    from service.adapters import brief

    for name in ("trade_cal", "share_float", "stock_basic", "rt_quote", "daily"):
        assert brief._coverage_boundary(name) == catalog.coverage_boundary(name)


# ---------------------------------------------------------------- B 组
def test_b1_locate_finds_the_sentence_not_just_the_number():
    text = "收盘核 39 · 错 8 · 待修：5、84、4，缺口是费用口径"
    rows = journal_check.locate(text, ["5", "84", "4"])
    assert [r["value"] for r in rows] == ["5", "84", "4"]
    for row in rows:
        assert "待修" in row["excerpt"]
        assert row["start"] is not None and row["end"] > row["start"]
        assert text[row["start"]:row["end"]] == row["value"]
        assert row["occurrences"] >= 1
        assert row["field"] in {"percent", "shares", "cny", "count", "price", "unknown"}


def test_b1_thousand_separator_and_no_false_partial_match():
    rows = journal_check.locate("权益 5,007.51 元，另有 50 家", ["5007.51", "50"])
    assert rows[0]["occurrences"] == 1
    assert "5,007.51" in rows[0]["excerpt"]
    assert rows[0]["field"] == "cny"
    assert rows[1]["value"] == "50" and rows[1]["field"] == "count"


def test_b1_b2_detail_is_additive_and_gives_sign_hints():
    text = "跌 3.04%，还有 912 家没核上"
    numbers = {"checked": 3, "matched": 1, "unmatched": ["912"],
               "unmatched_count": 1, "sign_from_words": ["3.04"]}
    out = journal_check.detail(text, numbers)
    for key, value in numbers.items():
        assert out[key] == value                       # 旧字段逐项保留
    assert out["unmatched_detail"][0]["excerpt"]
    assert out["sign_hints"][0]["direction_unverified"] is True
    assert "符号未核" in out["sign_hints"][0]["meaning"]


def test_b1_check_returns_detail_on_the_write_path():
    verdict = journal_check.check("今天 912 家上涨，权益 5007.51 元",
                                  {"equity": 5000.0})
    assert "912" in verdict["unmatched"]             # 旧行为不变
    assert verdict["unmatched_detail"]
    assert verdict["unmatched_detail"][0]["value"] == "912"


def test_b3_not_scored_detail_names_the_tickets(client):
    body = client.get("/arena", headers=TOKEN).json()
    assert isinstance(body["not_scored"], dict)       # 旧计数保留
    detail = body["not_scored_detail"]
    for key, count in body["not_scored"].items():
        assert detail[key]["count"] == count
        assert "case_ids" in detail[key]
    if "unknown_closing_ticket" in detail:
        assert "ticket_ids" in detail["unknown_closing_ticket"]


# ---------------------------------------------------------------- C 组
def test_c1_vocabulary_covers_every_literal_in_source():
    """新增一个 kind / 检查项却忘了改词表，这条会先失败。"""
    sources = [ROOT / "service/trading/decisions.py", ROOT / "service/trading/book.py",
               ROOT / "service/api/trading.py", ROOT / "service/trading/state.py"]
    literal = re.compile(
        r'log_decision\(\s*["\']([a-z_]+)["\']'
        r'|commit_ticket_state\([\s\S]{0,300}?\bkind=["\']([a-z_]+)["\']'
    )
    found = set()
    for path in sources:
        for a, b in literal.findall(path.read_text(encoding="utf-8")):
            found.add(a or b)
    assert found, "正则没抓到任何字面值，扫描本身失效了"
    assert found <= vocabulary.DECISION_KINDS, sorted(found - vocabulary.DECISION_KINDS)

    guard_src = (ROOT / "service/trading/guard.py").read_text(encoding="utf-8")
    names = set(re.findall(r'Check\(\s*["\']([a-z_.]+)["\']', guard_src))
    assert names, "正则没抓到任何检查项"
    assert names <= vocabulary.GUARD_CHECKS, sorted(names - vocabulary.GUARD_CHECKS)


def test_c1_undeclared_values_are_visible_not_fatal():
    vocabulary.check_decision("typo_kind_for_test", "agent")
    try:
        assert "typo_kind_for_test" in vocabulary.UNDECLARED_KINDS
        assert "typo_kind_for_test" in vocabulary.as_dict()["decisions"]["undeclared_kind"]
    finally:
        vocabulary.UNDECLARED_KINDS.discard("typo_kind_for_test")


def test_c1_meta_vocabulary_endpoint(client):
    body = client.get("/meta/vocabulary", headers=TOKEN).json()
    assert "proposal_blocked" in body["decisions"]["kind"]
    assert "agent" in body["decisions"]["actor"]
    assert "daily_actions" in body["guard_checks"]
    assert body["decay"]["reasons"]
    assert body["units"]["pct_of_equity"]


def test_c2_every_guard_check_is_structured():
    plain = guard.Check("kill_switch", True, "off").as_dict()
    assert plain["structured"]["unit"] == "bool"
    assert plain["structured"]["basis"] == "status_only"
    measured = guard.Check(
        "daily_actions", True, "0/4 fills today",
        structured={"metric": "fills_today", "actual": 0, "cap": 4,
                    "unit": "count"}, detail_zh="今日已成交 0/4 笔").as_dict()
    assert measured["structured"]["cap"] == 4
    assert measured["detail_zh"] == "今日已成交 0/4 笔"
    assert measured["detail"] == "0/4 fills today"     # 结算文本一字未改


def test_c3_decay_note_meta_is_machine_readable():
    baseline = decay.Baseline(mean=0.001, sigma=0.01, source="evidence:test")
    out = decay.evaluate([0.001] * 5, baseline, window=20)
    assert "need at least" in out["note"]              # 旧句子保留
    assert out["note_meta"] == {"reason": "sample_below_min", "need": 20, "have": 5}
    assert out["note_meta"]["reason"] in vocabulary.DECAY_REASONS


def test_c3_decay_endpoint_has_note_meta(client):
    body = client.get("/decay", headers=TOKEN).json()
    for row in body["strategies"]:
        assert "note_meta" in row
        assert row["note_meta"]["reason"] in vocabulary.DECAY_REASONS


# ---------------------------------------------------------------- D 组
def test_d1_persona_keeps_its_payload_with_scorecard_cached(client):
    body = client.get("/persona", headers=TOKEN).json()
    assert "scorecard" in body and "history" in body
    assert "current" in body and "eligibility" in body
    assert "today" in body and "probe" in body


def test_d3_identity_has_explicit_delist_status(desk):
    from service.trading import profile

    symbol = str(desk.market.basic().iloc[0]["ts_code"])
    day = desk.market.latest_bar_date()
    identity = profile._identity(desk, symbol, day)
    assert identity["delist_status"] in vocabulary.DELIST_STATUSES
    if identity["delist_date"] is None and identity["delist_status"] == "listed":
        assert identity["delist_date_note"] == "未退市（仍正常交易）"
    assert "unknown" in identity["delist_status_note"]


# ------------------------------------------------- 2026-11-08 复查：跨端点量纲
def test_recheck_unit_conventions_cover_both_scales():
    """量纲只有 vocabulary 一处手写，且键是可机械定位的完整路径。

    第六轮把原先"裸字段名 + 另一张容器表"收成单一完整路径：
    ``industries[].pct_chg`` 直接说明它在响应里第几层，不再需要递归猜。
    """
    conv = vocabulary.UNIT_CONVENTIONS
    by = conv["by_endpoint"]
    assert by["/market/sector"]["industries[].pct_chg"].startswith("percent")
    assert by["/market/sector"]["industries[].pct_chg_eq"].startswith("percent")
    assert by["/market/pools"]["pools.limit_up[].seal_ratio"].startswith("fraction")
    assert by["/market/pools"]["pools.limit_up[].seal_ratio_pct"].startswith("percent")
    assert by["/facts"]["breadth.equal_weight_return"].startswith("fraction")
    assert by["/facts"]["account.day_pnl_pct"].startswith("fraction")
    assert by["/regime"]["breadth_above_ma20"].startswith("fraction")
    assert by["/regime"]["vol_percentile_1y"].startswith("fraction")
    assert by["/risk"]["daily_pnl_pct"].startswith("fraction")
    assert by["/risk/exposure"]["cash_weight"].startswith("fraction")
    assert "row_paths" not in conv, "容器信息不能另立一张表"
    exceptions = {row["field"] for row in conv["naming_exceptions"]}
    assert {"daily_pnl_pct", "day_pnl_pct"} <= exceptions
    assert conv["naming_exceptions"][0]["actual"].startswith("fraction")


def test_recheck_every_numeric_endpoint_carries_the_same_units(client):
    """逐端点验，不是把所有响应的键并成一个"宇宙"再比。"""
    for endpoint in vocabulary.UNIT_CONVENTIONS["by_endpoint"]:
        if endpoint == "/market/pools.sentiment":
            continue  # 虚拟端点，字段在 /market/pools 响应里
        body = client.get(endpoint, headers=TOKEN).json()
        assert body.get("units") == vocabulary.units_for(endpoint), endpoint
    vocab = client.get("/meta/vocabulary", headers=TOKEN).json()
    assert vocab["units"] == vocabulary.units_for("/market/pools")
    assert vocab["units_text"] == vocabulary.units_text()


def test_recheck_frontend_formatters_match_declared_scales():
    """消费者侧：所有歧义字段都必须走 fmtByUnits（由 units 决定是否 ×100）。

    第五轮这里只是 6 行源码子串包含断言，且漏了最危险的 daily_pnl_pct。
    现在解析全部页面里 fmtByUnits 的调用字段，并反查词表：字段没声明时
    fmtByUnits 会返回破折号而不是猜一个换算，测试要保证不会有这种字段。
    """
    pages = sorted((ROOT / "web/assets/js").rglob("*.js"))
    sources = {p: p.read_text(encoding="utf-8") for p in pages}
    calls = {}
    for path, source in sources.items():
        calls[path] = set(re.findall(r"fmtByUnits\([^,]+,\s*'([^']+)'", source))
    used = set().union(*calls.values()) if calls else set()
    required = {
        "daily_pnl_pct",  # 命名例外：名字带 _pct，值是小数
        "pct_chg", "pct_chg_eq", "mom20", "seal_ratio_pct", "pct_of_equity",
        "break_rate", "equal_weight_return", "median_return",
        "vol_percentile_1y", "breadth_above_ma20",
    }
    assert required <= used, f"未按 units 渲染的字段: {sorted(required - used)}"
    declared = set().union(*(vocabulary.units_for(ep) for ep in
                             vocabulary.UNIT_CONVENTIONS["by_endpoint"]))
    assert used <= declared, f"前端读了未声明量纲的字段: {sorted(used - declared)}"
    # 这些字段不允许再被旧函数直接包裹（换了就是 100 倍，后端断言仍全绿）。
    joined = "\n".join(sources.values())
    for legacy in ("pct(s.daily_pnl_pct)", "pct(p.daily_pnl_pct)",
                   "pctPts(r.pct_chg)", "pctPts(r.mom20)",
                   "ratio(s.break_rate", "pct(b.equal_weight_return)",
                   "ratio(r.vol_percentile_1y", "pctPlain(r.seal_ratio_pct"):
        assert legacy not in joined, legacy
