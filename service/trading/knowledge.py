"""Kernel knowledge: what this system knows, and how sure it is.

This module is the index of the kernel's mind. It does not decide anything and it
does not forecast. It answers four kinds of question the model actually asks:

    ledger     哪些结构已经被证伪？用什么数字证伪的？
    regime     每种市场状态历史上给过什么？
    crowding   这只票现在是不是人多的地方？
    position   我手上这一笔的处境到底怎样？

Every answer carries ``provenance`` (where the numbers came from, so a claim can
be reproduced) and ``caveats`` (what would make the number wrong). A knowledge
layer that hides its own error bars is worse than no knowledge layer, because the
model will treat it as ground truth.

The falsification ledger is the most valuable object here. It is the only thing
that stops a language model from re-deriving the same plausible story that was
already tested and lost money.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from service.kernel.clock import SHANGHAI
from service.kernel.units import TRADING_DAYS_PER_YEAR

from . import analog

# Structures measured before the strategy registry existed, so they live in the
# research reports rather than in the hypotheses table. Numbers are copied from
# reports/P5_findings.md and reports/P7_findings.md.
#
# **No `verdict` field, deliberately.** `spec` already says what was bought and
# sold, and `headline` already says what it returned; a "rejected" label on top
# of that adds no information — only authority. And that authority got misread:
# in reports/close/20261030.md a model wrote "追二板既越权限又无证据" citing
# these entries, when every one of them tested next_open (次日开盘接力) and none
# tested T 日收盘买入（打板）. The label said "this family is dead"; the evidence
# only ever said "this entry point returns this much".
#
# Whether a new proposal is the same experiment again is a judgement about two
# specs, not a lookup — so it stays with whoever is going to act on it.
# `service/research/claim.py` already works this way ("deliberately no verdict
# field"); this is the same rule applied to the ledger the model reads.
PRIORS: tuple[dict, ...] = (
    {
        "name": "全市场反转（跌幅最大）",
        "spec": "成交额>=5000万，当日跌幅最大一只，**次日开盘买、次日收盘卖**",
        "headline": "单笔超额 -0.737%",
        "why": ["跌得最惨的那只第二天继续弱，不是均值回归", "扣 20bps 后无可交易残余"],
        "source": "reports/P5_findings.md",
    },
    {
        "name": "全市场动量（涨幅最大）",
        "spec": "成交额>=5000万，当日涨幅最大一只，**次日开盘买、次日收盘卖**",
        "headline": "p=0.209，无法与 0 区分",
        "why": ["方向对但噪声大，样本不足以支撑下注"],
        "source": "reports/P5_findings.md",
    },
    {
        "name": "可转债反转",
        "spec": "转债池中当日跌幅最大，T+0 当日了结",
        "headline": "单笔 -1.156%",
        "why": ["T+0 也没救回来：跌的是流动性不是情绪", "买卖价差吃掉了全部边际"],
        "source": "reports/P7_findings.md",
    },
    {
        "name": "可转债动量",
        "spec": "转债池中当日涨幅最大，T+0 当日了结",
        "headline": "单笔 -0.353%，胜率 28.2%",
        "why": ["追涨在转债上等于给做市商送价差"],
        "source": "reports/P7_findings.md",
    },
    {
        "name": "正股涨停→买转债",
        "spec": "正股涨停当日买入其可转债，**次日开盘卖**",
        "headline": "超额为负且 3x 成本下更差",
        "why": ["涨停当天转债已被抢跑，溢价先于正股反映"],
        "source": "reports/P8_findings.md",
    },
)


def _json(value, ndigits: int = 4):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if not np.isfinite(number):
        return None
    return round(number, ndigits)


# ---------------------------------------------------------------- falsification
def _ledger_scope(rows_measured: list[dict]) -> dict:
    """Which entry points actually have a verdict, read from the book itself.

    A hard-coded list went stale on 2026-11-09: it kept saying ``t_close`` had
    never been registered after hypothesis id=6 measured exactly that. Derive it
    instead, and say separately what the *default grid* still omits — those are
    two different gaps and conflating them caused a duplicate proposal.
    """
    from service.research.spec import _ENTRIES as SUPPORTED_ENTRIES

    tested: dict[str, list[int]] = {}
    for row in rows_measured:
        spec = row.get("spec")
        if not isinstance(spec, dict):
            continue
        entry = str(spec.get("entry") or "").strip()
        if entry and row.get("id") is not None:
            tested.setdefault(entry, []).append(int(row["id"]))
    untested = sorted(set(SUPPORTED_ENTRIES) - set(tested))
    return {
        "entry_tested": {k: sorted(v) for k, v in sorted(tested.items())},
        "entry_not_tested": untested,
        "measured_count": len(rows_measured),
        "grid_scope": ("默认网格 `_grid()` 只展开 base.entry / next_open / "
                       "next_close（service/research/runner.py）。2026-11-12 起 "
                       "`--include-t-close` 可显式把 entry=t_close 作为竞争变体加入，"
                       "但会扩大 n_variants 与 DSR 分母，因此那次判决与默认族在案"
                       "判决不可直接比较（payload.grid_scope 会记录）。"
                       "未做『改默认并重跑全部在案策略』的破坏性迁移：7 条在案全 "
                       "rejected，迁移只会让分母变大、毁掉历史可比性，没有收益。"
                       "所以真缺口是『默认族里 t_close 不是竞争变体』，"
                       "与『有没有测过 t_close』是两件事。"),
        "reading": ("measured 里每条 spec 都写明测的是哪个入场点、判决是什么。"
                    "把某一条 rejected 读成整类交易不行，是把 spec 丢了；"
                    "把没测过的入场点说成测过，是把账本丢了。"
                    "两者都先查这张 scope，再决定要不要新登记一条假设。"),
        "source": "hypotheses 表的 spec.entry（动态生成，非静态文案）",
    }


def ledger(desk, detail: str = "summary") -> dict:
    """Everything this desk has measured, with the numbers it measured.

    The key is ``measured``, not ``falsified``. That is not cosmetic: a key name
    is read as a conclusion by whoever consumes it, and ``falsified`` asserted
    something the evidence never said. Five entries measured one entry point
    (``next_open``) and got read as "this whole family of trades is dead" —
    see the note in ``PRIORS`` for the time it actually happened.

    What this function guarantees is that a reader can disagree: every row
    carries the exact spec, the sample, and the numbers the thresholds saw.
    Whether to act on it is a judgement about two specs — the one measured and
    the one being proposed — and that judgement belongs to the reader.
    """
    from service.research.alpha_reading import alpha_reading, slim_reading
    from service.research.fill_scope import fill_scope, scope_gap, scope_key, slim_scope

    full = detail == "full"
    fill_reference: dict[str, dict] = {}
    rows_measured: list[dict] = []
    for row in desk.paper.list_hypotheses(limit=200):
        result = row.get("result") or {}
        if isinstance(result, str):
            try:
                import json

                result = json.loads(result)
            except ValueError:
                result = {}
        best = result.get("best") or {}
        trades = best.get("trades") or {}
        spec = result.get("spec") or {}
        evidence = {
            "trades": trades.get("n"),
            "mean_excess": trades.get("mean_excess"),
            "p_value": trades.get("p_value"),
            "dsr": (result.get("dsr") or {}).get("dsr"),
            "pbo": (result.get("pbo") or {}).get("pbo"),
            "cpcv_p05": (result.get("cpcv") or {}).get("oos_sharpe_p05"),
        }
        # 一排 null 会被读成"测过、没效果"。真正的情况往往是"当时没存"——
        # 2026-10-25 那批旧格式只有 metrics 与 notes。把两种说清，
        # 否则读者会把"没有数据"当成"数据说没有"。
        stored = any(value is not None for value in evidence.values())
        row_fill = fill_scope(spec.get("entry"), spec.get("pool"))
        row_alpha = alpha_reading(result)
        fill_reference.setdefault(
            scope_key(row_fill.get("entry"), row_fill.get("pool")), row_fill)
        rows_measured.append({
            "id": row.get("id"),
            "statement": row.get("statement"),
            "status": row.get("status"),
            # 机械读数。它来自预注册阈值（policy），不是关于市场的结论——
            # 换一套阈值会得到另一个词，同一个数不变。
            # 旧行存的是 `verdict`，新行存 `gate_reading`，两个都要认。
            "gate_reading": result.get("gate_reading") or result.get("verdict"),
            "reasons": (result.get("reasons") or [])[:3],
            # 判断需要的数，原样给出。少一个，读者就只能信那个标签。
            "evidence": evidence if stored else None,
            **({} if stored else {"evidence_missing":
                                  "这条没存下统计量（旧格式）。要数就重跑同一条 spec，"
                                  "不要把它读成'测过但没有效果'"}),
            "spec": result.get("spec"),
            # 2026-11-12 P0/P1-a：判决里原本没有成交口径，也没有把
            # 「信号账本 Sharpe」与「账户可实现」分开。id=6 的
            # +0.7547%/笔 / Sharpe 7.04 因此被反复读成「差一点找到 α」。
            # 这两个字段是**只读展示**，从已存 result 提取，不改 reasons、
            # 不重跑判决、不改阈值。
            "fill_scope": row_fill if full else slim_scope(row_fill),
            "alpha_reading": row_alpha if full else slim_reading(row_alpha),
            "source": "hypotheses",
        })

    live = []
    from . import strategy as strategy_mod

    for meta in strategy_mod.describe():
        live.append({"key": meta["key"], "name": meta["name"], "status": meta["status"],
                     "max_weight": meta["max_weight"], "evidence": meta["evidence"],
                     "regimes": meta["regimes"], "note": meta["note"]})

    return {
        "ok": True,
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "measured": [dict(p) for p in PRIORS] + rows_measured,
        "live": live,
        "note": ("这些是读数，不是判决。每条都带着它测的 spec、样本量与数字——"
                 "要不要做，由你判断，不由这张表替你判断。"
                 "把自己想做的事和 spec 逐项比：entry / exit / 池子 / 成本任何一项不同，"
                 "就是另一个实验，这些数字不适用；四项都一样，就该预期同样的结果。"
                 "判断完写进 journal（kind=review 或 note），带上你凭什么这么判断，"
                 "下一轮才有人能复核。"
                 "每条还带 fill_scope（成交口径与已知偏差）与 alpha_reading"
                 "（信号级 / 账户可实现分列）：先读这两项，再解读 mean_excess 与 Sharpe。"
                 "这两项只读，不改变 gate_reading 与 reasons。"),
        # 判决的**适用范围**。这些条目说的都是"某一种买法不赚钱"，不是
        # "这一类交易不行"——两者差一个入场点。不写清这一层，模型会把
        # "某一种买法被判负"读成"整条短线方向死了"。
        #
        # 2026-11-12 修：这里原来是静态文案，写着 t_close / next_limit
        # "还没有登记过假设"，但 hypotheses 表 id=6/7 早就测过且判
        # rejected（见 reports/kernel-capability-audit-20261109.html 18:50
        # 补记）。一张会过期的清单比没有清单更危险：它让下一轮模型重复
        # 登记已测过的假设。现在从账本里的 spec 动态生成。
        "scope": _ledger_scope(rows_measured),
        # 哪些在案判决建立在乐观成交口径上。reasons 里没有这一项，
        # 所以必须单独列出来，否则高 mean_excess 会被当成可交易 α。
        "fill_gap": scope_gap(rows_measured),
        # 完整口径按 (entry|pool) 去重只存一份，逐行只给精简读数：
        # 7 行各重复 3 条 board legs 与整段 basis 会把响应撑到 19 KB，
        # 超过宿主对单个工具响应的体积上限（整包丢弃，不是截断）。
        "fill_scope_reference": (
            fill_reference if full else {
                key: (scope if scope.get("status") == "optimistic" else {
                    "status": scope.get("status"),
                    "entry": scope.get("entry"),
                    "pool": scope.get("pool"),
                    "detail": "非乐观口径；完整假设见 service/research/fill_scope.py 或 detail=full",
                })
                for key, scope in fill_reference.items()
            }),
        "alpha_reading_note": {
            "signal_book_basis": ("信号账本口径：当日所有信号按等权组合计入，"
                                  "未受 max_positions / 现金 / 整手 / 最低佣金约束；"
                                  "capital.py 自述 30 信号曾被当成满仓 30 名组合打分，"
                                  "broken_board 报过 190% CAGR，是容量检查救回了判决"),
            "account_basis": ("账户口径：max_positions / 现金 / 整手 / ¥5 最低佣金；"
                              "stress 是同一套收费模型的 2× / 3× 压力测试"),
            "cost_rule": ("并列报 1× / 2× / 3× 成本，崩塌的形状比单点 headline "
                          "Sharpe 更有信息；未成交的信号不是成本，不能只加一笔费"
                          "用就当作持有过"),
            "reading": ("gate_reading=rejected 是预注册门禁的机械读数，不是『短线没有 α』。"
                        "分列的是『信号级测到了什么』与『这个账户能不能实现』"),
            "read_only": True,
        },
        "detail": detail,
        "detail_note": ("默认 summary：逐行只给精简读数，完整口径在 "
                        "fill_scope_reference / alpha_reading_note。"
                        "detail=full 会把完整字段展开到每一行（响应可能超过 16 KB "
                        "宿主上限，被整包丢弃），仅在直接读文件时用"),
        # 这张表有两个年龄：P5–P8 的静态先验不会随行情变，hypotheses 表里的会。
        # 只写一个 generated_at 会让读者以为整张表都是刚算的。
        "age_note": (f"measured 里前 {len(PRIORS)} 条是 reports/P5–P8 的静态读数"
                     "（当年测的，不再更新）；其余来自 hypotheses 表，"
                     "带 status 与 gate_reading。"),
        "provenance": {"sources": ["hypotheses 表", "reports/P5-P8", "strategy.REGISTRY"]},
    }


# ------------------------------------------------------------------ portfolio
def risk(desk, day: str | None = None, window: int = 60) -> dict:
    """What the book is exposed to, measured rather than described.

    Historical simulation on the current weights: no covariance model, no normal
    assumption. With three positions and sixty sessions, a fitted covariance
    matrix would be a story; the empirical distribution is at least honest about
    being small.
    """
    from .exposure import describe

    day = day or desk.market.latest_bar_date()
    summary = desk.book.summary()
    equity = float(summary["equity"]) or 1.0
    limits = desk.limits()
    try:
        exp = describe(desk.book, desk.market, day)
    except (ValueError, KeyError):
        exp = None
    weights = {p["symbol"]: float(p["weight"]) for p in (exp.positions if exp else [])}

    series = None
    correlations = []
    if weights:
        panel = desk.market.panel(day, window + 1)
        if not panel.empty:
            col = "adj_close" if "adj_close" in panel.columns else "close"
            p = panel[["trade_date", "ts_code", col]].copy().rename(columns={col: "px"})
            p["trade_date"] = p["trade_date"].astype(str)
            p["ret"] = p.groupby("ts_code")["px"].pct_change()
            wide = p.pivot_table(index="trade_date", columns="ts_code",
                                 values="ret", aggfunc="last").sort_index()
            held = [s for s in weights if s in wide.columns]
            if held:
                port = sum(weights[s] * wide[s].fillna(0.0) for s in held)
                series = port.dropna()
                if len(held) > 1 and len(wide) > 20:
                    corr = wide[held].corr()
                    for i, a in enumerate(held):
                        for b in held[i + 1:]:
                            value = corr.loc[a, b]
                            if np.isfinite(value):
                                correlations.append({"pair": [a, b],
                                                     "rho_60d": _json(value, 3)})

    var95 = worst = vol = None
    if series is not None and len(series) >= 20:
        var95 = float(np.percentile(series, 5))
        worst = float(series.min())
        vol = float(series.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))

    drawdown = float(summary.get("drawdown") or 0.0)
    return {
        "ok": True,
        "as_of": day,
        "equity": _json(equity, 2),
        "invested_weight": None if exp is None else _json(exp.invested),
        "daily": {
            "window": window,
            "var95_pct": _json(var95),
            "worst_day_pct": _json(worst),
            "var95_cny": None if var95 is None else _json(var95 * equity, 2),
            "vol_annualized": _json(vol),
            "method": "历史模拟：按当前权重回放过去 N 日收益，不做正态假设",
            # 空仓这条必答：五个 null 摆在一起，读起来像"风险算不出来"。
            **({} if var95 is not None else {"note":
               "空仓（或持仓不足 20 个交易日）：没有权重可回放，风险指标不适用"}),
        },
        "concentration": None if exp is None else {
            "top1": _json(exp.top1), "top3": _json(exp.top3), "hhi": _json(exp.hhi),
            "by_industry": {k: _json(v) for k, v in sorted(exp.by_industry.items(), key=lambda kv: -kv[1])[:3]},
        },
        "beta_vs_equal_weight": None if exp is None else _json(exp.beta, 3),
        **({} if (exp is not None and exp.beta is not None) else
           {"beta_vs_equal_weight_note": ("空仓：没有仓位可加权，beta 不适用"
                                          if exp is None or not exp.positions
                                          else "持仓历史不足以估 beta")}),
        "correlation": sorted(correlations, key=lambda c: -abs(c["rho_60d"] or 0))[:3],
        "thresholds": {
            "drawdown_now": _json(drawdown),
            "daily_loss_no_new": limits["daily_loss_no_new"],
            "drawdown_half": limits["drawdown_half"],
            "drawdown_stop": limits["drawdown_stop"],
            # How much more equity can be lost before the half-position rule trips.
            "room_to_half_cny": _json(max(0.0, (drawdown - limits["drawdown_half"])) * equity, 2),
        },
        "caveats": [
            "历史模拟用的是过去 60 日，没经历过的事件不在分布里。",
            "VaR 是账户口径，含现金：低仓位时数字很小，看持仓风险请除以 invested_weight。",
            "权重按当前市值，未考虑卖出时的冲击；仓位小时影响可忽略，仓位大时不是。",
        ],
        "provenance": {"datasets": ["daily"], "window": window},
    }


# ------------------------------------------------------------------ liquidity
def liquidity(desk, symbol: str, day: str | None = None,
              sizes: tuple[int, ...] = (1000, 5000, 20000)) -> dict:
    """What it would cost to trade this size, and how much it can absorb."""
    from .impact import estimate as impact_estimate
    from .impact import finite as impact_finite
    from .impact import round_trip_cost_bps

    day = day or desk.market.latest_bar_date()
    snap = desk.market.snapshot(symbol, day)
    if snap is None or snap.last <= 0:
        return {"ok": False, "error": f"{symbol} 在 {day} 没有行情"}
    hist = desk.market.history(symbol, day, 30)
    if len(hist) < 10:
        return {"ok": False, "error": f"{symbol} 历史不足"}
    col = "adj_close" if "adj_close" in hist.columns else "close"
    close = hist[col].astype(float)
    sigma = impact_finite(close.pct_change().tail(20).std(ddof=1))
    adv = float(desk.market.adv_map(day).get(symbol, 0) or 0.0)
    price = float(snap.last)
    instrument = desk.market.instrument(symbol, day)
    curve = []
    for qty in sizes:
        # No ADV is not low ADV. ``impact.estimate`` clamps an unknown ADV to one
        # share, which turns a 20,000-share order into 66,667 "days needed" and
        # a schedule loop that runs that many times — an endpoint that never
        # returns, for a symbol the store simply has no turnover for.
        imp = impact_estimate(qty, price, adv, sigma) if adv > 0 else None
        charged = round_trip_cost_bps(qty * price, instrument)
        if imp is None:
            curve.append({"qty": qty, "notional_cny": _json(qty * price, 2),
                          "participation": None,
                          "fill_cost_bps": charged["total_bps"],
                          "fill_cost_cny": _json(charged["total_bps"] / 10_000 * qty * price, 2),
                          "impact_bps": None, "impact_cny": None,
                          "total_cost_bps": None, "days_needed": None,
                          "note": "ADV20 缺失：只能报费用，冲击不可估"})
            continue
        curve.append({"qty": qty, "notional_cny": _json(qty * price, 2),
                      "participation": _json(imp.participation, 4),
                      # The kernel quote is round-trip; modelled impact is per side.
                      "fill_cost_bps": charged["total_bps"],
                      "fill_cost_cny": _json(charged["total_bps"] / 10_000 * qty * price, 2),
                      "impact_bps": _json(imp.impact_bps, 2),
                      "impact_cny": _json(imp.cost_cny, 2),
                      "total_cost_bps": _json(charged["total_bps"] + 2 * imp.impact_bps, 2),
                      "days_needed": imp.days_needed})
    return {
        "ok": True,
        "symbol": symbol,
        "as_of": day,
        "price": _json(price, 3),
        "adv20_cny": _json(adv, 0),
        "adv20_shares": _json(adv / price, 0) if price > 0 else None,
        "sigma_daily": _json(sigma),
        "curve": curve,
        "capacity_1pct_adv_cny": _json(adv * 0.01, 0),
        "band": {"up_limit": str(snap.up_limit), "down_limit": str(snap.down_limit),
                 "distance_to_up": _json(float(snap.up_limit / snap.last - 1)),
                 "distance_to_down": _json(float(snap.down_limit / snap.last - 1))},
        "cost_note": ("fill_cost_bps 是撮合器实际收取的往返成本（双边佣金、税费与固定滑点）；"
                      "impact_bps 是单边平方根冲击估计，回测里没有收。"
                      "total_cost_bps 统一按往返口径，计入双边冲击，即 fill + 2×impact。"),
        "caveats": ["impact 是平方根模型的估计，系数 c=0.4 取自 A 股小盘经验值，不是定律。",
                     "capacity 按 1% ADV 估算，超过 10% 参与率时单日成交不现实。",
                     "impact_bps 不进账本：没有逐笔盘口数据，撮合器只按固定滑点定价。"],
        "provenance": {"datasets": ["daily", "stk_limit"],
                       "model": "kernel round-trip fees + 2*c*sigma*sqrt(Q/ADV)"},
    }


# --------------------------------------------------------------- distribution
def distribution(desk, symbol: str, day: str | None = None, window: int = 250) -> dict:
    """The shape of this symbol's returns — tails, skew, streaks, drawdown."""
    day = day or desk.market.latest_bar_date()
    hist = desk.market.history(symbol, day, window)
    if len(hist) < 60:
        return {"ok": False, "error": f"{symbol} 在 {day} 之前的历史不足 60 天"}
    close = hist["close"].astype(float)
    pre = hist["pre_close"].astype(float)
    ret = (close / pre.where(pre > 0) - 1).dropna()
    if ret.empty:
        return {"ok": False, "error": "无法计算收益序列"}
    px = (1 + ret).cumprod()
    peak = px.cummax()
    dd = float((px / peak - 1).min())

    streak = longest = 0
    for value in ret:
        streak = streak + 1 if value < 0 else 0
        longest = max(longest, streak)

    return {
        "ok": True,
        "symbol": symbol,
        "as_of": day,
        "window": len(ret),
        "mean_daily": _json(float(ret.mean())),
        "vol_annualized": _json(float(ret.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))),
        "skew": _json(float(ret.skew()), 2),
        "kurtosis": _json(float(ret.kurt()), 2),
        "percentiles": {f"p{int(q)}": _json(float(np.percentile(ret, q)))
                        for q in (1, 5, 25, 50, 75, 95, 99)},
        "up_day_ratio": _json(float((ret > 0).mean()), 3),
        "worst_day": _json(float(ret.min())),
        "best_day": _json(float(ret.max())),
        "max_drawdown": _json(dd),
        "longest_losing_streak": int(longest),
        "autocorr_lag1": _json(float(ret.autocorr(lag=1) or 0.0), 3),
        "caveats": [
            f"{len(ret)} 个交易日不足以估计 1% 分位，p1/p99 基本是单个观测值。",
            "偏度/峰度在 250 个样本上噪声很大，只用来判断'有没有厚尾'，不要读具体数值。",
        ],
        "provenance": {"datasets": ["daily"], "window": len(ret)},
    }


