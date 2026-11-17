"""样本独立性：一个事件表里到底有几个独立观测。

为什么这个模块必须存在
----------------------
2026-11-12 的公告事件研究，初次测得事件池（回购/业绩/增持/质押/股权激励，h=5）
日均超额 **+46.9bps、t = 3.14**，一度被当成整段研究里最好的结果。

做去重叠校正后：

```
去重叠间隔    超额        t
无           +46.9      3.14
5 天         +41.8      2.71
10 天        +35.1      2.45
20 天        +22.8      1.43
40 天        +18.5      1.06
```

单调衰减，没有一个口径通过 |t|>3。那个 46.9bps 主要来自「同一只票 10 天内发多条公告」——
它们的前向收益区间高度重叠，本质是同一个观测被数了多次。

更值得注意的是：``board`` 的 caveat 早就写过同一件事——「样本按笔计，同一天多只票
同涨同跌，区间会偏窄」。道理一直在仓库里，只是没做成一个每次研究都要过的检查。

判定规则
--------
- 同一标的的事件，若日期间隔小于持有期 h，前向收益区间必然重叠 → 只保留第一条。
- 即使去重叠后，不同标的在**同一天**的事件仍共享市场收益 → 独立交易日数是另一个上限。
- t 值的虚高倍数近似 ``sqrt(n_obs / n_independent)``，因为标准误 ∝ 1/√n。

本模块只做核算，不判断「这个结果算不算数」——给出去重叠前后的 n 与预估的 t 虚高倍数，
读的人自己决定要不要重测。
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


def _day_key(value: Any) -> str:
    return str(value).replace("-", "").replace("/", "")


def _sessions(calendar: Any) -> tuple[list[str], str]:
    """Return ordered sessions from an explicit frame/sequence or data store.

    The research layer must not import the trading-layer singleton. A caller that
    wants the project's ``trade_cal`` passes its DatasetStore as ``calendar``.
    """
    basis = "supplied_sessions"
    if calendar is None:
        raise ValueError("trading calendar is required; pass trade_cal sessions or a data store")
    if hasattr(calendar, "read"):
        calendar = calendar.read("trade_cal", columns=["cal_date", "is_open"])
        basis = "trade_cal"
    if isinstance(calendar, pd.DataFrame):
        from service.adapters.pit import TradingCalendarStore
        days = list(TradingCalendarStore.from_frame(calendar).days)
        basis = "trade_cal" if "is_open" in calendar.columns else basis
    else:
        days = [str(day) for day in calendar]
    sessions = sorted({_day_key(day) for day in days if str(day)})
    if not sessions:
        raise ValueError("trading calendar is required; event dates are not a trading calendar")
    return sessions, basis


def dedup_events(events: pd.DataFrame, *, horizon: int, gap: int | None = None,
                 date_col: str = "trade_date", code_col: str = "ts_code",
                 calendar: Any = None) -> pd.DataFrame:
    """Keep the first event per code inside each ``gap``-session window.

    ``gap`` defaults to ``horizon``. Event dates are mapped to the first real
    trading session on or after the event, so weekend/holiday announcements do
    not become fictitious sessions. ``calendar`` accepts a trade_cal DataFrame,
    a session sequence, or a DatasetStore injected by the caller. It is required:
    the function never numbers only dates present in the event table, because
    sparse events would make a month look like one session.
    """
    if events is None or events.empty:
        return pd.DataFrame(columns=[code_col, date_col])
    gap = int(gap if gap is not None else horizon)
    frame = events[[code_col, date_col]].copy()
    frame[date_col] = frame[date_col].astype(str)
    frame["_day_key"] = frame[date_col].map(_day_key)
    frame = frame.drop_duplicates().sort_values([code_col, "_day_key"])
    sessions, basis = _sessions(calendar)
    import bisect

    kept: list[tuple[str, str]] = []
    last: dict[str, int] = {}
    unmapped = 0
    for code, day, raw_day in zip(frame[code_col].astype(str), frame["_day_key"],
                                  frame[date_col]):
        pos = bisect.bisect_left(sessions, day)
        if pos >= len(sessions):
            unmapped += 1
            continue
        prev = last.get(code)
        if prev is None or pos - prev >= gap:
            kept.append((code, raw_day))
            last[code] = pos
    out = pd.DataFrame(kept, columns=[code_col, date_col])
    out.attrs["calendar_basis"] = basis
    out.attrs["n_unmapped_events"] = unmapped
    out.attrs["n_calendar_sessions"] = len(sessions)
    return out


def independence(events: pd.DataFrame, *, horizon: int, gap: int | None = None,
                 date_col: str = "trade_date", code_col: str = "ts_code",
                 calendar: Any = None) -> dict[str, Any]:
    """Count observations before and after removing same-code overlap.

    Returns the inflation factor and the t-statistic inflation it implies, so a
    caller can see at a glance whether a reported t was propped up by double
    counting.
    """
    if events is None or events.empty:
        return {"status": "no_events", "n_obs": 0, "n_independent": 0}

    raw = events[[code_col, date_col]].drop_duplicates()
    n_obs = len(raw)
    kept = dedup_events(raw, horizon=horizon, gap=gap,
                        date_col=date_col, code_col=code_col, calendar=calendar)
    n_ind = len(kept)
    n_days = int(raw[date_col].astype(str).nunique())
    n_codes = int(raw[code_col].astype(str).nunique())
    fallback = max(n_ind, 1)
    inflation = n_obs / fallback
    # 标准误 ∝ 1/√n，所以 n 被放大 x 倍时 t 被放大 √x 倍。
    t_factor = math.sqrt(inflation)
    days_ratio = n_obs / n_days if n_days else None

    out: dict[str, Any] = {
        "status": "measured",
        "horizon": int(horizon),
        "gap_sessions": int(gap if gap is not None else horizon),
        "n_obs": n_obs,
        "n_independent": n_ind,
        "n_days": n_days,
        "n_codes": n_codes,
        "calendar_basis": kept.attrs.get("calendar_basis"),
        "n_unmapped_events": kept.attrs.get("n_unmapped_events", 0),
        "n_calendar_sessions": kept.attrs.get("n_calendar_sessions"),
        "observations_per_day": round(n_obs / n_days, 2) if n_days else None,
        "inflation": round(inflation, 3),
        "t_inflation_if_ignored": round(t_factor, 3),
        # 两个上限分开报，不要合成一个"有效样本"：
        # - n_independent 已去掉同标的的期初重叠，但仍把同日多只票当独立；
        # - n_days 是绝对下限（假设同一天所有标的完全相关），通常过于保守。
        # 真实的独立样本落在这两者之间，取决于个股特异性占比（A 股大约 0.6~0.7 的
        # 方差是特异性的，所以真值更靠近 n_independent 一侧）。
        "independent_n_range": [int(min(n_ind, n_days)) if n_days else n_ind, n_ind],
        "t_inflation_vs_days": (round(math.sqrt(days_ratio), 3) if days_ratio else None),
        "reading": (
            "inflation > 1 表示那个 n 含同标的的重复观测；把 t 除以 "
            "t_inflation_if_ignored 可粗估去重叠后的 t。"
            "independent_n_range 的下界是独立交易日数（假设同日完全相关，过于保守），"
            "上界是去重叠笔数（假设同日完全独立，过于乐观）；真值在两者之间，"
            "A 股约 6~7 成方差是个股特异性的，所以更靠近上界"
        ),
    }
    if inflation >= 1.15:
        out["advice"] = ("重叠显著（n 虚高 ≥15%）：报告 t 值前必须先做去重叠，"
                         "否则显著性会被高估")
    else:
        out["advice"] = "重叠不显著，可直接使用原样本"
    return out


def t_after_dedup(t_reported: float, info: dict[str, Any]) -> float | None:
    """Approximate the t this result would have had without double counting.

    A magnitude, not a substitute for re-running: the de-duplicated sample has
    its own variance, not just a smaller n.
    """
    if t_reported is None:
        return None
    factor = info.get("t_inflation_if_ignored")
    if not factor:
        return None
    return round(t_reported / factor, 3)


__all__ = ["dedup_events", "independence", "t_after_dedup"]
