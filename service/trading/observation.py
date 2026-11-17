"""Observation: the kernel as an information source, not an adviser.

A suggestion says "sell this". Information says: "the close is 3.8% below the
stop you wrote on the ticket, the position is 18.5% of equity, exiting at the next
open would cost 4 bps of impact and CNY 5 of commission, and in the last 250
sessions this stock fell 3% or more on 14 days — five sessions later the median
was -1.2%, 36% of those outcomes were positive, and the worst was -9.4%."

The second form is strictly more useful, because it does not smuggle a judgement
into a number. It also survives being wrong: if the model decides to hold, it did
so with the distribution in front of it.

So this module has no ``action``, no ``recommendation``, no ``should``. Every
record is ``fact`` (what is true now, measured), ``measures`` (what it would cost
or pay under an explicit hypothetical), and ``attention`` (how much of the
account's risk sits behind this fact). Choosing is not in scope.

Two consequences worth stating:

* There is nothing to accept or reject, so there is no acknowledgement table.
  Whether the model acted is visible in `orders` and `cases`, which is the record
  that matters.
* A breached stop still blocks *new* risk. That is not advice, it is the same
  rule as "do not add to a position that is already past its risk budget", and it
  is evaluated from prices, not from whether the model replied to a message.
"""

from __future__ import annotations

import math

import numpy as np

from service.kernel.models import D
from service.settings import BadParameter, MissingParameter

from .impact import estimate as impact_estimate
from .impact import finite as impact_finite


def _f(value, ndigits: int = 4):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, ndigits)


# ------------------------------------------------------------------ statistics
def _tail_stats(hist, threshold: float = -0.03, horizon: int = 5) -> dict | None:
    """How this symbol behaved after days like today.

    Uses only its own history and enters at the next open, matching how the desk
    fills. Cheap enough to compute inside an observation: no index, no model.
    """
    if hist is None or len(hist) < 80:
        return None
    close = hist["close"].astype(float).to_numpy()
    open_ = hist["open"].astype(float).to_numpy()
    pre = hist["pre_close"].astype(float).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = np.where(pre > 0, close / pre - 1.0, 0.0)
        px = np.cumprod(1.0 + np.clip(ret, -0.5, 0.5))
        open_adj = np.concatenate([[np.nan], px[:-1]]) * np.where(pre > 0, open_ / pre, np.nan)
    events = np.where(ret[:-horizon - 1] <= threshold)[0]
    if len(events) < 5:
        return {"n": len(events), "note": f"历史上跌幅 <= {threshold:.0%} 的日子不足 5 次"}
    fwd = []
    for i in events:
        entry, exit_ = open_adj[i + 1], px[i + 1 + horizon]
        if np.isfinite(entry) and entry > 0 and np.isfinite(exit_):
            fwd.append(exit_ / entry - 1.0)
    if len(fwd) < 5:
        return {"n": len(fwd), "note": "可比样本不足"}
    arr = np.array(fwd)
    return {
        "condition": f"单日跌幅 <= {threshold:.0%}",
        "horizon": horizon,
        "entry": "next_open",
        "n": len(arr),
        "mean": _f(arr.mean()),
        "median": _f(float(np.median(arr))),
        "win_rate": _f(float((arr > 0).mean()), 3),
        "p10": _f(float(np.percentile(arr, 10))),
        "worst": _f(float(arr.min())),
        "note": "这只票自己的历史，不是全市场；样本重叠，n 不是独立证据数",
    }


