"""Pre-registered promotion gate for the only switch that spends money.

`trigger.observe` lets the model write conditional buy/sell points and record a
shadow trail without cash. `trigger.enabled` is the only flag that lets the
capture loop approve an armed ticket and actually spend money. These are not
the same decision, so the second one gets a written, numeric bar.

The bar is deliberately defined *before* looking at any result. A promotion
criteria decided after a flattering backtest is a rationalisation with a number
attached. Every gate has a fail condition as well as a pass condition, and
demotion is as mechanical as promotion: once `enabled=true`, a gate that falls
back out of compliance turns this script into a warning via postclose_check.

    python scripts/promotion_gate.py            # JSON
    python scripts/promotion_gate.py --human    # readable
    python scripts/promotion_gate.py --strict   # exit 1 only on a violation

Read-only: nothing here writes, orders, edits config, or opens a switch.
A blocked gate while `enabled=false` is the *compliant* state, not a failure.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8811"
# 直接 `python scripts/promotion_gate.py` 时 sys.path 只有 scripts/，
# 而 collect() 要 import service.*。pytest 从仓库根跑时不需要这一行。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _token() -> str:
    cfg = Path.home() / ".workbuddy" / "mcp.json"
    return json.loads(cfg.read_text(encoding="utf-8"))[
        "mcpServers"]["quant-desk"]["env"]["QUANT_TOKEN"]


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"X-Quant-Token": token})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def _config() -> dict:
    from service import settings

    return {
        "min_memo_n": settings.integer("promotion.min_memo_n"),
        "min_bss": settings.num("promotion.min_bss"),
        "min_accepted_hypotheses": settings.integer(
            "promotion.min_accepted_hypotheses"),
        "atr_multiple": settings.num("promotion.atr_multiple"),
        "benchmark_symbol": settings.text("promotion.benchmark_symbol"),
        "max_trade_risk": settings.num("risk.max_trade_risk"),
    }


def _finite(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _shown(value) -> str:
    """0.0 -> "0", -0.2282 stays as-is; counts should read like counts."""
    number = _finite(value)
    if number is None:
        return "不可读"
    return str(int(number)) if number == int(number) else str(number)


def evaluate(facts: dict, cfg: dict | None = None) -> dict:
    """Pure numeric eligibility check; missing data always fails closed."""
    cfg = cfg or _config()
    checks: list[dict] = []

    def add(name: str, ok, detail: str, fix: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "fix": fix})

    arena = facts.get("arena") or {}
    n = _finite(arena.get("n"))
    minimum = _finite(arena.get("min_decisions")) or 20
    ci_low = _finite(arena.get("ci_low"))
    ok = n is not None and n >= minimum and ci_low is not None and ci_low > 0
    sample_cases = facts.get("sample_cases")
    isolation = (
        f"样本账本已平仓 {_shown(sample_cases)} 笔，但它在 data/samples.sqlite，"
        "与 arena 读的 data/paper.sqlite 物理隔离，不计入 edge.n。"
        if sample_cases is not None else
        "样本账本笔数不可读")
    add("decisions", ok,
        f"可判定平仓 {_shown(n)}/{_shown(minimum)}；"
        f"超额 95%CI 下界={ci_low if ci_low is not None else '样本不足，未出区间'}；"
        + isolation,
        "主账户 edge 需要 directed_by=agent 的已平仓 case；样本账本的 20 笔 KPI "
        "是另一套（结算笔数，不是收益率），不能填进这个数。"
        "当前 observe=true/enabled=false 且主账本保持空仓，edge.n 不会自然增长——"
        "这是设计上的闭环，不是『再等几天就好』。破环必须由人明确选择："
        "① 主账户走不带 contingency 的普通提案（会违背零资金约定）；"
        "② 改门禁口径让样本账本参与 edge 估计（会混入研究成本，污染测量）；"
        "③ 保持影子期，接受账户级 α 现阶段不可判定")

    memo = facts.get("memo") or {}
    memo_n = _finite(memo.get("n"))
    bss = _finite(memo.get("bss"))
    ok = (memo_n is not None and memo_n >= cfg["min_memo_n"]
          and bss is not None and bss > cfg["min_bss"])
    add("calibration", ok,
        f"已结算断言 {_shown(memo_n)}/{cfg['min_memo_n']}；"
        f"bss={bss if bss is not None else '不可读'}（要求 >{cfg['min_bss']}）",
        "每份研报留带概率/期限/证伪条件的 memo，由 Brier 结算；"
        "bss<=0 表示还不如只会报基础率")

    hyp = facts.get("hypotheses") or {}
    accepted = _finite(hyp.get("accepted"))
    total = _finite(hyp.get("total"))
    ok = accepted is not None and accepted >= cfg["min_accepted_hypotheses"]
    add("hypotheses", ok,
        f"通过成本/DSR 的策略 {_shown(accepted)}"
        f"/{cfg['min_accepted_hypotheses']}（在案 {_shown(total)} 条）",
        "quant_hypothesis(add) 预注册 spec，run_research.py 由统计裁判判；"
        "2x 成本 Sharpe 与 DSR 必须过 bar")

    account = facts.get("account") or {}
    bench = facts.get("benchmark") or {}
    equity = _finite(account.get("equity"))
    lot = _finite(account.get("lot")) or 100
    last = _finite(bench.get("last"))
    atr_pct = _finite(bench.get("atr20_pct"))
    risk = _finite(cfg.get("max_trade_risk"))
    required = None
    if None not in (last, atr_pct, risk) and risk and risk > 0:
        # 2.5xATR 止损一手风险 = multiple * atr% * last * lot；
        # 它必须装得进 equity * max_trade_risk。反解所需最小权益。
        required = (cfg["atr_multiple"] * (atr_pct / 100.0) * last * lot) / risk
    ok = (equity is not None and required is not None and equity >= required)
    equity_txt = _shown(equity)
    if required is None:
        detail = f"权益 {equity_txt} 元；基准价/ATR 不可读，按 fail-closed 处理"
    else:
        detail = (f"权益 {equity_txt} 元；基准 {bench.get('symbol', '?')} "
                  f"last={last} / ATR20={atr_pct}% 下，{cfg['atr_multiple']}xATR "
                  f"止损一手需权益 ≥{required:.2f} 元")
        if equity and required > equity:
            detail += (f"，差 {required - equity:.2f} 元"
                       f"（+{(required / equity - 1) * 100:.1f}%）")
    add("sizing", ok, detail,
        "不注资、不调风控阈值（会污染历史 KPI 可比性 / 等于为下单放松风控）；"
        "20 笔 KPI 交样本账本，或换低波动低价标的")

    flags = facts.get("flags") or {}
    observe = flags.get("observe") is True
    add("observe", observe, f"trigger.observe={flags.get('observe')}",
        "先跑影子阶段，模型写条件入场点并留可结算轨迹，再谈资金")

    blockers = [c["name"] for c in checks if not c["ok"]]
    eligible = len(checks) - len(blockers)
    session = facts.get("session") or {}
    in_session = session.get("in_session") is True
    enabled = flags.get("enabled") is True
    compliance = ("violation" if enabled and blockers else "ok")

    return {
        "gate": "ready" if not blockers else "blocked",
        "eligible": eligible,
        "total": len(checks),
        "blockers": blockers,
        "checks": checks,
        "runtime": {
            "in_session": in_session,
            "label": session.get("label"),
            "note": ("资格齐备也只能在交易时段开启；这是时机，不是资格"
                     if not in_session else "交易时段"),
        },
        "flags": {"observe": observe, "enabled": enabled},
        "compliance": compliance,
        "decision": ("保持 trigger.enabled=false" if not enabled
                     else ("门禁已过，可按既有纪律执行" if not blockers
                           else "资金开关已开但门禁不满足：按降级规则关回")),
        "rule": ("门槛在看到结果之前写死；升级与降级都是机械的。"
                 "门禁只读，不会替任何人打开开关。"),
    }


def collect() -> dict:
    """Aggregate the facts the gate needs. Read-only, no side effects."""
    from service.trading import build_desk
    from service.kernel.units import SHARES_PER_LOT

    cfg = _config()
    token = _token()
    arena_raw = _get("/arena", token)
    edge = arena_raw.get("edge") or {}
    arena = {
        "n": edge.get("n"),
        "min_decisions": arena_raw.get("min_decisions_for_an_interval"),
        "ci_low": edge.get("ci_low"),
        "ci_high": edge.get("ci_high"),
    }

    desk = build_desk()
    cal = desk.paper.calibration() or {}
    memo = {"n": cal.get("n"), "bss": cal.get("bss")}
    equity = desk.book.summary().get("equity")
    account = {"equity": equity, "lot": SHARES_PER_LOT}

    connection = sqlite3.connect(ROOT / "data" / "paper.sqlite")
    try:
        rows = connection.execute("select status, count(*) from hypotheses group by status").fetchall()
    finally:
        connection.close()
    by_status = {str(k): int(v) for k, v in rows}
    hypotheses = {"accepted": by_status.get("accepted", 0),
                  "total": sum(by_status.values())}

    # The 20-close KPI is accumulated in a *separate* sqlite. Reporting it
    # alongside edge.n is the only way to make the structural gap visible:
    # without it, "0/20" reads as "not enough time has passed" when in fact
    # the sample book's closes can never enter this number.
    sample_cases = None
    try:
        samples = sqlite3.connect(f"file:{ROOT / 'data' / 'samples.sqlite'}?mode=ro", uri=True)
        try:
            sample_cases = int(samples.execute("select count(*) from cases").fetchone()[0])
        finally:
            samples.close()
    except sqlite3.Error:
        sample_cases = None

    symbol = cfg["benchmark_symbol"]
    profile = _get(f"/symbol/{symbol}/profile", token)
    technicals = profile.get("technicals") or {}
    quote = _get(f"/market/quote?symbols={symbol}", token)
    rows = quote.get("quotes") or []
    last = rows[0].get("last") if rows else None
    benchmark = {"symbol": symbol, "last": last,
                 "atr20_pct": technicals.get("atr20_pct")}

    from service import settings
    status = _get("/market/status", token)
    session = status.get("session") or {}
    flags = {"observe": settings.flag("trigger.observe"),
             "enabled": settings.flag("trigger.enabled")}

    return {
        "arena": arena, "memo": memo, "hypotheses": hypotheses,
        "account": account, "benchmark": benchmark,
        "sample_cases": sample_cases,
        "flags": flags,
        "session": {"in_session": session.get("in_session"),
                    "label": session.get("label")},
        "config": {k: v for k, v in cfg.items() if k != "benchmark_symbol"},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--human", action="store_true", help="readable output")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 only when enabled=true but a gate is blocked")
    args = parser.parse_args(argv)

    facts = collect()
    out = evaluate(facts)
    if args.human:
        print(f"晋级门禁  gate={out['gate']}  资格 {out['eligible']}/{out['total']}  "
              f"compliance={out['compliance']}")
        for c in out["checks"]:
            print(f"  [{'ok  ' if c['ok'] else 'BLOCK'}] {c['name']:12s} {c['detail']}")
        print(f"  runtime: in_session={out['runtime']['in_session']} "
              f"({out['runtime']['label']})")
        print(f"  决定: {out['decision']}")
        print(f"  blockers: {out['blockers']}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if args.strict and out["compliance"] == "violation" else 0


if __name__ == "__main__":
    sys.exit(main())
