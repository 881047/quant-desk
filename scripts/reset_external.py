"""把实盘账本清空，从零开始手动记。

    python scripts/reset_external.py            # 只看会删什么
    python scripts/reset_external.py --yes      # 备份后清空

"重置"在这里是**清空数据、保留表结构**：成交、持仓快照、每日数字、已发提醒、
导入记录全部清零，`data/external.sqlite` 本身还在。备份写在同目录下，
`external.before-reset-<时间>.sqlite`，反悔就是把它复制回去。

清算之后的第一件事不是记账，是在面板「同步账户」里把**总资产和可用资金**填一次：
没有这两个数，"今天赚了多少"和"持仓与账户对不对得上"都没有分母。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "external.sqlite"

TABLES = ("trades", "position_snapshot", "snapshot_meta", "day", "alerts", "imports")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清空实盘账本（保留表结构）")
    parser.add_argument("--yes", action="store_true", help="真的执行；不加只打印")
    parser.add_argument("--db", default=str(DB))
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.exists():
        print(f"[skip] 没有 {path}")
        return 0

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    counts = {}
    for table in TABLES:
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        except sqlite3.OperationalError:
            counts[table] = 0
    total = sum(counts.values())

    print(f"账本：{path}")
    for table, count in counts.items():
        print(f"  {table:<18} {count:>6} 行")
    if not total:
        print("[ok] 已经是空的")
        conn.close()
        return 0

    if not args.yes:
        print(f"\n[dry] 会清掉 {total} 行。加 --yes 执行（会先备份）。")
        conn.close()
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.before-reset-{stamp}{path.suffix}")
    conn.close()
    shutil.copy2(path, backup)
    print(f"\n[backup] {backup}")

    conn = sqlite3.connect(path)
    for table in TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"[ok] 已清空 {total} 行；表结构保留")
    print("     下一步：打开面板 → 同步账户 → 填总资产与可用资金")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