# ----------------------------------------------------------------- constraints
def _sell_math(desk, symbol: str, qty: int, day: str,
               available: int | None = None) -> dict | None:
    """What an exit at the next open would actually cost, if it can happen.

    Priced on the whole position even when T+1 freezes part of it: the number the
    decision needs is "what is this exit worth", and the freeze is reported
    separately rather than turning the answer into zero.
    """
    if qty <= 0:
        return None
    snap = desk.market.snapshot(symbol, day)
    if snap is None or snap.last <= 0:
        return None
    inst = desk.market.instrument(symbol, day)
    price = float(snap.last)
    value = D(qty) * snap.last
    fees = desk.book.cost.sell_fees(value, inst)
    hist = desk.market.history(symbol, day, 25)
    sigma = 0.0
    if len(hist) > 5:
        col = "adj_close" if "adj_close" in hist.columns else "close"
        close = hist[col].astype(float)
        sigma = impact_finite(close.pct_change().tail(20).std(ddof=1))
    adv = float(desk.market.adv_map(day).get(symbol, 0) or 0.0)
    imp = impact_estimate(qty, price, adv, sigma) if adv > 0 else None
    free = qty if available is None else max(0, int(available))
    return {
        "price_ref": _f(price, 3),
        "qty": int(qty),
        "qty_sellable_today": free,
        "blocked_t1": free < qty,
        "gross": _f(float(value), 2),
        "fee_cny": _f(float(fees), 2),
        "impact_bps": None if imp is None else _f(imp.impact_bps, 2),
        "impact_cny": None if imp is None else _f(imp.cost_cny, 2),
        "net_if_filled_at_ref": _f(float(value) - float(fees) - (imp.cost_cny if imp else 0.0), 2),
        "participation": None if imp is None else _f(imp.participation, 4),
        "fill_basis": "next_open",
        "caveat": "按参考价估算；next_open 成交价未定，跳空穿过限价会作废",
    }


def stop_constraints(desk, day: str, quotes=None) -> list[dict]:
    """Positions whose close is through the stop written on their ticket.

    Reported as a state, not as an instruction: the stop is a constraint the model
    itself wrote down, and this says whether that constraint currently holds.
    """
    from .stops import open_stops

    out: list[dict] = []
    for view in open_stops(desk, day, quotes=quotes):
        if view.stop <= 0:
            continue
        gap = float(view.last / view.stop - 1)
        if gap > 0:
            continue
        hist = desk.market.history(view.symbol, day, 260)
        below = 0
        if len(hist) > 5:
            closes = hist["close"].astype(float).to_numpy()
            stop = float(view.stop)
            for value in closes[::-1]:
                if value <= stop:
                    below += 1
                else:
                    break
        out.append({
            "id": f"stop:{view.symbol}",
            "kind": "stop_breach",
            "subject": view.symbol,
            "name": view.name,
            "attention": "high",
            "fact": {
                "close": str(view.last),
                "price_source": view.price_source,
                "price_ts": view.price_ts,
                "price_age_seconds": None if view.age_seconds is None
                else round(view.age_seconds, 1),
                "stop": str(view.stop),
                "breach_pct": _f(gap),
                "sessions_below": below,
                "intraday_only": bool(view.pierce_only),
                "breach_at": view.breach_at,
                "sleeve": view.sleeve,
                "ticket_id": view.ticket_id,
            },
            "measures": {
                "exit_at_next_open": _sell_math(desk, view.symbol, int(view.qty), day,
                                                available=int(view.available)),
                "hold_and_reassess": {
                    "risk_to_stop_cny": _f(float((view.last - view.stop) * view.qty), 2),
                    "note": "已经是负数：现价在止损之下，'到止损'的距离为负",
                },
                "history": _tail_stats(hist),
            },
            "constraints": [
                f"T+1: 今日可卖 {view.available} / 持仓 {view.qty}"
                + ("" if view.available >= view.qty else "（买入当日不可卖）"),
                "卖出不受任何建议层阻断；买入在此状态下会被风控拒绝",
            ],
        })
    return out


