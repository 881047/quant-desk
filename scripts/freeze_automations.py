#!/usr/bin/env python
"""把线上 automation 的 prompt 冻结成仓库副本。

为什么需要：宿主不保存版本历史，仓库里的 `docs/automations/*.md` 是唯一可审计的
记录。2026-11-08 的审查发现 7 份副本停在 2026-11-05 的 v1，而线上早已是 v2——
任何人按副本去"修"线上，都会把已修好的东西改回去。

所以副本不再手写，由本脚本从宿主数据库实读生成：

    python scripts/freeze_automations.py            # 重写 11 份
    python scripts/freeze_automations.py --check    # 只校验，不写盘

`--check` 做三件事：
1. 文件必须是 11 份，且 id 集合与线上一致（线上少了/多了都会红）；
2. 每份正文的 sha256 必须等于 frontmatter 里的 `prompt_sha256`（手改正文会红）；
3. 正文必须与线上 prompt 逐字相同（改了 automation 却没重冻会红）。

数据库默认 `%USERPROFILE%/.workbuddy/workbuddy.db`，可用 `--db` 覆盖。
数据库不存在时只做第 1、2 项——CI 或换台机器仍能校验副本自身完整性。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "automations"

#: 11 个任务的时间槽 -> 文件名。名字里的分钟不是时刻断言，只是文件名。
SLOTS = {
    (9, 5): "0905-persona-calibration.md",
    (9, 24): "0924-short-alpha-entry.md",
    (9, 26): "0926-open-decision.md",
    (9, 35): "0935-samples.md",
    (13, 35): "1335-noon-check.md",
    (14, 28): "1428-short-alpha-exit.md",
    (14, 30): "1430-close-entry.md",   # 任务 2026-11 已从 14:35 前移到 14:30
    (15, 25): "1525-alert-handoff.md",
    (18, 50): "1850-market-review.md",
    (19, 10): "1910-short-alpha-judgment.md",
    (19, 30): "1930-reconcile.md",
}

NOTE = "宿主不保存版本历史；本文件由 scripts/freeze_automations.py 实读生成，不要手改。改 automation 后重跑该脚本。"


def default_db() -> Path:
    return Path(os.environ.get("USERPROFILE", Path.home())) / ".workbuddy" / "workbuddy.db"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalise(prompt: str) -> str:
    """文件正文的唯一形态：去掉首尾空白，结尾一个换行。哈希也按这个形态算。"""
    return prompt.strip() + "\n"


def parse_rrule(rrule: str) -> tuple[int, int]:
    fields = dict(part.split("=", 1) for part in rrule.split(";") if "=" in part)
    return int(fields["BYHOUR"]), int(fields["BYMINUTE"])


def load_live(db: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select id, name, prompt, rrule, cwds from automations "
            "where deleted_at is null and status = 'ACTIVE' "
            "and name like 'quant-desk ·%' order by name"
        ).fetchall()
    finally:
        con.close()
    out = []
    for ident, name, prompt, rrule, cwds in rows:
        hour, minute = parse_rrule(rrule)
        slot = SLOTS.get((hour, minute))
        if slot is None:
            raise SystemExit(f"未登记的时间槽 {hour:02d}:{minute:02d}（{name}）；先补 SLOTS")
        out.append({"id": ident, "name": name, "prompt": prompt, "rrule": rrule,
                    "cwd": json.loads(cwds)[0] if cwds else "D:/quant-desk",
                    "file": slot})
    return out


def render(item: dict, captured: str) -> str:
    body = normalise(item["prompt"])
    head = [
        "---",
        f"id: {item['id']}",
        f"name: {item['name']}",
        f"rrule: {item['rrule']}",
        f"cwd: {item['cwd']}",
        f"captured: {captured}",
        f"prompt_sha256: {digest(body)}",
        f"note: {NOTE}",
        "---",
    ]
    return "\n".join(head) + "\n\n" + body


def read_copy(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise SystemExit(f"{path.name}: 缺 frontmatter")
    _, head, body = text.split("---\n", 2)
    # frontmatter 与正文之间有一个空行；它属于分隔，不属于 prompt。
    body = body.removeprefix("\n")
    meta = {}
    for line in head.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            meta[key.strip()] = value.strip()
    return meta, body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db())
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    live: list[dict] = []
    if args.db.exists():
        live = load_live(args.db)
    elif args.check:
        print(f"[warn] {args.db} 不存在，只校验副本自身", file=sys.stderr)
    else:
        raise SystemExit(f"找不到宿主数据库 {args.db}；用 --db 指定")

    if live and len(live) != len(SLOTS):
        raise SystemExit(f"线上有 {len(live)} 个 quant-desk 任务，SLOTS 登记了 {len(SLOTS)} 个")

    if not args.check:
        if not live:
            raise SystemExit("没有可冻结的数据")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        # 冻结时刻按这一次运行算。写死 2026-11-08 的后果是：11-14 重冻的副本
        # 仍然声称自己是 11-08 抓的，而这正是读它的人唯一想知道的元数据。
        captured = datetime.now().astimezone().isoformat(timespec="seconds")
        for item in live:
            (OUT_DIR / item["file"]).write_text(render(item, captured), encoding="utf-8")
        print(f"已冻结 {len(live)} 份到 {OUT_DIR}")
        return 0

    problems: list[str] = []
    files = sorted(OUT_DIR.glob("*.md"))
    if len(files) != len(SLOTS):
        problems.append(f"文件数 {len(files)} != {len(SLOTS)}")

    by_id = {item["id"]: item for item in live}
    seen: set[str] = set()
    for path in files:
        meta, body = read_copy(path)
        ident = meta.get("id", "")
        seen.add(ident)
        if digest(body) != meta.get("prompt_sha256"):
            problems.append(f"{path.name}: 正文与 prompt_sha256 不符（被手改过？）")
        if live and ident not in by_id:
            problems.append(f"{path.name}: id {ident} 不在线上")
        elif live:
            item = by_id[ident]
            if path.name != item["file"]:
                problems.append(f"{path.name}: 线上对应 {item['file']}")
            if normalise(item["prompt"]) != body:
                problems.append(f"{path.name}: 与线上 prompt 不一致，需重冻")
            if meta.get("rrule") != item["rrule"]:
                problems.append(f"{path.name}: rrule 与线上不一致")

    if live:
        missing = set(by_id) - seen
        for ident in sorted(missing):
            problems.append(f"线上任务 {by_id[ident]['name']} 没有副本")

    if problems:
        print("\n".join(f"[fail] {p}" for p in problems), file=sys.stderr)
        return 1
    print(f"副本一致：{len(files)} 份" + ("（含线上比对）" if live else "（仅自身完整性）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
