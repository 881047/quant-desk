"""Context pack: the whole trading state in under 4 KB.

The model gets a compact, factual snapshot and then drills down with tools. If
the pack is too large it is trimmed deterministically (candidates first, then
history), never truncated mid-JSON.
"""

from __future__ import annotations

import json
from copy import copy, deepcopy
from datetime import datetime
from pathlib import Path

from service import settings
from service.adapters.brief import build_brief
from service.kernel.clock import SHANGHAI

from .readiness import check as check_readiness
from .screen import ScreenConfig, affordable_price, screen

MAX_BYTES = settings.integer("context.max_bytes")

#: What a candidate row is allowed to spend the budget on, in this envelope.
#: `screen` returns more than this (board, band, as_of); see the projection in
#: `build_context_pack` for why those three are not here.
#: 候选只带**原始分位**（mom20_pct / adv20_pct）与筛选键 rank_key。
#: 综合评分不进 context：模型会把一个加权分当成质量结论，而那个 0.65/0.35
#: 只是「哪 N 只进候选」的规则权重。
CANDIDATE_FIELDS = ("symbol", "name", "close", "pct_chg", "mom20", "adv20_cny",
                    "rank_key", "mom20_pct", "adv20_pct", "why")



def _sentiment(root: Path, date: str) -> dict | None:
    """The freshest sentiment block, tagged with the session it is for.

    This used to read ``{date}.json`` and nothing else. On any evening before the
    snapshot ran — or on a weekend, or after a missed run — the whole pack said
    ``"sentiment": null`` while yesterday's complete block sat next door. The
    ``trade_date`` key is what makes the fallback honest rather than a lie about
    which day the reader is looking at.
    """
    from .sentiment import gates, read_snapshot

    payload, trade_date = read_snapshot(root / "data" / "sentiment", date)
    if payload is None:
        return None
    block = {**payload["sentiment"], "trade_date": trade_date}
    # `height_ok` was the only gate check that reached the pack, and it arrived
    # without its two siblings or the threshold behind it. A bare
    # `break_rate: 0.3974` is not judgeable: it is 1.7pp over the 0.38 gate, and
    # the reader has no way to know that from the number. The verdict comes from
    # the same `gates()` the guard uses, so the pack and /market/pools cannot
    # drift into two opinions about the same tape.
    verdict = gates(payload["sentiment"])
    block["gate"] = {
        "ok": verdict["ok"],
        "failed": verdict["failed"],
        # The threshold that actually judged the number that arrived. Reading it
        # off the config again reported a vendor 0.3621 beside the derived 0.38
        # on 2026-11-13 — a verdict nobody could reproduce from the pack.
        "break_rate_gate": verdict["break_rate_gate"],
        **({"break_rate_basis": verdict["break_rate_basis"]}
           if verdict.get("break_rate_basis") else {}),
        **({"indeterminate": verdict["indeterminate"]}
           if verdict["indeterminate"] else {}),
    }
    return block


ALERT_FILES = ("ALERT.txt", "READINESS_ALERT.txt")


def _alert(root: Path) -> str | None:
    """Pipeline failures still standing, from either writer.

    Two producers, one field. `daily_job` records a run that failed; the
    scheduled `check_readiness.py` records a day that could not be checked at all
    because the job never started — the machine was off, or the task disabled.
    A session should not have to know which one spoke, and on 2026-10-29 nobody
    spoke: the bar was missing, `data_ready` was false, and a close report was
    written anyway.
    """
    parts: list[str] = []
    for name in ALERT_FILES:
        path = root / "data" / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            parts.append(text)
    if not parts:
        return None
    # chr(10) rather than "\n" only because this line is generated into the file
    # by a tool that eats escapes; the separator is a plain newline.
    return chr(10).join(parts)[:300] or None


