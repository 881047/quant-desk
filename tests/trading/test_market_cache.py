"""Market cache concurrency tests use only in-memory fakes and temporary paths."""

from __future__ import annotations

import threading
import time
from asyncio import CancelledError
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pandas as pd
import pytest

import service.trading.market as market_module
from service.trading.market import MarketRepository


def test_concurrent_cache_miss_builds_only_once(tmp_path):
    market = MarketRepository(SimpleNamespace(root=tmp_path))
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0
    value = object()

    def build():
        nonlocal calls
        with lock:
            calls += 1
        started.set()
        assert release.wait(timeout=2)
        return value

    def read():
        return market._cached("partition", build)

    gate = threading.Barrier(8)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(lambda: (gate.wait(timeout=30), read())[1]) for _ in range(8)]
        assert started.wait(timeout=2)
        release.set()
        results = [future.result(timeout=2) for future in futures]

    assert results == [value] * 8
    assert calls == 1
    assert market._cached("partition", build) is value
    assert calls == 1


def test_cancelled_refresh_wakes_waiter_and_allows_retry(tmp_path):
    market = MarketRepository(SimpleNamespace(root=tmp_path))
    first_started = threading.Event()
    second_calling = threading.Event()
    lock = threading.Lock()
    calls = 0

    def build():
        nonlocal calls
        with lock:
            calls += 1
            attempt = calls
        if attempt == 1:
            first_started.set()
            assert second_calling.wait(timeout=2)
            raise CancelledError
        return "recovered"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(market._cached, "partition", build)
        assert first_started.wait(timeout=2)

        def second_read():
            second_calling.set()
            return market._cached("partition", build)

        second = pool.submit(second_read)
        with pytest.raises(CancelledError):
            first.result(timeout=2)
        assert second.result(timeout=2) == "recovered"

    assert calls == 2
    assert market._cached("partition", build) == "recovered"
    assert calls == 2
    assert not market._refreshing


def test_invalidate_during_build_prevents_stale_cache_write(tmp_path):
    market = MarketRepository(SimpleNamespace(root=tmp_path))
    started = threading.Event()
    release = threading.Event()
    calls = []

    def build():
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            started.set()
            assert release.wait(timeout=2)
            return "old"
        return "new"

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(market._cached, "partition", build)
        assert started.wait(timeout=2)
        market.invalidate()
        release.set()
        assert pending.result(timeout=2) == "old"

    assert market._cached("partition", build) == "new"
    assert calls == [1, 2]


def test_ttl_starts_when_refresh_completes(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(market_module.time, "monotonic", lambda: clock[0])
    market = MarketRepository(SimpleNamespace(root=tmp_path), ttl_seconds=5)
    calls = 0

    def build():
        nonlocal calls
        calls += 1
        clock[0] += 10
        return "value"

    assert market._cached("partition", build) == "value"
    assert market._cached("partition", build) == "value"
    assert calls == 1


def test_calendar_loads_once_concurrently_and_retries_after_failure(tmp_path):
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0

    class FakeStore:
        root = tmp_path

        def read(self, dataset):
            nonlocal calls
            assert dataset == "trade_cal"
            with lock:
                calls += 1
                attempt = calls
            if attempt == 1:
                raise RuntimeError("temporary read failure")
            started.set()
            assert release.wait(timeout=2)
            return pd.DataFrame({"cal_date": ["20260105"], "is_open": [1]})

    market = MarketRepository(FakeStore())
    with pytest.raises(RuntimeError, match="temporary"):
        _ = market.calendar

    gate = threading.Barrier(8)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(lambda: (gate.wait(timeout=30), market.calendar)[1])
            for _ in range(8)
        ]
        assert started.wait(timeout=2)
        release.set()
        calendars = [future.result(timeout=2) for future in futures]

    assert calls == 2
    assert all(calendar is calendars[0] for calendar in calendars)
    assert calendars[0].days == ("20260105",)
    assert market.calendar is calendars[0]
    assert calls == 2


