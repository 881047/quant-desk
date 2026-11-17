"""The knowledge layer must be reproducible, bracketed, and honest about doubt."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from service.trading import analog, knowledge
from tests.trading.helpers import pick_symbols

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cache_root(tmp_path_factory):
    return tmp_path_factory.mktemp("analog-cache")


@pytest.fixture(scope="module")
def index(cache_root, real_registry_copy):
    from service.adapters.parquet_store import DatasetStore
    from service.adapters.sqlite_store import Registry

    store = DatasetStore(ROOT / "data", Registry(real_registry_copy))
    return analog.get_index(store, cache_root)


def test_analog_matches_cannot_overlap_the_query_window(desk, index):
    day = desk.market.latest_bar_date()
    i = index.day_index(day)
    assert i is not None, "the index must cover the latest bar"
    symbol = pick_symbols(desk, 1)[0]
    raw = analog.features_for(desk.market, symbol, day)
    assert raw is not None
    vec = np.array([raw[name] for name in analog.STOCK_FEATURES]
                   + [float(index.day_mkt[i][0]), float(index.day_mkt[i][1])],
                   dtype=np.float32)
    hit = index.query(vec, day, k=100)
    days = index.day_of_row[hit["rows"]["day"]]
    horizon = max(analog.HORIZONS)
    assert np.all(np.abs(days - i) >= horizon + 1), "an analog may not share its own future"


def test_analog_report_brackets_its_own_uncertainty(desk, cache_root):
    symbol = pick_symbols(desk, 1)[0]
    report = analog.explain(desk, symbol, k=200, root=cache_root)
    assert report["ok"], report.get("error")
    assert set(report["horizons"]) == {"1d", "2d", "3d", "4d", "5d", "10d"}
    for stats in report["horizons"].values():
        low, high = stats["excess_ci95"]
        assert low is not None and high is not None
        assert low <= high
        assert 0.0 <= stats["win_rate"] <= 1.0
        assert stats["n_days"] <= stats["n"]
    assert report["match"]["distance_band"] in ("tight", "normal", "far", "unknown")
    assert "quality" not in report["match"], "那是几何事实，不是评价"
    assert 0.0 <= report["match"]["nearest_percentile"] <= 1.0
    assert report["caveats"], "knowledge without caveats is a claim"
    assert report["provenance"]["entry"] == "next_open"


def test_analog_refuses_a_symbol_it_cannot_measure(desk, cache_root):
    """Derived from the data, not from a fixed date.

    This used to hardcode 2023-01-20 as "too early to measure". That was true
    when the store began in 2023-01; after the backfill to 2005 the same date has
    twelve years behind it, the query succeeds, and the test failed for the best
    possible reason. Anchor it to the calendar instead so it keeps testing the
    rule rather than a memory of the data.
    """
    sessions = sorted(desk.market.calendar.days)
    early = sessions[analog.MIN_HISTORY // 3]
    report = analog.explain(desk, "000001.SZ", as_of=early, root=cache_root)
    assert report["ok"] is False, f"{early} should not have {analog.MIN_HISTORY} sessions behind it"
    assert "交易日不足" in report["error"]

    # and the same symbol, asked about a date with enough history, must work
    assert analog.explain(desk, "000001.SZ", root=cache_root)["ok"] is True

    unknown = analog.explain(desk, "999999.SH", root=cache_root)
    assert unknown["ok"] is False


def test_history_never_reads_past_as_of(desk):
    """A replay at an old date must not see the rest of that year's partition."""
    frame = desk.market.history("000001.SZ", "20230120", 61)
    assert not frame.empty
    assert frame["trade_date"].max() <= "20230120"
    assert len(frame) <= 61


def test_regime_table_covers_the_four_labels(desk, cache_root):
    table = analog.regime_table(desk, root=cache_root)
    assert table["ok"] is True
    assert set(table["by_regime"]) == {"risk_on", "neutral", "risk_off", "stressed"}
    for row in table["by_regime"].values():
        assert row["days"] > 0
        assert 0.0 <= row["share"] <= 1.0
        assert "fwd_5d_mean" in row