def limit_constraints(desk, day: str) -> list[dict]:
    """Positions or the book pressing against a hard cap. Facts about the cap."""
    from .exposure import describe

    limits = desk.limits()
    summary = desk.book.summary()
    out: list[dict] = []
    equity = float(summary["equity"]) or 1.0
    try:
        exp = describe(desk.book, desk.market, day)
    except (ValueError, KeyError):
        exp = None

    for pos in summary["positions"]:
        weight = float(pos["market_value"]) / equity
        cap = limits["max_core_single"] if not pos["symbol"].startswith(
            ("51", "56", "58", "15", "16", "18")) else limits["max_core_etf_single"]
        if weight > cap + 1e-9:
            out.append({
                "id": f"cap:{pos['symbol']}",
                "kind": "single_name_cap",
                "subject": pos["symbol"],
                "name": pos.get("name"),
                "attention": "high",
                "fact": {"weight": _f(weight, 4), "cap": cap,
                         "excess": _f(weight - cap, 4)},
                "measures": {"trim_to_cap_cny": _f((weight - cap) * equity, 2)},
                "constraints": ["超过单票上限时不接受新的同标的买入"],
            })

    if exp is not None:
        for industry, weight in exp.by_industry.items():
            if weight > limits["industry_cap"] + 1e-9:
                out.append({
                    "id": f"industry:{industry}",
                    "kind": "industry_cap",
                    "subject": industry,
                    "attention": "high",
                    "fact": {"weight": _f(weight, 4), "cap": limits["industry_cap"],
                             "excess": _f(weight - limits["industry_cap"], 4)},
                    "measures": {"holdings": [p["symbol"] for p in exp.positions
                                              if p["industry"] == industry]},
                    "constraints": ["超过行业上限时该行业的新买入会被拒绝"],
                })
    return out


# ------------------------------------------------------------------ deviations
def core_deviation(desk, day: str) -> list[dict]:
    """Registered core rule vs the book it is supposed to describe.

    The kernel does not say "rebalance". It says: the rule's target weights are X,
    the book's are Y, the largest gap is Z, and closing it costs these orders and
    this much money. Whether the rule still deserves to be followed is a judgement.
    """
    from .core import CoreBook

    book = CoreBook(desk.data, desk.paper)
    need, why = book.should_rebalance(day)
    weights = book.target_weights(day)
    meta = weights.pop("_meta", {})
    plan = book.plan(day)
    # A sleeve the book cannot buy (100-share lot above half the NAV) is not a
    # "drift": the rule is unexecutable at this account size, not violated.
    skipped = set(plan["skipped_unaffordable"])
    drifted = {symbol: round(float(plan["weights"].get(symbol, 0.0))
                             - float(plan["targets"].get(symbol, 0.0)), 4)
               for symbol in (set(plan["weights"]) | set(plan["targets"])) - skipped}
    max_drift = max((abs(v) for v in drifted.values()), default=0.0)
    fees = sum(float(o["fee"]) for o in plan["orders"])
    if not plan["orders"] and not need:
        return [{
            "id": "core:drift", "kind": "target_drift", "subject": "core",
            "attention": "info",
            "fact": {"max_drift": _f(max_drift, 4), "gap_closable": False,
                     "trigger": why,
                     "skipped_unaffordable": plan["skipped_unaffordable"]},
            "measures": {"orders": 0, "fees_cny": 0.0},
            "constraints": ["核心盘当前无订单可下，或标的整手买不起"],
            "note": "规则与账本没有可执行的差距；不可买的 sleeve 留在现金里。",
        }]
    return [{
        "id": "core:drift",
        "kind": "target_drift",
        "subject": "core",
        "attention": "normal" if need else "info",
        "fact": {
            "targets": plan["targets"],
            "actual": plan["weights"],
            "drift": {k: v for k, v in drifted.items() if abs(v) >= 0.01},
            "max_drift": _f(max_drift, 4),
            "gap_closable": bool(plan["orders"]),
            "trigger": why,
            "rule": {"trend_ok": meta.get("trend_ok"), "port_vol": meta.get("port_vol"),
                     "scale": meta.get("scale")},
        },
        "measures": {
            "orders": plan["orders"],
            "fees_cny": _f(fees, 2),
            "turnover_cny": _f(sum(float(o["qty"]) * float(o["price"])
                                   for o in plan["orders"]), 2),
            "nav_after": plan["nav"],
            "cash_weight_after": plan["cash_weight"],
            "skipped_unaffordable": plan["skipped_unaffordable"],
        },
        "constraints": ["整手约束：100 股一手，买不起的 sleeve 留现金，不集中到别的标的"],
    }]


