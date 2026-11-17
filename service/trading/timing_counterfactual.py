"""时机反事实：只回答「若当时触发，之后的条件分布是什么」。

不回答什么
----------
* **不给「本该赚多少」的点估计。** 一张 `expired_untriggered` 的票从未进
  OMS，它没有成交价、没有费用、没有持仓；给它编一个收益就是把愿望写成账本。
* **不下 verdict。** `arena.py:258-266` 已经把 verdict 删掉了，理由是区间落在
  哪、样本够不够都是机械事实，包成一个词只增加权威不增加信息。本模块沿用。
* **不改 `EventStudy` / runner / referee，也不写 `declined.py` / `memo.py`。**
  那些模块的「价格不进写入路径」是纪律；这里只读、只算、只返回。

分母口径（必须与 TCA 分开）
--------------------------
* 未触发 intent：整张票从未进 OMS，**不进 `tca.summary`**（后者只读
  `recent_fills`）。它是「一个决定没有被执行」的样本。
* partial-fill remainder：已进 OMS、余量被显式 retired，才是
  `opportunity_cost: "not measurable"` 的根因。两者混在一个分母里会让
  「没下单」看起来像「下了单没成交」，这是执行报告最常见的撒谎方式。

窗口口径
--------
`to_close` 用 `intraday_features.forward_to_close_pct`（到**实际最后一帧**，
不是 15:00 固定时刻）。`next_N_minutes` 由 `with_forward_horizon` 按实际帧时间
取该帧之后 N 分钟内最后一帧算，非周期帧不按序号推时刻。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd

from .intraday_buckets import conditional_distribution

#: 只有这两种终态进反事实；pending 还在等，triggered 已经兑现。
TERMINAL_INTENT_STATES = frozenset({"expired_untriggered", "denied"})


def _minute_of_day(stamp: Any) -> int | None:
    text = str(stamp or "")
    parts = text.split(":")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]) * 60 + int(parts[1])


def with_forward_horizon(features: pd.DataFrame, minutes: int = 30) -> pd.DataFrame:
    """补一列 ``forward_<N>min_pct``：同一交易日内 N 分钟内最后一帧的收益。

    按 ``code + day`` 分组、按实际帧时间排序；午休造成的空档自然跳过。
    没有更晚的可比帧时返回 ``NaN``，不是 0%——『没观察到』和『价格没变』
    是两种事实。跨日帧永不拼接，否则隔夜价格变化会被伪装成 N 分钟收益。
    """
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    column = f"forward_{minutes}min_pct"
    df = features.copy()
    df["_minute"] = df["frame_at"].map(_minute_of_day)
    df["_day"] = df["day"].astype(str) if "day" in df.columns else ""
    df = df.sort_values(["code", "_day", "frame_id"])
    pieces: list[pd.DataFrame] = []
    for (_code, _day), group in df.groupby(["code", "_day"], sort=False):
        group = group.copy()
        times = group["_minute"].tolist()
        prices = pd.to_numeric(group["last"], errors="coerce").tolist()
        forward: list[float | None] = []
        for i, (start, base) in enumerate(zip(times, prices)):
            if start is None or base is None or base <= 0 or pd.isna(base):
                forward.append(None)
                continue
            target = start + minutes
            j = i
            for k in range(i + 1, len(times)):
                if times[k] is None or times[k] > target:
                    break
                j = k
            if j == i:
                forward.append(None)
                continue
            end = prices[j]
            forward.append((end / base - 1) * 100
                           if end is not None and not pd.isna(end) else None)
        group[column] = forward
        pieces.append(group)
    if not pieces:
        return (df.drop(columns=["_minute", "_day"])
                .assign(**{column: pd.Series(dtype=float)}))
    return pd.concat(pieces).drop(columns=["_minute", "_day"])


def counterfactual(
    events: Iterable[Mapping[str, Any]] | None,
    features: pd.DataFrame | None,
    *,
    sentiment_state: str = "unspecified",
    horizon_minutes: int = 30,
    min_n: int = 30,
) -> dict:
    """按 `(shape, bucket_5min, sentiment_state)` 给终态意图的条件分布。

    返回里没有任何 verdict / edge / action / advice，也没有「本该赚多少」。
    样本不足的桶沿用 `intraday_buckets` 的 `insufficient`，不插值。
    """
    terminal = [e for e in (events or [])
                if str(e.get("state")) in TERMINAL_INTENT_STATES]
    empty = {
        "ok": False, "status": "no_features", "n_events": len(terminal),
        "events": [], "horizon_minutes": horizon_minutes,
        "note": "没有逐帧特征；旧分区缺 frame_id 时不可回补。",
    }
    if features is None or features.empty or not terminal:
        if terminal and (features is None or features.empty):
            return empty
        return {**empty, "ok": True, "status": "no_terminal_events"}

    enriched = with_forward_horizon(features, horizon_minutes)
    horizon_col = f"forward_{horizon_minutes}min_pct"
    by_window = conditional_distribution(enriched, sentiment_state=sentiment_state,
                                        value_col=horizon_col, min_n=min_n)
    by_close = conditional_distribution(enriched, sentiment_state=sentiment_state,
                                       value_col="forward_to_close_pct", min_n=min_n)

    def _index(report: dict) -> dict:
        return {(r["shape"], r["bucket_5min"], r["sentiment_state"]): r
                for r in report["buckets"]}

    window_idx, close_idx = _index(by_window), _index(by_close)
    records = []
    for event in terminal:
        key = (str(event.get("shape") or "unknown"),
               str(event.get("bucket_5min") or "unknown"),
               sentiment_state)
        records.append({
            "ticket_id": event.get("ticket_id"),
            "symbol": event.get("symbol"),
            "state": event.get("state"),
            "shape": key[0], "bucket_5min": key[1],
            "next_window": window_idx.get(key, {"status": "no_bucket", "n": 0}),
            "to_close": close_idx.get(key, {"status": "no_bucket", "n": 0}),
        })

    return {
        "ok": True,
        "status": "ok",
        "n_events": len(records),
        "events": records,
        "horizon_minutes": horizon_minutes,
        "sentiment_state": sentiment_state,
        "min_bucket_n": min_n,
        "denominator": {
            "untriggered_intents": len(records),
            "note": ("这些票整张从未进 OMS，不进 tca.summary（后者只读 recent_fills）。"
                     "partial-fill remainder 已进 OMS、余量 retired，是另一类样本，"
                     "不与此处合并。"),
        },
        "caveats": [
            "只给条件分布（n / mean / p25→p75），不给「本该赚多少」的点估计。",
            "未成交票没有成交价、费用与持仓，任何点估计都是把愿望写成账本。",
            "opportunity_cost 仍是 'not measurable'：本模块不翻案。",
            "不下 verdict；区间落在哪、样本够不够由读的人自己判断。",
        ],
    }
