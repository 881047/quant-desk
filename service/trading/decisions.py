"""Decision protocol: the model proposes, Python computes, the guard decides.

A proposal carries *intent* (symbol, side, thesis, evidence, invalidation) and
at most a **size intent**: a ``qty`` in board lots or a ``target_weight``, never
both. The price is not an input at all — the limit, the stop and the fees are
computed here from stored data, then re-checked against the guard. A supplied
quantity is not obeyed either: it must clear the lot table, the single-position
cap and the per-ticket risk budget, and the error names the largest legal
number instead of silently resizing.

Approval is idempotent: the same ticket can be approved twice and only one fill
exists. A ticket from a previous session is expired, never silently executed.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from service import settings
from service.adapters.catalog import SPECS
from service.adapters.lineage import collect as collect_lineage
from service.kernel.clock import SHANGHAI
from service.kernel.models import D, Session, integral_qty, money

from .book import PaperBook
from .guard import Guard
from .impact import round_trip_cost_bps
from .market import MarketRepository, Snapshot
from .state import PaperStore, utc_now

TICK = D("0.01")
# With 5,000 CNY and a 100-share lot, a 20% target (1,000 CNY) can only buy
# shares below 10. 30% is the smallest default that keeps a usable universe.
DEFAULT_TARGET = {"CORE": 0.30, "AGGRESSIVE": 0.30}


class DecisionError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _refuse_open_buys(market, book, day: str) -> None:
    """The one ``OPEN_VIOLATION`` gate: one predicate, one refusal shape.

    ``propose`` and its idempotent-retry replay both call this, so a retry can
    never answer from a stale ticket while a holding is through its stop. The
    predicate itself lives in ``observation.evaluate_stop_gate`` — the shim the
    scan needs is built once there, not once per caller. ``unknown`` and
    ``breached`` are both refused: "cannot answer" is not "no breach".
    """
    from .observation import evaluate_stop_gate

    gate = evaluate_stop_gate(market, book, day)
    if gate["unknown"]:
        listed = ", ".join(gate["unknown"][:4])
        raise DecisionError(
            "OPEN_VIOLATION",
            f"{listed} 的止损状态无法取得同时点有效价格；未知不等于无违约，"
            "在恢复有效报价前不加仓，卖出不受此限。",
            409,
        )
    breached = gate["breached"]
    if breached:
        listed = ", ".join(sorted(set(breached))[:4])
        raise DecisionError(
            "OPEN_VIOLATION",
            f"{listed} 仍在各自止损之下，风险预算处在违约状态。"
            f"未平掉或不打算处理之前不加仓；卖出不受此限。"
            f"状态在 /observations 里，不需要回应任何'建议'。",
            409,
        )


def _nearest_lot(qty: int, inst=None) -> int:
    """Largest legal quantity not above ``qty`` — for a message that can be acted on."""
    minimum = max(1, int(getattr(inst, "min_qty", 100) or 100))
    step = max(1, int(getattr(inst, "qty_step", 100) or 100))
    if qty < minimum:
        return minimum
    return minimum + ((qty - minimum) // step) * step


def _floor_shares(shares: float, inst=None) -> int:
    """Largest legal quantity not above ``shares``."""
    minimum = max(1, int(getattr(inst, "min_qty", 100) or 100))
    step = max(1, int(getattr(inst, "qty_step", 100) or 100))
    n = math.floor(shares)
    if n < minimum:
        return 0
    return minimum + ((n - minimum) // step) * step


def _floor_lot(value: float, price: Decimal, inst=None) -> int:
    """Largest legal quantity whose notional fits ``value``.

    Board lots are not uniform (STAR: 200 up, 1 up; BSE: 100 up, 1 up), so the
    instrument supplies the floor and the step instead of a hardcoded 100.
    """
    if price <= 0 or value <= 0:
        return 0
    return _floor_shares(float(value) / float(price), inst)


def _cannot_buy_one_lot(inst, price: Decimal, budget: float,
                        label: str = "单票额度") -> str:
    """``0 股`` 不是"不许买"，是"一手就超了"——说清是哪一手、超了哪条线。

    2026-11-02 实测：外行看到 6 连板想买一手 001219.SZ（21.93 元），拿到的是
    "这只票最多 0 股"。读起来像系统坏了，实际是一手 2,193 元已经超过 1,502 元的
    单票额度。同一个 "0" 有两种完全不同的意思，不写出来就是把人留在原地。
    """
    lot = float(getattr(inst, "min_qty", 100) or 100) * float(price)
    return (f"一手 {getattr(inst, 'min_qty', 100)} 股 = {lot:,.0f} 元，已经超过"
            f"{label} {budget:,.0f} 元，整手买不进——换一只便宜些的票，"
            "或者换一个额度更高的 sleeve")


class DecisionService:
    def __init__(self, book: PaperBook, market: MarketRepository,
                 store: PaperStore, registry, guard: Guard | None = None, now=None):
        self.book = book
        self.market = market
        # Injectable clock: tests must be able to pin "today" to the last bar,
        # otherwise every proposal fails on data that simply has not published yet.
        self._now = now or (lambda: datetime.now(SHANGHAI))
        self.store = store
        self.registry = registry
        self.guard = guard or Guard(market, registry)
        self.lock = threading.RLock()

    # --------------------------------------------------------------- helpers
    def _date(self, symbol: str | None = None) -> str:
        """The date a decision may be priced at, not merely today's date.

        Three cases, and the difference between the last two is the whole point:

        * today's bar exists **in this instrument's own feed** - the normal
          after-close path;
        * it does not, but a **real-time quote** does, so today is tradable at a
          price that can actually be seen (``_snapshot`` proves it);
        * neither - refuse. Filling at a bar that predates the decision would put
          a look-ahead into the record used to judge the model.

        ``symbol`` is what makes both the first and the second case decidable: a
        bar and a quote are per instrument, so "the market is open" is not
        enough, we need a price.
        """
        today = self._now().strftime("%Y%m%d")
        calendar = self.market.calendar
        # Coverage first: ``is_trading_day`` answers an out-of-range date with
        # the weekday rule, while the kernel's frozen calendar answers CLOSED.
        # A ticket created on the strength of the weekday rule is one the
        # settlement pass can only cancel with MARKET_CLOSED — the money
        # decision is lost after it was reported as accepted.
        covers = getattr(calendar, "covers", None)
        if callable(covers) and not covers(today):
            days = list(getattr(calendar, "days", []) or [])
            first, last = (days[0], days[-1]) if days else ("?", "?")
            raise DecisionError(
                "CALENDAR_NOT_COVERED",
                f"交易日历只覆盖 {first}..{last}，没有 {today}。"
                f"先跑日更补齐 trade_cal 再提案；否则这张票只会在结算时被 "
                f"MARKET_CLOSED 取消。",
                503,
            )
        if not calendar.is_trading_day(today):
            raise DecisionError("MARKET_CLOSED", f"{today} is not a trading day", 409)
        # Which feed can price *this* instrument, not "the market" in general.
        # The four bar datasets are ingested on separate schedules: on the
        # evening the stock partition lands, `daily` is at today while
        # `cb_daily` still ends yesterday. Asking the stock calendar about a
        # bond made the refusal read "20261104 的日线还没发布（最新 20261105）"
        # — a sentence that cannot be true, and that hid which file was short.
        dataset = self.market.quote_dataset(symbol) if symbol else "daily"
        bar = self.market.dataset_cover_end(dataset)
        if not bar:
            raise DecisionError("NO_CALENDAR", f"no {dataset} bars at all", 503)
        if bar >= today:
            # `>` means the feed runs ahead of the clock (an NTP correction, or
            # a partition written with tomorrow's session). Today is still the
            # date a decision may be priced at; whether a price actually exists
            # is `_reference`'s question, not this one's.
            return today
        if symbol is not None and self._live_snapshot(symbol, today) is not None:
            return today
        raise DecisionError(
            "DATA_NOT_READY",
            f"{today} 的日线还没发布（{dataset} 最新 {bar}），也没有可用的实时报价；"
            f"不以过期收盘价下单，等数据任务或行情恢复",
            409,
        )

    def _live_snapshot(self, symbol: str, date: str):
        """Today's price from the quote service, shaped like a stored Snapshot.

        Same fields on purpose: the guard, the limit-price rule and the sizing
        code all take a snapshot, and none of them should care where it came
        from. What a reader must be able to see is ``source``, which reads
        ``live:ths`` here and ``daily`` for a stored bar.
        """
        from .live_fill import build as build_live

        if self.book.quotes is None:
            return None
        try:
            quote = self.book.quotes([symbol]).get(symbol)
        except (ValueError, KeyError, RuntimeError):
            return None
        live, _refusal = build_live(self.market, symbol, quote, now=self._now())
        if live is None:
            return None
        bar = live.bar
        return Snapshot(
            symbol=symbol, trade_date=date, last=bar.close, pre_close=bar.pre_close,
            open=bar.open, high=bar.high, low=bar.low, up_limit=bar.up_limit,
            down_limit=bar.down_limit, volume=float(bar.volume),
            amount=float(bar.amount), suspended=bar.suspended,
            source=f"live:{live.source}",
        )

    def _reference(self, symbol: str, date: str):
        """The best price available for this symbol on this date.

        A stored bar wins even on a live day: if today's bar exists the session
        is over, and the official close is better evidence than a quote that
        will be stale by the time anyone reads the record.
        """
        snap = self.market.snapshot(symbol, date)
        if snap is not None:
            return snap
        return self._live_snapshot(symbol, date)

    def _ticket_id(self, date: str) -> str:
        return f"td-{date}-{uuid.uuid4().hex[:8]}"

    # --------------------------------------------------------------- propose
    def _expected_ceiling_fill(self, symbol: str, day: str, side: str,
                               qty: int, inst) -> dict | None:
        """涨停价挂单大概能成交多少股。

        只在 ``fill_mode=limit_queue`` 时返回内容：别的模式下票据按全量冻结、
        按全量成交，``qty`` 就是结果。这里是**估算**，不是承诺——真正的成交
        由 ``book.settle_ceiling`` 在收盘后用当日封单结算。

        没有这个数，模型会按 ``qty`` 算仓位与风险，而实际只成交一部分，
        风险预算整体偏大一倍以上。返回 ``ratio=None`` 表示"大概率不成交"
        （没有封单数据、或全天没开板），不是"50% 概率"。
        """
        if getattr(self.book, "fill_mode", None) != "limit_queue" or side != "BUY":
            return None
        try:
            ratio = self.book.expected_ceiling_fill(symbol, day)
        except (AttributeError, KeyError, ValueError):
            ratio = None
        if ratio is None:
            return {"ratio": None, "qty": None,
                    "note": ("没有封单数据或全天没开板：这张单大概率一分不成交。"
                             "全天封死时是排队买不到，不是概率低。")}
        step = max(1, int(getattr(inst, "qty_step", 100) or 100))
        return {
            "ratio": round(float(ratio), 4),
            "qty": int(int(qty) * ratio) // step * step,
            "frozen_qty": int(qty),
            "note": ("按 qty 冻结现金，预期只成交这么多股；风险预算按成交后的数量读。"
                     "这个比例用**收盘**封单算，对开过板的票偏乐观——要不要打折、"
                     "打多少，是你的判断，内核不替你折。"),
        }

    def propose(self, req: dict[str, Any], actor: str = "agent") -> dict:
        with self.lock:
            symbol = str(req.get("symbol", "")).strip().upper()
            side = str(req.get("side", "BUY")).upper()
            sleeve = str(req.get("sleeve", "CORE")).upper()
            thesis = str(req.get("thesis", "")).strip()
            invalidation = str(req.get("invalidation", "")).strip()
            evidence = [str(e) for e in req.get("evidence", []) if str(e).strip()]
            request_id = str(req.get("client_request_id") or "").strip() or None
            if side not in ("BUY", "SELL"):
                raise DecisionError("BAD_SIDE", f"side must be BUY or SELL, got {side}")
            if sleeve not in ("CORE", "AGGRESSIVE"):
                raise DecisionError("BAD_SLEEVE", f"sleeve must be CORE or AGGRESSIVE, got {sleeve}")
            if req.get("qty") is not None and req.get("target_weight") is not None:
                raise DecisionError(
                    "BAD_SIZE",
                    "qty 与 target_weight 只能给一个：一个说买多少股，一个说占多少仓位；"
                    "同时给等于两个不同的下单意图。")

            # Idempotency first — before personas, pricing and the guard. A retry
            # arrives *after* the first call committed its cash commitment, so
            # re-running the guard would answer "insufficient cash" to a request
            # that actually succeeded, and the model would report a rejection for
            # a ticket that exists. The handle is the only thing that can tell a
            # retry from a second intent when the prose changed.
            if request_id:
                previous = self.store.proposal_for_request_id(request_id)
                if previous is not None:
                    # A retry of a still-open BUY must not skip the stop gate just
                    # because the handle short-circuits the whole pipeline. Re-run
                    # only that one predicate; every other status (rejected,
                    # filled, queued) is an answer, not a live intent, and must
                    # be returned verbatim — do not re-run the whole guard.
                    if (previous.get("status") == "proposed"
                            and str(previous.get("side", "")).upper() == "BUY"):
                        _refuse_open_buys(self.market, self.book,
                                          self._now().strftime("%Y%m%d"))
                    return previous

            # 意图层总开关（fail-closed）。放在幂等之后：超时重试仍拿回原来那张
            # 票；新请求在开关关闭时**硬拒绝**而不是静默丢掉条件。后者会让模型
            # 以为写好了入场条件，approve 却沿普通路径立刻成交——那是资金动作。
            from . import intent as intent_mod

            contingency_raw = req.get("contingency") or None
            accepts_conditions = (settings.flag("trigger.observe")
                                  or settings.flag("trigger.enabled"))
            if contingency_raw is not None and not accepts_conditions:
                raise DecisionError(
                    "TRIGGER_DISABLED",
                    "trigger.observe / trigger.enabled 均为 false：不接收 "
                    "contingency。请按现有一次性提案路径提票，或由人激活 "
                    "observe（只写影子轨迹、不出钱）后再写条件；关闭时提交会留 "
                    "proposal_blocked 记录。",
                    409,
                )
            if contingency_raw is not None:
                problem = intent_mod.validate(intent_mod.coerce(contingency_raw))
                if problem is not None:
                    raise DecisionError("BAD_CONTINGENCY", f"入场条件不可求值：{problem}")

            # Which decision style is spending the money. This is checked before
            # anything is priced, because a style whose prerequisites are missing
            # produces a plausible ticket at an impossible price — e.g. hot money
            # sized off a 15:00 close and booked as if it were a live tape.
            from . import persona as persona_mod

            active = self.store.persona_current()["current"]
            persona_key = str(req.get("persona") or "").strip().lower() or active
            if persona_key != active and side == "BUY":
                raise DecisionError(
                    "PERSONA_MISMATCH",
                    f"提案写的是 {persona_key}，当前活跃人格是 {active}。"
                    f"换风格要显式记录：POST /persona/switch（带 reason/evidence/"
                    f"expected_edge_bps/invalidation）。卖出不受此限。",
                    409,
                )
            if side != "BUY":
                persona_key = active
            persona_meta = persona_mod.get(persona_key)
            if persona_meta is None:
                raise DecisionError(
                    "BAD_PERSONA",
                    f"unknown persona {persona_key}; known={persona_mod.keys()}")
            if side == "BUY" and not persona_meta.allows_sleeve(sleeve):
                raise DecisionError(
                    "PERSONA_SLEEVE",
                    f"{persona_key}（{persona_meta.name}）只能使用 "
                    f"{list(persona_meta.sleeves)}，收到 {sleeve}")
            persona_probe = persona_mod.probe(self.market, self.registry)
            persona_missing = persona_meta.missing_requirements(persona_probe)
            if side == "BUY" and persona_missing:
                raise DecisionError(
                    "PERSONA_NOT_READY",
                    f"{persona_key}（{persona_meta.name}）依赖 "
                    f"{persona_missing}，现在拿不到；"
                    f"缺实时报价时按日线价成交等于把回看写成成交。"
                    f"可以先切到不需要它的风格，或者等数据齐。",
                    409,
                )
            if len(thesis) < 20:
                raise DecisionError("THESIS_TOO_SHORT", "thesis must be >= 20 characters")
            if len(invalidation) < 5:
                raise DecisionError("NO_INVALIDATION", "invalidation must be >= 5 characters")
            if not evidence:
                raise DecisionError("NO_EVIDENCE", "at least one evidence id is required")
            if not symbol.endswith((".SH", ".SZ", ".BJ")):
                raise DecisionError("BAD_SYMBOL", f"{symbol} must look like 600000.SH")

            # A position trading below the stop written on its ticket is a breach
            # of the risk budget the model set for itself. Adding risk elsewhere
            # while that is true contradicts the budget, so the guard refuses it.
            # This is evaluated from prices, not from whether the model replied to
            # a message: there is no message to reply to.
            if side == "BUY":
                _refuse_open_buys(self.market, self.book,
                                  self._now().strftime("%Y%m%d"))
            strategy = str(req.get("strategy") or "llm").lower()
            # Who is answerable for this order. It changes nothing about legality;
            # it decides whether the closed case counts as evidence about the model.
            directed_by = str(req.get("directed_by") or "agent").lower()
            if directed_by not in ("agent", "user"):
                raise DecisionError("BAD_AUTHOR",
                                    f"directed_by must be agent or user, got {directed_by}")
            from . import strategy as strategy_mod

            meta = strategy_mod.get(strategy)
            if meta is None:
                raise DecisionError("BAD_STRATEGY",
                                    f"unknown strategy {strategy}; known={strategy_mod.keys()}")
            if meta.status not in ("live", "probation"):
                raise DecisionError("STRATEGY_INACTIVE", f"{strategy} is {meta.status}")

            date = self._date(symbol)
            # C4: what the exchange clock says about this session, written onto
            # the ticket as information, not as a rejection. The answer depends
            # on BOTH the session and the book's ``fill_mode``, because "will
            # this wait for the next open" is decided by how this desk settles:
            #   * next_open  — every ticket waits for an open except the two
            #     opening-auction windows, which join *today's* open;
            #   * intraday   — fills on a live quote only in CONTINUOUS, so
            #     AUCTION_CLOSE / AFTER_HOURS / CLOSED fall through to next open;
            #   * close      — settles at this session's close, never next open;
            #   * limit_queue — settles same-session (ceiling queue).
            # ``session`` stays the raw enum value at propose time. Note this is
            # the *session + mode* half only: under ``intraday`` a missing/frozen
            # quote can still push the order to the next open at approve time
            # (``FALLBACK_TO_OPEN``), which propose cannot know — so a ``False``
            # here means "not forced by the clock or the mode", not "will fill".
            session_now = self.market.calendar.calendar().session_at(self._now())
            fill_mode = getattr(self.book, "fill_mode", "next_open")
            opening = session_now in (Session.AUCTION_OPEN, Session.PRE_OPEN)
            if fill_mode == "next_open":
                will_queue_next_open = not opening
            elif fill_mode == "intraday":
                will_queue_next_open = session_now in (
                    Session.AUCTION_CLOSE, Session.AFTER_HOURS, Session.CLOSED)
            else:  # "close" settles this close; "limit_queue" settles same-session
                will_queue_next_open = False
            self.book.rollover(date)
            # Re-mark before anything reads equity: the daily-loss and drawdown
            # breakers are computed from ``last_price``, and the last write to it
            # may have been yesterday's close (or an earlier read endpoint).
            self.book.mark_prices(date, live=True)
            snap = self._reference(symbol, date)
            if snap is None:
                # Say *which* feed is short, not just "no bar": the four bar
                # datasets are ingested on separate schedules, so a bond can be
                # unpriced because `cb_daily` has not landed yet while `daily`
                # is already at today. Without that clause the caller cannot
                # tell "this instrument did not trade" from "we are waiting on
                # a file", and only one of the two is worth retrying.
                dataset = self.market.quote_dataset(symbol)
                cover = self.market.dataset_cover_end(dataset)
                late = (f"（{dataset} 只覆盖到 {cover}）"
                        if cover and cover < date else "")
                raise DecisionError("NO_BAR", f"no bar for {symbol} on {date}{late}", 404)
            inst = self.market.instrument(symbol, date)
            equity = self.book.equity()
            price = snap.last
            atr = self.market.atr(symbol, date, 20)
            stop_pct = 0.05
            if price > 0 and atr > 0:
                stop_pct = min(0.12, max(0.03, float(atr * D("1.5") / price)))

            # Size against the same price the guard will value the order at. Using
            # the last price while the limit sits 0.5% above it made every position
            # sized to a cap come out a hair over the cap and get rejected by our
            # own risk check.
            # Two buffers, two config keys. This line used to hold a third
            # hand-typed copy while `oms._buy_buffer()` and `kernel/risk.py` read
            # `risk.market_buy_buffer`; editing that key moved the freeze and
            # left the limit price where it was.
            buffer = (settings.decimal("execution.next_open_buffer")
                      if getattr(self.book, "fill_mode", "close") == "next_open"
                      else settings.decimal("execution.intraday_buffer"))
            limit_price = self._limit_price(side, price, snap, buffer)
            # The stop is a property of the entry the trader can actually get.
            # It is written against the reference price (an ATR distance from
            # today's close), but the *risk* it implies must be measured from the
            # worst price this ticket is allowed to pay — otherwise a 2% gap-up
            # fill silently turns a 4% budget into 6%.
            stop_price = (price * (D("1") - D(str(round(stop_pct, 4))))).quantize(TICK) \
                if side == "BUY" else None
            if side == "BUY":
                qty, target = self._size_buy(req, symbol, sleeve, limit_price, equity,
                                             stop_pct, inst, stop_price=stop_price)
            else:
                qty, target = self._size_sell(req, symbol)

            if qty <= 0:
                need = money(D(inst.min_qty) * limit_price)
                return self._reject_ticket(
                    symbol, side, sleeve, date,
                    [(f"size=0: 最小申报 {inst.min_qty} 股 x {limit_price} = {need}，"
                      f"超出目标仓位与风险预算可承受的金额")],
                    req, price, stop_pct,
                )

            value = money(D(qty) * limit_price)
            fees = (self.book.cost.buy_fees(value, inst) if side == "BUY"
                    else self.book.cost.sell_fees(value, inst))

            result = self.guard.preflight(
                self.book, symbol=symbol, side=side, sleeve=sleeve,
                qty=qty, price=limit_price, date=date, strategy=strategy,
                snapshot=snap,
            )
            if result.size_multiplier < 1.0 and side == "BUY":
                scaled = _floor_lot(float(value) * result.size_multiplier, limit_price, inst)
                if scaled > 0:
                    qty = scaled
                    value = money(D(qty) * limit_price)
                    fees = self.book.cost.buy_fees(value, inst)
                    result = self.guard.preflight(
                        self.book, symbol=symbol, side=side, sleeve=sleeve,
                        qty=qty, price=limit_price, date=date, strategy=strategy,
                        snapshot=snap,
                    )
            if not result.ok:
                rejected = self._reject_ticket(
                    symbol, side, sleeve, date, result.failures, req, price,
                    stop_pct, checks=result.as_dict(), qty=qty, limit_price=limit_price,
                )
                if request_id:
                    winner = self.store.claim_request(request_id, rejected["ticket_id"])
                    if winner is not None:
                        return winner
                return rejected

            # The fingerprint describes the *intent*; the transport handle is not
            # part of it. Hashing the handle in would make two identical
            # proposals with different ids look like different intents.
            canonical = {key: value for key, value in req.items()
                         if key != "client_request_id"}
            canonical.update({
                "symbol": symbol, "side": side, "sleeve": sleeve,
                "strategy": strategy, "persona": persona_key,
                "directed_by": directed_by, "thesis": thesis,
                "invalidation": invalidation, "evidence": evidence,
            })
            fingerprint = hashlib.sha256(json.dumps(
                canonical, ensure_ascii=False, sort_keys=True, default=str,
            ).encode("utf-8")).hexdigest()
            existing = self.store.proposal_for_fingerprint(fingerprint)
            if existing is not None:
                if request_id:
                    winner = self.store.claim_request(request_id, existing["ticket_id"])
                    if winner is not None:
                        return winner
                return existing

            # 往返成本与"需要涨多少才回本"一起写进票据。后者是前者除以 100 就能
            # 得到的数，但它值得单独出现：¥1,500 的一笔在 5 元最低佣金下往返
            # 约 92bps，而常被引用的「短线结构毛边」是 50-140bps——两个数量级
            # 相同，所以这笔到底值不值得做，取决于模型看见的是哪一个数。
            round_trip = round_trip_cost_bps(float(value), inst)

            ticket = {
                "ticket_id": self._ticket_id(date),
                "intent_fingerprint": fingerprint,
                "client_request_id": request_id,
                "created_at": utc_now(),
                "expires_at": f"{date}T23:59:59+08:00",
                "status": "proposed",
                "symbol": symbol,
                "name": inst.name,
                "side": side,
                "sleeve": sleeve,
                "strategy": strategy,
                "directed_by": directed_by,
                "persona": persona_key,
                "intent_type": str(req.get("intent_type", "unspecified")),
                "thesis": thesis,
                "evidence": evidence,
                "invalidation": invalidation,
                "trade_date": date,
                "qty": int(qty),
                "limit_price": str(limit_price),
                "stop_price": str(stop_price) if stop_price else None,
                "price_ref": str(price),
                "est_value": str(value),
                # `est_fees` is the buy side at this quantity. The number that
                # decides whether the trade is worth taking is the round trip:
                # at 1,500 元 the 5 元 minimum commission lands twice, which is
                # 66bps before stamp duty and slippage. Stating it here means the
                # reader does not have to derive it from two Decimals while
                # looking at a ticket it is about to approve.
                "est_fees": str(fees),
                "round_trip_cost_bps": round_trip["total_bps"],
                # 需要涨多少才回本。含费用 + 双边滑点，不含冲击。
                "breakeven_move_pct": round(round_trip["total_bps"] / 100.0, 2),
                "est_risk_cny": str(money(
                    D(qty) * ((limit_price if stop_price else price) - (stop_price or price)))),
                "risk_basis": "worst_fill_at_limit" if stop_price else "none",
                # 天花板单的预期成交。模型按 qty 算仓位与风险，而挂涨停价实际只
                # 成交一部分——不回填这个差，风险预算会整体偏大一倍以上。
                # 只在 fill_mode=limit_queue 时给；别的模式下 qty 就是 qty。
                "expected_fill": (self._expected_ceiling_fill(symbol, date, side, qty, inst)
                                  if side == "BUY" else None),
                "stop_pct": round(stop_pct, 4),
                "target_weight": round(target, 4),
                "atr20": str(atr),
                "adv20": int(self.market.adv_map(date).get(symbol, 0) or 0),
                "fill_basis": self._fill_basis(date),
                # C4: the session at propose time and whether that means the
                # order will wait for the next open. ``session`` is the enum's
                # value (CONTINUOUS / AUCTION_CLOSE / AFTER_HOURS / CLOSED /
                # PRE_OPEN / AUCTION_OPEN); ``will_queue_next_open`` is true
                # only for the three windows that execute_ticket now sends to
                # the next open (C1). Both are informational — nothing here
                # rejects the proposal.
                "session": session_now.value,
                "will_queue_next_open": will_queue_next_open,
                # ``fill`` is the executed fill record (book._finalize_ticket
                # writes it). This block is the estimate written *before* any
                # execution, so it needs its own name or the two overwrite.
                "fillability": self._fillability(symbol, date, side, snap),
                "computed_by": "python",
                "checks": result.as_dict(),
                "snapshot": self._snapshot(date, evidence),
                # ── 意图层：全可选字段 ────────────────────────────────
                # 四个决策时刻字段在 arm() 时冻结；propose 阶段恒为 null。
                # contingency 是模型提票时写好的条件，仅在开关开启时才有值。
                # price_ref（日线估值口径）与 decision_quote（arm 时帧价）
                # 是两种基准，谁也不许顶替谁。
                "decision_ts": None,
                "decision_quote": None,
                "decision_frame_id": None,
                "decision_quote_basis": None,
                "contingency": contingency_raw,
            }
            ticket, created = self.store.save_proposal(
                ticket, fingerprint, client_request_id=request_id)
            if not created:
                return ticket
            self.store.log_decision("propose", {
                "request": req, "ticket": {k: ticket[k] for k in (
                    "ticket_id", "symbol", "side", "sleeve", "persona", "qty",
                    "limit_price", "stop_price", "est_value", "est_fees",
                    "target_weight")},
                "persona": {"key": persona_key, "ready": not persona_missing,
                            "requires": list(persona_meta.requires)},
                "checks": result.as_dict(),
            }, ticket_id=ticket["ticket_id"], actor=actor)
            return ticket

    def _dataset_store(self):
        return self.market.store

    def _fill_basis(self, date: str) -> str:
        """How this ticket can be filled, and whether the price is already public.

        The label used to be the literal string ``"close"``, which was wrong in
        both directions: under ``next_open`` the ticket is queued, and a ticket
        written after the session it names is not filling at that session's
        close at all — it is reading a price everyone already knows.

        On 2026-10-25 a decision made at 23:32 was booked against the 2026-10-23
        close and labelled ``close``, so the account held a two-day-old price
        that looked like an execution. That case is ``backfill``: it can still be
        recorded, but nothing downstream may treat it as a live fill.
        """
        mode = getattr(self.book, "fill_mode", "next_open")
        if mode not in {"next_open", "intraday", "close"}:
            mode = "close"
        if mode == "close" and date < self._now().strftime("%Y%m%d"):
            return "backfill"
        return mode

    def _snapshot(self, date: str, evidence=()) -> dict:
        # Evidence such as ``screen:...`` or ``lineage:...`` is an evidence
        # kind, not a dataset.  Registered prefixes such as ``limit_pool_t0``
        # and ``daily_basic`` are real inputs and must join the fingerprint;
        # otherwise ``complete`` only describes the fixed core list.
        datasets = []
        for item in evidence:
            name = str(item).split(":", 1)[0].strip()
            if name in SPECS:
                datasets.append(name)
        return collect_lineage(self.market.store, date, datasets=datasets).as_dict()

    def _fillability(self, symbol: str, date: str, side: str, snap) -> dict:
        """Can this order fill at all — attached to the ticket, not used to veto.

        The guard answers "is this order legal"; it never answered "can it
        clear". Those are different questions and the model was only being told
        the first one, so a ticket to buy a sealed board came back ``proposed``
        with no hint that the morning would refuse it. That is a decision input
        the desk already had the data for.

        It stays informational. ``queue.required`` is a fact about the market,
        not permission — turning it into a veto would be the kernel deciding not
        to trade, which is not its job.
        """
        from .fill import feasibility

        try:
            return feasibility(self.market, symbol, day=date, side=side, snapshot=snap)
        except Exception as exc:  # noqa: BLE001 - a missing pool must not block a legal order
            # Degrade to "cannot say", never take the proposal down: the order's
            # legality does not depend on this block, and a ticket that fails to
            # write because a side table is missing is a worse failure than one
            # written without a fill estimate.
            return {"ok": False, "symbol": symbol, "as_of": date,
                    "error": f"{type(exc).__name__}: {exc}",
                    "note": "成交可行性取不到，不影响这笔委托的合法性判断"}

    # ----------------------------------------------------------------- sizing
    def _size_buy(self, req: dict, symbol: str, sleeve: str, price: Decimal,
                  equity: Decimal, stop_pct: float, inst,
                  stop_price: Decimal | None = None) -> tuple[int, float]:
        limits = self.guard.limits
        single_cap = (limits.max_aggressive_single if sleeve == "AGGRESSIVE"
                      else (limits.max_core_etf_single if inst.is_etf else limits.max_core_single))
        risk_budget = float(equity) * limits.max_trade_risk
        # Risk per share is measured from the worst price this ticket may pay to
        # the stop, not from the stop distance alone: the limit sits above the
        # reference price, and that gap is risk too.
        risk_per_share = (float(price - stop_price) if stop_price is not None
                          else float(price) * max(stop_pct, 0.001))
        qty_risk = _floor_shares(risk_budget / max(risk_per_share, 1e-6), inst)

        # An explicit quantity is a legitimate intent — "one lot" is a judgment,
        # not a leak of kernel internals. It is still bounded by the lot table,
        # the single-position cap and the per-ticket risk budget; what changes is
        # that the model no longer has to reverse-engineer equity × weight just
        # to say "buy 100 shares".
        requested_qty = req.get("qty")
        if requested_qty is not None:
            # ``int(100.7)`` used to write 100 into the ticket: a size the caller
            # never asked for, invisible in the response, and the same question
            # ``oms.submit`` already answers with BAD_QTY. Same rule, same
            # refusal — a ticket that survives sizing must also survive submit.
            qty = integral_qty(requested_qty)
            if qty is None:
                raise DecisionError(
                    "BAD_SIZE",
                    f"qty 必须是整数股数，收到 {requested_qty!r}；"
                    "内核不会替你截断小数，请先按手数取整再提")
            if qty <= 0:
                raise DecisionError("BAD_SIZE", "qty must be positive")
            legal = _nearest_lot(qty, inst)
            if legal != qty:
                raise DecisionError(
                    "BAD_SIZE",
                    f"{qty} 股不是合法手数：{inst.min_qty} 股起、步长 {inst.qty_step} 股；"
                    f"这个方向最接近的合法数量是 {legal} 股")
            target = (float(qty) * float(price) / float(equity)) if equity else 1.0
            if target > single_cap + 1e-9:
                max_qty = _floor_lot(float(equity) * single_cap, price, inst)
                raise DecisionError(
                    "POSITION_CAP",
                    f"{qty} 股 = {target:.1%} 权益，超过单票上限 {single_cap:.0%}；"
                    + (_cannot_buy_one_lot(inst, price, float(equity) * single_cap)
                       if max_qty == 0 else f"这只票最多 {max_qty} 股"))
            if qty > qty_risk:
                raise DecisionError(
                    "RISK_CAP",
                    f"{qty} 股在 {stop_pct:.1%} 止损、限价 {price} 下承担约 "
                    f"{qty * risk_per_share:,.0f} 元风险，超过单笔预算 "
                    f"{limits.max_trade_risk:.0%}（{risk_budget:,.0f} 元）；"
                    + (_cannot_buy_one_lot(inst, price, risk_budget, "单笔风险预算")
                       if qty_risk == 0 else f"按风险最多 {qty_risk} 股"))
            return qty, target

        requested = req.get("target_weight")
        target = float(requested) if requested is not None else DEFAULT_TARGET.get(sleeve, 0.2)
        if target <= 0 or target > 1:
            raise DecisionError("BAD_TARGET", "target_weight must be in (0,1]")
        target = min(target, single_cap)
        qty_value = _floor_lot(float(equity) * target, price, inst)
        return min(qty_value, qty_risk), target

    def _size_sell(self, req: dict, symbol: str) -> tuple[int, float]:
        """Sell exits the whole available position unless a `qty` trims it.

        "Cut half and keep the rest" is a judgment. Forcing every trim into an
        all-or-nothing exit made the model either express a full sell it did not
        mean, or say nothing at all.
        """
        pos = self.book.ledger.account.peek(symbol)
        available = pos.available_qty if pos is not None else 0
        requested = req.get("qty")
        if requested is None:
            return available, 0.0
        qty = integral_qty(requested)
        if qty is None:
            raise DecisionError(
                "BAD_SIZE",
                f"qty 必须是整数股数，收到 {requested!r}；"
                "内核不会替你截断小数，请先按手数取整再提")
        if qty <= 0:
            raise DecisionError("BAD_SIZE", "qty must be positive")
        if pos is None:
            # `peek` returns None for a symbol with no position; the branch above
            # read that as "sell nothing", but an explicit qty then dereferenced
            # `pos.available_qty` and surfaced as a 500 (AttributeError) instead
            # of a 4xx. A sell for something you do not hold is a caller error,
            # and it has a name in the kernel already.
            raise DecisionError(
                "INSUFFICIENT_SHARES",
                f"{symbol} 没有持仓可卖（请求 {qty} 股）")
        if qty > pos.total_qty:
            # Same distinction the kernel makes (``oms.submit``): "you do not own
            # that many" is final, "you own them but cannot deliver them today"
            # is a wait. Reporting the second for the first told the caller to
            # retry tomorrow, which fails identically forever.
            raise DecisionError(
                "INSUFFICIENT_SHARES",
                f"{symbol} 持仓 {pos.total_qty} 股，请求 {qty} 股；卖出不能超过持仓总数")
        if qty > pos.available_qty:
            raise DecisionError(
                "T_PLUS_ONE",
                f"可卖 {pos.available_qty} 股（当天买入的部分 T+1 才可卖，"
                f"或已被在途卖单占用），请求 {qty} 股")
        return qty, 0.0

    # ------------------------------------------------------------------ price
    @staticmethod
    def _limit_price(side: str, price: Decimal, snap, buffer: Decimal | None = None) -> Decimal:
        """Marketable limit around the reference price.

        When execution happens at the next open the buffer has to absorb an
        overnight gap, otherwise almost every entry dies on a 0.5% move. 2% is
        the compromise: wide enough to trade, narrow enough that a real gap-up
        still refuses the fill instead of chasing it.
        """
        buffer = D("0.005") if buffer is None else buffer
        if side == "BUY":
            raw = max(price, (price * (D("1") + buffer)).quantize(TICK))
            # A zero limit means the bar carried no band (a gap in ``stk_limit``),
            # not "the ceiling is zero". Clamping to it unconditionally wrote a
            # limit of 0 into the ticket, and the kernel then refused the order as
            # PRICE_LIMIT — missing data wearing the costume of an illegal price.
            if snap.up_limit > 0:
                raw = min(snap.up_limit, raw)
        else:
            raw = min(price, (price * (D("1") - buffer)).quantize(TICK))
            if snap.down_limit > 0:
                raw = max(snap.down_limit, raw)
        return raw.quantize(TICK, rounding=ROUND_DOWN)

    # ---------------------------------------------------------------- rejects
    def _reject_ticket(self, symbol: str, side: str, sleeve: str, date: str,
                       reasons: list[str], req: dict, price: Decimal, stop_pct: float,
                       *, checks: dict | None = None, qty: int = 0,
                       limit_price: Decimal | None = None) -> dict:
        ticket = {
            "ticket_id": self._ticket_id(date),
            "created_at": utc_now(),
            "expires_at": f"{date}T23:59:59+08:00",
            "status": "rejected",
            "symbol": symbol,
            "side": side,
            "sleeve": sleeve,
            "intent_type": str(req.get("intent_type", "unspecified")),
            "thesis": str(req.get("thesis", "")),
            "evidence": [str(e) for e in req.get("evidence", [])],
            "invalidation": str(req.get("invalidation", "")),
            "trade_date": date,
            "qty": qty,
            "limit_price": str(limit_price) if limit_price else None,
            "price_ref": str(price),
            "stop_pct": round(stop_pct, 4),
            "reasons": reasons,
            "checks": checks or {},
            "computed_by": "python",
            "snapshot": self._snapshot(date),
            # 拒单票同形：审计链不因「被 guard 拒绝」而少一列时间维度。
            "decision_ts": None,
            "decision_quote": None,
            "decision_frame_id": None,
            "decision_quote_basis": None,
            "contingency": req.get("contingency") or None,
        }
        self.store.save_ticket(ticket)
        self.store.log_decision("reject", {"reasons": reasons, "request": req},
                                ticket_id=ticket["ticket_id"], actor="guard")
        return ticket

    # ------------------------------------------------------------------ arm
    def _freeze_decision_quote(self, ticket: dict) -> dict:
        """D-7：只读帧、冻结决策价。任何异常都退化成 unavailable + 原因。

        arm 是 approve 路径上的一步：帧目录不可读、parquet 损坏、列缺失都
        **不许让它抛出去变成 500**，否则「记不上决策价」会连带把一次合法的
        条件批准变成失败。宁可如实说取不到，也不假装、也不阻断。
        """
        from . import replay

        unavailable = {"decision_quote": None,
                       "decision_quote_basis": "unavailable",
                       "decision_frame_id": None}
        root = getattr(getattr(self.market, "store", None), "root", None)
        if root is None:
            return {**unavailable, "decision_price_reason": "frame_root_unavailable"}
        try:
            fields = replay.backfill_fields(ticket, root)
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            return {**unavailable,
                    "decision_price_reason": f"replay_failed:{type(exc).__name__}"}
        out = {
            "decision_quote": fields["decision_quote"],
            "decision_quote_basis": fields["decision_quote_basis"],
            "decision_frame_id": fields["decision_frame_id"],
            "decision_price_reason": fields.get("reason"),
        }
        # 两个时钟都留：source_ts 是帧内每行报价时刻，acquisition_ts 是
        # 文件名 batch clock（系统收到这一批的时刻）。谁也不顶替谁。
        if fields.get("source_ts"):
            out["decision_source_ts"] = fields["source_ts"]
        if fields.get("acquisition_ts"):
            out["decision_acquisition_ts"] = fields["acquisition_ts"]
        return out

    def _arm(self, ticket: dict, *, actor: str) -> dict:
        """冻结决策时刻、置 ``status="armed"``。调用方持 ``self.lock``。

        零资金动作：不调 ``book.execute_ticket``、不跑 guard（propose 已跑过）、
        不写 pending。票只在 tickets 表里改状态，账本/持仓/队列三项零变化。

        ``decision_quote`` 在 arm **当场冻结**（裁定 D-7）：从 ``replay`` 取决策
        时刻可见的最后一帧的价与 ``frame_id``，仅真读到帧价时写 ``basis="frame"``；
        取不到保持 ``unavailable`` 并留原因。不许拿 ``snap.last``（日线昨收）
        冒充——那会把隔夜跳空算成 delay cost。事后回填会让「决策价」变成看到
        结果后挑出来的基准；只留只读查询则让可复现性依赖冷存文件是否还在。
        冻结发生在 approve 持锁期间、零资金动作，且第二次 approve 不覆盖。
        """
        from . import intent as intent_mod

        problem = intent_mod.validate(intent_mod.coerce(ticket.get("contingency")))
        if problem is not None:
            ticket["status"] = "rejected"
            ticket["reasons"] = [f"BAD_CONTINGENCY: {problem}"]
            stored = self.store.save_ticket(ticket)
            if stored.get("status") != "rejected":
                return self._current_ticket_result(stored)
            self.store.log_decision("risk_veto", {"contingency": problem},
                                    ticket_id=ticket["ticket_id"], actor="guard")
            return {"status": "rejected", "ticket": stored, "reasons": stored["reasons"]}

        stamp = utc_now()
        ticket["status"] = "armed"
        ticket["decision_ts"] = stamp
        ticket["armed_at"] = stamp
        ticket.update(self._freeze_decision_quote(ticket))
        stored = self.store.save_ticket(ticket)
        if stored.get("status") != "armed":
            return self._current_ticket_result(stored)
        self.store.log_decision(
            "arm", {"contingency": stored["contingency"],
                    "decision_quote": stored["decision_quote"],
                    "decision_quote_basis": stored["decision_quote_basis"],
                    "decision_frame_id": stored["decision_frame_id"],
                    "decision_price_reason": stored.get("decision_price_reason"),
                    "note": "条件与决策价已冻结，等待捕获帧求值；未出钱、未进撮合队列"},
            ticket_id=ticket["ticket_id"], actor=actor,
        )
        return {"status": "armed", "ticket": stored,
                "note": "条件已冻结，等待帧触发；未出钱、未进入撮合队列"}

    def _current_ticket_result(self, ticket: dict) -> dict:
        """Describe a transition that another process committed first."""
        status = str(ticket.get("status") or "")
        if status == "filled":
            return {"status": "already_filled", "ticket": ticket,
                    "fill": self.book._fill_exists(str(ticket["ticket_id"]))}
        result = {"status": status, "ticket": ticket}
        if ticket.get("reasons"):
            result["reasons"] = ticket["reasons"]
        return result

    def _expire_stale(self, ticket: dict, ticket_id: str) -> dict:
        """跨日票的唯一终态出口：改状态、留审计、零资金动作。

        11-08 深审 B3 之前，只有 approve 尾部会写 expired，昨天的条件票
        今天 approve 会先被 arm（状态标签错、还多一条 arm 日志）。抽成
        单一出口，让 arm 之前与 guard 之前共用同一段处理。
        """
        ticket["status"] = "expired"
        ticket["expired_at"] = utc_now()
        stored = self.store.save_ticket(ticket)
        if stored.get("status") != "expired":
            return self._current_ticket_result(stored)
        self.store.log_decision("expire", {"trade_date": ticket["trade_date"]},
                                ticket_id=ticket_id, actor="system")
        return {"status": "expired", "ticket": stored,
                "reasons": ["ticket is from a previous session"]}

    # ---------------------------------------------------------------- approve
    def approve(self, ticket_id: str, actor: str = "agent") -> dict:
        with self.lock:
            ticket = self.store.get_ticket(ticket_id)
            if ticket is None:
                raise DecisionError("NOT_FOUND", f"ticket {ticket_id} not found", 404)
            if ticket["status"] == "filled":
                return {"status": "already_filled", "ticket": ticket,
                        "fill": self.book._fill_exists(ticket_id)}
            if ticket["status"] == "queued":
                return {"status": "queued", "ticket": ticket,
                        "note": "already waiting for the next open"}
            if ticket["status"] in ("rejected", "expired", "cancelled"):
                return {"status": ticket["status"], "ticket": ticket,
                        "reasons": ticket.get("reasons", [])}

            # ── armed -> filled 的迁移守卫（2026-11-08 深审 B2，严重度 高）──
            # 这张票只有一条合法出口：盯盘回路在 intent.run 判 triggered 之后，
            # 带着 intent.actor_for() 的 `trigger:<ticket_id>` 身份兑现同一张票。
            # HTTP approve 固定传 actor="agent"，请求体不接受 actor，所以手工
            # approve 无法伪造兑现身份。缺了这组守卫，条件没成立、valid_until
            # 已过、或开关已关的票都会被一次手工 approve 按市价成交。
            if ticket.get("status") == "armed":
                if not settings.flag("trigger.enabled"):
                    return {"status": "armed", "ticket": ticket,
                            "note": ("observe-only：trigger.enabled=false，armed 票"
                                     "只留影子终态，approve 不会把它变成订单")}
                terminal = ticket.get("intent_terminal_state")
                if terminal in ("expired_untriggered", "denied"):
                    return {"status": "armed", "ticket": ticket,
                            "note": f"条件终态 {terminal}：不得复活成交"}
                if not str(actor).startswith("trigger:"):
                    return {"status": "armed", "ticket": ticket,
                            "note": ("armed 意图只能由盯盘兑现回路推进；手工 approve "
                                     "不会成交，撤销请用 reject/cancel")}
                # 有效期二次复核：票可能跨过 valid_until 才轮到兑现，或时钟跳变。
                until = (ticket.get("contingency") or {}).get("valid_until")
                if until:
                    try:
                        cutoff = datetime.fromisoformat(str(until))
                        cutoff = (cutoff.replace(tzinfo=SHANGHAI)
                                  if cutoff.tzinfo is None
                                  else cutoff.astimezone(SHANGHAI))
                        expired = datetime.now(SHANGHAI) > cutoff
                    except (TypeError, ValueError):
                        expired = True   # 无法解析的有效期按失效处理，fail-closed
                    if expired:
                        return {"status": "armed", "ticket": ticket,
                                "note": "条件已越过 valid_until：不得成交"}

            # ── 跨日判据（11-08 深审 B3）──────────────────────────────
            # 只比较时钟与 trade_date，不调用 _date()：后者会因非交易日或行情
            # 未就绪抛错，而 arm / expired 都是零资金动作，不该被行情可用性
            # 拦住。原路径仍照常用 _date() 做定价日校验。
            stale = self._now().strftime("%Y%m%d") > str(ticket["trade_date"])
            if (ticket.get("status") == "proposed" and ticket.get("contingency")
                    and stale):
                return self._expire_stale(ticket, ticket_id)

            # ── 两阶段 approve（意图层）────────────────────────────────
            # 首次 approve 只把**已批准的**意图冻结成 armed：不出钱、不跑 guard、
            # 不进 pending、不碰 SAME_SESSION_QUEUES / _try_fill。若在这里直接
            # 落到下面的 execute_ticket，条件还没成立钱就出去了，「为什么是现在」
            # 就永远无法结算。触发兑现那一次 approve（status=armed）自然穿过
            # 跨日检查 → guard 二次 → execute_ticket，guard 恰好落在真正出钱时点。
            if (ticket.get("status") == "proposed" and ticket.get("contingency")
                    and (settings.flag("trigger.observe")
                         or settings.flag("trigger.enabled"))):
                return self._arm(ticket, actor=actor)

            today = self._date(ticket["symbol"])
            self.book.rollover(today)
            self.book.mark_prices(today, live=True)
            if today > ticket["trade_date"]:
                return self._expire_stale(ticket, ticket_id)

            # The guard runs twice: once when the ticket is written, once here,
            # because the world may have moved between the two. Both calls must
            # price the order the same way, so both are handed the same
            # reference - a live quote when today's bar does not exist yet.
            result = self.guard.preflight(
                self.book, symbol=ticket["symbol"], side=ticket["side"],
                sleeve=ticket["sleeve"], qty=int(ticket["qty"]),
                price=D(ticket["limit_price"]), date=today,
                strategy=ticket.get("strategy"),
                snapshot=self._reference(ticket["symbol"], today),
            )
            if not result.ok:
                ticket["status"] = "rejected"
                ticket["reasons"] = result.failures
                ticket["checks"] = result.as_dict()
                stored = self.store.save_ticket(ticket)
                if stored.get("status") != "rejected":
                    return self._current_ticket_result(stored)
                self.store.log_decision("risk_veto", result.as_dict(),
                                        ticket_id=ticket_id, actor="guard")
                return {"status": "rejected", "ticket": stored, "reasons": result.failures}

            exec_result = self.book.execute_ticket(ticket, actor=actor)
            if exec_result.ok and exec_result.status == "queued":
                stored = self.store.get_ticket(ticket_id) or ticket
                # ``open``（09:25–09:30 下的单）用的是**今天**的开盘价，
                # ``close`` 用的是今天的收盘价，``next_open`` 才是明天。三个都
                # 叫"挂单"，但前两个当天 17:30 后就有结果，最后一个要等到次日
                # 开盘——提示词会照着这句话写报告，说错就是把"今天收盘"写成
                # "明天开盘"。
                kind = str(stored.get("queue_kind") or "next_open")
                if kind == "open":
                    note = ("queued for this session's opening auction; today's "
                            "print settles it after the 17:30 bar, and a gap "
                            "through the limit cancels it")
                elif kind == "close":
                    note = ("queued for this session's close; the 17:30 bar "
                            "settles it at that day's closing print, and a limit "
                            "on the wrong side cancels it rather than repricing")
                else:
                    note = ("will execute at the next session's open; "
                            "a gap through the limit cancels it")
                return {"status": "queued", "ticket": stored, "note": note}
            if not exec_result.ok:
                ticket["status"] = "rejected"
                ticket["reasons"] = [exec_result.reason]
                stored = self.store.save_ticket(ticket)
                if stored.get("status") != "rejected":
                    return self._current_ticket_result(stored)
                return {"status": "rejected", "ticket": stored,
                        "reasons": stored.get("reasons", [exec_result.reason])}

            # The book committed account, fill, queue removal and this ticket in
            # one transaction.  Return that durable ticket instead of rewriting
            # its provenance with a mode-specific constant here.
            stored = self.store.get_ticket(ticket_id)
            if stored is None or stored.get("status") != "filled":
                raise DecisionError(
                    "EXECUTION_STATE", f"ticket {ticket_id} has no committed fill", 500)
            return {"status": "filled", "ticket": stored, "fill": stored["fill"]}

    # ----------------------------------------------------------------- reject
    def reject(self, ticket_id: str, reason: str, actor: str = "agent") -> dict:
        """Close a proposal. On ``armed`` this is a withdrawal, not a refusal.

        Returns ``{"status": "cancelled", "rejected": False}`` for an armed
        intent (D-13/D-21): it was approved and has no order, so calling the
        result "rejected" would claim a guard decision that never happened.
        Ordinary ``proposed`` tickets still become ``rejected``.
        """
        with self.lock:
            ticket = self.store.get_ticket(ticket_id)
            if ticket is None:
                raise DecisionError("NOT_FOUND", f"ticket {ticket_id} not found", 404)
            status = str(ticket["status"])
            if status == "filled":
                raise DecisionError("ALREADY_FILLED", "cannot reject a filled ticket", 409)
            if status in {"cancelled", "expired", "rejected"}:
                # Idempotent: a terminal status is an answer, not a new decision.
                # Do NOT call ``cancel_ticket`` here — it would append a second
                # decisions row and could rewrite the status back to rejected.
                return {"status": status, "ticket": ticket, "rejected": False}
            if status == "armed":
                # D-13: an armed intent was approved but has no order yet.
                # Withdrawal is a cancel, not a rejection of the proposal.
                stored = self.book.cancel_armed_ticket(ticket, reason, actor)
                return {"status": stored["status"], "ticket": stored,
                        "rejected": False}
            return self.book.cancel_ticket(ticket, reason, actor)

    def cancel(self, ticket_id: str, *, actor: str = "agent",
               reason: str = "") -> dict:
        """Withdraw a queued order **or an armed intent**.

        ``reject`` closes a proposal; ``cancel`` normally withdraws an order
        that was already queued. On an ``armed`` intent both routes go through
        ``book.cancel_armed_ticket`` (D-13/D-21) and return
        ``status="cancelled"`` — no order exists and nothing is released. A
        filled ticket reports ``status="filled"`` with ``cancelled: False`` —
        "cancel" was tried, the fill had already won — rather than 409, so the
        model reads the fact instead of an error code.

        D-13: ``armed`` has no order to withdraw. It used to fall through to
        ``cancel_order`` and come back ``not_queued``, leaving the intent armed
        while the caller believed it was gone. Route it through the same
        armed-cancel path as ``reject`` so both entry points really release it.
        """
        with self.lock:
            pending_ticket = self.store.get_ticket(ticket_id)
            if (pending_ticket is not None
                    and str(pending_ticket.get("status")) == "armed"):
                stored = self.book.cancel_armed_ticket(pending_ticket, reason, actor)
                return {"ticket_id": ticket_id, "status": stored["status"],
                        "cancelled": stored["status"] == "cancelled",
                        "released_qty": 0, "queue_kind": None, "ticket": stored}
        result = self.book.cancel_order(ticket_id, actor=actor, reason=reason)
        if result.get("status") == "not_found":
            raise DecisionError("NOT_FOUND", f"ticket {ticket_id} not found", 404)
        return result

    # ------------------------------------------------------------------- read
    def get(self, ticket_id: str) -> dict:
        ticket = self.store.get_ticket(ticket_id)
        if ticket is None:
            raise DecisionError("NOT_FOUND", f"ticket {ticket_id} not found", 404)
        return ticket

    def list_tickets(self, limit: int = 20, status: str | None = None,
                     view: str = "brief") -> list[dict]:
        tickets = self.store.list_tickets(limit=limit, status=status)
        if view == "full":
            return tickets
        return [ticket_brief(t) for t in tickets]


#: What a **list** of tickets carries. Deliberately an allow-list, not a deny-list
#: of the heavy fields: the ticket record is 2.8–3.8 KB and most of it is the
#: guard's evidence (``checks`` ~1.4 KB, ``fillability`` 0.5–0.9 KB, ``snapshot``
#: ~0.4 KB). Twenty of those is 56 KB — past the 16 KB tool-output ceiling, so a
#: list built on the full record would be shrunk to about five rows and the
#: "proposals I forgot about" question would still have no answer.
#:
#: A field added to the ticket later is *not* here by default. That is the point:
#: the list is an index, and ``quant_ticket(id)`` is the record.
_TICKET_BRIEF_FIELDS = (
    "ticket_id", "client_request_id", "created_at", "expires_at", "status",
    "symbol", "name", "side", "sleeve", "strategy", "persona", "directed_by",
    "intent_type", "trade_date", "qty", "target_weight",
    "est_value", "est_fees", "limit_price", "stop_price", "stop_pct",
    "price_ref", "round_trip_cost_bps", "fill_basis",
    "thesis", "invalidation", "rejected_at", "reasons",
)


def ticket_brief(ticket: dict[str, Any]) -> dict[str, Any]:
    """The list view of one ticket: what it is, what it costs, why it is still open."""
    return {key: ticket[key] for key in _TICKET_BRIEF_FIELDS if key in ticket}
