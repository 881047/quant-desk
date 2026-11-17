"""把指向已删除版本的清单指回磁盘上还活着的那一个。

为什么需要它
------------
清单是一句承诺："这个路径上的文件，哈希是��个值"。承诺一旦落空，存储层就
拒绝读——这是对的，**但修的办法不是放松检查，而是把指针指回一个真的存在、
而且内容与承诺一致的文件。**

2026-11-02 17:30 的日更之后，这个台子出现了两行这样的清单：

    limit_up_pool[20261102]     → 路径不存在
    limit_down_pool[20261102]   → 路径不存在

原因是同一个分区的多个写入者（10:30 快照、15:20 收盘、17:30 日更）各自提交
自己的清单，**最后提交的那个说了算**——而不是最新写的那次。一个 10:30 开工、
17:35 才提交的慢写入者把指针拖回了它自己的 10:30 版本，而那个版本已经被
后来的写入者清理掉了。于是每一次读这个分区都抛
"current manifest has no readable parquet/metadata"。

已经修掉根因（``Registry.upsert_manifest`` 不再允许指针倒退，见那里的说明），
本脚本负责修**已经坏掉**的指针：版本目录里每个 parquet 旁边有一份
``.meta.json``，里面记着这一版的 checksum / schema_hash / rows /
snapshot_id。拿清单里的 checksum 去比对，找到仍然存在、且内容对得上的那一版，
把指针指过去。内容一个字节都不改——只改指针。

    python scripts/repair_manifest.py             # 只报告
    python scripts/repair_manifest.py --apply     # 真的改指针
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

REGISTRY = ROOT / "data" / "registry.sqlite"
DATA_ROOT = ROOT / "data" / "parquet"


def _broken_rows() -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"{REGISTRY.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT dataset, partition, path, snapshot_id, checksum, schema_hash, rows "
            "FROM dataset_manifest ORDER BY dataset, partition").fetchall()
    finally:
        conn.close()
    return [r for r in rows if not Path(r["path"]).exists()]


def _candidates(dataset: str, partition: str) -> list[tuple[Path, dict]]:
    directory = DATA_ROOT / dataset / ".versions" / partition
    out: list[tuple[Path, dict]] = []
    if not directory.exists():
        return out
    for meta_path in sorted(directory.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        parquet = meta_path.with_suffix("").with_suffix(".parquet")
        if parquet.exists():
            out.append((parquet, meta))
    return out


def plan() -> list[dict]:
    """One entry per broken manifest row, with the version it can point at."""
    out: list[dict] = []
    for row in _broken_rows():
        candidates = _candidates(row["dataset"], row["partition"])
        exact = None
        for parquet, meta in candidates:
            if (meta.get("checksum") == row["checksum"]
                    and meta.get("schema_hash") == row["schema_hash"]
                    and meta.get("rows") == row["rows"]):
                exact = (parquet, meta)
                break
        # No exact match means the promise named a version whose content nobody
        # else has. The newest version still on disk is the one the newest
        # writer published, so the repair re-points at it *and* re-states the
        # promise from that version's own metadata. Content is not touched.
        pick = exact
        mode = "same-content"
        if pick is None and candidates:
            newest = max(candidates, key=lambda item: item[0].stat().st_mtime)
            pick = newest
            mode = "newest-on-disk"
        out.append({
            "dataset": row["dataset"],
            "partition": row["partition"],
            "missing_path": row["path"],
            "snapshot_id": row["snapshot_id"],
            "repair_to": str(pick[0]) if pick else None,
            "repair_snapshot": pick[1].get("snapshot_id") if pick else None,
            "repair_checksum": pick[1].get("checksum") if pick else None,
            "repair_schema_hash": pick[1].get("schema_hash") if pick else None,
            "repair_rows": pick[1].get("rows") if pick else None,
            "repair_as_of": pick[1].get("as_of") if pick else None,
            "repair_cover_start": pick[1].get("cover_start") if pick else None,
            "repair_cover_end": pick[1].get("cover_end") if pick else None,
            "mode": mode if pick else "none",
            "repairable": pick is not None,
        })
    return out


#: Same wait as ``PaperStore``. The 17:30 job holds the write lock while it
#: merges a session; a repair that gave up after a second would report a lock
#: conflict as "cannot fix this pointer".
BUSY_TIMEOUT = 30.0


def apply(entries: list[dict]) -> int:
    """Re-point the broken rows, deciding *inside* the write transaction.

    ``plan()`` reads on a different connection, so everything it learned can be
    stale by the time this runs: the daily job may have published a new version,
    or another operator may have repaired the row already. The old version of
    this function issued the UPDATE unconditionally, which meant the repair tool
    could drag a pointer back to a version that had been superseded — the exact
    failure ``Registry.upsert_manifest`` was hardened against, reintroduced
    through the side door. So each row is re-read here, under the write lock,
    and skipped unless it still looks exactly as broken as it did in the plan.
    """
    fixed = 0
    conn = sqlite3.connect(REGISTRY, timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT * 1000)}")
        # Write lock before the first read: every decision below is made from a
        # read, and another process writes the same rows.
        conn.execute("BEGIN IMMEDIATE")
        for entry in entries:
            if not entry["repairable"]:
                continue
            current = conn.execute(
                "SELECT path FROM dataset_manifest WHERE dataset=? AND partition=?",
                (entry["dataset"], entry["partition"])).fetchone()
            reason = None
            if current is None:
                reason = "清单行已不存在"
            elif current["path"] != entry["missing_path"]:
                reason = f"清单已改指 {current['path']}"
            elif not Path(entry["repair_to"]).exists():
                reason = "计划指向的那一版在这期间被清掉了"
            if reason:
                print(f"[repair_manifest] 跳过 {entry['dataset']}[{entry['partition']}]："
                      f"{reason}")
                continue
            if entry["mode"] == "same-content":
                conn.execute(
                    "UPDATE dataset_manifest SET path=? WHERE dataset=? AND partition=?",
                    (entry["repair_to"], entry["dataset"], entry["partition"]))
            else:
                # Re-state the promise from the version that is actually there.
                conn.execute(
                    "UPDATE dataset_manifest SET path=?, snapshot_id=?, checksum=?, "
                    "schema_hash=?, rows=?, as_of=?, cover_start=?, cover_end=? "
                    "WHERE dataset=? AND partition=?",
                    (entry["repair_to"], entry["repair_snapshot"], entry["repair_checksum"],
                     entry["repair_schema_hash"], entry["repair_rows"], entry["repair_as_of"],
                     entry["repair_cover_start"], entry["repair_cover_end"],
                     entry["dataset"], entry["partition"]))
            fixed += 1
        conn.commit()
    finally:
        conn.close()
    return fixed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the repaired pointers (default: report only)")
    args = parser.parse_args(argv)

    entries = plan()
    if not entries:
        print("[repair_manifest] 所有清单都指向存在的文件")
        return 0

    for entry in entries:
        state = {"same-content": "可修（内容一致）",
                 "newest-on-disk": "可修（改指最新版本并重述承诺）",
                 "none": "无匹配版本"}[entry["mode"]]
        print(f"[repair_manifest] {entry['dataset']}[{entry['partition']}] {state}")
        print(f"    现在指向（已不存在）: {entry['missing_path']}")
        if entry["repairable"]:
            print(f"    将指向            : {entry['repair_to']}")
        else:
            print("    磁盘上没有还活着的版本——需要重跑一次这个数据集的抓取")

    if not args.apply:
        print("\n[repair_manifest] 只报告。加 --apply 才改指针。")
        return 0

    fixed = apply(entries)
    skipped = sum(1 for entry in entries if entry["repairable"]) - fixed
    print(f"\n[repair_manifest] 已修好 {fixed} 条指针"
          + (f"，跳过 {skipped} 条（见上面的原因）" if skipped else ""))
    # Non-zero when anything was left broken, so a scheduled run cannot report
    # success while a partition is still unreadable.
    return 0 if fixed == len(entries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