# -------------------------------------------------------------------- text
def news(desk, day: str | None = None, symbol: str | None = None, *,
         days: int = 7, limit: int = 20, kind: str | None = None) -> dict:
    """Announcements, headlines and notes, filtered by the publication clock.

    This is the one channel where the desk reads the world outside prices. It is
    deliberately thin: a title, a source, a tier and a timestamp. No summary is
    generated here — if the model wants to know what a filing means, it has to
    read the title and go to the URL, which is exactly the behaviour that keeps
    it from inventing a mechanism.
    """
    from service.adapters.text import query as query_text

    day = day or desk.market.latest_bar_date()
    if not day:
        return {"ok": False, "error": "no daily bars to anchor the clock"}
    result = query_text(desk.data, day=day, symbol=symbol, days=days, limit=limit)
    if kind and result.get("ok"):
        wanted = str(kind).lower()
        result["items"] = [i for i in result.get("items", [])
                           if str(i.get("kind")).lower() == wanted]
        result["filtered_kind"] = wanted
        if not result["items"]:
            result["note"] = f"窗口内没有 kind={wanted} 的文本；其余类型仍可用 quant_knowledge(news)"
    return result


# -------------------------------------------------------------------- crowding
def crowding(desk, symbol: str, day: str | None = None, window: int = 250) -> dict:
    """Is this a crowded trade? Turnover percentile, extension, consecutive days."""
    day = day or desk.market.latest_bar_date()
    hist = desk.market.history(symbol, day, window)
    if len(hist) < 60:
        return {"ok": False, "error": f"{symbol} 在 {day} 之前的样本不足 60 天"}
    # Range position and the up-streak are price-level statements, so they use
    # the adjusted series; `last` stays the real price the model can compare to
    # a limit or a stop.
    col = "adj_close" if "adj_close" in hist.columns else "close"
    close = pd.to_numeric(hist[col], errors="coerce").astype(float)
    last_raw = float(pd.to_numeric(hist["close"], errors="coerce").iloc[-1])
    amount = pd.to_numeric(hist["amount"], errors="coerce").astype(float) * 1000.0

    hi = float(close.max())
    lo = float(close.min())
    last = float(close.iloc[-1])
    dist_high = last / hi - 1 if hi > 0 else 0.0
    pos_in_range = (last - lo) / (hi - lo) if hi > lo else 0.5

    amt_ratio = float(amount.iloc[-1] / amount.iloc[-21:-1].mean()) \
        if amount.iloc[-21:-1].mean() > 0 else 1.0
    up_streak = 0
    for ret in (close.pct_change().iloc[::-1]):
        if ret > 0:
            up_streak += 1
        else:
            break

    turnover_pct = None
    try:
        # ``end=day`` is not decoration. Without it the year partition is read
        # to its last row, so a replay at 2026-03-01 computes "current turnover
        # percentile" against sessions through December — the denominator knows
        # the future, and the further back the replay the larger that share.
        basic = desk.data.read("daily_basic", start=day[:4] + "0101", end=day)
        if not basic.empty and "turnover_rate" in basic.columns:
            row = basic[(basic["ts_code"] == symbol)
                        & (basic["trade_date"].astype(str) == day)]
            series = basic[basic["ts_code"] == symbol]["turnover_rate"].astype(float).dropna()
            if not row.empty and len(series) > 60:
                now = float(row.iloc[0]["turnover_rate"])
                turnover_pct = float((series <= now).mean())
    except (KeyError, ValueError, OSError):
        turnover_pct = None

    # 这里原来叫 `flags`，每条是一句解读："换手 93% 分位：人多"、"贴着新高：
    # 上方没有套牢盘"、"连涨 3 天：回撤概率上升"、"区间下沿 10%：接飞刀区"。
    #
    # 指标本来就在同一个响应里单列（turnover_percentile、distance_from_high、
    # amount_ratio、up_streak、range_position），解读没有增加信息；而其中
    # "回撤概率上升"这类话**从未被测过**，是句子自带的权威而非数据。
    # 现在只报「哪个阈值被跨过、跨了多少」——同样的信息，读者可以自己解读，
    # 也可以不同意这些阈值。
    crossed: list[dict] = []
    if turnover_pct is not None and turnover_pct >= 0.90:
        crossed.append({"metric": "turnover_percentile",
                        "value": round(turnover_pct, 3), "at_least": 0.90})
    if dist_high >= -0.02:
        crossed.append({"metric": "distance_from_high",
                        "value": round(dist_high, 4), "at_least": -0.02})
    if amt_ratio >= 2.0:
        crossed.append({"metric": "amount_ratio",
                        "value": round(amt_ratio, 2), "at_least": 2.0})
    if up_streak >= 3:
        crossed.append({"metric": "up_streak_days",
                        "value": up_streak, "at_least": 3})
    if pos_in_range <= 0.10:
        crossed.append({"metric": "range_position",
                        "value": round(pos_in_range, 4), "at_most": 0.10})

    return {
        "ok": True, "symbol": symbol, "as_of": day,
        # The real price, next to the adjusted range it is being placed in — the
        # caller compares this to a limit or a stop, not `high_250`.
        "last": _json(last_raw, 3),
        "distance_from_high": _json(dist_high),
        "range_position": _json(pos_in_range),
        "amount_ratio": _json(amt_ratio),
        "turnover_percentile": _json(turnover_pct, 3),
        "up_streak": up_streak,
        "high_250": _json(hi, 3), "low_250": _json(lo, 3),
        "thresholds_crossed": crossed,
        "caveats": [
            "拥挤度描述的是参与度，不是方向；拥挤的票可以继续涨，也可以一周跌回去。",
            ("thresholds_crossed 是机械判据（哪个数越过了哪条线），不是结论——"
             "这些线是约定，不是市场事实。"),
        ],
        "provenance": {"datasets": ["daily", "daily_basic"], "window": window},
    }


