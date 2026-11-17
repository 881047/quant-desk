"""Account simulation: can a CNY 5,000 book actually hold the strategy?

The referee used to grade a *signal* book. ``portfolio_series`` averages the
return of every open leg on a day, so a strategy with thirty signals is scored
as a fully invested thirty-name equal-weight portfolio — a portfolio this
account cannot buy, cannot finance and, at three positions, cannot even hold.
``broken_board`` reported a 190% CAGR that way; the verdict was rescued by the
capacity check, not by the return being believable.

This module answers the other question, in the units the desk trades:

* **slots** - at most ``max_positions`` names, enforced at entry;
* **cash** - a ticket must be payable from cash on hand, with the kernel's own
  fee model (CNY 5 minimum commission included);
* **lots** - whole trading units only, so a CNY 30 stock in a CNY 1,666 slot
  buys nothing and says so instead of pretending to hold 55 shares;
* **T+1** - entries and exits come from the trade table, which already refuses
  same-day round trips for stocks.

Ordinary open positions are carried at cost until they exit, so their curve is
*realised cash equity*. The sole exception is an explicitly unresolved position:
its separate mark may value the holding, but is never sale proceeds or cash.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from service import settings
from service.kernel.costs import CostModel
from service.kernel.models import D, money

from . import stats
from .spec import T0_POOLS, EventStudy, fee_instrument

DEFAULT_MAX_POSITIONS = settings.integer("research.account_max_positions")


def _fees(model: CostModel, amount: float, instrument, side: str,
          mult: float = 1.0) -> float:
    """Fees for a notional, in the kernel's own Decimal units.

    ``mult`` is the cost-stress multiplier and it has to apply here, not only to
    slippage. ``spec.effective_cost_bps`` multiplies the *sum* of fees and
    slippage, so the stress test and the signal-level cost were reading two
    different "2x": on a 5,000 CNY account the 5-yuan minimum commission is the
    dominant term (50bps on a 1,000 CNY ticket against 20bps of slippage), and it
    was the one term the account curve never stressed.
    """
    notional = money(D(str(amount)))
    if side == "buy":
        charged = float(model.buy_fees(notional, instrument))
    else:
        charged = float(model.sell_fees(notional, instrument))
    return charged * mult


@dataclass
class CapitalResult:
    daily: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    metrics: dict = field(default_factory=dict)
    trades_taken: int = 0
    trades_skipped: int = 0
    skip_reasons: dict = field(default_factory=dict)
    avg_positions: float = 0.0
    peak_positions: int = 0
    days_in_market: int = 0
    calendar_days: int = 0
    cash_min: float = 0.0
    notional: float = 0.0
    equity: float = 0.0
    max_positions: int = DEFAULT_MAX_POSITIONS
    cash_end: float = 0.0
    open_positions: int = 0
    taken_gross: float = 0.0
    skipped_gross: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "equity_start": round(self.equity, 2),
            "max_positions": self.max_positions,
            "trades_taken": self.trades_taken,
            "trades_skipped": self.trades_skipped,
            "skip_reasons": dict(self.skip_reasons),
            "avg_positions": round(self.avg_positions, 2),
            "peak_positions": self.peak_positions,
            "days_in_market": self.days_in_market,
            "calendar_days": self.calendar_days,
            "time_in_market": (round(self.days_in_market / self.calendar_days, 4)
                               if self.calendar_days else 0.0),
            "cash_min": round(self.cash_min, 2),
            "cash_end": round(self.cash_end, 2),
            "open_positions": self.open_positions,
            "notional": round(self.notional, 2),
            # The diagnostic that matters when an account loses on a strategy
            # with a positive per-trade edge: was the edge in the names it could
            # not afford?
            "mean_gross": {"taken": round(self.taken_gross, 6),
                           "skipped": round(self.skipped_gross, 6)},
            "metrics": self.metrics,
            "note": self.note,
        }


def _empty(equity: float, max_positions: int, calendar: list[str] | None,
           note: str) -> CapitalResult:
    idx = [str(d) for d in calendar] if calendar else []
    daily = pd.Series([0.0] * len(idx), index=idx, dtype="float64") if idx else pd.Series(dtype="float64")
    return CapitalResult(
        daily=daily,
        metrics=stats.metrics(daily) if len(daily) else stats.metrics(pd.Series(dtype="float64")),
        equity=equity, max_positions=max_positions, cash_end=equity, note=note,
        calendar_days=len(idx),
    )


def simulate(trades: pd.DataFrame, spec: EventStudy, *, equity: float = 5000.0,
             max_positions: int = DEFAULT_MAX_POSITIONS,
             calendar: list[str] | None = None,
             cost_model: CostModel | None = None) -> CapitalResult:
    """Walk the trade list against cash, slots and the actual execution phase.

    Open proceeds can fund open entries; close proceeds cannot rewind time and
    fund that morning's entries. A row without an executable exit remains an
    open position. In particular, ``stuck`` prices are marks and are never cash.
    """
    if trades is None or trades.empty:
        return _empty(equity, max_positions, calendar, "no trades")

    needed = {"entry_date", "entry_price", "ts_code"}
    missing = needed - set(trades.columns)
    if missing:
        return _empty(equity, max_positions, calendar, f"missing columns: {sorted(missing)}")

    frame = trades.copy()
    frame["entry_date"] = frame["entry_date"].astype(str)
    if "exit_date" not in frame:
        frame["exit_date"] = ""
    frame["exit_date"] = frame["exit_date"].fillna("").astype(str).replace(
        {"nan": "", "None": "", "NaT": ""})
    if "exit_price" not in frame:
        frame["exit_price"] = float("nan")
    raw_exit = pd.to_numeric(frame["exit_price"], errors="coerce")
    frame["exit_price"] = raw_exit.where(raw_exit.gt(0))

    def _flag(name: str) -> pd.Series:
        if name not in frame:
            return pd.Series(False, index=frame.index)
        return frame[name].map(lambda v: (str(v).strip().lower() == "true"
                                          if isinstance(v, str) else bool(v) if pd.notna(v) else False))

    stuck = _flag("stuck")
    open_position = (_flag("open_position") | stuck | frame["exit_date"].str.len().ne(8)
                     | frame["exit_price"].isna())
    if "mark_price" not in frame:
        frame["mark_price"] = float("nan")
    frame["mark_price"] = pd.to_numeric(frame["mark_price"], errors="coerce")
    # Also protect callers still supplying the old stuck-row shape, where the
    # blocked price lived in exit_price. Convert it to a mark before clearing it.
    legacy_mark = open_position & frame["mark_price"].isna() & raw_exit.gt(0)
    frame.loc[legacy_mark, "mark_price"] = raw_exit[legacy_mark]
    if "mark_date" not in frame:
        frame["mark_date"] = ""
    frame["mark_date"] = frame["mark_date"].fillna("").astype(str).replace(
        {"nan": "", "None": "", "NaT": ""})
    legacy_date = open_position & frame["mark_date"].str.len().ne(8)
    frame.loc[legacy_date, "mark_date"] = frame.loc[legacy_date, "exit_date"]
    frame.loc[open_position, "exit_date"] = ""
    frame.loc[open_position, "exit_price"] = float("nan")

    frame = frame[pd.to_numeric(frame["entry_price"], errors="coerce").gt(0)]
    if frame.empty:
        return _empty(equity, max_positions, calendar, "no usable trades")

    event_days = set(frame["entry_date"]) | set(frame["exit_date"]) | set(frame["mark_date"])
    event_days.discard("")
    if calendar is None or not len(calendar):
        days = sorted(event_days)
    else:
        days = [str(d) for d in calendar]
        days = sorted(set(days) | (event_days - set(days)))

    cm = cost_model or CostModel()
    instrument = fee_instrument(spec.pool)
    qty_step = max(1, int(instrument.qty_step))
    mult = float(spec.cost_mult)
    slip = float(spec.cost_bps) * mult / 10_000.0
    if slip >= 1.0:
        slip = 0.99

    by_entry: dict[str, list[int]] = {}
    for idx, row in frame.iterrows():
        by_entry.setdefault(row["entry_date"], []).append(idx)
    for items in by_entry.values():
        # Deterministic priority: the trade table's own order, which already
        # reflects the spec's ranking. Ties break on trade_id.
        items.sort(key=lambda i: (frame.at[i, "trade_id"]
                                  if "trade_id" in frame.columns else i))

    cash = float(equity)
    open_positions: list[dict] = []
    curve: dict[str, float] = {}
    skips: dict[str, int] = {"slots": 0, "same_symbol": 0, "min_lot": 0, "cash": 0}
    taken = 0
    taken_gross = 0.0
    skipped_gross = 0.0
    notional = 0.0
    cash_min = cash
    peak = 0
    days_in_market = 0
    position_days = 0

    def _gross(row) -> float:
        try:
            ep = float(row["entry_price"])
            xp = float(row["exit_price"])
        except (TypeError, ValueError):
            return 0.0
        if ep <= 0 or not math.isfinite(xp):
            return 0.0
        return xp / ep - 1.0

    for day in days:
        for phase in ("open", "close"):
            if spec.exit_at == phase:
                still: list[dict] = []
                for pos in open_positions:
                    if pos["exit_date"] == day:
                        px = pos["exit_price"] * (1.0 - slip)
                        amount = px * pos["qty"]
                        fees = _fees(cm, amount, instrument, "sell", mult)
                        cash += amount - fees
                    else:
                        # No date means no fill. Never liquidate a stale mark or
                        # a missed date merely to make the curve terminate.
                        still.append(pos)
                open_positions = still

            entry_phase = "open" if spec.entry == "next_open" else "close"
            if entry_phase != phase:
                continue
            for idx in by_entry.get(day, []):
                row = frame.loc[idx]
                symbol = str(row["ts_code"])
                exit_date = str(row["exit_date"])
                gross = _gross(row)
                if exit_date and exit_date < day:
                    skips["cash"] += 1
                    skipped_gross += gross
                    continue
                if len(open_positions) >= max_positions:
                    skips["slots"] += 1
                    skipped_gross += gross
                    continue
                if any(p["symbol"] == symbol for p in open_positions):
                    skips["same_symbol"] += 1
                    skipped_gross += gross
                    continue

                px = float(row["entry_price"]) * (1.0 + slip)
                held = sum(p["qty"] * p["entry_price"] for p in open_positions)
                slot = max(0.0, (cash + held) / max_positions)
                budget = min(slot, cash)
                qty = instrument.buy_lot(math.floor(budget / px))
                if qty <= 0:
                    skips["min_lot"] += 1
                    skipped_gross += gross
                    continue
                while qty >= instrument.min_qty:
                    amount = px * qty
                    fees = _fees(cm, amount, instrument, "buy", mult)
                    if amount + fees <= cash:
                        break
                    qty -= qty_step
                qty = qty if qty >= instrument.min_qty else 0
                if qty <= 0:
                    skips["cash"] += 1
                    skipped_gross += gross
                    continue

                amount = px * qty
                cash -= amount + _fees(cm, amount, instrument, "buy", mult)
                notional += amount
                taken += 1
                taken_gross += gross
                mark_price = float(row["mark_price"])
                open_positions.append({
                    "symbol": symbol, "qty": qty, "entry_price": px,
                    "exit_price": float(row["exit_price"]), "exit_date": exit_date,
                    "mark_price": mark_price, "mark_date": str(row["mark_date"]),
                })

        def _position_value(pos: dict, day: str = day) -> float:
            # Only unresolved positions may contribute an unrealised mark. It is
            # valuation, not cash, and no sell fee/slippage is charged until exit.
            if (not pos["exit_date"] and pos["mark_date"] <= day
                    and math.isfinite(pos["mark_price"]) and pos["mark_price"] > 0):
                return pos["qty"] * pos["mark_price"]
            return pos["qty"] * pos["entry_price"]

        equity_now = cash + sum(_position_value(p) for p in open_positions)
        curve[day] = equity_now
        cash_min = min(cash_min, cash)
        peak = max(peak, len(open_positions))
        if open_positions:
            days_in_market += 1
        position_days += len(open_positions)

    daily = pd.Series(curve, dtype="float64").sort_index()
    base = float(equity)
    daily_ret = daily.pct_change()
    if len(daily):
        daily_ret.iloc[0] = daily.iloc[0] / base - 1 if base else 0.0
    daily_ret = daily_ret.fillna(0.0)

    return CapitalResult(
        daily=daily_ret,
        metrics=stats.metrics(daily_ret),
        trades_taken=taken,
        trades_skipped=sum(skips.values()),
        skip_reasons={k: v for k, v in skips.items() if v},
        avg_positions=(position_days / len(daily)) if len(daily) else 0.0,
        peak_positions=peak,
        days_in_market=days_in_market,
        calendar_days=len(daily),
        cash_min=cash_min,
        notional=notional,
        equity=equity,
        max_positions=max_positions,
        cash_end=cash,
        open_positions=len(open_positions),
        taken_gross=(taken_gross / taken) if taken else 0.0,
        skipped_gross=(skipped_gross / sum(skips.values())) if sum(skips.values()) else 0.0,
        note=(("estimation: T+0 bond rules use instrument min_qty/qty_step; "
               "cash is released only at the exit phase; " if spec.pool in T0_POOLS else "")
              + "realised cash for closed trades; unresolved positions stay open and "
              "may be valued at their explicit mark, never converted to cash"),
    )
