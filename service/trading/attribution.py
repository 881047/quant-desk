"""Per-strategy attribution.

Two questions this answers, neither of which NAV can:

1. Which strategy actually made the money? A book that is +3% because one
   discretionary call worked and a systematic sleeve bled is not "working".
2. Is a strategy's allocation justified? The allocator needs closed-trade stats
   per strategy, and the excess must be measured against the same window as the
   trade — not against a different calendar.

Excess is computed with the arena's window helper so the two numbers cannot
disagree.
"""

from __future__ import annotations

import pandas as pd

from . import strategy as strategy_mod
from .arena import _window_return, market_for_cases


def _market_for(desk, cases):
    """Kept as a named seam: attribution and the arena must share one window.

    An empty book has no holding window to measure, and ``market_for_cases``
    falls back to the full archive when it has no earliest case — twenty years
    of daily bars, about eight seconds, decoded to produce a table with no rows.
    ``arena.scoreboard`` already short-circuits here; this is the same guard.
    """
    if not cases:
        return pd.Series(dtype="float64")
    return market_for_cases(desk.data, cases)


def _tagged_positions_by(desk, field: str, default: str) -> dict[str, float]:
    """Market value per tag, from the most recent filled BUY ticket."""
    latest: dict[str, str] = {}
    for ticket in desk.paper.list_tickets(limit=500, status="filled"):
        if str(ticket.get("side", "")).upper() != "BUY":
            continue
        symbol = str(ticket.get("symbol") or "")
        if symbol and symbol not in latest:
            latest[symbol] = str(ticket.get(field) or default)
    out: dict[str, float] = {}
    for pos in desk.book.summary()["positions"]:
        key = latest.get(pos["symbol"], default)
        out[key] = out.get(key, 0.0) + float(pos["market_value"])
    return out


def position_provenance(desk) -> dict[str, dict]:
    """Latest filled BUY provenance per symbol for position-level ownership checks."""
    latest: dict[str, dict] = {}
    paper = getattr(desk, "paper", None)
    list_tickets = getattr(paper, "list_tickets", None)
    if not callable(list_tickets):
        return latest
    for ticket in list_tickets(limit=500, status="filled"):
        if str(ticket.get("side", "")).upper() != "BUY":
            continue
        symbol = str(ticket.get("symbol") or "")
        if symbol and symbol not in latest:
            latest[symbol] = {
                "strategy": str(ticket.get("strategy") or "llm"),
                "entry_client_request_id": ticket.get("client_request_id"),
                "entry_ticket_id": ticket.get("ticket_id"),
            }
    return latest


def tag_position_summary(desk, summary: dict) -> dict:
    """Add source labels without changing quantities, cash or portfolio semantics."""
    provenance = position_provenance(desk)
    tagged = dict(summary)
    rows = []
    for position in summary.get("positions", []):
        row = dict(position)
        source = provenance.get(str(row.get("symbol") or ""), {})
        row["strategy"] = source.get("strategy") or "llm"
        row["entry_client_request_id"] = source.get("entry_client_request_id")
        row["entry_ticket_id"] = source.get("entry_ticket_id")
        rows.append(row)
    tagged["positions"] = rows
    return tagged


def _tagged_positions(desk) -> dict[str, float]:
    """Market value per strategy, from the most recent filled BUY ticket."""
    return _tagged_positions_by(desk, "strategy", "llm")


def core_retired() -> bool:
    """核心盘这个"袖"有没有被关掉。读 configs/core.json 的 retired，不是 REGISTRY。

    两处开关各管一件事：REGISTRY 说"这是不是一条在运营的策略"（allocator 按它
    分钱），core.json 说"这个袖关没关"（账户规模不够买一手国债 ETF 时关掉）。
    2026-11-01 关掉核心盘时只改了 core.json，REGISTRY 一直写着 live，于是复盘页
    把一条从未成交、已退休的袖显示成「在跑」，而入擂台那边早就把它摘掉了。
    """
    try:
        from .core import UNIVERSE_CFG

        return bool(UNIVERSE_CFG.retired)
    except Exception:  # noqa: BLE001 - 状态标签不该让整张归因表挂掉
        return False


def strategy_status(key: str) -> tuple[str, float | None]:
    """归因表里这条策略该显示的状态与权重上限。"""
    meta = strategy_mod.REGISTRY.get(key)
    status = meta.status if meta else "unknown"
    weight = meta.max_weight if meta else None
    if key == "core_etf" and core_retired():
        return "retired", 0.0
    return status, weight


