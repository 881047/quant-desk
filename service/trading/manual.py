"""手动同步：把账户页上的数字和你看到的持仓，写进账本。

读屏（``scripts/sync_today.py``）能全自动跑，但它有一个前提：同花顺停在
「查询资金股票」页，窗口没被遮挡。这个前提在实盘里经常不成立——换一台机器、
客户端更新、字体缩放、DPI 变了，OCR 就什么都读不出来。所以手动这条路必须
存在，而且要**不是**"去命令行敲一条你没记住的命令"。

分两步，因为写账本之前人得先看见自己要写什么：

    plan = prepare(as_of=..., positions=[...], total=..., cash=...)   # 纯函数
    persist(store, plan)                                             # 写库

``prepare`` 只做算术和校验，不碰数据库；``persist`` 只写 ``plan`` 里已经
校验过的行。这样 UI 可以先展示"要写什么、哪里对不上"，再让用户按下保存。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from service import settings

from .external import ExternalStore, normalize_date, normalize_symbol

# Same tolerance as account_check: 2% of the panel's market value, never less
# than 30 元. Two different tolerances for the same question is how a page says
# "一致" while the checker says "不一致".
TOLERANCE_PCT = settings.num("manual.tolerance_pct")
MIN_ABS = settings.num("manual.min_abs")

# A holding whose cost is unknown cannot have a stop: levels() would anchor at
# zero and report every price as "above the stop". Falling back to the live
# price is a guess, so it is recorded as a warning, not as a silent default.
UNKNOWN_COST = 0.0


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        text = str(value).replace(",", "").replace("¥", "").strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return None if number is None else round(number)


def clean_positions(raw: Iterable[dict], prices: dict[str, float] | None = None
                    ) -> tuple[list[dict], list[str], list[str]]:
    """Rows from the form → snapshot rows.

    Returns ``(rows, problems, warnings)``. A row is dropped only when its code
    cannot be resolved; a quantity of zero is a deliberate deletion (the user
    sold it), so it is dropped without a complaint.
    """
    prices = prices or {}
    rows: list[dict] = []
    problems: list[str] = []
    warnings: list[str] = []
    seen: dict[str, int] = {}

    for index, item in enumerate(raw, start=1):
        raw_symbol = str(item.get("symbol") or "").strip()
        if not raw_symbol:
            continue
        symbol = normalize_symbol(raw_symbol)
        if not symbol:
            problems.append(f"第 {index} 行：代码「{raw_symbol}」认不出来")
            continue
        qty = _as_int(item.get("qty"))
        if qty is None:
            problems.append(f"{symbol}：数量「{item.get('qty')}」不是数字")
            continue
        if qty <= 0:
            continue
        if symbol in seen:
            problems.append(f"{symbol}：重复出现（第 {seen[symbol]} 行和第 {index} 行）")
            continue
        seen[symbol] = index

        cost = _as_float(item.get("cost"))
        last = _as_float(item.get("last"))
        if last is None:
            last = prices.get(symbol)
        if cost is None or cost <= UNKNOWN_COST:
            fallback = last if last and last > 0 else None
            if fallback is None:
                problems.append(f"{symbol}：没有成本价，也没有现价，止损没法算")
                continue
            warnings.append(f"{symbol}：成本价留空，按现价 {fallback:.3f} 记（止损会锚在这里）")
            cost = fallback
        rows.append({
            "symbol": symbol,
            "qty": qty,
            "cost": round(float(cost), 4),
            "last": round(float(last), 3) if last else None,
            "name": str(item.get("name") or "").strip(),
        })
    return rows, problems, warnings


def prepare(*, as_of: str | None = None, positions: Iterable[dict],
            total: Any = None, cash: Any = None, market_value: Any = None,
            prices: dict[str, float] | None = None) -> dict:
    """Everything the save button will write, plus what does not add up.

    ``ok`` means "safe to write"; ``warnings`` are shown but do not block. The
    only thing that blocks is a market value that disagrees with the holdings by
    more than the account checker's tolerance — that is the signature of a
    missed decimal point, and writing it would poison every stop downstream.
    """
    prices = prices or {}
    rows, problems, warnings = clean_positions(positions, prices)
    day = normalize_date(as_of) if as_of else normalize_date(None)

    # Market value: the panel's number wins when the user typed it, because the
    # broker knows; otherwise it is quantity × last, with cost as the last resort.
    computed = 0.0
    for row in rows:
        price = row.get("last") or row.get("cost") or 0.0
        computed += price * row["qty"]
    computed = round(computed, 2)

    claimed = _as_float(market_value)
    effective = round(claimed, 2) if claimed is not None else computed
    diff = round(effective - computed, 2)
    tolerance = max(MIN_ABS, abs(effective) * TOLERANCE_PCT) if effective else MIN_ABS
    mismatch = abs(diff) > tolerance and bool(rows) and computed > 0

    equity = _as_float(total)
    cash_value = _as_float(cash)
    if equity is not None and cash_value is not None and claimed is not None:
        gap = round(equity - cash_value - claimed, 2)
        # 总资产 = 可用 + 市值 + 冻结. A couple of yuan of frozen cash is normal
        # (fee pre-authorisation, 申购冻结); complaining about it every save
        # trains the reader to ignore the box.
        limit = max(10.0, abs(equity) * 0.002)
        if abs(gap) > limit:
            warnings.append(
                f"总资产 {equity:,.2f} 与「可用 {cash_value:,.2f} + 市值 {claimed:,.2f}」"
                f"差 {gap:+,.2f}，可能还有冻结资金")

    if mismatch:
        problems.append(
            f"持仓按现价算是 {computed:,.2f}，面板市值是 {effective:,.2f}"
            f"（差 {diff:+,.2f}）——先对一下小数点和股数")

    return {
        "ok": not problems,
        "as_of": day,
        "rows": rows,
        "count": len(rows),
        "computed_market_value": computed,
        "market_value": effective,
        "total": round(equity, 2) if equity is not None else None,
        "cash": round(cash_value, 2) if cash_value is not None else None,
        "mismatch": mismatch,
        "diff": diff,
        "tolerance": round(tolerance, 2),
        "problems": problems,
        "warnings": warnings,
    }


def persist(store: ExternalStore, plan: dict, *, source: str = "manual:panel",
            dry_run: bool = False) -> dict:
    """Write a plan from :func:`prepare`. Refuses a plan with problems."""
    if not plan.get("ok") or plan.get("problems"):
        return {"ok": False, "saved": 0,
                "message": "；".join(plan.get("problems") or ["没有可写的计划"])}
    day = plan["as_of"]
    if dry_run:
        return {"ok": True, "saved": 0, "dry_run": True,
                "message": f"[dry] 将写入 {plan['count']} 只"}

    with store.transaction() as conn:
        count = store.replace_snapshot(day, plan["rows"], source=source, conn=conn)
        if plan.get("total") is not None or plan.get("cash") is not None:
            store.save_day(day, cash=float(plan.get("cash") or 0.0),
                           market_value=float(plan.get("market_value") or 0.0),
                           equity=float(plan.get("total") or 0.0), source=source, conn=conn)
        store.log_import(file_name=f"manual-{day}", file_sha="", profile="positions",
                         rows=len(plan["rows"]), inserted=count, duplicates=0,
                         status="ok", note=source, conn=conn)
    return {"ok": True, "saved": count,
            "message": f"已写入 {day} 的持仓快照：{count} 只（{source}）"}


def current_holdings(store: ExternalStore, *, conn=None) -> dict[str, dict]:
    """What the user says they hold, or — before the first snapshot — the ledger.

    The snapshot is the manual book: after "I'll type my own trades" there is no
    reason to keep reading the broker's grid, and every reason not to, since a
    stale grid silently rewrites positions the user has already booked. Preserve
    each untouched row's last market value for the account-total calculation.
    """
    snapshot = store.snapshot(conn=conn)
    return {symbol: {"qty": int(held.qty), "cost": float(held.avg_price),
                     "name": "",
                     "market_value": float((snapshot.get(symbol) or {}).get("market_value") or 0)}
            for symbol, held in store.holding_book(conn=conn).items()}


def apply_trade(store: ExternalStore, row: dict, *, source: str = "manual:entry",
                stocks: dict[str, str] | None = None,
                instrument=None) -> dict:
    """Book one fill: fees, position, and the trade row — in that order of trust.

    Typing "bought 200 at 8.12" should be enough. The fee is computed here
    rather than asked for, because the fee is what makes the cost basis (and
    therefore the stop) correct, and nobody remembers whether today's rate is
    万 2.5 or 万 3.

    The position is updated from the *current* snapshot, not by replaying the
    whole ledger: the ledger is missing most of this account's history, and a
    replay would answer "you own nothing" to a user who owns 200 shares.
    """
    from . import fees as fee_mod
    from .external import normalize_date, normalize_side, normalize_symbol, trade_key

    symbol = normalize_symbol(row.get("symbol"))
    if not symbol:
        return {"ok": False, "message": f"代码「{row.get('symbol')}」认不出来"}
    side = normalize_side(str(row.get("side") or "BUY"))
    if not side:
        return {"ok": False, "message": "方向只能是买入或卖出"}
    qty = _as_int(row.get("qty"))
    price = _as_float(row.get("price"))
    if not qty or qty <= 0:
        return {"ok": False, "message": "数量要大于 0"}
    if not price or price <= 0:
        return {"ok": False, "message": "价格要大于 0"}

    # `instrument` is the difference between right and merely plausible: an ETF
    # or a convertible pays neither stamp duty nor transfer fee, and without it
    # every such fill is charged 5bps it does not owe. The caller resolves it —
    # this module must work with the ledger unavailable.
    computed = fee_mod.compute(side, price, qty, instrument=instrument)
    override = _as_float(row.get("fee"))
    fee_total = round(override, 2) if override is not None else computed["total"]
    day = normalize_date(row.get("trade_date")) or normalize_date(None)

    record = {
        "trade_date": day,
        "trade_time": str(row.get("trade_time") or ""),
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": round(float(price), 4),
        "amount": round(price * qty, 2),
        "fee": fee_total,
        "broker_id": str(row.get("broker_id") or ""),
    }
    record["trade_key"] = trade_key(record)

    # BEGIN IMMEDIATE serialises writers before either the duplicate check or the
    # holding read. All four writes use this connection; a failure in any one
    # rolls back the fill, snapshot, cash and import log together.
    with store.transaction() as conn:
        exists = conn.execute("SELECT 1 FROM trades WHERE trade_key = ?",
                              (record["trade_key"],)).fetchone()
        if exists:
            return {"ok": True, "duplicate": True, "inserted": 0,
                    "fees": computed, "fee_total": fee_total, "warnings": [],
                    "message": "这笔成交已经记录；持仓、现金和日志均未重复更新"}

        holdings = current_holdings(store, conn=conn)
        held = holdings.get(symbol) or {"qty": 0, "cost": 0.0,
                                       "name": "", "market_value": 0.0}
        qty_now = int(held["qty"])
        cost_now = float(held["cost"])

        if side == "BUY":
            new_qty = qty_now + qty
            new_cost = ((cost_now * qty_now) + price * qty + fee_total) / new_qty
            realized = 0.0
        else:
            if qty > qty_now:
                return {"ok": False,
                        "message": (f"持仓只有 {qty_now} 股，卖不了 {qty} 股。"
                                    "如果数量没错，先把持仓补到当前值，再记这笔卖出。")}
            new_qty = qty_now - qty
            new_cost = cost_now    # 卖出不改剩余仓位成本（A股摊薄法的另一半在账本里）
            realized = round((price - cost_now) * qty - fee_total, 2)

        rows = []
        for other, item in holdings.items():
            if other == symbol:
                continue
            rows.append({"symbol": other, "qty": int(item["qty"]),
                         "cost": float(item["cost"]),
                         "market_value": float(item.get("market_value") or 0),
                         "name": str(item.get("name") or "")})
        if new_qty > 0:
            rows.append({"symbol": symbol, "qty": new_qty, "cost": round(new_cost, 4),
                         "market_value": round(price * new_qty, 2),
                         "name": (stocks or {}).get(symbol, str(held.get("name") or ""))})

        inserted = store.add_trades([record], source=source, conn=conn)
        if not inserted["inserted"]:
            # The uniqueness constraint remains the final guard even though the
            # IMMEDIATE transaction already excludes a concurrent writer.
            return {"ok": True, "duplicate": True, "inserted": 0,
                    "fees": computed, "fee_total": fee_total, "warnings": [],
                    "message": "这笔成交已经记录；持仓、现金和日志均未重复更新"}
        store.replace_snapshot(day, rows, source=source, conn=conn)

        # Cash follows the fill when the day already has numbers. Only then: inventing
        # a starting balance from nothing would report an equity the user never had.
        cash_after = None
        today_row = next((item for item in store.days(1, conn=conn)
                          if str(item.get("as_of")) == day), None)
        if today_row:
            delta = (-1 if side == "BUY" else 1) * (price * qty) - fee_total
            cash_after = round(float(today_row.get("cash") or 0) + delta, 2)
            value = sum(float(item["market_value"]) for item in rows)
            store.save_day(day, cash=cash_after, market_value=round(value, 2),
                           equity=round(cash_after + value, 2), source=source, conn=conn)

        store.log_import(file_name=f"{source}-{day}", file_sha="", profile="trades",
                         rows=1, inserted=1, duplicates=0, status="ok",
                         note=source, conn=conn)

    action = "买入" if side == "BUY" else "卖出"
    parts = [f"{symbol} {action} {qty} 股 @ {price:.3f}",
             f"费用 {fee_total:.2f}（{fee_mod.describe(computed)}）"]
    if side == "BUY":
        parts.append(f"成本变为 {new_cost:.3f}")
    else:
        parts.append(f"已实现 {realized:+,.2f}" if realized else "已实现 0.00")
        parts.append(f"剩 {new_qty} 股" if new_qty else "已清仓")
    if cash_after is not None:
        parts.append(f"可用 {cash_after:,.2f}")
    return {"ok": True, "duplicate": False, "inserted": 1,
            "fees": computed, "fee_total": fee_total, "realized": realized,
            "holding": {"symbol": symbol, "qty": new_qty, "cost": round(new_cost, 4)},
            "rows": rows, "warnings": [], "cash": cash_after,
            "message": "；".join(parts)}
