#!/usr/bin/env python
"""Push the eleven reviewed prompt bodies to WorkBuddy without changing scheduling.

Default mode is a read-only diff. ``--write`` makes a SQLite online backup, updates
only automations.prompt in one transaction, and verifies every row after commit.
Run scripts/freeze_automations.py afterwards to refresh frontmatter hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import freeze_automations as freeze

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TOOLS = {"quant_query", "quant_order", "quant_workflow", "quant_alert"}
PRESERVED = ("name", "rrule", "cwds", "model_id", "model_is_thinking", "status")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def copies() -> list[dict]:
    rows = []
    for path in sorted(freeze.OUT_DIR.glob("*.md")):
        meta, body = freeze.read_copy(path)
        body = freeze.normalise(body)
        tools = set(re.findall(r"\bquant_[a-z_]+\b", body))
        unknown = tools - PUBLIC_TOOLS
        if unknown:
            raise SystemExit(f"{path.name}: 仍有非公开工具引用 {sorted(unknown)}")
        for marker in ("开工契约 v2", "ToolSearch", "DeferExecuteTool", "market_open_today", "session.now"):
            if marker not in body:
                raise SystemExit(f"{path.name}: 缺开工契约标记 {marker}")
        rows.append({"file": path.name, "meta": meta, "prompt": body, "sha256": sha(body)})
    if len(rows) != len(freeze.SLOTS):
        raise SystemExit(f"副本数 {len(rows)} != {len(freeze.SLOTS)}")
    if len({row["meta"].get("id") for row in rows}) != len(rows):
        raise SystemExit("副本 id 不唯一或缺失")
    return rows


def live_rows(db: Path) -> dict[str, dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        raw = con.execute(
            "select id, name, prompt, rrule, cwds, model_id, model_is_thinking, status "
            "from automations where deleted_at is null and status = 'ACTIVE' "
            "and name like 'quant-desk ·%'"
        ).fetchall()
    finally:
        con.close()
    rows = {
        ident: {
            "id": ident, "name": name, "prompt": freeze.normalise(prompt), "rrule": rrule,
            "cwds": cwds, "model_id": model_id, "model_is_thinking": thinking, "status": status,
        }
        for ident, name, prompt, rrule, cwds, model_id, thinking, status in raw
    }
    if len(rows) != len(freeze.SLOTS):
        raise SystemExit(f"线上任务数 {len(rows)} != {len(freeze.SLOTS)}")
    return rows


def plan(db: Path) -> list[dict]:
    live = live_rows(db)
    docs = copies()
    if {row["meta"]["id"] for row in docs} != set(live):
        raise SystemExit("线上与副本的 automation id 集合不一致，拒绝写入")
    planned = []
    for doc in docs:
        meta = doc["meta"]
        ident = meta["id"]
        row = live[ident]
        hour, minute = freeze.parse_rrule(row["rrule"])
        expected_file = freeze.SLOTS.get((hour, minute))
        cwd = json.loads(row["cwds"])[0] if row["cwds"] else "D:/quant-desk"
        checks = {
            "file": doc["file"] == expected_file,
            "name": meta.get("name") == row["name"],
            "rrule": meta.get("rrule") == row["rrule"],
            "cwd": meta.get("cwd") == cwd,
        }
        if not all(checks.values()):
            raise SystemExit(f"{doc['file']}: 元数据与线上不一致 {checks}，拒绝写入")
        planned.append({
            **doc,
            "id": ident,
            "before": {key: row[key] for key in PRESERVED},
            "before_prompt": row["prompt"],
            "before_sha256": sha(row["prompt"]),
            "changed": row["prompt"] != doc["prompt"],
        })
    return planned


def backup(db: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db.with_name(f"{db.name}.bak-quant-facade-{stamp}")
    source = sqlite3.connect(str(db))
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=freeze.default_db())
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.db.exists():
        raise SystemExit(f"找不到宿主数据库 {args.db}")
    planned = plan(args.db)
    result = {
        "write": args.write,
        "task_count": len(planned),
        "changed_count": sum(row["changed"] for row in planned),
        "preserved_fields": list(PRESERVED),
        "tasks": [{
            "id": row["id"], "file": row["file"], "model_id": row["before"]["model_id"],
            "cwds": json.loads(row["before"]["cwds"]), "rrule": row["before"]["rrule"],
            "before_sha256": row["before_sha256"], "after_sha256": row["sha256"],
            "changed": row["changed"],
        } for row in planned],
    }
    if args.write:
        result["backup"] = str(backup(args.db))
        con = sqlite3.connect(str(args.db), timeout=30)
        try:
            con.execute("begin immediate")
            for row in planned:
                current = con.execute(
                    "select prompt, name, rrule, cwds, model_id, model_is_thinking, status "
                    "from automations where id = ? and deleted_at is null", (row["id"],)
                ).fetchone()
                if current is None:
                    raise RuntimeError(f"任务在写入前消失：{row['file']}")
                prompt, *preserved = current
                if freeze.normalise(prompt) != row["before_prompt"] or list(preserved) != [row["before"][key] for key in PRESERVED]:
                    raise RuntimeError(f"任务在预检后被修改：{row['file']}")
                con.execute("update automations set prompt = ? where id = ?", (row["prompt"].rstrip(), row["id"]))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        after = live_rows(args.db)
        for row in planned:
            current = after[row["id"]]
            if current["prompt"] != row["prompt"]:
                raise RuntimeError(f"写入后 prompt 不一致：{row['file']}")
            if any(current[key] != row["before"][key] for key in PRESERVED):
                raise RuntimeError(f"写入后保留字段发生变化：{row['file']}")
        result["verified"] = True
        result["synced_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if args.write or not result["changed_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
