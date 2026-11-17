"""重抄交易所涨跌停样本：``tests/kernel/fixtures/limit_bands.csv``。

**选择哪几行是人工判断，这个脚本不改它。** 每一行覆盖一个口径分支：主板 ST 在新规前后、
未股改、主板普通、创业板、科创板、北交所、四舍五入到分。少一行就是少一条防线，而测试仍
会全绿——所以换行等于换尺子，要连同 ``test_limit_pct_rules.py`` 的覆盖断言一起看。

脚本只做一件事：拿每行的 ``ts_code`` + ``trade_date`` 回 ``data/parquet`` 重抄
``pre_close`` / ``up_limit`` / ``down_limit``。日更换了数据、或交易所改了价，跑一次即可。
交易所改了**规则**时不要先跑它——先跑测试：``test_limit_pct_rules.py`` 在有数据的环境里
会红，告诉你代码常量跟不上了；把常量改对之后再重抄样本。

用法::

    PYTHONPATH=. .venv/Scripts/python.exe scripts/record_limit_bands.py
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "kernel" / "fixtures" / "limit_bands.csv"
COLUMNS = ["ts_code", "trade_date", "pre_close", "up_limit", "down_limit",
           "name", "is_st", "expect_pct", "note"]


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """保留注释头，返回 (注释行, 数据行)。

    ``utf-8-sig``：Windows 编辑器会给 CSV 补 BOM，BOM 会让第一行注释变成
    "不是注释"，于是 ``DictReader`` 把注释当表头，报一个和真实原因无关的
    ``KeyError``。读的时候容掉，写的时候不补。
    """
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header = [line for line in lines if line.startswith("#") or not line.strip()]
    body = "\n".join(line for line in lines
                     if line.strip() and not line.startswith("#"))
    return header, list(csv.DictReader(io.StringIO(body)))


def main() -> int:
    header, rows = _read(SAMPLE)
    lim = pd.read_parquet(ROOT / "data" / "parquet" / "stk_limit" / "2026.parquet")
    daily = pd.read_parquet(ROOT / "data" / "parquet" / "daily" / "2026.parquet")
    daily = daily[["ts_code", "trade_date", "pre_close"]]

    changed = 0
    for row in rows:
        match = lim[(lim["ts_code"] == row["ts_code"])
                    & (lim["trade_date"].astype(str) == row["trade_date"])]
        if match.empty:
            print(f"缺这一行：{row['ts_code']} {row['trade_date']}", file=sys.stderr)
            return 2
        price = daily[(daily["ts_code"] == row["ts_code"])
                      & (daily["trade_date"].astype(str) == row["trade_date"])]
        if price.empty:
            print(f"缺昨收：{row['ts_code']} {row['trade_date']}", file=sys.stderr)
            return 2
        fresh = {
            "pre_close": f"{float(price['pre_close'].iloc[0]):.2f}",
            "up_limit": f"{float(match['up_limit'].iloc[0]):.2f}",
            "down_limit": f"{float(match['down_limit'].iloc[0]):.2f}",
        }
        if any(row[key] != value for key, value in fresh.items()):
            changed += 1
            row.update(fresh)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    with SAMPLE.open("w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(header) + "\n")
        fh.write(buffer.getvalue())
    print(f"{len(rows)} 行，{changed} 行价格更新 -> {SAMPLE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
