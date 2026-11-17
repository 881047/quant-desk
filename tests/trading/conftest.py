"""Trading desk fixtures. Uses the real read-only data store; paper DB is fresh per test.

**Fixtures only.** Plain helpers live in :mod:`tests.trading.helpers` — a conftest
that test modules also import for helpers can be loaded twice under two names
and lose its fixture registration depending on collection order (measured
2026-11-07: ``fixture 'desk' not found``, 12 errors). The names below are
re-exported for backwards compatibility, but no test module should import them
from here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from service.trading import build_desk
from tests.trading.helpers import (  # noqa: F401 - re-exported, see docstring
    ROOT,
    pick_st_symbol,
    pick_symbols,
    pin_to_dataset_bar,
    pin_to_last_bar,
    proposal,
)


@pytest.fixture(scope="session")
def real_registry_copy(tmp_path_factory) -> Path:
    """Snapshot production metadata through SQLite's read-only URI, without DDL.

    The copy is a snapshot while the parquet files are live, and the daily job
    rewrites partitions while the suite runs. That mismatch reads as a broken
    store: the manifest names a version the writer has already replaced or
    pruned, and every read of that partition raises
    ``StoreCorruptionError: current manifest has no readable parquet/metadata``.

    Measured 2026-11-02: the 17:30 job was still running when the suite
    started, and 17 tests failed on that error — not on anything the code did.
    So the copy is made consistent with the files that are actually there:
    a row whose file is gone is dropped, and the store then serves the
    previous partition of that dataset, which is exactly what it would have
    served before the writer got to it.
    """
    source = ROOT / "data" / "registry.sqlite"
    target = tmp_path_factory.mktemp("real-data") / "registry.sqlite"
    with sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as src, \
            sqlite3.connect(target) as dst:
        src.backup(dst)
        stale = [
            row for row in dst.execute(
                "SELECT dataset, partition, path FROM dataset_manifest")
            if not Path(row[2]).exists()
        ]
        for dataset, partition, _path in stale:
            dst.execute(
                "DELETE FROM dataset_manifest WHERE dataset=? AND partition=?",
                (dataset, partition))
        if stale:
            dst.commit()
    return target


@pytest.fixture(scope="session")
def shared_market(real_registry_copy):
    """One read-only market repository for the whole session.

    The `desk` fixture used to build a fresh one per test, so every test re-read
    the year partitions behind the guard, the screen and the context pack. That
    is what made the suite take twenty minutes: the desk is cheap, the *data
    behind it* is not, and none of it changes during a run. Market data is
    read-only by contract, and the few tests that patch a method on
    ``desk.market`` do so through ``monkeypatch``, which restores the attribute
    at teardown — so sharing the object does not leak state between cases.
    """
    from service.adapters.parquet_store import DatasetStore
    from service.adapters.sqlite_store import Registry
    from service.trading.market import MarketRepository

    # The 60s TTL exists so a running service picks up a partition written a
    # minute ago. A long TTL is not a weakened assertion — it is the same data
    # without re-reading it.
    #
    # 清单仍是那份拷贝（测试会往里写：`intraday.collect` 抓到数据要合并分区），
    # 但多给一个回退：拷贝里的行指向的文件已被写者剪掉时，去问当前的那一份。
    # 那份拷贝的前提本来是"跑测试期间没有人写"，而盘中不成立——`snapshot_intraday`
    # 每 40 秒发布一版并剪掉旧文件，几行记录几分钟后就悬空，
    # 每次读都抛 "current manifest has no readable parquet/metadata"。
    # 2026-11-04 09:41 就是这样红了 4 条。
    return MarketRepository(
        DatasetStore(ROOT / "data",
                     Registry(real_registry_copy,
                              fallback_to=ROOT / "data" / "registry.sqlite")),
        ttl_seconds=3600.0)


@pytest.fixture
def desk(tmp_path, monkeypatch, real_registry_copy, shared_market):
    # The protocol tests below assert the ticket/guard contract, not the execution
    # micro-structure. They run with the legacy same-bar fill so a ticket is
    # observable immediately; the next-open queue has its own file
    # (test_execution_next_open.py) with synthetic bars.
    monkeypatch.setenv("FILL_MODE", "close")
    # Tests must never call a market-data vendor: no network in the suite, and a
    # live price would make assertions depend on the hour the suite runs.
    monkeypatch.setenv("REALTIME_QUOTES", "0")
    # The 4xx breaker is a production guard against a caller retrying a wrong
    # route 241 times. A test file legitimately sends many 4xx in one second, and
    # the counter is module-level, so it would leak across cases.
    monkeypatch.setenv("API_BREAKER", "0")
    d = build_desk(ROOT, paper_db=tmp_path / "paper.sqlite", initial_cash="5000",
                   registry_db=real_registry_copy, market=shared_market)
    date = pin_to_last_bar(d)
    d.book.rollover(date)
    return d
