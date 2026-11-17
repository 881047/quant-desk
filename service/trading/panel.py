"""The 实盘 panel's data, assembled once and shared by every front end.

There are two front ends now — the web page and the desktop window — and the rule
that matters is that they cannot disagree. So the payload is built here, from one
set of sources, and both render it:

    panel       账户级总额（客户端 Static，实时）
    positions   持仓逐只（快照为准，现价来自自家行情源）
    drift       面板市值 vs 账本市值
    reconcile   流水 vs 快照
    last_sync   上一次同步是什么时候、从哪来、几行

The snapshot answers "what do you hold"; the ledger answers "how you got there".
Mixing those up is what put a sold position back on the page this morning.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from threading import Lock
from typing import Any

from .account_check import compare
from .external import ExternalStore, levels, reconcile
from .ths_account import read_account

ROOT = Path(__file__).resolve().parents[2]

# One worker, reused: a per-call executor would leak a thread every refresh when
# the vendor stalls, and the vendor does stall (8s on the first call of the day).
_QUOTE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="panel-quote")
_QUOTE_LOCK = Lock()


def _quotes(desk, symbols: list[str], timeout: float) -> dict:
    """Quotes, or {} when the vendor takes longer than the window can wait.

    The panel is useful without a live price — the snapshot carries the broker's
    market value, which is a real number from minutes ago. A window that freezes
    for eight seconds is not useful at all, so the price is the part that gives
    way.
    """
    if not symbols or not _QUOTE_LOCK.acquire(blocking=False):
        # One stalled vendor must not create an unbounded queue behind the single
        # worker. While a request is busy, the panel degrades immediately.
        return {}
    future = None
    try:
        future = _QUOTE_POOL.submit(desk.quotes, symbols)
        return future.result(timeout=timeout) or {}
    except FutureTimeout:
        future.cancel()  # no-op when already running; the callback still releases
        return {}
    except Exception:  # noqa: BLE001 - a vendor error is not a window error
        return {}
    finally:
        if future is None or future.done():
            _QUOTE_LOCK.release()
        else:
            future.add_done_callback(lambda _done: _QUOTE_LOCK.release())


def account_panel(desk, *, root: Path | None = None,
                  read_client: bool = False, with_quotes: bool = True,
                  quote_timeout: float = 6.0) -> dict[str, Any] | None:
    """Everything the panel shows, or None when the ledger cannot be opened."""
    base = Path(root) if root else ROOT
    try:
        store = ExternalStore(base / "data" / "external.sqlite")
        snap_day = store.snapshot_day()
        snap = store.snapshot(snap_day) if snap_day else {}

        # What you hold: the broker's snapshot when there is one, else the ledger.
        # "There is one" is asked as ``snapshot_day()``, not as ``if snap``: an
        # account that is entirely in cash writes an empty snapshot, and reading
        # that as "never synced" put the ledger's sold-out positions back on the
        # screen — the exact mistake this panel exists to prevent.
        if snap_day:
            holdings = {
                symbol: {"qty": int(row["qty"]), "cost": float(row.get("cost") or 0),
                         "market_value": float(row.get("market_value") or 0)}
                for symbol, row in snap.items() if int(row["qty"]) > 0
            }
            source = "snapshot"
        else:
            holdings = {
                symbol: {"qty": position.qty, "cost": position.cost,
                         "market_value": 0.0}
                for symbol, position in store.holdings().items()
            }
            source = "ledger"

        quotes = _quotes(desk, sorted(holdings), quote_timeout) if with_quotes else {}

        positions = []
        book_value = 0.0
        for symbol, item in sorted(holdings.items()):
            quote = quotes.get(symbol)
            # No quote (client closed, vendor down) must not become "worth the
            # cost": the snapshot carries the broker's own 市值, so the last
            # price can still be recovered from it.
            implied = (item["market_value"] / item["qty"]) if item.get("qty") else 0.0
            prev_close = float(quote.pre_close) if quote else (implied or item["cost"])
            last = float(quote.last) if quote else prev_close
            inst = desk.market.instrument(symbol, snap_day or desk.market.latest_bar_date())
            level = levels(symbol=symbol, qty=item["qty"], cost=item["cost"],
                           prev_close=prev_close, last=last, entry=item["cost"],
                           st=bool(inst.is_st), name=getattr(inst, "name", "") or "")
            book_value += level["market_value"]
            level.update({
                "name": (getattr(quote, "name", "") if quote else ""),
                "quote_source": (str(quote.source) if quote else "none"),
                "quote_time": (str(getattr(quote, "quote_ts", "") or "") if quote else ""),
            })
            positions.append(level)

        # Not reading the client is the default now: the account numbers come
        # from what the user typed. `read_client=True` survives for the one
        # caller that still wants the grid (nothing in the window does).
        panel = read_account() if read_client else {"ok": False, "reason": "未读取客户端"}
        if not panel.get("ok") and snap_day:
            # A hand-entered day is a legitimate source: the user typed the three
            # numbers off the same panel we failed to read. Refusing to use it
            # would leave the window saying "读不到" on a day it was synced.
            day = (store.days(1) or [{}])[0]
            if str(day.get("as_of") or "") == str(snap_day):
                cash = float(day.get("cash") or 0)
                equity = float(day.get("equity") or 0)
                panel = {"ok": True, "total": equity, "cash": cash,
                         "market_value": float(day.get("market_value") or 0),
                         "cash_ratio": round(cash / equity, 6) if equity else 0.0,
                         "source": str(day.get("source") or "manual"),
                         "note": "客户端读不到，用的是上次手动录入的数"}

        drift = compare(panel=panel, book_value=book_value, positions=len(positions),
                        # An empty book has no quotes to be stale: 0 == 0 is the
                        # whole comparison, and reporting "行情不是实时的" for
                        # an account in cash trains the reader to ignore the
                        # verdict that matters when there *are* positions.
                        # Every holding needs a genuinely live quote. A daily
                        # fallback has a source label too, but comparing its old
                        # close with the broker's current market value subtracts
                        # two different times; one fresh symbol must not make the
                        # whole mixed book look fresh either.
                        quotes_fresh=(not positions)
                        or all(symbol in quotes and quotes[symbol].is_realtime
                               for symbol in holdings))
        alert_path = base / "data" / "external" / "ALERT.txt"

        return {
            "positions": positions,
            "panel": panel,
            "snapshot_day": snap_day,
            # The caller asked for the fast view: prices are still on the wire,
            # so "行情不是实时的" would be a verdict on a comparison that has
            # not been attempted yet.
            "quotes_pending": (not with_quotes) and bool(positions),
            "book_value": round(book_value, 2),
            "source": source,
            "drift": drift,
            "reconcile": reconcile(store),
            "last_sync": (store.imports(1) or [{}])[0],
            "alert": (alert_path.read_text(encoding="utf-8").strip()
                      if alert_path.exists() else None),
        }
    except Exception:  # noqa: BLE001 - a broken panel must not take the page down
        return None


def summary_lines(payload: dict[str, Any] | None) -> list[str]:
    """One-line facts for a status bar, worst news first."""
    if not payload:
        return ["实盘账本读不到（data/external.sqlite）"]
    lines: list[str] = []
    panel = payload.get("panel") or {}
    drift = payload.get("drift") or {}
    gap = payload.get("reconcile") or {}

    if panel.get("ok"):
        lines.append(
            f"总资产 {panel['total']:,.2f} · 可用 {panel['cash']:,.2f} · "
            f"市值 {panel['market_value']:,.2f} · 现金 {panel['cash_ratio']:.1%}")
    else:
        lines.append(f"账户面板读不到：{panel.get('reason') or '—'}")

    if drift.get("verdict") == "match":
        lines.append("与账户一致")
    elif drift.get("verdict") in ("book_missing", "book_extra"):
        lines.append("与账户不一致：" + str(drift.get("message") or ""))
    else:
        lines.append(str(drift.get("reason") or "未比对"))

    if gap.get("snapshot_missing"):
        lines.append("还没有持仓快照")
    elif not gap.get("ok"):
        lines.append(f"流水与快照 {len(gap.get('mismatches') or [])} 处不一致")

    last = payload.get("last_sync") or {}
    if last.get("imported_at"):
        lines.append(f"上次同步 {str(last['imported_at'])[:16]}"
                     f"（{last.get('file_name') or '—'}，{last.get('rows') or 0} 行）")
    return lines
