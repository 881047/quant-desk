"""Importing the ASGI app must never migrate a real account or data registry."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PAPER_DB", "QUANT_DATA_ROOT", "QUANT_REGISTRY_DB", "QUANT_CACHE_ROOT"):
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_import_app_leaves_real_databases_and_cache_untouched(tmp_path):
    """点在于「import 不会去迁移/写入它被告知的那两个库」。

    早先这条测试直接哈希 `data/` 下的真实库和 250 MB 的 `data/cache`。两个问题：
    正在运行的 uvicorn 会同时写这两个地方（跑全量时偶发一条红），而且拿 250 MB
    的哈希去证明"import 没写缓存"本身就很贵。改成**指向副本**：内容一样，所以
    "有没有被迁移"照样测得到；而缓存指向一个空目录，"有没有建缓存"比"共享缓存
    有没有变"更直接。
    """
    origin = ROOT / "data"
    work = tmp_path / "data"
    work.mkdir()
    pointed = {}
    for name in ("paper.sqlite", "registry.sqlite"):
        source = origin / name
        if not source.exists():
            continue
        shutil.copy2(source, work / name)
        pointed[name] = _digest(work / name)
    assert pointed, "没有可指的库，这条测试就没意义"

    empty_cache = tmp_path / "cache"
    empty_cache.mkdir()
    env = _clean_environment()
    env.update({
        "PAPER_DB": str(work / "paper.sqlite"),
        "QUANT_DATA_ROOT": str(work),
        "QUANT_REGISTRY_DB": str(work / "registry.sqlite"),
        "QUANT_CACHE_ROOT": str(empty_cache),
    })
    result = subprocess.run(
        [sys.executable, "-c", "import service.api.app"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for name, before in pointed.items():
        assert _digest(work / name) == before, f"import 改写了 {name}"
    assert list(empty_cache.rglob("*")) == [], "import 不该建缓存"


def test_first_store_access_uses_environment_set_before_import(tmp_path):
    env = _clean_environment()
    paper = tmp_path / "paper.sqlite"
    data = tmp_path / "data"
    registry = data / "registry.sqlite"
    env.update({
        "PAPER_DB": str(paper),
        "QUANT_DATA_ROOT": str(data),
        "QUANT_REGISTRY_DB": str(registry),
        "REALTIME_QUOTES": "0",
    })
    code = """
from pathlib import Path
import os
import service.api.app as app_module
from service.api import data as data_api
from service.trading import get_desk

paper = Path(os.environ['PAPER_DB'])
registry = Path(os.environ['QUANT_REGISTRY_DB'])
assert not paper.exists() and not registry.exists()
assert app_module._usage is None
assert data_api._store is None and data_api._registry is None
assert data_api.get_store().root == Path(os.environ['QUANT_DATA_ROOT'])
assert app_module._get_usage().db_path == paper
assert get_desk().paper.db_path == paper
assert paper.exists() and registry.exists()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
