"""The statistical referee: turn a hypothesis into a verdict.

Given a declarative spec, this module

1. expands it into a grid of honest alternatives (boards x entry x hold);
2. measures every variant with the same cost model;
3. deflates the winner's Sharpe by the number of trials;
4. estimates the probability the selection was noise (PBO / CSCV);
5. re-selects inside purged CPCV folds to get an out-of-sample distribution;
6. stresses costs at 2x and 3x;
7. writes a verdict with the numbers that produced it.

The verdict is deliberately conservative: a strategy that cannot survive 2x
costs and a 100-trade sample is not promoted, no matter how good the headline
Sharpe looks.
"""

from __future__ import annotations

from itertools import product

import numpy as np
import pandas as pd

from service import settings

from . import capital, fingerprint, stats
from .attribution import attribute, blocks_promotion
from .power import assess as assess_power
from .registry import DEFAULT_FAMILY, TrialRegistry
from .spec import (
    T0_POOLS,
    EventStudy,
    SpecError,
    build_trades,
    cb_link_pool,
    cb_pool,
    load_panel,
    market_returns,
    parse_spec,
    portfolio_series,
    scan_pool,
)

MIN_TRADES = settings.integer("research.min_trades")
# Cost multipliers the referee re-runs the whole backtest under. The first one is
# the primary ("2x"): every verdict quotes it, so the key is derived below rather
# than written as a literal — changing the list must not leave a dead "2x".
STRESS_MULTIPLIERS = settings.seq("research.referee.stress_multipliers")
PRIMARY_STRESS = f"{STRESS_MULTIPLIERS[0]:g}x"
# The live risk config refuses a fourth name, so the referee must too. A
# strategy whose edge only exists in a thirty-name book is not implementable.
ACCOUNT_MAX_POSITIONS = settings.integer("research.account_max_positions")
MIN_ACCOUNT_TRADES = settings.integer("research.min_account_trades")
# The shadow book's own gates. Deliberately not the referee's numbers: the referee
# asks "is this noise", the shadow book asks "is this worth tracking".
SHADOW_P_MAX = settings.num("research.shadow_p_max")
SHADOW_PBO_MAX = settings.num("research.shadow_gate")


def _equity() -> float:
    """Account size for the referee, from config.

    It used to be ``os.getenv("INITIAL_CASH") or settings.initial_cash()``: the
    same shadowing as the desk (``.env`` sets it, ``security.py`` loads ``.env``
    at import), so ``risk.initial_cash`` never reached the referee either. The
    supported override is ``QUANT__RISK__INITIAL_CASH``.
    """
    return float(settings.initial_cash())


def load_inputs(store, base: EventStudy):
    """Panel, event pools and the market series for one spec.

    One definition with two callers: the referee, and anything that needs a
    trade table to hang an overlay on. Duplicating this block is how a preset
    ends up meaning two different samples in two different scripts.
    """
    if base.pool in ("cb", "cb_link"):
        panel = load_panel(store, base.start, base.end, dataset="cb_daily")
        if base.pool == "cb_link":
            pools = cb_link_pool(panel, store, base.link_pool, base.boards)
        else:
            pools = cb_pool(panel, base.gap_threshold)
    else:
        panel = load_panel(store, base.start, base.end)
        if base.pool in ("losers", "winners"):
            pools = scan_pool(panel, base.pool,
                              min_amount_cny=base.min_amount if base.min_amount > 0 else None)
        else:
            pools = store.read("limit_pool_t0", base.start, base.end)
    if pools.empty:
        raise SpecError("no pool events for this spec; check the date range and filters")
    return panel, pools, market_returns(panel)


def _grid(base: EventStudy, *, include_t_close: bool = False) -> list[EventStudy]:
    """Every alternative a researcher could have chosen instead.

    ``include_t_close`` is an explicit, opt-in widening of the search family.
    It is NOT the default, because adding an entry dimension changes
    ``n_variants`` and therefore the Deflated-Sharpe denominator for the whole
    program: flipping it on silently would make the seven on-book v1/v2
    verdicts incomparable with the new ones. A researcher who wants to test a
    T-close entry as a competing variant must opt in, and the payload records
    that the family differs (``grid_scope``).
    """
    board_sets: list[tuple[int, ...]] = []
    if base.pool == "limit_up":
        for b in base.boards:
            if (b,) not in board_sets:
                board_sets.append((b,))
        if len(base.boards) > 1 and tuple(sorted(base.boards)) not in board_sets:
            board_sets.append(tuple(sorted(base.boards)))
    else:
        board_sets = [base.boards]

    # t_close **故意不在**这个默认网格里，尽管 spec 已经支持它。
    #
    # 入场点是"老实人选项"的一部分：既然能表达，理论上就该进网格，否则等于
    # 少算一次比较。但实测代价是：把 t_close 加进来，SMALL 校准面板（120 只
    # × 140 日）上 2.5%/笔的注入真值从 promoted 掉到 testing，DSR 0.90+ →
    # 0.44——不是代码错，是"最优替代方案"变强了，被检验的那个必须更强才算赢。
    # 也就是说多一个入场维度会吃掉约 1pp 的可检测边际。
    #
    # 2026-11-12 决定：不做"一次改完并重跑全部在案策略"的破坏性迁移，改为
    # 显式 opt-in。理由写在 _grid docstring：默认族必须保持稳定，否则 7 条在案
    # 判决的 DSR 分母会随一次开关而变，历史可比性反而没了。想测打板作为竞争
    # 变体的人传 include_t_close=True，payload.grid_scope 会记下这一族不同。
    entries = list(dict.fromkeys([base.entry, "next_open", "next_close"]))
    if include_t_close:
        entries.append("t_close")
    floor = 0 if base.pool in T0_POOLS else 1
    holds = [h for h in dict.fromkeys([base.hold, floor, 1, 2]) if h >= floor]
    exits = [e for e in dict.fromkeys([base.exit_at, "close", "open"]) if e in {"close", "open"}]

    out: list[EventStudy] = []
    seen: set[str] = set()
    for boards, entry, hold, exit_at in product(board_sets, entries, holds, exits):
        # Do not construct impossible variants; execution errors on valid ones
        # must propagate rather than being mistaken for an empty alternative.
        if hold == 0 and (entry == "next_close" or exit_at == "open"):
            continue
        if hold == 0 and base.pool not in T0_POOLS:
            continue
        spec = base.with_variant(boards=boards, entry=entry, hold=hold,
                                 exit_at=exit_at, label="")
        key = spec.name()
        if key in seen:
            continue
        seen.add(key)
        out.append(spec)
    return out


