"""Is the model actually making money, or just making trades?

Three sleeves share one clock and one cost model:

- ``llm``    the agent's own book (the paper book it proposes into)
- ``core``   the rule-driven ETF book, which cannot be argued with
- ``market`` equal-weight buy-and-hold from the first LLM decision

The honest metric is not NAV (one lucky position can dominate a 5,000 account).
It is the **excess return of each closed decision versus the equal-weight market
over the same holding window**, with a bootstrap interval. Until that interval
clears zero on 20+ trades, "the model has an edge" is a story.
"""

from __future__ import annotations

import json
import math

import pandas as pd

from service.research import stats


def _days_before(yyyymmdd: str, days: int) -> str:
    """``days`` calendar days before ``yyyymmdd``. Positive means earlier.

    Named for the direction, not the sign: this module already carries three
    copies of the same helper with two different conventions, and calling one of
    them with a negative number is how a bounded read silently returns zero rows.
    """
    from datetime import datetime, timedelta

    return (datetime.strptime(yyyymmdd, "%Y%m%d") - timedelta(days=abs(days))).strftime("%Y%m%d")


def earliest_day(cases, keys=("opened_at", "closed_at", "day")) -> str | None:
    """The oldest date any of these rows needs a market return for.

    ``day`` covers persona switches, which have no open/close pair: a switch is
    scored from the day it happened to now.
    """
    days = []
    for case in cases or []:
        for key in keys:
            raw = str(case.get(key) or "")[:10].replace("-", "")
            if len(raw) == 8 and raw.isdigit():
                days.append(raw)
    return min(days) if days else None


def _market_returns(store, start: str | None = None) -> pd.Series:
    """Equal-weight market return per session.

    ``start`` bounds the read. Attribution used to rebuild this twenty-year
    series to look up a handful of dates, which is most of its 9.5 seconds. Ten
    calendar days of slack are subtracted from ``start`` because ``_window_return``
    anchors on the last close *before* entry — start the series at the entry day
    and every trade silently gets a different denominator.
    """
    if start:
        daily = store.read("daily", _days_before(start, 10), None,
                           columns=["trade_date", "close", "pre_close"])
    else:
        daily = store.read("daily")
    if daily.empty:
        return pd.Series(dtype="float64")
    daily["trade_date"] = daily["trade_date"].astype(str)
    px = daily[["trade_date", "close", "pre_close"]].copy()
    # A zero or missing ``pre_close`` (first session of a new listing, a halted
    # name served with zeros) makes the return inf, and ``groupby.mean`` skips
    # NaN but not inf — one such row turns the whole benchmark into inf, then
    # ``cumprod`` carries it forever. Every excess against the market becomes
    # NaN, and NaN is not valid JSON, so the endpoint that computes it fails.
    safe = px["pre_close"].where(px["pre_close"] > 0)
    px["ret"] = (px["close"] / safe - 1).replace([float("inf"), float("-inf")], float("nan"))
    return px.groupby("trade_date")["ret"].mean().sort_index()


def market_for_cases(store, cases) -> pd.Series:
    """Market series bounded to the trades being measured, or the full one.

    Reading twenty years of daily bars to look up a handful of holding windows
    is most of what these endpoints used to spend their time on. The bound comes
    from the cases themselves; if the bounded read does not reach back past the
    oldest case, it falls back to the full series rather than measuring the first
    trade from the wrong base.
    """
    start = earliest_day(cases)
    if not start:
        return _market_returns(store)
    market = _market_returns(store, start=start)
    if market.empty or market.index.min() > start:
        return _market_returns(store)
    return market


def _window_return_detail(series: pd.Series, start: str, end: str) -> dict | None:
    """Benchmark return plus the dates actually used to compute it.

    At least one session inside the requested holding window is required. Using
    the last pre-window cumulative value as both endpoints would otherwise
    fabricate a flat (0%) benchmark when the market data is absent altogether.
    """
    if series.empty or not start or not end:
        return None
    sessions = series[(series.index >= start) & (series.index <= end)]
    if sessions.empty:
        return None
    cum = (1 + series).cumprod()
    earlier = cum[cum.index < start]
    previous = len(earlier) > 0
    base_date = str(earlier.index[-1]) if previous else str(sessions.index[0])
    base = cum.loc[base_date]
    end_date = str(sessions.index[-1])
    if not math.isfinite(float(base)) or base <= 0:
        return None
    return {"return": float(cum.loc[end_date] / base - 1),
            "base_date": base_date, "end_date": end_date,
            "basis": "previous_close" if previous else "entry_close_fallback",
            "sessions": len(sessions)}


def _window_return(series: pd.Series, start: str, end: str) -> float | None:
    """Close-to-close market return over the holding window, or None if absent."""
    detail = _window_return_detail(series, start, end)
    return detail["return"] if detail is not None else None