def budget_state(desk, day: str) -> dict:
    """两份不同的东西：算出来的**分配**，与 guard 真正执行的**上限**。

    原来这里只有一个 ``caps``，note 写着"超了会被 guard 拒绝"。两处都不对：
    guard 的 ``strategy_cap`` 检查的是 ``REGISTRY[key].max_weight``（静态注册值），
    不是这个按 regime × 战绩算出来的数。分配通常比注册上限严格，于是
    "超过分配但没超过 max_weight"这一段里，那句话是假的。

    判断冒充约束比没有约束更危险——读者会以为自己在做"系统允许的事"，
    而系统只是没说话。现在两个数分开给，各自标明谁执行。
    """
    from .allocator import REGISTRY, allocate, stats_from_cases
    from .arena import scoreboard
    from .regime import compute

    try:
        regime = compute(desk.market, day).label
    except (ValueError, KeyError):
        return {"ok": False, "error": "no regime"}
    decisions = scoreboard(desk)["decisions"]
    stats = stats_from_cases(desk.paper.list_cases(limit=500), decisions)
    alloc = allocate(regime, stats, book="paper")
    summary = desk.book.summary()
    equity = float(summary["equity"]) or 1.0
    used: dict[str, float] = {}
    # One read, outside the loop. This used to call ``list_tickets`` per holding —
    # each call opening SQLite and decoding 200 JSON payloads, so five positions
    # meant a thousand decodes for a table nobody waits for.
    tagged: dict[str, dict] = {}
    for ticket in desk.paper.list_tickets(limit=200, status="filled"):
        if ticket.get("side") != "BUY":
            continue
        symbol = str(ticket.get("symbol") or "")
        if symbol and symbol not in tagged:      # newest first
            tagged[symbol] = ticket

    for pos in summary["positions"]:
        # A CORE holding is not a strategy bet, so it must not consume the
        # discretionary cap. On 2026-10-26 a single 18.5% bank position in the
        # CORE sleeve was booked against `llm` and ate a third of its 60% cap,
        # because this loop only ever looked at `ticket.strategy`.
        ticket = tagged.get(str(pos["symbol"])) or {}
        sleeve = ticket.get("sleeve")
        strategy = ticket.get("strategy") or "llm"
        bucket = "core" if sleeve == "CORE" else strategy
        used[bucket] = used.get(bucket, 0.0) + float(pos["market_value"]) / equity
    return {
        "ok": True,
        "regime": regime,
        # 按 regime × 战绩 × 回撤算出的分配建议。是判断，不是闸门。
        "allocation": alloc["weights"],
        "cash_allocation": alloc["cash_weight"],
        "used": {k: _f(v, 4) for k, v in used.items()},
        "headroom_vs_allocation": {k: _f(max(0.0, v - used.get(k, 0.0)), 4)
                                   for k, v in alloc["weights"].items()},
        "core_weight": _f(used.get("core", 0.0), 4),
        # guard 真正读的那一组。静态注册值，与上面那个分配无关。
        "enforced": {
            "strategy_max_weight": {k: meta.max_weight
                                    for k, meta in REGISTRY.items()
                                    if meta.book == "paper"
                                    and meta.status not in ("research", "retired")},
            "checks": ["strategy_cap", "sleeve_cap", "industry_cap", "position_cap"],
            "by": "guard.check；超了返回 409，与 allocation 无关",
        },
        "reasons": alloc["reasons"],
        "note": ("allocation 是按 regime 与战绩算出的权重，enforced.strategy_max_weight "
                 "才是 guard 拒绝的那条线。两者之间有一段：超过 allocation 但没超过注册上限，"
                 "系统不会拒绝——要不要走进去是读的人的判断，记下理由。"
                 "CORE 袖的仓位记在 core_weight，不占策略额度。"),
    }


