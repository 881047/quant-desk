"""Live quotes must be labelled, cached, and degrade without lying."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from service.kernel.clock import SHANGHAI
from service.trading import observation
from service.trading.realtime import Quote, QuoteService, session_state
from tests.trading.helpers import pick_symbols, proposal


def _quote(symbol: str, last: float, stop_below: float = 0.0) -> Quote:
    return Quote(
        symbol=symbol, name="测试", last=Decimal(str(last)),
        pre_close=Decimal(str(last + stop_below)), open=Decimal(str(last)),
        high=Decimal(str(last)), low=Decimal(str(last - 0.1)),
        volume=1_000_000, amount=9_000_000,
        up_limit=Decimal(str(round(last * 1.1, 2))),
        down_limit=Decimal(str(round(last * 0.9, 2))),
        quote_ts="2026-10-26T10:00:00+08:00", age_seconds=2.5,
        source="ths", is_realtime=True,
    )


def test_session_labels_follow_the_exchange_clock():
    morning = datetime(2026, 10, 26, 10, 0, tzinfo=SHANGHAI)
    lunch = datetime(2026, 10, 26, 12, 11, tzinfo=SHANGHAI)
    evening = datetime(2026, 10, 26, 20, 0, tzinfo=SHANGHAI)
    assert session_state(morning)["in_session"] is True
    assert session_state(lunch)["in_session"] is False
    assert session_state(evening)["in_session"] is False
    assert session_state(morning)["label"] == "连续竞价"


def test_quotes_degrade_to_the_stored_bar_and_say_so(desk):
    symbol = pick_symbols(desk, 1)[0]
    service = QuoteService(desk.market, allow_network=False)
    quote = service.quote(symbol)
    assert quote is not None
    assert quote.source == "daily"
    assert quote.is_realtime is False
    assert quote.age_seconds is None
    assert quote.last > 0
    assert quote.up_limit > quote.last > quote.down_limit


def test_quotes_are_cached_within_the_ttl(desk):
    symbol = pick_symbols(desk, 1)[0]
    service = QuoteService(desk.market, allow_network=False, ttl=30.0)
    service.quotes([symbol, symbol])
    first = service.health()["cache"]["entries"]
    service.quotes([symbol])
    assert service.health()["cache"]["entries"] == first
    assert service.health()["cache"]["ttl_seconds"] == 30.0


def test_observation_marks_which_price_it_used(desk):
    day = desk.market.latest_bar_date()
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol), actor="test")
    assert ticket["status"] == "proposed", ticket.get("reasons")
    desk.decisions.approve(ticket["ticket_id"], actor="test")

    stop = float(ticket["stop_price"])
    # A live price under the stop, arriving through the same path the API uses.
    live = {symbol: _quote(symbol, round(stop * 0.98, 2))}
    records = observation.stop_constraints(desk, day, quotes=live)
    assert records
    fact = records[0]["fact"]
    assert fact["price_source"] == "ths"
    assert fact["price_age_seconds"] == 2.5
    assert fact["breach_pct"] < 0
    assert records[0]["attention"] == "high"


def test_limit_prices_follow_the_board_rule(desk):
    symbol = pick_symbols(desk, 1)[0]
    service = QuoteService(desk.market, allow_network=False)
    quote = service.quote(symbol)
    inst = desk.market.instrument(symbol, desk.market.latest_bar_date())
    expected_up = (quote.pre_close * (Decimal(1) + inst.limit_pct)).quantize(Decimal("0.01"))
    assert quote.up_limit == expected_up


def test_context_pack_stays_inside_its_budget(desk):
    from service.trading.context_pack import build_context_pack

    pack = build_context_pack(desk)
    assert pack["account"]["prices"]["realtime_available"] is False
    for row in pack["account"]["positions"]:
        assert row["price_source"] == "book_mark_unknown"
    import json

    size = len(json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= 4096, f"context pack grew to {size} bytes"


def test_context_fit_is_deterministic_bounded_and_reports_omissions():
    from service.trading.context_pack import _fit, _size

    pack = {"as_of": "20261027", "account": {"equity": 5000},
            "candidates": [{"symbol": f"{i}.SZ", "why": "长" * 2000} for i in range(8)],
            "recent_tickets": [{"id": "x" * 1000}]}
    first = _fit(pack)
    second = _fit(pack)
    assert first == second
    assert _size(first) <= 4096
    assert first["omitted"]
    assert len(pack["candidates"]) == 8, "fitting must not mutate the caller's pack"

    pathological = _fit({"as_of": "20261027", "account": {"equity": "x" * 10000}})
    assert _size(pathological) <= 4096 and pathological["omitted"]


def test_context_positions_reuse_the_book_mark_without_quoting_again():
    from types import SimpleNamespace

    from service.trading.context_pack import _positions

    def no_quotes(_symbols):
        raise AssertionError("a second quote request would mix mark timestamps")

    desk = SimpleNamespace(
        market=SimpleNamespace(snapshot=lambda symbol, day: SimpleNamespace(last="10")),
        quotes=no_quotes,
    )
    summary = {"equity": "2000", "positions": [
        {"symbol": "A", "name": "a", "qty": 100, "available_qty": 100,
         "avg_cost": "9", "last_price": "10", "weight": 0.5},
        {"symbol": "B", "name": "b", "qty": 100, "available_qty": 100,
         "avg_cost": "9", "last_price": "11", "weight": 0.55},
    ]}
    rows = _positions(desk, summary, "20261027")
    assert [row["last"] for row in rows] == ["10", "11"]
    assert [row["price_source"] for row in rows] == ["book_mark_unknown"] * 2
    assert all(row["is_realtime"] is None for row in rows)


def test_health_reports_which_source_answered(desk):
    symbol = pick_symbols(desk, 1)[0]
    service = QuoteService(desk.market, allow_network=False)
    service.quotes([symbol])
    health = service.health()
    assert health["sources"]["daily_fallback"] == 1
    assert health["realtime_available"] is False
    assert "session" in health and "in_session" in health["session"]


def test_fallback_is_per_symbol_not_per_batch(desk, monkeypatch):
    """A partial answer from a better source must not shadow the other symbols.

    The old loop stopped at the first non-empty frame. A mixed request — one
    stock, one ETF, one convertible — was satisfied by THS returning the stock
    alone; the other two never reached Tencent and silently degraded to the
    previous close. Nothing raised, the answer was just smaller.
    """
    import pandas as pd

    def frame(code, last):
        return pd.DataFrame([{
            "code": code, "name": "x", "last": last, "pre_close": last,
            "open": last, "high": last, "low": last, "volume": 1.0,
            "amount": 1.0, "quote_ts": None, "source": "x",
        }])

    service = QuoteService(desk.market, allow_network=True)
    monkeypatch.setattr(service, "_fetch_ths",
                        lambda symbols: frame("600519", 10.0))
    monkeypatch.setattr(service, "_fetch_eastmoney",
                        lambda symbols: pd.DataFrame())
    monkeypatch.setattr(service, "_fetch_tencent",
                        lambda symbols: pd.concat([frame("510300", 4.0),
                                                   frame("111012", 100.0)]))

    got = service.quotes(["600519.SH", "510300.SH", "111012.SH"])
    assert set(got) == {"600519.SH", "510300.SH", "111012.SH"}
    assert got["600519.SH"].source == "ths"
    assert got["510300.SH"].source == "tencent"
    assert got["111012.SH"].source == "tencent"


def test_a_later_source_is_not_asked_for_what_it_already_has(desk, monkeypatch):
    """Once THS answers everything, the fallbacks must not be called at all."""
    import pandas as pd

    calls = []

    def frame(code, last):
        return pd.DataFrame([{
            "code": code, "name": "x", "last": last, "pre_close": last,
            "open": last, "high": last, "low": last, "volume": 1.0,
            "amount": 1.0, "quote_ts": None, "source": "x",
        }])

    service = QuoteService(desk.market, allow_network=True)
    monkeypatch.setattr(service, "_fetch_ths",
                        lambda symbols: frame("600519", 10.0))
    monkeypatch.setattr(service, "_fetch_eastmoney",
                        lambda symbols: calls.append("eastmoney") or pd.DataFrame())
    monkeypatch.setattr(service, "_fetch_tencent",
                        lambda symbols: calls.append("tencent") or pd.DataFrame())

    service.quotes(["600519.SH"])
    assert calls == []


def test_a_dead_source_is_skipped_after_repeated_failures(desk, monkeypatch):
    """A channel that keeps throwing must stop being paid for.

    EastMoney returned "Server disconnected" on every call while Tencent was
    answering in 200ms. Each batch still walked into the dead channel first and
    waited out its timeout — the quote tool took 7.6s to produce numbers that
    were already available.
    """
    import pandas as pd

    attempts = {"eastmoney": 0, "tencent": 0}

    def boom(symbols):
        attempts["eastmoney"] += 1
        raise RuntimeError("Server disconnected")

    def ok(symbols):
        attempts["tencent"] += 1
        return pd.DataFrame([{
            "code": "600519", "name": "x", "last": 10.0, "pre_close": 10.0,
            "open": 10.0, "high": 10.0, "low": 10.0, "volume": 1.0,
            "amount": 1.0, "quote_ts": None, "source": "x",
        }])

    service = QuoteService(desk.market, allow_network=True)
    monkeypatch.setattr(service, "_fetch_ths", lambda symbols: pd.DataFrame())
    monkeypatch.setattr(service, "_fetch_eastmoney", boom)
    monkeypatch.setattr(service, "_fetch_tencent", ok)

    for _ in range(5):
        service._cache.clear()
        service.quotes(["600519.SH"])

    assert attempts["eastmoney"] == 3, "熔断前只该试 3 次"
    assert attempts["tencent"] == 5, "每次都应由下一个源作答"
    assert service.breakers()["eastmoney"]["open"] is True


def test_an_empty_answer_does_not_trip_the_breaker(desk, monkeypatch):
    """THS covers A-shares only; an ETF request returns nothing by design."""
    import pandas as pd

    service = QuoteService(desk.market, allow_network=True)
    monkeypatch.setattr(service, "_fetch_ths", lambda symbols: pd.DataFrame())
    monkeypatch.setattr(service, "_fetch_eastmoney", lambda symbols: pd.DataFrame())
    monkeypatch.setattr(service, "_fetch_tencent", lambda symbols: pd.DataFrame())

    for _ in range(5):
        service._cache.clear()
        service.quotes(["510300.SH"])
    assert service.breakers().get("ths", {}).get("open") is not True


def test_a_working_source_closes_its_breaker(desk, monkeypatch):
    import pandas as pd

    state = {"fail": True}

    def flaky(symbols):
        if state["fail"]:
            raise RuntimeError("down")
        return pd.DataFrame([{
            "code": "600519", "name": "x", "last": 10.0, "pre_close": 10.0,
            "open": 10.0, "high": 10.0, "low": 10.0, "volume": 1.0,
            "amount": 1.0, "quote_ts": None, "source": "x",
        }])

    service = QuoteService(desk.market, allow_network=True)
    monkeypatch.setattr(service, "_fetch_ths", flaky)
    monkeypatch.setattr(service, "_fetch_eastmoney", lambda symbols: pd.DataFrame())
    monkeypatch.setattr(service, "_fetch_tencent", lambda symbols: pd.DataFrame())

    for _ in range(3):
        service._cache.clear()
        service.quotes(["600519.SH"])
    assert service.breakers()["ths"]["open"] is True

    service._breaker["ths"]["until"] = 0.0   # cooldown elapses
    state["fail"] = False
    service._cache.clear()
    service.quotes(["600519.SH"])
    assert service.breakers()["ths"]["open"] is False
    assert service.breakers()["ths"]["consecutive_failures"] == 0


def test_breaker_state_survives_a_restart(desk, monkeypatch, tmp_path):
    """A vendor outage outlives the process. So must the breaker.

    Without persistence every deploy paid three more 7.9-second timeouts
    relearning what the previous process already knew.
    """
    import pandas as pd

    store = tmp_path / "breakers.json"

    def boom(symbols):
        raise RuntimeError("Server disconnected")

    first = QuoteService(desk.market, allow_network=True, breaker_file=store)
    monkeypatch.setattr(first, "_fetch_ths", lambda s: pd.DataFrame())
    monkeypatch.setattr(first, "_fetch_eastmoney", boom)
    monkeypatch.setattr(first, "_fetch_tencent", lambda s: pd.DataFrame())
    for _ in range(3):
        first._cache.clear()
        first.quotes(["600519.SH"])
    assert first.breakers()["eastmoney"]["open"] is True
    assert store.exists(), "熔断打开时必须落盘"

    # A fresh process: same desk, new service, empty in-memory state.
    second = QuoteService(desk.market, allow_network=True, breaker_file=store)
    assert second.breakers()["eastmoney"]["open"] is True, "重启后应直接是打开的"
    assert second.breakers()["eastmoney"]["consecutive_failures"] >= 3


def test_an_expired_cooldown_is_not_restored_as_open(desk, tmp_path):
    """A stored deadline in the past must load as closed, not as 'open forever'."""
    import json
    import time

    store = tmp_path / "breakers.json"
    store.write_text(json.dumps({"eastmoney": {
        "fails": 5, "until_wall": time.time() - 60, "last_error": "old",
    }}), encoding="utf-8")

    svc = QuoteService(desk.market, allow_network=True, breaker_file=store)
    assert svc.breakers()["eastmoney"]["open"] is False
    assert svc.breakers()["eastmoney"]["retry_in_seconds"] == 0.0


def test_a_corrupt_breaker_file_does_not_stop_the_service(desk, tmp_path):
    store = tmp_path / "breakers.json"
    store.write_text("{not json", encoding="utf-8")
    svc = QuoteService(desk.market, allow_network=True, breaker_file=store)
    assert svc.breakers() == {}


# ------------------------------------------------------- synthetic-only quality gates
class _FakeInstrument:
    name = "fake"
    limit_pct = Decimal("0.10")


class _FakeMarket:
    def latest_bar_date(self):
        return "20261028"

    def instrument(self, symbol, day):
        return _FakeInstrument()

    def snapshot(self, symbol, day):
        return None


def _vendor_row(code: str, **overrides) -> dict:
    row = {"code": code, "name": "fake", "last": 10.0, "pre_close": 9.0,
           "open": 10.0, "high": 10.0, "low": 10.0, "volume": 100.0,
           "amount": 1000.0, "quote_ts": 1_700_000_000, "source": "fake"}
    row.update(overrides)
    return row


def test_a_vendor_cannot_insert_an_unrequested_quote_into_the_cache(monkeypatch):
    import pandas as pd

    service = QuoteService(_FakeMarket(), allow_network=True)
    monkeypatch.setattr(service, "_fetch_ths", lambda symbols: pd.DataFrame([
        _vendor_row("600000"), _vendor_row("000001")]))
    monkeypatch.setattr(service, "_fetch_eastmoney", lambda symbols: pd.DataFrame())
    monkeypatch.setattr(service, "_fetch_tencent", lambda symbols: pd.DataFrame())

    got = service.quotes(["600000.SH"])
    assert set(got) == {"600000.SH"}
    assert set(service._cache) == {"600000.SH"}


def test_duplicate_symbols_only_fall_back_once(monkeypatch):
    service = QuoteService(_FakeMarket(), allow_network=False)
    calls = []
    monkeypatch.setattr(service, "_from_daily", lambda symbol: calls.append(symbol) or None)
    assert service.quotes(["600000.SH", "600000.sh", " 600000.SH "]) == {}
    assert calls == ["600000.SH"]


def test_non_finite_vendor_numbers_never_become_quote_facts():
    import pandas as pd

    service = QuoteService(_FakeMarket(), allow_network=False)
    infinite = service._to_quotes(pd.DataFrame([_vendor_row("600000", last=float("inf"))]))
    assert infinite == {}

    got = service._to_quotes(pd.DataFrame([_vendor_row(
        "600000", volume=float("nan"), amount=float("inf"))]))["600000.SH"]
    assert got.volume == 0.0
    assert got.amount == 0.0
    assert got.suspended is True


def test_non_finite_or_non_positive_epoch_is_not_a_quote_timestamp():
    import pandas as pd

    now = pd.Timestamp("2026-10-28T10:00:00", tz=SHANGHAI)
    for value in (float("nan"), float("inf"), float("-inf"), 0, -1):
        assert QuoteService._timestamp(value, now) == (None, None)


def test_only_a_fresh_same_session_exchange_timestamp_is_realtime():
    import pandas as pd

    service = QuoteService(_FakeMarket(), allow_network=False)

    def at(now, age):
        stamp = now - pd.Timedelta(seconds=age)
        frame = pd.DataFrame([_vendor_row("600000", quote_ts=stamp.timestamp())])
        return service._to_quotes(frame, now=now)["600000.SH"]

    now = pd.Timestamp("2026-10-28T10:00:00", tz=SHANGHAI)
    fresh = at(now, 30)
    assert fresh.is_realtime is True and fresh.realtime_reason is None
    assert at(now, 121).realtime_reason == "stale_quote"
    future = at(now, -1)
    assert future.is_realtime is False
    assert future.age_seconds < 0 and future.realtime_reason == "future_timestamp"
    assert at(now, 86400).realtime_reason == "cross_session"
    missing = service._to_quotes(pd.DataFrame([
        _vendor_row("600000", quote_ts=None)]), now=now)["600000.SH"]
    assert missing.is_realtime is False and missing.realtime_reason == "missing_timestamp"


def test_session_state_accepts_an_exchange_calendar_for_weekday_holidays():
    class Calendar:
        @staticmethod
        def is_trading_day(day):
            return False

    morning = datetime(2026, 10, 26, 10, 0, tzinfo=SHANGHAI)
    state = session_state(morning, calendar=Calendar())
    assert state["trading_day"] is False
    assert state["in_session"] is False
    assert state["label"] == "非交易日"


def test_concurrent_callers_share_one_symbol_fetch(monkeypatch):
    import threading

    service = QuoteService(_FakeMarket(), allow_network=True, ttl=30)
    started = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    calls = []
    results = []

    def fetch(symbols):
        calls.append(list(symbols))
        started.set()
        assert release.wait(2)
        return {symbols[0]: _quote(symbols[0], 10.0)}

    monkeypatch.setattr(service, "_fetch", fetch)
    first = threading.Thread(target=lambda: results.append(service.quotes(["600000.SH"])))
    second = threading.Thread(target=lambda: (
        second_entered.set(), results.append(service.quotes(["600000.SH"]))))
    first.start()
    assert started.wait(1)
    second.start()
    assert second_entered.wait(1)
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert len(calls) == 1
    assert len(results) == 2 and all("600000.SH" in result for result in results)


def test_breaker_updates_merge_across_service_instances(tmp_path):
    import json

    store = tmp_path / "breakers.json"
    first = QuoteService(_FakeMarket(), breaker_file=store)
    second = QuoteService(_FakeMarket(), breaker_file=store)
    for _ in range(3):
        first._record_source("eastmoney", ok=False, now=1.0, detail="down")
    for _ in range(3):
        second._record_source("tencent", ok=False, now=1.0, detail="down")
    payload = json.loads(store.read_text(encoding="utf-8"))
    assert set(payload) == {"eastmoney", "tencent"}

    first._record_source("eastmoney", ok=True, now=2.0)
    payload = json.loads(store.read_text(encoding="utf-8"))
    assert payload["eastmoney"]["fails"] == 0
    assert payload["tencent"]["fails"] == 3
    assert list(tmp_path.glob("*.tmp")) == []


def test_the_same_digits_on_two_markets_are_two_instruments():
    """`000001.SH` 是上证指数，`000001.SZ` 是平安银行。

    2026-11-02：东财的 secid 只按六位数字判市场，于是指数的请求被发成银行，
    回包又被 `sym in remaining` 丢掉，表现成"这个指数没有实时价"。
    """
    from service.adapters.eastmoney_adapter import _secid

    assert _secid("000001.SH") == "1.000001"
    assert _secid("000001.SZ") == "0.000001"
    assert _secid("600519.SH") == "1.600519"
    assert _secid("430047.BJ") == "0.430047"
    assert _secid("000001") == "0.000001", "没有后缀时按数字猜，保持旧行为"


def test_an_index_does_not_get_a_fabricated_price_band(desk):
    """指数没有涨跌停。给它算 ±10% 会得到一个很像价格、实际不存在的数。"""
    quote = desk.quotes(["000001.SH"]).get("000001.SH")
    if quote is None:
        import pytest

        pytest.skip("index quote unavailable offline")

    body = quote.as_dict()
    assert body["up_limit"] == "0"
    assert body["down_limit"] == "0"
    # 指数没有涨跌停，所以封板状态只能是"没碰过"或"这条报价答不了"（日线来源）。
    assert body["limit_state"] in ("none", None)
    assert "没有涨跌停" in body["limits_note"]


def test_a_daily_bar_does_not_claim_a_live_limit_state():
    """`source=daily` 是库里最后一根日线，它的 ``sealed`` 是**那一天**的。

    2026-11-03 09:26 拿到的 7.86 是 11-02 的收盘价，也确实是 11-02 的涨停价，
    于是 `limit_state` 算出 `sealed`，002343 被剔成"买不到"——而 11-03 的涨停价
    是 8.65，该票收 8.30。昨天的封板状态不是今天的封板状态。
    """
    stale = Quote(
        symbol="002343.SZ", name="慈文传媒", last=Decimal("7.86"),
        pre_close=Decimal("7.15"), open=Decimal("7.86"),
        high=Decimal("7.86"), low=Decimal("7.86"),
        volume=1_000_000, amount=7_860_000,
        up_limit=Decimal("7.86"), down_limit=Decimal("6.44"),
        quote_ts="20261102", age_seconds=None,
        source="daily", is_realtime=False,
        timestamp_basis="daily_bar",
    )
    body = stale.as_dict()
    assert body["limit_state"] is None
    # 空值必须自带原因，否则读的人会把 None 当成"没有涨停这回事"。
    assert "daily" in body["limit_state_note"]
    assert "不是现在" in body["limit_state_note"]

    # 同一个报价换成盘中来源，就该给出状态——判据是来源，不是价格。
    live = Quote(**{**stale.__dict__, "source": "ths", "is_realtime": True})
    assert live.limit_state == "one_word"



def test_every_window_gets_its_own_name():
    """09:26 可以被下单，但不能被当作"现在就能按现价成交"。

    标签原来对 09:15–11:30 整段都说「连续竞价」，于是 09:26 的自动化以为
    市价单当场成交，而内核按 PRE_OPEN 拒绝（`MARKET_CLOSED`）。一个时段两个
    名字，是那次冲突的根。

    第二版把"不是连续竞价"的都叫「集合竞价/待开盘」，于是 15:01（已收盘，
    15:00–15:05 留在 SESSIONS 里只为让快照算这一场）读起来像"就要开盘"，
    11:30–13:00 读起来像"盘前/盘后"。名字是模型唯一能问的时钟，含糊就是错。
    """
    day = (2026, 10, 26)          # 周一
    at = lambda h, m: datetime(*day, h, m, tzinfo=SHANGHAI)

    assert session_state(at(9, 26))["in_session"] is True
    assert session_state(at(9, 26))["label"] == "开盘集合竞价"
    assert session_state(at(9, 30))["label"] == "连续竞价"
    assert session_state(at(12, 0))["label"] == "午休"
    assert session_state(at(14, 58))["label"] == "收盘集合竞价"
    assert session_state(at(15, 1))["label"] == "已收盘"
    assert session_state(at(20, 0))["label"] == "盘前/盘后"

    # 收单不等于撮合：能下单的三个窗口没有一个是"连续竞价"。
    for h, m in ((9, 26), (14, 58)):
        assert session_state(at(h, m))["in_session"] is True
        assert session_state(at(h, m))["label"] != "连续竞价"
