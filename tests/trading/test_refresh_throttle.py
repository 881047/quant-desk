"""强制刷新池子：三种"没抓"必须分得清，失败不能报成别人的成功。

一次强制刷新是全市场 5,500 只，所以它有三道闸：正在进行、刚成功过、刚失败过。
原来"成功"和"失败"共用一个时刻，于是上游挂掉之后 ``refresh_min_gap`` 秒内每个
请求都读到"另一请求 N 秒前刚抓过，直接复用"——调用方没有任何办法知道身后的池子
还是旧的。这个文件钉住三种回答各自的文本与字段。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from service.api import trading as trading_api


@pytest.fixture
def state(monkeypatch):
    """每个用例一把干净的状态：模块级单例在用例之间会串。"""
    fresh = trading_api._RefreshState()
    monkeypatch.setattr(trading_api, "_REFRESH_STATE", fresh)
    return fresh


@pytest.fixture
def snapshot(monkeypatch):
    """抓取换成记录调用，既不碰网络也不写 parquet。"""
    import service.adapters.eastmoney_adapter as eastmoney
    import service.trading.snapshot as intraday

    calls: list[list[str]] = []

    def take(data, registry, adapter, codes, with_tape=True):
        calls.append(list(codes))
        return {"ok": True}

    monkeypatch.setattr(intraday, "snapshot", take)
    monkeypatch.setattr(eastmoney, "EastMoneyAdapter", lambda *a, **k: object())
    return calls


def _desk() -> SimpleNamespace:
    basic = pd.DataFrame({"ts_code": ["600000.SH", "000001.SZ", "830799.BJ"]})
    return SimpleNamespace(data=SimpleNamespace(read=lambda _name: basic),
                           registry=SimpleNamespace(db_path=":memory:"))


def test_a_second_refresh_reuses_the_first_inside_the_gap(state, snapshot, monkeypatch):
    monkeypatch.setattr(trading_api, "_REFRESH_MIN_GAP", 120.0)

    first = trading_api._refresh_pools(_desk())
    assert first["attempted"] is True and first["ok"] is True
    assert snapshot == [["600000.SH", "000001.SZ"]], "北交所不在这条路径里"

    second = trading_api._refresh_pools(_desk())
    assert second["attempted"] is False
    assert "刚抓过" in second["skipped"]
    assert "ok" not in second, "没抓就不该声称结果"
    assert len(snapshot) == 1


def test_a_failure_is_not_reported_as_another_request_s_success(state, snapshot, monkeypatch):
    import service.trading.snapshot as intraday

    def boom(*_a, **_k):
        raise RuntimeError("eastmoney 502")

    monkeypatch.setattr(intraday, "snapshot", boom)
    monkeypatch.setattr(trading_api, "_REFRESH_MIN_GAP", 120.0)

    first = trading_api._refresh_pools(_desk())
    assert first["attempted"] is True
    assert first["ok"] is False
    assert "502" in first["error"]

    second = trading_api._refresh_pools(_desk())
    assert second["attempted"] is False
    assert second["ok"] is False, "这一次没抓，但身后的池子确实没更新"
    assert "刚抓过" not in second["skipped"], "失败被报成了别人的成功"
    assert "失败" in second["skipped"]
    assert "502" in second["last_error"]
    assert 0 < second["retry_after"] <= 120
    assert len(snapshot) == 0, "失败后没有第二次抓取——节流还在"


def test_the_throttle_expires_so_the_next_request_tries_again(state, snapshot, monkeypatch):
    """间隔从配置来，所以它必须能被改；顺便证明它不是写死的 120。

    等 250 ms 而不是 60 ms：``time.monotonic()`` 在 Windows 上取自
    ``GetTickCount64``，实测步长 15–16 ms。第一次调用结束时的读数可能比真实
    时刻晚一格、第二次的读数早一格，于是 ``sleep(0.06)`` 会量出 47 ms——低于
    50 ms 的间隔，第二次被正确地判成"刚抓过"。这条用例因此在负载下五次里红两次，
    而它盯的代码没有问题。阈值留四个量化步长的余量。
    """
    monkeypatch.setattr(trading_api, "_REFRESH_MIN_GAP", 0.05)

    assert trading_api._refresh_pools(_desk())["ok"] is True
    time.sleep(0.25)
    again = trading_api._refresh_pools(_desk())
    assert again["attempted"] is True and again["ok"] is True
    assert len(snapshot) == 2


def test_a_concurrent_refresh_returns_instead_of_queueing_behind_the_first(
        state, monkeypatch):
    """锁只用来认领：第二个请求不该陪着第一个一起等几十秒。

    它原来在锁里做整段抓取，于是两个请求同时打进来时，第二个占着一个线程池 worker
    等完整个 5,500 只，前端看到的是超时。
    """
    import service.adapters.eastmoney_adapter as eastmoney
    import service.trading.snapshot as intraday

    started = threading.Event()
    release = threading.Event()

    def slow(*_a, **_k):
        started.set()
        release.wait(5)
        return {"ok": True}

    monkeypatch.setattr(intraday, "snapshot", slow)
    monkeypatch.setattr(eastmoney, "EastMoneyAdapter", lambda *a, **k: object())

    first: dict = {}
    thread = threading.Thread(target=lambda: first.update(trading_api._refresh_pools(_desk())),
                              daemon=True)
    thread.start()
    assert started.wait(2), "第一个请求没有走到抓取"

    begin = time.monotonic()
    second = trading_api._refresh_pools(_desk())
    elapsed = time.monotonic() - begin

    assert second["attempted"] is False
    assert "另一个刷新" in second["skipped"]
    assert elapsed < 1.0, f"第二个请求等了 {elapsed:.1f} 秒"

    release.set()
    thread.join(5)
    assert first["ok"] is True


def test_a_cancelled_refresh_does_not_leave_the_claim_set(state, monkeypatch):
    """``finally`` 而不是两个分支各写一遍：BaseException 也要清掉"正在抓"。

    漏掉它，``in_flight_since`` 永远留着，之后每一次刷新都会被自己上一次挡住，
    而接口会一直回答"另一个刷新还在跑"——一个永远不会结束的刷新。
    """
    import service.adapters.eastmoney_adapter as eastmoney
    import service.trading.snapshot as intraday

    def cancelled(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(intraday, "snapshot", cancelled)
    monkeypatch.setattr(eastmoney, "EastMoneyAdapter", lambda *a, **k: object())

    with pytest.raises(KeyboardInterrupt):
        trading_api._refresh_pools(_desk())

    assert state.in_flight_since is None
    assert state.last_failure_at is not None
