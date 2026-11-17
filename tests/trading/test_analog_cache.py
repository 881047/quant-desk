"""类比索引的缓存策略：一次重建、旧文件不无限堆积。

两个失败模式都是实测出来的，不是假想：

* 冷启动重建 3,410,457 行要 46.8 秒，而宿主工具超时是 20 秒 —— 每个交易日
  第一次调用必然超时；
* 缓存键里含 ``cover_end``，所以每个交易日新增一个约 130MB 的 npz，代码里
  没有任何清理。
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from service.trading import analog


@pytest.fixture(autouse=True)
def _clean_index():
    analog.reset_cache()
    yield
    analog.reset_cache()


class _Registry:
    def summary(self):
        return [{"dataset": "daily", "cover_end": "20261029"}]


class _Store:
    registry = _Registry()

    def read(self, _name):
        return None


def test_concurrent_misses_rebuild_only_once(tmp_path, monkeypatch):
    """重建窗口内进来的并发请求必须复用第一次的结果。

    没有锁时每个请求都会各自 build，串行叠加：三个调用者等两分半，全部超时。
    """
    calls: list[str | None] = []
    built = object()

    def fake_build(store, **kwargs):
        calls.append(kwargs.get("end"))
        time.sleep(0.15)          # 给其它线程进入重建窗口的机会
        return built

    monkeypatch.setattr(analog, "build", fake_build)
    monkeypatch.setattr(analog, "save", lambda index, root: None)
    monkeypatch.setattr(analog, "_prune_cache", lambda root: [])
    monkeypatch.setattr(analog, "default_start", lambda store, end=None: "20050101")

    results: list[object] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(analog.get_index(_Store(), tmp_path, end="20261029"))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(calls) == 1, f"重建了 {len(calls)} 次，同一 key 应当只重建一次"
    assert len(results) == 5
    assert all(result is built for result in results)


def test_prune_keeps_the_newest_caches(tmp_path):
    """旧键不会再命中，但每个 130MB；保留最新几个即可。"""
    cache = tmp_path / "data" / "cache" / "analog"
    cache.mkdir(parents=True)
    made = []
    for i in range(6):
        path = cache / f"key{i}.npz"
        path.write_bytes(b"x")
        os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
        made.append(path)

    removed = analog._prune_cache(tmp_path, keep=4)

    assert len(removed) == 2
    assert not made[0].exists()
    assert not made[1].exists()
    assert all(path.exists() for path in made[2:])


def test_prune_survives_an_empty_or_missing_directory(tmp_path):
    """目录还不存在时预热也会被调用，不能让清理把任务带崩。"""
    assert analog._prune_cache(tmp_path) == []
