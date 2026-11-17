"""Implementation shortfall, measured from the fills the desk actually booked.

The question this answers is *not* "did the strategy make money" — `arena` does
that. It is "how much of the gross edge did execution eat". Perold's 1988
decomposition splits that gap into four parts:

    delay        decision price → arrival price   (the overnight gap)
    impact       arrival price  → average fill    (slippage and footprint)
    opportunity  the unfilled remainder           (what the order never got)
    fees         commission, stamp duty, transfer fee

This desk can measure **delay + impact + fees** and cannot measure opportunity:
a partially filled ticket is retired explicitly rather than left resting, so
"what the unfilled part would have earned" has no counterfactual here. That is a
real limitation of the recorded data, not a rounding issue, and it is reported
as `opportunity_cost: "not measurable"` rather than as zero.

The benchmark is the ticket's own `price_ref` — the close the decision was made
against. A fill with no reference is counted under `no_reference`, never
silently dropped: a falling average caused by unmeasurable trades is how
execution reports start lying.
"""

from __future__ import annotations

from decimal import Decimal
from statistics import median

from service.kernel import units

BPS = units.BPS


def _bps(price: Decimal, reference: Decimal, side: str) -> float:
    """Signed cost in basis points: positive means the fill was worse."""
    if reference <= 0:
        return 0.0
    move = (price / reference - 1) * BPS
    signed = move if side.upper() == "BUY" else -move
    return float(signed)


def _frame_quote(desk, record: dict) -> Decimal | None:
    """读票据上 arm 时冻结的帧价。取不到返回 None，**不许拿 price_ref 顶替**。

    ``recent_fills`` 只给 fill payload，票据字段要按 ticket_id 回查。只有
    ``decision_quote_basis == "frame"`` 且价格为正才算有基准；unavailable 就是
    「cannot say」，渲染成 0 会让执行报告开始撒谎。
    """
    raw = record.get("decision_quote")
    basis = record.get("decision_quote_basis")
    if raw in (None, "") or str(basis) != "frame":
        getter = getattr(getattr(desk, "paper", None), "get_ticket", None)
        if getter is None:
            return None
        try:
            ticket = getter(str(record.get("ticket_id") or "")) or {}
        except Exception:  # noqa: BLE001 - 票据读不出等价于没有帧价
            return None
        raw = ticket.get("decision_quote")
        basis = ticket.get("decision_quote_basis")
    if raw in (None, "") or str(basis) != "frame":
        return None
    try:
        value = Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None
    return value if value > 0 else None