def _tickets_for_cases(paper, cases: list[dict], batch_size: int = 500) -> dict[str, dict]:
    """Load every referenced ticket, including IDs beyond the newest 500 rows.

    ``PaperStore`` exposes bounded newest-first reads rather than offset paging.
    Growing the bound until all IDs are found (or the table is exhausted) keeps
    the lookup batched without silently assigning a missing ticket to the agent.
    """
    wanted = {str(ticket_id) for case in cases for ticket_id in (case.get("ticket_ids") or [])
              if ticket_id}
    found: dict[str, dict] = {}
    limit = batch_size
    while wanted - found.keys():
        rows = paper.list_tickets(limit=limit)
        for ticket in rows:
            ticket_id = str(ticket.get("ticket_id") or "")
            if ticket_id in wanted:
                found[ticket_id] = ticket
        if len(rows) < limit:
            break
        limit += batch_size
    return found


def _backfilled_authors(paper) -> dict[str, str]:
    """Explicit author corrections for tickets with no stored row.

    ``tickets`` holds live intents; a closed ticket's row can be cleaned while
    the append-only ``decisions`` audit log still carries a *written* correction
    (kind=author_backfill). Use it, but never infer an author: a missing ticket
    with no correction stays unknown. `directed_by` is not `actor` — the latter
    names who submitted (guard/system/human), the former who decided.
    """
    reader = getattr(paper, "list_decisions", None)
    if reader is None:
        return {}
    try:
        rows = reader(limit=1000)
    except Exception:  # noqa: BLE001 - a read failure must not invent authors
        return {}
    out: dict[str, str] = {}
    for row in rows:
        if str(row.get("kind") or "") != "author_backfill":
            continue
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                continue
        if not isinstance(payload, dict):
            continue
        tid = str(payload.get("ticket_id") or "")
        who = str(payload.get("directed_by") or "")
        if tid and who in ("agent", "user"):
            out[tid] = who
    return out


def _closing_ticket_id(case: dict) -> str | None:
    ids = case.get("ticket_ids") or []
    return str(ids[-1]) if ids else None


def _author_of(case: dict, tickets: dict[str, dict]) -> str:
    """Who closed this case; an absent ticket is unknown, never implied agent."""
    closing = _closing_ticket_id(case)
    ticket = tickets.get(closing) if closing else None
    if ticket is None:
        return "unknown"
    return str(ticket.get("directed_by") or "unknown")


