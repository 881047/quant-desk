"""Pre-trade guard: the part the model cannot argue with.

Every check returns a machine-readable row. ``ok=False`` blocks the ticket at
proposal time *and* again at approval time, because the world may have changed
between the two (another fill, a data quarantine, a limit-down day).

Numbers here are deliberately conservative for a 5,000 CNY account: a 5 CNY
minimum commission makes small tickets expensive, and a single 20% gap can wipe
a month of gains.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from service import settings
from service.adapters.brief import build_brief
from service.kernel.models import Board, D, money

from . import strategy as strategy_meta
from .book import PaperBook
from .exposure import industry_weight
from .market import MarketRepository

# A convertible's last two weeks. The store has no redemption schedule, so this
# is a time fence rather than a prediction: inside it, a new position could be
# called away at par before the thesis has time to work. Ten sessions is roughly
# two calendar weeks and matches the shortest horizon the referee will judge.
# The value lives in configs/params.yaml (guard.cb_last_buy_sessions); read it
# where it is used so a config edit takes effect without touching this file.


def _strategy_value(book, strategy: str, price: Decimal) -> Decimal:
    """Market value already tagged to one strategy, excluding a new order."""
    owners = book.state.get("strategy_of", {})
    total = D("0")
    # Snapshot the pairs: this runs at proposal time, outside ``PaperBook.lock``,
    # while another thread's fill may add a position. Iterating a dict that
    # grows raises RuntimeError, and a guard crashing is worse than a stale mark.
    for symbol, pos in tuple(book.ledger.account.positions.items()):
        if pos.total_qty <= 0 or owners.get(symbol, "llm") != strategy:
            continue
        mark = pos.last_price if pos.last_price > 0 else price
        total += money(D(pos.total_qty) * mark)
    return total


def ratio_within(value: Decimal, equity: Decimal, cap: float) -> bool:
    """Whether ``value / equity`` stays inside a float cap.

    Comparing a Decimal ratio to a float limit is a trap: ``Decimal("0.3") <= 0.3``
    is False because the float is 0.2999999999999999889. A position sized exactly
    at its cap was therefore rejected by its own risk check.
    """
    if equity <= 0:
        return True
    return float(value) / float(equity) <= float(cap) + 1e-12


def _pct_note(actual: float, cap: float, prefix: str = "") -> str:
    """``"银行 47.4% > 35%"`` — the sign has to match the verdict.

    The detail string travels into the model's error message and into the cancel
    reason of a queued ticket. A row that reads ``47.4% <= 35%"`` while blocking
    the order is worse than no row at all.
    """
    sign = "<=" if actual <= cap + 1e-12 else ">"
    return f"{prefix}{actual:.1%} {sign} {cap:.0%}"


def _num(key: str):
    """A dataclass default read from configs/params.yaml at construction time.

    Not a module-level constant: ``samples_limits()`` and the tests build their
    own ``RiskLimits`` with explicit overrides, and those must keep working
    without a second copy of the numbers. Reading at construction also means a
    config edit plus a restart is enough — no source change.
    """
    return field(default_factory=lambda: settings.num(f"risk.{key}"))


def _int(key: str):
    return field(default_factory=lambda: settings.integer(f"risk.{key}"))


@dataclass(frozen=True)
class RiskLimits:
    max_positions: int = _int("max_positions")
    max_core_weight: float = _num("max_core_weight")
    max_aggressive_weight: float = _num("max_aggressive_weight")
    min_cash_weight: float = _num("min_cash_weight")
    max_core_single: float = _num("max_core_single")
    max_core_etf_single: float = _num("max_core_etf_single")
    max_aggressive_single: float = _num("max_aggressive_single")
    max_trade_risk: float = _num("max_trade_risk")          # equity at risk per ticket
    daily_loss_no_new: float = _num("daily_loss_no_new")
    daily_loss_reduce_only: float = _num("daily_loss_reduce_only")
    drawdown_half: float = _num("drawdown_half")
    drawdown_stop: float = _num("drawdown_stop")
    max_adv_ratio: float = _num("max_adv_ratio")            # order value <= 1% of 20d ADV
    # Round-trip fixed cost ceiling, in bps. 0 = report only (the first week),
    # >0 = reject. On a CNY 5,000 account the minimum commission alone is ~60 bps
    # on a 1,600 ticket and ~290 bps on a 350 one, so this is the number that
    # decides whether a trade is worth placing at all, not a style preference.
    min_ticket_bps: float = field(
        default_factory=lambda: settings.num("execution.min_ticket_bps"))
    # Must stay above ``max_core_single`` (30%) or it silently becomes the real
    # single-name cap: a 30% position in one industry would always be rejected.
    # settings.INVARIANTS enforces that ordering, so a bad config fails at startup
    # instead of blocking every order at 09:31.
    max_industry: float = _num("max_industry")
    # A 24-hour desk wakes several times a day, and a looping agent can turn that
    # into a burst of orders in minutes. This caps *fills* per session, buys and
    # sells together, because the risk is the same either way: churn. Sells are
    # exempt when they reduce risk - see ``preflight``.
    max_actions_per_day: int = _int("max_actions_per_day")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    #: 结构化的读数：数字不嵌在英文句子里，前端可以按 unit 统一对齐、自己拼文案
    #: （docs/14 C2 方案②）。``None`` 表示这项检查没有可拆的数值。
    structured: dict | None = None
    #: 给界面看的中文文案。留空时前端退回 ``detail``；旧 detail 一字不改，
    #: 因为它还会进 cancel reason 与票据 reasons，改字符串会动到结算文本。
    detail_zh: str | None = None

    def __post_init__(self) -> None:
        # pandas/numpy comparisons produce numpy.bool_, which json cannot encode.
        object.__setattr__(self, "ok", bool(self.ok))

    def as_dict(self) -> dict:
        out = {"name": self.name, "ok": self.ok, "detail": self.detail}
        if self.detail_zh is not None:
            out["detail_zh"] = self.detail_zh
        # 每一项都给结构化读数，前端不必按 name 特判"这项有没有数字"。
        # 没有可拆数值的开关型检查用 bool 兜底，并标明 basis=status_only，
        # 免得 `actual` 被当成量出来的数字。
        out["structured"] = self.structured or {
            "metric": self.name, "actual": self.ok, "cap": None,
            "unit": "bool", "basis": "status_only",
        }
        return out


@dataclass
class GuardResult:
    checks: list[Check] = field(default_factory=list)
    size_multiplier: float = 1.0

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.ok]

    def as_dict(self) -> dict:
        return {"ok": self.ok, "size_multiplier": self.size_multiplier,
                "checks": [c.as_dict() for c in self.checks],
                "failures": self.failures}


class Guard:
    def __init__(self, market: MarketRepository, registry, limits: RiskLimits | None = None):
        self.market = market
        self.registry = registry
        self.limits = limits or RiskLimits()

    # ------------------------------------------------------------------ data
    def data_checks(self, date: str, sleeve: str, *, live: bool = False) -> list[Check]:
        """Is the *history* current enough to trade on?

        ``live`` changes the question. After the close, "ready" means today's bar
        exists. During a session today's bar cannot exist yet - it is published
        after 15:00 - so requiring it would block every intraday order while the
        price is right there. In that case the requirement is: history is current
        through the previous session, and the current price comes from the quote
        that ``preflight`` was handed.
        """
        calendar = self.market.calendar
        as_of = (calendar.prev(date) or date) if live else date
        # One query, not two. ``summary()`` is a GROUP BY over the manifest with
        # a correlated subquery per dataset — measured 5.0 ms for 43 datasets —
        # and this method used to call it once for the brief and again for the
        # aggressive pool check below, on every preflight (proposal *and* fill).
        summary = self.registry.summary()
        brief = build_brief(
            summary=summary,
            calendar=calendar,
            today=as_of,
            rt_age_seconds=None,
            sentiment=None,
        )
        core_zh = ("历史覆盖到上一交易日，当日价来自实时报价" if live
                   else "日线/复权因子/涨跌停价覆盖到本交易日")
        checks = [Check(
            "data.core", brief["core"]["usable"],
            ("历史覆盖到上一交易日（当日价来自实时报价）" if live
             else "daily/adj_factor/stk_limit cover the session")
            if brief["core"]["usable"] else ",".join(brief["core"]["missing"]),
            structured={"metric": "core_data_usable",
                        "actual": bool(brief["core"]["usable"]), "cap": None,
                        "unit": "bool", "missing": list(brief["core"]["missing"])},
            detail_zh=(core_zh if brief["core"]["usable"]
                       else "核心数据缺失：" + ",".join(brief["core"]["missing"])),
        )]
        if sleeve.upper() == "AGGRESSIVE":
            # ``as_of``, not ``date``: during a session today's limit-up pool does
            # not exist yet (it is derived after the close), so demanding it would
            # block every aggressive ticket until 17:30. The brief already treats
            # one session of lag as current; this check used to demand same-day
            # coverage and the two readiness answers disagreed.
            #
            # The detail now names *which* pool satisfied the check. Both used to
            # pass the same sentence, so a run that fell back to a vendor pool —
            # 1.6 names short on an average day — read exactly like one backed by
            # the audited derived table.
            covering = [
                row["dataset"] for row in summary
                if row["dataset"] in ("limit_up_pool", "limit_pool_t0")
                and (row.get("cover_end") or "") >= as_of
            ]
            pool_ok = bool(covering)
            if not pool_ok:
                detail = f"pool missing through {as_of}"
            else:
                detail = (f"limit-up pool covers {as_of} "
                          f"({'derived: audited' if 'limit_pool_t0' in covering else 'vendor only: 1.6 names short a day'})")
            if not pool_ok:
                pool_zh = f"涨停池缺 {as_of} 的覆盖"
            elif "limit_pool_t0" in covering:
                pool_zh = f"涨停池覆盖到 {as_of}（派生池，已审计）"
            else:
                pool_zh = f"涨停池覆盖到 {as_of}（仅厂商池，日均少 1.6 只）"
            checks.append(Check(
                "data.aggressive", pool_ok, detail,
                structured={"metric": "limit_up_pool_covered", "actual": pool_ok,
                            "cap": None, "unit": "bool", "covering": covering},
                detail_zh=pool_zh))
        return checks

    # ------------------------------------------------------------------ main
    def preflight(self, book: PaperBook, *, symbol: str, side: str, sleeve: str,
                  qty: int, price: Decimal, date: str,
                  strategy: str | None = None, snapshot=None,
                  exclude_ticket: str | None = None,
                  quotes: dict | None = None) -> GuardResult:
        cfg = self.limits
        account = book.ledger.account
        equity = book.equity()
        checks: list[Check] = []
        mult = 1.0

        checks.extend(self.data_checks(
            date, sleeve,
            live=str(getattr(snapshot, "source", "") or "").startswith("live:"),
        ))
        checks.append(Check("kill_switch", not book.state.get("kill_switch"),
                            "off" if not book.state.get("kill_switch") else "active"))
        safe = bool(book.state.get("safe_mode"))
        checks.append(Check("safe_mode", not (safe and side.upper() == "BUY"),
                            "off" if not safe else "new buys disabled"))

        # Churn cap. A sell that closes an existing position is exempt: the point
        # is to stop a looping agent from flipping in and out, not to trap it in
        # a position it wants out of.
        done = book.actions_on(date)
        if side.upper() == "BUY":
            checks.append(Check(
                "daily_actions", done < cfg.max_actions_per_day,
                f"{done}/{cfg.max_actions_per_day} fills today",
                structured={"metric": "fills_today", "actual": done,
                            "cap": cfg.max_actions_per_day, "unit": "count",
                            "reason": "churn_cap"},
                detail_zh=f"今日已成交 {done}/{cfg.max_actions_per_day} 笔"))
        else:
            checks.append(Check(
                "daily_actions", True,
                f"{done}/{cfg.max_actions_per_day} (sells exempt)",
                structured={"metric": "fills_today", "actual": done,
                            "cap": cfg.max_actions_per_day, "unit": "count",
                            "exempt": True, "reason": "churn_cap"},
                detail_zh=f"今日已成交 {done}/{cfg.max_actions_per_day} 笔（卖出豁免）"))

        # ``snapshot`` is supplied when the caller priced the order off a live
        # quote because today's bar does not exist yet. Without it a buy at 10:31
        # would be vetted against yesterday's close - and rejected for sitting
        # outside a price band that no longer applies.
        snap = snapshot if snapshot is not None else self.market.snapshot(symbol, date)
        if snap is None:
            checks.append(Check("bar", False, "no bar for symbol/date"))
            return GuardResult(checks, 0.0)
        inst = self.market.instrument(symbol, date)
        # ST 只挡买入。卖出永远放行——股票被 ST 之后持仓必须还能出，
        # 否则止损单和清仓单会被风控锁死。内核的 ``risk`` 与 ``oms``
        # 用的是同一条规则（``tests/kernel/test_st_sell_allowed.py`` 把三层
        # 钉在一起，避免再分叉）。
        st_blocked = inst.is_st and side.upper() == "BUY"
        checks.append(Check("st", not st_blocked,
                            "normal" if not inst.is_st else
                            "ST: buys disabled" if st_blocked else "ST: sells allowed"))
        checks.append(Check("suspended", not snap.suspended,
                            "trading" if not snap.suspended else "suspended"))
        # 北交所只做信息，不做仓。用户口径：可以拿它的数据看盘，选股与买入不碰
        # （30% 涨跌幅、流动性与主板不是一回事）。
        #
        # 挡在 guard 而不是只挡 screen：`screen` 只是"找"，而提案可以从别的路径
        # 进来（直接 propose 一个代码即可）。检查项是唯一绕不过去的地方。
        # 卖出放行——已有的仓位必须永远能出。
        bse = inst.board is Board.BSE
        checks.append(Check(
            "board", not (side.upper() == "BUY" and bse),
            ("北交所只用于看盘，不开新仓（30% 涨跌幅、流动性另算）" if bse
             else inst.board.value)))
        if side.upper() == "BUY" and inst.board is Board.CB:
            # A convertible stops existing on its last session. The store has no
            # redemption schedule (only titles in the announcement feed), so the
            # honest rule is a time fence: no new position inside the final two
            # weeks. Sells stay open — an exit must never be blocked.
            days = self.market.cb_days_to_delist(symbol, date)
            min_sessions = settings.integer("guard.cb_last_buy_sessions") + 1
            checks.append(Check(
                "cb_maturity",
                days is None or days > settings.integer("guard.cb_last_buy_sessions"),
                "no delist date" if days is None
                else f"{days} sessions to delisting "
                     f"(min {min_sessions})",
                structured={"metric": "sessions_to_delisting", "actual": days,
                            "cap": min_sessions, "unit": "sessions",
                            "reason": "cb_last_buy_window"},
                detail_zh=("摘牌日未知" if days is None
                           else f"距摘牌 {days} 个交易日（至少需 {min_sessions}）")))

        band_ok = snap.down_limit <= price <= snap.up_limit
        checks.append(Check(
            "price_band", band_ok,
            f"limit {price} within [{snap.down_limit},{snap.up_limit}]",
            structured={"metric": "limit_price_cny", "actual": float(price),
                        "lower": float(snap.down_limit), "upper": float(snap.up_limit),
                        "unit": "cny"}))

        if side.upper() == "BUY":
            # A breached stop blocks new risk. propose raises 409 OPEN_VIOLATION
            # before this point; re-check here so approve and the book's
            # fill-time preflight cannot slip past a ticket that was priced
            # while no stop was broken. The scan may touch the quote service,
            # and book._preflight has no try/except on the fill path — so any
            # error must fail closed here instead of surfacing as a 500.
            #
            # ``quotes`` is supplied by the fill path, which holds the book lock:
            # it is a cache-only mapping, so this check can never turn into a
            # vendor round trip under that lock. ``None`` (the proposal path)
            # keeps the original "go ask" behaviour, where no lock is held.
            try:
                from .observation import evaluate_stop_gate

                # Two call shapes on purpose: ``quotes is None`` is the original
                # "go ask" path (proposal, no lock held), and keeping that call
                # signature exact means tests and other callers that stub
                # ``evaluate_stop_gate(market, book, day)`` keep working.
                if quotes is None:
                    gate = evaluate_stop_gate(self.market, book, date)
                else:
                    gate = evaluate_stop_gate(self.market, book, date, quotes=quotes)
                breached, unknown = gate["breached"], gate["unknown"]
                blocked = bool(breached or unknown)
                named = ", ".join(sorted(set(breached + unknown))[:4])
                detail = (f"{named} 止损违约/状态未知，不加仓"
                          f"（breached={len(breached)}, unknown={len(unknown)}）"
                          if blocked else "no open breach")
            except Exception as exc:  # noqa: BLE001 - fail closed on the fill path
                blocked = True
                detail = f"stop_gate 无法判定（{type(exc).__name__}）：按违约处理"
            checks.append(Check("stop_gate", not blocked, detail))
            checks.append(Check("equity", equity > 0,
                                f"equity {equity}" if equity > 0
                                else f"equity {equity}: new buys disabled"))
            value = money(D(qty) * price)
            fees = book.cost.buy_fees(value, inst)
            total = money(value + fees)
            # 队列里的买单没有账本订单，也就不冻结现金。只看
            # ``cash_available`` 时，09:26（或昨晚 19:30）排进去的那一张在这一刻
            # 是不存在的——同一笔钱会被再承诺一次，而结算时被取消的是**先下的**
            # 那一张，尽管模型已经被告知"已挂单"。
            queued = book.queued_buy_commitment(exclude=exclude_ticket)
            free = money(account.cash_available - queued)
            checks.append(Check(
                "cash", total <= free,
                f"need {total}, free {free}"
                + (f" ({queued} in queued orders)" if queued > 0 else ""),
                structured={"metric": "cash_required_cny", "actual": float(total),
                            "cap": float(free), "unit": "cny",
                            "queued_cny": float(queued)}))

            # Tuple, not a live view: `preflight` runs under the decision lock,
            # but a fill runs under the book lock, and those are two different
            # locks (see `desk.decisions.lock` vs `desk.book.lock`). A new name
            # bought mid-scan raises "dictionary changed size during iteration" —
            # a 500 on the propose path, which reads as "the desk is down"
            # rather than "retry".
            positions = [p for p in tuple(account.positions.values()) if p.total_qty > 0]
            is_new = symbol not in {p.symbol for p in positions}
            checks.append(Check(
                "max_positions", len(positions) + (1 if is_new else 0) <= cfg.max_positions,
                f"{len(positions)}/{cfg.max_positions}",
                structured={"metric": "positions_after", "actual": len(positions) + (1 if is_new else 0),
                            "cap": cfg.max_positions, "unit": "count"}))

            cap = cfg.max_aggressive_single if sleeve.upper() == "AGGRESSIVE" else (
                cfg.max_core_etf_single if inst.is_etf else cfg.max_core_single)
            pos = account.peek(symbol)
            existing = money(D(pos.total_qty if pos is not None else 0) * price)
            held_pct = float((existing + value) / equity) if equity > 0 else 0.0
            checks.append(Check(
                "single_cap", ratio_within(existing + value, equity, cap),
                _pct_note(held_pct, cap),
                structured={"metric": "single_position_weight", "actual": held_pct,
                            "cap": cap, "unit": "fraction"}))

            sleeve_value = book.sleeve_value(sleeve)
            sleeve_cap = cfg.max_aggressive_weight if sleeve.upper() == "AGGRESSIVE" \
                else cfg.max_core_weight
            sleeve_pct = float((sleeve_value + value) / equity) if equity > 0 else 0.0
            checks.append(Check(
                "sleeve_cap", ratio_within(sleeve_value + value, equity, sleeve_cap),
                _pct_note(sleeve_pct, sleeve_cap),
                structured={"metric": "sleeve_weight", "actual": sleeve_pct,
                            "cap": sleeve_cap, "unit": "fraction"}))

            # One theme is one position: three banks is a bank bet, not diversity.
            #
            # Built from the account rather than ``book.summary()``: summary also
            # computes the queued-cash commitment (one instrument lookup per
            # pending ticket) and the display rows, and this check needs two
            # fields. Measured before the change: every proposal, approval and
            # fill re-ran that work for a number it discarded.
            summary = {
                "equity": str(equity),
                "positions": [
                    {"symbol": pos.symbol,
                     "market_value": str(money(D(pos.total_qty) * pos.last_price))}
                    for pos in tuple(account.positions.values()) if pos.total_qty > 0
                ],
            }
            industry = self.market.industry_of(symbol)
            if industry and industry != "未知":
                iw = industry_weight(summary, self.market, industry, value)
                checks.append(Check("industry_cap", iw <= cfg.max_industry,
                                    _pct_note(iw, cfg.max_industry, prefix=f"{industry} ")))

            if strategy:
                meta = strategy_meta.get(strategy)
                if meta is not None:
                    sv = _strategy_value(book, strategy, price)
                    strategy_pct = (float((sv + value) / equity)
                                    if equity > 0 else 0.0)
                    checks.append(Check("strategy_cap", ratio_within(sv + value, equity,
                                                                    meta.max_weight),
                                        _pct_note(strategy_pct, meta.max_weight,
                                                  prefix=f"{strategy} ")))

            # What this ticket costs before the market does anything. Reported on
            # every buy, enforced only when `min_ticket_bps` is set: the point is
            # to make "this trade needs 68 bps just to break even" visible at the
            # moment the decision is made, not to change what is allowed yet.
            cost_bps = None
            cost_model = getattr(book, "cost", None)
            round_trip = getattr(cost_model, "round_trip", None)
            if value > 0 and callable(round_trip):
                cost_bps = float(round_trip(D(value), inst)) / float(value) * 1e4
            checks.append(Check(
                "cost_bps",
                cfg.min_ticket_bps <= 0 or cost_bps is None or cost_bps <= cfg.min_ticket_bps,
                # 这里算的是**费用**，不含滑点；票据上的 round_trip_cost_bps 含
                # 2×滑点。两个数都叫 round trip，差 20bps 上下，不写清哪一个含
                # 什么，读的人只会以为其中一个算错了（2026-11-02 实测 174.0 与
                # 194.03 同时出现在一张票据上）。
                (f"{cost_bps:.1f} bps round-trip fees (excl. slippage)"
                 if cost_bps is not None else "cost unavailable")
                + (f" (limit {cfg.min_ticket_bps:.0f})" if cfg.min_ticket_bps > 0
                   else " (report only)"),
                structured={"metric": "round_trip_fees_bps", "actual": cost_bps,
                            "cap": cfg.min_ticket_bps, "unit": "bps",
                            "excludes": "slippage", "enforced": cfg.min_ticket_bps > 0},
                detail_zh=(f"往返费用 {cost_bps:.1f} bps（不含滑点）"
                           if cost_bps is not None else "费用数据不可用")))

            adv = self.market.adv_map(date).get(symbol, 0.0)
            adv_ok = bool(adv and adv > 0)
            adv_ratio = float(value) / adv if adv_ok else None
            checks.append(Check(
                "liquidity",
                adv_ok and float(value) / adv <= cfg.max_adv_ratio,
                f"{float(value)/adv:.2%} of ADV20" if adv_ok
                else "ADV20 unavailable: buys disabled",
                structured={"metric": "order_value_over_adv20", "actual": adv_ratio,
                            "cap": cfg.max_adv_ratio, "unit": "fraction",
                            "adv20_cny": float(adv) if adv_ok else None,
                            "reason": "adv20_participation"},
                detail_zh=(f"委托额占 ADV20 的 {adv_ratio:.2%}" if adv_ok
                           else "ADV20 不可用，禁止买入")))

            cash_weight = float(free / equity) if equity > 0 else 1.0
            cash_after = cash_weight - float(total / equity) if equity > 0 else None
            checks.append(Check(
                "min_cash", cash_after >= cfg.min_cash_weight
                if cash_after is not None else False,
                f"cash after {cash_after:.1%}" if cash_after is not None
                else "cash after unavailable: equity <= 0",
                structured={"metric": "cash_weight_after", "actual": cash_after,
                            "cap": cfg.min_cash_weight, "unit": "fraction"}))
        else:
            # ``peek``, not ``position``: a check must not conjure a holding. The
            # guard runs on every proposal, so creating one here put an empty
            # Position into the account for every symbol the model ever asked
            # about — persisted in every snapshot, and reported by
            # ``Ledger.reconcile`` as an unexpected position.
            pos = account.peek(symbol)
            available = pos.available_qty if pos is not None else 0
            total = pos.total_qty if pos is not None else 0
            checks.append(Check(
                "t_plus_one", qty <= available,
                f"sell {qty}, available {available}",
                structured={"metric": "sell_qty", "actual": qty, "cap": available,
                            "unit": "shares"}))
            checks.append(Check("has_position", total > 0, f"holding {total}"))

        daily = book.daily_pnl_pct()
        if side.upper() == "BUY":
            checks.append(Check("daily_loss", daily > cfg.daily_loss_no_new,
                                f"{daily:.2%} > {cfg.daily_loss_no_new:.0%}"))
            checks.append(Check("reduce_only", daily > cfg.daily_loss_reduce_only,
                                f"{daily:.2%} > {cfg.daily_loss_reduce_only:.0%}"))
        dd = book.drawdown()
        if dd <= cfg.drawdown_stop:
            blocked = side.upper() == "BUY"
            checks.append(Check("drawdown", not blocked,
                                f"{dd:.2%} <= {cfg.drawdown_stop:.0%}: stop"
                                if blocked else
                                f"{dd:.2%} <= {cfg.drawdown_stop:.0%}: sells allowed"))
            if blocked:
                mult = 0.0
        elif dd <= cfg.drawdown_half:
            checks.append(Check("drawdown", True, f"{dd:.2%}: half size"))
            mult = min(mult, 0.5)
        else:
            checks.append(Check("drawdown", True, f"{dd:.2%}"))

        return GuardResult(checks, mult)