def build_context_pack(desk, *, root: Path | None = None, max_candidates: int = 5) -> dict:
    root = root or Path(__file__).resolve().parents[2]
    date = desk.market.latest_bar_date() or desk.market.last_trading_day()
    if not date:
        return {"error": "no trading calendar"}
    today = datetime.now(SHANGHAI).strftime("%Y%m%d")
    # `data_ready` used to be `date == today`: true or false, and read by nobody.
    # The 2026-10-29 close report was written while it was false. It is now a
    # verdict with a code and an explicit list of what it blocks.
    ready = check_readiness(desk.market)
    data_ready = ready.ok
    # Two different questions: is today's bar stored (no, until 17:30), and can
    # an order be priced right now (yes, off a live quote). The pack used to
    # answer only the first and then tell the model "此刻下单会被拒绝", which is
    # how a desk that could fill spent the session believing it could not.
    tradable, quote_why = _can_price(desk)
    # Reading the pack is not account maintenance. Value a private preview so
    # current marks/T+1 are visible without persisting a rollover, writing audit
    # events, or settling queued orders. All downstream account readers must use
    # this same view; mixing it with desk.book would reintroduce two timelines.
    book = desk.book.read_view(date, live=True)
    desk = copy(desk)
    desk.book = book
    summary = book.summary()

    brief = build_brief(
        summary=desk.registry.summary(),
        calendar=desk.market.calendar,
        today=date,
        rt_age_seconds=0.0 if tradable else None,
        sentiment=None,
    )

    # Explicit projection, not "everything except as_of".
    #
    # Each row carries its own `as_of`, and the pack already states it at the
    # top: five copies of one date is 85 bytes, and this is a 4 KB envelope. The
    # same argument now applies to `board` and `band`, which `screen` gained on
    # 2026-11-01 — 13 measured bytes a row, 65 for the five here, and this
    # envelope had 56: the candidate list went from five names to three. They
    # are not missing from the desk, they are one call away
    # (`quant_screen` / `quant_profile`), and they matter at the step *after*
    # this one: a band decides stop distance, which nobody computes while
    # reading "what state is my account in".
    #
    # A named allowlist rather than a denylist, because a denylist silently
    # inherits whatever `screen` grows next, and the budget is 56 bytes.
    candidates = [{k: row[k] for k in CANDIDATE_FIELDS if k in row}
                  for row in screen(desk.market, date, ScreenConfig(
                      max_candidates=max_candidates,
                      max_price=affordable_price(float(summary["equity"])),
                  ))]

    recent = []
    for ticket in desk.paper.list_tickets(limit=3):
        row = {
            "id": ticket["ticket_id"],
            "symbol": ticket.get("symbol"),
            "side": ticket.get("side"),
            "status": ticket.get("status"),
            "qty": ticket.get("qty"),
        }
        # A rejected ticket without its reason is a row the reader can count but
        # not learn from, and the cost is the next call that repeats the same
        # intent. `reasons` is already stored on the ticket; the pack was the
        # only place that dropped it. Truncated to 40 characters because this is
        # a 4 KB envelope and "为什么被拒" fits in a sentence.
        reasons = ticket.get("reasons") or []
        if reasons:
            row["why"] = str(reasons[0])[:28]
        recent.append(row)

    pack = {
        "as_of": date,
        "session": {
            "calendar_today": today,
            "last_bar": date,
            "market_open_today": desk.market.calendar.is_trading_day(today),
            "data_ready": data_ready,
            "readiness": ready.code,
            "blocked": ready.as_dict()["blocks"],
            "tradable_now": tradable,
            "bars_behind": ready.bars_behind,
            # 用具体日期，不写 "today"：这个块里 `calendar_today` 是 10-31（周六）
            # 而 `last_bar` 是 10-30，一句 "today's bar is available" 里的 today
            # 指的是后者——两个不同的日子写在同一个块里，读者只能猜。
            "note": (f"{date} 的日线已入库，按它定价" if data_ready else
                     f"{ready.detail} " +
                     (f"现在（{quote_why}）可以按实时报价下单，propose 时把 symbol "
                      f"带上——定价是逐只的。" if tradable else
                      f"且现在没有可成交的实时价（{quote_why}）；"
                      f"此刻下单会被拒绝（DATA_NOT_READY），不要把它读成'没有机会'。")),
        },
        # These two booleans answer "is the data there", not "may I trade".
        #
        # Named `core_ok` / `aggressive_ok` they read as permission, and they sat
        # ~900 bytes above `market.permission`, which on 2026-11-01 said the
        # opposite: 只允许减仓与对冲，不新开多头. A reader holding both had no
        # way to tell which one was the rule — and the prompt's own sentence
        # ("aggressive_ok=false 时不做游资仓") reinforced the wrong reading.
        # `usable` names the property actually measured; `meaning` says the rest
        # once, so no single key has to carry the distinction.
        "data": {
            "core_usable": brief["core"]["usable"],
            "aggressive_usable": brief["aggressive"]["usable"],
            "meaning": "数据可用性，不是准入",
            # Only when something is missing: an empty list is a byte that says
            # nothing, and this envelope is 4 KB.
            **({"core_missing": brief["core"]["missing"]}
               if brief["core"]["missing"] else {}),
            **({"aggressive_missing": brief["aggressive"]["missing"]}
               if brief["aggressive"]["missing"] else {}),
        },
        "account": {
            "read_only": True,
            "equity": summary["equity"],
            "cash": summary["cash_available"],
            "market_value": summary["market_value"],
            "day_pnl_pct": summary["daily_pnl_pct"],
            "drawdown": summary["drawdown"],
            "positions": _positions(desk, summary, date),
            "prices": _price_state(desk, summary),
        },
        "risk": {
            "limits": desk.limits(),
            # 开工契约 v2 写的是 `quant_context.risk.fill_mode`（8 个 prompt 都这么读），
            # 而 fill_mode 原本只躺在 `risk.limits` 里 —— 模型会读到 None，然后自己猜
            # 成交口径。同一个值两处暴露，`limits.fill_mode` 保留给既有消费者。
            "fill_mode": desk.limits()["fill_mode"],
            "open_slots": max(0, desk.limits()["max_positions"] - len(summary["positions"])),
            "kill_switch": summary["kill_switch"],
            "safe_mode": summary["safe_mode"],
        },
        "observations": _observations(desk, date),
        "market": _regime(desk, date),
        "exposure": _exposure(desk, date),
        "sentiment": _sentiment(root, date),
        "candidates": candidates,
        # The screen ranks by 20-day momentum and liquidity, and momentum is one
        # of the structures the ledger has rejected. These are a starting
        # universe to look at, not a signal to copy — say so where they appear,
        # because a list of five names with a score reads as a recommendation.
        "candidates_basis": ("按 20 日动量(0.65)+20 日均额(0.35) 排序的可负担池；"
                             "score=两项排名加权均值(1.0=都最前)，非概率；"
                             "20 日动量本身已在 ledger 被判 rejected。这是待查清单，不是信号。"
                             "close 是 as_of 那一日日线的收盘价，盘中不是现价，"
                             "下单前必须用 quant_quote 复核实时价与涨跌停状态。"),
        "recent_tickets": recent,
        "pipeline_alert": _alert(root),
        "rules": [
            "propose intent only: symbol/side/sleeve/thesis/evidence/invalidation",
            "never send price; qty or target_weight, never both",
            "one ticket <= 4% equity at risk; <=3 positions",
            "no ST, no suspended; price must be within inclusive [down_limit, up_limit]",
            "cite evidence ids and a falsifiable invalidation",
        ],
    }
    return _fit(pack)


