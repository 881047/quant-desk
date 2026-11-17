from __future__ import annotations

import json

import pandas as pd

from service.trading.arena import _author_of, _tickets_for_cases, scoreboard
from service.trading.universe import browse


class Store:
    def __init__(self, daily, basic=None, daily_basic=None):
        self.daily = daily
        self.basic = basic if basic is not None else pd.DataFrame()
        self.daily_basic = daily_basic if daily_basic is not None else pd.DataFrame()

    def read(self, dataset, start=None, end=None, **kwargs):
        if dataset == "daily":
            df = self.daily.copy()
            if start:
                df = df[df["trade_date"] >= str(start)]
            if end:
                df = df[df["trade_date"] <= str(end)]
            return df
        if dataset == "stock_basic":
            return self.basic.copy()
        if dataset == "daily_basic":
            return self.daily_basic.copy()
        return pd.DataFrame()


def make_store() -> Store:
    days = ["20260105", "20260106", "20260107"]
    rows = []
    for code, price, amt in (("600001.SH", 10.0, 500000), ("600002.SH", 30.0, 10000)):
        for i, d in enumerate(days):
            rows.append({"ts_code": code, "trade_date": d, "close": price * (1 + 0.01 * i),
                         "pre_close": price * (1 + 0.01 * (i - 1)), "amount": amt})
    daily = pd.DataFrame(rows)
    basic = pd.DataFrame([
        {"ts_code": "600001.SH", "name": "甲股份", "industry": "银行"},
        {"ts_code": "600002.SH", "name": "ST乙", "industry": "软件服务"},
    ])
    db = pd.DataFrame([
        {"ts_code": "600001.SH", "trade_date": "20260107", "turnover_rate": 1.2,
         "volume_ratio": 1.5, "circ_mv": 5_000_000, "pe": 8.0, "pb": 0.9},
        {"ts_code": "600002.SH", "trade_date": "20260107", "turnover_rate": 3.0,
         "volume_ratio": 2.2, "circ_mv": 900_000, "pe": 40.0, "pb": 3.1},
    ])
    return Store(daily, basic, db)


def test_browse_returns_facts_not_a_ranking():
    out = browse(make_store(), "20260107")
    assert out["universe_size"] == 1          # ST excluded by default
    row = out["rows"][0]
    assert row["symbol"] == "600001.SH"
    for key in ("turnover", "volume_ratio", "float_cap_1e4", "range_pos_20d"):
        assert key in row
    assert "score" not in row and "recommendation" not in row
    assert "not a recommendation" in out["note"]


def test_browse_filters():
    store = make_store()
    assert browse(store, "20260107", industry="银行")["universe_size"] == 1
    assert browse(store, "20260107", industry="软件")["universe_size"] == 0
    assert browse(store, "20260107", min_amount_cny=10_000_000)["universe_size"] == 1
    assert browse(store, "20260107", max_price=5)["universe_size"] == 0
    assert browse(store, "20260107", exclude_st=False)["universe_size"] == 2


# ------------------------------------------------------------------- arena
class Book:
    def __init__(self, equity=5200.0):
        self._equity = equity

    def summary(self):
        return {"equity": str(self._equity), "cash_available": "1000", "initial_cash": "5000",
                "positions": [{"symbol": "600001.SH", "qty": 100}]}


class Paper:
    def __init__(self, cases):
        self._cases = []
        for i, case in enumerate(cases):
            row = dict(case)
            row.setdefault("ticket_ids", [f"td-{row.get('case_id') or i}"])
            self._cases.append(row)

    def list_cases(self, limit=500):
        return self._cases[:limit]

    def list_tickets(self, limit=50, status=None):
        return [{"ticket_id": tid, "directed_by": "agent"}
                for case in self._cases for tid in (case.get("ticket_ids") or [])]

    def core_nav_history(self, limit=1):
        return [{"trade_date": "20260107", "nav": "5050.00", "cash": "0", "weights": {}}]


class Market:
    def last_trading_day(self):
        return "20260107"


class Desk:
    def __init__(self, cases, equity=5200.0):
        self.book = Book(equity)
        self.paper = Paper(cases)
        self.market = Market()
        self.data = Store(pd.DataFrame([
            {"ts_code": "X", "trade_date": d, "close": c, "pre_close": p}
            for d, c, p in (("20260105", 100.0, 99.0), ("20260106", 101.0, 100.0),
                            ("20260107", 102.0, 101.0))
        ]))