# -------------------------------------------------------------------- position
def position(desk, symbol: str, day: str | None = None) -> dict:
    """The book's side of one holding: cost, stop distance, exposure, risk."""
    from .exposure import describe
    from .stops import open_stops

    day = day or desk.market.latest_bar_date()
    summary = desk.book.summary()
    held = [p for p in summary["positions"] if p["symbol"] == symbol]
    if not held:
        # "I don't hold it" is an answer, not a failure.
        #
        # This used to be `{"ok": false, "error": "600519.SH 不在持仓里"}`, which
        # from the caller's side is indistinguishable from "the tool broke" — the
        # same envelope as a missing dataset or a bad symbol. A model that gets
        # it retries, or drops the question, when the fact it asked for was
        # delivered. `ok: true` with `held: false` says what happened and points
        # at the two tools that answer the question it probably meant.
        return {"ok": True, "symbol": symbol, "as_of": day, "held": False,
                "note": (f"{symbol} 不在持仓里——这一块只讲账本的这一侧，空手就是空手。"
                         "想了解这只票本身用 quant_profile；"
                         "想评估能不能买用 quant_knowledge(topic=liquidity, symbol=…)"
                         "与 quant_exposure。")}
    pos = held[0]
    view = next((v for v in open_stops(desk, day) if v.symbol == symbol), None)
    try:
        exp = describe(desk.book, desk.market, day)
        industry = desk.market.industry_of(symbol)
        industry_weight = exp.by_industry.get(industry, 0.0)
    except (ValueError, KeyError):
        industry, industry_weight = desk.market.industry_of(symbol), None

    risk_cny = None
    distance = None
    if view is not None:
        risk_cny = float((view.last - view.stop) * view.qty)
        distance = float(view.last / view.stop - 1) if view.stop > 0 else None
    try:
        unrealized = float(pos["last_price"]) / float(pos["avg_cost"]) - 1
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        unrealized = None

    # Stop/volatility/risk-budget compatibility. These are the numbers a model
    # otherwise has to derive by hand (and, on 2026-11-09, did): a stop narrower
    # than one ATR sits inside daily noise, while an ATR-based stop can exceed
    # the per-ticket risk budget on a small book. Both are arithmetic, so the
    # kernel should supply them rather than make the reader re-derive them.
    #
    # ATR20 is an *adjusted* price. It is converted to a fraction of the same
    # adjusted close before being passed in — never subtracted from `view.last`,
    # which is a traded price.
    sizing = None
    if view is not None:
        try:
            from .stops import stop_risk_context

            atr_fraction = None
            try:
                hist = desk.market.history(symbol, day, 21)
                if not hist.empty and "adj_close" in hist.columns:
                    adj_last = float(pd.to_numeric(
                        hist["adj_close"], errors="coerce").astype(float).iloc[-1])
                    atr = float(desk.market.atr(symbol, day, 20))
                    if adj_last > 0 and atr > 0:
                        atr_fraction = atr / adj_last
            except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                atr_fraction = None

            limits = desk.limits() if hasattr(desk, "limits") else {}
            sizing = stop_risk_context(
                last=float(view.last), stop=float(view.stop), qty=view.qty,
                equity=float(summary["equity"]),
                max_trade_risk=float(limits.get("max_trade_risk", 0)),
                atr20_fraction=atr_fraction,
            )
        except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError):
            # A desk double without market/limits still gets the rest of the
            # position answer. Never invent a risk number to fill this block.
            sizing = None
    return {
        "ok": True, "symbol": symbol, "as_of": day, "held": True,
        "qty": pos["qty"], "available": pos["available_qty"],
        "avg_cost": pos["avg_cost"], "last": pos["last_price"],
        "unrealized_pct": _json(unrealized),
        "weight": _json(pos["weight"], 3),
        "stop": None if view is None else str(view.stop),
        "distance_to_stop": _json(distance, 4),
        "risk_to_stop_cny": _json(risk_cny, 2),
        "breached": None if view is None else view.breached,
        "blocked_t1": None if view is None else view.blocked,
        "industry": industry,
        "industry_weight": _json(industry_weight, 3),
        "stop_risk_context": sizing,
        "note": ("distance_to_stop 为负表示已经跌破；risk_to_stop_cny 是'再跌到止损'"
                 "还要亏多少钱，不是止损价与成本的距离。stop_risk_context 是纯测算："
                 "止损宽度相当于几个 ATR、占单笔风险预算多少，不含建议或动作；"
                 "取不到 ATR / 风控读数时该块为 None，不编数。"),
        "provenance": {"datasets": ["daily", "stk_limit"], "book": "paper"},
    }