class _StubStore:
    """Just enough DatasetStore for `_year_frame`: projection, and a read count."""

    def __init__(self, frame):
        self.frame = frame
        self.reads = 0

    def read_partition(self, dataset, year, columns=None):
        self.reads += 1
        return self.frame[list(columns)] if columns else self.frame


def test_one_year_is_cached_once_even_when_asked_for_different_columns():
    """The key is `dataset:year`, so the projection must not fork the entry.

    Before: the column list was hashed into the key, and one cold context pack
    held `daily/2026` twice (9 columns and 6) — the same 1.09M-row partition read
    twice and resident twice.
    """
    store = _StubStore(pd.DataFrame({
        "ts_code": ["600000.SH", "000001.SZ"],
        "trade_date": ["20261030", "20261030"],
        "close": [10.0, 11.0],
        "high": [10.5, 11.2],
    }))
    market = MarketRepository(store)

    narrow = market._year_frame("daily", "2026", ["ts_code", "trade_date", "close"])
    assert store.reads == 1
    assert "high" not in narrow.columns

    # A wider request must not be answered from the narrow entry.
    wide = market._year_frame("daily", "2026", ["ts_code", "trade_date", "close", "high"])
    assert "high" in wide.columns
    assert store.reads == 2, "the wider projection needs its own read"

    # …and both shapes are now served from that one grown entry.
    again = market._year_frame("daily", "2026", ["ts_code", "trade_date", "close"])
    assert store.reads == 2
    assert "high" not in again.columns


def test_hits_refresh_the_eviction_stamp_so_the_cache_is_lru_not_fifo():
    store = _StubStore(pd.DataFrame({"ts_code": ["600000.SH"], "close": [10.0]}))
    market = MarketRepository(store)
    # The constructor floors max_entries at 16 on purpose (a cache that holds one
    # frame is not a cache); the ceiling itself is what this test is about.
    market.max_entries = 2
    market._year_frame("daily", "2025", ["ts_code", "close"])
    market._year_frame("daily", "2026", ["ts_code", "close"])
    first = market._cache["daily:2025"].stamp

    time.sleep(0.02)   # Windows monotonic resolution is ~15.6 ms
    market._year_frame("daily", "2025", ["ts_code", "close"])   # a hit
    assert market._cache["daily:2025"].stamp > first

    # Inserting a third key evicts the least recently *used*, which is 2026.
    market._year_frame("daily", "2024", ["ts_code", "close"])
    assert "daily:2025" in market._cache
    assert "daily:2026" not in market._cache


def test_the_hit_path_waits_for_the_cache_lock():
    """命中也要在 ``_cache_condition`` 里判定。

    快路径原来在锁外读 ``_cache`` / ``_versions`` 并写 ``entry.stamp``，而
    ``invalidate()`` 与 ``_evict_oldest()`` 都在锁内改同一个字典。窗口很窄，后果
    却正是 epoch 想防的那件事：``invalidate()`` 之后返回一帧旧数据，还给它盖上
    "刚用过"的时间戳，于是它在 TTL 内再也醒不过来。

    这里不模拟那个窗口（人造的时序不是真实时序），只钉住不变量：锁被别人拿着时，
    命中也不能返回。
    """
    store = _StubStore(pd.DataFrame({"ts_code": ["600000.SH"], "close": [10.0]}))
    market = MarketRepository(store)
    market._year_frame("daily", "2026", ["ts_code", "close"])   # 热身，下一次是命中
    assert store.reads == 1

    returned = threading.Event()

    def read():
        market._year_frame("daily", "2026", ["ts_code", "close"])
        returned.set()

    with market._cache_condition:
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        assert not returned.wait(0.2), "命中路径没等锁就返回了一帧"
    assert returned.wait(2)
    thread.join(2)
    assert store.reads == 1, "等锁之后仍应是命中，不该重建"


