"""清单指针不能倒退——它指着磁盘上一个真实存在的文件。

2026-11-02 的 17:30 日更之后，生产库里出现了这样的清单：

    limit_up_pool[20261102]     → 指向一个已经不在磁盘上的版本
    limit_down_pool[20261102]   → 同上
    broken_board_pool[20261102] → 同上

于是每一次读这三个分区都抛 "current manifest has no readable
parquet/metadata"——而池子端点正是盘中盘后要看的东西。

根因是同一个分区的多个写入者：10:30 的盘中快照、15:20 的收盘任务、17:30 的
日更都写 ``limit_up_pool``。每个写入者都提交自己的清单，而**最后提交的那个
说了算**——不是最新写的那次。一个 10:30 开工、17:35 才提交的慢写入者把指针
拖回了它自己的 10:30 版本，而那个版本已经被后来的写入者清理掉了。

所以清单的更新必须是**有条件的**：只有不比现有那条旧的记录，才允许接管指针。
``ingested_at`` 在记录构造时打戳——也就是写入**开始**的时候——所以它给出的
就是写入者的先后顺序，而这正是我们要的顺序。

这个文件把这条规则钉住，并把"指针指向的文件必须存在"变成一条不变式。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from service.adapters.sqlite_store import ManifestRecord, Registry
from tests.data.helpers import make_daily


def _record(dataset: str, partition: str, path: str, ingested_at: str,
            snapshot_id: str = "snap") -> ManifestRecord:
    return ManifestRecord(
        dataset=dataset,
        partition=partition,
        source="test",
        trust_tier="raw",
        as_of="20240104",
        ingested_at=ingested_at,
        rows=1,
        snapshot_id=snapshot_id,
        checksum="c" * 64,
        schema_hash="s" * 16,
        cover_start="20240104",
        cover_end="20240104",
        status="ok",
        path=path,
    )


def test_a_slower_writer_cannot_drag_the_pointer_back(registry):
    """旧写入者的清单不得接管指针。"""
    registry.upsert_manifest(_record("daily", "2024", "/tmp/new.parquet",
                                      "2026-11-02T17:30:00+08:00", "new"))
    # 一个 10:30 开工、17:35 才提交的慢写入者：它的记录更旧。
    registry.upsert_manifest(_record("daily", "2024", "/tmp/old.parquet",
                                      "2026-11-02T10:30:00+08:00", "old"))

    live = registry.get_manifest("daily", "2024")
    assert live is not None
    assert live.path == "/tmp/new.parquet", (
        f"指针被拖回了 {live.path}——那正是会被清理掉的旧版本")
    assert live.snapshot_id == "new"


def test_a_newer_write_does_take_the_pointer(registry):
    """正常情况：新写入者接管指针。"""
    registry.upsert_manifest(_record("daily", "2024", "/tmp/old.parquet",
                                      "2026-11-02T10:30:00+08:00", "old"))
    registry.upsert_manifest(_record("daily", "2024", "/tmp/new.parquet",
                                      "2026-11-02T17:30:00+08:00", "new"))

    live = registry.get_manifest("daily", "2024")
    assert live is not None
    assert live.path == "/tmp/new.parquet"


def test_the_pointer_always_names_a_file_that_exists(store, registry):
    """不变式：清单指着的文件必须在磁盘上。"""
    days = ["20240102", "20240103"]
    store.write("daily", "2024", make_daily(days), as_of="20240103")

    live = registry.get_manifest("daily", "2024")
    assert live is not None
    assert Path(live.path).exists(), (
        f"清单指着 {live.path}，而它不在磁盘上")


def test_two_writers_on_one_partition_leave_a_readable_pointer(store, registry):
    """两个写入者先后写同一个分区，指针必须仍然可读。

    这就是 2026-11-02 17:30 的形状：一次写入被登记之后，另一个更旧的记录
    迟到了。登记表不许它接管，但存储层也必须能读。
    """
    days = ["20240102"]
    first = store.write("daily", "2024", make_daily(days), as_of="20240102")

    # 迟到的旧记录：内容更旧，路径指向一个已经不存在的版本。
    registry.upsert_manifest(_record(
        "daily", "2024", str(Path(first.path).with_name("gone.parquet")),
        "2020-01-01T00:00:00+08:00", "gone"))

    frame = store.read("daily", "20240102", "20240104")
    assert not frame.empty, "分区必须仍然读得出来"


def test_the_repair_script_finds_and_repoints_a_broken_pointer(tmp_path,
                                                                monkeypatch):
    """``scripts/repair_manifest.py`` 能把断掉的指针接回去。

    根因已经修掉（``upsert_manifest`` 不许倒退），但**已经坏掉的指针**还在
    库里——它们要能被修，而修的办法是把指针指回磁盘上还活着的那一版。
    """
    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "repair_manifest", root / "scripts" / "repair_manifest.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["repair_manifest"] = module
    spec.loader.exec_module(module)

    # 一个"坏掉"的清单：指向不存在的文件；旁边有一版真实存在的。
    data_root = tmp_path / "parquet"
    versions = data_root / "daily" / ".versions" / "2024"
    versions.mkdir(parents=True)
    good = versions / "2024.good.parquet"
    good.write_bytes(b"x")
    import json

    (versions / "2024.good.meta.json").write_text(json.dumps({
        "dataset": "daily", "partition": "2024", "rows": 1,
        "snapshot_id": "good", "checksum": "c" * 64, "schema_hash": "s" * 16,
        "as_of": "20240104", "cover_start": "20240104", "cover_end": "20240104",
    }), encoding="utf-8")

    registry_path = tmp_path / "registry.sqlite"
    reg = Registry(registry_path)
    reg.upsert_manifest(_record("daily", "2024", str(versions / "2024.gone.parquet"),
                                "2026-11-02T17:30:00+08:00", "gone"))

    monkeypatch.setattr(module, "REGISTRY", registry_path)
    monkeypatch.setattr(module, "DATA_ROOT", data_root)

    entries = module.plan()
    assert entries, "坏掉的清单应该被列出来"
    assert entries[0]["repairable"] is True
    assert entries[0]["repair_to"] == str(good)

    fixed = module.apply(entries)
    assert fixed == 1
    live = reg.get_manifest("daily", "2024")
    assert live is not None
    assert Path(live.path).exists()


@pytest.fixture()
def repair_module():
    """把 ``scripts/repair_manifest.py`` 载入成模块，路径由用例改指到 tmp。"""
    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "repair_manifest", root / "scripts" / "repair_manifest.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["repair_manifest"] = module
    spec.loader.exec_module(module)
    return module


def _broken_partition(tmp_path, monkeypatch, module, ingested_at="2026-11-02T17:30:00+08:00"):
    """一个断掉的指针，旁边放一版真实存在的；返回 (registry, 好版本路径)。"""
    import json

    data_root = tmp_path / "parquet"
    versions = data_root / "daily" / ".versions" / "2024"
    versions.mkdir(parents=True)
    good = versions / "2024.good.parquet"
    good.write_bytes(b"x")
    (versions / "2024.good.meta.json").write_text(json.dumps({
        "dataset": "daily", "partition": "2024", "rows": 1,
        "snapshot_id": "good", "checksum": "c" * 64, "schema_hash": "s" * 16,
        "as_of": "20240104", "cover_start": "20240104", "cover_end": "20240104",
    }), encoding="utf-8")

    registry_path = tmp_path / "registry.sqlite"
    reg = Registry(registry_path)
    reg.upsert_manifest(_record("daily", "2024", str(versions / "2024.gone.parquet"),
                                ingested_at, "gone"))

    monkeypatch.setattr(module, "REGISTRY", registry_path)
    monkeypatch.setattr(module, "DATA_ROOT", data_root)
    return reg, good


def test_the_repair_script_leaves_a_pointer_someone_else_already_fixed(
        tmp_path, monkeypatch, repair_module):
    """``plan`` 之后清单被别人改好了，``apply`` 不许把它改回去。

    plan 用的是只读连接、另一时刻的快照。这中间的几秒里日更可能已经发布了新
    版本——旧版 ``apply`` 无条件 UPDATE，于是"修复工具"成了把指针拖回去的那
    条旁路，正是 ``upsert_manifest`` 的守卫要挡的事。
    """
    reg, good = _broken_partition(tmp_path, monkeypatch, repair_module)
    entries = repair_module.plan()
    assert entries and entries[0]["repairable"] is True

    # 另一个写入者在 plan 之后把这一行指向了有效的版本。
    reg.upsert_manifest(_record("daily", "2024", str(good),
                                "2026-11-02T18:00:00+08:00", "good"))

    fixed = repair_module.apply(entries)
    assert fixed == 0, "清单已经有效，修复脚本不该再动它"
    assert reg.get_manifest("daily", "2024").path == str(good)


def test_the_repair_script_skips_a_target_that_was_pruned_meanwhile(
        tmp_path, monkeypatch, repair_module):
    """计划指向的那一版在 apply 之前被清掉了——不能把指针改成另一条死路。"""
    reg, good = _broken_partition(tmp_path, monkeypatch, repair_module)
    entries = repair_module.plan()
    assert entries[0]["repair_to"] == str(good)

    good.unlink()

    fixed = repair_module.apply(entries)
    assert fixed == 0
    assert reg.get_manifest("daily", "2024").path.endswith("2024.gone.parquet"), \
        "没有可指的版本时，指针应保持原样，等重抓"