def _can_price(desk) -> tuple[bool, str]:
    """Can an order be priced right now, at a price someone could fill at?

    This is the question the pack got wrong. It used to answer "today's bar is
    missing" and conclude "orders will be refused", while `DecisionService`
    happily priced off a live quote — so the workspace agent read a permission
    problem that was not there.
    """
    try:
        from .persona import probe

        rt = (probe(desk.market, desk.registry) or {}).get("rt_quote") or {}
        if rt.get("tradable"):
            return True, f"盘中，{rt.get('session_label') or '连续竞价'}"
        if rt.get("feed_available"):
            return False, f"{rt.get('session_label') or '盘后'}，只有收盘静态价"
        return False, "行情源不可用（断网或缺少密钥）"
    except Exception:  # noqa: BLE001 - a failed probe is a "no", not a crash
        return False, "行情状态读不到"


def _positions(desk, summary, date: str) -> list[dict]:
    """Holdings at the same mark as the account summary, without a second quote.

    Price equality cannot prove provenance: a live quote can equal the close.
    The receipt is captured by the same book mark that priced the account.
    """
    from .attribution import position_provenance

    provenance = position_provenance(desk)
    out = []
    for p in summary["positions"]:
        last = p["last_price"]
        try:
            weight = float(last) * int(p["qty"]) / float(summary["equity"] or 1)
        except (TypeError, ValueError):
            weight = p["weight"]
        marker = getattr(getattr(desk, "book", None), "mark_receipt", None)
        receipt = marker(p["symbol"]) if callable(marker) else {
            "price_source": "book_mark_unknown", "price_as_of": None,
            "price_age_seconds": None, "is_realtime": None,
        }
        out.append({
            "symbol": p["symbol"], "name": p["name"], "qty": p["qty"],
            "available": p["available_qty"], "cost": p["avg_cost"],
            "strategy": provenance.get(p["symbol"], {}).get("strategy") or "llm",
            "entry_client_request_id": provenance.get(p["symbol"], {}).get("entry_client_request_id"),
            "entry_ticket_id": provenance.get(p["symbol"], {}).get("entry_ticket_id"),
            "last": str(last), "weight": round(weight, 3),
            "price_source": receipt["price_source"],
            "price_as_of": (None if receipt["price_as_of"] is None
                            else str(receipt["price_as_of"])),
            "price_age_seconds": receipt["price_age_seconds"],
            "is_realtime": receipt["is_realtime"],
        })
    return out


