"""The dead-symbol scan is evidence, so it has to run from the repo.

The 2026-11-04 review produced its symbol list from ``.audit-scratch/``, which
``.gitignore`` excludes: the report said "re-runnable" and a fresh clone had no
scanner at all. ``scripts/audit_symbols.py`` is that scanner, and this file pins
its answer so the list cannot quietly grow.

If this fails because you added a symbol nothing calls yet: that is the gate
doing its job. Either wire it up, delete it, or add it here **with a reason**.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audit_symbols

#: Zero production readers, and staying that way on purpose.
EXPECTED_DEAD = {
    # 由 panel_ui/app.js 按字符串名调用
    "parse_text": "scripts/external.py",
    # T0–T3 的唯一文字定义，没有读者也不该删
    "TRUST_TIERS": "service/adapters/catalog.py",
    # t_after_dedup 是按需研究核算 API，暂无生产读者；independence 有测试读者，
    # 扫描器归为 test_only，不能错误地列进 dead 白名单。
    "t_after_dedup": "service/research/independence.py",
}


def _split(entry: str) -> tuple[str, str]:
    path, _, tail = entry.partition(":")
    return path, tail.rsplit(" ", 1)[-1]


def test_the_only_dead_symbols_are_the_documented_ones():
    dead = audit_symbols.scan()["dead"]
    found = {_split(entry)[1] for entry in dead}
    assert found == set(EXPECTED_DEAD), (
        "a symbol with no production reader appeared or disappeared: "
        f"{sorted(found ^ set(EXPECTED_DEAD))}"
    )


def test_each_documented_exception_still_exists_where_it_is_claimed():
    """A whitelist whose entries have moved is worse than no whitelist."""
    for entry in audit_symbols.scan()["dead"]:
        path, name = _split(entry)
        assert EXPECTED_DEAD.get(name) == path, f"{name} moved to {path}"
        assert name in (ROOT / path).read_text(encoding="utf-8")


def test_decorated_functions_are_not_counted_as_dead(tmp_path):
    """``@router.get`` registers the handler; nothing calls it by name.

    The first version of the scanner reported 30 route handlers out of 47
    "dead" symbols, which is exactly the noise that makes a gate ignorable.
    """
    sample = tmp_path / "routes.py"
    sample.write_text(
        "def plain():\n    return 1\n\n"
        "@router.get('/x')\n"
        "def handler():\n    return 2\n",
        encoding="utf-8",
    )
    assert [name for name, _ in audit_symbols._definitions(sample)] == ["plain"]


def test_reads_count_attributes_and_imports(tmp_path):
    sample = tmp_path / "reader.py"
    sample.write_text(
        "from service.catalog import TRUST_TIERS\n"
        "from service import catalog\n"
        "print(catalog.TRUST_TIERS)\n",
        encoding="utf-8",
    )
    reads = audit_symbols._reads(sample)
    assert reads["TRUST_TIERS"] >= 2
    assert reads["catalog"] >= 1


def test_the_scan_finds_enough_definitions_to_be_meaningful():
    """The meta-assertion: "no dead symbols" must not mean "nothing parsed"."""
    report = audit_symbols.scan()
    assert len(report["test_only"]) >= 20, (
        "the test-only list collapsed; the parser or the tree moved")


def test_the_core_book_has_production_readers():
    """``/core/*`` 的路由 409 了，不等于 ``core.py`` 死了。

    ``/discover`` 的 core regime 块、``observation``、``persona``、``arena``、
    ``scripts/core_job.py`` 都还在读它。2026-11-04 的报告把它写成"只由测试保活"，
    照那句话删掉会直接打断发现面板——这条用例就是那句错话的反面。
    """
    report = audit_symbols.scan()
    names = {_split(entry)[1] for entry in report["dead"] + report["test_only"]}
    assert "CoreBook" not in names


def test_the_reference_engine_is_only_exercised_by_tests():
    """``kernel/engine.py`` 是 golden scenario 的可执行规格，不是生产路径。

    ``tests/kernel/test_boundaries.py`` 已经禁止生产模块 import 它；这条把
    "它只被测试读"也钉住，免得下次审查把它当成漏网的死代码删掉。
    """
    test_only = {_split(entry)[1] for entry in audit_symbols.scan()["test_only"]}
    assert "TradingEngine" in test_only
    assert "StaticDataView" in test_only
