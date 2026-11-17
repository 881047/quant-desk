"""Two arms and a difference. Not a verdict.

An overlay claim — "a 5% stop would be better" — is a statement about a rule
applied to trades that already happened. Answering it needs no panel, no new
backtest and no referee: it needs the same trades, run twice.

What this module does **not** do is decide whether the difference is good. It
returns the two distributions, the paired difference, its interval, and how many
trades the rule actually touched. Reading it is the caller's job.

Three measurement choices worth stating, because each one is where this kind of
analysis usually goes wrong:

**The trigger is the close, not the intraday low.** Daily bars cannot say whether
the low came before or after the high, so a stop that fires mid-session cannot be
priced honestly. A close-based stop fills at that session's close, which may be
worse than the threshold — never better. That direction of error is the one to
accept when the alternative is inventing an intrabar path. It also means the
result is a *lower bound* on how much a real stop costs when the stock gaps
through the level.

**Cost does not shrink when the trade does.** Both legs still happen, so the
round-trip cost is unchanged; `net = gross - cost` as everywhere else in the desk.

**The paired difference is reported twice.** Over all trades, and over only the
ones the rule changed. A stop that fires on 3 of 2,000 trades has a portfolio
effect near zero and a large effect on those 3, and reporting only the first
number lets a real rule look inert — while reporting only the second lets an
irrelevant rule look decisive.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import stats

# Rules that change an exit and therefore keep the sample paired. Anything that
# changes *which* trades exist is a different primitive (``compare_filter``).
EXIT_RULES = ("stop_loss", "take_profit", "trailing_stop", "time_exit")

DEFAULT_PARAMS = {
    "stop_loss": {"threshold": 0.05},
    "take_profit": {"threshold": 0.10},
    "trailing_stop": {"threshold": 0.08},
    "time_exit": {"days": 3},
}

TRIGGER_BASIS = "close"
PAIR_TOL = 1e-9


def _require(trades: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    rows = (trades.attrs or {}).get("daily_rows")
    if rows is None or len(rows) == 0:
        raise ValueError(
            "trades 没有 attrs['daily_rows']：无法重建每日路径，"
            "无法判断止损在哪一天触发。请用 spec.build_trades() 的返回值。")
    need = {"trade_id", "entry_price", "cost", "gross"}
    missing = need - set(trades.columns)
    if missing:
        raise ValueError(f"trades 缺少列: {sorted(missing)}")
    return rows, {"trade_id", "date", "leg", "ret", "kind"}


def path(trades: pd.DataFrame) -> pd.DataFrame:
    """Cumulative **gross** return after each holding day, per trade.

    The entry leg's return already has the cost subtracted (``build_trades``
    does that so the daily series nets out); adding it back reconstructs the
    price move, which is what a stop level acts on. Verifying that the last
    cumulative value equals ``trades.gross`` is the check that this
    reconstruction is right, and it is reported rather than assumed.
    """
    rows, cols = _require(trades)
    missing = cols - set(rows.columns)
    if missing:
        raise ValueError(f"daily_rows 缺少列: {sorted(missing)}")

    cost = trades.set_index("trade_id")["cost"].astype(float)
    frame = rows[["trade_id", "date", "leg", "ret", "kind"]].copy()
    frame["ret"] = frame["ret"].astype(float)
    frame["cost"] = frame["trade_id"].map(cost).astype(float).fillna(0.0)
    entry = (frame["kind"] == "entry").to_numpy()
    frame["factor"] = 1.0 + frame["ret"] + np.where(entry, frame["cost"], 0.0)
    frame = frame.sort_values(["trade_id", "leg"], kind="mergesort")
    frame["gross_cum"] = frame.groupby("trade_id", sort=False)["factor"].cumprod() - 1.0
    return frame


def reconstruct_error(trades: pd.DataFrame, frame: pd.DataFrame) -> float:
    """Max |rebuilt gross - recorded gross|. Must be ~1e-16 or the legs are wrong."""
    last = frame.groupby("trade_id", sort=False)["gross_cum"].last()
    recorded = trades.set_index("trade_id")["gross"].astype(float)
    both = pd.concat([last.rename("rebuilt"), recorded.rename("recorded")], axis=1, join="outer")
    if both.empty:
        return 0.0
    if both.isna().any().any() or not np.isfinite(both.to_numpy(dtype=float)).all():
        return float("inf")
    return float((both["rebuilt"] - both["recorded"]).abs().max())


def _trigger(cum: np.ndarray, rule: str, params: dict) -> int:
    if len(cum) == 0:
        return -1
    if rule == "stop_loss":
        hit = np.flatnonzero(cum <= -float(params["threshold"]))
    elif rule == "take_profit":
        hit = np.flatnonzero(cum >= float(params["threshold"]))
    elif rule == "trailing_stop":
        # The peak starts at 0: a position that never gains behaves exactly like
        # a plain stop, which is the intent of "trailing" in a long-only book.
        peak = np.maximum.accumulate(np.maximum(cum, 0.0))
        hit = np.flatnonzero(cum <= peak - float(params["threshold"]))
    elif rule == "time_exit":
        days = int(params["days"])
        hit = np.array([days]) if 0 < days < len(cum) else np.array([], dtype=int)
    else:
        raise ValueError(f"unknown exit rule: {rule}")
    return int(hit[0]) if len(hit) else -1


def apply_rule(trades: pd.DataFrame, rule: str, **params) -> tuple[pd.DataFrame, dict]:
    """Rebuild the trade table as if ``rule`` had been in force.

    Returns the new table and a per-trade record of what happened. Unchanged
    trades are kept, not dropped: the paired difference is only defined over the
    same sample, and dropping the untriggered ones would silently change the
    question from "does this rule improve the book" to "what do stopped-out
    trades look like".
    """
    if rule not in EXIT_RULES:
        raise ValueError(f"{rule} 不改变出场，不能用配对对比；入场过滤请用 compare_filter")
    p = {**DEFAULT_PARAMS.get(rule, {}), **params}
    frame = path(trades)

    exits: dict[str, tuple[int, str, float]] = {}
    crossed_on_last = 0
    for trade_id, group in frame.groupby("trade_id", sort=False):
        cum = group["gross_cum"].to_numpy(dtype=float)
        k = _trigger(cum, rule, p)
        if k < 0:
            continue
        if k == len(cum) - 1:
            # The level was crossed on the session the trade was leaving anyway.
            # The exit does not move, so the trade is unchanged: counting it as
            # "fired" would inflate the affected count with no-ops and make the
            # rule look like it acted on more of the book than it did.
            crossed_on_last += 1
            continue
        exits[trade_id] = (k, str(group["date"].iloc[k]), float(cum[k]))

    out = trades.copy()
    idx = out["trade_id"]
    hit = idx.isin(exits)
    if hit.any():
        span = frame.groupby("trade_id", sort=False)["leg"].max()
        days = pd.Series({t: int(span[t]) - v[0] for t, v in exits.items()})
        out.loc[hit, "exit_date"] = [exits[t][1] for t in idx[hit]]
        out.loc[hit, "gross"] = [exits[t][2] for t in idx[hit]]
        out.loc[hit, "net"] = out.loc[hit, "gross"] - out.loc[hit, "cost"]
        out.loc[hit, "exit_price"] = (
            out.loc[hit, "entry_price"].astype(float)
            * (1.0 + out.loc[hit, "gross"].astype(float)))
        out["overlay_fired"] = hit.to_numpy()
        out["overlay_days_saved"] = [days.get(t, 0) for t in idx]
    else:
        out["overlay_fired"] = False
        out["overlay_days_saved"] = 0

    detail = {
        "rule": rule,
        "params": p,
        "trigger": TRIGGER_BASIS,
        "n_trades": len(out),
        "n_fired": int(hit.sum()),
        "fired_share": round(float(hit.mean()), 4) if len(out) else 0.0,
        "crossed_on_last_leg": crossed_on_last,
        "reconstruction_error": reconstruct_error(trades, frame),
    }
    if hit.any():
        detail["days_saved"] = {
            "mean": round(float(out.loc[hit, "overlay_days_saved"].mean()), 3),
            "median": float(out.loc[hit, "overlay_days_saved"].median()),
        }
    error = detail["reconstruction_error"]
    if not np.isfinite(error) or error > PAIR_TOL:
        raise ValueError(f"daily_rows 无法重建 trades.gross（reconstruction_error={error}）")
    return out, detail


def _dist(values: pd.Series) -> dict:
    x = pd.to_numeric(values, errors="coerce").dropna().astype(float)
    if x.empty:
        return {"n": 0}
    return {
        "n": len(x),
        "mean": round(float(x.mean()), 6),
        "median": round(float(x.median()), 6),
        "p25": round(float(x.quantile(0.25)), 6),
        "p75": round(float(x.quantile(0.75)), 6),
        "win": round(float((x > 0).mean()), 4),
        "std": round(float(x.std(ddof=1)), 6) if len(x) > 1 else 0.0,
    }


def _paired_boot(frame: pd.DataFrame, dates: pd.Series, column: str) -> dict:
    """Bootstrap the mean difference, resampling days not trades.

    Trades entered on the same session share that session's market move, so
    treating them as independent would shrink the interval and make every rule
    look sharper than it is. Same convention as the referee's trade stats.
    """
    x = pd.to_numeric(frame[column], errors="coerce")
    ok = x.notna() & np.isfinite(x)
    insufficient = {
        "ok": False, "status": "insufficient", "n": int(ok.sum()),
        "mean": round(float(x[ok].mean()), 6) if ok.any() else 0.0,
        "ci_low": None, "ci_high": None, "p_value": 1.0,
        "p_basis": "insufficient-independent-days",
    }
    if not ok.any() or dates is None:
        return insufficient
    by_day = x[ok].groupby(dates[ok].astype(str)).mean()
    insufficient["independent_days"] = len(by_day)
    if len(by_day) < 10:
        return insufficient
    boot = stats.block_bootstrap_mean(by_day, block=5)
    boot.update({"ok": True, "status": "ok", "p_basis": f"day-block({len(by_day)}d)",
                 "n": int(ok.sum()), "independent_days": len(by_day)})
    return boot


def compare(trades: pd.DataFrame, rule: str, **params) -> dict:
    """Run the same trades with and without the rule, and say how they differ."""
    treated, detail = apply_rule(trades, rule, **params)
    base_net = pd.to_numeric(trades["net"], errors="coerce").astype(float)
    new_net = pd.to_numeric(treated["net"], errors="coerce").astype(float)

    paired = pd.DataFrame({
        "trade_id": trades["trade_id"].to_numpy(),
        "entry_date": (trades["entry_date"].astype(str).to_numpy()
                       if "entry_date" in trades.columns else ""),
        "without": base_net.to_numpy(),
        "with": new_net.to_numpy(),
    })
    paired["diff"] = paired["with"] - paired["without"]
    fired = treated["overlay_fired"].to_numpy(dtype=bool)
    affected = paired[fired]

    base_sharpe = _sharpe_like(trades)
    new_sharpe = _sharpe_like(treated)

    return {
        "ok": True,
        "rule": rule,
        "params": detail["params"],
        "trigger": detail["trigger"],
        "n_trades": detail["n_trades"],
        "n_fired": detail["n_fired"],
        "fired_share": detail["fired_share"],
        "crossed_on_last_leg": detail["crossed_on_last_leg"],
        "arms": {
            "without": _dist(paired["without"]),
            "with": _dist(paired["with"]),
        },
        "paired_diff": _paired_boot(paired, paired["entry_date"], "diff"),
        "affected_diff": {
            **_paired_boot(affected, affected["entry_date"], "diff"),
            "median": (round(float(affected["diff"].median()), 6)
                       if len(affected) else 0.0),
            "better_share": (round(float((affected["diff"] > 0).mean()), 4)
                             if len(affected) else 0.0),
            "worst": round(float(affected["diff"].min()), 6) if len(affected) else 0.0,
            "best": round(float(affected["diff"].max()), 6) if len(affected) else 0.0,
        },
        "days_saved": detail.get("days_saved"),
        "signal_sharpe": {
            "without": base_sharpe,
            "with": new_sharpe,
            "note": ("两臂的逐日曲线只有出场日不同，Sharpe 只能当粗略参照；"
                     "配对差才是这条规则的可检验量。"),
        },
        "reconstruction_error": detail["reconstruction_error"],
        "basis": [
            "同一批交易跑两次：只有出场日、出场价、gross、net 不同，样本不变。",
            "触发依据是收盘价，不是盘中最低价：日线无法给出盘中路径。",
            "成本不变：买入与卖出两腿都仍然发生。",
            "paired_diff 覆盖全部交易；affected_diff 只覆盖规则真正改变了的那部分。",
        ],
        "caveats": [
            ("收盘触发会高估单次损失（可能在阈值之外成交），不会低估——这是刻意选的方向。"),
            ("「以该收盘价成交」本身也是一个假设：决策与成交发生在同一时刻。"
             "真实做法是尾盘近似或次日开盘，两者都会比这里更差。"),
            "没有建模「跌停当天卖不掉」：止损触发日若跌停，实际要顺延，见 spec._resolve_exit。",
            ("crossed_on_last_leg 是「阈值被触及但出场日本来就是那天」的笔数，"
             "这些交易的出场没变，未计入 n_fired。"),
            "这不是判决。两臂的分布、配对差与样本都在这里，要不要采用由调用方决定。",
        ],
    }


def _sharpe_like(trades: pd.DataFrame) -> dict:
    """Per-trade Sharpe, not annualised: a rough same-scale comparison only."""
    net = pd.to_numeric(trades["net"], errors="coerce").dropna().astype(float)
    if len(net) < 3 or net.std(ddof=1) == 0:
        return {"n": len(net), "per_trade": 0.0}
    return {"n": len(net),
            "per_trade": round(float(net.mean() / net.std(ddof=1)), 4)}


def compare_filter(trades: pd.DataFrame, keep: pd.Series, label: str = "") -> dict:
    """Two arms that are *not* paired: a subset against the whole.

    An entry filter changes which trades exist, so the "with" arm is nested
    inside the "without" arm. The difference is a selection effect, and calling
    it causal would be wrong — a filter that keeps only winning trades looks
    excellent and predicts nothing. ``overlap`` is reported for that reason.
    """
    mask = pd.Series(keep).astype(bool)
    if len(mask) != len(trades):
        raise ValueError("keep 的长度必须与 trades 一致")
    kept = trades[mask.to_numpy()]
    base_net = pd.to_numeric(trades["net"], errors="coerce").astype(float)
    kept_net = pd.to_numeric(kept["net"], errors="coerce").astype(float)

    return {
        "ok": True,
        "kind": "filter",
        "label": label,
        "n_trades": len(trades),
        "n_kept": len(kept),
        "kept_share": round(float(len(kept) / len(trades)), 4) if len(trades) else 0.0,
        "arms": {"without": _dist(base_net), "with": _dist(kept_net)},
        "difference": {
            "mean": round(float(kept_net.mean() - base_net.mean()), 6)
            if len(kept) and len(trades) else 0.0,
            "note": "子集均值减全体均值；两臂重叠，不是配对差。",
        },
        "overlap": {
            "n_shared": len(kept),
            "share_of_with_arm": 1.0 if len(kept) else 0.0,
            "note": "with 臂完全包含于 without 臂，所以这个差是选择效应。",
        },
        "basis": [
            "入场过滤改变的是「有哪些交易」，不是「同一笔交易怎么做」。",
            "未做配对，因为被剔除的交易没有对应观测。",
        ],
        "caveats": [
            "把只保留赢家的过滤器读成 alpha 是这类分析最常见的错误。",
            "若要判因果，需要把过滤器当成一条新假设回到裁判（run_research）跑一遍。",
        ],
    }
