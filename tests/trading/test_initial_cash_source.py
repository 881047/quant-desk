"""账户规模只能有一个来源：配置。

C-02：``build_desk`` 里写着

    cash = initial_cash or os.getenv("INITIAL_CASH") or settings.initial_cash_text()

而 ``.env`` 里有 ``INITIAL_CASH=5000``、``security.py`` 又在 import 时把整个
``.env`` 灌进进程环境——于是 **env 一直压过 ``risk.initial_cash``**。
``settings.initial_cash()`` 的 docstring 却宣称那三处 ``os.getenv`` 已经消灭。
两个值恰好都是 5000，所以改配置的人不会立刻发现"改了没反应"。

这些用例盯的是同一件事：改 ``params.yaml`` 真的改账户规模；临时覆盖只能走
``QUANT__RISK__INITIAL_CASH``（``/health.config.overridden`` 里点得到名）；
旧的 ``INITIAL_CASH`` 被忽略，但会被 ``/health`` 念出来，不是静默丢弃。
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from service import settings
from service.trading import build_desk, build_samples_desk

D = Decimal
ROOT = Path(__file__).resolve().parents[2]

#: 曾经绕过配置、直接从进程环境读账户规模的变量。
LEGACY_NAMES = ("INITIAL_CASH", "SAMPLES_CASH")


@pytest.fixture
def config_override(monkeypatch):
    """用 settings 支持的 env 覆盖链改一个键，用完把缓存恢复回去。

    ``monkeypatch`` 的清理在本 fixture 的清理之后才跑，所以这里必须自己把变量
    摘掉再 ``load(force=True)``；否则被改过的值会跟着进程级缓存漏进后面的用例。
    """
    def _apply(key: str, value: str) -> None:
        monkeypatch.setenv("QUANT__" + key.upper().replace(".", "__"), value)
        settings.load(force=True)

    yield _apply

    for key in settings.SPECS:
        monkeypatch.delenv("QUANT__" + key.upper().replace(".", "__"), raising=False)
    settings.load(force=True)


def _opening_cash(desk) -> Decimal:
    return D(desk.book.state["initial_cash"])


def test_the_configured_size_is_what_opens_the_book(tmp_path, shared_market,
                                                    config_override):
    """改 ``risk.initial_cash``，新账本的开户余额就得跟着变。"""
    config_override("risk.initial_cash", "6100")

    desk = build_desk(paper_db=tmp_path / "paper.sqlite", market=shared_market)

    assert _opening_cash(desk) == D("6100.00")
    assert D(desk.book.state["account"]["cash_available"]) == D("6100.00")
    assert D(desk.book.state["account"]["cash_withdrawable"]) == D("6100.00")


def test_the_supported_override_is_the_one_health_reports(tmp_path, shared_market,
                                                          config_override):
    """临时覆盖走 ``QUANT__RISK__INITIAL_CASH``，并且来源可查。

    ``/health.config.overridden`` 就是拿 ``provenance()`` 里 ``source == "env"``
    的键拼出来的——覆盖必须在那一份里，否则"改了配置没反应"又换了个地方发生。
    """
    config_override("risk.initial_cash", "6100")

    item = settings.provenance()["risk.initial_cash"]
    assert item["source"] == "env"
    assert D(str(item["value"])) == D("6100")

    desk = build_desk(paper_db=tmp_path / "paper.sqlite", market=shared_market)
    assert _opening_cash(desk) == D("6100.00")


def test_a_legacy_env_var_no_longer_beats_the_config(tmp_path, shared_market,
                                                     monkeypatch):
    """``INITIAL_CASH=9999`` 不再改变账户规模，但会被点名。"""
    monkeypatch.setenv("INITIAL_CASH", "9999")
    configured = settings.initial_cash()

    desk = build_desk(paper_db=tmp_path / "paper.sqlite", market=shared_market)

    assert _opening_cash(desk) == configured
    assert _opening_cash(desk) != D("9999")
    assert settings.legacy_env()["INITIAL_CASH"] == "risk.initial_cash"


def test_an_explicit_argument_still_wins(tmp_path, shared_market, config_override):
    """样本账本与测试传的是显式参数，配置里的环境覆盖不该漏进去。"""
    config_override("risk.initial_cash", "6100")

    desk = build_desk(paper_db=tmp_path / "paper.sqlite", initial_cash="4321.00",
                      market=shared_market)

    assert _opening_cash(desk) == D("4321.00")


def test_the_sample_ledger_has_its_own_configured_size(tmp_path, shared_market,
                                                       config_override):
    """样本账本 2,000 也是配置，不是 import 时读死的常量。"""
    config_override("research.samples_cash", "2500")

    desk = build_samples_desk(root=tmp_path, market=shared_market)

    assert _opening_cash(desk) == D("2500.00")


def _is_env_lookup(func: ast.expr) -> bool:
    """``os.getenv`` 或 ``os.environ.get``。"""
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "getenv":
        return True
    return (func.attr == "get"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ")


def _env_reads(path: Path) -> set[str]:
    """``os.getenv("X")`` / ``os.environ.get("X")`` 里的常量名。

    用 AST 而不是 grep：``settings.py``、``ledger.py``、``models.py`` 的 docstring
    里都写着 ``os.getenv("INITIAL_CASH", "5000")``——那是**解释这段历史**的文本，
    不是读取。按行匹配会把这些说明判成缺陷，然后逼着人删掉解释。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not _is_env_lookup(node.func):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.add(arg.value)
    return names


def test_no_module_reads_an_account_size_from_the_process_env():
    """账户规模不许再从进程环境读——只能经 ``settings``。

    这条是结构闸，不是风格偏好：``.env`` 在 ``security.py`` import 时就被灌进
    进程环境，所以任何一处 ``os.getenv("INITIAL_CASH")`` 都会悄悄压过配置，
    而且不会出现在 ``provenance()`` 里。
    """
    offenders: list[str] = []
    for base in ("service", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            hits = _env_reads(path) & set(LEGACY_NAMES)
            if hits:
                offenders.append(f"{path.relative_to(ROOT)}: {sorted(hits)}")
    assert offenders == [], "这些文件仍从进程环境读账户规模：" + "; ".join(offenders)