def test_readers_survive_an_invalidate_under_them():
    """``invalidate()`` 与读并发时不能抛，返回的帧也不能是半新半旧的。

    这里只是压力测试，不是时序测试：它证明锁纪律在真实交错下成立，而不去断言某个
    特定交错。旧实现的三个字段在锁外被读，任何一次"读到一半被清空"都会让
    ``entry.stamp`` 写到已经不在缓存里的对象上。
    """
    store = _StubStore(pd.DataFrame({"ts_code": ["600000.SH"], "close": [10.0]}))
    market = MarketRepository(store)
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader():
        try:
            while not stop.is_set():
                frame = market._year_frame("daily", "2026", ["ts_code", "close"])
                assert list(frame.columns) == ["ts_code", "close"]
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常要带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    for _ in range(30):
        market.invalidate()
        time.sleep(0.001)
    stop.set()
    for thread in threads:
        thread.join(2)

    assert not errors, errors
    assert store.reads > 1, "invalidate 之后必须重新读，不能一直吃第一帧"


def test_the_default_cache_can_hold_one_profile(tmp_path):
    """512 MB 装不下一个 profile 的工作集（实测 1.26 GB），于是每次调用都在重建。

    2026-11-02 量到的：`quant_profile` 冷 4.1 s、**热** 3.8 s——热不起来，因为 15 个
    年度分区轮着把对方挤出去。上限调到 1.5 GB 之后热调用 0.84 s。
    """
    market = MarketRepository(SimpleNamespace(root=tmp_path))

    assert market.cache_stats()["max_bytes"] >= 1536 * 1024 * 1024


