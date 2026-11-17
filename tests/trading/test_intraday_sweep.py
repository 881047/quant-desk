"""全市场分时的调度规则：换源可续跑、预算先算后用、盘中不跑。

这几条都是"跑一次要二十分钟"逼出来的。跑得慢的东西，判断错一次的代价是重新等二十分钟，
所以判据要小到能单独测。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from scripts import intraday_sweep as sweep_mod
from scripts.intraday_sweep import needs_fetch, target_days


def test_a_symbol_present_every_day_needs_nothing():
    have = {"20261029": {"000001.SZ": frozenset({"baostock"})},
            "20261030": {"000001.SZ": frozenset({"baostock"})}}

    assert not needs_fetch(have, ["20261029", "20261030"], "000001.SZ",
                           refresh=False, want="baostock")


def test_a_symbol_missing_one_day_is_fetched_even_without_refresh():
    have = {"20261029": {"000001.SZ": frozenset({"baostock"})}, "20261030": {}}

    assert needs_fetch(have, ["20261029", "20261030"], "000001.SZ",
                       refresh=False, want="baostock")


def test_refresh_keeps_the_symbols_already_on_the_new_source():
    """换源那一遍断了重跑：已经换成 baostock 的不该再花一次请求。"""
    have = {"20261030": {"000001.SZ": frozenset({"baostock"}),
                         "000002.SZ": frozenset({"tencent"}),
                         "600519.SH": frozenset({"baostock", "tencent"})}}

    assert not needs_fetch(have, ["20261030"], "000001.SZ", refresh=True, want="baostock")
    assert needs_fetch(have, ["20261030"], "000002.SZ", refresh=True, want="baostock")
    assert needs_fetch(have, ["20261030"], "600519.SH", refresh=True, want="baostock"), \
        "两种源混着的 symbol-day 必须重抓——混源的日子读起来是两个口径"


def test_without_refresh_a_mixed_row_is_left_alone():
    """不换源时混源不重抓：那是历史，重抓要二十分钟，而它不影响新的一天。"""
    have = {"20261030": {"600519.SH": frozenset({"baostock", "tencent"})}}

    assert not needs_fetch(have, ["20261030"], "600519.SH", refresh=False, want="baostock")


def test_target_days_reads_a_real_session_range_not_the_single_day_cross_section():
    """--days 120 must not silently become the one day accepted by market.daily()."""
    calls = []

    class Store:
        def read(self, dataset, start, end, *, columns):
            calls.append((dataset, start, end, columns))
            return pd.DataFrame({"trade_date": [
                "20261113", "20261111", "20261112", "20261111", "20261110",
                "20261116",  # a future row must never enter this end-dated plan
            ]})

    class Market:
        store = Store()

        def daily(self, *_args, **_kwargs):
            raise AssertionError("the single-day cross-section is not a session calendar")

    out = target_days(SimpleNamespace(market=Market()), "20261113", 3)

    assert out == ["20261111", "20261112", "20261113"]
    assert len(calls) == 1
    assert calls[0][0] == "daily" and calls[0][2] == "20261113"
    assert calls[0][1] < "20261113" and calls[0][3] == ["trade_date"]


def test_target_days_normalizes_iso_dates_and_zero_count_does_no_io():
    class Store:
        def read(self, *_args, **_kwargs):
            return pd.DataFrame({"trade_date": ["2026-11-12", "2026-11-13"]})

    desk = SimpleNamespace(market=SimpleNamespace(store=Store()))
    assert target_days(desk, "20261113", 2) == ["20261112", "20261113"]
    assert target_days(desk, "20261113", 0) == []


def test_each_completed_block_writes_a_resume_checkpoint(tmp_path, monkeypatch):
    day = "20261030"
    progress = tmp_path / "sweep.progress.json"

    class Store:
        def __init__(self):
            self.writes = []

        def read_partition(self, *_args, **_kwargs):
            raise FileNotFoundError("no minutes yet")

        def write(self, dataset, partition, frame, **kwargs):
            self.writes.append((dataset, partition, len(frame), kwargs))

    class Market:
        def __init__(self):
            self.store = Store()

        def daily(self, _end, *, columns):
            return pd.DataFrame({"ts_code": ["000001.SZ", "000002.SZ"],
                                 "trade_date": [day, day]})[columns]

    desk = SimpleNamespace(market=Market())
    observed_previous_checkpoint = []

    def frame(symbol):
        return pd.DataFrame([{
            "ts_code": symbol, "trade_time": "2026-10-30 09:35", "trade_date": day,
            "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0,
            "vol": 1.0, "amount": 100.0, "source": "baostock",
        }])

    def fake_fetch(symbols, start, end, *, budget, on_symbol, request_timeout):
        assert start == end == "2026-10-30"
        assert request_timeout == 120.0
        out = []
        for index, symbol in enumerate(symbols, start=1):
            if symbol == "000002.SZ":
                observed_previous_checkpoint.append(json.loads(progress.read_text(
                    encoding="utf-8"))["processed"])
            budget.spend(1)
            record = {"symbol": symbol, "rows": 1, "frame": frame(symbol), "why": "ok"}
            on_symbol(index, len(symbols), record)
            out.append(record)
        return out

    monkeypatch.setattr(sweep_mod, "fetch_range", fake_fetch)
    monkeypatch.setattr(sweep_mod, "BUDGET", tmp_path / "budget.json")

    out = sweep_mod.sweep(
        desk, [day], refresh=False, limit=0, chunk=1, dry=False, force=True,
        source="baostock", heartbeat=0, progress_path=progress)

    assert observed_previous_checkpoint == [1], "第二块开始前必须已有第一块断点"
    assert out["fetched"] == 2 and out["failed_n"] == 0 and out["rows"] == 2
    state = json.loads(progress.read_text(encoding="utf-8"))
    assert state["complete"] is True and state["processed"] == 2
    assert state["requests_spent"] == 2
    assert len(desk.market.store.writes) == 2
