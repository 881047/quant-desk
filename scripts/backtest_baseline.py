"""First ETF rotation baseline vs 1/N vs buy-and-hold.

This is a research script, not the production kernel. It caches Tushare ETF
daily data in data/parquet/etf_daily.parquet, then runs a weekly momentum
rotation with costs.

Usage:
    .venv/Scripts/python.exe scripts/backtest_baseline.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from service.kernel.units import TRADING_DAYS_PER_YEAR

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

ETFS = {
    "510300.SH": "沪深300ETF",
    "510500.SH": "中证500ETF",
    "159915.SZ": "创业板ETF",
    "518880.SH": "黄金ETF",
    "511010.SH": "国债ETF",
    "513100.SH": "纳指ETF",
}
START = "20190101"
END = "20251231"
COST_PER_SIDE = 0.0010  # 10 bps: conservative for a 5,000 CNY account
CACHE = ROOT / "data" / "parquet" / "etf_daily.parquet"
REPORT = ROOT / "reports" / "baseline_etf_rotation.json"
EQUITY = ROOT / "reports" / "baseline_equity.csv"


def fetch() -> pd.DataFrame:
    if CACHE.exists():
        return pd.read_parquet(CACHE)
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        raise SystemExit("TUSHARE_TOKEN missing; put it in .env")
    import tushare as ts

    pro = ts.pro_api(token)
    rows = []
    for code, name in ETFS.items():
        daily = pro.fund_daily(ts_code=code, start_date=START, end_date=END)
        adj = pro.fund_adj(ts_code=code, start_date=START, end_date=END)
        if daily is None or daily.empty:
            print(f"[skip] {code} no daily")
            continue
        daily = daily.copy()
        daily["trade_date"] = pd.to_datetime(daily["trade_date"])
        if adj is not None and not adj.empty:
            adj = adj.copy()
            adj["trade_date"] = pd.to_datetime(adj["trade_date"])
            daily = daily.merge(adj[["trade_date", "adj_factor"]], on="trade_date", how="left")
        else:
            daily["adj_factor"] = 1.0
        daily["adj_factor"] = daily["adj_factor"].ffill().bfill().fillna(1.0)
        daily["adj_close"] = daily["close"] * daily["adj_factor"]
        daily["adj_open"] = daily["open"] * daily["adj_factor"]
        daily["code"] = code
        daily["name"] = name
        rows.append(daily[["trade_date", "code", "name", "open", "close", "adj_open", "adj_close", "vol", "amount"]])
        print(f"[ok] {code}: {len(daily)} rows")
    if not rows:
        raise SystemExit("no ETF data")
    df = pd.concat(rows, ignore_index=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CACHE, index=False)
    return df


def metrics(ret: pd.Series) -> dict:
    ret = ret.dropna()
    if ret.empty:
        return {}
    equity = (1 + ret).cumprod()
    years = len(ret) / 252
    ann = equity.iloc[-1] ** (1 / years) - 1 if years > 0 else 0
    vol = ret.std() * np.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = ret.mean() / ret.std() * np.sqrt(TRADING_DAYS_PER_YEAR) if ret.std() > 0 else 0
    dd = equity / equity.cummax() - 1
    maxdd = dd.min()
    calmar = ann / abs(maxdd) if maxdd < 0 else 0
    return {
        "total_return": float(equity.iloc[-1] - 1),
        "cagr": float(ann),
        "vol": float(vol),
        "sharpe": float(sharpe),
        "max_drawdown": float(maxdd),
        "calmar": float(calmar),
        "days": len(ret),
    }


def _next_open_returns(
    open_: pd.DataFrame,
    close: pd.DataFrame,
    target: pd.DataFrame,
    cost_per_side: float,
) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    """Execute close-formed targets at the next open and charge costs at that open."""
    weights = target.shift(1).ffill().fillna(0.0)
    held_weights = weights.shift(1).fillna(0.0)
    overnight_ret = open_ / close.shift(1) - 1
    intraday_ret = close / open_ - 1
    overnight = (held_weights * overnight_ret).sum(axis=1)
    intraday = (weights * intraday_ret).sum(axis=1)
    turnover = (weights - held_weights).abs().sum(axis=1)
    ret = (1 + overnight) * (1 - turnover * cost_per_side) * (1 + intraday) - 1
    return ret, weights, turnover


def run() -> None:
    df = fetch()
    close = df.pivot(index="trade_date", columns="code", values="adj_close").sort_index()
    open_ = df.pivot(index="trade_date", columns="code", values="adj_open").sort_index()
    close = close.ffill(limit=5)
    open_ = open_.ffill(limit=5)
    daily_ret = close.pct_change()

    mom20 = close / close.shift(20) - 1
    mom60 = close / close.shift(60) - 1
    ma60 = close.rolling(60).mean()
    score = 0.5 * mom20.rank(axis=1, pct=True) + 0.5 * mom60.rank(axis=1, pct=True)
    eligible = (close > ma60) & (mom60 > 0) & close.notna()

    # Weekly signal at Friday close, executed at the next trading day's open.
    target = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    fridays = close.index[close.index.dayofweek == 4]
    for d in fridays:
        row = score.loc[d].where(eligible.loc[d])
        target.loc[d:, :] = 0.0
        if row.notna().any():
            best = row.idxmax()
            target.loc[d:, best] = 1.0
    strat_ret, weights, turnover = _next_open_returns(open_, close, target, COST_PER_SIDE)

    # 1/N: equal weight, monthly close signals executed at the next open.
    eq_target = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    month_first = close.index.to_series().groupby([close.index.year, close.index.month]).head(1)
    for d in month_first:
        avail = close.loc[d].notna()
        eq_target.loc[d:, :] = 0.0
        if avail.any():
            eq_target.loc[d:, avail] = 1.0 / avail.sum()
    eq_ret, eq_weights, eq_turnover = _next_open_returns(open_, close, eq_target, COST_PER_SIDE)

    # Buy and hold 510300.
    bh_ret = daily_ret["510300.SH"].fillna(0.0)

    # Core: risk parity across risk assets + MA200 regime filter + 12% vol target.
    risk_cols = [c for c in close.columns if c != "511010.SH"]
    ma200 = close["510300.SH"].rolling(200).mean()
    vol20 = daily_ret.rolling(20).std()
    core_target = 0.12
    core_weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for d in month_first:
        core_weights.loc[d:, :] = 0.0
        defensive = pd.isna(ma200.loc[d]) or close.loc[d, "510300.SH"] <= ma200.loc[d]
        if defensive:
            core_weights.loc[d:, "511010.SH"] = 1.0
            continue
        v = vol20.loc[d, risk_cols].replace(0, np.nan)
        if v.notna().sum() < len(risk_cols) - 1:
            core_weights.loc[d:, "511010.SH"] = 1.0
            continue
        inv = 1.0 / v
        inv = inv / inv.sum()
        inv = inv.clip(upper=0.35)
        inv = inv / inv.sum()
        port_vol = float((inv * v).sum()) * np.sqrt(TRADING_DAYS_PER_YEAR)
        scale = min(1.0, core_target / port_vol) if port_vol > 0 else 1.0
        for c in risk_cols:
            core_weights.loc[d:, c] = float(inv.get(c, 0.0)) * scale
        core_weights.loc[d:, "511010.SH"] += max(0.0, 1.0 - scale)
    core_ret, core_weights, core_turnover = _next_open_returns(
        open_, close, core_weights, COST_PER_SIDE
    )

    out = {
        "config": {
            "etfs": ETFS,
            "start": START,
            "end": END,
            "cost_per_side": COST_PER_SIDE,
            "execution_rule": (
                "strategy signals form at T close and execute at T+1 open; "
                "overnight returns use old holdings, intraday returns use new holdings, "
                "and costs are charged at the execution open; buy-and-hold is close-to-close"
            ),
            "rotation_rule": "weekly top-1 20/60 momentum, close>MA60, else cash",
            "core_rule": "monthly inverse-vol risk parity, MA200 regime -> bond, 12% vol target",
        },
        "period": {"start": str(close.index.min().date()), "end": str(close.index.max().date())},
        "rotation": metrics(strat_ret),
        "core_risk_parity": metrics(core_ret),
        "equal_weight": metrics(eq_ret),
        "buy_hold_510300": metrics(bh_ret),
        "rotation_turnover_annual": float(turnover.sum() / (len(turnover) / TRADING_DAYS_PER_YEAR)),
        "rotation_switches": int((weights.diff().abs().sum(axis=1) > 0).sum()),
        "core_turnover_annual": float(core_turnover.sum() / (len(core_turnover) / TRADING_DAYS_PER_YEAR)),
        "core_switches": int((core_weights.diff().abs().sum(axis=1) > 0).sum()),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(
        {
            "rotation": (1 + strat_ret.fillna(0)).cumprod(),
            "core_risk_parity": (1 + core_ret.fillna(0)).cumprod(),
            "equal_weight": (1 + eq_ret.fillna(0)).cumprod(),
            "buy_hold_510300": (1 + bh_ret.fillna(0)).cumprod(),
        }
    ).to_csv(EQUITY, encoding="utf-8-sig")

    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"report: {REPORT}")
    print(f"equity: {EQUITY}")


if __name__ == "__main__":
    run()