# ------------------------------------------------------------------ dispatcher
def cards(desk) -> dict:
    """The map of what this brain knows, organised by layer. Cheap by design."""
    try:
        n_ledger = len(ledger(desk)["measured"])
    except Exception:  # noqa: BLE001
        n_ledger = None
    return {
        "ok": True,
        # 这张表是"知识库里有什么"，不是一个时点的市场快照，所以锚是"什么时候数的"，
        # 不是"哪一天的数"。少了它，读者无法判断 index 里的计数有多旧。
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "layers": [
            {
                "layer": "市场",
                "topics": [
                    {"topic": "regime", "question": "四种市场状态各自的历史前瞻收益"},
                    {"topic": "industries", "question": "申万一级横截面：涨跌、宽度、涨停、主力净流入"},
                    {"topic": "tape_flow", "question": "昨日涨停股今天的开盘溢价、红盘率、晋级率"
                                                     "（按昨日板数分层）——打板环境开没开"},
                    {"topic": "cycle", "question": "今天这档炸板率/晋级率，历史上之后 1/3 天怎么走；"
                                                  "附最近 15 天序列与连续天数、同档预期差"},
                    {"topic": "seal_tape", "question": "今天的封单轨迹：涨停家数在第几帧见顶、"
                                                      "哪些票的封单在收工前被撤"},
                    {"topic": "discover", "question": "宽度、涨停结构、行业分化、跳空行为（走 /discover）"},
                ],
            },
            {
                "layer": "标的",
                "topics": [
                    {"topic": "analog", "question": "这个形态历史上最像的 k 次，之后 1/2/3/4/5/10 日怎样",
                     "args": {"symbol": "600000.SH", "k": 300}},
                    {"topic": "distribution", "question": "这只票的收益分布：尾部、偏度、回撤、连跌",
                     "args": {"symbol": "600000.SH"}},
                    {"topic": "crowding", "question": "是不是人多的地方：换手分位、离新高、连涨",
                     "args": {"symbol": "600000.SH"}},
                    {"topic": "liquidity", "question": "这个量级的买卖要付多少冲击，一天能吃多少",
                     "args": {"symbol": "600000.SH", "sizes": [1000, 5000, 20000]}},
                    {"topic": "board", "question": "这只票的封板结构（板数/封单强度/首封时段），"
                                                  "以及同结构的历史样本之后怎么走",
                     "args": {"symbol": "600000.SH"}},
                    {"topic": "industry_rank", "question": "这只票当日在其申万行业里排第几"
                                                           "（涨幅与成交额两个名次）",
                     "args": {"symbol": "002027.SZ"}},
                ],
            },
            {
                "layer": "组合",
                "topics": [
                    {"topic": "risk", "question": "组合的历史模拟 VaR、集中度、相关性、距风控阈值的距离"},
                    {"topic": "position", "question": "某一笔的处境：止损距离、行业暴露、风险金额",
                     "args": {"symbol": "600000.SH"}},
                ],
            },
            {
                "layer": "盘面",
                "topics": [
                    {"topic": "limit_structure",
                     "question": "昨日涨停结构：封板率、炸板率、连板高度、晋级率、涨停原因"},
                    {"topic": "lhb",
                     "question": "龙虎榜：谁买了、机构还是游资、净买多少",
                     "args": {"symbol": "002966.SZ", "limit": 20}},
                ],
            },
            {
                "layer": "竞价",
                "topics": [
                    {"topic": "auction",
                     "question": "开盘前谁在被抢筹/砸盘，抢筹的后来封住了没有"},
                    {"topic": "anomaly",
                     "question": "个股异动原因（上游 AI 归纳的文本线索，需回原文核对）",
                     "args": {"symbol": "002966.SZ"}},
                ],
            },
            {
                "layer": "文本",
                "topics": [
                    {"topic": "news", "question": "这只票（或全市场）最近的公告/新闻，按发布时间过滤",
                     "args": {"symbol": "002966.SZ", "days": 7, "limit": 20}},
                    {"topic": "filings", "question": "只看公告/业绩预告（kind=announcement）；正文用 quant_filing(announcement_id) 分段读",
                     "args": {"symbol": "002966.SZ", "days": 30}},
                ],
            },
            {
                "layer": "历史结论",
                "topics": [
                    {"topic": "ledger", "question": "哪些结构被测过，各是什么数字、测的是哪个入场点"},
                ],
            },
            {
                "layer": "α 来源",
                "topics": [
                    {"topic": "factors",
                     "question": "横截面因子读数：9 个预注册因子 × 4 个持有期的 RankIC / ICIR / "
                                 "HAC t / 分层单调 / 衰减，含行业与市值中性化、BH 多重检验校正",
                     "args": {"window": 120},
                     "read_only": True,
                     "note": "只读读数，不是信号；结果里的 survivors 为空是常见且诚实的答案"},
                    {"topic": "breadth",
                     "question": "哪些数据集真的进了 α 估计、哪些短线相关数据集还闲着、"
                                 "以及把它们接进来要守什么规矩",
                     "read_only": True,
                     "note": "只读覆盖度读数；新增检验必须先登记 registry，本层不新增检验"},
                ],
            },
        ],
        "index": {"measured_structures": n_ledger,
                  "analog_universe": "流动性前 1200 只，2023-01 至今"},
        "rule": ("知识不是结论。每个答案都带 caveats 与 provenance：先读误差，再决定权重。"
                 "analog 的 match.distance_band=far 表示今天这个形态在历史上找不到近邻：最近邻比 60% 的随机标的都远。那组统计该降权甚至忽略——忽略与否由你判断，但不要把 far 的类比当作支持。"
                 "analog 的 net 块已经扣掉你自己那一笔的往返成本（按账本规模算），"
                 "survives_cost=false 表示这段历史超额在你的规模上不存在——"
                 "毛超额不是理由，净超额才是。"),
    }