def test_arena_refuses_to_call_three_trades_an_edge(monkeypatch):
    # `core` was retired on 2026-11-01 and is omitted from the arena by default.
    # This test is about the verdict, not the retirement, so it holds the sleeve
    # alive explicitly instead of dropping the assertion.
    import dataclasses

    import service.trading.core as core_mod

    monkeypatch.setattr(core_mod, "UNIVERSE_CFG",
                        dataclasses.replace(core_mod.UNIVERSE_CFG, retired=False))
    cases = [
        {"case_id": "a", "symbol": "600001.SH", "opened_at": "2026-01-05T10:00:00",
         "closed_at": "2026-01-06T14:00:00", "pnl_pct": 0.05},
        {"case_id": "b", "symbol": "600002.SH", "opened_at": "2026-01-05T10:00:00",
         "closed_at": "2026-01-07T14:00:00", "pnl_pct": -0.02},
    ]
    out = scoreboard(Desk(cases))
    assert out["edge"]["n"] == 2
    assert out["edge"]["mean_excess"] is not None
    # 判词删了，判据还在：区间算不出来（样本不足），且门槛要求 20 笔。
    assert out["ci_position"] == "sample_below_20"
    assert out["min_decisions_for_an_interval"] == 20
    assert out["edge"]["ci_low"] is None, "2 笔不给区间——零宽区间会被读成确定"
    assert out["sleeves"]["core"]["nav"] == 5050.0


def test_arena_reports_negative_edge_direction():
    # Model loses while the market rises -> excess must be negative.
    cases = [{"case_id": "a", "symbol": "600001.SH", "opened_at": "2026-01-05T10:00:00",
              "closed_at": "2026-01-07T14:00:00", "pnl_pct": -0.01}]
    out = scoreboard(Desk(cases))
    assert out["edge"]["mean_excess"] < 0


def test_arena_does_not_fabricate_a_flat_benchmark_when_window_is_absent():
    cases = [{"case_id": "missing", "symbol": "600001.SH",
              "opened_at": "2026-01-08T10:00:00", "closed_at": "2026-01-09T14:00:00",
              "pnl_pct": 0.05}]
    out = scoreboard(Desk(cases))

    assert out["decisions"] == []
    gap = out["benchmark_gaps"][0]
    assert gap["case_id"] == "missing"
    assert gap["available_from"] == "20260105"
    assert gap["available_to"] == "20260107"
    assert out["edge"]["n"] == 0
    assert out["edge"]["excluded_missing_benchmark"] == 1
    assert out["edge"]["unscored_no_market"] == 1
    assert out["ci_position"] == "sample_below_20"
    assert out["market_window"] == {
        "from": "20260105", "to": "20260107", "sessions": 3}


def test_arena_exposes_the_actual_benchmark_end_date_for_partial_coverage():
    cases = [{"case_id": "partial", "symbol": "600001.SH",
              "opened_at": "2026-01-06T10:00:00", "closed_at": "2026-01-20T14:00:00",
              "pnl_pct": 0.05}]
    out = scoreboard(Desk(cases))

    row = out["decisions"][0]
    assert row["market"] is not None
    assert row["market_window"]["base_date"] == "20260105"
    assert row["market_window"]["end_date"] == "20260107"
    assert row["market_window"]["sessions"] == 2


def test_ticket_lookup_grows_beyond_500_and_missing_author_is_unknown():
    old = {"ticket_id": "old-close", "directed_by": "user"}
    rows = [{"ticket_id": f"new-{i}", "directed_by": "agent"} for i in range(599)] + [old]

    class TruncatingPaper:
        def __init__(self):
            self.limits = []

        def list_tickets(self, limit=500, status=None):
            self.limits.append(limit)
            return rows[:limit]

    paper = TruncatingPaper()
    found = _tickets_for_cases(paper, [{"ticket_ids": ["old-close"]}])

    assert paper.limits == [500, 1000]
    assert found["old-close"]["directed_by"] == "user"
    assert _author_of({"ticket_ids": ["old-close"]}, found) == "user"
    assert _author_of({"ticket_ids": ["not-stored"]}, found) == "unknown"
    assert _author_of({}, found) == "unknown"


def test_unknown_closing_ticket_is_not_counted_as_agent_evidence():
    cases = [{"case_id": "unknown", "symbol": "600001.SH",
              "opened_at": "2026-01-05T10:00:00", "closed_at": "2026-01-07T14:00:00",
              "pnl_pct": 0.05}]
    desk = Desk(cases)
    desk.paper.list_tickets = lambda limit=500, status=None: []

    out = scoreboard(desk)

    assert out["edge"]["n"] == 0
    assert out["edge"]["excluded_unknown_author"] == 1
    assert out["decisions"][0]["directed_by"] == "unknown"