def by_strategy(desk) -> dict:
    cases = desk.paper.list_cases(limit=500)
    market = _market_for(desk, cases)
    equity = float(desk.book.summary()["equity"]) or 1.0
    exposure = _tagged_positions(desk)

    rows: dict[str, dict] = {}
    for key in strategy_mod.REGISTRY:
        rows[key] = {"strategy": key, "closed": 0, "wins": 0, "total_pnl": 0.0,
                     "mean_pnl_pct": 0.0, "mean_excess": None, "excess_n": 0,
                     "open_value": round(exposure.get(key, 0.0), 2),
                     "open_weight": round(exposure.get(key, 0.0) / equity, 4)}

    for case in cases:
        key = str(case.get("strategy") or "llm")
        row = rows.setdefault(key, {"strategy": key, "closed": 0, "wins": 0,
                                    "total_pnl": 0.0, "mean_pnl_pct": 0.0,
                                    "mean_excess": None, "excess_n": 0,
                                    "open_value": 0.0, "open_weight": 0.0})
        pnl_pct = float(case.get("pnl_pct") or 0.0)
        row["closed"] += 1
        row["wins"] += 1 if pnl_pct > 0 else 0
        row["total_pnl"] = round(row["total_pnl"] + float(case.get("pnl") or 0.0), 2)
        row["mean_pnl_pct"] += pnl_pct
        opened = str(case.get("opened_at") or "")[:10].replace("-", "")
        closed = str(case.get("closed_at") or "")[:10].replace("-", "")
        bench = _window_return(market, opened, closed)
        if bench is not None:
            excess = (1 + pnl_pct) / (1 + bench) - 1
            row["excess_sum"] = row.get("excess_sum", 0.0) + excess
            row["excess_n"] += 1

    out = []
    for row in rows.values():
        n = row["closed"]
        row["mean_pnl_pct"] = round(row["mean_pnl_pct"] / n, 5) if n else 0.0
        row["win_rate"] = round(row["wins"] / n, 3) if n else None
        if row["excess_n"]:
            row["mean_excess"] = round(row.pop("excess_sum") / row["excess_n"], 5)
        else:
            row.pop("excess_sum", None)
        row["status"], row["max_weight"] = strategy_status(row["strategy"])
        out.append(row)

    return {"as_of": desk.market.latest_bar_date(), "strategies": out,
            # Which sessions the benchmark was actually built from. A reader who
            # sees an excess number should be able to see its denominator.
            "excess_window": ({"from": str(market.index.min()),
                               "to": str(market.index.max()),
                               "sessions": len(market)}
                              if not market.empty else None),
            "note": "mean_excess 与 arena 使用同一窗口口径；closed=0 的策略还没有可归因的样本"}


def by_persona(desk) -> dict:
    """Per-persona attribution: which decision style actually made the money.

    Unlabelled rows are kept separate on purpose. Cases closed before P26 carry
    no persona, and assigning them to whoever happens to be running today would
    fabricate the exact label this table exists to measure.
    """
    from . import persona as persona_mod

    cases = desk.paper.list_cases(limit=2000)
    market = _market_for(desk, cases)
    equity = float(desk.book.summary()["equity"]) or 1.0
    exposure = _tagged_positions_by(desk, "persona", "unlabelled")

    def blank(key: str) -> dict:
        return {"persona": key, "closed": 0, "wins": 0, "total_pnl": 0.0,
                "mean_pnl_pct": 0.0, "mean_excess": None, "excess_n": 0,
                "open_value": round(exposure.get(key, 0.0), 2),
                "open_weight": round(exposure.get(key, 0.0) / equity, 4)}

    rows = {k: blank(k) for k in persona_mod.REGISTRY}
    rows["unlabelled"] = blank("unlabelled")

    for case in cases:
        key = str(case.get("persona") or "unlabelled")
        row = rows.setdefault(key, blank(key))
        pnl_pct = float(case.get("pnl_pct") or 0.0)
        row["closed"] += 1
        row["wins"] += 1 if pnl_pct > 0 else 0
        row["total_pnl"] = round(row["total_pnl"] + float(case.get("pnl") or 0.0), 2)
        row["mean_pnl_pct"] += pnl_pct
        opened = str(case.get("opened_at") or "")[:10].replace("-", "")
        closed = str(case.get("closed_at") or "")[:10].replace("-", "")
        bench = _window_return(market, opened, closed)
        if bench is not None:
            excess = (1 + pnl_pct) / (1 + bench) - 1
            row["excess_sum"] = row.get("excess_sum", 0.0) + excess
            row["excess_n"] += 1

    out = []
    for row in rows.values():
        n = row["closed"]
        row["mean_pnl_pct"] = round(row["mean_pnl_pct"] / n, 5) if n else 0.0
        row["win_rate"] = round(row["wins"] / n, 3) if n else None
        if row["excess_n"]:
            row["mean_excess"] = round(row.pop("excess_sum") / row["excess_n"], 5)
        else:
            row.pop("excess_sum", None)
        meta = persona_mod.REGISTRY.get(row["persona"])
        row["name"] = meta.name if meta else "未标注（P26 之前）"
        row["max_weight"] = meta.max_weight if meta else None
        out.append(row)

    return {"as_of": desk.market.latest_bar_date(), "personas": out,
            "note": "unlabelled 是 P26 之前平仓、票面没有 persona 的记录；不按今天的风格回填"}