#: Topics computed per instrument. Everything else is a property of the whole
#: market. Kept next to ``answer`` and mirrored into the tool description,
#: because the description is where the model decides whether to pass a symbol —
#: and until 2026-11-01 the plugin prompt told it to ask ``topic=distribution``
#: with none, so those three round trips could only ever end in an error.
SYMBOL_TOPICS = ("analog", "crowding", "position", "liquidity", "distribution",
                 "board", "timing_distribution")


def timing_distribution(desk, symbol: str, *, as_of: str | None = None) -> dict:
    """Read-only conditional forward-return distribution for one name.

    ``as_of`` is an end-of-day history cutoff, not a decorative response field.
    Frames after that trading day must be removed *before* bucket statistics are
    computed; otherwise a historical research request silently receives the
    latest sample. Intraday time-of-day replay is deliberately not implied.
    """
    from . import intraday_buckets

    code = str(symbol).split(".")[0]
    cutoff = str(as_of).strip().replace("-", "")[:8] if as_of else None
    if cutoff is not None and (len(cutoff) != 8 or not cutoff.isdigit()):
        return {"ok": False, "topic": "timing_distribution", "status": "bad_as_of",
                "symbol": str(symbol).upper(), "as_of": str(as_of),
                "error": "as_of 必须是 YYYYMMDD 或 YYYY-MM-DD（按收盘后日界解释）"}
    meta = {"ok": True, "topic": "timing_distribution",
            "symbol": str(symbol).upper(), "read_only": True,
            "requested_as_of": cutoff, "as_of_basis": "end_of_day",
            "provenance": {"datasets": ["intraday_features"], "book": "none"}}
    try:
        frame = desk.market.store.read("intraday_features")
    except Exception as exc:  # noqa: BLE001 - a missing partition is an answer
        return {**meta, "ok": False, "status": "no_features", "buckets": [],
                "reason": f"{type(exc).__name__}: {exc}",
                "note": "逐帧特征不可读；冷存之前的日子物理上不可回补。"}
    if frame is None or frame.empty:
        return {**meta, "ok": False, "status": "no_features", "buckets": [],
                "note": "intraday_features 为空；每个交易日 17:30 落一个分区。"}
    if "day" not in frame.columns:
        return {**meta, "ok": False, "status": "bad_features", "buckets": [],
                "error": "intraday_features 缺 day 列，无法执行 PIT 截断"}
    frame = frame.assign(day=frame["day"].astype(str).str.replace("-", "", regex=False))
    if cutoff is not None:
        frame = frame[frame["day"] <= cutoff]
        if frame.empty:
            return {**meta, "ok": False, "status": "no_frames_before_as_of",
                    "buckets": [], "n_frames": 0, "days": [],
                    "note": "该日界之前没有逐帧记录；不许用之后的分区代替。"}
    mine = (frame[frame["code"].astype(str) == code]
            if "code" in frame.columns else frame.iloc[0:0])
    if mine.empty:
        return {**meta, "ok": False, "status": "no_frames_for_symbol",
                "buckets": [], "n_frames": 0,
                "note": "这只票没有逐帧记录；不代表未来不会发生。"}
    try:
        result = intraday_buckets.conditional_distribution(
            mine, value_col="forward_to_close_pct",
            min_n=intraday_buckets.MIN_BUCKET_N)
    except (ValueError, KeyError) as exc:
        return {**meta, "ok": False, "status": "bad_features", "buckets": [],
                "reason": f"{type(exc).__name__}: {exc}"}
    result.update(meta)
    result["n_frames"] = len(mine)
    days = (sorted(str(d) for d in mine["day"].astype(str).unique())
            if "day" in mine.columns else [])
    result["days"] = days
    result["as_of"] = cutoff or (days[-1] if days else None)
    result["data_as_of"] = days[-1] if days else None
    result["pit_safe"] = True
    result["pit_scope"] = "frame day <= requested end-of-day; not an intraday-clock replay"
    result["note"] = (str(result.get("note") or "")
                      + " 本工具只读：没有 action / verdict / 目标价；"
                        "样本不足的桶按 insufficient 处理，跨日分区累积后才可引用。")
    result["caveats"] = [
        "单日分区下几乎所有桶都是 insufficient，这是样本事实不是故障",
        "forward_to_close_pct 是当日到收盘，A 股 T+1 下不可当日收割",
        "分布是描述性统计，不是可交易信号的胜率或期望收益",
    ]
    return result