def summary(desk, *, limit: int = 500) -> dict:
    """Per-fill shortfall, split by fill basis and by side."""
    rows: list[dict] = []
    no_reference = 0
    no_quote_reference = 0
    for record in desk.paper.recent_fills(limit):
        reference = record.get("ref_price")
        if reference in (None, ""):
            no_reference += 1
            continue
        try:
            price = Decimal(str(record["price"]))
            ref = Decimal(str(reference))
            qty = int(record["qty"])
            fees = Decimal(str(record.get("fees") or 0))
            # Queue-position penalty is an arrival→fill (impact) cost, not a
            # broker fee. ``fill.fees`` no longer carries it, so ``fee_bps`` is
            # exactly the three CostModel lines; the penalty is read from its
            # own field and reported under ``impact_bps`` instead. The total is
            # unchanged — it is a reclassification, not a disappearance.
            penalty = Decimal(str(record.get("limit_up_penalty") or 0))
        except (KeyError, ArithmeticError, ValueError):
            no_reference += 1
            continue
        if qty <= 0 or price <= 0:
            no_reference += 1
            continue
        notional = price * qty
        fee_bps = float(fees / notional * BPS) if notional > 0 else 0.0
        impact_bps = float(penalty / notional * BPS) if notional > 0 else 0.0
        shortfall = _bps(price, ref, str(record.get("side", "BUY")))
        # 并列第二基准：帧价。两个 shortfall 起点不同，绝不合并、不互相覆盖，
        # 也不把差额解释成执行质量改善（见 _caveats）。
        quote = _frame_quote(desk, record)
        side = str(record.get("side", "BUY"))
        vs_quote = _bps(price, quote, side) if quote is not None else None
        if vs_quote is None:
            no_quote_reference += 1
        decision_latency = _bps(quote, ref, side) if quote is not None else None
        rows.append({
            "ticket_id": record.get("ticket_id"),
            "symbol": record.get("symbol"),
            "side": str(record.get("side", "")).upper(),
            "basis": record.get("fill_basis") or "unknown",
            "qty": qty,
            "price": float(price),
            "ref_price": float(ref),
            "shortfall_bps": round(shortfall, 2),
            "shortfall_vs_ref_bps": round(shortfall, 2),
            "shortfall_vs_quote_bps": (round(vs_quote, 2)
                                       if vs_quote is not None else None),
            "decision_quote_basis": "frame" if quote is not None else "unavailable",
            "decision_latency_bps": (round(decision_latency, 2)
                                     if decision_latency is not None else None),
            "fee_bps": round(fee_bps, 2),
            "impact_bps": round(impact_bps, 2),
            "total_bps": round(shortfall + fee_bps + impact_bps, 2),
        })

    if not rows:
        return {
            "n": 0,
            "no_reference": no_reference,
            "no_quote_reference": no_quote_reference,
            "by_basis": {},
            "by_side": {},
            "fills": [],
            "caveats": _caveats(no_reference, no_quote_reference),
        }

    def _agg(block: list[dict]) -> dict:
        values = sorted(row["total_bps"] for row in block)
        shortfalls = [row["shortfall_bps"] for row in block]
        quoted = [row["shortfall_vs_quote_bps"] for row in block
                  if row["shortfall_vs_quote_bps"] is not None]
        return {
            "n": len(block),
            "mean_shortfall_bps": round(sum(shortfalls) / len(shortfalls), 2),
            "mean_shortfall_vs_quote_bps": (round(sum(quoted) / len(quoted), 2)
                                            if quoted else None),
            "n_with_quote": len(quoted),
            "mean_total_bps": round(sum(values) / len(values), 2),
            "median_total_bps": round(median(values), 2),
            # Nearest-rank p90: ``int(len * 0.9)`` is an off-by-one for any
            # sample size divisible by ten, where it returns the maximum and
            # reports it as the 90th percentile.
            "p90_total_bps": round(values[min(len(values) - 1, (len(values) * 9 + 9) // 10 - 1)],
                                   2),
            "share_positive": round(sum(1 for v in values if v > 0) / len(values), 3),
        }

    by_basis: dict[str, list[dict]] = {}
    by_side: dict[str, list[dict]] = {}
    for row in rows:
        by_basis.setdefault(row["basis"], []).append(row)
        by_side.setdefault(row["side"], []).append(row)

    return {
        "n": len(rows),
        "no_reference": no_reference,
        "no_quote_reference": no_quote_reference,
        "overall": _agg(rows),
        "by_basis": {key: _agg(block) for key, block in sorted(by_basis.items())},
        "by_side": {key: _agg(block) for key, block in sorted(by_side.items())},
        "fills": rows[:100],
        "caveats": _caveats(no_reference, no_quote_reference),
    }


def _caveats(no_reference: int, no_quote_reference: int = 0) -> list[str]:
    notes = [
        ("shortfall = (fill/ref − 1), signed so positive means worse; "
         "ref is the decision price on the ticket"),
        "fee_bps uses the filled notional; the ¥5 minimum dominates small tickets",
        ("fee_bps is commission + stamp duty + transfer fee only; a limit-up "
         "queue penalty is reported as impact_bps (arrival→fill), not as a fee"),
        ("fills whose rule_id carries +TOUCH_DAY were triggered by the day's "
         "low/high — a 15:00 fact — so the exact fill time is unobservable; for "
         "those fills delay and impact cannot be separated, so only their "
         "combined gap is reported as shortfall_bps"),
        ("opportunity cost is not measurable: an unfilled remainder is retired, "
         "not left resting"),
        ("shortfall_vs_ref_bps uses the ticket's price_ref (daily close); "
         "shortfall_vs_quote_bps uses the frame price frozen at arm(). They are "
         "PARALLEL benchmarks, not additive — do not sum them, and do not read "
         "their difference as an execution-quality improvement or a timing gain"),
        ("decision_latency_bps = (decision_quote/price_ref − 1), signed. It is "
         "Perold's delay component, a DIFFERENT LAYER from shortfall. The arm→fill "
         "wait has no independently observable interval on this desk, so it is "
         "not separately attributed"),
    ]
    if no_reference:
        notes.append(f"{no_reference} fills carried no reference price and are "
                     "excluded from the averages, not counted as zero")
    if no_quote_reference:
        notes.append(f"{no_quote_reference} fills had no frozen frame quote; their "
                     "shortfall_vs_quote_bps is null (cannot say), never zero")
    return notes
