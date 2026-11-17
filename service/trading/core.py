"""The core sleeve: a slow ETF book the system can actually hold.

It is deliberately boring. The aggressive sleeve was falsified at P5, so the
only validated thing left is this: inverse-volatility risk parity across a small
ETF set, gated by a 200-day trend filter, scaled to a 12% volatility target,
with the remainder in a treasury ETF.

Signals use stored adjusted closes, but orders and account values use raw closes.
The book is lot-based (100 shares), charges the same commission model as the
stock book, and records one NAV row per trading day so the shadow record can be
audited later.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from service import settings
from service.kernel.units import SHARES_PER_LOT, TRADING_DAYS_PER_YEAR

from .state import PaperStore, utc_now

D = Decimal
_MEMORY_EXECUTION_LOCK = threading.RLock()

CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "core.json"


@dataclass(frozen=True)
class CoreUniverse:
    defensive: str
    risk: tuple[str, ...]
    exposures: dict[str, str]
    max_positions: int
    # A retired sleeve keeps its universe and its numbers and loses two things:
    # the right to book a NAV row every session, and the right to take money.
    # See `_retired_note` in configs/core.json for the measurement behind it.
    retired: bool = False

    @property
    def codes(self) -> tuple[str, ...]:
        return self.risk + (self.defensive,)

    def bucket(self, code: str) -> str:
        return self.exposures.get(code, "")


def _finite_price(value: object) -> float | None:
    """A usable positive price, or ``None`` for missing / NaN / inf / <= 0.

    One place, because every hand-rolled ``float(x or 0)`` in this file had the
    same hole: ``float('nan')`` is truthy, so ``or`` never fires.
    """
    try:
        price = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def _px(frame_row, symbol: str) -> float:
    """A price from a marked row, non-finite folded to 0.0 — "no price".

    Every ``float(px.get(sym, 0.0))`` in this module meant "the price, or zero
    when there isn't one", but a present NaN cell defeated it: the default only
    fires for a missing key. ``price <= 0`` then read False for NaN, so the sell
    loop fell through to ``int(nan // ...)`` and raised. Folding to 0 here makes
    the existing ``price <= 0`` branches mean what they say.
    """
    return _finite_price(frame_row.get(symbol)) or 0.0


def load_universe(path: Path | None = None) -> CoreUniverse:
    """Read the opportunity set from configs/core.json.

    This used to be two tuples in this file, while the list of funds *fetched*
    lived in configs/data.json. They had already drifted: 512690 was downloaded
    for two years and never allocated a cent. A broken config is fatal on
    purpose - silently trading a different universe is worse than not trading.
    """
    target = path or CONFIG_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read the core universe from {target}: {exc}") from exc

    defensive = str((raw.get("defensive") or {}).get("code") or "").strip()
    entries = [r for r in (raw.get("risk") or []) if str(r.get("code") or "").strip()]
    risk = tuple(str(r["code"]).strip() for r in entries)
    if not defensive or not risk:
        raise RuntimeError(f"{target} needs a defensive code and at least one risk code")
    if defensive in risk:
        raise RuntimeError(f"{target} lists {defensive} as both defensive and risk")
    if len(set(risk)) != len(risk):
        dupes = sorted({c for c in risk if list(risk).count(c) > 1})
        raise RuntimeError(f"{target} lists {dupes} more than once")

    exposures = {str(r["code"]).strip(): str(r.get("exposure") or "") for r in entries}
    exposures[defensive] = str((raw.get("defensive") or {}).get("exposure") or "defensive")
    return CoreUniverse(
        defensive=defensive,
        risk=risk,
        exposures=exposures,
        max_positions=max(1, int(raw.get("max_positions") or len(risk))),
        retired=bool(raw.get("retired") or False),
    )


UNIVERSE_CFG = load_universe()
RISK_ETFS = UNIVERSE_CFG.risk
DEFENSIVE_ETF = UNIVERSE_CFG.defensive
UNIVERSE = UNIVERSE_CFG.codes


@dataclass(frozen=True)
class CoreConfig:
    """How the core book sizes and rebalances — not what it may hold.

    ``core.json`` answers "which ETFs"; these numbers answer "how much of each".
    They were literals here, which meant tuning the volatility target or the
    per-leg cap required a code change *and* a re-run of the P0 table that quotes
    the resulting CAGR and Sharpe. They now live in ``params.yaml`` (``core.*``).

    The fee schedule is deliberately **not** a field: it is ``costs.*``, the same
    file the individual-stock sleeve charges from. A copy here would let the two
    sleeves of one account simulate two different commissions — and it did.
    ``lot`` is ``units.SHARES_PER_LOT`` for the same reason.
    """

    vol_window: int = field(default_factory=lambda: settings.integer("core.vol_window"))
    vol_target: float = field(default_factory=lambda: settings.num("core.vol_target"))
    max_weight: float = field(default_factory=lambda: settings.num("core.max_weight"))
    ma_window: int = field(default_factory=lambda: settings.integer("core.ma_window"))
    min_trade_cny: float = field(default_factory=lambda: settings.num("core.min_trade_cny"))
    fee_rate: float = field(
        default_factory=lambda: settings.num("costs.commission_rate"))
    fee_min: float = field(
        default_factory=lambda: settings.num("costs.min_commission"))
    lot: int = SHARES_PER_LOT
    # How many names the book may hold. Not a risk limit: a lot of most of these
    # ETFs costs CNY 200-700, so a 5,000 account cannot spread across seventeen
    # names however attractive the weights look. It stays in core.json because
    # the measurement that justifies it (three lots vs four vs five, on a 5,000
    # account) is about the *universe*, not about the sizing rule.
    max_positions: int = UNIVERSE_CFG.max_positions


@dataclass
class CoreState:
    cash: float = 5000.0
    holdings: dict[str, int] = field(default_factory=dict)
    last_rebalance: str = ""
    last_regime: str = ""


class CoreBook:
    def __init__(self, store, paper: PaperStore, config: CoreConfig | None = None,
                 initial_cash: float = 5000.0):
        self.store = store
        self.paper = paper
        self.cfg = config or CoreConfig()
        self._prices: pd.DataFrame | None = None
        self._raw_prices: pd.DataFrame | None = None
        self._execution_lock = threading.RLock()

    # ------------------------------------------------------------------ data
    def _load_prices(self) -> None:
        """Build separate signal and accounting series from the same bars."""
        if self._prices is not None and self._raw_prices is not None:
            return
        raw = self.store.read("fund_daily")
        adj = self.store.read("fund_adj")
        if raw.empty:
            raise RuntimeError("fund_daily is empty; run the P1 backfill")
        raw = raw[["ts_code", "trade_date", "close"]].copy()
        raw["trade_date"] = raw["trade_date"].astype(str)
        raw["raw_px"] = pd.to_numeric(raw["close"], errors="coerce")
        if not adj.empty:
            adj = adj[["ts_code", "trade_date", "adj_factor"]].copy()
            adj["trade_date"] = adj["trade_date"].astype(str)
            raw = raw.merge(adj, on=["ts_code", "trade_date"], how="left")
            factor = raw["adj_factor"].fillna(1.0)
        else:
            factor = 1.0
        raw["adj_px"] = raw["raw_px"] * factor

        def wide(values: str) -> pd.DataFrame:
            frame = raw.pivot_table(index="trade_date", columns="ts_code", values=values,
                                    aggfunc="last").sort_index()
            frame = frame.reindex(columns=[c for c in UNIVERSE if c in frame.columns])
            return frame.ffill(limit=5)

        self._raw_prices = wide("raw_px")
        self._prices = wide("adj_px")

    def universe(self) -> tuple[str, tuple[str, ...]]:
        """``(defensive, risk)`` —— 这个 sleeve 能持的代码，来自 configs/core.json。

        存在的理由是让 ``research.discover`` 不必为了两个代码去 import 退役
        引擎：核心盘的路由已 409，但这条只读的 regime 面板还在用同一份配置。
        配置有几个来源就有几处会漂移，所以由这里给出唯一答案。
        """
        return DEFENSIVE_ETF, RISK_ETFS

    def prices(self) -> pd.DataFrame:
        """Adjusted close per ETF for trends, returns and volatility only."""
        self._load_prices()
        assert self._prices is not None
        return self._prices

    def raw_prices(self) -> pd.DataFrame:
        """Unadjusted close for the ledger; no second adjustment is credited."""
        self._load_prices()
        assert self._raw_prices is not None
        return self._raw_prices

    def last_day(self) -> str:
        return str(self.prices().index.max())

    # --------------------------------------------------------------- targets
    def target_weights(self, as_of: str) -> dict:
        px = self.prices().loc[:as_of]
        if px.empty:
            raise RuntimeError(f"no ETF prices up to {as_of}")
        risk = [c for c in RISK_ETFS if c in px.columns]
        if DEFENSIVE_ETF not in px.columns or not risk:
            raise RuntimeError("core universe is incomplete in fund_daily")

        rets = px[risk].pct_change(fill_method=None)
        vol = rets.tail(self.cfg.vol_window).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)
        vol = vol.replace(0, np.nan).dropna()
        if vol.empty:
            return {DEFENSIVE_ETF: 1.0, "_meta": {"note": "no usable risk vol yet"}}

        benchmark = px["510300.SH"] if "510300.SH" in px.columns else px[risk[0]]
        ma = benchmark.rolling(self.cfg.ma_window).mean()
        trend_ok = bool(pd.notna(ma.iloc[-1]) and benchmark.iloc[-1] > ma.iloc[-1])
        detail = {"trend_ok": trend_ok,
                  "benchmark": round(float(benchmark.iloc[-1]), 4),
                  "ma": None if pd.isna(ma.iloc[-1]) else round(float(ma.iloc[-1]), 4)}

        if not trend_ok:
            return {DEFENSIVE_ETF: 1.0, "_meta": detail}

        inv = 1.0 / vol
        inv = inv / inv.sum()

        dropped: list[str] = []
        if len(inv) > self.cfg.max_positions:
            # Inverse-vol over seventeen names produces seventeen small weights
            # that mostly cannot be bought in one lot. Take the largest and say
            # what was left out, so "diversified" is a number and not a slogan.
            keep = inv.sort_values(ascending=False).head(self.cfg.max_positions).index
            dropped = [c for c in inv.index if c not in set(keep)]
            inv = inv.loc[keep]
            inv = inv / inv.sum()

        # The single-name cap is a risk limit, so it has to hold *after* the
        # breadth cut. Trimming before the cut is useless: renormalising four
        # survivors puts each of them back above a 20% cap. If the two settings
        # cannot both be satisfied, say so instead of quietly breaking the cap.
        if len(inv) * self.cfg.max_weight < 1.0 - 1e-9:
            raise RuntimeError(
                f"max_positions={len(inv)} with max_weight={self.cfg.max_weight:.0%} "
                f"cannot both hold: {len(inv)} names cannot each stay under the cap "
                f"and still sum to 1. Raise max_positions to at least "
                f"{math.ceil(1.0 / self.cfg.max_weight)} or loosen the cap."
            )
        for _ in range(8):
            if float(inv.max()) <= self.cfg.max_weight + 1e-12:
                break
            inv = inv.clip(upper=self.cfg.max_weight)
            inv = inv / inv.sum()

        # Scale toward the volatility target; the leftover goes to the bond ETF.
        port_vol = float((inv * vol.reindex(inv.index)).sum())
        scale = min(1.0, self.cfg.vol_target / port_vol) if port_vol > 0 else 1.0
        weights = {code: float(w * scale) for code, w in inv.items()}
        weights[DEFENSIVE_ETF] = weights.get(DEFENSIVE_ETF, 0.0) + (1.0 - scale)
        detail.update({"port_vol": round(port_vol, 4), "scale": round(scale, 3),
                       "held": len(inv), "dropped_for_breadth": dropped,
                       "buckets": sorted({UNIVERSE_CFG.bucket(c) for c in inv.index})})
        weights["_meta"] = detail
        return weights

    # ------------------------------------------------------------------ book
    def _holdings(self) -> dict[str, int]:
        """Aggregate every trade; a recent-row window is not an account state."""
        if hasattr(self.paper, "_conn"):
            with self.paper._conn() as conn:
                rows = conn.execute(
                    "SELECT symbol, side, SUM(qty) AS qty FROM core_trade "
                    "GROUP BY symbol, side").fetchall()
        else:
            # Lightweight stores do not expose SQL. Their API is newest-first,
            # so request the full signed 64-bit SQLite range rather than an
            # arbitrary recent window; aggregation below is order-independent.
            rows = self.paper.core_trades(limit=2**63 - 1)
        holdings: dict[str, int] = {}
        for trade in rows:
            side = str(trade["side"])
            if side not in {"BUY", "SELL"}:
                raise RuntimeError(f"unknown core trade side: {side}")
            sign = 1 if side == "BUY" else -1
            symbol = str(trade["symbol"])
            holdings[symbol] = holdings.get(symbol, 0) + sign * int(trade["qty"])
        return {symbol: qty for symbol, qty in holdings.items() if qty > 0}

    def state(self) -> CoreState:
        """Rebuild the book from the complete trade ledger, not cached memory."""
        hist = self.paper.core_nav_history(limit=1)
        holdings = self._holdings()
        cash = float(hist[-1]["cash"]) if hist else self._initial_cash()
        last = hist[-1]["trade_date"] if hist else ""
        # Regime answers "does the book hold a risk leg", and that question is
        # about the ledger, not about a display dictionary. Reading it from the
        # stored NAV weights made the answer depend on arithmetic that had
        # nothing to do with it: those weights are rounded to four decimals and
        # filtered to ``> 0`` before being written, so a dust position vanished
        # from the dict while still being held — and with no NAV row at all
        # (first session after a rebalance) the dict is empty and *every* book
        # read as defensive. ``should_rebalance`` already asks the ledger
        # (``any(k in st.holdings ...)``); this is the same question.
        regime = "risk" if any(k in holdings for k in RISK_ETFS) else "defensive"
        return CoreState(cash=cash, holdings=holdings, last_rebalance=last, last_regime=regime)

    def _initial_cash(self) -> float:
        # Only used when there is no NAV row yet. Same source as every other
        # account size: config, with ``QUANT__RISK__INITIAL_CASH`` as the
        # supported temporary override. The old body also consulted
        # ``INITIAL_CASH`` from the process env, which beat the config.
        return float(settings.initial_cash())

    def _state_version(self, state: CoreState) -> str:
        latest = self.paper.core_trades(limit=1)
        payload = {
            "cash": f"{state.cash:.2f}",
            "holdings": sorted(state.holdings.items()),
            "last_rebalance": state.last_rebalance,
            # Cash/holdings alone can return to the same values after a same-day
            # round trip. The ledger sequence cannot, so it is part of the version.
            "last_trade": latest[0] if latest else None,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _content_hash(plan: dict) -> str:
        # Results can be passed back on a retry; transport/status fields are not
        # part of the accepted economic plan.
        transient = {"content_hash", "applied", "forced", "note", "recovered"}
        payload = {k: v for k, v in plan.items() if k not in transient}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    def mark(self, as_of: str, state: CoreState | None = None) -> dict:
        """Value the sleeve at one session's prices.

        A held ETF with no usable print on ``as_of`` is reported in
        ``missing_prices`` instead of contributing ``NaN``. It used to
        contribute ``float(px.get(sym, 0.0))`` — and the default only fires for
        a *missing column*, not for a present-but-NaN cell, which is what a
        suspended or delisted fund leaves behind after ``ffill(limit=5)``. The
        sum then became NaN, ``f"{nav:.2f}"`` wrote the literal string ``"nan"``
        into ``core_nav`` (an upsert, so it overwrote the day's good row), and
        every downstream number — since-inception return, max drawdown, the
        decay monitor — inherited it. ``max_dd = min(max_dd, nan)`` stays 0, so
        a blown-up curve reported no drawdown at all.
        """
        st = state or self.state()
        px = self.raw_prices().loc[:as_of].iloc[-1]
        invested = 0.0
        missing: list[str] = []
        for sym, qty in st.holdings.items():
            price = _finite_price(px.get(sym))
            if price is None:
                missing.append(str(sym))
                continue
            invested += qty * price
        nav = st.cash + invested
        out = {"trade_date": as_of, "nav": round(nav, 2), "cash": round(st.cash, 2),
               "invested": round(invested, 2), "holdings": dict(st.holdings)}
        if missing:
            # Named, not counted: "1 position unpriceable" tells the reader
            # nothing, and the NAV above is now an understatement rather than a
            # completed valuation.
            out["missing_prices"] = sorted(missing)
        return out

    def record_nav(self, as_of: str) -> dict:
        """Write one NAV row per trading day, including days spent entirely in cash.

        Until P26 a row was written only when ``execute()`` ran, so a book that
        correctly stayed in cash produced no record at all — and ``/decay`` then
        reported ``insufficient`` forever for the only strategy that carries
        out-of-sample evidence. Absence of trades is a position; it still needs a
        price. The alternative is a strategy whose performance curve exists only
        on days it happened to trade, which is a selection effect, not a record.

        Two guards, both about not writing a plausible lie:

        * no bar for that session → nothing is written (a carried-forward price
          would silently duplicate the previous day's NAV);
        * ``as_of`` earlier than the last stored row → refused, because
          ``state()`` reflects today's holdings and back-filling would value
          yesterday with today's book.
        """
        px = self.raw_prices()
        if as_of not in {str(i) for i in px.index}:
            return {"trade_date": as_of, "recorded": False,
                    "reason": "no ETF bar for this session"}

        hist = self.paper.core_nav_history(limit=1)
        if hist and as_of < str(hist[-1]["trade_date"]):
            return {"trade_date": as_of, "recorded": False,
                    "reason": (f"refusing to rewrite history before "
                               f"{hist[-1]['trade_date']}: state() is today's book")}

        st = self.state()
        marked = self.mark(as_of, st)
        if marked.get("missing_prices"):
            # Same rule as "no bar for this session": a NAV that omits part of
            # the book is a plausible lie, and this one is written with an
            # upsert that would replace the day's correct row.
            return {"trade_date": as_of, "recorded": False,
                    "reason": (f"no usable ETF price for "
                               f"{', '.join(marked['missing_prices'])}")}
        nav = float(marked["nav"])
        last = px.loc[as_of]
        weights = {sym: round(q * (_finite_price(last.get(sym)) or 0.0) / nav, 4)
                   for sym, q in st.holdings.items()} if nav > 0 else {}
        weights = {k: v for k, v in weights.items() if v > 0}
        self.paper.core_record_nav(as_of, f"{nav:.2f}", f"{marked['cash']:.2f}",
                                   f"{marked['invested']:.2f}", weights)
        return {"trade_date": as_of, "recorded": True, "nav": marked["nav"],
                "cash": marked["cash"], "invested": marked["invested"],
                "weights": weights, "positions": len(st.holdings)}

    # ------------------------------------------------------------- rebalance
    def _fee(self, notional: float) -> float:
        return max(self.cfg.fee_min, notional * self.cfg.fee_rate)

    def _validate_targets(self, targets: dict) -> dict[str, float]:
        """Check a caller's allocation, without second-guessing it.

        The kernel's role here is arithmetic and legality, not opinion: it does
        not argue with a weight, only with the things that would make the weight
        meaningless - an unknown code, a negative size, a book that adds up to
        more than the account.
        """
        if not isinstance(targets, dict) or not targets:
            raise ValueError("targets must be a non-empty mapping of code -> weight")
        clean: dict[str, float] = {}
        unknown: list[str] = []
        for code, weight in targets.items():
            key = str(code).strip().upper()
            if key not in UNIVERSE:
                unknown.append(key)
                continue
            try:
                value = float(weight)
            except (TypeError, ValueError):
                raise ValueError(f"weight for {key} is not a number: {weight!r}") from None
            if not math.isfinite(value) or value < 0 or value > 1:
                raise ValueError(f"weight for {key} must be between 0 and 1, got {value}")
            if value > 0:
                clean[key] = value
        if unknown:
            raise ValueError(f"not in the core universe: {sorted(unknown)}. "
                             f"Allowed: {sorted(UNIVERSE)}")
        total = sum(clean.values())
        if total > 1.0 + 1e-6:
            raise ValueError(
                f"targets sum to {total:.4f}; whatever is not allocated stays in "
                f"cash, so the sum must be <= 1"
            )
        return clean

    def plan(self, as_of: str, *, plan_id: str | None = None,
             targets: dict[str, float] | None = None) -> dict:
        """Turn a set of target weights into an order list. Pure: no money moves.

        ``targets`` is the caller's own allocation. When it is omitted the
        registered rule's weights are used instead - but that path exists to
        *show* what the rule would do, not to execute it: ``plan`` never books
        anything, and ``/core/execute`` refuses to run without explicit targets.

        Before this, ``/core/execute`` silently executed ``CoreBook.plan()``,
        i.e. the kernel wrote the order list and the model signed it. That is a
        code-authored trade with a human-shaped signature on it.
        """
        st = self.state()
        px = self.raw_prices().loc[:as_of].iloc[-1]
        if targets is None:
            weights = self.target_weights(as_of)
            meta = weights.pop("_meta", {})
            meta["source"] = "registered rule (reference only, not executed)"
        else:
            weights = self._validate_targets(targets)
            meta = {"source": "caller-supplied targets",
                    "cash_target": round(1.0 - sum(weights.values()), 4)}
        nav = st.cash + sum(q * _px(px, s) for s, q in st.holdings.items())

        # A 100-share lot of 511010 is ~CNY 14,700 and a lot of 513100 ~CNY 1,120,
        # so a CNY 5,000 book cannot hold every sleeve. What it cannot buy stays
        # in cash: dropping a sleeve must never concentrate the rest.
        affordable = {}
        skipped = {}
        for symbol, weight in weights.items():
            price = round(_px(px, symbol), 4)
            if weight <= 0:
                continue
            if price > 0 and price * self.cfg.lot <= nav * 0.5:
                affordable[symbol] = weight
            else:
                skipped[symbol] = round(weight, 3)

        orders = []
        gaps: list[dict] = []
        cash = st.cash
        holdings = dict(st.holdings)
        # Sell first so the cash is available for the buys.
        for symbol, qty in sorted(holdings.items()):
            price = round(_px(px, symbol), 4)
            target = float(weights.get(symbol, 0.0)) * nav
            current = qty * price
            if price <= 0 or current - target < self.cfg.min_trade_cny:
                continue
            lots = int((current - target) // (price * self.cfg.lot))
            lots = min(lots, qty // self.cfg.lot)
            if lots <= 0:
                # A holding of 150 shares from a bonus issue cannot be reduced by
                # whole lots below 100, and 50 shares rarely clear
                # `min_trade_cny`, so the remainder sat in the book for ever.
                # A-share rules allow an odd lot to be sold in one go when it
                # closes the position, which is exactly this case.
                if target <= 0 and 0 < qty < self.cfg.lot and current >= self.cfg.min_trade_cny:
                    lots = 0
                    qty_sell = qty
                else:
                    continue
            else:
                qty_sell = lots * self.cfg.lot
            notional = qty_sell * price
            fee = round(self._fee(notional), 2)
            cash += notional - fee
            holdings[symbol] = qty - qty_sell
            orders.append({"symbol": symbol, "side": "SELL", "qty": qty_sell,
                           "price": round(price, 4), "fee": round(fee, 2)})

        for symbol, weight in sorted(affordable.items()):
            if weight <= 0:
                continue
            price = round(_px(px, symbol), 4)
            if price <= 0:
                continue
            target = weight * nav
            current = holdings.get(symbol, 0) * price
            if target - current < self.cfg.min_trade_cny:
                continue
            lots = int((target - current) // (price * self.cfg.lot))
            if lots <= 0:
                # The weight was real and the name was selected, but its target
                # is smaller than one lot, so it buys zero shares. Saying nothing
                # here is how a 55% target silently becomes a 22% book.
                gaps.append({"symbol": symbol, "weight": round(weight, 4),
                             "target_cny": round(target, 2),
                             "lot_cny": round(price * self.cfg.lot, 2),
                             "reason": "target below one lot"})
                continue
            cost = lots * self.cfg.lot * price
            fee = round(self._fee(cost), 2)
            if cost + fee > cash:
                # Start from the cash-only size and walk down until the *actual*
                # fee schedule fits. The old formula divided by
                # ``1 + fee_rate`` — which ignores the 5-yuan minimum commission
                # that dominates on this account — and then subtracted a whole
                # lot for safety, so every cash-constrained buy was one lot short
                # and `rounding_gaps` could not report it.
                lots = int(cash / (price * self.cfg.lot))
                while lots > 0:
                    cost = lots * self.cfg.lot * price
                    fee = round(self._fee(cost), 2)
                    if cost + fee <= cash:
                        break
                    lots -= 1
                if lots <= 0:
                    continue
            cash -= cost + fee
            holdings[symbol] = holdings.get(symbol, 0) + lots * self.cfg.lot
            orders.append({"symbol": symbol, "side": "BUY", "qty": lots * self.cfg.lot,
                           "price": round(price, 4), "fee": round(fee, 2)})

        holdings = {k: v for k, v in holdings.items() if v > 0}
        invested = sum(q * round(_px(px, s), 4)
                       for s, q in holdings.items())
        final_nav = cash + invested
        # Record achieved exposure, not the target. A sleeve can be "affordable"
        # and still buy zero lots after rounding, and the shadow record must not
        # claim exposure the book never held.
        achieved = {s: round(q * round(_px(px, s), 4) / final_nav, 4)
                    for s, q in holdings.items() if q > 0 and final_nav > 0}
        result = {
            "plan_id": plan_id or f"core-{as_of}",
            "trade_date": as_of,
            "nav": round(final_nav, 2),
            "cash": round(cash, 2),
            "invested": round(invested, 2),
            "weights": achieved,
            "targets": {k: round(v, 4) for k, v in weights.items()},
            "cash_weight": round(cash / final_nav, 3) if final_nav else 1.0,
            "skipped_unaffordable": skipped,
            "rounding_gaps": gaps,
            "effective_positions": len([q for q in holdings.values() if q > 0]),
            "orders": orders,
            "regime": meta,
            "state_version": self._state_version(st),
        }
        result["content_hash"] = self._content_hash(result)
        return result

    def execute(self, plan: dict, *, force: bool = False) -> dict:
        """Atomically book one caller-owned plan, idempotently by content.

        A successful execution writes a receipt in the same transaction as all
        trades and NAV. A retry therefore compares the whole plan rather than
        guessing from the presence of one trade. ``force`` has exactly one safe
        meaning: resume a legacy partial write whose stored trades are an exact
        prefix of this plan. It never overrides a hash, stale-state or duplicate
        execution guard.
        """
        source = str((plan.get("regime") or {}).get("source") or "")
        if source.startswith("registered rule"):
            raise RuntimeError(
                "this plan came from the registered rule; the kernel does not "
                "execute its own output. Re-plan with plan(as_of, targets=...) "
                "and supply the weights yourself."
            )
        if not isinstance(plan.get("orders"), list):
            raise TypeError("plan orders must be a list")
        day = str(plan.get("trade_date") or "")
        plan_id = str(plan.get("plan_id") or "")
        if not day or not plan_id.strip():
            raise ValueError("plan needs a trade_date and a non-empty plan_id")
        try:
            digest = self._content_hash(plan)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"plan is not canonical JSON: {exc}") from exc
        supplied = str(plan.get("content_hash") or "")
        if not supplied or not hmac.compare_digest(supplied, digest):
            raise ValueError("plan content hash is missing or does not match; re-plan")

        with self._execution_lock:
            if hasattr(self.paper, "_conn") and hasattr(self.paper, "_lock"):
                # BEGIN IMMEDIATE serialises the receipt check and writes across
                # processes, not just threads sharing this CoreBook instance.
                with self.paper._lock, self.paper._conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    # The write lock makes the check-and-write race impossible;
                    # this partial unique index also makes one execution per plan
                    # a database invariant rather than merely a locking convention.
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_core_plan_executed_unique "
                        "ON decisions(ticket_id) WHERE kind='core_plan_executed'")
                    return self._execute_locked(plan, force=force, conn=conn)

            # The production store is SQLite above. Lightweight test stores need
            # a shared lock too: two CoreBook instances must not both pass the
            # receipt check before either one records its result.
            with _MEMORY_EXECUTION_LOCK:
                snapshot = copy.deepcopy(vars(self.paper))
                try:
                    return self._execute_locked(plan, force=force, conn=None)
                except Exception:
                    self.paper.__dict__.clear()
                    self.paper.__dict__.update(snapshot)
                    raise

    def _execute_locked(self, plan: dict, *, force: bool, conn) -> dict:
        plan_id = str(plan["plan_id"])
        digest = self._content_hash(plan)
        receipt = self._execution_receipt(conn, plan_id)
        if receipt is not None:
            if not hmac.compare_digest(str(receipt.get("content_hash") or ""), digest):
                raise ValueError(f"plan_id {plan_id} was already used for different content")
            return {**plan, "applied": False, "forced": force, "recovered": False,
                    "note": f"{plan_id} 已经完整入账，重复执行被忽略"}

        existing = self._execution_trades(conn, plan_id)
        if existing:
            return self._recover_legacy(plan, existing, force=force, conn=conn)

        self._validate_current_plan(plan)
        self._write_execution(plan, conn=conn)
        return {**plan, "applied": True, "forced": force, "recovered": False}

    def _validate_current_plan(self, plan: dict) -> None:
        """Reject stale or illegal orders before the first ledger write."""
        state = self.state()
        version = self._state_version(state)
        supplied = str(plan.get("state_version") or "")
        if not supplied or not hmac.compare_digest(supplied, version):
            raise RuntimeError("stale plan: the core book changed; re-plan before executing")
        targets = plan.get("targets")
        if not isinstance(targets, dict):
            raise TypeError("plan targets must be a mapping")
        if targets:  # an all-zero caller allocation legitimately cleans to {}
            self._validate_targets(targets)

        day = str(plan["trade_date"])
        frame = self.raw_prices().loc[:day]
        if frame.empty:
            raise ValueError(f"no raw ETF prices up to {day}")
        px = frame.iloc[-1]
        cash = float(state.cash)
        holdings = dict(state.holdings)
        seen: set[str] = set()
        for order in plan["orders"]:
            if not isinstance(order, dict):
                raise TypeError("every order must be a mapping")
            symbol = str(order.get("symbol") or "")
            side = str(order.get("side") or "")
            if symbol not in UNIVERSE or symbol in seen:
                raise ValueError(f"invalid or duplicate core order symbol: {symbol}")
            seen.add(symbol)
            if side not in {"BUY", "SELL"}:
                raise ValueError(f"invalid side for {symbol}: {side}")
            try:
                qty = int(order["qty"])
                price = float(order["price"])
                fee = float(order["fee"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"malformed order for {symbol}: {exc}") from exc
            # An odd lot is legal only when it closes the position outright, which
            # is the one case `plan()` now produces (a bonus-issue remainder).
            odd_close = (side == "SELL" and qty % self.cfg.lot
                         and qty == int(holdings.get(symbol, 0)))
            if (qty <= 0 or (qty % self.cfg.lot and not odd_close)
                    or D(str(order["qty"])) != qty
                    or not math.isfinite(price) or not math.isfinite(fee) or fee < 0):
                raise ValueError(f"invalid quantity, price or fee for {symbol}")
            expected_price = round(_px(px, symbol), 4)
            if expected_price <= 0 or not math.isclose(price, expected_price, abs_tol=1e-9):
                raise ValueError(f"order price for {symbol} is not the raw close; re-plan")
            expected_fee = round(self._fee(qty * expected_price), 2)
            if not math.isclose(fee, expected_fee, abs_tol=1e-9):
                raise ValueError(f"order fee for {symbol} does not match the core cost model")
            notional = qty * price
            if side == "SELL":
                if qty > holdings.get(symbol, 0):
                    raise ValueError(f"sell exceeds current holdings for {symbol}")
                holdings[symbol] -= qty
                cash += notional - fee
            else:
                cash -= notional + fee
                if cash < -1e-6:
                    raise ValueError("plan spends more cash than the current book holds")
                holdings[symbol] = holdings.get(symbol, 0) + qty

        holdings = {k: v for k, v in holdings.items() if v > 0}
        invested = sum(q * round(_px(px, s), 4) for s, q in holdings.items())
        nav = cash + invested
        for key, expected in (("cash", cash), ("invested", invested), ("nav", nav)):
            try:
                actual = float(plan[key])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"plan has no valid {key}: {exc}") from exc
            if not math.isclose(actual, round(expected, 2), abs_tol=0.001):
                raise ValueError(f"plan {key} does not follow from its orders and current state")
        expected_weights = {s: round(q * round(_px(px, s), 4) / nav, 4)
                            for s, q in holdings.items() if nav > 0}
        weights = plan.get("weights") or {}
        if set(weights) != set(expected_weights) or any(
                not math.isclose(float(weights[s]), expected_weights[s], abs_tol=1e-9)
                for s in expected_weights):
            raise ValueError("plan weights do not match its post-trade holdings")

    @staticmethod
    def _same_order(stored: dict, expected: dict) -> bool:
        try:
            return (str(stored["symbol"]) == str(expected["symbol"])
                    and str(stored["side"]) == str(expected["side"])
                    and int(stored["qty"]) == int(expected["qty"])
                    and D(str(stored["price"])) == D(str(expected["price"]))
                    and D(str(stored["fee"])) == D(str(expected["fee"])))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return False

    @staticmethod
    def _nav_matches(stored: dict, plan: dict) -> bool:
        try:
            return all(math.isclose(float(stored[k]), float(plan[k]), abs_tol=0.001)
                       for k in ("nav", "cash", "invested")) and stored.get("weights") == plan.get("weights", {})
        except (KeyError, TypeError, ValueError):
            return False

    def _recover_legacy(self, plan: dict, existing: list[dict], *,
                        force: bool, conn) -> dict:
        """Adopt a complete old write or resume an exact partial prefix."""
        plan_id = str(plan["plan_id"])
        orders = plan["orders"]
        if (len(existing) > len(orders)
                or any(not self._same_order(old, new) for old, new in zip(existing, orders))):
            raise RuntimeError(f"plan_id {plan_id} conflicts with existing core trades")
        complete = len(existing) == len(orders)
        nav = self._execution_nav(conn, str(plan["trade_date"]))
        if complete and nav is not None:
            if not self._nav_matches(nav, plan):
                raise RuntimeError(f"plan_id {plan_id} conflicts with its stored NAV")
            self._write_execution_receipt(plan, conn)
            return {**plan, "applied": False, "forced": force, "recovered": True,
                    "note": f"{plan_id} 的旧账本已核对完整，补记执行回执"}
        if not force:
            raise RuntimeError(
                f"plan_id {plan_id} is an incomplete legacy write; verify the exact "
                "prefix and retry with force=True to resume without duplicates"
            )
        self._write_execution(plan, conn=conn, start=len(existing))
        return {**plan, "applied": True, "forced": True, "recovered": True}

    def _execution_receipt(self, conn, plan_id: str) -> dict | None:
        if conn is None:
            return vars(self.paper).setdefault("_core_receipts", {}).get(plan_id)
        row = conn.execute(
            "SELECT payload FROM decisions WHERE kind='core_plan_executed' "
            "AND ticket_id=? ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def _execution_trades(self, conn, plan_id: str) -> list[dict]:
        if conn is None:
            return [t for t in reversed(self.paper.core_trades(limit=2**63 - 1))
                    if str(t.get("reason") or "") == plan_id]
        rows = conn.execute(
            "SELECT * FROM core_trade WHERE reason=? ORDER BY id", (plan_id,)).fetchall()
        return [dict(r) for r in rows]

    def _execution_nav(self, conn, day: str) -> dict | None:
        if conn is None:
            rows = [r for r in self.paper.core_nav_history(limit=2**63 - 1)
                    if str(r["trade_date"]) == day]
            return rows[-1] if rows else None
        row = conn.execute("SELECT * FROM core_nav WHERE trade_date=?", (day,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["weights"] = json.loads(item.get("weights") or "{}")
        return item

    def _write_execution(self, plan: dict, conn, start: int = 0) -> None:
        day = str(plan["trade_date"])
        plan_id = str(plan["plan_id"])
        for order in plan["orders"][start:]:
            if conn is None:
                self.paper.core_record_trade(day, order["symbol"], order["side"],
                                             order["qty"], f"{order['price']}",
                                             f"{order['fee']}", plan_id)
            else:
                conn.execute(
                    "INSERT INTO core_trade(trade_date, symbol, side, qty, price, fee, reason) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (day, order["symbol"], order["side"], int(order["qty"]),
                     str(order["price"]), str(order["fee"]), plan_id))
        invested = float(plan["invested"])
        if conn is None:
            self.paper.core_record_nav(day, f"{plan['nav']:.2f}", f"{plan['cash']:.2f}",
                                       f"{invested:.2f}", plan.get("weights") or {})
        else:
            conn.execute(
                "INSERT INTO core_nav(trade_date, nav, cash, invested, weights) "
                "VALUES (?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
                "nav=excluded.nav, cash=excluded.cash, invested=excluded.invested, "
                "weights=excluded.weights",
                (day, f"{plan['nav']:.2f}", f"{plan['cash']:.2f}", f"{invested:.2f}",
                 json.dumps(plan.get("weights") or {}, ensure_ascii=False, sort_keys=True)))
        self._write_execution_receipt(plan, conn)

    def _write_execution_receipt(self, plan: dict, conn) -> None:
        plan_id = str(plan["plan_id"])
        receipt = {"plan_id": plan_id, "trade_date": str(plan["trade_date"]),
                   "content_hash": self._content_hash(plan),
                   "state_version": plan["state_version"], "orders": len(plan["orders"])}
        if conn is None:
            vars(self.paper).setdefault("_core_receipts", {})[plan_id] = receipt
            return
        conn.execute(
            "INSERT INTO decisions(ts, kind, ticket_id, actor, payload) VALUES (?,?,?,?,?)",
            (utc_now(), "core_plan_executed", plan_id, "system",
             json.dumps(receipt, ensure_ascii=False, sort_keys=True)))

    @property
    def retired(self) -> bool:
        """Closed for business: no NAV rows, no orders, still readable.

        Read from the universe config on every access rather than cached at
        import, so flipping `configs/core.json` takes effect on the next request
        instead of the next process.
        """
        return bool(UNIVERSE_CFG.retired)

    def should_rebalance(self, as_of: str) -> tuple[bool, str]:
        st = self.state()
        weights = self.target_weights(as_of)
        weights.pop("_meta", None)
        target_risk = any(k in weights for k in RISK_ETFS)
        current_risk = any(k in st.holdings for k in RISK_ETFS)
        if not st.holdings and not target_risk:
            return False, "cash (defensive, bond lot unaffordable)"
        if not st.holdings:
            return True, "empty book"
        if target_risk != current_risk:
            return True, "regime flip"
        prev = self.paper.core_nav_history(limit=1)
        if not prev:
            return True, "no NAV yet"
        last = prev[-1]["trade_date"]
        if last[:7] != as_of[:7]:
            return True, "new month"
        return False, "hold"

    def status(self, as_of: str | None = None) -> dict:
        day = as_of or self.last_day()
        marked = self.mark(day)
        hist = self.paper.core_nav_history(limit=400)
        start = float(hist[0]["nav"]) if hist else marked["nav"]
        peak = 0.0
        max_dd = 0.0
        for row in hist:
            value = float(row["nav"])
            peak = max(peak, value)
            if peak:
                max_dd = min(max_dd, value / peak - 1)
        weights = self.target_weights(day)
        meta = weights.pop("_meta", {})
        px = self.raw_prices().loc[:day].iloc[-1]
        unaffordable = {}
        executable = {}
        for symbol, weight in weights.items():
            price = round(_px(px, symbol), 4)
            if price > 0 and price * self.cfg.lot > marked["nav"] * 0.5:
                unaffordable[symbol] = round(weight, 3)
            if price > 0:
                lot_cost = price * self.cfg.lot
                # The two numbers that turn "unaffordable" from a flag into a
                # plan: what one lot costs today, and how big the book has to be
                # before this target can be held at all. For 511010 at 100% on
                # 2026-10-30 that is 14,112 against a 5,007 account — 2.8x — which
                # is why the sleeve has been 100% cash for six sessions and will
                # stay there. A cash book that cannot move is not a strategy
                # opinion, and reporting it as a flat 0.00% says otherwise.
                executable[symbol] = {
                    "price": price,
                    "lot_cost": round(lot_cost, 2),
                    "share_of_nav": round(lot_cost / marked["nav"], 3) if marked["nav"] else None,
                    "required_nav": round(lot_cost / weight, 2) if weight else None,
                }
        blocked = sorted(s for s, row in executable.items()
                         if (row["share_of_nav"] or 0) > 1.0 and row["required_nav"])
        if self.retired:
            # Reads before `holdings`, because a retired book has none and the
            # honest answer is "closed", not "waiting for a signal".
            state = "retired"
            why = ("该袖已于 2026-11-01 关闭：防守腿一手价格超过账户规模，"
                   "每个防守日只会写一条 100% 现金的净值行。要恢复，把 "
                   "configs/core.json 的 retired 改为 false")
        elif marked["holdings"]:
            state, why = "invested", None
        elif blocked:
            state = "cash_unexecutable"
            why = ("；".join(
                f"{s} 一手 {executable[s]['lot_cost']:,.0f} 元 = 权益的 "
                f"{executable[s]['share_of_nav']:.1f} 倍，需要净值 "
                f"{executable[s]['required_nav']:,.0f} 元才买得起"
                for s in blocked) + "。这不是空仓判断，是账户规模买不起规则指定的标的")
        else:
            state, why = "cash_by_rule", "规则判定防守，且标的可买；本日无仓位是规则输出"
        return {
            "as_of": day,
            "nav": marked["nav"],
            "cash": marked["cash"],
            "cash_weight": round(marked["cash"] / marked["nav"], 3) if marked["nav"] else 1.0,
            "invested": marked["invested"],
            "since_inception": round(marked["nav"] / start - 1, 4) if start else 0.0,
            "max_drawdown": round(max_dd, 4),
            "days": len(hist),
            "holdings": marked["holdings"],
            "target_weights": {k: round(v, 3) for k, v in weights.items()},
            "unaffordable": unaffordable,
            "executable": executable,
            "state": state,
            "why": why,
            "regime": meta,
            "last_rebalance": hist[-1]["trade_date"] if hist else None,
        }


def describe(weights: dict) -> str:
    """One-line rationale, used in the daily log so a human can audit the book."""
    meta = weights.get("_meta", {})
    parts = [f"{k} {v:.0%}" for k, v in sorted(weights.items())
             if not k.startswith("_") and v > 0.01]
    return (f"trend_ok={meta.get('trend_ok')} port_vol={meta.get('port_vol')} "
            f"scale={meta.get('scale')} | " + ", ".join(parts))