#: Market-level topics offered as hints when a symbol-only topic is called
#: without one. Kept in one place so ``cards`` and the error hint cannot drift.
MARKET_TOPICS = ("cards", "ledger", "regime", "risk", "industries", "factors",
                 "breadth", "market_state")


def factor_read(desk, *, as_of: str | None = None, window: int = 120,
                factor: str | None = None, detail: str = "summary",
                horizons: str | None = None) -> dict:
    """Cross-sectional factor reads: where an alpha *could* come from.

    This is the layer that answers "which signals have measurable predictive
    power at all", with the data that used to sit in the store and never enter
    an alpha estimate (``daily_basic``, ``moneyflow``). Read-only: no signal,
    no ranking, no action. See ``service/research/factors.py`` for the method.
    """
    from service.research import factors

    try:
        horizon_family = factors.parse_horizons(horizons)
    except ValueError as exc:
        return {"ok": False, "topic": "factors", "status": "bad_horizons",
                "error": str(exc),
                "available": {"default": list(factors.HORIZONS),
                              "dense": list(factors.DENSE_HORIZONS),
                              "custom": "逗号分隔整数，如 1,2,3,4,5"},
                "read_only": True}

    store = desk.market.store
    end = str(as_of or desk.market.latest_bar_date() or "")
    if not end:
        return {"ok": False, "topic": "factors", "status": "no_bar_date",
                "error": "没有可用的日线日期"}
    window = max(60, min(int(window), 500))
    try:
        cal = store.read("trade_cal", start="19900101", end=end,
                         columns=["cal_date", "is_open"])
        opened = sorted(set(cal.loc[cal["is_open"] == 1, "cal_date"].astype(str)))
    except Exception:  # noqa: BLE001 - a missing calendar is an answer, not a crash
        opened = []
    if len(opened) < window:
        return {"ok": False, "topic": "factors", "status": "short_calendar",
                "error": f"交易日历只有 {len(opened)} 个开市日 < {window}",
                "read_only": True}
    start = opened[-window]
    try:
        sw = desk.market.sw_table()
    except Exception:  # noqa: BLE001
        sw = pd.DataFrame()
    out = factors.panel(
        store, start=start, end=end, sw_table=sw,
        factors=(factor,) if factor else None,
        detail=detail if factor else "summary",
        horizons=horizon_family,
    )
    out.update({"topic": "factors", "read_only": True,
                "window_sessions": window, "as_of": end,
                "note": (str(out.get("note") or "") +
                         " 本层只读：没有 action / verdict / signal / 目标价；"
                         "挑因子去构造策略须经 quant_hypothesis(add) 预注册，"
                         "并把新增检验计入试验分母。")})
    return out


