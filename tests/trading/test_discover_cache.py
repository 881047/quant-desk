"""/discover 的面板不该被算十遍。

2026-11-02 实测：空闲时单路 ``/discover`` 1.42 秒，10 路并发每路 21.7 秒——
刚好越过客户端 20 秒的超时，于是审计把一次超时记成了"服务不可达"。根因有
两个：面板每次都重算，而服务是单进程 GIL 服务器，CPU 密集的读在 GIL 下线性
串行。

修法是让面板走市场缓存：它是 (as_of, 已落库数据) 的纯函数，60 秒内重算没有
意义；缓存同时具备 single-flight 行为，所以十个并发调用等的是同一次计算。

这个文件钉住那两条性质：TTL 内不重算，并发只算一次。
"""

from __future__ import annotations

import threading
import time

from service.trading.market import MarketRepository


def _repo() -> MarketRepository:
    """A repository with no store: only the cache behaviour is under test."""
    return MarketRepository(store=None, ttl_seconds=60.0)


def test_the_second_call_within_the_ttl_does_not_rebuild():
    market = _repo()
    calls: list[int] = []

    def builder() -> dict:
        calls.append(1)
        return {"as_of": "20261030"}

    first = market.cached_answer("discover:20261030", builder)
    second = market.cached_answer("discover:20261030", builder)

    assert len(calls) == 1, "同一个键在 TTL 内不该重算"
    assert first == second


def test_a_different_day_is_a_different_answer():
    market = _repo()
    calls: list[str] = []

    def builder(day: str):
        def build() -> dict:
            calls.append(day)
            return {"as_of": day}
        return build

    market.cached_answer("discover:20261030", builder("20261030"))
    market.cached_answer("discover:20261031", builder("20261031"))

    assert sorted(calls) == ["20261030", "20261031"]


def test_ten_concurrent_callers_build_once():
    """并发不是"算十遍"，而是"算一遍，等十个人"。

    这一条就是 21.7 秒那件事的直接修复：没有 single-flight，十个并发调用
    各自排队进 GIL，墙钟时间就是十份。
    """
    market = _repo()
    calls: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def builder() -> dict:
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        return {"as_of": "20261030"}

    results: list[dict] = []
    lock = threading.Lock()

    def worker() -> None:
        value = market.cached_answer("discover:20261030", builder)
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    started.wait(timeout=5)
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 10, "十个调用都要拿到答案"
    assert len(calls) == 1, f"面板被算了 {len(calls)} 次，应该只算一次"
    assert all(value == {"as_of": "20261030"} for value in results)


def test_an_expired_entry_is_rebuilt():
    market = MarketRepository(store=None, ttl_seconds=0.05)
    calls: list[int] = []

    def builder() -> dict:
        calls.append(1)
        return {"as_of": "20261030"}

    market.cached_answer("discover:20261030", builder)
    time.sleep(0.12)
    market.cached_answer("discover:20261030", builder)

    assert len(calls) == 2, "TTL 过期后要重算——数据每天都在变"
