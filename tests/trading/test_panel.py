"""面板的两套前端读同一份数据；这里钉住那份数据的措辞。"""

from __future__ import annotations

from service.trading.panel import summary_lines

MATCH = {
    "panel": {"ok": True, "total": 4799.37, "cash": 3173.37,
              "market_value": 1624.0, "cash_ratio": 0.661},
    "positions": [{"symbol": "000590.SZ"}],
    "drift": {"verdict": "match", "message": "一致"},
    "reconcile": {"ok": True, "mismatches": []},
    "last_sync": {"imported_at": "2026-10-27T12:59:00", "file_name": "sync-20261027",
                  "rows": 1},
}


def test_summary_reports_totals_then_agreement():
    lines = summary_lines(MATCH)
    assert "总资产 4,799.37" in lines[0]
    assert "现金 66.1%" in lines[0]
    assert lines[1] == "与账户一致"
    assert "上次同步 2026-10-27T12:59" in lines[-1]


def test_summary_puts_the_disagreement_in_front_of_the_reader():
    payload = {**MATCH, "drift": {"verdict": "book_missing",
                                  "message": "账户市值 1,624.00，账本只有 1,000.00"}}
    lines = summary_lines(payload)
    assert any("不一致" in line for line in lines)
    assert any("1,624.00" in line for line in lines)


def test_missing_snapshot_is_not_called_agreement():
    """没有快照 ≠ 一致 —— 这条假安慰今晚已经出现过一次。"""
    payload = {**MATCH, "reconcile": {"ok": True, "snapshot_missing": True, "mismatches": []}}
    lines = summary_lines(payload)
    assert "还没有持仓快照" in lines
    assert "流水与快照 0 处不一致" not in lines


def test_unreadable_client_says_why_instead_of_zeros():
    payload = {**MATCH, "panel": {"ok": False, "reason": "没找到窗口"}}
    lines = summary_lines(payload)
    assert any("没找到窗口" in line for line in lines)
    assert not any("0.00" in line for line in lines)


def test_no_ledger_at_all_is_a_single_honest_line():
    assert summary_lines(None) == ["实盘账本读不到（data/external.sqlite）"]


def test_snapshot_cash_ratio_does_not_leak_float_noise(tmp_path):
    from types import SimpleNamespace

    from service.trading.external import ExternalStore
    from service.trading.panel import account_panel

    store = ExternalStore(tmp_path / "data" / "external.sqlite")
    store.save_day("20261112", cash=1.0, market_value=2.0, equity=3.0,
                   source="fake")
    store.replace_snapshot("20261112", [], source="fake")

    payload = account_panel(SimpleNamespace(), root=tmp_path, with_quotes=False)

    assert payload is not None
    assert payload["panel"]["cash_ratio"] == 0.333333


def test_daily_fallback_and_partial_live_quotes_are_not_fresh(tmp_path):
    from types import SimpleNamespace

    from service.trading.external import ExternalStore
    from service.trading.panel import account_panel

    store = ExternalStore(tmp_path / "data" / "external.sqlite")
    store.save_day("20261028", cash=0.0, market_value=2000.0, equity=2000.0,
                   source="fake")
    store.replace_snapshot("20261028", [
        {"symbol": "600000.SH", "qty": 100, "cost": 10.0, "market_value": 1000.0},
        {"symbol": "000001.SZ", "qty": 100, "cost": 10.0, "market_value": 1000.0},
    ], source="fake")

    def quote(source: str):
        return SimpleNamespace(
            name="fake", pre_close=10.0, last=10.0, suspended=True,
            source=source, quote_ts="2026-10-28T10:00:00+08:00",
            is_realtime=source != "daily")

    class FakeDesk:
        def __init__(self):
            self.market = SimpleNamespace(
                instrument=lambda symbol, day: SimpleNamespace(is_st=False),
                latest_bar_date=lambda: "20261028")

        def quotes(self, symbols):
            return {symbol: quote("daily" if symbol == "000001.SZ" else "ths")
                    for symbol in symbols}

    payload = account_panel(FakeDesk(), root=tmp_path, quote_timeout=0.1)
    assert payload is not None
    assert payload["drift"]["verdict"] == "stale_quotes"
    assert payload["book_value"] == 2000.0
    assert payload["positions"][0]["up_limit"] == 11.0, "停牌/盘前零量不是 ST"


def test_quote_worker_busy_degrades_without_queuing_and_releases_when_done():
    import threading

    from service.trading import panel as panel_mod

    started = threading.Event()
    release = threading.Event()
    calls = []

    class FakeDesk:
        @staticmethod
        def quotes(symbols):
            calls.append(list(symbols))
            started.set()
            assert release.wait(1)
            return {}

    first = panel_mod._quotes(FakeDesk(), ["600000.SH"], 0.001)
    assert first == {} and started.is_set()
    assert panel_mod._quotes(FakeDesk(), ["000001.SZ"], 0.001) == {}
    assert len(calls) == 1, "忙碌时不能再排一个超时请求"

    release.set()
    assert panel_mod._QUOTE_LOCK.acquire(timeout=1), "原请求结束后必须释放 busy 状态"
    panel_mod._QUOTE_LOCK.release()
