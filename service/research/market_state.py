"""市场状态画像：当前处于什么位置，历史上类似位置之后发生过什么。

为什么有这个模块
----------------
前面十几轮研究一直在做"让内核输出信号"——因子打分、分位谱、择时投票。
方向上错了：内核是**信息源**，不是判断者（见 AGENTS.md 的分工表）。
一个输出 action / score / ranking 的内核，等于让算法替模型下结论，
而这个台子从第一天起就把这条线划在"没有 action 字段"上。

本模块只做两件事，都不含结论：

1. **画像**：把五个市场层面指标放到各自的历史分位上。分位是事实，
   "现在成交额处在近两年的 82% 分位"不是"该买"。
2. **条件分布**：找出历史上综合状态最接近的 k 个交易日，报告它们之后
   5 / 10 / 20 日的市场等权收益分布（n、p25、median、p75、正收益占比）。

**没有 action、没有 score、没有 ranking、没有目标价。** 分位低不代表便宜，
条件分布的中位数为正不代表会涨——它只说明"过去这种状态下，一半的日子比这个数更好"。

为什么用条件分布而不是回归
--------------------------
回归给点估计，点估计在噪声大的时候会制造虚假精确感。分布给区间与样本量，
读的人自己判断这个区间是否窄到足以支撑一个决定。这也是 `analog` 对个股用的同一套逻辑。

只读：不写 store、不登记假设、不改 trial 分母、不进 arena.edge、不产生委托。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from service.research.factors import _f, _wide

#: 五个指标。每个都是"当下可观测"，不依赖任何估计参数。
#: 方向不预设——`direction_hint` 只是历史读数的方向，不是建议。
METRICS: tuple[dict[str, str], ...] = (
    {"key": "amt_rel", "label": "成交额 / 20日均值",
     "why": "参与度。放量本身既可能是抢筹也可能是出货，方向由条件分布回答"},
    {"key": "mom20", "label": "市场 20 日累计收益",
     "why": "中期位置。跌了一段还是涨了一段，是判断反转/延续的输入"},
    {"key": "breadth_ma", "label": "上涨家数占比（20日均）",
     "why": "赚钱效应的广度。指数涨但广度窄，与指数涨且广度宽是两回事"},
    {"key": "csvol_rel", "label": "截面波动 / 60日均值",
     "why": "分歧程度。波动放大时个股分化，选股与择时的难度同时上升"},
    {"key": "margin_chg", "label": "两融余额 5 日变化",
     "why": "杠杆资金的边际方向。收缩与扩张对应不同的风险偏好"},
)

DEFAULT_HORIZONS = (5, 10, 20)


def _metrics(daily: pd.DataFrame, margin: pd.DataFrame | None) -> pd.DataFrame:
    close = _wide(daily, "close")
    pre = _wide(daily, "pre_close")
    amt = _wide(daily, "amount")
    if close.empty:
        return pd.DataFrame()
    ret = (close / pre.where(pre > 0) - 1).replace([np.inf, -np.inf], np.nan)
    ew = ret.mean(axis=1)
    total_amt = amt.sum(axis=1)
    out = pd.DataFrame({
        "ew": ew,
        "amt_rel": total_amt / total_amt.rolling(20).mean(),
        "mom20": (1 + ew).rolling(20).apply(np.prod, raw=True) - 1,
        "breadth_ma": ((ret > 0).sum(axis=1) / ret.notna().sum(axis=1)).rolling(20).mean(),
        "csvol_rel": ret.std(axis=1) / ret.std(axis=1).rolling(60).mean(),
    })
    if margin is not None and not margin.empty:
        tot = margin.groupby("trade_date")["rzye"].sum()
        out["margin_chg"] = (tot / tot.shift(5) - 1).reindex(out.index)
    else:
        out["margin_chg"] = np.nan
    return out


def _pct_rank(series: pd.Series, window: int = 500) -> pd.Series:
    """Rolling percentile that preserves an unknown current value as ``NaN``.

    Rolling comparisons alone turn ``NaN <= NaN`` into a numeric result once the
    window has enough history. Explicitly handling the current value and using
    only valid observations prevents a missing metric becoming a 0th percentile.
    """
    def rank(w: np.ndarray) -> float:
        current = w[-1]
        if not np.isfinite(current):
            return np.nan
        valid = w[np.isfinite(w)]
        return float((valid <= current).mean()) if len(valid) else np.nan

    return series.rolling(window, min_periods=120).apply(rank, raw=True)


def market_state(store, *, as_of: str | None = None, lookback: int = 500,
                 k: int = 120, horizons: tuple[int, ...] = DEFAULT_HORIZONS) -> dict[str, Any]:
    """Current state on five axes, plus what followed similar states historically.

    ``k`` similar days are selected by Euclidean distance in *percentile* space,
    so a 0.82 on volume and a 0.80 on momentum are close regardless of their raw
    scales. Ties are broken by recency, not by any quality judgement.
    """
    end = str(as_of or "")
    daily = store.read("daily", start="20230101", end=end or "20991231",
                       columns=["ts_code", "trade_date", "close", "pre_close", "amount"])
    daily = daily.assign(trade_date=daily["trade_date"].astype(str))
    if daily.empty:
        return {"ok": False, "status": "no_bars", "error": "窗口内没有日线"}
    end = end or daily["trade_date"].max()

    try:
        margin = store.read("margin_detail", start="20230101", end=end,
                            columns=["ts_code", "trade_date", "rzye"])
        margin = margin.assign(trade_date=margin["trade_date"].astype(str))
    except Exception:  # noqa: BLE001 - missing margin is a stated gap, not a crash
        margin = None

    frame = _metrics(daily, margin)
    if frame.empty:
        return {"ok": False, "status": "no_metrics"}
    frame = frame[frame.index <= end].dropna(subset=["ew"])

    # 分位用 rolling，避免全样本分位偷看未来。成交额、动量、广度和波动是
    # 四个核心维度；两融是可选维度，局部或整列缺失都不能把整段历史 drop 光。
    pct = pd.DataFrame({m["key"]: _pct_rank(frame[m["key"]]) for m in METRICS})
    core_keys = [m["key"] for m in METRICS if m["key"] != "margin_chg"]
    base = pct.dropna(subset=core_keys)
    if end not in base.index or len(base) < 200:
        return {"ok": False, "status": "short_history" if len(base) < 200 else "no_row_for_as_of",
                "error": f"{end} 不在核心四维可用分位索引里，或核心历史不足 200 天",
                "available_days": len(base), "required_dimensions": core_keys}

    today = pct.loc[end]
    available_metrics = [m["key"] for m in METRICS if pd.notna(today[m["key"]])]
    missing_metrics = [m["key"] for m in METRICS if pd.isna(today[m["key"]])]

    # 每个候选日只使用『当日与候选日都真实存在』的分位。用均方欧氏距离
    # （RMSE）而不是随维度数增长的平方和，使四维与五维距离仍可比较。
    values = base[available_metrics].to_numpy(dtype=float)
    current = today[available_metrics].to_numpy(dtype=float)
    valid = np.isfinite(values) & np.isfinite(current)
    n_dimensions = valid.sum(axis=1)
    squared = np.where(valid, (values - current) ** 2, 0.0).sum(axis=1)
    distances = np.sqrt(squared / np.maximum(n_dimensions, 1))
    distances[n_dimensions == 0] = np.nan
    dist = pd.Series(distances, index=base.index, dtype=float)
    # 排除最近 20 个核心可用交易日，避免"和上周的自己最像"这种无信息匹配。
    cutoff = base.index[max(0, len(base) - 21)]
    peers = dist[dist.index < cutoff].dropna().sort_values().head(k)
    dimensions_by_date = pd.Series(n_dimensions, index=base.index)

    fwd: dict[str, Any] = {}
    ew = frame["ew"]
    for h in horizons:
        realized = (1 + ew).rolling(h).apply(np.prod, raw=True).shift(-h) - 1
        vals = realized.reindex(peers.index).dropna()
        if len(vals) < 20:
            fwd[str(h)] = {"status": "insufficient", "n": len(vals)}
            continue
        fwd[str(h)] = {
            "status": "measured",
            "n": len(vals),
            "p25": _f(float(vals.quantile(0.25))),
            "median": _f(float(vals.median())),
            "p75": _f(float(vals.quantile(0.75))),
            "share_positive": _f(float((vals > 0).mean())),
            # 区间宽度是比中位数更该被读的东西：宽区间下中位数几乎没有信息量
            "iqr_width": _f(float(vals.quantile(0.75) - vals.quantile(0.25))),
        }

    return {
        "ok": True,
        "read_only": True,
        "as_of": end,
        "topic": "market_state",
        "degraded": bool(missing_metrics),
        "available_days": len(base),
        "available_metrics": available_metrics,
        "missing_metrics": missing_metrics,
        "picture": {
            m["key"]: {
                "label": m["label"],
                "value": (_f(float(frame.at[end, m["key"]]))
                          if pd.notna(frame.at[end, m["key"]]) else None),
                "percentile": (_f(float(today[m["key"]]))
                               if pd.notna(today[m["key"]]) else None),
                "why": m["why"],
            } for m in METRICS
        },
        "peers": {
            "k": len(peers),
            "method": ("当日可用分位上的两两共有维度 RMSE（均方欧氏距离），"
                       "核心四维必须存在，排除最近 20 个核心可用交易日"),
            "dimensions_today": len(available_metrics),
            "distance_normalization": "rmse_over_pairwise_available_dimensions",
            "peer_dimension_counts": {
                "4": int((dimensions_by_date.reindex(peers.index) == 4).sum()),
                "5": int((dimensions_by_date.reindex(peers.index) == 5).sum()),
            },
            "closest_date": str(peers.index[0]) if len(peers) else None,
            "closest_distance": _f(float(peers.iloc[0])) if len(peers) else None,
            "date_span": [str(peers.index.min()), str(peers.index.max())] if len(peers) else None,
            "note": ("相似的『日』不是相似的『行情』：单日状态接近不代表所处阶段相同。"
                     "这是条件分布的已知局限，读的时候必须带上"),
        },
        "forward_distribution": fwd,
        "caveats": [
            "没有 action / score / ranking / 目标价；分位低不代表便宜，中位数为正不代表会涨",
            "条件分布读的是『类似状态下的一半日子比这个数更好』，不是预测",
            "IQR 宽度比中位数更重要：区间宽时中位数几乎没有信息量",
            ("相似度只用当日与候选日都存在的分位，距离按共有维度数归一化；"
             "换权重或维度会得到另一批『相似』的日子"),
            *(([f"缺失指标 {', '.join(missing_metrics)} 保持 null，不按 0 分位参与画像或距离；"
                "本响应为 degraded 的四维读数"] if missing_metrics else [])),
        ],
        "note": "本层只读：不写 store、不登记假设、不改 trial 分母、不进 arena.edge、不产生委托。",
    }
