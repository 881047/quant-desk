"""`docs/automations/*.md` 是线上 prompt 的唯一版本记录，必须与线上同步。

2026-11-08 的审查发现 7 份副本停在 2026-11-05 的 v1，而线上早已是 v2：
副本里写「股票 30%」「age ≤300 秒」，线上写「按 sleeve 从 risk.limits 取」
「不写 30%」。谁按副本去改线上，谁就把修好的东西改回去。

本文件只校验副本自身（不读宿主数据库，CI 也能跑）：
- 11 份、id 唯一、文件名前缀等于 rrule 的 HHMM；
- frontmatter 字段齐全，正文 sha256 与 `prompt_sha256` 一致（手改必红）；
- 每份都带开工契约 v2 的五个标记；
- 不含已废弃的字面量。

与线上的逐字比对由 `scripts/freeze_automations.py --check` 负责（需要宿主 DB）。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIES = sorted((ROOT / "docs" / "automations").glob("*.md"))

REQUIRED = ("id", "name", "rrule", "cwd", "captured", "prompt_sha256", "note")
V2_MARKERS = ("开工契约 v2", "ToolSearch", "DeferExecuteTool",
              "market_open_today", "session.now")
OBSOLETE = ("P0 首版是 Shadow", "股票 100 股 / 30%", "quant_anomaly(limit=10)")


def _read(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name}: 缺 frontmatter"
    _, head, body = text.split("---\n", 2)
    meta = {}
    for line in head.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            meta[key.strip()] = value.strip()
    body = body.removeprefix("\n")
    return meta, body


def test_exactly_eleven_copies_with_unique_ids():
    assert len(COPIES) == 11, [p.name for p in COPIES]
    ids = [_read(p)[0]["id"] for p in COPIES]
    assert len(set(ids)) == 11


@pytest.mark.parametrize("path", COPIES, ids=lambda p: p.name)
def test_frontmatter_and_digest_are_intact(path):
    meta, body = _read(path)
    for key in REQUIRED:
        assert meta.get(key), f"{path.name}: 缺 {key}"
    datetime.fromisoformat(meta["captured"])          # 可解析的冻结时刻
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == meta["prompt_sha256"], (
        f"{path.name}: 正文被手改过；重跑 scripts/freeze_automations.py"
    )


@pytest.mark.parametrize("path", COPIES, ids=lambda p: p.name)
def test_filename_prefix_is_the_schedule(path):
    meta, _ = _read(path)
    fields = dict(part.split("=", 1) for part in meta["rrule"].split(";") if "=" in part)
    assert path.name.startswith(f"{int(fields['BYHOUR']):02d}{int(fields['BYMINUTE']):02d}")


@pytest.mark.parametrize("path", COPIES, ids=lambda p: p.name)
def test_every_copy_carries_contract_v2(path):
    _, body = _read(path)
    for marker in V2_MARKERS:
        assert marker in body, f"{path.name}: 缺契约标记 {marker!r}"


@pytest.mark.parametrize("path", COPIES, ids=lambda p: p.name)
def test_no_obsolete_literals(path):
    _, body = _read(path)
    for stale in OBSOLETE:
        assert stale not in body, f"{path.name}: 仍含废弃字面量 {stale!r}"


@pytest.mark.parametrize("path", COPIES, ids=lambda p: p.name)
def test_task_name_has_no_minute_assertion(path):
    meta, _ = _read(path)
    assert re.search(r"\d{4}", meta["name"]) is None, (
        f"{path.name}: 名字里的分钟会被读成时刻断言，见 docs/12 §9.1"
    )