def test_written_author_backfill_is_classified_as_user_not_unknown():
    """A closed ticket with no stored row but an *explicit* correction.

    2026-10-28: the only closed case was a user instruction to flatten the
    book. `tickets` is empty, the correction lives in the audit log. It must be
    excluded as user-directed, not reported as a missing ticket — and must not
    become agent evidence either.
    """
    cases = [{"case_id": "closed", "symbol": "002966.SZ",
              "opened_at": "2026-01-05T10:00:00", "closed_at": "2026-01-07T14:00:00",
              "pnl_pct": 0.008, "ticket_ids": ["td-close"]}]
    desk = Desk(cases)
    desk.paper.list_tickets = lambda limit=500, status=None: []
    desk.paper.list_decisions = lambda limit=50: [{
        "kind": "author_backfill", "ticket_id": "td-close",
        "payload": json.dumps({"ticket_id": "td-close", "directed_by": "user",
                               "why": "用户指令清空模拟盘"}),
    }]

    out = scoreboard(desk)

    assert out["edge"]["n"] == 0
    assert out["edge"]["excluded_user_directed"] == 1
    assert out["edge"]["excluded_unknown_author"] == 0
    assert out["decisions"][0]["directed_by"] == "user"
    assert out["not_scored"].get("user_instruction") == 1
    assert "unknown_closing_ticket" not in out["not_scored"]


def test_no_written_correction_stays_unknown_even_with_other_audit_rows():
    """Actor is not directed_by: guard/system rows must never imply an author."""
    cases = [{"case_id": "closed", "symbol": "600001.SH",
              "opened_at": "2026-01-05T10:00:00", "closed_at": "2026-01-07T14:00:00",
              "pnl_pct": 0.05, "ticket_ids": ["td-close"]}]
    desk = Desk(cases)
    desk.paper.list_tickets = lambda limit=500, status=None: []
    desk.paper.list_decisions = lambda limit=50: [
        {"kind": "propose", "ticket_id": "td-close", "actor": "agent",
         "payload": json.dumps({"request": {"side": "SELL"}})},
        {"kind": "fill", "ticket_id": "td-close", "actor": "guard",
         "payload": json.dumps({"request": {"side": "SELL"}})},
    ]

    out = scoreboard(desk)

    assert out["edge"]["n"] == 0
    assert out["edge"]["excluded_unknown_author"] == 1
    assert out["decisions"][0]["directed_by"] == "unknown"


def test_beijing_names_are_listed_but_marked_not_tradeable():
    """北交所可以看，不可以选、不可以买。

    用户口径：信息层可以含北交所（家数、连板、情绪照算），但选股与买入不碰。
    所以 `browse` 不过滤它——过滤掉等于假装它不存在——而是把 `tradeable=false`
    写在行上，让模型在提出之前就知道。真正的拦截在 guard。
    """
    days = ["20260105", "20260106", "20260107"]
    rows = []
    for code in ("600001.SH", "920592.BJ"):
        for i, d in enumerate(days):
            rows.append({"ts_code": code, "trade_date": d, "close": 10.0 + i,
                         "pre_close": 10.0, "amount": 1_000_000})
    out = browse(Store(pd.DataFrame(rows)), "20260107")

    board = {row["symbol"]: row["board"] for row in out["rows"]}
    tradeable = {row["symbol"]: row["tradeable"] for row in out["rows"]}

    assert board["920592.BJ"] == "BSE"
    assert tradeable["920592.BJ"] is False
    assert tradeable["600001.SH"] is True
    # 两行都在：不给看不等于不给买，两件事分开表达。
    assert out["universe_size"] == 2
    # 沪深那一行也必须说出自己的板块。原先这里是 `None`，于是 5,377 只沪深股
    # 的 board 全为空——字段在，信息只在 4% 的行上。
    assert board["600001.SH"] == "MAIN"


def test_a_row_carries_the_band_its_board_implies():
    """板块的后果是涨跌幅，而涨跌幅决定"离天花板还有多远"。

    只给板块名，读者还得知道 300 开头是 20%、600 开头是 10%——那正是这张表
    想免掉的领域常识。ST 的 5% 更让"看板块就能推"这条捷径彻底失效。
    """
    days = ["20260105", "20260106", "20260107"]
    rows = []
    for code in ("600001.SH", "300750.SZ", "688981.SH", "920592.BJ"):
        for i, d in enumerate(days):
            rows.append({"ts_code": code, "trade_date": d, "close": 10.0 + i,
                         "pre_close": 10.0, "amount": 1_000_000})
    out = browse(Store(pd.DataFrame(rows)), "20260107")

    band = {row["symbol"]: row["band"] for row in out["rows"]}

    assert band == {"600001.SH": 0.10, "300750.SZ": 0.20,
                    "688981.SH": 0.20, "920592.BJ": 0.30}
