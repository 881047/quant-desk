"""Watch the real positions and decide when a human must be told.

Two layers, deliberately separate:

* **rules** — deterministic, testable, no model. Each one answers "has something
  happened that changes what I must decide today", and each one carries the
  numbers it fired on. A notification whose facts cannot be recomputed by hand is
  not an alert, it is an opinion.
* **prose** — the reading, which happens in the quant-desk workspace (see
  ``analyst``). Nothing in this module writes it.

``Alert.ref`` is the only prose a rule may emit, and it is a **quotation**:
``计划：`` the position's own invalidation condition, ``规程：`` an operational
runbook step, ``算式：`` a restatement of arithmetic already in ``facts``. Every
non-empty ``ref`` names its source; a rule that cannot name one has nothing to
quote and leaves it blank.

This replaced a free-form ``why`` field whose nine entries were the module
disagreeing with its own contract: ``auction_breach`` said "先减风险，再谈理由"
under a docstring promising "nothing here recommends a trade", and the mail went
out labelled "未附模型解读" while printing nine readings underneath it. A rule
that cannot stop itself from interpreting will be read as the interpretation, and
then nobody will ask the workspace.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field

from service import settings
from service.kernel.units import CNY_PER_YI

ALERT, WARN, INFO = "alert", "warn", "info"
_SEVERITY_ORDER = {ALERT: 0, WARN: 1, INFO: 2}


@dataclass
class PositionContext:
    """Everything a rule is allowed to look at, gathered once per run."""
    symbol: str
    qty: int
    cost: float                 # fee-inclusive break-even
    entry: float                # raw average entry
    prev_close: float
    last: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    adv20: float = 0.0
    # ``market.history`` stores amount in thousand CNY; live quotes use CNY.
    # Keep the unit explicit so the volume rule does not differ by 1,000×.
    adv20_unit: str = "thousand_cny"
    ma5: float | None = None
    ma20: float | None = None
    up_limit: float = 0.0
    down_limit: float = 0.0
    name: str = ""
    # A live/preopen price for **today**. Kept apart from `last` (the latest
    # daily bar) because the auction line is derived from `last` — comparing
    # `last` against a line built from itself can never fire.
    auction_price: float | None = None
    quote_source: str = ""
    # A level a person pinned in ``configs/stops.json``. Kept beside ``entry``
    # rather than replacing it, because the message still has to say what the
    # position cost.
    stop_override: float | None = None

    @property
    def pct(self) -> float:
        return (self.last / self.prev_close - 1) if self.prev_close else 0.0

    @property
    def stop(self) -> float:
        """The line this position is actually judged against.

        ``configs/stops.json`` wins when it names the symbol. Someone who
        replaced the -5% rule with a hand-set level did it on purpose; a
        monitor that recomputed ``entry × 0.95`` anyway kept mailing a breach
        of a line that had already been retired (002185 on 2026-11-16: the
        book said 9.90, the alert said 14.17).
        """
        if self.stop_override is not None:
            return round(float(self.stop_override), 2)
        return round(self.entry * (1 - settings.num("monitor.hard_stop_pct")), 2)

    @property
    def auction_line(self) -> float:
        return round(self.prev_close * (1 - settings.num("monitor.auction_line_pct")), 2)

    @property
    def adv20_cny(self) -> float:
        if self.adv20_unit == "cny":
            return self.adv20
        if self.adv20_unit == "thousand_cny":
            return self.adv20 * 1000.0
        return 0.0

    @property
    def pnl(self) -> float:
        return round((self.last - self.cost) * self.qty, 2)


@dataclass
class Alert:
    rule: str
    severity: str
    title: str
    symbol: str = ""
    facts: list[str] = field(default_factory=list)
    # A quotation, not a reading: must start with 计划/规程/算式, or stay empty.
    ref: str = ""
    # Extra discriminator for the once-a-day dedupe. A holdings drift is keyed on
    # the account value it observed, so a *second* trade the same day produces a
    # new alert instead of being swallowed as "already sent".
    key_suffix: str = ""

    def key(self, day: str) -> str:
        base = f"{self.rule}|{self.symbol or '-'}|{day}"
        return f"{base}|{self.key_suffix}" if self.key_suffix else base


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


# Every 计划/规程 citation names a clause in a written source, and that source is
# checked in tests. A citation that drifts from its source is worse than none: it
# reads as authoritative while pointing at text that no longer says that. The
# ``playbook`` key is the phrase that must still appear in playbook/*.md; a rule
# whose clause was edited away fails the build instead of quoting a ghost.
CITATIONS: dict[str, dict] = {
    "stop": {
        "tag": "计划",
        # No "建仓时写入" here. The number in this line is ``entry x 0.95`` — the
        # playbook's fixed hard stop — not the stop written on the ticket, which
        # ``stops.py`` / ``observation.py`` / ``duty.py`` read and which may be a
        # tighter ATR distance. Claiming it was written at entry made the mail
        # cite a provenance that did not exist, and two components could then
        # disagree about one position's stop with both sounding authoritative.
        # The wording is *shorter* than what it replaced on purpose: the prompt
        # this card becomes has a hard 2,000-byte ceiling, and the tighter stop,
        # when there is one, is already stated by /observations.
        "text": (f"本仓硬止损 = 跌破 {{stop}}"
                 f"（playbook 的 -{settings.num('monitor.hard_stop_pct'):.0%} 硬止损）"),
        "playbook": f"-{settings.num('monitor.hard_stop_pct'):.0%} 硬止损",
    },
    "auction": {
        "tag": "计划",
        "text": (f"低开超 {settings.num('monitor.auction_line_pct'):.0%}"
                 f" 属于 playbook 的「不隔夜死扛」一档"),
        "playbook": "不隔夜死扛",
    },
    # 系数不写死。0.9 只对主板成立：创业板/科创板是 0.8、北交所 0.7、ST 0.95，
    # 而这条文案是给读者**验算**用的——算式算不出旁边那个数，读者只会怀疑价格。
    "limit_down_price": {"tag": "算式", "text": "跌停价 = 昨收 {prev} × {band}"},
    "stop_distance": {"tag": "算式", "text": "距止损 = 现价/止损 − 1 = {gap:+.2f}%"},
    "reconcile": {"tag": "规程",
                  "text": "分红送股/转股不产生成交；以券商快照为准，补导一次持仓表"},
    "holdings": {"tag": "规程",
                 "text": "股数只在成交/分红送股/转股时变化；对不上就补记一笔，"
                         "否则止损与盈亏会挂在已经不存在的仓位上"},
    "stale": {"tag": "规程", "text": "行情过期时本轮的判断降级为「不知道」"},
}

REF_SOURCES = ("计划：", "规程：", "算式：")


def cite(key: str, **fields) -> str:
    """Build a ``ref`` from the registry, so a tag and its wording cannot drift apart."""
    entry = CITATIONS[key]
    return f"{entry['tag']}：" + entry["text"].format(**fields)


def evaluate(contexts: list[PositionContext], *, day: str, session: str = "intraday",
             reconcile: dict | None = None, quote_age: float | None = None,
             stale_after: float = 900.0) -> list[Alert]:
    """Rules in, alerts out.

    ``day`` and ``session`` do **not** take part in any verdict: every rule fires
    on a price fact (``last`` vs ``down_limit`` / ``stop`` / ``auction_line``), and
    a fact that does not exist yet — no auction print at preopen — is represented
    by ``None`` and skipped. They stay in the signature because the caller stamps
    them into the message and the dedupe key, and because a rule that later needs
    "is the session still open" should not have to change the call shape. If one
    ever does, it must also gain a test: today nothing verifies that a rule fires
    only in the session it is meant for, because no rule looks.
    """
    alerts: list[Alert] = []
    stale = bool(quote_age is not None
                 and (not math.isfinite(quote_age) or quote_age > stale_after))

    for ctx in contexts:
        if stale:
            # An old quote cannot support a new stop/MA/volume conclusion. The
            # single data_stale finding below is the honest result for this run.
            continue
        numeric = (ctx.entry, ctx.prev_close, ctx.last, ctx.amount, ctx.adv20)
        try:
            invalid = (any(not math.isfinite(value) for value in numeric)
                       or min(ctx.entry, ctx.prev_close, ctx.last) <= 0
                       or ctx.amount < 0 or ctx.adv20 < 0)
        except (TypeError, ValueError):
            invalid = True
        if invalid or ctx.adv20_unit not in ("cny", "thousand_cny"):
            alerts.append(Alert(
                rule="data_invalid", severity=WARN, symbol=ctx.symbol,
                title=f"{ctx.symbol} 行情或成本价格无效",
                facts=[f"entry={ctx.entry} prev_close={ctx.prev_close} last={ctx.last}",
                       "缺少有限正价格，本轮不计算止损、竞价或放量规则"]))
            continue
        stop, auction = ctx.stop, ctx.auction_line
        pct_text = f"{ctx.pct * 100:+.2f}%"
        adv20 = ctx.adv20_cny

        if ctx.down_limit and ctx.last <= ctx.down_limit:
            alerts.append(Alert(
                rule="limit_down", severity=ALERT, symbol=ctx.symbol,
                title=f"{ctx.symbol} 跌停 {_fmt(ctx.last)}",
                facts=[f"现价 {_fmt(ctx.last)} = 跌停价 {_fmt(ctx.down_limit)}",
                       f"持仓 {ctx.qty} 股，浮亏 {ctx.pnl:+,.0f} 元（{pct_text}）"],
                # 系数从跌停价本身反推，不按板块前缀猜：这里唯一可信的来源是
                # 交易所给的那个价。
                ref=cite("limit_down_price", prev=_fmt(ctx.prev_close),
                         band=(f"{ctx.down_limit / ctx.prev_close:.2f}"
                               if ctx.prev_close else "?"))))

        elif ctx.last <= stop:
            # 文案必须说清这条线是谁定的：写死的 -5% 和人身钉的价位是两种
            # 纪律，混在一句里会让人以为止损还在原处。
            origin = ("configs/stops.json 钉的价" if ctx.stop_override is not None
                      else f"买入价 {_fmt(ctx.entry)} × "
                           f"{1 - settings.num('monitor.hard_stop_pct'):.2f}")
            alerts.append(Alert(
                rule="stop_breach", severity=ALERT, symbol=ctx.symbol,
                title=f"{ctx.symbol} 跌破止损 {_fmt(stop)}",
                facts=[f"现价 {_fmt(ctx.last)}，止损线 {_fmt(stop)}（{origin}）",
                       f"距止损 {((ctx.last / stop) - 1) * 100:+.2f}%，浮亏 {ctx.pnl:+,.0f} 元"],
                ref=cite("stop", stop=_fmt(stop))))

        elif ctx.auction_price is not None and ctx.auction_price <= auction:
            anchor = auction / (1 - settings.num("monitor.auction_line_pct"))
            alerts.append(Alert(
                rule="auction_breach", severity=WARN, symbol=ctx.symbol,
                title=f"{ctx.symbol} 破竞价线 {_fmt(auction)}",
                facts=[(f"竞价价 {_fmt(ctx.auction_price)}，竞价线 {_fmt(auction)}"
                        f"（昨收 {_fmt(anchor)} × "
                        f"{1 - settings.num('monitor.auction_line_pct'):.2f}）"),
                       (f"低开 {((ctx.auction_price / anchor) - 1) * 100:+.2f}%，"
                        f"浮亏 {ctx.pnl:+,.0f} 元")],
                ref=cite("auction")))

        elif (ctx.last - stop) / stop <= settings.num("monitor.near_stop_pct"):
            alerts.append(Alert(
                rule="near_stop", severity=INFO, symbol=ctx.symbol,
                title=f"{ctx.symbol} 距止损只剩 {((ctx.last / stop) - 1) * 100:.2f}%",
                facts=[f"现价 {_fmt(ctx.last)}，止损 {_fmt(stop)}"],
                ref=cite("stop_distance", gap=(ctx.last / stop - 1) * 100)))

        if ctx.pct <= -0.05:
            alerts.append(Alert(
                rule="big_drop", severity=WARN, symbol=ctx.symbol,
                title=f"{ctx.symbol} 单日 {pct_text}",
                facts=[f"昨收 {_fmt(ctx.prev_close)} → 现价 {_fmt(ctx.last)}",
                       f"成交额 {ctx.amount / CNY_PER_YI:.2f} 亿（20 日均 {adv20 / CNY_PER_YI:.2f} 亿）"]))

        if adv20 and ctx.pct <= -0.03 and ctx.amount >= 1.5 * adv20:
            alerts.append(Alert(
                rule="volume_breakdown", severity=WARN, symbol=ctx.symbol,
                title=f"{ctx.symbol} 放量下跌 {pct_text}",
                facts=[(f"成交额 {ctx.amount / 1e8:.2f} 亿 = 20 日均量的 "
                        f"{ctx.amount / adv20:.1f} 倍")]))

        if ctx.ma5 and ctx.prev_close >= ctx.ma5 > ctx.last:
            alerts.append(Alert(
                rule="ma5_break", severity=INFO, symbol=ctx.symbol,
                title=f"{ctx.symbol} 跌破 MA5 {_fmt(ctx.ma5)}",
                facts=[(f"现价 {_fmt(ctx.last)}，MA5 {_fmt(ctx.ma5)}，"
                        f"昨收 {_fmt(ctx.prev_close)}")]))

    if reconcile and not reconcile.get("ok") and reconcile.get("mismatches"):
        rows = reconcile["mismatches"]
        alerts.append(Alert(
            rule="reconcile_gap", severity=WARN, symbol="",
            title=f"实盘账实不符：{len(rows)} 处",
            facts=[(f"{r['symbol']} 流水 {r['ledger_qty']} vs 券商 "
                    f"{r['broker_qty']}（差 {r['delta']:+d}）") for r in rows[:5]],
            ref=cite("reconcile")))

    if stale:
        age_text = (f"{quote_age / 60:.0f} 分钟" if math.isfinite(quote_age)
                    else "非有限值")
        alerts.append(Alert(
            rule="data_stale", severity=INFO, symbol="",
            title=f"行情已过期 {age_text}",
            facts=[(f"最新报价年龄 {quote_age} 秒，阈值 {stale_after:.0f} 秒；"
                    "本轮价格规则均为 unknown")],
            ref=cite("stale")))

    return sorted(alerts, key=lambda a: (_SEVERITY_ORDER.get(a.severity, 9), a.symbol))


# ------------------------------------------------------------------ rendering
_ICON = {ALERT: "🔴", WARN: "🟠", INFO: "🔵"}


def render_text(alerts: list[Alert], *, day: str, stamp: str,
                contexts: list[PositionContext], note: str | None = None) -> str:
    lines = [f"quant-desk 持仓提醒 · {day} {stamp}", ""]
    for alert in alerts:
        lines.append(f"{_ICON.get(alert.severity, '·')} [{alert.severity}] {alert.title}")
        for fact in alert.facts:
            lines.append(f"    · {fact}")
        if alert.ref:
            lines.append(f"    引用 {alert.ref}")
        lines.append("")
    if note:
        lines += ["—— 解读 ——", note.strip(), ""]

    if contexts:
        lines += ["持仓与纪律位", ""]
        for ctx in contexts:
            lines.append(
                f"  {ctx.symbol} {ctx.qty} 股 现价 {_fmt(ctx.last)} "
                f"(个股 {ctx.pct * 100:+.2f}%) 成本 {_fmt(ctx.cost, 3)} "
                f"盈亏 {ctx.pnl:+,.0f} | 止损 {_fmt(ctx.stop)} "
                f"竞价线 {_fmt(ctx.auction_line)}")
        lines.append("")

    lines += [
        "—— 说明 ——",
        "触发项与纪律位由规则层计算；「引用」是计划/规程/算式的原样复述，不是判断。",
        "这条邮件不给买卖建议：解读在 quant-desk 工作区里做，执行与否由你决定，",
        "账本会记录你实际做了什么。",
    ]
    return "\n".join(lines)


def render_html(alerts: list[Alert], *, day: str, stamp: str,
                contexts: list[PositionContext], note: str | None = None) -> str:
    color = {ALERT: "#b00020", WARN: "#a15c00", INFO: "#0b5cad"}
    safe_day, safe_stamp = html.escape(str(day)), html.escape(str(stamp))
    blocks = []
    for alert in alerts:
        facts = "".join(f"<li>{html.escape(str(f))}</li>" for f in alert.facts)
        blocks.append(
            f"<div style='margin:0 0 14px;padding:10px 12px;border-left:3px solid "
            f"{color.get(alert.severity, '#888')};background:#fafafa'>"
            f"<div style='font-weight:600;color:{color.get(alert.severity, '#333')}'>"
            f"{_ICON.get(alert.severity, '·')} {html.escape(str(alert.title))}</div>"
            f"<ul style='margin:6px 0 0 18px;padding:0;font-size:13px'>{facts}</ul>"
            + (f"<div style='margin-top:6px;font-size:13px;color:#666'>"
               f"引用 {html.escape(str(alert.ref))}</div>"
               if alert.ref else "")
            + "</div>")

    rows = "".join(
        f"<tr><td>{html.escape(str(c.symbol))}</td><td align='right'>{c.qty}</td>"
        f"<td align='right'>{_fmt(c.last)}</td>"
        f"<td align='right'>{c.pct * 100:+.2f}%</td>"
        f"<td align='right'>{_fmt(c.cost, 3)}</td>"
        f"<td align='right'>{c.pnl:+,.0f}</td>"
        f"<td align='right'>{_fmt(c.stop)}</td>"
        f"<td align='right'>{_fmt(c.auction_line)}</td></tr>"
        for c in contexts)

    note_html = (f"<h3 style='font-size:14px;margin:18px 0 6px'>解读</h3>"
                 f"<p style='font-size:13px;line-height:1.6;color:#333;white-space:pre-wrap'>"
                 f"{html.escape(str(note))}</p>"
                 if note else "")

    return f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#222">
<h2 style="font-size:16px;margin:0 0 12px">quant-desk 持仓提醒 · {safe_day} {safe_stamp}</h2>
{''.join(blocks)}
{note_html}
<h3 style="font-size:14px;margin:18px 0 6px">持仓与纪律位</h3>
<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px">
<tr style="background:#f0f0f0"><th>代码</th><th>股数</th><th>现价</th><th>个股今日</th>
<th>成本</th><th>盈亏</th><th>止损</th><th>竞价线</th></tr>
{rows}
</table>
<p style="margin-top:16px;font-size:12px;color:#666">
触发项与纪律位由规则层计算；「引用」是计划/规程/算式的原样复述，不是判断。
这条邮件不给买卖建议：解读在 quant-desk 工作区里做，执行与否由你决定。</p>
</div>"""
