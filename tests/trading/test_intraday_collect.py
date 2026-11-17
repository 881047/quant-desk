"""分时按需抓取：观察池之外的票，读的时候补一次。

原先分钟表只给"日更认为要看的票"填——持仓、当日涨停/炸板。可模型问的往往是
**筛选器刚选出来的候选**，那些票恰恰不在集合里，于是 `intraday.available=false`，
理由写着"观察池之外的票不抓"。集合之外不是"没有数据"，是"没人去看"，
这两件事的下一步完全不同：前者只能放弃，后者花一秒就能看。

这份测试钉的是：补的时候不改别的行、补不到时说清楚是哪一种补不到、主源挂了不拖累每一个请求。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from service.kernel.clock import SHANGHAI
from service.trading import intraday


class FakeMarket:
    """只提供 `collect` 用到的读与写。"""

    def __init__(self, frames: dict[tuple[str, str], pd.DataFrame]):
        self._frames = frames
        self.writes: list[tuple[str, pd.DataFrame]] = []
        self.store = self

    def minutes(self, symbol: str, day: str) -> pd.DataFrame:
        frame = self._frames.get((symbol, day))
        return pd.DataFrame() if frame is None else frame.copy()

    def write(self, dataset, partition, df, **kwargs):
        self.writes.append((partition, df.copy()))
        # Storing it is half the point: `collect` re-reads so the caller gets the
        # persisted row, not the in-flight frame.
        for symbol, part in df.groupby("ts_code"):
            self._frames[(str(symbol), str(partition))] = part.reset_index(drop=True)


def _bars(symbol: str, day: str, n: int = 6) -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": symbol,
        "trade_time": [f"{day[:4]}-{day[4:6]}-{day[6:]} {9 + i // 12:02d}:{(i % 12) * 5:02d}"
                       for i in range(n)],
        "trade_date": day,
        "open": 10.0, "close": 10.1, "high": 10.2, "low": 9.9,
        "vol": 100.0, "amount": 1000.0, "source": "eastmoney",
    })


def test_a_stored_session_costs_no_network(monkeypatch):
    """表里有的就直接读——按需抓取不能变成"每次调用都联网"。"""
    def boom(*args, **kwargs):
        raise AssertionError("不该联网")

    monkeypatch.setattr("service.adapters.minutes.fetch_minutes", boom)
    market = FakeMarket({("600519.SH", "20261030"): _bars("600519.SH", "20261030")})

    frame, how = intraday.collect(market, "600519.SH", "20261030")

    assert how == "stored" and len(frame) == 6
    assert market.writes == []


def test_a_candidate_outside_the_watch_set_is_fetched_once(monkeypatch):
    day, symbol = "20261030", "002966.SZ"
    monkeypatch.setattr("service.adapters.minutes.fetch_minutes",
                        lambda *a, **k: (_bars(symbol, day), "eastmoney"))
    market = FakeMarket({})

    frame, how = intraday.collect(market, symbol, day)

    assert how == "fetched:eastmoney"
    assert len(frame) == 6, "写完之后要重新读，返回的必须是落盘后的那份"
    assert len(market.writes) == 1
    partition, written = market.writes[0]
    assert partition == day
    assert set(written["ts_code"]) == {symbol}


def test_the_write_is_a_merge_so_other_symbols_survive(monkeypatch):
    """分区里已经有别的票。替换会把它们删掉，而"删掉"在下一次读时看起来像"没抓过"。"""
    day = "20261030"
    monkeypatch.setattr("service.adapters.minutes.fetch_minutes",
                        lambda *a, **k: (_bars("002966.SZ", day), "eastmoney"))
    market = FakeMarket({})
    seen: list[tuple[str, dict]] = []
    real_write = market.write
    market.write = lambda dataset, partition, df, **kw: (
        seen.append((dataset, kw)), real_write(dataset, partition, df, **kw))[1]

    intraday.collect(market, "002966.SZ", day)

    assert seen, "抓到了新数据却没落盘"
    dataset, kwargs = seen[0]
    assert dataset == "stk_mins"
    assert kwargs.get("mode") == "merge"


def test_a_session_the_provider_no_longer_keeps_says_so(monkeypatch):
    """免费分钟线只回看 32 个交易日。回放两个月前要区分"没人抓"和"源不给了"。"""
    monkeypatch.setattr("service.adapters.minutes.fetch_minutes",
                        lambda *a, **k: (_bars("600519.SH", "20261030"), "eastmoney"))
    market = FakeMarket({})

    frame, how = intraday.collect(market, "600519.SH", "20200102")

    assert frame.empty
    assert how == "provider_has_no_20200102"
    assert market.writes == []


def test_fetch_false_never_touches_the_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("不该联网")

    monkeypatch.setattr("service.adapters.minutes.fetch_minutes", boom)
    frame, how = intraday.collect(FakeMarket({}), "600519.SH", "20261030", fetch=False)

    assert frame.empty and how == "not_collected"


def test_a_dead_primary_feed_does_not_cost_every_request(monkeypatch):
    """主源挂掉时每个请求都要付一次重试预算（约 3 秒）。熔断把它变成十分钟一次。"""
    from service.adapters import minutes as adapter

    adapter.reset_breaker()
    calls = {"em": 0}

    class DeadEM:
        def fetch_minutes(self, symbol, klt=5):
            calls["em"] += 1
            raise RuntimeError("connection closed")

    class GoodTG:
        def get_json(self, url):
            return {}

    monkeypatch.setattr("service.adapters.minutes._from_tencent",
                        lambda tg, symbol, klt=5: _bars(symbol, "20261030"))

    for _ in range(3):
        adapter.fetch_minutes("600519.SH", em=DeadEM(), tg=GoodTG())

    assert calls["em"] == 1, "熔断之后不该再试主源"
    assert adapter.em_available() is False
    adapter.reset_breaker()


def test_a_recovering_primary_is_used_again(monkeypatch):
    """熔断不是永久判决：主源恢复后要能重新用上，否则永远只能拿到腾讯那 7 天。"""
    from service.adapters import minutes as adapter

    adapter.reset_breaker()

    class GoodEM:
        def fetch_minutes(self, symbol, klt=5):
            return _bars(symbol, "20261030")

    bars, source = adapter.fetch_minutes("600519.SH", em=GoodEM())

    assert source == "eastmoney"
    assert adapter.em_available() is True


def test_live_refresh_replaces_a_stored_current_day_snapshot(monkeypatch):
    """盘中的快照不是"今天的分时已经取过了"。

    页面每 30 秒刷新时，若分区里已有 11:30 那批快照，`collect` 不能直接返回
    `stored`——那会把同一张图挂到收盘。实时刷新必须重新访问单票源，同时仍
    遵守"当日数据不落库"。
    """
    day, symbol = datetime.now(SHANGHAI).strftime("%Y%m%d"), "600467.SH"
    stored = _bars(symbol, day, n=3)
    fresh = _bars(symbol, day, n=6)
    fresh["close"] = 10.6
    monkeypatch.setattr("service.adapters.minutes.fetch_minutes",
                        lambda *a, **k: (fresh, "eastmoney"))
    market = FakeMarket({(symbol, day): stored})

    frame, how = intraday.collect(market, symbol, day, refresh=True)

    assert len(frame) == 6 and float(frame["close"].iloc[-1]) == 10.6
    assert how.startswith("fetched:eastmoney") and "不落库" in how
    assert market.writes == [], "实时刷新改写了当日分区，晚间 sweep 会漏补这只票"


def test_a_failed_live_refresh_keeps_the_last_stored_chart(monkeypatch):
    """源暂时断掉时保留上一张图，但回执必须说清这是失败，不能把旧值标成实时。"""
    day, symbol = datetime.now(SHANGHAI).strftime("%Y%m%d"), "600467.SH"
    stored = _bars(symbol, day, n=3)

    def boom(*args, **kwargs):
        raise RuntimeError("connection closed")

    monkeypatch.setattr("service.adapters.minutes.fetch_minutes", boom)
    market = FakeMarket({(symbol, day): stored})

    frame, how = intraday.collect(market, symbol, day, refresh=True)

    assert len(frame) == 3, "刷新失败把可用的上一张图清空了"
    assert how == "failed:RuntimeError"
    assert market.writes == []
