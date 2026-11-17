"""请求计数必须落在 API 读它的那份账本里，而且写失败要看得见。

``/usage`` 的 "0 次调用" 有两种含义：没人用过，或者一次都没写成功。以前这两种
情况在响应里完全一样——中间件把异常吞得干干净净，而计数写去的文件在
``QUANT_DATA_ROOT`` 改动之后会和账本分家。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import service.trading as composition
from service import paths
from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app
from service.trading import get_desk
from service.trading.state import PaperStore


@pytest.fixture
def fresh_stats(monkeypatch):
    """每个用例一份干净的计数，别去改别的用例留下的全局状态。"""
    monkeypatch.setattr(app_module, "_usage_stats", {
        "written": 0, "failed": 0, "last_write_at": None, "last_error": None,
    })


REPO = Path(__file__).resolve().parents[2]


def test_the_resolver_defaults_to_the_repository(tmp_path, monkeypatch):
    """环境变量一个都不设时，落到仓库的 ``data/``。

    这条盯的是一个手滑：``service/paths.py`` 在 ``service/`` 下，``parents[2]``
    是盘符根（``D:\\``），不是仓库。测试里 ``PAPER_DB`` 与 ``QUANT_DATA_ROOT``
    永远被 conftest 设着，所以写错层级只有生产会看见——``/health.usage.db``
    报出 ``D:\\data\\paper.sqlite`` 的那一次。
    """
    monkeypatch.delenv("PAPER_DB", raising=False)
    monkeypatch.delenv("QUANT_DATA_ROOT", raising=False)
    assert paths.ROOT == REPO
    assert paths.data_dir() == REPO / "data"
    assert paths.paper_db() == REPO / "data" / "paper.sqlite"
    assert paths.data_dir(tmp_path) == tmp_path / "data"


def test_the_ledger_follows_the_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("PAPER_DB", raising=False)
    assert paths.data_dir() == tmp_path
    assert paths.paper_db() == tmp_path / "paper.sqlite"


def test_the_ledger_override_beats_the_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("PAPER_DB", str(tmp_path / "elsewhere.sqlite"))
    assert paths.paper_db() == tmp_path / "elsewhere.sqlite"


def test_the_desk_and_the_counters_use_one_file(tmp_path, monkeypatch):
    """回归：数据根换到不以 ``data`` 结尾的目录时，两者曾经分家。

    旧代码里 desk 的账本是 ``data_root.parent / "data" / paper.sqlite``，计数
    却是 ``<仓库>/data/paper.sqlite``。于是 ``/usage``（读 desk.paper）永远说
    "没被调用过"，而行数涨在另一个文件里，一声不响。
    """
    root = tmp_path / "elsewhere"          # 故意不以 data 结尾
    monkeypatch.setenv("QUANT_DATA_ROOT", str(root))
    monkeypatch.delenv("PAPER_DB", raising=False)
    monkeypatch.setattr(composition, "_desk", None)
    monkeypatch.setattr(app_module, "_usage", None)

    desk = get_desk()
    assert desk.paper.db_path == root / "paper.sqlite"
    assert app_module._get_usage().db_path == desk.paper.db_path

    app_module._write_usage("/portfolio", "GET", 200)
    assert desk.paper.usage_summary(days=1)["by_endpoint"]["GET /portfolio"] == 1


def _serve(monkeypatch, usage):
    """跑一遍中间件，返回 (响应, 统计)。"""
    monkeypatch.setattr(app_module, "_get_usage", lambda: usage)
    request = Request({
        "type": "http", "method": "GET", "path": "/portfolio", "headers": [],
        "client": ("127.0.0.1", 12345),
        "route": SimpleNamespace(path="/portfolio"),
    })

    async def call_next(_request):
        return JSONResponse({"ok": True})

    return asyncio.run(app_module.count_usage(request, call_next))


def test_a_failed_write_is_counted_named_and_logged(fresh_stats, monkeypatch, caplog):
    class Broken:
        def record_usage(self, *_args):
            raise RuntimeError("database or disk is full")

    with caplog.at_level(logging.WARNING, logger="service.api.app"):
        response = _serve(monkeypatch, Broken())

    # 遥测坏了不能让调用方看到失败——但也不能因此没人知道。
    assert response.status_code == 200
    stats = app_module.usage_stats()
    assert stats["failed"] == 1
    assert stats["written"] == 0
    assert "database or disk is full" in str(stats["last_error"])
    assert any("telemetry write failed" in r.getMessage() for r in caplog.records)


def test_only_the_first_failure_and_every_hundredth_are_logged(fresh_stats, monkeypatch, caplog):
    """故障期间一个请求一条 traceback，会把 service.log 变成只有一种内容。"""

    class Broken:
        def record_usage(self, *_args):
            raise OSError("locked")

    with caplog.at_level(logging.WARNING, logger="service.api.app"):
        for _ in range(3):
            _serve(monkeypatch, Broken())

    assert app_module.usage_stats()["failed"] == 3
    assert len([r for r in caplog.records if "telemetry" in r.getMessage()]) == 1


def test_telemetry_gives_up_before_the_ledger_would(tmp_path, monkeypatch):
    """计数不值得为了一把写锁等三十秒，响应体早就好了。"""
    monkeypatch.setenv("QUANT_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("PAPER_DB", raising=False)
    monkeypatch.setattr(app_module, "_usage", None)
    store = app_module._get_usage()
    assert store.BUSY_TIMEOUT < PaperStore.BUSY_TIMEOUT

    holder = sqlite3.connect(store.db_path)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(app_module.StoreBusy):
            store.record_usage("/portfolio", "GET", 200)
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    assert elapsed < 5, f"计数等了 {elapsed:.1f} 秒"


def test_health_says_where_the_counters_are_going(fresh_stats):
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert set(body["usage"]) == {
        "written", "failed", "last_write_at", "last_error", "db"}
    assert body["usage"]["db"].endswith("paper.sqlite")
    assert body["usage"]["failed"] == 0


def test_usage_names_the_file_it_read(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    body = trading_api.usage(days=1)
    assert Path(body["db"]) == desk.paper.db_path


def test_usage_windows_every_number_to_the_same_days(desk):
    """``days`` 只切 ``by_day`` 的显示时，一个响应里装着两个窗口。

    实测 2026-11-05：``days=1`` 的响应里 ``by_day`` 只有今天一条，而
    ``total`` 是 19,398——11 天的全部历史。桌面面板与 ``/dashboard`` 都用
    ``days=1`` 当"今天用了多少"，一个安静的上午会显示成史上最忙的一天。
    """
    local = datetime.now().astimezone()
    today = local.strftime("%Y%m%d")
    recent = (local - timedelta(days=5)).strftime("%Y%m%d")
    desk.paper.record_usage("/portfolio", "GET", 200, day=today)
    desk.paper.record_usage("/portfolio", "GET", 200, day=recent)
    desk.paper.record_usage("/old/endpoint", "GET", 200, day=recent)

    one = desk.paper.usage_summary(days=1)
    assert one["since"] == today
    assert one["by_day"] == {today: 1}
    assert one["total"] == 1
    assert set(one["by_endpoint"]) == {"GET /portfolio"}

    window = desk.paper.usage_summary(days=10)
    assert window["since"] == (local - timedelta(days=9)).strftime("%Y%m%d")
    assert window["by_day"] == {today: 1, recent: 2}
    assert window["total"] == 3
    assert window["by_endpoint"]["GET /old/endpoint"] == 1


def test_usage_keeps_a_caller_supplied_zero_day_window(desk):
    """``days=0`` 是调用方写错了，不是"给我全部"。

    旧实现里 ``days`` 只影响切片，0 会切出空的 ``by_day`` 配上全量 ``total``
    ——最糟的一种：看起来在回答，其实答的是另一个问题。
    """
    desk.paper.record_usage("/portfolio", "GET", 200)
    body = desk.paper.usage_summary(days=0)
    assert body["days"] == 1
    assert body["total"] == 1