def test_the_ledger_reports_measurements_not_verdicts(desk):
    """表名从 ``falsified`` 改成 ``measured``，因为那个名字断言了证据没说过的话。

    五条先验测的都是 ``next_open``（次日开盘接力），却被读成"这一类交易都不行"——
    reports/close/20261030.md 里模型据此写下"追二板既越权限又无证据"。
    标签只增加了权威，没增加信息：``spec`` 早就写明买了什么卖了什么。
    """
    ledger = knowledge.ledger(desk)

    assert ledger["ok"] is True
    assert "falsified" not in ledger, "那个键是判决，不是读数"
    assert "measured" in ledger
    names = [item.get("name") or item.get("statement") for item in ledger["measured"]]
    assert "全市场反转（跌幅最大）" in names
    # 静态先验不再自带判词；能判断的是 spec + 数字
    for prior in knowledge.PRIORS:
        assert "verdict" not in prior, prior["name"]
        assert prior["spec"] and prior["headline"]
    # 每条都得让读者能自己判断，而不是只能信一个标签
    for row in ledger["measured"]:
        assert row.get("spec"), row
        assert row.get("headline") or (row.get("evidence") or {}).get("trades") is not None
    assert "读数，不是判决" in ledger["note"]
    assert {row["key"] for row in ledger["live"]} >= {"llm", "core_etf"}


def test_crowding_returns_percentiles_not_opinions(desk):
    symbol = pick_symbols(desk, 1)[0]
    report = knowledge.crowding(desk, symbol)
    assert report["ok"], report.get("error")
    assert -1.0 <= report["distance_from_high"] <= 0.0
    assert 0.0 <= report["range_position"] <= 1.0
    assert report["amount_ratio"] > 0
    # 原来这里有一条 `flags`：字符串列表，每条带一句解读（"人多"、
    # "回撤概率上升"、"接飞刀区"）。指标本来就在同一响应里单列，
    # 解读没有增加信息，而其中一句是从未测过的因果断言。
    assert "flags" not in report, "意见不该以事实的形态出现"
    assert isinstance(report["thresholds_crossed"], list)
    for hit in report["thresholds_crossed"]:
        assert hit["metric"] in ("turnover_percentile", "distance_from_high",
                                 "amount_ratio", "up_streak_days", "range_position")
        assert "value" in hit and ("at_least" in hit or "at_most" in hit)
    assert report["caveats"]


def test_answer_dispatches_and_never_raises(desk):
    assert knowledge.answer(desk, "ledger")["ok"] is True
    assert knowledge.answer(desk, "cards")["ok"] is True

    unknown = knowledge.answer(desk, "astrology")
    assert unknown["ok"] is False
    assert unknown["topics"]

    missing = knowledge.answer(desk, "analog")
    assert missing["ok"] is False

    broken = knowledge.answer(desk, "crowding", symbol="999999.SH")
    assert broken["ok"] is False


def test_index_metadata_matches_the_data_cover(desk, index):
    assert index.meta["end"] == desk.market.latest_bar_date()
    assert index.meta["top_n"] == analog.TOP_N
    assert index.meta["entry"] == "next_open"
    assert index.meta["rows"] > 100_000
    assert len(index.meta["dist_ref"]) >= 50, "the distance calibration must survive a rebuild"