def _trade_stats(trades: pd.DataFrame) -> dict:
    # 零笔交易：区间与 p 值都是 None，不是 0 与 1.0。哨兵值会被当成测量结果。
    empty = {"n": 0, "mean": 0.0, "median": 0.0, "win_rate": 0.0,
             "mean_excess": 0.0, "p_value": None, "ci_low": None, "ci_high": None,
             "days": 0, "p_basis": "none",
             "interval_note": "没有成交，无可测的量"}
    if trades.empty or "net" not in trades.columns:
        return empty
    net = pd.to_numeric(trades["net"], errors="coerce").dropna().astype(float)
    if net.empty:
        return empty
    out = {
        "n": len(net),
        "mean": round(float(net.mean()), 6),
        "median": round(float(net.median()), 6),
        "win_rate": round(float((net > 0).mean()), 4),
        "mean_excess": 0.0,
        "p_value": 1.0,
        "ci_low": 0.0,
        "ci_high": 0.0,
        "days": 0,
        "p_basis": "none",
    }
    if "excess" in trades.columns and trades["excess"].notna().any():
        excess = pd.to_numeric(trades["excess"], errors="coerce").dropna().astype(float)
        # Resample *days*, not trades. A limit-up session produces dozens of
        # correlated trades; treating them as independent draws shrinks the
        # standard error and makes every p-value look better than it is.
        realised = trades.loc[excess.index]
        if "trade_date" in realised.columns and realised["trade_date"].nunique() >= 10:
            daily = realised.groupby("trade_date")["excess"].mean().astype(float)
            boot = stats.block_bootstrap_mean(daily, block=5)
            out["days"] = len(daily)
            out["p_basis"] = f"day-block({out['days']}d)"
        else:
            boot = stats.bootstrap_mean(excess)
            out["p_basis"] = "trade-iid"
        out.update({
            "mean_excess": round(float(excess.mean()), 6),
            # Stored so a promoted strategy can carry its own decay baseline:
            # without a sigma, no sequential test below has a yardstick.
            "std": (round(float(excess.std(ddof=1)), 6) if len(excess) > 1 else 0.0),
            "p_value": boot["p_value"],
            "ci_low": boot["ci_low"],
            "ci_high": boot["ci_high"],
        })
        # `bootstrap_mean` / `block_bootstrap_mean` 在少于 10 个观测时返回
        # ci=[0,0] 与 p=1.0 这些**哨兵值**。它们是"算不出来"，不是"算出来是 0"：
        # 零宽区间会被读成确定，p=1.000 会被读成"完全没有效应"。
        # `arena.py:228` 早就不打印这种区间（"a zero-width interval reads as
        # certainty"），裁判这边一直没跟上——而它正是把 5 笔样本判成 rejected 的
        # 另一半原因。
        if len(excess) < 10:
            out["p_value"] = None
            out["ci_low"] = None
            out["ci_high"] = None
            out["interval_note"] = (f"只有 {len(excess)} 笔，区间与 p 值算不出来；"
                                    "均值是一个观测，不是结论")
    return out


def _run_one(panel: pd.DataFrame, pools: pd.DataFrame, spec: EventStudy,
             market: pd.DataFrame, equity: float = 5000.0) -> dict:
    trades = build_trades(panel, pools, spec, market, equity=equity)
    if trades.empty:
        return {"spec": spec.name(), "empty": True, "max_per_day": spec.max_per_day,
                "metrics": stats.metrics(pd.Series(dtype=float)),
                "trades": _trade_stats(pd.DataFrame()), "cost": {}}
    daily = portfolio_series(trades.attrs.get("daily_rows"),
                             calendar=trades.attrs.get("calendar"))
    return {
        "spec": spec.name(),
        "empty": False,
        "max_per_day": spec.max_per_day,
        "metrics": stats.metrics(daily),
        "daily": daily,
        "trades": _trade_stats(trades),
        "trade_frame": trades,
        "cost": trades.attrs.get("cost") or {},
    }


