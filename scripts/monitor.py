"""Watch the real positions and mail a human when something changes.

    python scripts/monitor.py --once            # 盘中轮询用这个（去重）
    python scripts/monitor.py --once --dry-run  # 只看会发什么，不发信，不占去重名额
    python scripts/monitor.py --test-mail       # 验证 QQ 邮箱配置
    python scripts/monitor.py --digest          # 收盘摘要（每天都发一条）

The split matters: `--once` is event-driven and deduplicated, so it can run every
five minutes; `--digest` is a heartbeat that proves the pipe is alive even on a
day when nothing happened.

No rule here recommends a trade. Each alert carries the numbers it fired on and
the plan line it corresponds to; the decision stays with the person, and the
ledger records what they actually did.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.kernel.clock import SHANGHAI
from service.notify import alert_log
from service.notify.mail import MailDeliveryUnknown, MailNotConfigured, load_config, send_mail
from service.trading import analyst, monitor
from service.trading.external import SCHEMA, ExternalStore, reconcile
from service.trading.external_stops import load_overrides

DB = Path(os.getenv("EXTERNAL_DB") or (ROOT / "data" / "external.sqlite"))
# 路径归 ``alert_log`` 定：它既写这个文件（本脚本），也读它（/market/frame），
# 两处各写一遍 = 两个目录，而分叉那天只有一边会写。
REPORTS = alert_log.default_base()


class _ReadOnlyExternalStore(ExternalStore):
    """Dry-run reads never create a database or migrate its schema."""

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def _conn(self) -> sqlite3.Connection:
        if self.path.exists():
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        else:
            # A dry-run before the first real sync still has a valid empty book.
            conn = sqlite3.connect(":memory:")
            conn.executescript(SCHEMA)
        conn.row_factory = sqlite3.Row
        return conn


def _open_store(*, dry_run: bool) -> ExternalStore:
    return _ReadOnlyExternalStore(DB) if dry_run else ExternalStore(DB)


def _release_undelivered(store: ExternalStore, keys: list[str]) -> None:
    """Release only unsent claims; the store preserves delivered evidence."""
    store.release_alerts(keys, note="delivery failed before acceptance")


def _session(now: datetime) -> str:
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 30:
        return "preopen"
    if 11 * 60 + 30 <= minutes < 13 * 60:
        # 午休不是盘中。行情源在 11:30 后不再出 tick，报价自然 `is_realtime=false`、
        # `age=None`；把它当盘中，就会在每天 11:35 得到一条「行情未更新」的假告警
        # 和一份假的解读交接（2026-11-06 与 2026-11-09 各留了一条，字面完全一样）。
        return "lunch"
    if minutes <= 15 * 60:
        return "intraday"
    return "close"


def _contexts(desk, held, session: str) -> tuple[list[monitor.PositionContext], str | None]:
    """Build one context per holding. Returns (contexts, stale_reason).

    The price comes from the live feed first. It used to come only from
    `market.snapshot`, which reads the daily bar — and today's bar does not exist
    until 17:30, so every intraday run between the open and the daily job fell
    into "行情未更新" and judged nothing at all. The five-minute monitor was
    running all morning and never looked at a single stop.
    """
    day = desk.market.latest_bar_date()
    today = datetime.now(SHANGHAI).strftime("%Y%m%d")
    contexts: list[monitor.PositionContext] = []
    stale: str | None = None

    live = desk.quotes(sorted(held)) if held else {}
    # 人身钉的价位优先于 -5% 规则，与 /stops 接口同源（configs/stops.json）。
    # 这个脚本曾自己算 entry×0.95，于是同一只票在两个地方有两条止损线：
    # 账本说 9.90、告警说 14.17，而人只会读到告警那一条。
    overrides = load_overrides(ROOT)

    for symbol, position in sorted(held.items()):
        quote = live.get(symbol)
        snap = None
        if quote is None:
            try:
                snap = desk.market.snapshot(symbol)
            except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                snap = None

        if quote is not None:
            if not quote.is_realtime and session == "intraday":
                # A stored bar dressed as a quote would trigger a stop at a price
                # that is not trading; say so instead of judging.
                stale = (
                    f"{symbol} 的报价不是实时的（source={quote.source}，"
                    f"age={quote.age_seconds}）；盘中不用旧价判断今天的纪律"
                )
                return [], stale
            last = float(quote.last)
            prev_close = float(quote.pre_close)
            open_ = float(quote.open)
            high = float(quote.high)
            low = float(quote.low)
            volume = float(quote.volume)
            amount = float(quote.amount)
            up_limit = float(quote.up_limit)
            down_limit = float(quote.down_limit)
            source = str(quote.source)
            # The auction line is only about the opening print. Comparing an
            # intraday price to it produced "破竞价线 / 低开 x%" at 14:00, which
            # describes something that did not happen.
            auction_price = last if session == "preopen" else None
        elif snap is not None:
            if snap.trade_date != today and session == "intraday":
                stale = (
                    f"最新行情停在 {snap.trade_date}，今天 {today} 还没有数据；"
                    "盘中不做判断，避免用昨天的价格触发今天的纪律"
                )
                return [], stale
            last = float(snap.last)
            prev_close = float(snap.pre_close)
            open_ = float(snap.open)
            high = float(snap.high)
            low = float(snap.low)
            volume = float(snap.volume)
            amount = float(snap.amount)
            up_limit = float(snap.up_limit)
            down_limit = float(snap.down_limit)
            source = str(snap.source)
            auction_price = None
        else:
            stale = f"{symbol} 取不到行情"
            continue

        history = desk.market.history(symbol, day, 30) if day else None
        ma5 = adv20 = None
        if history is not None and not history.empty:
            closes = history["close"].astype(float)
            if len(closes) >= 5:
                ma5 = float(closes.tail(5).mean())
            if "amount" in history.columns and len(history) >= 5:
                adv20 = float(history["amount"].astype(float).tail(20).mean())

        pinned = overrides.get(symbol) or {}
        contexts.append(
            monitor.PositionContext(
                symbol=symbol,
                qty=position.qty,
                cost=position.cost,
                entry=position.avg_price,
                prev_close=prev_close,
                last=last,
                open=open_,
                high=high,
                low=low,
                volume=volume,
                amount=amount,
                adv20=adv20 or 0.0,
                ma5=ma5,
                up_limit=up_limit,
                down_limit=down_limit,
                auction_price=auction_price,
                quote_source=source,
                stop_override=float(pinned["stop"]) if pinned.get("stop") else None,
            )
        )
    return contexts, stale


# 人格决定问哪个窗口。1-2 日的仓位不该由 20 日分布来校准——那是另一个问题的答案，
# 而且它会答得很自信。数字必须来自 state.HORIZONS，人格只能挑，不能自己造。
PERSONA_HORIZONS = {
    "hot_money": (1,),
    "trader": (2, 5),
    "retail": (5, 20),
}
# 只有游资的问题是关于涨停结构的；给另外两个人格放这段是噪声。
PERSONA_TAPE = ("hot_money",)


def _persona(desk) -> str:
    """当前人格。取不到就当散户——窗口取小的那一侧，不猜一个更激进的。"""
    try:
        return str(desk.paper.persona_current().get("current") or "retail")
    except Exception:  # noqa: BLE001 - 背景的窗口选择不该让提醒停摆
        return "retail"


def _last_session(desk) -> str | None:
    """最后一个有日线的交易日：3 毫秒，来自 registry 的 cover_end。

    没有它，两个缓存都得先读一遍全量日线才能判断自己是否过期——那是 16M 行，
    而这正是缓存本来要避免的事。
    """
    try:
        return desk.market.latest_bar_date()
    except Exception:  # noqa: BLE001 - 取不到就让缓存走全量检查
        return None


def _tape_background(desk, day: str) -> dict:
    """涨停结构在什么位置，以及「次日买封住的票、第三日卖」在这个位置之后怎样。

    数据是**推导的**：炸板 = 日线高点触及涨停价、收盘没封住。上游的炸板池只留一年
    （243 个交易日），所以 P35 在这里写的是「未建」；`stk_limit` 从 2010 年起给了精确
    涨停价，于是同一条问题有 4,081 个交易日可答。

    推导的序列与上游池子**水平不可互换**（封板率中位比 0.94、涨停家数 1.15），
    所以只用分位分桶。分桶本身也不是完全不动的：同窗口下与上游同桶 85%（封板率）
    / 87%（涨停家数），**尾档 95%、中档 63%**——这和上游家数无关，是 33% 分位附近
    的日子两边都能翻。所以档位落中档时，卡片会多说一句它有多不稳。
    """
    out: dict = {}
    try:
        from service.research import tape_state

        t0 = _last_session(desk)
        tape = tape_state.cached_tape(desk.data, last=t0)
        block = tape_state.block(tape, day, calibration=tape_state.load_calibration())
    except Exception:  # noqa: BLE001 - 背景可缺，事实不可缺
        block = {"ok": False}

    if block.get("ok"):
        seal, up = block.get("seal_pct"), block.get("up_pct")
        head = []
        if block.get("seal_rate") is not None:
            head.append(
                f"封板率 {block['seal_rate']:.0%}（{seal:.0%} 分位）"
                if seal is not None
                else f"封板率 {block['seal_rate']:.0%}"
            )
        if block.get("up_count") is not None:
            head.append(
                f"涨停 {block['up_count']} 家（{up:.0%} 分位）"
                if up is not None
                else f"涨停 {block['up_count']} 家"
            )
        if head:
            out["涨停结构"] = "·".join(head)
        # 中档是唯一需要附注的位置：尾档与上游同桶 95%，中档只有 63%。
        # 不平铺这句，是因为尾档的日子不该为它付字节，也不该被它稀释。
        if "mid" in str(block.get("bucket", "")).split("|")[0]:
            cal = (block.get("calibration") or {}).get("seal") or {}
            if cal.get("n"):
                # 只留事实：说「两侧都可能翻」，不说「不要当作明确读数」。
                # 同一件事，前者可以被引用，后者只是一句叮嘱，而叮嘱会被跳过。
                out["档位把握"] = (
                    f"封板率处中档边界（与上游同桶 {cal['mid_agree']:.0%}），两侧都可能翻"
                )
        dist = block.get("fwd_seal2d") or {}
        recent = block.get("recent") or {}
        recent_ci = recent.get("episode_ci") or {}
        if dist.get("n"):
            # 近端在前、全样本在后，并指出全样本的钱是哪一段挣的。
            # 近端报**段均 + 区间**而不是日均点估计：`low|low` 近三年日均 −0.28%、
            # 段均 −0.09%，两者的区间都横跨零。点估计会把它读成一个方向。
            if recent.get("usable") and recent_ci:
                head = (
                    f"近 3 年段均 {recent_ci['mean']:+.1%}"
                    f"[{recent_ci['ci_low']:+.1%}~{recent_ci['ci_high']:+.1%}]"
                    f"（{recent_ci['n']} 段）；"
                )
            else:
                # usable 要求段数 ≥ MIN_EPISODES(30)，而区间只要 ≥10 段，
                # 所以走不到「可用但没区间」那一支——要么两者都有，要么都不足。
                head = (
                    f"近 3 年样本不足（n={recent.get('fwd', {}).get('n', 0)}/"
                    f"{recent.get('episodes', 0)} 段）；"
                )
            dom = block.get("era_dominant") or {}
            if dom.get("share", 0) >= 0.4:
                tail = (
                    f"全历史 {dist['mean']:+.1%}，其中 {dom['era']} 贡献 "
                    f"{dom['share']:.0%}（该段 {dom['mean']:+.1%}）"
                )
            else:
                tail = f"全历史 {dist['mean']:+.1%}（各段分摊接近）"
            out["之后两日"] = f"买当天封住的票、隔日收盘卖 — {head}{tail}，未扣成本"
        return out

    # 推导线拿不到时才回落到上游——只报位置，并说明为什么没有后半句。
    try:
        from service.trading import tape

        hist = tape.limit_history(desk, day)
    except Exception:  # noqa: BLE001
        return {}
    if not hist.get("ok"):
        return {}
    up = hist.get("limit_up") or {}
    seal = hist.get("seal_rate") or {}
    parts = []
    if up.get("today") is not None:
        parts.append(f"涨停 {up['today']} 家（{up.get('percentile', 0):.0%} 分位）")
    if seal.get("today") is not None:
        parts.append(
            f"封板率 {seal['today']:.0%}（{seal.get('percentile', 0):.0%} 分位"
            f"／{seal.get('sessions')} 日）"
        )
    if not parts:
        return {}
    return {
        "涨停结构": "、".join(parts),
        "之后两日": "推导序列不可用，只剩上游位置；此处不报之后会怎样",
    }


def _state_background(desk, day: str, persona: str = "retail") -> dict:
    """按人格取窗口的两三行基础概率，给问题卡当校准用。

    这一段可以缺。取不到状态序列就不写背景，而不是让一条止损提醒因为缓存文件被锁
    住而发不出去——所以这里吞掉异常。代价是背景为空，收益是事实照发。
    """
    horizons = PERSONA_HORIZONS.get(persona, PERSONA_HORIZONS["retail"])
    try:
        from service.research import state as state_mod

        series = state_mod.cached_state(desk.data, last=_last_session(desk))
        block = state_mod.block(series, day, horizons=horizons)
        if not block.get("ok") and len(series):
            # 周末/非交易日：用最后一个有数据的交易日，并在口径里写明是哪一天。
            block = state_mod.block(series, str(series.index.max()), horizons=horizons)
    except Exception:  # noqa: BLE001 - 见 docstring：背景可缺，事实不可缺
        return {}
    if not block.get("ok"):
        return {}

    hist = block.get("history") or {}
    cells = block.get("state") or {}
    breadth = (cells.get("breadth20") or {}).get("pct")
    ret20 = (cells.get("ew_ret_20d") or {}).get("pct")

    status = str(block.get("bucket") or "?")
    if breadth is not None and ret20 is not None:
        status += f"（宽度分位 {breadth:.0%}、20 日收益分位 {ret20:.0%}）"
    out: dict = {"市场状态": status}
    for h in horizons:
        dist = hist.get(f"fwd_{h}d") or {}
        if not dist.get("n"):
            continue
        edge = hist.get(f"edge_{h}d")
        tail = f"，较平均日 {edge:+.1%}" if edge is not None else ""
        out[f"该状态之后 {h} 日"] = (
            f"p25 {dist['p25']:+.1%} / p50 {dist['p50']:+.1%} / p75 {dist['p75']:+.1%}，"
            f"胜率 {dist['win']:.0%}（n={dist['n']}，{hist.get('episodes')} 段{tail}）"
        )
    if persona in PERSONA_TAPE:
        out.update(_tape_background(desk, day))
    out["口径"] = f"分位与分布截至 {block.get('as_of')}；前向收益自次日开盘计"
    return out


def _interpret(
    alerts, contexts, *, background: dict | None = None, dry_run: bool = False
) -> tuple[str | None, str, Path | None]:
    """Rules first, then the interpretation layer, then the mail.

    The handoff file is written before either backend runs, so a failure to
    interpret still leaves the facts where a human or the desktop agent can pick
    them up. A dry-run renders the same message without leaving a pending task.
    """
    if not alerts:
        return None, "no_alerts", None
    handoff = None
    if not dry_run:
        prompt = analyst.build_prompt(alerts, contexts, background=background)
        handoff = analyst.write_handoff(alerts, contexts, prompt, source="monitor")
    prefer = (os.getenv("ANALYST") or "").strip() or None
    text, source = analyst.note(alerts, contexts, prefer=prefer)
    if text is None:
        if dry_run:
            text = "本次只发事实；dry-run 不创建解读交接。"
        else:
            # Designed path, not a failure: the handoff is how the workspace agent
            # gets asked. `handoff.py answer` sends the second mail with the prose.
            text = (
                f"本次只发事实，解读随后补发。问题已写入 "
                f"data/external/handoff/{handoff.stem}.json：在 quant-desk 工作区用 "
                f"quant_alert_handoff 取出，用三句话回答（每句引用一个数字），"
                f"答案会作为第二封邮件发出。"
            )
    return text, source, handoff


def _account_alerts(contexts, *, session: str, store: ExternalStore | None = None) -> list:
    """Compare the account totals **you typed** with what the book says.

    不读客户端。账户数字由你自己填（面板「同步账户」），这里只拿你填的那份和
    当前持仓市值比。没填过就不比——静默比编一个数要好，而且一个未被要求去读
    窗口的监控进程，本来也不该在盘中每五分钟去摸一次别人的窗口。

    Only meaningful when both sides describe the same moment: intraday and at the
    close. Before the open the panel still shows yesterday's settlement and our
    quotes are either yesterday's close or an auction print — comparing those two
    produces a number that looks like a discrepancy and is not.

    同一条件也管日期：账户数字必须是**今天的**。11-05 的市值由 11-05 的价格算，
    拿它减今天的实时市值，差额完全由行情决定，而措辞却是「可能有买入没记账」。
    """
    from service.trading import account_check

    if session == "preopen":
        return []

    store = store or ExternalStore(DB)
    today = datetime.now(SHANGHAI).strftime("%Y%m%d")
    day_row = next(
        (row for row in store.days(5) if str(row.get("as_of")) == today), None
    )
    if not day_row:
        # 账户数字是某一天收盘的市值，用那天的价格算出来；今天的持仓市值用的是
        # 现在的价格。两者相减，差额完全由行情决定——11-05 的数字（2994，按
        # 14.97 算）拿来比 11-06 和 11-09 的实时市值，各报了一次 +78 / +90，
        # 措辞都是「可能有买入（或送股）没有记进账本」，两次都是假的。
        # 股数对不对由 ``reconcile`` 按持仓快照核，与这条市值比对无关。
        latest = next(iter(store.days(5)), None)
        as_of = str((latest or {}).get("as_of") or "无")
        print(f"[account] 未比对：账户数字是 {as_of} 的，不是今天的（面板 → 同步账户）")
        return []

    cash = float(day_row.get("cash") or 0)
    equity = float(day_row.get("equity") or 0)
    panel = {
        "ok": True,
        "total": equity,
        "cash": cash,
        "market_value": float(day_row.get("market_value") or 0),
        "cash_ratio": (cash / equity) if equity else 0.0,
        "source": str(day_row.get("source") or "manual"),
    }

    book_value = sum(position.qty * (position.last or 0) for position in contexts)
    fresh = bool(contexts) and all(
        str(position.quote_source or "daily") not in ("daily", "") for position in contexts
    )
    if not contexts:
        fresh = True

    result = account_check.compare(
        panel=panel, book_value=book_value, positions=len(contexts), quotes_fresh=fresh
    )
    if not result["ok"]:
        print(f"[account] 未比对：{result['reason']}")
        return []

    if result["verdict"] == "match":
        print(f"[account] {result['message']}")
        return []

    return [
        monitor.Alert(
            rule="holdings_drift",
            severity=monitor.WARN,
            title=f"持仓与账户对不上（差 {result['diff']:+,.0f} 元）",
            facts=[
                result["message"],
                (
                    f"账户总资产 {panel.get('total', 0):,.2f} · 可用 "
                    f"{panel.get('cash', 0):,.2f} · 持仓 {result['positions']} 只"
                ),
                f"容差 {result['tolerance']:,.0f} 元（2% 或 30 元取大者）",
            ],
            ref=monitor.cite("holdings"),
            key_suffix=f"mv={result['panel_value']:,.0f}",
        )
    ]


def cmd_once(args) -> int:
    from service.trading import build_desk

    store = _open_store(dry_run=args.dry_run)
    # The manual snapshot, not the ledger: the monitor watches what you say you
    # hold. A fill you forgot to type changes the ledger and nothing else — and
    # watching the ledger would then quietly stop watching the position.
    held = store.holding_book()
    now = datetime.now(SHANGHAI)
    day = now.strftime("%Y%m%d")
    session = _session(now)

    desk = None
    if not held:
        # An empty book is only quiet if the account is empty too. If the panel
        # shows market value while the ledger holds nothing, the trade that
        # emptied it — and whatever replaced it — was never recorded, and every
        # number computed from here on is about shares that do not exist.
        alerts = _account_alerts([], session=session, store=store)
        if not alerts:
            print("[monitor] 没有实盘持仓，账户也没有市值，不需要监控")
            return 0
        contexts, stale = [], None
    else:
        desk = build_desk(ROOT)
        contexts, stale = _contexts(desk, held, session)
        if stale:
            alerts = [
                monitor.Alert(
                    rule="data_stale",
                    severity=monitor.WARN,
                    title="行情未更新，本轮跳过判断",
                    facts=[stale],
                    ref=monitor.cite("stale"),
                )
            ]
        else:
            alerts = monitor.evaluate(
                contexts, day=day, session=session, reconcile=reconcile(store)
            )
            alerts += _account_alerts(contexts, session=session, store=store)

    if not alerts:
        print(f"[monitor] {now:%H:%M} 无触发（{len(contexts)} 只）")
        return 0

    claimed = []
    if args.force or args.dry_run:
        fresh = alerts
    else:
        fresh = []
        for alert in alerts:
            if store.claim_alert(
                key=alert.key(day),
                day=day,
                rule=alert.rule,
                symbol=alert.symbol,
                severity=alert.severity,
                title=alert.title,
            ):
                fresh.append(alert)
                claimed.append(alert)
    if not fresh:
        print(f"[monitor] {now:%H:%M} {len(alerts)} 项已发过，跳过")
        return 0

    claimed_keys = [alert.key(day) for alert in claimed]
    try:
        background = _state_background(desk, day, _persona(desk)) if desk is not None else {}
        note, source, handoff = _interpret(
            fresh, contexts, background=background, dry_run=args.dry_run
        )
        stamp = now.strftime("%H:%M")
        print(
            f"[ok] 规则命中 {len(fresh)} 项；解读来源 {source}；"
            f"交接 {handoff.name if handoff else '-'}"
        )
        text = monitor.render_text(fresh, day=day, stamp=stamp, contexts=contexts, note=note)
        html = monitor.render_html(fresh, day=day, stamp=stamp, contexts=contexts, note=note)
        subject = f"[持仓提醒] {fresh[0].title}" + (
            f" 等 {len(fresh)} 项" if len(fresh) > 1 else ""
        )
    except Exception as exc:  # noqa: BLE001 - every pre-send failure must release claims
        _release_undelivered(store, claimed_keys)
        print(f"[fail] 渲染提醒失败：{type(exc).__name__}: {exc}")
        return 2

    if args.dry_run:
        print(text)
        print(f"\n[dry-run] 会发送 {len(fresh)} 项，主题：{subject}")
        return 0

    try:
        result = send_mail(subject, text, html)
    except MailDeliveryUnknown as exc:
        # Keep the dedupe key: the server may already have accepted this mail.
        print(f"[unknown] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - delivery failure must not consume the alert
        _release_undelivered(store, claimed_keys)
        print(f"[fail] {exc}")
        return 2

    store.mark_alert_sent([a.key(day) for a in fresh])
    _archive(day, fresh, contexts, note)
    print(f"[sent] {result['to']} · {subject}")
    return 0


def cmd_digest(args) -> int:
    from service.trading import build_desk

    # A digest is a heartbeat, not an alert: if mail is not configured yet the
    # run says so and exits clean, because nothing is being lost. `--once`
    # makes the opposite choice on purpose — a breach that cannot be delivered
    # must fail loudly.
    if not args.dry_run and not load_config().configured:
        print("[skip] 邮件未配置，摘要跳过（配置后再跑 `--test-mail` 验证）")
        return 0

    store = _open_store(dry_run=args.dry_run)
    # The manual snapshot, not the ledger: the monitor watches what you say you
    # hold. A fill you forgot to type changes the ledger and nothing else — and
    # watching the ledger would then quietly stop watching the position.
    held = store.holding_book()
    now = datetime.now(SHANGHAI)
    day = now.strftime("%Y%m%d")

    desk = None
    if not held:
        drift = _account_alerts([], session="close", store=store)
        if not drift:
            print("[digest] 没有实盘持仓，账户也没有市值")
            return 0
        contexts, stale = [], None
        alerts = drift
    else:
        desk = build_desk(ROOT)
        contexts, stale = _contexts(desk, held, "close")
        alerts = (
            monitor.evaluate(contexts, day=day, session="close", reconcile=reconcile(store))
            if contexts
            else []
        )
        if not stale:
            alerts += _account_alerts(contexts, session="close", store=store)

    if stale:
        alerts.insert(
            0,
            monitor.Alert(
                rule="data_stale",
                severity=monitor.WARN,
                title="行情未更新",
                facts=[stale],
                ref=monitor.cite("stale"),
            ),
        )

    digest_bg = _state_background(desk, day, _persona(desk)) if desk is not None else {}
    note, source, handoff = _interpret(alerts, contexts, background=digest_bg, dry_run=args.dry_run)
    stamp = now.strftime("%H:%M")
    text = monitor.render_text(alerts, day=day, stamp=stamp, contexts=contexts, note=note)
    html = monitor.render_html(alerts, day=day, stamp=stamp, contexts=contexts, note=note)
    subject = f"[收盘摘要] {day} {len(contexts)} 只 · {len(alerts)} 项触发"

    if args.dry_run:
        print(text)
        return 0
    try:
        send_mail(subject, text, html)
    except MailDeliveryUnknown as exc:
        # The server may already have the digest. Say so instead of reporting a
        # plain failure that a later run would happily repeat.
        print(f"[unknown] {exc}")
        return 2
    except (MailNotConfigured, RuntimeError) as exc:
        print(f"[fail] {exc}")
        return 2
    _archive(day, alerts, contexts, note)
    print(f"[sent] {subject}")
    return 0


def cmd_test_mail(args) -> int:
    cfg = load_config()
    if not cfg.configured:
        print("[fail] 邮件未配置。在 .env 里补：")
        print("  QQMAIL_USER=你的QQ邮箱@qq.com")
        print("  QQMAIL_AUTH_CODE=授权码（QQ邮箱 设置→账户→POP3/SMTP服务→生成授权码）")
        print("  QQMAIL_TO=收件邮箱@qq.com")
        return 2
    try:
        result = send_mail(
            "[quant-desk] 邮件通道自检",
            "如果你收到这条，说明持仓提醒可以发出来了。\n\n"
            "接下来的规则：破止损/破竞价线/跌停/单日跌 5%/放量下跌/跌破 MA5/账实不符，"
            "每项每天最多发一次。\n\n这条是自检，不含任何判断。",
        )
    except (MailNotConfigured, RuntimeError) as exc:
        print(f"[fail] {exc}")
        return 2
    print(f"[ok] 已发送到 {result['to']}（{result['host']}）")
    return 0


def _archive(
    day: str, alerts: list[monitor.Alert], contexts: list[monitor.PositionContext], note: str | None
) -> None:
    rows = []
    for alert in alerts:
        rows.append(
            {
                "ts": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                "rule": alert.rule,
                "severity": alert.severity,
                "symbol": alert.symbol,
                "title": alert.title,
                "facts": alert.facts,
                "note": note,
            }
        )
    # 行格式归 ``alert_log`` 管：这份记录的另一个读者是 /market/frame，
    # 在这里再拼一遍字典就是给同一份文件写第二份 schema。
    alert_log.append(day, rows, base=REPORTS, scope="external")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实盘持仓监控与提醒")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="跑一轮，只在有新触发时发信")
    group.add_argument("--digest", action="store_true", help="收盘摘要，每天都发")
    group.add_argument("--test-mail", dest="test_mail", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="忽略去重")
    args = parser.parse_args(argv)

    if args.test_mail:
        return cmd_test_mail(args)
    if args.digest:
        return cmd_digest(args)
    # 心跳在**跑完之后**写。崩溃、被杀、卡住都留不下"跑过了"的痕迹，
    # 于是 /market/frame 上那个秒数会一直涨——这正是读者需要看到的信号。
    rc = cmd_once(args)
    if not args.dry_run:
        alert_log.beat(
            datetime.now(SHANGHAI).strftime("%Y%m%d"),
            scope="external",
            outcome="ok" if rc == 0 else f"exit {rc}",
            base=REPORTS,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
