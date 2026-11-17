"""Sensitive GET authentication and API middleware concurrency contracts."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import service.trading as composition
from service.api import app as app_module
from service.api import data as data_api
from service.api import security
from service.api import trading as trading_api
from service.api.app import app
from service.trading import get_desk

BEARER = {"Authorization": "Bearer change-me-local-only"}


@pytest.fixture
def client(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    with TestClient(app) as value:
        yield value


def test_sensitive_gets_require_credentials_and_accept_bearer(client, monkeypatch):
    monkeypatch.setattr("service.api.dashboard.collect", lambda _desk, slim=False: {"ok": True})
    for path in ("/account", "/portfolio", "/dashboard.json"):
        missing = client.get(path)
        assert missing.status_code == 401
        assert missing.headers["cache-control"] == "no-store"
        assert missing.headers["www-authenticate"] == "Bearer"

        response = client.get(path, headers=BEARER)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"


def test_account_reads_the_desk_ledger(client, desk):
    desk.book.ledger.account.cash_available = Decimal("4321.00")
    body = client.get("/account", headers=BEARER).json()
    assert body["cash_available"] == "4321.00"
    assert body["equity"] == "4321.00"


def test_remote_mode_refuses_the_documented_default_token(client, monkeypatch):
    monkeypatch.setenv("MODE", "remote")
    assert client.get("/account", headers=BEARER).status_code == 503

    monkeypatch.setattr(security, "TOKEN", "a-configured-test-secret")
    response = client.get(
        "/account", headers={"Authorization": "Bearer a-configured-test-secret"})
    assert response.status_code == 200


def _loopback_request(host: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/account", "headers": [],
        "client": (host, 12345), "server": ("127.0.0.1", 8811), "scheme": "http",
    })


def test_loopback_bypass_is_explicit_and_never_for_remote_hosts(monkeypatch):
    request = _loopback_request("127.0.0.1")
    with pytest.raises(HTTPException) as exc:
        security.require_token(request, Response(), None, None)
    assert exc.value.status_code == 401

    monkeypatch.setenv("QUANT_ALLOW_LOOPBACK", "1")
    security.require_token(request, Response(), None, None)
    with pytest.raises(HTTPException):
        security.require_token(_loopback_request("192.0.2.10"), Response(), None, None)

    monkeypatch.setenv("MODE", "remote")
    with pytest.raises(HTTPException) as exc:
        security.require_token(request, Response(), None, None)
    assert exc.value.status_code == 503


def _run_concurrently(fn, count: int = 8) -> list:
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(lambda _: fn(), range(count)))


def test_usage_store_is_initialized_once(monkeypatch):
    monkeypatch.setattr(app_module, "_usage", None)
    created = []

    class FakeStore:
        def __init__(self, path):
            time.sleep(0.02)
            created.append(path)

    # 计数用的 store 是 ``_UsageStore``（``BUSY_TIMEOUT`` 更短），不是 ``PaperStore``：
    # 这条测的是"只建一个"，不是"用哪个类"。
    monkeypatch.setattr(app_module, "_UsageStore", FakeStore)
    stores = _run_concurrently(app_module._get_usage)
    assert len(created) == 1
    assert all(store is stores[0] for store in stores)


def test_data_store_is_initialized_once(monkeypatch):
    monkeypatch.setattr(data_api, "_registry", None)
    monkeypatch.setattr(data_api, "_store", None)
    created = []

    class FakeRegistry:
        def __init__(self, path):
            time.sleep(0.02)
            created.append(("registry", path))

    class FakeStore:
        def __init__(self, root, registry):
            created.append(("store", root, registry))

    monkeypatch.setattr(data_api, "Registry", FakeRegistry)
    monkeypatch.setattr(data_api, "DatasetStore", FakeStore)
    stores = _run_concurrently(data_api.get_store)
    assert len(created) == 2
    assert len({id(store) for store in stores}) == 1


def test_desk_is_initialized_once(monkeypatch):
    monkeypatch.setattr(composition, "_desk", None)
    created = []

    def fake_build(**kwargs):
        time.sleep(0.02)
        created.append(kwargs)
        return object()

    monkeypatch.setattr(composition, "build_desk", fake_build)
    desks = _run_concurrently(get_desk)
    assert len(created) == 1
    assert all(desk is desks[0] for desk in desks)


def test_usage_sql_runs_off_the_event_loop(monkeypatch):
    seen = {}

    def assert_breaker_is_free():
        assert app_module._breaker_lock.acquire(blocking=False)
        app_module._breaker_lock.release()

    class FakeUsage:
        def record_usage(self, endpoint, method, status):
            assert_breaker_is_free()
            seen["call"] = (endpoint, method, status)
            seen["thread"] = threading.get_ident()

    monkeypatch.setattr(app_module, "_get_usage", lambda: FakeUsage())
    monkeypatch.setenv("API_BREAKER", "1")
    request = Request({
        "type": "http", "method": "GET", "path": "/portfolio", "headers": [],
        "client": ("127.0.0.1", 12345),
        "route": SimpleNamespace(path="/portfolio"),
    })

    async def call_next(_request):
        assert_breaker_is_free()
        return JSONResponse({"ok": True})

    response = asyncio.run(app_module.count_usage(request, call_next))
    assert response.status_code == 200
    assert seen["call"] == ("/portfolio", "GET", 200)
    assert seen["thread"] != threading.get_ident()


def test_usage_updates_are_atomic_under_concurrency(desk, monkeypatch):
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    _run_concurrently(
        lambda: app_module._write_usage("/portfolio", "GET", 200), count=12)
    summary = desk.paper.usage_summary(days=1)
    assert summary["by_endpoint"]["GET /portfolio"] == 12


def test_health_says_when_the_token_is_still_the_published_default(monkeypatch):
    """默认口令写在仓库里，只发一条 import 时的 warning 等于没发。

    `/health` 是唯一不需要鉴权的端点，所以它是最合适的公示位置：监控、看板、
    以及"为什么我这套东西的密码是公开的"这个问题，都从这里看见。
    修它必须同时改 `.env` 和 MCP 客户端那份配置，只改一边会让所有工具调用 401。
    """
    # `app_module` 里那个名字是从 security 导入的函数对象，它的全局就是 security 的
    # 模块字典——所以改 security.TOKEN 就够了，不用再替换一遍函数。
    monkeypatch.setattr(security, "TOKEN", security.DEFAULT_TOKEN)
    monkeypatch.setattr(app_module, "clock", SimpleNamespace(now=lambda: __import__(
        "datetime").datetime(2026, 10, 31, 9, 0)))
    assert app_module.health()["security"]["token_is_default"] is True

    monkeypatch.setattr(security, "TOKEN", "a-real-secret")
    assert app_module.health()["security"]["token_is_default"] is False
