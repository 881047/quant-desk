"""今天的账户同步，一条命令。

读完账户面板（总资产/可用/市值），读持仓表（PrintWindow + OCR），把两者对上，
写一份快照，然后告诉你账本还差什么。

    python scripts/sync_today.py            # 读、校验、写快照、报告
    python scripts/sync_today.py --dry-run  # 只看，不写

"手动同步"在这里只指一件事：**你交易之后跑一次**。读数本身是全自动的——不需要点客户端、
不需要复制、不需要导出，窗口被别的程序挡住也行。

为什么仍要你按下这个按钮：这套系统的账本只接受两种东西——券商的成交明细，或者一份经过
校验的持仓快照。让它自己在后台随便改写持仓，等于把"账本"变成"缓存"，那就没有对账可言了。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service.kernel.clock import SHANGHAI
from service.trading.account_check import compare
from service.trading.external import ExternalStore, reconcile
from service.trading.ths_account import read_account


def read_positions() -> tuple[list[dict], list[str], str]:
    """Holdings from pixels. Returns (positions, problems, scale note)."""
    from scripts import read_ths_screen as screen

    _, grid = screen._windows()
    if not grid:
        return [], ["没找到持仓表格：客户端没开，或没停在「查询资金股票」页面"], ""

    tokens = screen.ocr(screen.capture(grid))
    rows, row_problems = screen.build_rows(tokens)
    panel = read_account()
    panel_value = float(panel["market_value"]) if panel.get("ok") else None
    positions, number_problems, convention = screen.interpret(rows, panel_value=panel_value)
    screen.fix_names(positions)
    return positions, row_problems + number_problems, convention


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读取账户并写一份持仓快照")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--as-of", dest="as_of", default="")
    args = parser.parse_args(argv)

    day = args.as_of or datetime.now(SHANGHAI).strftime("%Y%m%d")
    store = ExternalStore(ROOT / "data" / "external.sqlite")

    panel = read_account()
    print("账户面板：" + (
        f"总资产 {panel['total']:,.2f} · 可用 {panel['cash']:,.2f} · "
        f"市值 {panel['market_value']:,.2f} · 现金占比 {panel['cash_ratio']:.1%}"
        if panel.get("ok") else f"读不到（{panel.get('reason')}）"))

    positions, problems, convention = read_positions()
    for problem in problems[:6]:
        print(f"   [skip] {problem}")
    if convention:
        print(f"持仓表：{convention}")

    if not positions:
        print("[stop] 一只都没读出来，不写快照。")
        return 1

    total = sum(p["market_value"] for p in positions)
    print(f"读出的持仓（{len(positions)} 只，市值 {total:,.2f}）：")
    for p in positions:
        print(f"   {p['symbol']} {p['name']:<6} {p['qty']:>6} 股  成本 {p['cost']:.3f}  "
              f"市值 {p['market_value']:>10,.2f}  盈亏 {p['pnl']:+,.2f}")

    if panel.get("ok"):
        drift = compare(panel=panel, book_value=total, positions=len(positions),
                        quotes_fresh=True)
        print(f"对面板：{drift.get('message', drift.get('reason'))}")
        if drift.get("verdict") in ("book_missing", "book_extra"):
            print("[stop] 读出的持仓与账户面板不一致，说明有列没读对；不写快照")
            return 1

    if problems and not args.dry_run:
        print("[stop] OCR 有未通过校验的行；部分持仓不能替换完整快照，不写库。")
        return 1

    if not args.dry_run:
        count = store.replace_snapshot(day, positions, source="screen:ths-ocr")
        store.log_import(file_name=f"sync-{day}", file_sha="", profile="positions",
                         rows=len(positions), inserted=count, duplicates=0, status="ok")
        print(f"[ok] 快照 {day} 已写入：{count} 只（source=screen:ths-ocr）")
    else:
        print("[dry] 未写快照")

    gap = reconcile(store, as_of=day)
    if gap.get("snapshot_missing"):
        # "no snapshot" is not "agrees" — saying 一致 here is the exact class of
        # false reassurance this project keeps having to remove.
        print(f"账本对账：还没写过快照（{gap.get('note')}）")
        return 0
    if gap.get("ok"):
        print("账本对账：一致")
        return 0

    rows = gap.get("mismatches") or []
    print(f"账本对账：{len(rows)} 处不一致——")
    for row in rows[:6]:
        print(f"   {row['symbol']} 账本 {row['ledger_qty']} 股 vs 账户 {row['broker_qty']} 股"
              f"（差 {row['delta']:+d}）")
    print("   这是「你有成交没记进账本」。补一次成交明细即可：导出到 "
          "data/external/drop，或切到成交页复制后 paste。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