def capacity(trades: pd.DataFrame, equity: float, lot: int = 100) -> dict:
    """Can a small account actually hold this strategy?

    A daily equal-weight book of 25 names is a fine research object and an
    impossible portfolio at CNY 5,000: one lot of a CNY 11 stock is CNY 1,100.
    """
    input_count = 0 if trades is None else len(trades)
    empty = {"implementable": False, "median_signals_per_day": 0, "median_price": 0.0,
             "capital_one_lot_each": 0.0, "max_names_affordable": 0, "account_equity": equity,
             "input_trades": input_count, "valid_trades": 0,
             "limit_data_valid": None, "limit_data_coverage": None}
    if trades is None or trades.empty:
        return empty
    price = pd.to_numeric(trades["entry_price"], errors="coerce")
    valid = price.notna() & np.isfinite(price) & price.gt(0) & trades["entry_date"].notna()
    frame = trades.loc[valid].copy()
    frame["entry_price"] = price[valid].astype(float)
    limit_valid = (int(frame["limit_data"].fillna(False).astype(bool).sum())
                   if "limit_data" in frame else None)
    if frame.empty:
        return {**empty, "limit_data_valid": limit_valid}
    per_day = frame.groupby("entry_date").size()
    median_signals = float(per_day.median())
    median_price = float(frame["entry_price"].median())
    required = median_signals * lot * median_price
    affordable = int(equity // (lot * median_price)) if median_price > 0 else 0
    return {
        "median_signals_per_day": round(median_signals, 1),
        "median_price": round(median_price, 2),
        "capital_one_lot_each": round(required, 0),
        "max_names_affordable": affordable,
        "account_equity": equity,
        "implementable": required <= equity,
        "input_trades": input_count,
        "valid_trades": len(frame),
        "limit_data_valid": limit_valid,
        "limit_data_coverage": (round(limit_valid / len(frame), 4)
                                if limit_valid is not None else None),
    }


def _non_overlapping_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """One active label per symbol; unresolved marks block later overlapping events."""
    if trades is None or trades.empty:
        return pd.DataFrame() if trades is None else trades.iloc[0:0].copy()
    frame = trades.copy()
    frame["_order"] = (pd.to_numeric(frame["trade_id"], errors="coerce")
                       if "trade_id" in frame else pd.Series(np.arange(len(frame)), index=frame.index))
    frame = frame.sort_values("_order", kind="mergesort")
    kept: list[object] = []
    last_exit: dict[str, str] = {}
    for idx, row in frame.iterrows():
        symbol = str(row["ts_code"])
        entry = str(row["entry_date"])
        if entry <= last_exit.get(symbol, ""):
            continue
        kept.append(idx)
        exit_date = str(row.get("exit_date") or "")
        mark_date = str(row.get("mark_date") or "")
        last_exit[symbol] = exit_date or mark_date or "99999999"
    return frame.loc[kept].drop(columns="_order").sort_index()


def _label_spans(results: list[dict], index: pd.Index) -> np.ndarray:
    """Exclusive label ends for the matrix, conservatively covering all variants."""
    days = [str(d) for d in index]
    position = {day: i for i, day in enumerate(days)}
    spans = np.arange(len(days), dtype=int) + 1
    for result in results:
        trades = result.get("trade_frame")
        rows = (trades.attrs or {}).get("daily_rows") if trades is not None else None
        if rows is None or rows.empty:
            continue
        final = rows.groupby("trade_id")["date"].max().astype(str)
        dated = rows[["trade_id", "date"]].copy()
        dated["date"] = dated["date"].astype(str)
        dated["end"] = dated["trade_id"].map(final)
        for date, end in dated.groupby("date")["end"].max().items():
            if date in position and end in position:
                i = position[date]
                spans[i] = max(spans[i], position[end] + 1)
    return spans


# 数值在 configs/params.yaml 的 research.referee.* 下。判决文案一律从这里格式化，
# 不再各写一遍字面量：此前 "p > 0.10" 是手写的，而闸门读 REFEREE_POLICY["p_max"]，
# 改阈值后文案会继续说 0.10，读者按文案核对就会得出相反结论。
REFEREE_POLICY = {
    # 2026-11-12 收紧了 p / PBO / DSR / 最小样本，阈值必须换版本号：
    # 每条历史判决自带 policy 快照，v1 与 v2 混用而不标版本会让复核的人
    # 以为同一套规则给出了不同的词。收紧是单向的，见
    # tests/research/test_policy_tightening.py 的单调性验证。
    "name": "desk/v2",
    "min_trades": MIN_TRADES,
    "p_max": settings.num("research.referee.p_max"),
    "pbo_max": settings.num("research.referee.pbo_max"),
    "pbo_promote": settings.num("research.referee.pbo_promote"),
    "dsr_min": settings.num("research.referee.dsr_min"),
    "dsr_promote": settings.num("research.referee.dsr_promote"),
    "cost_stress_mult": STRESS_MULTIPLIERS[0],
    "cpcv_p05_min": settings.num("research.referee.cpcv_p05_min"),
    "cpcv_positive_min": settings.num("research.referee.cpcv_positive_min"),
    "account_min_trades": MIN_ACCOUNT_TRADES,
    "account_total_min": settings.num("research.referee.account_total_min"),
    "note": ("预注册决策规则：阈值事先定好、不可逐条协商。"
             "每个判决都带着它看到的值，便于重读，而不是重谈。"),
}


def _gate(gates: list[dict], name: str, value, threshold, op: str,
          level: str | None, detail: str = "") -> bool:
    """Record one threshold together with what it actually saw.

    A bare "rejected" tells a reader nothing about which number failed, so they
    cannot check it and a model cannot disagree with it. The value, the operator
    and the threshold turn a verdict into a statement that can be argued with —
    which is the only kind worth keeping.

    ``value=None`` means the number could not be computed (sample too small, a
    degenerate bootstrap). That is **not** a failed check: it is recorded with
    ``passed=None`` and ``not_computable`` so nothing downstream can read
    "we could not measure it" as "we measured it and it was bad".
    """
    if value is None:
        gates.append({"gate": name, "value": None, "op": op,
                      "threshold": threshold, "passed": None,
                      "fails_as": level, "not_computable": True,
                      "detail": detail})
        return False
    if op == ">=":
        passed = value >= threshold
    elif op == "<=":
        passed = value <= threshold
    elif op == ">":
        passed = value > threshold
    elif op == "<":
        passed = value < threshold
    elif op == "==":
        passed = value == threshold
    else:
        raise ValueError(f"unknown comparison {op}")
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    gates.append({
        "gate": name,
        "value": round(float(value), 6) if numeric else value,
        "op": op,
        "threshold": threshold,
        "passed": bool(passed),
        "fails_as": None if passed else level,
        "detail": detail,
    })
    return bool(passed)


def _verdict(best: dict, dsr: dict, pbo: dict, cpcv: dict,
             stress: dict[str, dict], cap: dict,
             power: dict | None = None,
             account: dict | None = None) -> tuple[str, list[str], dict]:
    """Collect every failure, then let the worst one decide the verdict.

    Semantics matter: "rejected" means the evidence says there is no edge;
    "testing" means the evidence is not strong enough (or not harvestable at
    this account size), which is a different message to the trader.

    The third return value is the audit trail: every gate, the value it saw, the
    threshold, and whether it passed. It is what lets a reader tell "the numbers
    say X" from "this desk, under these rules, records X".
    """
    reasons: list[str] = []
    gates: list[dict] = []
    rejected = False
    testing = False

    n = best["trades"]["n"]
    excess = best["trades"]["mean_excess"]
    p_value = best["trades"]["p_value"]
    underpowered = bool(power and power.get("underpowered"))

    sample_ok = _gate(gates, "sample_trades", n, MIN_TRADES, ">=", "testing")
    if not sample_ok:
        testing = True
        reasons.append(f"sample too small: {n} < {MIN_TRADES} trades")

    # 样本没过门槛时，凡是用交易收益算出来的门只记录数值，不判生死。
    #
    # 从前这里是 `rejected` 优先于 `testing`：5 笔全亏 → `mean_excess <= 0` 成立
    # → 判 `rejected`，而 reasons 里那句 "sample too small: 5 < 100" 排在后面没人读。
    # 结果是"没看清"被写成"看清了是坏的"，而且写进了 hypotheses.status 冻结下来。
    # 一个在 5 笔样本上会开火的决策规则，不是决策规则，是噪声读数。
    if not _gate(gates, "mean_excess", excess, 0.0, ">", "rejected",
                 "vs equal-weight market, per trade"):
        if sample_ok:
            rejected = True
        reasons.append(f"mean excess {excess:.4%}/trade <= 0 vs equal-weight market")
    elif p_value is None:
        # 区间与 p 值算不出来（样本太少或退化）。这是"没测"，不是"测了不显著"。
        _gate(gates, "p_value", None, REFEREE_POLICY["p_max"], "<=", "testing",
              best["trades"].get("interval_note") or "p value not computable")
        testing = True
        reasons.append("p value not computable at this sample size")
    elif not _gate(gates, "p_value", p_value, REFEREE_POLICY["p_max"], "<=",
                   "underpowered" if underpowered else "rejected",
                   "block bootstrap, one-sided"):
        # A positive point estimate that misses significance means one of two
        # things, and they are not the same message: the design could have seen
        # the bar and did not, or the design never could have seen it.
        if underpowered:
            testing = True
            reasons.append(
                f"underpowered: p={p_value:.3f}, and {power['days']} days cannot "
                f"detect {power['mde_bps']:.0f}bps/trade at 80% power while "
                f"{power['floor_bps']:.0f}bps is the bar; needs "
                f"{power['required_days']} days / {power['required_trades']} trades"
            )
        else:
            rejected = True
            reasons.append(f"bootstrap p={p_value:.3f} > "
                           f"{REFEREE_POLICY['p_max']:.2f}: excess not distinguishable from 0")

    pbo_value = pbo.get("pbo")
    pbo_valid = (pbo.get("ok", True) is not False and pbo_value is not None
                 and np.isfinite(pbo_value))
    if not pbo_valid:
        _gate(gates, "pbo_valid", False, True, "==", "testing",
              pbo.get("note") or "CSCV returned no valid PBO")
        testing = True
        reasons.append(f"PBO unavailable: {pbo.get('note') or 'no valid CSCV result'}")
    elif not _gate(gates, "pbo", pbo_value, REFEREE_POLICY["pbo_max"], "<=",
                   "rejected", "CSCV: share of splits where the pick underperforms"):
        if sample_ok:
            rejected = True
        reasons.append(f"PBO {pbo_value:.2f} > "
                       f"{REFEREE_POLICY['pbo_max']:.2f}: selection is likely noise")

    sharpe_2x = stress[PRIMARY_STRESS]["metrics"]["sharpe"]
    if not _gate(gates, "cost_stress_sharpe", sharpe_2x, 0.0, ">", "rejected",
                 "Sharpe with costs doubled"):
        if sample_ok:
            rejected = True
        reasons.append(f"2x costs kill it: Sharpe {sharpe_2x:.2f}")

    if not _gate(gates, "deflated_sharpe", dsr.get("dsr", 0),
                 REFEREE_POLICY["dsr_min"], ">=", "testing",
                 f"after {dsr.get('trials')} trials"):
        testing = True
        reasons.append(
            f"deflated Sharpe {dsr.get('dsr', 0):.3f} < {REFEREE_POLICY['dsr_min']:.2f} "
            f"after {dsr.get('trials')} trials"
        )

    if not _gate(gates, "capacity", bool(cap["implementable"]), True, "==", "testing",
                 (f"needs CNY {cap.get('capital_one_lot_each', 0):,.0f} vs equity "
                  f"CNY {cap.get('account_equity', 0):,.0f}")):
        testing = True
        reasons.append(
            f"capacity: {cap['median_signals_per_day']:.0f} signals/day x "
            f"{cap['median_price']:.2f} needs CNY {cap['capital_one_lot_each']:,.0f}; "
            f"this account can hold {cap['max_names_affordable']}"
        )

    # The account gate. The signal book above may hold thirty names and never
    # run out of cash; this curve cannot. If a strategy survives every
    # statistical test but loses money once lots, slots and settlement are real,
    # it is not a strategy for this desk.
    if account:
        taken = int(account.get("trades_taken") or 0)
        acct_metrics = account.get("metrics") or {}
        total = float(acct_metrics.get("total_return") or 0.0)
        if taken >= MIN_ACCOUNT_TRADES:
            _gate(gates, "account_fillable_trades", taken, MIN_ACCOUNT_TRADES, ">=", None,
                  "trades the CNY account could actually buy")
            detail = ""
            mean_gross = account.get("mean_gross") or {}
            if int(account.get("trades_skipped") or 0) > 0 and mean_gross:
                detail = (f"mean gross taken {float(mean_gross.get('taken') or 0):.2%} "
                          f"vs skipped {float(mean_gross.get('skipped') or 0):.2%}")
            if not _gate(gates, "account_total_return", total, 0.0, ">", "rejected",
                         detail or "realised cash equity"):
                rejected = True
                suffix = f"; {detail}" if detail else ""
                reasons.append(
                    f"account curve loses money: {taken} fillable trades, "
                    f"total {total:.2%} on CNY {account.get('equity_start', 0):,.0f} "
                    f"with {account.get('max_positions')} slots{suffix}"
                )
        elif taken > 0:
            _gate(gates, "account_fillable_trades", taken, MIN_ACCOUNT_TRADES, ">=",
                  "testing", "trades the CNY account could actually buy")
            testing = True
            reasons.append(
                f"only {taken} trades fit the account (< {MIN_ACCOUNT_TRADES}): "
                f"the edge may exist but this book cannot express it"
            )

    promo_gates: list[dict] = []
    _gate(promo_gates, "dsr", dsr.get("dsr", 0), REFEREE_POLICY["dsr_promote"], ">=", "no")
    _gate(promo_gates, "pbo", pbo_value if pbo_valid else 1.0,
          REFEREE_POLICY["pbo_promote"], "<=", "no")
    _gate(promo_gates, "cpcv_p05", cpcv.get("oos_sharpe_p05", -1),
          REFEREE_POLICY["cpcv_p05_min"], ">", "no")
    _gate(promo_gates, "cpcv_positive", cpcv.get("positive", 0),
          REFEREE_POLICY["cpcv_positive_min"], ">=", "no")
    _gate(promo_gates, "capacity", bool(cap["implementable"]), True, "==", "no")
    if account:
        _gate(promo_gates, "account_fillable_trades",
              int(account.get("trades_taken") or 0), MIN_ACCOUNT_TRADES, ">=", "no")
        _gate(promo_gates, "account_total_return",
              float((account.get("metrics") or {}).get("total_return") or 0.0), 0.0, ">", "no")
    promotion = {"gates": promo_gates,
                 "all_passed": all(g["passed"] for g in promo_gates)}

    # 样本没过门槛时，用收益算出来的那几道门记的是"没测"，不是"没过"。
    # 只抑制 rejected 还不够：`passed: False` 留在审计轨里，下一个读的人（或模型）
    # 照样会看到"mean_excess 没过"，然后自己得出 rejected 的结论。
    #
    # 而且 `elif` 会短路：mean_excess 失败时 p_value 那道门根本没被记录。
    # 缺一条门和"过了"看起来一样，所以未跑的门也要显式补一条 passed=null。
    if not sample_ok:
        why = f"sample {n} < {MIN_TRADES}"
        for gate in gates:
            # 连"通过"也要抹掉：5 笔样本既不能否掉一个策略，也不能确认它。
            # 留着 passed: True 会让下一个读者以为这里测过并且过了。
            if gate["gate"] in ("mean_excess", "p_value", "pbo", "cost_stress_sharpe") \
                    and gate["passed"] is not None:
                gate["passed"] = None
                gate["fails_as"] = "testing"
                gate["not_judged"] = f"{why}: 这个数算得出来，但不该拿来定生死"
        present = {gate["gate"] for gate in gates}
        for name in ("mean_excess", "p_value", "pbo", "cost_stress_sharpe"):
            if name not in present:
                gates.append({"gate": name, "value": None, "op": None,
                              "threshold": None, "passed": None,
                              "fails_as": "testing",
                              "not_judged": f"{why}: 前面的门已短路，这道没跑"})

    record = {"policy": dict(REFEREE_POLICY), "gates": gates,
              "promotion": promotion,
              "verdict_from": ("worst failing gate decides; rejected > testing > "
                               "underpowered > promoted. 样本未过门槛时，"
                               "收益类门记 passed=null（没测），不参与合成")}

    if rejected:
        return "rejected", reasons, record
    if (not testing) and promotion["all_passed"]:
        reasons.append(
            f"excess {excess:.2%}/trade (p={p_value:.3f}), DSR {dsr['dsr']:.2f}, "
            f"PBO {pbo['pbo']:.2f}, CPCV p05 {cpcv['oos_sharpe_p05']:.2f}, "
            f"{PRIMARY_STRESS}-cost Sharpe {stress[PRIMARY_STRESS]['metrics']['sharpe']:.2f}"
        )
        return "promoted", reasons, record
    # "Not enough evidence" and "this sample can never produce enough evidence"
    # are different instructions to the researcher: one says wait, the other says
    # change the design or the holding period.
    return ("underpowered" if underpowered else "testing"), reasons, record


def shadow_gate(best: dict, dsr: dict, pbo: dict, cpcv: dict,
                stress: dict[str, dict], cap: dict,
                power: dict | None = None,
                account: dict | None = None) -> tuple[bool, list[str]]:
    """Should this advance to a paper shadow book?

    A backtest can rarely clear a deflated Sharpe gate after a real grid, and it
    should not: the point of the shadow book is to be the arbiter. So promotion
    to *shadow* asks only for a positive, cost-robust, implementable edge with
    low selection risk. Promotion to *capital* still requires the shadow record.

    Two thresholds live in config, not in this body. They used to be the literals
    ``0.05`` and ``0.30`` — the same failure this file already calls out at
    ``REFEREE_POLICY``: a gate that reads a literal keeps reporting last year's
    number after the yaml changes, and a 0.30 PBO ceiling sitting beside a
    ``pbo_promote`` of 0.30 is one fact with two homes.
    """
    fails: list[str] = []
    n = best["trades"]["n"]
    p_value = best["trades"]["p_value"]
    if n < MIN_TRADES:
        fails.append("sample")
    if best["trades"]["mean_excess"] <= 0:
        fails.append("excess<=0")
    # p 值可能是 None：样本太少或区间退化时它是"算不出来"，不是"很小/很大"。
    # 拿 None 去比阈值会崩，而把它当 1.0 又会说成"不显著"——两样都是错。
    if p_value is None:
        fails.append("p-unavailable")
    elif p_value > SHADOW_P_MAX:
        fails.append(f"p>{SHADOW_P_MAX:g}")
    pbo_value = pbo.get("pbo")
    if pbo.get("ok", True) is False or pbo_value is None or not np.isfinite(pbo_value):
        fails.append("pbo-unavailable")
    elif pbo_value > SHADOW_PBO_MAX:
        fails.append(f"pbo>{SHADOW_PBO_MAX:g}")
    if cpcv.get("oos_sharpe_p05", -1) <= 0:
        fails.append("cpcv")
    if stress[PRIMARY_STRESS]["metrics"]["sharpe"] <= 0:
        fails.append("2x-cost")
    if not cap["implementable"]:
        fails.append("capacity")
    if power and power.get("underpowered"):
        fails.append("underpowered")
    if account:
        taken = int(account.get("trades_taken") or 0)
        if taken < MIN_ACCOUNT_TRADES:
            fails.append("account-too-small")
        elif float((account.get("metrics") or {}).get("total_return") or 0) <= 0:
            fails.append("account-loses")
    return (not fails), fails


def _run_impl(store, spec_text: str | dict | EventStudy, *, keep_frames: bool = False,
              registry: TrialRegistry | None = None, source: str = "run",
              attach_attribution: bool = False, record: bool = True,
              include_t_close: bool = False, read_tracker=None) -> dict:
    """Run a full research pass and return the verdict payload.

    ``registry`` is optional and deliberately explicit: tests and the synthetic
    controls must not write into the book of record. When it is supplied, the
    Deflated Sharpe ratio is deflated by the **program** trial count, not by this
    run's grid, and the payload carries ``program_fdr`` / ``p_adjusted``.

    ``record`` separates *reading* the denominator from *writing* a row. They used
    to be the same switch, which made ``--no-record`` a way to skip the family
    count and deflate by this run's grid alone — a better-looking Sharpe for not
    telling anyone you looked.

    ``attach_attribution`` adds a characteristic-matched benchmark (beta, size,
    reversal, liquidity) and refuses promotion when the residual alpha is not
    positive with t >= 2. It costs a full-panel exposure build, so the CLI
    default is ON (2026-11-12) while the library default stays OFF for cheap
    synthetic tests. Every real promotion path must turn it on;
    ``payload.attribution.attached`` says whether it actually did.
    """
    base = parse_spec(spec_text)
    panel, pools, market = load_inputs(store, base)

    variants = _grid(base, include_t_close=include_t_close)
    equity = _equity()
    results: list[dict] = []
    for spec in variants:
        res = _run_one(panel, pools, spec, market, equity=equity)
        if not res["empty"]:
            results.append(res)
    if not results:
        raise SpecError("no variant produced trades; check the pool and date range")

    matrix = pd.DataFrame({r["spec"]: r["daily"] for r in results}).fillna(0.0).sort_index()
    trial_sharpes = [stats.sharpe_per_period(matrix[c]) for c in matrix.columns]
    best = max(results, key=lambda r: r["metrics"]["sharpe"])

    # The denominator: every search in the family, not just this grid. Nine
    # hypotheses on one panel are nine looks at the same data even when each
    # one only varied eight parameters inside its own run, and 9 x 8 searches
    # deflated as 8 is how a desk talks itself into a false positive.
    #
    # `n_variants` is the **search space**, not the count of variants that
    # happened to produce trades. A variant with zero trades was still looked
    # at: it consumed a degree of freedom and it is one of the draws that made
    # the surviving Sharpe look as good as it does. Counting only the survivors
    # (`len(results)`, which `_run_impl` has already filtered) deflated an
    # 8-variant grid as 5 whenever 3 variants were empty, and the error path at
    # `run()` below has always used the full grid — so the two paths disagreed
    # inside one family. Measured on the ledger: this is the difference between
    # a denominator that grows with effort and one that shrinks with failure.
    n_variants = len(variants)
    n_nonempty = len(results)
    family_variants = registry.variants_total(DEFAULT_FAMILY) if registry is not None else 0
    dsr_trials = family_variants + n_variants

    dsr = stats.deflated_sharpe(matrix[best["spec"]], dsr_trials, trial_sharpes)
    dsr["trials_local"] = n_variants
    dsr["variants_nonempty"] = n_nonempty
    pbo = stats.cscv_pbo(matrix, blocks=8)
    # The label of a hold=h trade spans h+1 sessions (entry at T+1, exit at
    # T+1+h), so embargoing h rows still leaves the last training label touching
    # the test block. One extra session closes it.
    cpcv = stats.select_and_test(matrix, groups=6, k_test=2, embargo=int(base.hold) + 1,
                                 label_spans=_label_spans(results, matrix.index))

    stress: dict[str, dict] = {}
    best_spec = parse_best(best, base)
    for mult in STRESS_MULTIPLIERS:
        stressed = _run_one(panel, pools, best_spec.with_cost_mult(mult), market, equity=equity)
        stressed_trades = stressed.get("trade_frame")
        stressed_account = capital.simulate(
            stressed_trades, best_spec.with_cost_mult(mult), equity=equity,
            max_positions=ACCOUNT_MAX_POSITIONS,
            calendar=(stressed_trades.attrs.get("calendar") if stressed_trades is not None else None),
        ).as_dict()
        stress[f"{mult:g}x"] = {
            # Cost robustness is an account-cash question, not a set of
            # overlapping event labels. Keep the signal metrics only as diagnostics.
            "metrics": stressed_account["metrics"],
            "signal_metrics": stressed["metrics"],
            "account": stressed_account,
            "trades": stressed["trades"],
            "cost": stressed.get("cost") or {},
        }

    cap = capacity(best.get("trade_frame"), equity, lot=best_spec.lot)

    # The account curve: same trades, but only the ones a CNY 5,000 book with
    # three slots and whole lots could actually have taken. This is what the
    # desk would have earned, as opposed to what the signal average implies.
    account_calendar = (best.get("trade_frame").attrs.get("calendar")
                        if best.get("trade_frame") is not None else None)
    account_sim = capital.simulate(
        best.get("trade_frame"), best_spec, equity=equity,
        max_positions=ACCOUNT_MAX_POSITIONS, calendar=account_calendar,
    )
    account = account_sim.as_dict()

    # Power is measured against the edge that would justify trading — 2x cost,
    # the same bar the stress test enforces. A design that cannot see that far
    # is not "waiting for more data"; it is the wrong design.
    power = assess_power(_non_overlapping_trades(best.get("trade_frame")),
                         cost_bps=float((best.get("cost") or {}).get("total_bps") or 0.0))

    verdict, reasons, policy_record = _verdict(
        best, dsr, pbo, cpcv, stress, cap, power=power, account=account)
    advance, blockers = shadow_gate(best, dsr, pbo, cpcv, stress, cap, power=power,
                                    account=account)

    attribution_summary = None
    if attach_attribution and best.get("trade_frame") is not None:
        try:
            basics = store.read("daily_basic", base.start, base.end)
        except (KeyError, ValueError):
            basics = pd.DataFrame()
        # The benchmark must use the *best variant's* entry/exit, not the base
        # spec's: a hold=0 base with a hold=1 winner would compare a two-day
        # trade against a same-day peer return.
        attribution_summary = attribute(best["trade_frame"], panel, best_spec, basics)
        blocked, why = blocks_promotion(attribution_summary)
        if blocked:
            advance = False
            blockers = [*blockers, f"attribution: {why}"]
            if verdict == "promoted":
                verdict = "testing"
                reasons = [*reasons, f"attribution: {why}"]

    program = {"recorded": False, "dsr_trials": dsr_trials,
               "family_variants": family_variants, "local_variants": n_variants}
    datasets = (read_tracker.datasets if read_tracker is not None
                else fingerprint.DATASETS)
    provenance = fingerprint.reproducibility(store, base.start, base.end, datasets)
    if registry is not None and record:
        program = registry.record(
            base, n_variants=n_variants, n_trades=best["trades"]["n"],
            p_value=best["trades"]["p_value"], effect=best["trades"]["mean_excess"],
            verdict=verdict, source=source,
            data_fingerprint=provenance["data_fingerprint"],
            code_fingerprint=provenance["code_fingerprint"],
            config_fingerprint=provenance["config_fingerprint"],
            data_inputs=provenance["data_inputs"])
        program["dsr_trials"] = dsr_trials
        program["family_variants"] = family_variants

    payload = {
        # 机械读数，不是判决。它由预注册阈值算出（见 policy），换一套阈值会得到
        # 另一个词，同一个数不变——所以它说的是"这套规则怎么读"，不是"市场怎样"。
        # 判断留给读的人：spec 写明测的什么，evidence 给出每个数，读者可以不同意。
        "gate_reading": verdict,
        "decided_by": (f"policy {REFEREE_POLICY['name']} 的预注册阈值（机械读数）。"
                       "是否据此下注是读的人的决定——预注册防的是事后调参，"
                       "它不替任何人判断这个 setup 值不值得做"),
        "shadow_candidate": advance,
        "shadow_blockers": blockers,
        "reasons": reasons,
        # The decision rule and what it saw. A verdict without this is a
        # conclusion nobody can check: `rejected` alone does not say which
        # number failed, so a reader cannot agree or disagree with it.
        "policy": policy_record,
        "provenance": provenance,
        "spec": {
            # Every search dimension, not a summary. The P8 verdict files only
            # carried pool/boards/entry/hold/rank_by, so replaying one silently
            # tested a different hypothesis: `link_pool` and `rank_dir` fell back
            # to their defaults. A verdict that cannot be reproduced is not a
            # record of an experiment.
            "pool": base.pool, "boards": list(base.boards), "entry": base.entry,
            "hold": base.hold, "exit_at": base.exit_at,
            "gap_threshold": base.gap_threshold, "link_pool": base.link_pool,
            "max_per_day": base.max_per_day, "rank_by": base.rank_by,
            "rank_dir": base.rank_dir, "min_amount": base.min_amount,
            "exclude_st": base.exclude_st, "exclude_bj": base.exclude_bj,
            "lot": base.lot, "start": base.start, "end": base.end,
            "cost_bps": base.cost_bps, "cost_mult": base.cost_mult,
            "ticket_cny": base.ticket_cny,
        },
        "cost": best.get("cost") or {},
        "cost_bps_effective": (best.get("cost") or {}).get("total_bps"),
        "power": power,
        "capacity": cap,
        "account": account,
        # How many exits could not clear on the planned session. A rolled exit
        # means the position was still held while the price kept falling; a
        # "stuck" one has no executable exit in the loaded forward window, so
        # only its separate mark is valued and no realised return is recorded.
        "sell_gate": ((best.get("trade_frame").attrs or {}).get("sell_gate") or {})
        if best.get("trade_frame") is not None else {},
        "metric_basis": {
            "price": ((best.get("trade_frame").attrs or {}).get("price_basis")
                      if best.get("trade_frame") is not None else "unknown"),
            "execution": ((best.get("trade_frame").attrs or {}).get("execution_basis")
                          if best.get("trade_frame") is not None else "unknown"),
            "signal_curve": ("daily equal-weight across open legs; flat sessions "
                             "included as 0 return"),
            "signal_days": best["metrics"]["n"],
            "calendar_days": len(best["daily"]),
            "exposure": best["metrics"].get("exposure"),
            "account_curve": (f"cash plus cost-carried positions (explicit marks only "
                              f"when unresolved), CNY {equity:,.0f}, "
                              f"{ACCOUNT_MAX_POSITIONS} slots at "
                              f"{1 / ACCOUNT_MAX_POSITIONS:.0%} of equity per name, whole lots"),
            "p_value": best["trades"].get("p_basis"),
        },
        "best": {
            "spec": best["spec"],
            "metrics": best["metrics"],
            "trades": best["trades"],
        },
        "deflated_sharpe": dsr,
        "pbo": pbo,
        "cpcv": cpcv,
        "stress": stress,
        "trials": n_variants,
        "trials_nonempty": n_nonempty,
        "trials_effective": dsr_trials,
        "program": program or {"recorded": False},
        "grid_scope": {
            "entries": sorted({str(v.entry) for v in variants}),
            "includes_t_close": bool(include_t_close),
            "note": ("默认族不含 t_close；显式 include_t_close=True 会扩大搜索族并改变 "
                     "n_variants / DSR 分母，因此这次判决与默认族在案判决不可直接比较。"
                     if include_t_close else
                     "默认族：base.entry / next_open / next_close。t_close 只能作为独立"
                     "假设测量，不作为本族的竞争变体（见 _grid docstring）。"),
        },
        "attribution": attribution_summary or {"attached": False},
        "panel": {"trades": int(sum(r["trades"]["n"] for r in results))},
    }
    if keep_frames:
        payload["frames"] = {"matrix": matrix, "best_trades": best.get("trade_frame")}
    return payload


def run(store, spec_text: str | dict | EventStudy, *, keep_frames: bool = False,
        registry: TrialRegistry | None = None, source: str = "run",
        attach_attribution: bool = False, record: bool = True,
        include_t_close: bool = False) -> dict:
    """Validate and run a study; failed and empty looks still enter the registry."""
    base = parse_spec(spec_text)
    tracked = fingerprint.ReadTracker(store)
    try:
        return _run_impl(tracked, base, keep_frames=keep_frames, registry=registry,
                         source=source, attach_attribution=attach_attribution,
                         record=record, include_t_close=include_t_close,
                         read_tracker=tracked)
    except Exception as exc:
        if registry is not None and record:
            try:
                n_variants = max(1, len(_grid(base, include_t_close=include_t_close)))
            except SpecError:
                n_variants = 1
            provenance = fingerprint.reproducibility(
                tracked, base.start, base.end, tracked.datasets)
            registry.record(base, n_variants=n_variants, n_trades=0,
                            p_value=None, effect=None,
                            verdict=f"error:{type(exc).__name__}:{exc}",
                            source=f"{source}:error",
                            data_fingerprint=provenance["data_fingerprint"],
                            code_fingerprint=provenance["code_fingerprint"],
                            config_fingerprint=provenance["config_fingerprint"],
                            data_inputs=provenance["data_inputs"])
        raise


def parse_best(best: dict, base: EventStudy) -> EventStudy:
    """Recover the best variant's parameters from its name.

    ``EventStudy.name()`` appends ``@d<percent>`` to a ``next_limit`` entry so
    two discounts cannot overwrite each other in the trial registry. The old
    reader fed that whole token back as ``entry``, producing the nonsensical
    "entry must be one of ['next_close', 'next_limit', ...]" for a value that
    was literally ``next_limit@d3``. Strip the suffix and restore the discount
    so name()/parse_best() stay inverse operations.
    """
    parts = best["spec"].split(":")
    boards = tuple(int(b) for b in parts[1].lstrip("b").split("+"))
    hold_token = parts[3]
    hold = int(hold_token[1:-1])
    exit_at = "open" if hold_token.endswith("x") else "close"
    entry_token = parts[2]
    drop = base.limit_drop_pct
    if "@d" in entry_token:
        entry_token, _, suffix = entry_token.partition("@d")
        try:
            drop = float(suffix) / 100.0
        except ValueError:
            drop = base.limit_drop_pct
    return base.with_variant(boards=boards, entry=entry_token, hold=hold,
                             exit_at=exit_at, limit_drop_pct=drop)
