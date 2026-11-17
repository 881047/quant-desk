"""每一条声明出来的路由都要真的挂在服务上。

``app.openapi()`` 里的每一条路径都来自代码里的装饰器——但装饰器写对了不等于
路由挂对了：前缀写错、路径写错、注册顺序不对，都可能让一条声明存在而实际
404。这份清单是模型唯一的接口目录（``docs/API_CONTRACT.md`` 就是它的表格），
所以"声明在、挂不上"是最坏的一种坏法。

这个文件把每个路由打一遍，断言它不是 404/405。参数用**假的但合法**的值填上：
我们要问的是"这条路径通不通"，不是"这个 id 存不存在"。

它跑在真实的只读行情上（``tests/trading/conftest.py`` 的 ``shared_market``），
所以"今天没有数据"不会伪装成"路由没挂"——两者必须分开看。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from service.api import trading as trading_api
from service.api.app import app
from service.trading import build_samples_desk

TOKEN = {"X-Quant-Token": "change-me-local-only"}

#: 每个路径参数一个能过格式校验的占位值。
PLACEHOLDERS = {
    "symbol": "600000.SH",
    "topic": "ledger",
    "ticket_id": "td-does-not-exist",
    "day": "20261030",
    "as_of": "20261030",
    "dataset": "daily",
    "announcement_id": "1225620609",
    "hyp_id": "hyp-does-not-exist",
}


def _fill(path: str) -> str:
    out = path
    for name, value in PLACEHOLDERS.items():
        out = out.replace("{" + name + "}", value)
    return out


def _routes() -> list[tuple[str, str]]:
    spec = app.openapi()
    routes = []
    for path, ops in spec.get("paths", {}).items():
        for method in ops:
            if method.lower() in {"get", "post"}:
                routes.append((method.upper(), path))
    return sorted(routes)


@pytest.fixture
def client(desk, tmp_path, monkeypatch, shared_market):
    """The real data behind the desk, so "no data today" is not the answer."""
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(trading_api, "get_samples_desk",
                        lambda: build_samples_desk(root=tmp_path, market=shared_market))
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("method,path", _routes(),
                         ids=[f"{m} {p}" for m, p in _routes()])
def test_the_route_is_mounted(client, method, path):
    """路由没挂上会回 404/405；资源不存在也会回 404——两者要分开。

    Starlette 的"这条路径不存在"回的是 ``{"detail": "Not Found"}``；
    应用层的"这个 id 不存在"回的是本项目自己的错误信封（有 code 与说明）。
    所以判据是：405 一律是不合格；404 只有在它是 Starlette 那个光秃秃的
    ``Not Found`` 时才算不合格。
    """
    url = _fill(path)
    if method == "GET":
        response = client.get(url, headers=TOKEN)
    else:
        # POST 的请求体各有各的 schema，这里不测业务，只测路径挂没挂。
        response = client.post(url, json={}, headers=TOKEN)

    assert response.status_code != 405, (
        f"{method} {path} 在 openapi 里声明了，实际却 405")
    if response.status_code == 404:
        body = response.json()
        assert body.get("detail") != "Not Found", (
            f"{method} {path} 在 openapi 里声明了，实际却 404（路由没挂上）")
        # 应用层的 404 是允许的：它说的是"这个 id 不存在"，不是"这条路径不存在"。


def test_the_contract_document_and_the_app_agree_on_the_route_count():
    """文档与服务的路由条数必须一样（双向相等在 test_api_contract_doc.py 里）。"""
    from scripts import api_contract  # 脚本目录不在默认路径上

    rows = api_contract.collect()
    assert len(rows) == sum(
        1 for path, ops in app.openapi()["paths"].items() for m in ops
        if m.lower() in {"get", "post", "put", "patch", "delete"}
    )
