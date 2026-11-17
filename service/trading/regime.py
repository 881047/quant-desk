"""Market regime: one label, four measured components, no prophecy.

The label exists so a strategy can declare *when it is allowed to run*. It is
deliberately coarse (four states) and computed only from data already stored, so
it can be reproduced for any past session.

What this is not: a predictor. Every component is descriptive, and the label is
allowed to be wrong; its job is to stop a mean-reversion tactic from sizing up
inside a trend breakdown without anyone noticing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from service import settings
from service.kernel.units import TRADING_DAYS_PER_YEAR

TREND_MA = settings.integer("regime.trend_ma")
VOL_WINDOW = settings.integer("regime.vol_window")
BREADTH_WINDOW = settings.integer("regime.breadth_window")
VOL_STRESS = settings.num("regime.vol_stress")   # 1-yr percentile: above = stressed
VOL_CALM = settings.num("regime.vol_calm")


# 这里原本有一张 PERMISSION 表，四档各配一句祈使句（"只允许减仓与对冲，
# 不新开多头"）。它有两个问题，第二个更重：
#
# 1. 措辞像规则，而 guard 不读它——`enforced: False` 就在旁边，等于承认了它是话。
# 2. 它在替调用方下决定。四档标签是**阈值约定的产物**，把约定翻译成"该怎么做"
#    是读的人的活。2026-11-03 的指令：判断交给调用方，内核只给事实。
#
# 留下的是同样的信息，换成语义上诚实的形态：标签由哪几个数、按什么阈值算出。
# 读者能看到 `vol_percentile_1y=0.86 > vol_stress=0.72` 并自己决定要不要减仓——
# 也可以不同意这个阈值。
_LABEL_RULE = ("trend = close vs MA{ma}; vol_state = 20日波动率在近一年的分位 vs "
               "stressed>{stress} / calm<{calm}; breadth = MA20 上方个股占比 vs "
               "0.35 / 0.50。label 是这三项的组合，是阈值约定，不是市场事实。")


@dataclass(frozen=True)
class Regime:
    as_of: str
    label: str                 # risk_on | neutral | risk_off | stressed
    trend: str                 # up | down
    vol_state: str             # calm | normal | stressed
    breadth: float
    vol_20d: float
    vol_percentile: float
    ew_index: float
    ew_ma: float | None
    reasons: list[str]

    def label_basis(self) -> str:
        """一行算式摘要，给 4 KB 上下文包用。

        `label_rule()` 是给人逐项核对的全量形态（~400 B），塞进 context pack
        会把候选行挤掉（实测 5 行掉到 3 行）。这里只留三个数和对的阈值——
        信息密度一样，字节数是它的六分之一。
        """
        return (f"{self.trend}·vol分位{self.vol_percentile:.2f}"
                f"(stressed≥{VOL_STRESS}/calm<{VOL_CALM})"
                f"·宽度{self.breadth:.0%}(risk_on≥50%/risk_off<35%)")

    def label_rule(self) -> dict:
        """标签是**从哪几个数、按什么阈值**算出来的。

        替代原来的 ``permission()``。那句话是"今天能做什么"的祈使句，
        而这里给的是"这个标签怎么来的"——读者可以据此同意，也可以据此反对。
        """
        return {
            "trend": {"name": "close vs MA", "ma": TREND_MA,
                      "value": self.trend},
            "vol_state": {"name": "20日波动率近一年分位",
                          "value": round(self.vol_percentile, 3),
                          "stressed_above": VOL_STRESS, "calm_below": VOL_CALM,
                          "state": self.vol_state},
            "breadth": {"name": "MA20 上方个股占比",
                        "value": round(self.breadth, 3),
                        "risk_on_at_least": 0.50, "risk_off_below": 0.35},
            "rule": _LABEL_RULE.format(ma=TREND_MA, stress=VOL_STRESS, calm=VOL_CALM),
        }

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "label": self.label,
            "trend": self.trend,
            "vol_state": self.vol_state,
            "breadth_above_ma20": round(self.breadth, 3),
            "vol_20d": round(self.vol_20d, 4),
            "vol_percentile_1y": round(self.vol_percentile, 3),
            "equal_weight_index": round(self.ew_index, 4),
            "equal_weight_ma": None if self.ew_ma is None else round(self.ew_ma, 4),
            # 原来的 `permission`（一句祈使）换成 `label_from`（标签的算式输入）。
            "label_from": self.label_rule(),
            # `permission` is prose, and prose about permission reads as a rule.
            # No check in the guard reads this label: a proposal inside `stressed`
            # can still be approved, and `duty` says so in words. Saying it here
            # too, in one boolean, is what keeps a reader from inferring a veto
            # that does not exist — or, worse, from assuming the label is
            # decorative when it is the one thing that stops a mean-reversion
            # tactic from sizing up inside a breakdown.
            "enforced": False,
            "reasons": self.reasons,

        }


def _ew_series(panel: pd.DataFrame) -> pd.Series:
    # Adjusted close: the index is a cumulative product, so a single ex-date
    # gap is permanent drift, and the drift is always downward (a dividend is not
    # a loss). Over 920 sessions that is a few percent of level, right where the
    # MA200 that decides `trend` lives.
    col = "adj_close" if "adj_close" in panel.columns else "close"
    p = panel[["trade_date", "ts_code", col]].copy().rename(columns={col: "px"})
    p["trade_date"] = p["trade_date"].astype(str)
    p["ret"] = p.groupby("ts_code")["px"].pct_change()
    daily = p.groupby("trade_date")["ret"].mean().dropna()
    return (1 + daily).cumprod()


def _breadth(panel: pd.DataFrame, window: int) -> float:
    p = panel.copy()
    p["trade_date"] = p["trade_date"].astype(str)
    col = "adj_close" if "adj_close" in p.columns else "close"
    try:
        # `pivot` is ~20% faster than `pivot_table(aggfunc="last")` and gives the
        # same frame when the pair is unique, which it is: `daily` has one row per
        # (code, date). The fallback keeps a duplicated vendor row from turning
        # the whole regime into a 500.
        wide = p.pivot(index="trade_date", columns="ts_code", values=col)
    except ValueError:
        wide = p.pivot_table(index="trade_date", columns="ts_code", values=col,
                             aggfunc="last")
    wide = wide.sort_index()
    if len(wide) <= window:
        return float("nan")
    # Only the last row of the rolling mean is read. Rolling the whole
    # 320 x ~5400 frame cost 0.65 s; the last `window + 1` rows give the same
    # number in 0.28 s (measured 2026-10-31, 0.1292 both ways). The row after the
    # window is the one being tested, so it has to stay.
    tail = wide.tail(window + 1)
    ma = tail.rolling(window).mean().iloc[-1]
    last = tail.iloc[-1]
    valid = last.notna() & ma.notna()
    if not valid.any():
        return float("nan")
    return float((last[valid] > ma[valid]).mean())


def compute(market, as_of: str | None = None, lookback: int = 320) -> Regime:
    """Cached per (day, lookback): this reads ~300 sessions of daily bars."""
    day = as_of or market.latest_bar_date()
    if day and hasattr(market, "_cached"):
        return market._cached(f"regime:{day}:{lookback}",
                              lambda: _compute(market, day, lookback))
    return _compute(market, day, lookback)


def _compute(market, day: str | None, lookback: int) -> Regime:
    if not day:
        raise ValueError("no daily bars")
    panel = market.panel(day, lookback)
    if panel.empty:
        raise ValueError(f"no bars around {day}")

    ew = _ew_series(panel)
    if ew.empty:
        raise ValueError("cannot build an equal-weight index")
    today = float(ew.iloc[-1])
    ma = float(ew.tail(TREND_MA).mean()) if len(ew) >= TREND_MA else None
    trend = "up" if (ma is not None and today >= ma) else "down"

    rets = ew.pct_change().dropna()
    vol_now = float(rets.tail(VOL_WINDOW).std() * np.sqrt(TRADING_DAYS_PER_YEAR)) if len(rets) >= VOL_WINDOW else 0.0
    rolling = rets.rolling(VOL_WINDOW).std() * np.sqrt(TRADING_DAYS_PER_YEAR)
    rolling = rolling.dropna()
    pct = float((rolling <= vol_now).mean()) if len(rolling) else 0.5
    vol_state = "stressed" if pct >= VOL_STRESS else ("calm" if pct <= VOL_CALM else "normal")

    breadth = _breadth(panel, BREADTH_WINDOW)
    breadth = 0.5 if np.isnan(breadth) else breadth

    reasons: list[str] = []
    if ma is None:
        reasons.append(f"历史不足 {TREND_MA} 日，趋势按向下处理")
    else:
        reasons.append(f"等权指数 {today:.3f} {'≥' if trend == 'up' else '<'} MA{TREND_MA} {ma:.3f}")
    reasons.append(f"20 日实现波动 {vol_now:.1%}，处于近一年 {pct:.0%} 分位")
    reasons.append(f"MA{BREADTH_WINDOW} 上方个股占比 {breadth:.1%}")

    if vol_state == "stressed":
        label = "stressed"
    elif trend == "down" and breadth < 0.35:
        label = "risk_off"
    elif trend == "up" and breadth >= 0.50:
        label = "risk_on"
    else:
        label = "neutral"

    return Regime(as_of=str(day), label=label, trend=trend, vol_state=vol_state,
                  breadth=breadth, vol_20d=vol_now, vol_percentile=pct,
                  ew_index=today, ew_ma=ma, reasons=reasons)