def scoreboard(desk) -> dict:
    as_of = desk.market.last_trading_day()
    summary = desk.book.summary()
    equity = float(summary["equity"])
    initial = float(summary["initial_cash"])

    cases = desk.paper.list_cases(limit=500)
    tickets = _tickets_for_cases(desk.paper, cases)
    # A closed ticket may have been cleaned from `tickets` while its explicit
    # author correction survives in the audit log. Only fill IDs still missing —
    # a stored row always wins, and no correction means unknown, never agent.
    for tid, who in _backfilled_authors(desk.paper).items():
        tickets.setdefault(tid, {"ticket_id": tid, "directed_by": who})
    # With no closed case there is no holding window to benchmark, so the market
    # series is never read: `market_for_cases` falls through to the full
    # twenty-year read when `cases` is empty, and every caller — context pack,
    # dashboard, the purity test — paid 8 seconds for a series whose only
    # consumers (per-case excess and the market NAV) both require a case.
    market = market_for_cases(desk.data, cases) if cases else pd.Series(dtype="float64")

    rows = []
    gaps = []
    available_from = str(market.index.min()) if not market.empty else None
    available_to = str(market.index.max()) if not market.empty else None
    for case in cases:
        opened = (case.get("opened_at") or "")[:10].replace("-", "")
        closed = (case.get("closed_at") or "")[:10].replace("-", "")
        benchmark = _window_return_detail(market, opened, closed)
        if benchmark is None:
            gaps.append({
                "case_id": case.get("case_id"), "symbol": case.get("symbol"),
                "opened": opened, "closed": closed,
                "reason": "no market session inside the holding window",
                "available_from": available_from, "available_to": available_to,
            })
            continue
        net = float(case.get("pnl_pct") or 0.0)
        bench = benchmark["return"]
        rows.append({
            "case_id": case.get("case_id"),
            "symbol": case.get("symbol"),
            "strategy": str(case.get("strategy") or "llm"),
            "opened": opened,
            "closed": closed,
            "pnl_pct": round(net, 5),
            "market": round(bench, 5),
            "excess": round((1 + net) / (1 + bench) - 1, 5),
            "market_base_date": benchmark["base_date"],
            "market_end_date": benchmark["end_date"],
            "market_basis": benchmark["basis"],
            "market_window": {key: benchmark[key]
                              for key in ("base_date", "end_date", "basis", "sessions")},
            "directed_by": _author_of(case, tickets),
            # 未计分告警以前只有一个计数（docs/14 B3），没法知道是哪张票。
            # id 在 `_tickets_for_cases` 里本来就取回来了，只是序列化时丢了。
            "closing_ticket_id": _closing_ticket_id(case),
        })

    judged = [r for r in rows if r["directed_by"] == "agent"]
    excluded = [r for r in rows if r["directed_by"] == "user"]
    unknown_author = [r for r in rows if r["directed_by"] not in ("agent", "user")]

    edge = {"n": 0, "n_days": 0, "p_basis": None,
            "mean_excess": None, "ci_low": None, "ci_high": None,
            "p_value": None, "win_rate": None,
            # 六个 null 加一个空 note，读起来像"没有优势"；真实含义是"还没有可判定的
            # 决策"。空值必须自带原因，否则它就是一条结论。
            "note": "还没有可判定的已平仓决策：需要有基准收益的单子才能算超额", 
            "excluded_user_directed": len(excluded),
            "excluded_unknown_author": len(unknown_author),
            "excluded_missing_benchmark": len(gaps),
            "unscored_no_market": len(gaps)}
    if judged:
        judged_frame = pd.DataFrame(judged)
        excess_by_day = (judged_frame.assign(
            _day=judged_frame["closed"].astype(str).replace("", "unknown"))
            .groupby("_day")["excess"].mean().sort_index())
        # Same-day decisions are not independent: an event day produces a
        # cluster of them sharing one market move. `bootstrap_mean` resamples
        # cases one by one, which shrinks the standard error and flatters the
        # p-value — the exact failure `stats.block_bootstrap_mean`'s docstring
        # warns about. The research referee and attribution already resample
        # days; the metric that gates real capital must not be the only one
        # still pretending otherwise.
        boot = stats.block_bootstrap_mean(excess_by_day, block=5)
        # With fewer than ten independent days the block bootstrap returns a
        # degenerate interval (0, 0) and p = 1.0. Printing that as a measurement
        # is worse than printing nothing: a zero-width interval reads as
        # certainty. Gate on DAYS, not cases — 20 cases on 3 days is 3 draws.
        measured = len(excess_by_day) >= 10
        edge = {
            "n": len(judged),
            "n_days": len(excess_by_day),
            "p_basis": boot.get("p_basis") if measured else None,
            "mean_excess": round(float(judged_frame["excess"].mean()), 5),
            "ci_low": boot["ci_low"] if measured else None,
            "ci_high": boot["ci_high"] if measured else None,
            "p_value": boot["p_value"] if measured else None,
            "win_rate": round(float((judged_frame["excess"] > 0).mean()), 3),
            "note": (None if measured else
                     f"独立交易日 {len(excess_by_day)} < 10：不出区间，"
                     "同日多笔平仓不是独立样本"),
            "excluded_user_directed": len(excluded),
            "excluded_unknown_author": len(unknown_author),
            "excluded_missing_benchmark": len(gaps),
            "unscored_no_market": len(gaps),
        }

    # The whole curve, not the last row: `ever_invested` below needs the history,
    # and a note that says "1 个交易日" when the book has been idle for six is the
    # kind of small lie that makes the rest of the page untrustworthy.
    core_rows = desk.paper.core_nav_history(limit=400)
    core_nav = float(core_rows[-1]["nav"]) if core_rows else None
    market_nav = None
    benchmarked = [r for r in rows if r["market"] is not None]
    if benchmarked:
        first = min(r["opened"] for r in benchmarked)
        mret = _window_return(market, first, as_of)
        market_nav = round(initial * (1 + mret), 2) if mret is not None else None

    # 原来这里是 verdict: insufficient/edge/no_edge/unproven 加一句 why。
    # 那是替读者下"这台台子有没有 edge"的结论。`edge` 块里 n、mean_excess、
    # ci_low、ci_high、p_value、win_rate 全都在，判据（区间是否跨 0、样本够不够
    # 20 笔）也是机械的——包成一个词只增加权威，不增加信息。
    #
    # 留下的是**区间落在哪**这个事实，以及未纳入计分的样本各有多少、为什么。
    ci_low, ci_high = edge["ci_low"], edge["ci_high"]
    if edge["n"] < 20:
        ci_position = "sample_below_20"
    elif ci_low is not None and ci_low > 0:
        ci_position = "above_zero"
    elif ci_high is not None and ci_high < 0:
        ci_position = "below_zero"
    else:
        ci_position = "straddles_zero"

    not_scored = {}
    not_scored_detail = {}
    def case_ids(batch: list[dict]) -> list[str]:
        return [str(r["case_id"]) for r in batch if r.get("case_id") is not None]
    if excluded:
        not_scored["user_instruction"] = len(excluded)
        not_scored_detail["user_instruction"] = {
            "count": len(excluded), "case_ids": case_ids(excluded)}
    if unknown_author:
        not_scored["unknown_closing_ticket"] = len(unknown_author)
        not_scored_detail["unknown_closing_ticket"] = {
            "count": len(unknown_author),
            "case_ids": case_ids(unknown_author),
            "ticket_ids": [r["closing_ticket_id"] for r in unknown_author
                           if r.get("closing_ticket_id")],
            "note": "平仓票据不存在，或 directed_by 不是 agent/user；按 id 去修，"
                    "不要对着一个计数猜。",
        }
    if gaps:
        not_scored["no_holding_window_data"] = len(gaps)
        not_scored_detail["no_holding_window_data"] = {
            "count": len(gaps), "case_ids": case_ids(gaps)}

    # Days running is "how long has this book been trading", not "how many
    # decisions have closed": an open position still counts as time under test,
    # otherwise the counter reads 0 for weeks and looks like nothing is happening.
    open_dates = [str(p.get("opened_at") or "")[:10].replace("-", "")
                  for p in summary["positions"] if int(p.get("qty", 0)) > 0]
    started = [d for d in ([r["opened"] for r in rows]
                           + [g["opened"] for g in gaps] + open_dates) if len(d) == 8]
    if not started:
        buys = [t for t in desk.paper.list_tickets(limit=50, status="filled")
                if str(t.get("side", "")).upper() == "BUY"]
        started = [str(t.get("trade_date") or "")[-8:] for t in buys if t.get("trade_date")]
    days_running = (pd.Timestamp(as_of) - pd.Timestamp(min(started))).days if started else 0

    # A sleeve that has never held anything is not a competing strategy, and
    # printing its flat 0.00% next to two that actually traded reads as one.
    # On 2026-10-30 the core book had been 100% cash for six sessions because its
    # defensive leg (511010, one lot = 14,112) costs 2.8x the account — a sizing
    # problem, not a market call. `invested` is the stored fact, so the label is
    # derived rather than asserted.
    core_ever_invested = any(float(r.get("invested") or 0) > 0 for r in core_rows)
    core_retired = False
    try:
        from .core import UNIVERSE_CFG

        core_retired = bool(UNIVERSE_CFG.retired)
    except Exception:  # noqa: BLE001 - a label must not break the page
        core_retired = False
    core_note = None
    if core_rows and not core_ever_invested:
        core_note = (f"{len(core_rows)} 个交易日 invested 恒为 0：该袖从未买入任何标的，"
                     f"与 llm / market 不可比。查 /core 的 state 与 required_nav")
    if core_retired:
        core_note = ("核心盘已于 2026-11-01 关闭（一手价格超过账户规模），"
                     "不再写净值行、不再入擂台。历史 6 行保留为审计记录")
    sleeves = {
        "llm": {"nav": round(equity, 2), "return": round(equity / initial - 1, 4),
                "cash": round(float(summary["cash_available"]), 2),
                "positions": len([p for p in summary["positions"]
                                  if int(p.get("qty", 0)) > 0])},
        "market": {"nav": market_nav,
                   "return": round(market_nav / initial - 1, 4) if market_nav else None},
    }
    if not core_retired:
        # Kept in the payload when it is alive, dropped when it is not: a retired
        # sleeve is not a third competitor, and a reader who sees three columns
        # will compare all three.
        sleeves["core"] = {
            "nav": core_nav,
            "return": round(core_nav / initial - 1, 4) if core_nav else None,
            "ever_invested": core_ever_invested,
            "comparable": core_ever_invested,
            "note": core_note,
        }

    return {
        "as_of": as_of,
        "days_running": max(0, days_running),
        "sleeves": sleeves,
        "core_retired": core_retired,
        "core_note": core_note if core_retired else None,
        "decisions": rows[-50:],
        "benchmark_gaps": gaps[-50:],
        "market_window": ({"from": str(market.index.min()),
                           "to": str(market.index.max()), "sessions": len(market)}
                          if not market.empty else None),
        "edge": edge,
        # 区间落在哪、门槛看过多少样本。要不要称它 edge，由读的人定。
        "ci_position": ci_position,
        "min_decisions_for_an_interval": 20,
        "not_scored": not_scored,
        "not_scored_detail": not_scored_detail,
        "note": ("NAV alone is not evidence: a 5,000 account is one position away from "
                 "anything. The decision excess is the number that matters. "
                 "not_scored 是兼容旧消费者的计数；not_scored_detail 给出对应的 "
                 "case_ids / ticket_ids，告警可以落成待修入口。"),
    }