def test_the_cache_budget_yields_to_the_operator(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_MARKET_CACHE_MB", "256")
    market = MarketRepository(SimpleNamespace(root=tmp_path))

    assert market.cache_stats()["max_bytes"] == 256 * 1024 * 1024


def test_evictions_are_counted_so_thrashing_is_visible(tmp_path):
    """一个边填边淘汰的缓存，每次调用的表现和冷缓存一模一样。"""
    market = MarketRepository(SimpleNamespace(root=tmp_path), max_entries=2, max_bytes=64 * 1024 * 1024)

    for index in range(6):
        market._cached(f"k{index}", lambda index=index: pd.DataFrame({"v": [index]}))
        market._cache[f"k{index}"].size = 40 * 1024 * 1024
        market._bytes += 40 * 1024 * 1024

    stats = market.cache_stats()
    assert stats["evictions"] >= 1
    assert "evictions" in stats


# ------------------------------------------------- process-wide snapshots
# `_calendar`, `_universe` and the dividend frame are built once and kept for
# the life of the process; they are not entries in `_cache`. The 17:30 job runs
# in *another* process, so the only cross-process signal is the manifest. These
# cases pin that signal: rewrite a dataset, advance the clock, and the snapshot
# must rebuild — without a restart and without anyone calling `invalidate()`.

class _MovingStore:
    """A store whose manifest and frames move when ``publish`` is called."""

    def __init__(self, basic: pd.DataFrame, namechange: pd.DataFrame,
                 calendar: pd.DataFrame):
        self.frames = {"stock_basic": basic, "namechange": namechange,
                       "trade_cal": calendar}
        self.stamps = {name: 0 for name in self.frames}
        self.registry = self
        self.reads: list[str] = []

    def read(self, dataset):
        self.reads.append(dataset)
        return self.frames[dataset].copy()

    def summary(self):
        return [{"dataset": name, "partitions": 1, "rows": len(frame),
                 "cover_start": None, "cover_end": None,
                 "last_ingested": f"2026-11-04T18:{self.stamps[name]:02d}:00"}
                for name, frame in self.frames.items()]

    def publish(self, dataset: str, frame: pd.DataFrame) -> None:
        self.frames[dataset] = frame
        self.stamps[dataset] += 1


def _universe_frames():
    basic = pd.DataFrame({"ts_code": ["600000.SH"], "name": ["浦发银行"]})
    namechange = pd.DataFrame({"ts_code": [], "name": [], "start_date": [],
                               "end_date": []})
    calendar = pd.DataFrame({"cal_date": ["20261104"], "is_open": [1]})
    return basic, namechange, calendar


def test_a_daily_namechange_rewrite_flips_the_st_flag_without_a_restart(monkeypatch):
    """12.2 的 60 秒 TTL 只重新过滤同一份旧更名表，数据源变了它不知道。

    症状：进程从早盘开到 17:30 之后，日更把一只票标成 ST，带宽还是 10%——
    也就是 `PitUniverse.is_st` 注释里说要防的那件事，实际没防住。
    """
    clock = [1000.0]
    monkeypatch.setattr(market_module.time, "monotonic", lambda: clock[0])
    store = _MovingStore(*_universe_frames())
    market = MarketRepository(store, ttl_seconds=60.0)

    assert market.universe.is_st("600000.SH", "20261104") is False
    assert market.snapshot_stats()["universe_built_at"] is not None

    clock[0] += 61
    store.publish("namechange", pd.DataFrame({
        "ts_code": ["600000.SH"], "name": ["ST浦发"],
        "start_date": ["20261104"], "end_date": [None]}))

    assert market.universe.is_st("600000.SH", "20261104") is True
    assert market.instrument("600000.SH", "20261104").is_st is True


def test_a_probe_without_a_write_keeps_the_universe_and_its_st_cache(monkeypatch):
    """没有写入时不重建：重建会丢掉两千只票的 ST 答案，把 60 秒的检查变成 4 秒。"""
    clock = [1000.0]
    monkeypatch.setattr(market_module.time, "monotonic", lambda: clock[0])
    market = MarketRepository(_MovingStore(*_universe_frames()), ttl_seconds=60.0)

    first = market.universe
    assert first.is_st("600000.SH", "20261104") is False
    assert first._st_cache

    clock[0] += 61
    assert market.universe is first
    assert first._st_cache, "内容没变，逐票缓存不该被清空"


def test_a_new_trading_calendar_replaces_the_one_built_at_startup(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(market_module.time, "monotonic", lambda: clock[0])
    store = _MovingStore(*_universe_frames())
    market = MarketRepository(store, ttl_seconds=60.0)

    assert market.calendar.days == ("20261104",)

    clock[0] += 61
    store.publish("trade_cal", pd.DataFrame({"cal_date": ["20261104", "20261105"],
                                             "is_open": [1, 1]}))

    assert market.calendar.days == ("20261104", "20261105")
    assert market.snapshot_stats()["calendar_built_at"] is not None


def test_invalidate_drops_the_snapshots_not_only_the_frames():
    market = MarketRepository(_MovingStore(*_universe_frames()), ttl_seconds=3600.0)
    universe = market.universe
    assert market.calendar.days == ("20261104",)

    market.invalidate()

    stats = market.snapshot_stats()
    assert stats["universe_built_at"] is None
    assert stats["calendar_built_at"] is None
    assert market.universe is not universe


# ── 三个"行情有多新"的窗口，只能有一个来源 ─────────────────────────────────
def test_the_repository_defaults_to_the_configured_market_window():
    """帧缓存与清单重读读同一个配置键，不再各写一个 60.0。"""
    from service import settings

    repo = MarketRepository(None)

    assert repo.ttl == settings.num("cache.market_seconds")
    assert repo.DATA_CHECK_TTL == repo.ttl


def test_the_st_cache_follows_the_window_it_is_given():
    """``PitUniverse`` 的 ST 缓存窗口由调用方传进来。

    旧代码是类常量 ``ST_CACHE_TTL = 60.0``，旁边一句注释
    "matches ``MarketRepository.ttl``"。注释不约束任何东西：仓库被调成
    3600 秒（``tests/trading/conftest.py`` 就这么干，免得跑一半帧过期）或
    5 秒，ST 标记照样 60 秒——同一个"这份行情有多新"的问题会有两个答案，
    而差异只在数据变化后的那个窗口里显形。
    """
    from service import settings
    from service.adapters.pit import PitUniverse

    default = PitUniverse(basic=pd.DataFrame(), namechange=pd.DataFrame())
    assert default._st_ttl == settings.num("cache.market_seconds")

    pinned = PitUniverse(basic=pd.DataFrame(), namechange=pd.DataFrame(),
                         st_ttl=3600.0)
    assert pinned._st_ttl == 3600.0