def _price_state(desk, summary) -> dict:
    try:
        health = desk.quote_health()
    except (ValueError, KeyError, RuntimeError):
        return {"realtime_available": False}
    return {"session": health["session"]["label"],
            "in_session": health["session"]["in_session"],
            "realtime_available": health["realtime_available"],
            "sources": health["sources"],
            "note": "position.last 与账户 summary 共用一次 book mark；缺少同源凭证时 price_source=book_mark_unknown，不按价格猜来源。"}


def _observations(desk, date: str) -> dict:
    """Constraint state, compressed to a few lines.

    A stop that breached overnight is not something the model should have to
    remember to ask about. This carries the state only — no instruction.
    """
    try:
        from .observation import snapshot

        obs = snapshot(desk, date)
    except (settings.MissingParameter, settings.BadParameter):
        # A missing rate is a configuration error, not "the snapshot is
        # unavailable": folding it into the fallback hides the one fact the
        # reader needs. Catch it explicitly and let it out.
        raise
    except (ValueError, KeyError, RuntimeError) as exc:
        # A failed observation is not an observation of zero constraints.  Keep
        # the response shape stable for callers, but make every count unknown
        # and carry an explicit machine-readable failure state.
        return {
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
            "constraints": None,
            "high_attention": None,
            "deviations": None,
            "items": [],
        }
    items = []
    for record in obs.get("constraints", [])[:3]:
        fact = record.get("fact") or {}
        items.append({
            "id": record.get("id"),
            "kind": record.get("kind"),
            "subject": record.get("subject"),
            "attention": record.get("attention"),
            "fact": {k: v for k, v in list(fact.items())[:5]},
        })
    # Deviations were counted and never shown. On 2026-11-01 the pack read
    # `deviations: 1` next to `items: []` — the one line reporting a gap between
    # the rule and the book was in /observations and nowhere in the pack, so the
    # reader could not tell whether it was worth a call. Counts and items now
    # describe the same set: whatever is counted is at least stubbed, and what
    # was cut is said in `items_dropped` rather than left as arithmetic.
    room = max(0, 3 - len(items))
    shown_dev = obs.get("deviations", [])[:room]
    for record in shown_dev:
        items.append({
            "id": record.get("id"),
            "kind": record.get("kind"),
            "attention": record.get("attention"),
            "note": str(record.get("note") or "")[:60],
        })
    counts = obs.get("counts", {})
    dropped = (len(obs.get("constraints", [])) + len(obs.get("deviations", []))
               - len(items))
    constraint_count = counts.get("constraints", 0)
    attention_count = counts.get("high_attention", 0)
    deviation_count = counts.get("deviations", 0)
    out = {
        "status": ("ok" if any((constraint_count, attention_count, deviation_count))
                   else "empty"),
        "constraints": constraint_count,
        "high_attention": attention_count,
        "deviations": deviation_count,
        "items": items,
        "note": "事实与测算，不是建议；完整测算见 /observations。",
    }
    if dropped > 0:
        out["items_dropped"] = dropped
    return out


