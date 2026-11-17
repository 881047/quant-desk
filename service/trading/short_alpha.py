"""短线 α 候选：把当日封板结构与 1–2 日净市场超额放在同一张证据卡上。

这里是信息合成层，不是顾问：没有 action、价格指令、数量指令或收益承诺。
``candidates`` 只表示通过预先固定的研究筛选，``watch`` 表示均值为正但区间
仍跨零等证据不足的情形。是否交易由 WorkBuddy 判断，最终可成交性仍由票据 guard
重算。

口径固定在 ``short-alpha-v1``，避免看结果后换桶或换期限：
1. 股票池为派生收盘涨停池与同花顺封板面板的交集，排除北交所和一字板；
2. 从「板+封单+时段 → 板+封单 → 板+时段 → 仅板」选第一个同时具有
   T+1/T+2 标签、n>=30、n_days>=8 的桶；不因收益正负换桶；
3. 历史只含非一字封板样本。收益减全市场等权收益，再扣账户情景的完整
   费用、固定滑点和双边模型冲击；
4. T+1 收盘与 T+2 收盘的净超额均值都必须为正，且两个交易日块 95% 区间
   下界都为正才进入 candidates。其余正均值但区间跨零的进入 watch。

日线只能给历史可成交机会代理，不能证明当前排队成交。当前封板股的
``fill_probability`` 永远是 null，绝不把 schedule 或历史 low 当成成交承诺。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from service.adapters.symbols import ts_code as canonical_ts_code
from service.kernel.clock import SHANGHAI

from . import board, seal_tape
from .impact import estimate as impact_estimate
from .impact import round_trip_cost_bps

VERSION = "short-alpha-v1"
MIN_DAYS = 8
WATCH_LIMIT = 3


def _f(value: Any, digits: int = 6) -> float | None:
    return board._f(value, digits)


def _compact_stats(values: pd.Series, days: pd.Series, cost_bps: float,
                   *, sessions: int, exit_at: str) -> dict:
    stats = board._stats(values, cost_bps, days)
    out = {
        "sessions": sessions,
        "exit_at": exit_at,
        "n": stats.get("n", 0),
        "n_days": stats.get("n_days"),
        "insufficient": bool(stats.get("insufficient")),
        "ci_method": stats.get("ci_method"),
        "ci_status": stats.get("ci_status"),
    }
    if stats.get("insufficient"):
        out["reason"] = stats.get("why")
        return out
    out.update({
        "gross_excess_mean": stats.get("mean"),
        "cost_bps": _f(cost_bps, 2),
        "net_excess_mean": stats.get("net_mean"),
        "net_excess_mean_ci95": stats.get("net_mean_ci95"),
        "gross_excess_mean_ci95": stats.get("mean_ci95"),
        "p10_gross_excess": stats.get("p10"),
    })
    return out


def _select_bucket(panel: pd.DataFrame, row: pd.Series) -> tuple[dict | None, dict]:
    """Select by sample adequacy only, never by the sign of forward returns."""
    tradable = panel[panel["fill_kind"].ne("one_word")]
    boards = row.get("boards_bucket")
    tier = row.get("seal_tier")
    window = row.get("seal_window")
    steps = [
        ("板+封单+时段", (tradable["boards_bucket"] == boards)
         & (tradable["seal_tier"] == tier) & (tradable["seal_window"] == window)),
        ("板+封单", (tradable["boards_bucket"] == boards)
         & (tradable["seal_tier"] == tier)),
        ("板+时段", (tradable["boards_bucket"] == boards)
         & (tradable["seal_window"] == window)),
        ("仅板", tradable["boards_bucket"] == boards),
    ]
    diagnostics = []
    for rule, mask in steps:
        sub = tradable.loc[mask]
        common = sub[sub["excess_close_1"].notna() & sub["excess_close_2"].notna()]
        n = int(len(common))
        n_days = int(common["trade_date"].nunique())
        diagnostics.append({"rule": rule, "n": n, "n_days": n_days})
        if n >= board.MIN_BUCKET_N and n_days >= MIN_DAYS:
            return {"rule": rule, "frame": common}, {"ladder": diagnostics}
    return None, {
        "ladder": diagnostics,
        "reason": f"没有同时满足 n>={board.MIN_BUCKET_N}、n_days>={MIN_DAYS} 的条件桶",
    }


def _evidence(panel: pd.DataFrame, row: pd.Series, cost_bps: float) -> dict:
    selected, diagnostic = _select_bucket(panel, row)
    if selected is None:
        return {"available": False, **diagnostic}
    frame = selected["frame"]
    days = frame["trade_date"]
    horizons = {
        "t1_open": _compact_stats(frame["excess_open_1"], days, cost_bps,
                                  sessions=1, exit_at="T+1 open"),
        "t1_close": _compact_stats(frame["excess_close_1"], days, cost_bps,
                                   sessions=1, exit_at="T+1 close"),
        "t2_close": _compact_stats(frame["excess_close_2"], days, cost_bps,
                                   sessions=2, exit_at="T+2 close"),
    }
    unique_days = sorted(panel["trade_date"].unique())
    recent_cutoff = unique_days[-250] if len(unique_days) > 250 else unique_days[0]
    recent = frame[frame["trade_date"] >= recent_cutoff]
    recent_days = recent["trade_date"]
    recent_250 = {
        "cutoff": str(recent_cutoff),
        "role": "descriptive_stability_check_not_selection_gate",
        "t1_close": _compact_stats(recent["excess_close_1"], recent_days, cost_bps,
                                   sessions=1, exit_at="T+1 close"),
        "t2_close": _compact_stats(recent["excess_close_2"], recent_days, cost_bps,
                                   sessions=2, exit_at="T+2 close"),
    }
    return {
        "available": True,
        "selected_rule": selected["rule"],
        "selection_policy": "first_sample_adequate_bucket_not_best_return",
        "sample_basis": "non_one_word_resealed_with_both_t1_t2_labels",
        "ladder": diagnostic["ladder"],
        "horizons": horizons,
        "recent_250": recent_250,
    }


def _scenario(desk, symbol: str, day: str, row: pd.Series) -> dict:
    """Whole-lot account scenario, including model impact; never an order."""
    price = float(row.get("close") or 0)
    try:
        instrument = desk.market.instrument(symbol, day)
        summary = desk.book.summary()
        equity = float(summary.get("equity"))
        cash_free = float(summary.get("cash_free"))
        limits = desk.limits()
        from .analog import _sizing_cap_key
        cap_key = _sizing_cap_key(desk)
        cap = float(limits.get(cap_key) or 0)
        max_positions = int(limits.get("max_positions") or 3)
        lot_size = int(getattr(instrument, "lot_size", 100) or 100)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return {"account_ok": False, "reason": f"account_scenario_unavailable:{type(exc).__name__}"}
    one_lot_cny = price * lot_size
    budget = min(equity * cap, equity / max_positions, cash_free)
    lots = int(budget // one_lot_cny) if one_lot_cny > 0 and budget > 0 else 0
    base = {
        "reference_price": _f(price, 3),
        "price_basis": "T_close_daily_bar_not_live_quote",
        "lot_size": lot_size,
        "one_lot_cny": _f(one_lot_cny, 2),
        "cash_free": _f(cash_free, 2),
        "single_cap_key": cap_key,
        "scenario_budget_cny": _f(budget, 2),
        "account_is_historical": False,
        "fill_shape": row.get("fill_kind"),
        "queue_required_at_reference_price": True,
        "queue_position_known": False,
        "fill_probability": None,
        "order_created": False,
        "fillability_status": "queue_required_fill_unknown",
        "schedule_is_fill_proof": False,
        "account_scenario_is_not_fill_proof": True,
    }
    if lots < 1:
        return {**base, "account_ok": False,
                "reason": "one_lot_exceeds_cash_or_single_position_budget"}
    qty = lots * lot_size
    notional = qty * price
    try:
        fee = round_trip_cost_bps(notional, instrument)
        adv = float(desk.market.adv_map(day).get(symbol, 0.0))
        hist = desk.market.history(symbol, day, 21)
        rets = pd.to_numeric(hist["close"], errors="coerce").pct_change().dropna()
        sigma = float(rets.std()) if len(rets) > 2 else 0.0
        impact = impact_estimate(qty, price, adv, sigma)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return {**base, "account_ok": False,
                "reason": f"full_cost_unavailable:{type(exc).__name__}"}
    total_bps = float(fee["total_bps"]) + 2.0 * float(impact.impact_bps)
    return {
        **base,
        "account_ok": True,
        "scenario_qty": qty,
        "scenario_notional_cny": _f(notional, 2),
        "fees_and_fixed_slippage_bps": _f(fee["total_bps"], 2),
        "two_sided_model_impact_bps": _f(2.0 * impact.impact_bps, 2),
        "total_round_trip_cost_bps": _f(total_bps, 2),
        "total_round_trip_cost_cny": _f(notional * total_bps / 10_000.0, 2),
        # OOS 的 T+1 开盘成本仍用冻结时可见的 ADV/波动率，不能偷看入场日成交。
        "impact_inputs": {"adv_cny": float(adv), "sigma_daily": float(impact.sigma_daily)},
        "participation": _f(impact.participation),
        "cost_scope": "kernel_fees_taxes_fixed_slippage_plus_two_sided_model_impact",
        "fillability_scope": "daily_ohlc_historical_proxy_current_queue_fill_unknown",
    }


def _derived_universe(desk, day: str) -> tuple[set[str], dict[str, str]]:
    frame = desk.data.read("limit_pool_t0", day, day, columns=["code", "pool", "name"])
    if frame is None or frame.empty:
        raise ValueError("derived_limit_pool_empty")
    up = frame[frame["pool"].astype(str).eq("limit_up")]
    symbols = {canonical_ts_code(str(code).split(".")[0]) for code in up["code"]}
    names = {canonical_ts_code(str(row["code"]).split(".")[0]): str(row.get("name") or "")
             for _, row in up.iterrows()}
    return symbols, names


def _seal_map(desk, day: str) -> dict[str, dict]:
    """All per-name seal observations internally; the public response stays compact."""
    frame = seal_tape._frames(desk, day)
    if frame is None:
        return {}
    session = frame[frame["_in_session"]]
    comparable = session[session["_comparable"]]
    stamps = sorted(comparable["snapshot_ts"].unique())
    enough = len(stamps) >= seal_tape.MIN_FRAMES
    base = comparable if enough else frame
    horizon = stamps if enough else sorted(frame["snapshot_ts"].unique())
    rows = seal_tape._stock_series(base, "limit_up", horizon)
    return {str(row["code"]).split(".")[0]: row for row in rows}


def _base_compact(desk, boards: int, day: str, cost_bps: float) -> dict:
    base = board._base_rate(desk, boards, day, cost_bps) or {}
    buyable = base.get("buyable") or {}
    return {
        "available": bool(base.get("available") and buyable.get("available")),
        "n": buyable.get("n"),
        "n_days": buyable.get("n_days"),
        "reseal_rate": buyable.get("reseal_rate"),
        "reseal_rate_ci95": buyable.get("reseal_rate_ci95"),
        "required_reseal_rate": buyable.get("required_reseal_rate"),
        "gap_pp": buyable.get("gap_pp"),
        "if_reseal_mean": (buyable.get("if_reseal") or {}).get("mean"),
        "if_break_mean": (buyable.get("if_break") or {}).get("mean"),
        "scope": "same_height_all_touch_events_buyable_only_not_selected_conditional_bucket",
    }


def _positive_interval(stats: dict) -> bool:
    interval = stats.get("net_excess_mean_ci95") or [None, None]
    return interval[0] is not None and interval[0] > 0


def _horizon_net(evidence: dict, key: str) -> float | None:
    return ((evidence.get("horizons") or {}).get(key) or {}).get("net_excess_mean")


def _setup(row: pd.Series, symbol: str, name: str) -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "boards": int(row["boards"]) if pd.notna(row.get("boards")) else None,
        "seal_ratio": _f(row.get("seal_ratio")),
        "seal_window": row.get("seal_window"),
        "fill_kind": row.get("fill_kind"),
        "theme_cluster": _f(row.get("theme_cluster")),
        "reason": row.get("reason"),
    }


def analyze(desk, as_of: str | None = None, *, limit: int = 2) -> dict:
    day = as_of or desk.market.latest_bar_date()
    limit = max(0, min(int(limit), 2))
    panel = board._panel(desk, day)
    if panel is None:
        return {"ok": False, "as_of": day, "error": "board_panel_unavailable"}
    required = {"excess_open_1", "excess_close_1", "excess_close_2", "fill_kind"}
    if not required <= set(panel.columns):
        return {"ok": False, "as_of": day, "error": "short_alpha_panel_schema_missing",
                "missing": sorted(required - set(panel.columns))}
    try:
        derived, names = _derived_universe(desk, day)
    except (KeyError, ValueError, OSError) as exc:
        return {"ok": False, "as_of": day,
                "error": f"derived_limit_pool_unavailable:{type(exc).__name__}"}

    panel = panel.copy()
    ratios = panel["seal_ratio"].dropna()
    cuts = ((float(ratios.quantile(1 / 3)), float(ratios.quantile(2 / 3)))
            if len(ratios) > board.MIN_BUCKET_N else (np.nan, np.nan))
    panel["seal_tier"] = panel["seal_ratio"].map(
        lambda value: board._tier(float(value), cuts)
        if pd.notna(value) and np.isfinite(cuts[0]) else None)
    today = panel[(panel["trade_date"] == day) & panel["ts_code"].isin(derived)]
    today = today[~today["ts_code"].astype(str).str.endswith(".BJ")]
    seals = _seal_map(desk, day)
    latest = desk.market.latest_bar_date()
    excluded: Counter[str] = Counter()
    post_review: Counter[str] = Counter()
    reviews: list[dict] = []

    for _, row in today.iterrows():
        symbol = str(row["ts_code"])
        setup = _setup(row, symbol, names.get(symbol, ""))
        if row.get("fill_kind") == "one_word":
            excluded["one_word_no_touch_fill"] += 1
            continue
        execution = _scenario(desk, symbol, day, row)
        if not execution.get("account_ok"):
            excluded[str(execution.get("reason") or "execution_unavailable")] += 1
            continue
        evidence = _evidence(panel, row, float(execution["total_round_trip_cost_bps"]))
        if not evidence.get("available"):
            excluded["insufficient_common_t1_t2_sample"] += 1
            continue
        seal = seals.get(symbol.split(".")[0])
        if seal and not seal.get("at_last_frame"):
            excluded["not_in_last_comparable_seal_frame"] += 1
            continue
        net1 = _horizon_net(evidence, "t1_close")
        net2 = _horizon_net(evidence, "t2_close")
        if net1 is None or net2 is None or (net1 <= 0 and net2 <= 0):
            excluded["nonpositive_net_excess_both_horizons"] += 1
            continue
        lower = []
        for key in ("t1_close", "t2_close"):
            interval = evidence["horizons"][key].get("net_excess_mean_ci95") or [None, None]
            lower.append(interval[0] if interval[0] is not None else -np.inf)
        reviews.append({
            "setup": setup, "execution": execution, "evidence": evidence,
            "seal_tape": seal, "rank_key": (min(lower), min(net1, net2),
                                                int(evidence["horizons"]["t1_close"]["n_days"])),
        })

    reviews.sort(key=lambda item: item["rank_key"], reverse=True)
    candidates: list[dict] = []
    watch: list[dict] = []
    # Base-rate distributions are expensive; only the leading evidence cards need them.
    for review in reviews[:max(limit, WATCH_LIMIT)]:
        evidence = review["evidence"]
        h1 = evidence["horizons"]["t1_close"]
        h2 = evidence["horizons"]["t2_close"]
        net1 = h1.get("net_excess_mean")
        net2 = h2.get("net_excess_mean")
        qualifies = (net1 is not None and net2 is not None and net1 > 0 and net2 > 0
                     and _positive_interval(h1) and _positive_interval(h2))
        base = _base_compact(desk, int(review["setup"]["boards"] or 1), day,
                             float(review["execution"]["total_round_trip_cost_bps"]))
        counter = []
        if not _positive_interval(h1) or not _positive_interval(h2):
            counter.append("至少一个期限的净超额95%区间下界未高于0")
        if net1 is not None and net2 is not None and (net1 <= 0 or net2 <= 0):
            counter.append("T+1/T+2净超额均值方向不一致")
        for key in ("t1_close", "t2_close"):
            recent = evidence["recent_250"][key]
            interval = recent.get("net_excess_mean_ci95") or [None, None]
            if (recent.get("net_excess_mean") is None or recent["net_excess_mean"] <= 0
                    or interval[0] is None or interval[0] <= 0):
                counter.append(f"最近250个交易日诊断的{key}净均值或区间下界未确认为正")
        if not base.get("available"):
            counter.append("同身位无条件基准分布不可用")
        elif base.get("gap_pp") is not None and base["gap_pp"] > 0:
            counter.append("同身位可买组回封率仍低于盈亏平衡门槛")
        if not review.get("seal_tape"):
            counter.append("没有该股当日分帧封单确认")
        elif (review["seal_tape"].get("from_peak_pct") is not None
              and review["seal_tape"]["from_peak_pct"] <= -30):
            counter.append("封单较日内峰值回撤超过30%；仅作反证，不作为峰值回撤硬筛")
        card = {
            **review["setup"],
            "status": "research_candidate" if qualifies else "watch",
            "evidence_grade": "exploratory_not_confirmed_alpha",
            "evidence": evidence,
            "base_rate": base,
            "execution": review["execution"],
            "seal_tape": (None if not review.get("seal_tape") else {
                key: review["seal_tape"].get(key)
                for key in ("frames", "seen_ratio", "from_peak_pct", "change_pct",
                            "at_last_frame", "first_ts", "last_ts")
            }),
            "counter_evidence": counter,
            "invalidation_checks": [
                "任一主期限净超额区间下界不再高于0时降级为watch",
                "封单持续衰减且末帧前出池时视为当前结构反证",
                "一字不可成交或一手超过现金/单票预算时排除",
            ],
        }
        if qualifies and len(candidates) < limit:
            candidates.append(card)
        elif qualifies:
            post_review["candidate_limit_overflow"] += 1
        elif len(watch) < WATCH_LIMIT:
            # 观察卡只保留降级判断所需的最小证据，避免 2+3 张全量卡越过 16 KB。
            watch.append({
                **review["setup"],
                "status": "watch",
                "evidence_grade": "exploratory_not_confirmed_alpha",
                "selected_rule": evidence["selected_rule"],
                "horizons": {key: evidence["horizons"][key] for key in ("t1_close", "t2_close")},
                "recent_250": {
                    "cutoff": evidence["recent_250"]["cutoff"],
                    **{
                        key: {field: evidence["recent_250"][key].get(field)
                              for field in ("n", "n_days", "net_excess_mean", "net_excess_mean_ci95")}
                        for key in ("t1_close", "t2_close")
                    },
                },
                "counter_evidence": counter,
            })
        else:
            post_review["evidence_below_candidate_threshold"] += 1

    post_review["not_selected_after_top_review"] += max(0, len(reviews) - max(limit, WATCH_LIMIT))
    response = {
        "ok": True,
        "version": VERSION,
        "as_of": day,
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "time_basis": "daily_close_signal_plus_requested_day_pool_tape_current_account_snapshot",
        "signal_pit_safe": True,
        "pit_safe": day == latest,
        "account_is_historical": False,
        "pit_note": ("历史as_of只回放信号；现金和账户上限始终是当前快照，"
                     "因此历史请求的顶层pit_safe=false"),
        "horizon_sessions": [1, 2],
        "benchmark": {
            "name": "all_daily_stocks_equal_weight_same_session_forward_return",
            "price_basis": "unadjusted_ohlc_with_nonconsecutive_and_pre_close_jump_excluded",
            "formula": "net_excess = stock_return - market_equal_weight_return - full_round_trip_cost",
        },
        "universe": {
            "scope": "derived_limit_up_intersect_ths_limit_up_non_bj_close_sealed",
            "derived_limit_up": len(derived),
            "matched_current": int(len(today)),
            "reviewed": len(reviews),
            "excluded": int(sum(excluded.values())),
            "excluded_reasons": dict(excluded),
            "post_review": dict(post_review),
            "not_covered": ["低吸", "炸板反转", "非涨停全市场排名", "北交所"],
        },
        "selection_policy": {
            "bucket": "first_n>=30_and_n_days>=8_with_both_horizon_labels",
            "candidate": "t1_close_and_t2_close_net_mean>0_and_both_day_block_ci95_lower>0",
            "watch": "positive_mean_but_interval_or_direction_does_not_pass",
            "ranking": "minimum_horizon_ci_lower_then_minimum_horizon_net_mean_then_n_days",
            "max_candidates": limit,
            "recent_250": "descriptive_stability_check_not_selection_gate",
            "multiple_testing": "exploratory_intervals_not_bh_adjusted_across_names_or_buckets",
        },
        "candidate_count": len(candidates),
        "candidates": candidates,
        "watch": watch,
        "no_candidate_reason": (None if candidates else
                                "limit=0：调用方主动关闭candidate输出，仅保留watch与排除原因"
                                if limit == 0 else
                                "没有个股同时通过可买性、共同样本、两个期限的正净均值及正区间下界"),
        "provenance": {
            "datasets": ["limit_pool_t0", "ths_limit_up", "daily", "daily_basic",
                         "pool_tape", "paper_account_current"],
            "panel_window": [str(panel["trade_date"].min()), str(panel["trade_date"].max())],
            "panel_rows": int(len(panel)),
        },
        "caveats": [
            "candidates是探索性研究候选，不是已证实alpha、收益承诺或买入指令",
            "当前封板股排队位置与成交概率未知；历史日线可买代理不等于当前必能成交",
            "区间按交易日块bootstrap，但跨股票和条件桶探索未做BH校正",
            "账户现金与上限是当前快照；历史请求不能用于账户级PIT绩效断言",
            "最终数量、现金、费用、T+1和风控仍由proposal/ticket guard重算；本调用不创建订单",
        ],
    }
    return response


def evidence_pack(desk, as_of: str | None = None, *, symbols: list[str] | None = None,
                  limit: int = 8, offset: int = 0) -> dict:
    """Evidence only: WorkBuddy, not this module, decides which names have alpha.

    Rows use stable symbol order. No candidate/watch verdict, return ranking or
    post-hoc rejection is applied. Pagination happens before the expensive cards,
    so the caller can inspect the whole universe without crossing the host budget.
    """
    day = as_of or desk.market.latest_bar_date()
    limit = max(1, min(int(limit), 12))
    offset = max(0, int(offset))
    panel = board._panel(desk, day)
    if panel is None:
        return {"ok": False, "as_of": day, "error": "board_panel_unavailable"}
    try:
        derived, names = _derived_universe(desk, day)
    except (KeyError, ValueError, OSError) as exc:
        return {"ok": False, "as_of": day,
                "error": f"derived_limit_pool_unavailable:{type(exc).__name__}"}
    requested = None
    if symbols:
        requested = {canonical_ts_code(str(s).strip().split(".")[0]) for s in symbols}
    panel = panel.copy()
    ratios = panel["seal_ratio"].dropna()
    cuts = ((float(ratios.quantile(1 / 3)), float(ratios.quantile(2 / 3)))
            if len(ratios) > board.MIN_BUCKET_N else (np.nan, np.nan))
    panel["seal_tier"] = panel["seal_ratio"].map(
        lambda value: board._tier(float(value), cuts)
        if pd.notna(value) and np.isfinite(cuts[0]) else None)
    today = panel[(panel["trade_date"] == day) & panel["ts_code"].isin(derived)]
    today = today[~today["ts_code"].astype(str).str.endswith(".BJ")]
    if requested is not None:
        today = today[today["ts_code"].isin(requested)]
    today = today.sort_values("ts_code")
    total = int(len(today))
    page = today.iloc[offset:offset + limit]
    seals = _seal_map(desk, day)
    rows = []
    excluded: Counter[str] = Counter()
    for _, event in page.iterrows():
        symbol = str(event["ts_code"])
        setup = _setup(event, symbol, names.get(symbol, ""))
        evidence_id = f"short-alpha-evidence:{day}:{symbol}"
        base_row = {
            **setup, "evidence_id": evidence_id,
            "evidence_grade": "historical_exploratory_information_not_alpha_verdict",
            "model_judgment_required": True,
        }
        if event.get("fill_kind") == "one_word":
            excluded["one_word_no_touch_fill"] += 1
            rows.append({**base_row, "eligible_for_model_judgment": False,
                         "boundary": "one_word_no_touch_fill", "evidence": {"available": False},
                         "execution": {"account_ok": False, "order_created": False}})
            continue
        execution = _scenario(desk, symbol, day, event)
        if not execution.get("account_ok"):
            excluded[str(execution.get("reason") or "execution_unavailable")] += 1
            rows.append({**base_row, "eligible_for_model_judgment": False,
                         "boundary": execution.get("reason"), "evidence": {"available": False},
                         "execution": execution})
            continue
        evidence = _evidence(panel, event, float(execution["total_round_trip_cost_bps"]))
        seal = seals.get(symbol.split(".")[0])
        base = (_base_compact(desk, int(setup["boards"] or 1), day,
                              float(execution["total_round_trip_cost_bps"]))
                if evidence.get("available") else {"available": False})
        counter = []
        if not evidence.get("available"):
            counter.append("共同T+1/T+2历史样本不足；只能使用其他事实，不能虚构条件分布")
        else:
            for key in ("t1_close", "t2_close"):
                horizon = evidence["horizons"][key]
                net = horizon.get("net_excess_mean")
                interval = horizon.get("net_excess_mean_ci95") or [None, None]
                if net is not None and net <= 0:
                    counter.append(f"{key}历史净超额均值非正")
                if interval[0] is None or interval[0] <= 0:
                    counter.append(f"{key}历史净超额95%区间下界未高于0")
                recent = evidence["recent_250"][key]
                if (recent.get("net_excess_mean") is None
                        or recent["net_excess_mean"] <= 0):
                    counter.append(f"最近250个交易日诊断的{key}净均值未确认为正")
        if not base.get("available"):
            counter.append("同身位无条件基准分布不可用")
        if not seal:
            counter.append("没有该股当日分帧封单确认")
        elif seal.get("at_last_frame") is False:
            counter.append("该股不在最后可比封板帧中")
        elif (seal.get("from_peak_pct") is not None
              and seal["from_peak_pct"] <= -30):
            counter.append("封单较日内峰值回撤超过30%")
        rows.append({
            **base_row,
            "eligible_for_model_judgment": True,
            "evidence": evidence,
            "base_rate": base,
            "execution": execution,
            "seal_tape": (None if not seal else {
                key: seal.get(key) for key in (
                    "frames", "seen_ratio", "from_peak_pct", "change_pct",
                    "at_last_frame", "first_ts", "last_ts")
            }),
            "counter_evidence": counter,
            "known_limits": [
                "历史条件分布不是个股收益承诺",
                "当前封板排队位置与成交概率未知",
                "日线机会代理不等于实际成交",
            ],
        })
    next_offset = offset + len(page)
    return {
        "ok": True, "version": "short-alpha-evidence-v1", "as_of": day,
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "decision_contract": {
            "information_provider": "quant_kernel",
            "judgment_owner": "WorkBuddy",
            "row_order": "stable_symbol_order_not_return_ranking",
            "contains_kernel_alpha_verdict": False,
            "order_created": False,
        },
        "horizon_sessions": [1, 2],
        "benchmark": {
            "name": "all_daily_stocks_equal_weight_same_session_forward_return",
            "formula": "historical_net_excess = stock_return - market_return - full_round_trip_cost",
        },
        "universe": {
            "scope": "derived_limit_up_intersect_ths_limit_up_non_bj_close_sealed",
            "matched_current_after_requested_filter": total,
            "page_excluded_reasons": dict(excluded),
            "not_covered": ["低吸", "炸板反转", "非涨停全市场排名", "北交所"],
        },
        "page": {"offset": offset, "limit": limit, "returned": len(rows),
                 "next_offset": next_offset if next_offset < total else None},
        "rows": rows,
        "provenance": {
            "datasets": ["limit_pool_t0", "ths_limit_up", "daily", "daily_basic",
                         "pool_tape", "paper_account_current"],
            "panel_window": [str(panel["trade_date"].min()), str(panel["trade_date"].max())],
            "panel_rows": int(len(panel)),
        },
        "caveats": [
            "内核只提供证据，不给candidate/watch判断；WorkBuddy必须独立说明选择与失效条件",
            "历史区间是探索性证据，跨股票和条件桶未做BH校正",
            "账户现金与上限是当前快照；历史as_of不是账户级PIT回测",
            "本接口不创建订单，也不接受价格或数量指令",
        ],
    }
