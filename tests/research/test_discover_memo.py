from __future__ import annotations

import pandas as pd
import pytest

from service.research.discover import _breadth, _core_regime, _gap_behaviour
from service.research.memo import _resolve_one, score_memos
from service.trading.state import PaperStore


class LadderStore:
    """把两张天梯表摆在一起，别的一律读空。"""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames

    def read(self, dataset, start=None, end=None, **kwargs):
        frame = self.frames.get(dataset)
        return pd.DataFrame() if frame is None else frame.copy()


class DailyStore:
    def __init__(self, frame: pd.DataFrame, fund: pd.DataFrame | None = None):
        self.daily = frame
        self.fund = fund if fund is not None else pd.DataFrame()

    def read(self, dataset, start=None, end=None, **kwargs):
        if dataset == "daily":
            return self.daily.copy()
        if dataset == "fund_daily":
            return self.fund.copy()
        return pd.DataFrame()


def daily_frame(days: int, drift: float) -> pd.DataFrame:
    """Dates start after 'today' so predictions resolve against them."""
    dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-10-26", periods=days)]
    rows = []
    for code in ("600001.SH", "600002.SH"):
        price = 10.0
        for date in dates:
            prev = price
            price = round(price * (1 + drift), 4)
            rows.append({"ts_code": code, "trade_date": date,
                         "open": round(prev * 1.001, 4), "close": price, "pre_close": prev})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ discover
def test_breadth_counts_names_above_their_average():
    frame = daily_frame(40, 0.01)  # steadily rising -> everything above its MA
    assert _breadth(frame, 20) == pytest.approx(1.0)
    falling = daily_frame(40, -0.01)
    assert _breadth(falling, 20) == pytest.approx(0.0)


def test_gap_behaviour_buckets_today():
    frame = pd.DataFrame([
        {"ts_code": "A", "trade_date": "20260101", "open": 10.0, "close": 10.2, "pre_close": 10.0},
        {"ts_code": "B", "trade_date": "20260101", "open": 9.0, "close": 9.3, "pre_close": 10.0},
        {"ts_code": "C", "trade_date": "20251231", "open": 10.0, "close": 10.0, "pre_close": 10.0},
    ])
    out = _gap_behaviour(frame)
    assert set(out) <= {"gap<-3%", "-3~-1%", "-1~1%", "1~3%", "gap>3%"}
    assert sum(v[1] for v in out.values()) == 2  # only the last day is described


def test_core_regime_is_cut_to_the_requested_as_of():
    from service.trading.core import DEFENSIVE_ETF, RISK_ETFS, CoreBook

    days = pd.bdate_range("2025-01-02", periods=280).strftime("%Y%m%d")
    day = str(days[250])
    codes = list(dict.fromkeys(["510300.SH", *RISK_ETFS[:3], DEFENSIVE_ETF]))
    frame = pd.DataFrame([
        {"ts_code": code, "trade_date": d, "close": 4.0 + i * 0.001}
        for code in codes for i, d in enumerate(days)
    ])

    class FundStore:
        def read(self, dataset, start=None, end=None, **kwargs):
            if dataset != "fund_daily":
                return pd.DataFrame()
            out = frame.copy()
            if start:
                out = out[out["trade_date"] >= str(start)]
            if end:
                out = out[out["trade_date"] <= str(end)]
            return out

    before = _core_regime(FundStore(), as_of=day, book=CoreBook(FundStore(), None))
    shocked = frame.copy()
    shocked.loc[shocked["trade_date"] > day, "close"] *= 20
    frame = shocked
    after = _core_regime(FundStore(), as_of=day, book=CoreBook(FundStore(), None))
    assert before == after
    assert after["as_of"] == day


# --------------------------------------------------------------------- memos
class MemoPaper(PaperStore):
    pass


def test_memo_scoring_produces_a_brier(tmp_path):
    paper = MemoPaper(tmp_path / "paper.sqlite")
    memo_id = paper.add_memo(
        "market bounce", "the equal-weight market rises over the next five days",
        mechanism="breadth thrust", falsifier="breadth stays below 35%",
        confidence=0.8,
        predictions=[{"claim": "market up in 5d", "kind": "market_up",
                      "horizon": 5, "probability": 0.8}])
    store = DailyStore(daily_frame(30, 0.01))
    out = score_memos(store, paper)
    assert [m["id"] for m in out["scored"]] == [memo_id]
    assert out["scored"][0]["brier"] == pytest.approx(0.04)
    assert out["calibration"]["n"] == 1
    assert paper.list_memos(status="open") == []


def test_confident_and_wrong_costs_more(tmp_path):
    paper = MemoPaper(tmp_path / "p.sqlite")
    paper.add_memo("wrong", "market falls", confidence=0.9,
                   predictions=[{"claim": "down", "kind": "market_up",
                                 "horizon": 3, "probability": 0.1}])
    out = score_memos(DailyStore(daily_frame(20, 0.01)), paper)
    # probability 0.1 on an outcome of 1 -> Brier 0.81
    assert out["scored"][0]["brier"] == pytest.approx(0.81)


def test_unresolved_predictions_are_left_open(tmp_path):
    paper = MemoPaper(tmp_path / "p2.sqlite")
    paper.add_memo("far", "market up in a year", confidence=0.5,
                   predictions=[{"claim": "up in 250d", "kind": "market_up",
                                 "horizon": 250, "probability": 0.6}])
    out = score_memos(DailyStore(daily_frame(20, 0.01)), paper)
    assert out["scored"] == []
    assert len(paper.list_memos(status="open")) == 1