# ---------------------------------------------------------------------- facade
def snapshot(desk, day: str | None = None, *, live: bool = True) -> dict:
    """Everything observable about the book right now, with no opinion attached."""
    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "no daily bars"}
    quotes = {}
    if live:
        try:
            held = [p["symbol"] for p in desk.book.summary()["positions"]]
            quotes = desk.quotes(held) if held else {}
        except (ValueError, KeyError, RuntimeError):
            quotes = {}
    constraints: list[dict] = []
    for builder in (stop_constraints, limit_constraints):
        try:
            constraints.extend(builder(desk, day, quotes) if builder is stop_constraints
                               else builder(desk, day))
        except (MissingParameter, BadParameter):
            # A missing/malformed rate is a configuration error, not "this one
            # observation could not be computed": folding it into a kind="error"
            # constraint points the reader at the quote feed instead of
            # costs.yaml. ``stop_constraints`` prices exits, so it reads the cost
            # model through ``open_stops`` — the one builder that can raise this.
            # Surfacing it here is what makes the re-raise guards in ``duty`` /
            # ``context_pack`` reachable; ordinary builder failures keep their
            # per-item tolerance below. ``MissingParameter`` is a KeyError and
            # ``BadParameter`` a ValueError, so this clause must come first.
            raise
        except (ValueError, KeyError) as exc:
            constraints.append({"id": f"error:{builder.__name__}", "kind": "error",
                                "attention": "info",
                                "fact": {"error": f"{type(exc).__name__}: {exc}"}})
    try:
        deviations = core_deviation(desk, day)
    except (ValueError, KeyError) as exc:
        deviations = [{"id": "core:error", "kind": "error", "subject": "core",
                       "attention": "info",
                       "fact": {"error": f"{type(exc).__name__}: {exc}"}}]
    try:
        budget = budget_state(desk, day)
    except (ValueError, KeyError) as exc:
        budget = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "as_of": day,
        "counts": {"constraints": len(constraints),
                   "deviations": len(deviations),
                   "high_attention": len([c for c in constraints
                                          if c.get("attention") == "high"])},
        "constraints": constraints,
        "deviations": deviations,
        "budget": budget,
        "reading": ("constraints 是约束状态（止损/上限），deviations 是规则与账本的偏离，"
                    "budget 是剩余风险预算。三者都只有事实与测算，没有该不该做什么。"),
    }