def test_limit_structure_cross_checks_the_other_vendor():
    """同花顺 and 东财 disagree by design; the answer must say so.

    On 2026-10-26 the review quoted 45 limit-ups (同花顺) while the sentiment panel
    gated on 55 (东财), and nothing in either payload mentioned the other. Mixing
    the two across days turns a ten-name definition gap into a 45% "退潮".
    """
    from types import SimpleNamespace

    from service.trading.tape import _cross_check

    class _Frame:
        def __init__(self, n):
            self._n = n
            self.empty = n == 0

        def __len__(self):
            return self._n

    sizes = {"limit_up_pool": 55, "broken_board_pool": 23, "limit_down_pool": 26}
    desk = SimpleNamespace(data=SimpleNamespace(
        read=lambda name, start=None, end=None: _Frame(sizes.get(name, 0))))

    out = _cross_check(desk, "20261026", {"counts": {"limit_up": 45, "broken": 18,
                                                     "limit_down": 14}})
    assert out["ok"] is True
    assert out["eastmoney"] == {"limit_up": 55, "broken": 23, "limit_down": 26}
    assert out["delta"]["limit_up"] == 10, "东财 counts ten more than 同花顺"
    assert "同一来源" in out["note"]


def test_cross_check_reports_the_vendor_it_names_not_the_derived_pool():
    """`counts` 过了派生池覆盖之后，limit_up 已经不是同花顺的数了。

    `summarize_limit` 把派生池写进 ``counts.limit_up/broken/limit_down``，同花顺的
    原值挪进 ``counts.vendor_ths``。把它原样传给对账，"ths" 那一列印出来的就是派生
    数——同一个响应里两套同花顺家数，而 delta 算的其实是"东财 vs 派生"。
    实测 2026-11-02：vendor_ths 131/28/10、派生 132/34/11，修复前 cross_check 报
    ths=132/34/11、delta=-7/-6/-3；真值 −6/0/−2。
    """
    from types import SimpleNamespace

    from service.trading.tape import _cross_check

    class _Frame:
        def __init__(self, n):
            self._n = n
            self.empty = n == 0

        def __len__(self):
            return self._n

    sizes = {"limit_up_pool": 125, "broken_board_pool": 28, "limit_down_pool": 8}
    desk = SimpleNamespace(data=SimpleNamespace(
        read=lambda name, start=None, end=None: _Frame(sizes.get(name, 0))))

    summary = {"counts": {"limit_up": 132, "broken": 34, "limit_down": 11,
                          "vendor_ths": {"limit_up": 131, "broken": 28,
                                         "limit_down": 10}}}
    out = _cross_check(desk, "20261102", summary)

    assert out["eastmoney"] == {"limit_up": 125, "broken": 28, "limit_down": 8}
    assert out["ths"] == {"limit_up": 131, "broken": 28, "limit_down": 10}, (
        "同花顺那一列必须是 vendor_ths，不是被派生池覆盖后的 counts")
    assert out["delta"] == {"limit_up": -6, "broken": 0, "limit_down": -2}
    assert "derived" in out, "note 让人先看 derived，就得把它给出来"


def test_limit_structure_cross_check_says_when_it_cannot_compare():
    from types import SimpleNamespace

    from service.trading.tape import _cross_check

    class _Frame:
        empty = True

        def __len__(self):
            return 0

    desk = SimpleNamespace(data=SimpleNamespace(read=lambda *a, **k: _Frame()))
    out = _cross_check(desk, "20261026", {"counts": {"limit_up": 45}})
    assert out["ok"] is False
    assert "无法对账" in out["note"]


def test_liquidity_separates_the_cost_it_charges_from_the_one_it_only_estimates(desk):
    """两套成本语言必须分开写清楚，否则 /liquidity 报 38bps、账本记 25bps。"""
    from service.kernel.costs import CostModel
    from service.kernel.models import D

    symbol = pick_symbols(desk, 1)[0]
    out = knowledge.liquidity(desk, symbol, sizes=(1000, 5000))
    assert out["ok"] is True
    row = out["curve"][0]

    # 报出来的"实际收费"必须等于内核在这一票面下的往返成本 + 固定滑点
    inst = desk.market.instrument(symbol, out["as_of"])
    slip_bps = 5 if inst.is_etf else 10
    notional = D(str(round(row["notional_cny"], 2)))
    expected = CostModel().round_trip_bps(notional, inst) + 2 * slip_bps
    assert row["fill_cost_bps"] == pytest.approx(expected, abs=0.05)

    # fill 是往返口径，impact 是单边口径；总成本必须先把冲击计成双边。
    assert row["total_cost_bps"] == pytest.approx(
        row["fill_cost_bps"] + 2 * row["impact_bps"], abs=0.02)
    assert "没有收" in out["cost_note"]


