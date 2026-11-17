"""按时段的条件分布：把「这个形态之后会怎样」变成带 n 与区间的可复算查询。

与分析层其它模块的分工
----------------------
``analog.py`` 的最小粒度是**交易日**（8 个日频特征、horizons=[1,2,3,4,5,10] 天）。
本模块把统计单元换成 ``(形态, 5 分钟桶, 情绪状态)``，回答「09:35 看到这个
形态之后 30 分钟 / 到当日最后一帧会怎样」。它不改 ``analog.py`` 的 FEATURES /
HORIZONS / ``index_version``——动那些会触发 weights 长度硬校验。

口径（钉死）
------------
1. 分组键是三元组；桶边界由 ``intraday_features.frame_bucket`` 从**实际帧时间**
   算，不按序号推时刻。
2. 每桶必须带 ``n`` 与区间（p25/p75）。样本 < ``min_bucket_n``（默认 **30**，
   对齐 ``params.yaml:306``）返回 ``insufficient``，不插值、不跨桶借样本。
3. **不给点估计式的「本该赚多少」**：均值/中位数只是描述，不是反事实结算。
4. U 型波动率只是背景 note，绝不作为入场信号或自动阈值。响应里没有
   ``signal`` / ``score`` / ``ranking`` / ``action`` 任何一个键。
5. 情绪状态由调用方按 ``params.yaml:92-101`` 的既有分档传入，本模块不重造阈值。
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from service import vocabulary as vocab

#: 对齐 params.yaml:306 的 min_bucket_n；不足就如实说 insufficient。
MIN_BUCKET_N = 30

VALUE_COLUMNS = ("forward_to_close_pct", "frame_return_pct")
_HORIZON_RE = re.compile(r"^forward_\d+min_pct$")


def valid_value_col(column: str) -> bool:
    """允许 timing_counterfactual 生成的 forward_<N>min_pct 列，不另写一套统计。"""
    return column in VALUE_COLUMNS or bool(_HORIZON_RE.match(str(column)))


def conditional_distribution(
    features: pd.DataFrame | None,
    *,
    sentiment_state: str = "unspecified",
    value_col: str = "forward_to_close_pct",
    min_n: int = MIN_BUCKET_N,
) -> dict:
    """按 ``(shape, bucket_5min, sentiment_state)`` 分组，产出带 n 与区间的分布。"""
    if not valid_value_col(value_col):
        raise ValueError(f"value_col 只能是 {VALUE_COLUMNS} 或 forward_<N>min_pct，收到 {value_col!r}")
    if features is None or features.empty:
        return {"ok": False, "status": "no_features", "buckets": [],
                "value_col": value_col, "min_bucket_n": min_n,
                "note": "没有逐帧特征；旧分区缺 frame_id 时不可回补。"}

    df = features.copy()
    for column in ("shape", "bucket_5min"):
        if column not in df.columns:
            raise ValueError(f"features 缺列：{column}")
    df["shape"] = df["shape"].fillna("unknown")
    df["bucket_5min"] = df["bucket_5min"].fillna("unknown")
    df["sentiment_state"] = str(sentiment_state)
    values = pd.to_numeric(df[value_col], errors="coerce") if value_col in df.columns else pd.Series(dtype=float)
    df[value_col] = values

    rows: list[dict[str, Any]] = []
    keys = ("shape", "bucket_5min", "sentiment_state")
    for (shape, bucket, state), group in df.groupby(list(keys), sort=True):
        observed = group[value_col].dropna()
        n = len(observed)
        entry: dict[str, Any] = {
            "shape": str(shape), "bucket_5min": str(bucket),
            "sentiment_state": str(state), "n": n,
        }
        if n < min_n:
            entry.update({"status": "insufficient", "mean": None, "median": None,
                          "p25": None, "p75": None, "interval": None})
        else:
            p25 = float(observed.quantile(0.25))
            p75 = float(observed.quantile(0.75))
            entry.update({
                "status": "ok",
                "mean": round(float(observed.mean()), 4),
                "median": round(float(observed.median()), 4),
                "p25": round(p25, 4), "p75": round(p75, 4),
                "interval": [round(p25, 4), round(p75, 4)],
            })
        rows.append(entry)

    return {
        "ok": True,
        "status": "ok",
        "buckets": rows,
        "bucket_count": len(rows),
        "value_col": value_col,
        "min_bucket_n": min_n,
        "units": vocab.note("buckets"),
        "note": (
            "U 型日内波动率是背景条件，不是信号；分组结果不得直接当入场阈值。"
            "样本不足的桶只报 insufficient，不插值。均值/中位数是描述，"
            "不是「本该赚多少」的反事实结算。"
        ),
    }