def test_memo_anchor_is_the_last_session_at_or_before_the_memo_date():
    """A Sunday memo refers to Friday's close, not to Monday's.

    The old code took ``idx[0]`` from ``d >= start`` — the first session *after*
    the memo date — as the anchor, which shifted every horizon by one session and
    made a one-day prediction unresolvable on the very day /duty called it due.
    Two definitions of "matured"; the one that produces a number has to win.
    """
    market = pd.Series([0.01, 0.01, 0.01], index=["20261021", "20261022", "20261023"])
    pred = {"horizon": 1, "kind": "market_up"}

    # Memo written on Sunday 2026-10-25; the anchor is Friday, so one session of
    # look-ahead does not exist yet.
    assert _resolve_one(pred, "20261025", market, {}, None, None) is None

    with_monday = pd.Series([0.01, 0.01, 0.01, 0.02],
                            index=["20261021", "20261022", "20261023", "20261026"])
    assert _resolve_one(pred, "20261025", with_monday, {}, None, None) == 1.0


def test_memo_anchor_uses_the_session_itself_when_it_traded():
    market = pd.Series([0.01, 0.01, 0.01], index=["20261022", "20261023", "20261026"])
    pred = {"horizon": 1, "kind": "market_up"}
    # Memo written on Friday 10/23 → anchor is 10/23, one session later is 10/26.
    assert _resolve_one(pred, "20261023", market, {}, None, None) == 1.0


def test_partially_mature_memo_stays_open_until_every_prediction_settles(tmp_path):
    paper = MemoPaper(tmp_path / "partial.sqlite")
    memo_id = paper.add_memo("partial", "one short and one long claim", confidence=0.6,
        predictions=[
            {"claim": "short", "kind": "market_up", "horizon": 1, "probability": 0.7},
            {"claim": "long", "kind": "market_up", "horizon": 250, "probability": 0.6},
        ])
    out = score_memos(DailyStore(daily_frame(20, 0.01)), paper)
    assert out["scored"][0]["status"] == "partial"
    assert out["scored"][0]["pending"] == 1
    assert paper.list_memos(status="open")[0]["id"] == memo_id
    assert out["calibration"]["n"] == 0


@pytest.mark.parametrize("prediction", [
    {"kind": "market_up", "horizon": 0, "probability": 0.5},
    {"kind": "unknown", "horizon": 1, "probability": 0.5},
    {"kind": "market_up", "horizon": 1, "probability": float("nan")},
    {"kind": "market_up", "horizon": 1, "probability": 1.2},
])
def test_invalid_predictions_are_reported_and_never_closed(tmp_path, prediction):
    paper = MemoPaper(tmp_path / "invalid.sqlite")
    memo_id = paper.add_memo("invalid", "invalid prediction", confidence=0.5,
                             predictions=[prediction])
    out = score_memos(DailyStore(daily_frame(20, 0.01)), paper)
    assert out["invalid"][0]["id"] == memo_id
    assert paper.list_memos(status="open")[0]["id"] == memo_id


def test_the_ladder_value_comes_from_the_derived_pool_not_the_vendor():
    """厂商天梯不受预热影响，但它受「名单不全」影响——所以不能无条件信它。

    2026-09-01~10-30 的 38 个交易日里，同花顺把 2026-09-02 的 5 板报成了 4 板
    （漏掉当天那只 5 板），家数有 22 天偏低。而派生池唯一的已知失败模式是
    「没带预热重建、每只票都算 1 板」，那个能认出来：它说 1 板而别人说 2 板以上。
    能认出来的失败模式，比一个安静的低估更值得信任。
    """
    from service.research.discover import _limit_structure

    pools = pd.DataFrame({
        "trade_date": ["20261030"] * 4,
        "code": ["A", "B", "C", "D"],
        "pool": ["limit_up"] * 4,
        "boards": [5, 2, 1, 1],
    })
    store = LadderStore({
        "ths_limit_up": pd.DataFrame({
            "trade_date": ["20261030"] * 3,
            "continue_cnt": [4, 2, 1],       # 漏了那只 5 板
        }),
    })

    out = _limit_structure(store, pools, ["20261030"])

    assert out["source_used"] == "derived_t0", "出处，不是信任状"
    assert out["max_boards"] == 5
    assert out["conflict"] is True
    assert "漏" in out["note"] or "同花顺" in out["note"]


def test_an_unheated_derived_pool_loses_the_pick_instead_of_being_trusted():
    """派生池说全场都是首板、而厂商报到 4 板时，信厂商并说明原因。

    这就是 2026-10-28 那个 bug 的形状：单日重建、没有预热，每只票都算 1 板。
    一个错得很自信的数比一个缺失的数更糟。
    """
    from service.research.discover import _limit_structure

    pools = pd.DataFrame({
        "trade_date": ["20261028"] * 3,
        "code": ["A", "B", "C"],
        "pool": ["limit_up"] * 3,
        "boards": [1, 1, 1],
    })
    store = LadderStore({
        "ths_limit_up": pd.DataFrame({
            "trade_date": ["20261028"] * 2,
            "continue_cnt": [4, 1],
        }),
    })

    out = _limit_structure(store, pools, ["20261028"])

    assert out["max_boards"] == 4
    assert out["source_used"] == "ths_limit_up"
    assert "预热" in out["note"]