def stop_gate(desk, day: str | None = None, quotes=None) -> dict:
    """Separate measured stop breaches from prices that are unknowable now.

    Three outcomes, and the distinction between the last two is the whole point:

    ``breached``  a stop exists, a price exists, and the price is at or below it.
    ``unknown``   a stop exists but no price could be obtained, so the question
                  cannot be answered. "Cannot answer" is not "no breach": adding
                  risk while a position may already be through its stop is the
                  one thing this gate refuses to allow.
    ``no_stop``   the position carries no stop ticket at all. Nothing to breach.
                  This used to be folded into ``unknown``, so a single position
                  bought without a stop blocked every future buy with a message
                  claiming the *price* was unavailable — while the price was
                  right there, and the action that would have fixed it was not
                  "wait for quotes" but "write a stop".
    """
    # Delayed, like ``tripwire.evaluate``: realtime pulls in pandas, and this
    # module is imported by the observation/again chain at process start. The
    # freshness bound is one number with one meaning — a quote older than this
    # cannot be used to decide a breach — so it must come from the same config
    # source tripwire uses, not a second literal here.
    from .realtime import STALE_AFTER, quote_freshness

    day = day or desk.market.latest_bar_date()
    positions = desk.book.summary().get("positions", [])
    held = [str(p["symbol"]) for p in positions]
    if not day or not held:
        return {"breached": [], "unknown": [], "no_stop": []}
    if quotes is None:
        try:
            quote_service = getattr(desk, "quotes", None) or getattr(desk.book, "quotes", None)
            quotes = quote_service(held) if quote_service else {}
        except (ValueError, KeyError, RuntimeError):
            quotes = {}
    stopped: set[str] = set()
    try:
        from .stops import open_stops, stop_map, stopped_symbols

        # One ticket query, two consumers. ``stop_map`` reads the filled BUY
        # tickets for the names actually held; calling each helper separately
        # decoded the newest 500 tickets twice (4.1 ms per scan at that size,
        # on every proposal).
        stops = stop_map(desk)
        stopped = stopped_symbols(desk, stops)
        views = open_stops(desk, day, quotes, stops=stops)
    except (MissingParameter, BadParameter):
        # A missing/malformed rate is a *configuration* error, and it must not be
        # translated into "the price is unknown": ``unknown`` makes every BUY a
        # 409 OPEN_VIOLATION whose message blames the quote feed, so nobody
        # learns that costs.yaml is short a key. MissingParameter is a KeyError
        # and BadParameter a ValueError, so this clause has to come before the
        # generic one below to win the match.
        raise
    except (ValueError, KeyError, RuntimeError):
        return {"breached": [], "unknown": held, "no_stop": []}

    breached: list[str] = []
    unknown: list[str] = []
    measured: set[str] = set()
    # B2: set only when a quote is measurable (<=120s) but not tradable (<=90s)
    # — the one case where the fill side and the state side disagree.
    layered = None
    for view in views:
        quote = (quotes or {}).get(view.symbol)
        age = getattr(quote, "age_seconds", None) if quote is not None else None
        fresh = bool(
            quote is not None
            and getattr(quote, "is_realtime", False)
            and age is not None
            and 0 <= float(age) <= STALE_AFTER
        )
        if fresh:
            # Keep judging the breach on STALE_AFTER: the three states and the
            # unknown/fail-closed rule are unchanged. The extra fields only make
            # the deliberate 90/120 split auditable on this answer.
            freshness = quote_freshness(float(age))
            if freshness["measurable"] and not freshness["tradable"]:
                layered = freshness
            measured.add(view.symbol)
            if view.stop > 0 and quote.last <= view.stop:
                breached.append(view.symbol)
            continue
        snap = desk.market.snapshot(view.symbol, day)
        if snap is not None and snap.last > 0:
            measured.add(view.symbol)
            if view.stop > 0 and snap.last <= view.stop:
                breached.append(view.symbol)
        else:
            unknown.append(view.symbol)

    no_stop: list[str] = []
    for symbol in held:
        if symbol in measured or symbol in unknown:
            continue
        # A stop exists but `open_stops` skipped it (no bar and no quote): the
        # price is missing, which is `unknown`. Otherwise there is no stop.
        (unknown if symbol in stopped else no_stop).append(symbol)
    gate: dict = {"breached": sorted(set(breached)),
                  "unknown": sorted(set(unknown)),
                  "no_stop": sorted(set(no_stop))}
    if layered is not None:
        # Only attached on the divergent window, so an ordinary gate answer
        # stays the exact three-state dict its consumers (and existing tests)
        # expect. quote_freshness() is always available for the full structure.
        gate["freshness_basis"] = layered["basis"]
        gate["threshold_seconds"] = layered["stale_after"]
    return gate


def evaluate_stop_gate(market, book, day: str | None = None,
                       quotes: dict | None = None) -> dict:
    """The one way to ask "is a stop breached right now" from the desk's slices.

    ``propose``, its idempotent-retry replay and ``guard.preflight`` must all
    answer the same question of the same objects. ``DecisionService`` is only a
    slice of the desk, so ``stop_gate`` needs a small shim with the paper/data
    stores and the quote callable. Building that shim in three callers is how
    one predicate grows three answers, so it lives here — identical, field for
    field, to the shim ``propose`` used to build inline.
    """
    from types import SimpleNamespace

    shim = SimpleNamespace(market=market, book=book, paper=book.store,
                           data=market.store, quotes=book.quotes)
    # ``quotes`` is forwarded verbatim. ``None`` means "ask" (proposal path, no
    # lock held); an explicit mapping — including ``{}`` — means "use only this"
    # (fill path, book lock held), and is never allowed to become a vendor call.
    return stop_gate(shim, day, quotes=quotes)


def open_breaches(desk, day: str | None = None) -> list[str]:
    """Backward-compatible measured breach list; use stop_gate for decisions."""
    return stop_gate(desk, day)["breached"]
