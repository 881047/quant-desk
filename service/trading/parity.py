"""Shadow → Promotion 的 9 项一致性与 G1–G5 闸门。

命名独立
--------
所有键用 ``intent_parity_*``，与 ``params.yaml`` 的 ``research.shadow_*``
（「值不值得跟」）分开：这里比的是**两台引擎逐项是否一致**，不是策略好坏。
改这里不许动 ``research.shadow_gate=0.30`` / ``shadow_p_max=0.05``。

闸门口径（v4 §4 折中版）
------------------------
* G1 离线 parity：≥100 条 terminal 比较、跨 ≥10 交易日、≥3 regime 且每 regime ≥20。
  100 是**比较数**不是成交数，不依赖 20 笔平仓 KPI。
* G1' 真仓护栏：≥20 笔真实平仓才可讨论「这套系统行不行」；不足只标
  `insufficient_real_fills`，**不阻止离线 parity 运行**。
* G2 零容忍：重复成交 / 不可能成交 / 前视违规 / order-state 不一致，任一 >0 一票否决。
* G3 一致性：9 项一致率 ≥99%、中位成交价偏差 ≤10bps、P/L 对账误差 <0.1%。
  10bps 是数据精度如实标注（等于一档滑点），不是放松纪律。
* G4 中间态：60≤n<100 只报 `insufficient_shadow_samples`，不通过；n<60 → `no_parity`。
* G5 边界：通过只解锁「意图事件进入可结算口径」；`trigger.enabled` 仍默认
  false，**晋升判定不在本地自动发出**，激活必须走人。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from statistics import median
from typing import Any

PARITY_ITEMS = ("ticket_id", "symbol", "side", "qty", "status",
                "fill_basis", "price", "fees", "cash_after")

HARD_VIOLATIONS = ("duplicate_fill", "impossible_fill", "lookahead",
                   "order_state_mismatch")

BPS = 10_000.0
G1_MIN_COMPARISONS = 100
G1_MIN_DAYS = 10
G1_MIN_REGIMES = 3
G1_MIN_PER_REGIME = 20
G4_MID_FLOOR = 60
G3_MIN_CONSISTENCY = 0.99
G3_MAX_PRICE_GAP_BPS = 10.0
G3_MAX_PNL_ERROR_PCT = 0.1
REAL_FILL_MIN = 20


def _by_id(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    return {str(r.get("ticket_id")): dict(r) for r in rows or []}


def compare(live_rows: Iterable[Mapping[str, Any]] | None,
            shadow_rows: Iterable[Mapping[str, Any]] | None) -> dict:
    """按 ticket_id 对齐，逐项比 9 项并汇总四类硬闸。"""
    live, shadow = _by_id(live_rows), _by_id(shadow_rows)
    common = sorted(set(live) & set(shadow))
    comparisons: list[dict] = []
    mismatches = {item: 0 for item in PARITY_ITEMS}
    price_gaps: list[float] = []
    violations = {key: 0 for key in HARD_VIOLATIONS}
    for ticket_id in common:
        a, b = live[ticket_id], shadow[ticket_id]
        different = [item for item in PARITY_ITEMS
                     if a.get(item) != b.get(item)]
        for item in different:
            mismatches[item] += 1
        try:
            base = float(a.get("price") or 0)
            if base > 0 and b.get("price") is not None:
                price_gaps.append(abs(float(b["price"]) - base) / base * BPS)
        except (TypeError, ValueError):
            pass
        for key in HARD_VIOLATIONS:
            violations[key] += int(bool(a.get(key) or b.get(key)))
        comparisons.append({
            "ticket_id": ticket_id,
            "status": "match" if not different else "mismatch",
            "different_items": different,
        })

    checked = len(common) * len(PARITY_ITEMS)
    consistency = (1 - sum(mismatches.values()) / checked) if checked else 0.0
    live_cash = sum(float(r.get("cash_after") or 0) for r in live.values())
    shadow_cash = sum(float(r.get("cash_after") or 0) for r in shadow.values())
    pnl_error = (abs(live_cash - shadow_cash) / abs(live_cash) * 100
                 if live_cash else 0.0)
    return {
        "ok": True,
        "n": len(common),
        "unmatched_live": sorted(set(live) - set(shadow)),
        "unmatched_shadow": sorted(set(shadow) - set(live)),
        "items": list(PARITY_ITEMS),
        "mismatches": mismatches,
        "consistency_rate": round(consistency, 6),
        "median_price_gap_bps": round(median(price_gaps), 4) if price_gaps else None,
        "pnl_error_pct": round(pnl_error, 6),
        "hard_violations": violations,
        "comparisons": comparisons,
    }


def gate(
    comparison: Mapping[str, Any],
    *,
    trading_days: int = 0,
    regime_counts: Mapping[str, int] | None = None,
    real_fills: int = 0,
    trigger_enabled: bool = False,
    human_activated: bool = False,
    calibration_degraded: bool = False,
) -> dict:
    """G1–G5。**不自动发晋升**，也不改任何配置。

    ``calibration_degraded`` 来自 AKShare 离线校准集失败/限流：G3 的 **10bps
    闸门不放宽**（降级的免费源不能成为放松纪律的理由），但不允许 Promotion。
    兜底用 ``stk_mins``，报告写 ``calibration_degraded: true``。
    """
    n = int(comparison.get("n") or 0)
    regimes = regime_counts or {}
    g1 = (n >= G1_MIN_COMPARISONS and trading_days >= G1_MIN_DAYS
          and len(regimes) >= G1_MIN_REGIMES
          and all(int(v) >= G1_MIN_PER_REGIME for v in regimes.values()))
    violations = comparison.get("hard_violations") or {}
    g2 = all(int(violations.get(k) or 0) == 0 for k in HARD_VIOLATIONS)
    gap = comparison.get("median_price_gap_bps")
    g3 = (float(comparison.get("consistency_rate") or 0) >= G3_MIN_CONSISTENCY
          and (gap is None or float(gap) <= G3_MAX_PRICE_GAP_BPS)
          and float(comparison.get("pnl_error_pct") or 0) < G3_MAX_PNL_ERROR_PCT)

    if n < G4_MID_FLOOR:
        status = "no_parity"
    elif n < G1_MIN_COMPARISONS:
        status = "insufficient_shadow_samples"
    elif not (g1 and g2 and g3):
        status = "blocked"
    elif calibration_degraded:
        status = "calibration_degraded"
    else:
        status = "promotable"
    passed = status == "promotable"
    activated = bool(passed and human_activated and not calibration_degraded)

    gates = {
        "intent_parity_g1": {"passed": g1, "n": n, "trading_days": trading_days,
                             "regimes": dict(regimes),
                             "thresholds": {"comparisons": G1_MIN_COMPARISONS,
                                            "days": G1_MIN_DAYS,
                                            "regimes": G1_MIN_REGIMES,
                                            "per_regime": G1_MIN_PER_REGIME}},
        "intent_parity_g1_real": {"real_fills": int(real_fills),
                                  "min_for_edge_claim": REAL_FILL_MIN,
                                  "status": ("sufficient" if real_fills >= REAL_FILL_MIN
                                             else "insufficient_real_fills"),
                                  "blocks_parity": False},
        "intent_parity_g2": {"passed": g2, "violations": dict(violations),
                             "zero_tolerance": True},
        "intent_parity_g3": {"passed": g3,
                             "consistency_rate": comparison.get("consistency_rate"),
                             "median_price_gap_bps": gap,
                             "pnl_error_pct": comparison.get("pnl_error_pct"),
                             "thresholds": {"consistency": G3_MIN_CONSISTENCY,
                                            "price_gap_bps": G3_MAX_PRICE_GAP_BPS,
                                            "pnl_error_pct": G3_MAX_PNL_ERROR_PCT}},
        "intent_parity_g4": {"status": status, "mid_floor": G4_MID_FLOOR,
                             "full_floor": G1_MIN_COMPARISONS},
        "intent_parity_g5": {
            "unlocks": "意图事件进入可结算口径",
            "trigger_enabled": bool(trigger_enabled),
            "trigger_enabled_default_must_stay": False,
            "human_activation_required": True,
            "human_activated": bool(human_activated),
            "activated": activated,
            "calibration_degraded": bool(calibration_degraded),
            "promotion_allowed": not bool(calibration_degraded),
            "price_gap_gate_bps": G3_MAX_PRICE_GAP_BPS,
            "note": ("Promotion ≠ 自动入场 ≠ 系统有效性结论。"
                     "晋升判定不在本地自动发出；激活走 quant_playbook_propose + 人激活。"
                     "校准集降级时 10bps 闸门不放宽，且不允许 Promotion。"),
        },
    }
    if trigger_enabled and not human_activated:
        gates["intent_parity_g5"]["warning"] = (
            "开关已开但未见人激活记录，本次不视为合法激活。")
    return {
        "ok": True,
        "status": "activated" if activated else status,
        "passed": passed,
        "activated": activated,
        "gates": gates,
        "note": "命名 intent_parity_*，与 research.shadow_*（值不值得跟）是两个问题。",
    }