def _regime(desk, date: str) -> dict | None:
    try:
        from .regime import compute

        r = compute(desk.market, date)
    except (ValueError, KeyError):
        return None
    # 这里原来带 `permission`：一句"只允许减仓与对冲，不新开多头"。
    # 它是祈使句，而 guard 不读这个标签——`enforced: False` 就在旁边承认了。
    # 一句话像规则又不被执行，比不写更糟：读者要么高估它（以为会被拒单），
    # 要么低估它（以为纯装饰），两种都错，差别是一整个仓位。
    #
    # 现在给的是标签的**算式摘要**（`label_basis`）：趋势方向、波动率分位对
    # 哪个阈值、宽度对哪个阈值。同样的信息，读者可以同意也可以反对这个阈值。
    # 全量形态在 `/regime` 的 label_from 里——这里只放得下一行。
    # `enforced` 留着且仍是 False——它不是"这句话是软的"，而是"没有任何检查读它"。
    return {"label": r.label, "trend": r.trend, "vol_state": r.vol_state,
            "breadth": round(r.breadth, 3), "vol_20d": round(r.vol_20d, 3),
            "label_basis": r.label_basis(), "enforced": False}


def _exposure(desk, date: str) -> dict | None:
    try:
        from .exposure import describe

        e = describe(desk.book, desk.market, date)
    except (ValueError, KeyError):
        return None
    return {"invested": round(e.invested, 3), "cash": round(e.cash_weight, 3),
            "by_industry": {k: round(v, 3) for k, v in
                            sorted(e.by_industry.items(), key=lambda kv: -kv[1])[:4]},
            "top1": round(e.top1, 3), "beta": None if e.beta is None else round(e.beta, 2),
            # 这份投影是给首屏用的，别处 `exposure.describe` 已经带原因了；这里不写，
            # 模型看到的就是一个孤零零的 null（"beta 是 0 还是不知道？"）。
            **({} if e.beta is not None else {"beta_note":
               "空仓：没有仓位可加权，beta 不适用" if not e.positions
               else "持仓历史不足以估 beta"})}


def _fit(pack: dict) -> dict:
    if _size(pack) <= MAX_BYTES:
        return pack
    fitted = deepcopy(pack)
    omitted: list[str] = []
    fitted["omitted"] = omitted

    def fits() -> bool:
        return _size(fitted) <= MAX_BYTES

    def trim(key: str, keep: int) -> bool:
        rows = fitted.get(key)
        if isinstance(rows, list) and len(rows) > keep:
            omitted.append(f"{key}:{len(rows) - keep}")
            fitted[key] = rows[:keep]
        return fits()

    if trim("candidates", 3) or trim("recent_tickets", 0):
        return fitted
    for cand in fitted.get("candidates", []):
        if isinstance(cand, dict) and "why" in cand:
            cand.pop("why")
            omitted.append("candidates.why")
    if fits() or trim("candidates", 2) or trim("candidates", 1) or trim("candidates", 0):
        return fitted

    # Everything below is the fallback, and its **order is the point**. It used to
    # start with `sentiment`, then `observations`, then `market` — so the first
    # things sacrificed when a pack ran long were the two answers this pack exists
    # to give: "what state is the market in" and "what does the tape feel like".
    # Dropping them produced a response that looked complete and was answering a
    # different question. Drop commentary first, then measurements, and never
    # silently drop the tape.
    for key in ("rules", "candidates_basis", "exposure"):
        if key in fitted:
            fitted.pop(key)
            omitted.append(key)
        if fits():
            return fitted
    if "observations" in fitted:
        fitted.pop("observations")
        omitted.append("observations")
    if fits():
        return fitted

    # Even the tape will not fit: something upstream produced an enormous block.
    # Return a deliberately thin but *labelled* pack rather than a plausible one
    # with `market` and `sentiment` quietly missing.
    keep = ("as_of", "session", "data", "market", "sentiment", "account", "risk")
    slim = {k: fitted[k] for k in keep if k in fitted}
    slim["omitted"] = sorted({*omitted, *(k for k in fitted if k not in keep)})
    slim["degraded"] = True
    if _size(slim) <= MAX_BYTES:
        return slim
    # 连盘面骨架都装不下，说明上游某个块本身失控。给一个说清楚的错误包，
    # 不要把一个缺了 market/sentiment 的"正常"包发出去。
    return {"degraded": True, "omitted": ["content"],
            "error": (f"context pack 仍然超过 {MAX_BYTES} 字节："
                      f"market/sentiment 等核心块本身过大，先查数据源")}


def _size(pack: dict) -> int:
    return len(json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