def test_a_missing_symbol_says_which_argument_and_that_the_capability_exists(desk):
    """`{"ok": false, "error": "distribution 需要 symbol"}` 与「没有这个能力」同形。

    提示词曾经教模型不带 symbol 去问 distribution / crowding / liquidity，于是这条
    路只可能以错误收场；错误本身又没说是哪一类问题，读者只能放弃这个功能。现在
    错误里带 need、一个可照抄的调用，以及哪些 topic 不需要 symbol。
    """
    for topic in knowledge.SYMBOL_TOPICS:
        out = knowledge.answer(desk, topic)

        assert out["ok"] is False
        assert out["need"] == "symbol"
        assert out["example"] == {"topic": topic, "symbol": "600519.SH"}
        assert "不是没有这个能力" in out["error"]
        # 市场层面的主题必须列出来，否则读者不知道哪些可以直接问。
        # 引用同一份常量：2026-11-12 新增 factors 时，写死的 5 元集合
        # 把一次正常的功能扩展报成了回归。
        assert set(out["market_topics"]) == set(knowledge.MARKET_TOPICS)
        assert "factors" in out["market_topics"]


def test_the_symbol_list_matches_what_answer_actually_requires(desk):
    """清单和实现必须同一份。多列一个 topic 会让读者白传参数，少列一个就会失败。"""
    for topic in knowledge.SYMBOL_TOPICS:
        assert knowledge.answer(desk, topic)["need"] == "symbol"

    for topic in ("ledger", "cards", "regime", "risk"):
        out = knowledge.answer(desk, topic)
        assert out.get("need") != "symbol", f"{topic} 不该要求 symbol"


def test_position_exposes_stop_risk_context_without_an_action(desk):
    """The 2026-11-09 audit derived stop/ATR/budget compatibility by hand.

    Once a position exists, the kernel must supply those numbers itself, and
    degrade to null (never an invented value) when ATR or limits are missing.
    """
    from tests.trading.helpers import pick_symbols, proposal

    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol, target_weight=0.2))
    assert ticket["status"] == "proposed", ticket.get("reasons")
    desk.decisions.approve(ticket["ticket_id"])

    out = knowledge.answer(desk, "position", symbol=symbol)
    assert out["ok"] is True and out["held"] is True
    assert "stop_risk_context" in out, "the field must exist even when unavailable"

    ctx = out["stop_risk_context"]
    if ctx is not None and ctx.get("available"):
        assert "action" not in ctx
        assert ctx["risk_budget_cny"] > 0
        assert ctx["risk_to_stop_cny"] >= 0
        assert ctx["risk_budget_used_pct"] is not None
        # ATR is optional; when present the ratio must be dimensionless and
        # must never be built by subtracting an adjusted price from a traded one.
        if ctx["stop_width_in_atr"] is not None:
            assert ctx["stop_width_in_atr"] >= 0
    # The rest of the position answer survives regardless of that block.
    assert out["qty"] > 0 and out["stop"] is not None


def test_not_holding_a_name_is_an_answer_not_a_failure(desk):
    """`{"ok": false, "error": "不在持仓里"}` 与「工具坏了」是同一个信封。

    读者拿到的其实是要的事实（空手就是空手），但 ok:false 会让他重试或放弃，
    而真正该做的是换个问题——profile 讲这只票，liquidity/exposure 讲能不能买。
    """
    out = knowledge.answer(desk, "position", symbol="600519.SH")

    assert out["ok"] is True
    assert out["held"] is False
    assert out["symbol"] == "600519.SH"
    assert "quant_profile" in out["note"]