def _needs_symbol(topic: str) -> dict:
    """Say which argument is missing, and that the capability exists.

    ``{"ok": false, "error": "distribution 需要 symbol"}`` is accurate and, from
    the caller's side, indistinguishable from "this desk cannot answer that".
    The reply now names the missing argument, shows a callable example, and lists
    the topics that need no symbol — so a model that guessed wrong can correct
    itself in one turn instead of dropping the question.
    """
    return {
        "ok": False,
        "error": f"{topic} 是逐只标的的问题：缺 symbol，不是没有这个能力",
        "need": "symbol",
        "example": {"topic": topic, "symbol": "600519.SH"},
        "market_topics": list(MARKET_TOPICS),
    }


def answer(desk, topic: str, **kwargs) -> dict:
    """One entry point so the model does not need to know module names."""
    topic = str(topic or "").strip().lower()
    root = Path(__file__).resolve().parents[2]
    try:
        if topic == "analog":
            symbol = kwargs.get("symbol")
            if not symbol:
                return _needs_symbol("analog")
            return analog.explain(desk, str(symbol).upper(),
                                  as_of=kwargs.get("as_of"),
                                  k=int(kwargs.get("k") or 300),
                                  same_industry=bool(kwargs.get("same_industry")),
                                  root=root)
        if topic == "ledger":
            return ledger(desk, detail=str(kwargs.get("detail") or "summary"))
        if topic in ("regime", "regime_history"):
            return analog.regime_table(desk, root=root)
        if topic == "crowding":
            symbol = kwargs.get("symbol")
            if not symbol:
                return _needs_symbol("crowding")
            return crowding(desk, str(symbol).upper(), day=kwargs.get("as_of"))
        if topic == "position":
            symbol = kwargs.get("symbol")
            if not symbol:
                return _needs_symbol("position")
            return position(desk, str(symbol).upper(), day=kwargs.get("as_of"))
        if topic == "risk":
            return risk(desk, day=kwargs.get("as_of"))
        if topic in ("industries", "industry", "sectors"):
            from .industry import panel as industry_panel

            day = kwargs.get("as_of") or desk.market.latest_bar_date()
            return industry_panel(desk.market, day)
        if topic == "limit_structure":
            from .tape import limit_structure as read_tape

            return read_tape(desk, day=kwargs.get("as_of"))
        if topic in ("tape_flow", "yesterday_limit"):
            from .tape import yesterday_limit_today as read_flow

            return read_flow(desk, day=kwargs.get("as_of"))
        if topic in ("cycle", "sentiment_transition", "mood"):
            # 情绪状态转移：今天这档炸板率/晋级率，历史上之后 1/3 天怎么走。
            # 和 regime 的分工：regime 看**指数**在四种状态下的前瞻收益，
            # cycle 看**情绪结构**自己的转移——"退潮日之后次日还退不退"。
            from .cycle import transition

            return transition(desk, kwargs.get("as_of"))
        if topic in ("seal_tape", "seal_decay", "封单轨迹"):
            # 封单的**过程**。收盘那一刻的封单额是结局；这条给的是它怎么走到
            # 那里的——涨停家数在第几帧见顶、哪些票的封单在收工前被撤。
            # 与 board 的分工：board 是单票这一档的历史分布，这条是今天的分帧。
            from .seal_tape import tape as read_tape

            return read_tape(desk, kwargs.get("as_of"),
                             offset=int(kwargs.get("offset") or 0),
                             limit=int(kwargs.get("limit") or 20))
        if topic in ("market_state", "state_of_market", "市场状态"):
            # 市场的**状态画像 + 条件分布**，不是择时信号。五个指标放到 rolling
            # 分位上（事实），再给历史上相似状态之后 5/10/20 日的收益分布
            # （n / p25 / median / p75 / 正收益占比 / IQR 宽度）。
            # 没有 action / score / ranking：分位低不代表便宜，中位数为正不代表会涨。
            # 与 regime 的分工：regime 给当前标签（趋势/波动/广度三档），
            # 这条给「像什么时候」以及「那时候之后发生了什么」。
            from service.research.market_state import market_state

            return market_state(desk.market.store, as_of=kwargs.get("as_of"),
                                k=int(kwargs.get("limit") or 120))
        if topic in ("breadth", "alpha_breadth", "data_breadth"):
            from service.research.breadth import breadth_read

            return breadth_read(desk)
        if topic in ("factors", "factor_panel", "alpha_sources"):
            return factor_read(
                desk, as_of=kwargs.get("as_of"),
                window=int(kwargs.get("window") or 120),
                factor=kwargs.get("factor"),
                detail=str(kwargs.get("detail") or "summary"),
                horizons=kwargs.get("horizons"),
            )
        if topic in ("industry_rank", "position_in_industry", "peer_rank"):
            # 个股在申万行业内的位置。和 profile.relative_strength 的分工：
            # 那个是 20/60 日的累计超额，这个是**当日**的相对位置——
            # 2026-11-02 的分众 20 日超额 +17pp，当日却排 25/28，两者都对。
            symbol = kwargs.get("symbol")
            if not symbol:
                return _needs_symbol("industry_rank")
            from .industry_rank import in_industry

            return in_industry(desk, str(symbol).upper(),
                               kwargs.get("as_of"),
                               level=str(kwargs.get("level") or "l2"))
        if topic == "auction":
            from .tape import auction as read_auction

            return read_auction(desk, day=kwargs.get("as_of"),
                                limit=int(kwargs.get("limit") or 10))
        if topic in ("anomaly", "catalyst"):
            from .tape import anomaly as read_anomaly

            return read_anomaly(desk, day=kwargs.get("as_of"),
                                symbol=kwargs.get("symbol"),
                                limit=int(kwargs.get("limit") or 10))
        if topic in ("lhb", "dragon_tiger"):
            from .tape import lhb as read_lhb

            return read_lhb(desk, day=kwargs.get("as_of"),
                            symbol=kwargs.get("symbol"),
                            limit=int(kwargs.get("limit") or 20),
                            seat=kwargs.get("seat"))
        if topic in ("news", "filings", "text"):
            return news(desk, day=kwargs.get("as_of"),
                        symbol=kwargs.get("symbol"),
                        days=int(kwargs.get("days") or 7),
                        limit=int(kwargs.get("limit") or 20),
                        kind="announcement" if topic == "filings" else None)
        if topic == "liquidity":
            symbol = kwargs.get("symbol")
            if not symbol:
                return _needs_symbol("liquidity")
            sizes = kwargs.get("sizes")
            if isinstance(sizes, str) and sizes.strip():
                sizes = tuple(int(x) for x in sizes.split(",") if x.strip().isdigit())
            return liquidity(desk, str(symbol).upper(), day=kwargs.get("as_of"),
                             **({"sizes": tuple(sizes)} if sizes else {}))
        if topic == "distribution":
            symbol = kwargs.get("symbol")
            if not symbol:
                return _needs_symbol("distribution")
            return distribution(desk, str(symbol).upper(), day=kwargs.get("as_of"))
        if topic in ("timing_distribution", "timing_buckets"):
            symbol = kwargs.get("symbol")
            if not symbol:
                return _needs_symbol("timing_distribution")
            return timing_distribution(desk, str(symbol).upper(),
                                       as_of=kwargs.get("as_of"))
        if topic in ("board", "limit_board"):
            symbol = kwargs.get("symbol")
            if not symbol:
                return _needs_symbol("board")
            from .board import profile as board_profile

            return board_profile(desk, str(symbol).upper(), day=kwargs.get("as_of"))
        if topic in ("board_check", "board_oos"):
            # 样本外检验：conditional 里的分布是全样本拟合的，这个回答"它还能不能用"。
            from .board import validate as board_validate

            return board_validate(desk, day=kwargs.get("as_of"),
                                  split=str(kwargs.get("split") or "20250101"))
        if topic in ("cards", "index", ""):
            return cards(desk)
        return {"ok": False, "error": f"未知 topic {topic!r}", "topics": cards(desk)["layers"]}
    except Exception as exc:  # noqa: BLE001 - knowledge must fail loudly, not crash the turn
        return {"ok": False, "topic": topic,
                "error": f"{type(exc).__name__}: {exc}"}
