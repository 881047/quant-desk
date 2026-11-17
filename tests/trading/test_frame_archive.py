"""P1-1：裁剪前冷存。守的是「原始字节不可丢」这条 PIT 回放前提。"""

from __future__ import annotations

import pytest

from service.adapters.catalog import SPECS
from service.trading import snapshot

_NAMES = ["093000_000001.parquet", "093100_000002.parquet",
          "093200_000003.parquet"]


def _archive(root, names=_NAMES):
    path = root / "rt_snapshots" / "20261106"
    path.mkdir(parents=True, exist_ok=True)
    for name in names:
        (path / name).write_bytes(f"frame:{name}".encode())
    return path


def test_prune_copies_the_original_bytes_before_deleting(tmp_path):
    archive = _archive(tmp_path)
    cold = tmp_path / "rt_archive" / "20261106"
    out = snapshot.prune_frames(archive, cold, keep=1)
    assert out["copied"] == 2 and out["removed"] == 2 and out["kept"] == 1
    assert [p.name for p in archive.glob("*.parquet")] == [_NAMES[-1]]
    assert sorted(p.name for p in cold.glob("*.parquet")) == _NAMES[:2]
    assert (cold / _NAMES[0]).read_bytes() == f"frame:{_NAMES[0]}".encode()


def test_existing_cold_copy_is_not_overwritten_and_the_hot_file_is_removed(tmp_path):
    """上一轮复制过、崩在 unlink 之前：跳过复制直接删，幂等。"""
    archive = _archive(tmp_path, names=[_NAMES[0]])
    cold = tmp_path / "rt_archive" / "20261106"
    cold.mkdir(parents=True)
    (cold / _NAMES[0]).write_bytes(b"previous copy")
    out = snapshot.prune_frames(archive, cold, keep=0)
    assert out["copied"] == 0 and out["removed"] == 1
    assert (cold / _NAMES[0]).read_bytes() == b"previous copy"
    assert not (archive / _NAMES[0]).exists()


def test_copy_failure_keeps_the_hot_file_for_next_round(tmp_path, monkeypatch):
    """先删后复制会把这一帧永久丢掉；失败必须保留、下轮重试。"""
    archive = _archive(tmp_path, names=[_NAMES[0]])
    cold = tmp_path / "rt_archive" / "20261106"

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(snapshot.shutil, "copy2", broken)
    with pytest.raises(OSError):
        snapshot.prune_frames(archive, cold, keep=0)
    assert (archive / _NAMES[0]).exists()


def test_cold_archive_is_a_file_directory_not_a_dataset():
    """不登记 SPECS、不进 DAILY_JOB_STEPS，因此没有 owner 契约要同步。"""
    assert "rt_archive" not in SPECS
    assert snapshot.SNAPSHOT_KEEP == 120
