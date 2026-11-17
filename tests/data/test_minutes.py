"""Two free minute providers, and the difference between them kept explicit.

The fallback is not defensive coding for its own sake: on 2026-10-28 东财 answered
a handful of requests and then began closing connections, while 腾讯 answered
throughout. A reader that only knows one source sees "no bars" and concludes
something about the stock.
"""

from __future__ import annotations

import pandas as pd
import pytest

from service.adapters import minutes as mod
from service.adapters.eastmoney_adapter import EastMoneyError
from service.adapters.minutes import fetch_minutes


@pytest.fixture(autouse=True)
def _fresh_cooldown():
    """冷却窗口是进程级全局，测完必须复位。

    它是给生产用的断路器：东财一次失败就停 10 分钟，免得逐只股票去打同一个挂掉
    的接口。但在一个 pytest 进程里，它会把"上一个用例喂了一次失败"变成"后面每个
    用例都 cooling down"——单独跑绿、整套跑红，2026-11-02 就是这么红的
    （test_the_primary_is_used_when_it_answers: 'none (eastmoney: cooling down)'）。
    """
    mod.reset_breaker()
    yield
    mod.reset_breaker()


class _EM:
    def __init__(self, frame=None, boom=False):
        self.frame, self.boom, self.calls = frame, boom, 0

    def fetch_minutes(self, symbol, klt=5):
        self.calls += 1
        if self.boom:
            raise EastMoneyError("Server disconnected without sending a response")
        return self.frame


def _tencent_payload(bars, klt=5):
    return {"data": {"sh600519": {f"m{klt}": bars}}}


class _TG:
    def __init__(self, bars=None, boom=False, klt=5):
        self.bars, self.boom, self.calls = bars or [], boom, 0
        self.klt = klt
        self.last_url = ""

    def get_json(self, url):
        self.calls += 1
        self.last_url = url
        if self.boom:
            raise RuntimeError("dns")
        return _tencent_payload(self.bars, self.klt)


def test_the_primary_is_used_when_it_answers():
    frame = pd.DataFrame([{"ts_code": "600519.SH", "trade_time": "2026-10-28 09:35",
                           "trade_date": "20261028", "open": 1, "close": 1,
                           "high": 1, "low": 1, "vol": 1, "amount": 100.0}])
    out, source = fetch_minutes("600519.SH", em=_EM(frame), tg=_TG())
    assert source == "eastmoney"
    assert out.iloc[0]["source"] == "eastmoney"
    assert out.iloc[0]["amount"] == 100.0


def test_a_dead_primary_falls_back_instead_of_reporting_no_bars():
    bars = [["202610281455", "1334.57", "1331.25", "1334.57", "1330.83", "1198.00", {}, "0.96"]]
    out, source = fetch_minutes("600519.SH", em=_EM(boom=True), tg=_TG(bars))
    assert source == "tencent"
    row = out.iloc[0]
    assert row["trade_time"] == "2026-10-28 14:55", "腾讯的时间戳没有分隔符"
    assert row["vol"] == 1198.0
    assert pd.isna(row["amount"]), "腾讯没有成交额，留空而不是编一个"


def test_both_dead_says_which_and_why():
    out, source = fetch_minutes("600519.SH", em=_EM(boom=True), tg=_TG(boom=True))
    assert out.empty
    assert source.startswith("none")
    assert "eastmoney" in source and "tencent" in source


def test_tencent_prefix_is_concatenated_not_comma_separated(monkeypatch):
    """`param=sh600519,m5` works; `param=sh,600519,m5` returns an empty data node."""
    seen: list[str] = []

    class _Capture(_TG):
        def get_json(self, url):
            seen.append(url)
            return _tencent_payload([])

    mod._from_tencent(_Capture(), "600519.SH")
    assert "param=sh600519,m5" in seen[0]


def test_tencent_fallback_uses_the_requested_interval():
    bars = [["202610281455", "1", "1", "1", "1", "10"]]
    tg = _TG(bars, klt=15)
    out, source = fetch_minutes("600519.SH", em=_EM(boom=True), tg=tg, klt=15)

    assert source == "tencent"
    assert "param=sh600519,m15" in tg.last_url
    assert out["trade_time"].tolist() == ["2026-10-28 14:55"]


def test_invalid_tencent_bar_date_is_discarded():
    bars = [
        ["202602301455", "1", "1", "1", "1", "10"],
        ["202610281455", "1", "1", "1", "1", "10"],
    ]
    out = mod._from_tencent(_TG(bars), "600519.SH")
    assert out["trade_date"].tolist() == ["20261028"]


def test_a_symbol_with_no_bars_is_empty_not_an_error():
    out, source = fetch_minutes("600519.SH", em=_EM(pd.DataFrame()), tg=_TG([]))
    assert out.empty
    assert source.startswith("none")
