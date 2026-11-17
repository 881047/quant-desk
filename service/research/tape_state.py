"""涨停结构的状态，以及它之后发生了什么。

上游的炸板池只留一年，所以封板率一直只有 243 个交易日、没有前向分布（P35 把那句
「未建」写进了问题卡）。但**炸板是可以推导的**：某只票摸到涨停价、收盘没封住。
`stk_limit` 带了每只票每个交易日的精确涨停价，覆盖 2010 年至今——于是这条路从
243 天变成 4,000 天。

**推导出来的不是上游那一条。** 在 680 个重叠交易日上，涨停家数相关 0.990，
但中位比值 1.09：上游的池子排掉了一些这里算进来的东西（新股、北交所、特殊处理）。
所以**绝对水平不可互换**，这个模块用的是**分位分桶**，并且每个返回值都写明来源。

一个刻意的选择：前向指标是「次日开盘买当天封住的票，第三日收盘卖」。这才是游资的
问题，而且**它必须是 T+1 可执行的**：同日买同日卖在 A 股不允许，用「开盘买、收盘卖」
算出来的收益看起来漂亮，但没有人能拿到。第一版就是这么错的，数字是 +0.29%，
改成本条之后才算数。

等权市场的前向收益是另一个问题，这里不混进来。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from service import settings
from service.kernel.units import (
    LIMIT_BANDS as LIMIT_BANDS_ALL,
)

from .state import _content_digest

# 分位窗口与预热，和 state.py 同口径：只用当日及之前，未来不参与。
PCT_WINDOW = settings.integer("research.pct_window")
PCT_MIN = settings.integer("research.pct_min")
SCHEMA = "tape_v1"


def _tercile(pct: float | None) -> str:
    if pct is None or (isinstance(pct, float) and np.isnan(pct)):
        return "?"
    return "low" if pct < 1 / 3 else ("high" if pct >= 2 / 3 else "mid")


# 涨跌幅档：5% 是未完成股改（且 2026-07-06 之前是主板 ST），10% 是主板常态，
# 20% 是创业板/科创板，30% 是北交所。档位由 ``up_limit / pre_close`` 算出，
# 不靠股票名字——名字要另建时间轴，而幅度是数据本身。
#
# 这份列表跟着**数据里实际存在的档**走，不是跟着"当前规则"走：主板 ST 自
# 2026-07-06 起是 10%，但未股改股票恒为 5%，历史行情里也有大量 5% 的主板 ST。
# 用今天的规则去分类历史行情，会把 5% 的票判成"不属于任何一档"而丢掉。
LIMIT_BANDS = tuple(float(band) for band in LIMIT_BANDS_ALL)
BAND_TOL = settings.num("research.band_tol")


def band_of(up_limit: float, pre_close: float,
            tol: float = BAND_TOL) -> str | None:
    """该票当天的涨跌幅档，例如 ``"10%"``。落在档位之间返回 ``None``。

    用幅度而不是名字：ST 的判定要一张改名时间轴，而且改名有滞后；幅度是
    ``stk_limit`` 与日线直接给的事实。代价是同一天同一档里既有 ST 也有别的，
    那种时候幅度本来就不区分它们。
    """
    if not (pre_close and pre_close > 0 and up_limit and up_limit > 0):
        return None
    amp = up_limit / pre_close - 1
    for limit in LIMIT_BANDS:
        if abs(amp - limit) <= tol:
            return f"{limit:.0%}"
    return None


BOARDS = (("科创", ("68",)), ("创业", ("30",)),
          ("沪主板", ("60",)), ("深主板", ("00",)),
          ("北交", ("43", "83", "87", "88", "92")),
          ("沪B", ("90",)), ("深B", ("20",)))


def board_of(code: str) -> str:
    """板块。用于「制度稳定」口径：只有主板在 2010-2026 全程都是 10%。

    创业板 2020-08 从 10% 改到 20%，科创板 2019-07 起就是 20%，北交所 30%。
    所以按**幅度**分档会说「10% 这一档的成分变了」——2020 年之前它包含创业板，
    之后只剩主板。要一个跨全样本不变的口径，只能按板块切。

    前缀用两位而不是一位：``9`` 开头既有沪 B（900xxx）也有北交所（920xxx），
    按一位分会把 B 股算进北交所。
    """
    text = str(code)
    for name, prefixes in BOARDS:
        if text.startswith(prefixes):
            return name
    return "其他"


def _rolling_pct(series: pd.Series) -> pd.Series:
    """Share of the window at or below today, on the same definition as ``state``.

    This used to divide by ``len(w) - 1`` and to let NaN take part in the
    comparison (``NaN <= x`` is False, so a gap counted as "below today", which
    pushed the percentile down). ``state._rolling_pct`` divides by the valid
    count and includes today, so the same day could be a "low" bucket on the tape
    and a "mid" bucket on the tape's own breadth — the two halves of one regime
    reading, disagreeing about which tercile today is in.
    """
    def _pct(window: pd.Series) -> float:
        valid = window.dropna()
        if len(valid) < 2:
            return float("nan")
        return float((valid <= valid.iloc[-1]).mean())

    return series.rolling(PCT_WINDOW, min_periods=PCT_MIN).apply(_pct, raw=False)


def build_tape_state(daily: pd.DataFrame, limits: pd.DataFrame) -> pd.DataFrame:
    """每日的涨停结构、它的分位，以及「次日买涨停股」的前向收益。

    ``daily`` 需要 ts_code / trade_date / open / high / close；``limits`` 需要
    ts_code / trade_date / up_limit。两者按 (ts_code, trade_date) 内连接，涨停价
    缺失的行直接排除——用 0.9 或 1.1 去猜一个涨停价，会把 20% 板和新股一起算错。
    """
    cols = ["ts_code", "trade_date", "open", "high", "close"]
    df = daily[[c for c in cols if c in daily.columns]].copy()
    df["trade_date"] = df.trade_date.astype(str)
    lim = limits[["ts_code", "trade_date", "up_limit"]].copy()
    lim["trade_date"] = lim.trade_date.astype(str)

    for col in ("open", "high", "close"):
        value = pd.to_numeric(df[col], errors="coerce")
        df[col] = value.where(value > 0)
    lim["up_limit"] = pd.to_numeric(lim["up_limit"], errors="coerce")
    lim = lim[lim["up_limit"] > 0]

    m = df.merge(lim, on=["ts_code", "trade_date"], how="inner")
    m = m.dropna(subset=["open", "high", "close"])
    if m.empty:
        return pd.DataFrame()

    eps = 1e-6
    m["sealed"] = m["close"] >= m["up_limit"] - eps
    m["touched"] = m["high"] >= m["up_limit"] - eps
    m["broken"] = m["touched"] & ~m["sealed"]

    m = m.sort_values(["ts_code", "trade_date"], kind="mergesort")
    g = m.groupby("ts_code", sort=False)
    # 次日开盘买、第三日收盘卖。**必须是两个交易日**：同日买同日卖在 A 股不允许，
    # 而「次日开盘→次日收盘」正是那种算得出来、拿不到的数字。
    # 按该票的下一个/下下个交易行算；停牌跨越的那一段不计入，也不假装知道它中间
    # 经历了什么（因此这里统计的是「能买到且两天后能卖」的那些样本）。
    m["fwd_2d"] = g["close"].shift(-2) / g["open"].shift(-1) - 1

    per_day = m.groupby("trade_date")[["sealed", "broken"]].sum()
    per_day.columns = ["up_count", "brk_count"]
    per_day["seal_rate"] = (per_day["up_count"]
                            / (per_day["up_count"] + per_day["brk_count"]).replace(0, np.nan))
    # 这才是游资的前向：当天封住的票，次日开盘买、第三日收盘卖。
    per_day["fwd_seal2d"] = (m[m["sealed"]].groupby("trade_date")["fwd_2d"].mean())
    per_day["n_sealed_fwd"] = (m[m["sealed"]].groupby("trade_date")["fwd_2d"].count())
    per_day = per_day.sort_index()

    out = pd.DataFrame(index=per_day.index)
    out["up_count"] = per_day["up_count"].astype(float)
    out["brk_count"] = per_day["brk_count"].astype(float)
    out["seal_rate"] = per_day["seal_rate"]
    out["fwd_seal2d"] = per_day["fwd_seal2d"]
    out["n_sealed_fwd"] = per_day["n_sealed_fwd"].fillna(0).astype(int)

    for name in ("seal_rate", "up_count"):
        out[f"{name}_pct"] = _rolling_pct(out[name])
        out[f"{name}_tercile"] = out[f"{name}_pct"].map(_tercile)
    out["bucket"] = (out["seal_rate_tercile"].fillna("?") + "|"
                     + out["up_count_tercile"].fillna("?"))
    return out


def episode_table(tape: pd.DataFrame, bucket: str, column: str = "fwd_seal2d",
                  since: str | None = None) -> pd.DataFrame:
    """每次连续段一行：起始日、段内天数、段的均值。

    段是这里的观测单位。相邻两个同状态日共享同一个前向窗口（第二天的收益窗口
    和第一天重叠一天），把它们当成两个独立观测会低估不确定性——这不是精度问题，
    是符号问题：`low|low` 近三年的日均是 −0.28%，段均 −0.09%，
    两者的区间都横跨零，而报告一个点估计会让它看起来像一个结论。
    """
    empty = pd.DataFrame({"start": [], "n": [], "mean": []})
    if column not in tape.columns or tape.empty:
        return empty
    frame = tape if since is None else tape[tape.index >= str(since)]
    mask = frame["bucket"] == bucket
    if not mask.any():
        return empty
    sub = frame.loc[mask].copy()
    sub["run"] = (mask != mask.shift(1, fill_value=False)).cumsum()[mask]
    sub["day"] = sub.index
    table = sub.groupby("run").agg(start=("day", "min"), n=("day", "size"),
                                   mean=(column, "mean"))
    return table.dropna(subset=["mean"]).reset_index(drop=True)


def episode_means(tape: pd.DataFrame, bucket: str,
                  column: str = "fwd_seal2d",
                  since: str | None = None) -> np.ndarray:
    """一个连续段算一次观测。连续的同一状态共享同一个前向窗口。

    ``since`` 限定窗口起点：一个横跨 2015 和 2024 的段不该被算成一次观测，
    那两段时间的同一档根本不是同一件事。
    """
    table = episode_table(tape, bucket, column, since)
    return table["mean"].to_numpy(dtype=float) if len(table) else np.array([])


def episode_ci(episode_values: np.ndarray) -> dict | None:
    """段级 bootstrap 区间。段数不足 10 就不给区间——给一个假的比不给更糟。"""
    values = np.asarray(episode_values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 10:
        return None
    from . import stats

    boot = stats.bootstrap_mean(values)
    return {"n": boot["n"], "mean": round(float(boot["mean"]), 4),
            "ci_low": round(float(boot["ci_low"]), 4),
            "ci_high": round(float(boot["ci_high"]), 4)}


def _dist(values: pd.Series) -> dict:
    v = pd.to_numeric(values, errors="coerce").dropna()
    if v.empty:
        return {"n": 0}
    return {"n": len(v), "mean": round(float(v.mean()), 4),
            "p25": round(float(v.quantile(0.25)), 4),
            "p50": round(float(v.quantile(0.50)), 4),
            "p75": round(float(v.quantile(0.75)), 4),
            "win": round(float((v > 0).mean()), 3)}


def dominant_era(by_era: list[dict]) -> dict | None:
    """全样本均值里贡献最大的那一段，以及它的占比。

    算术上就是 ``n_era * mean_era / (n_total * mean_total)``。之所以要算它，是因为
    「全历史 +0.9%」这句话读起来像一个关于现在的判断，而实际上那 0.9% 里可能
    四分之三来自一段已经结束的行情。把来源指出来，比把均值藏起来有用。

    均值接近 0 时占比会失真（分母趋零），那种情况返回 ``None``——一个不稳定的
    比例不如没有比例。
    """
    if not by_era:
        return None
    total_n = sum(e["n"] for e in by_era)
    if not total_n:
        return None
    total = sum(e["n"] * e["mean"] for e in by_era)
    # ``share`` is the signed decomposition of the full-sample mean: how much of
    # it came from this era. That ratio only means anything when the parts add up
    # rather than cancel, so compare the total against the *absolute* mass. The
    # old guard tested ``abs(total) < 1e-9``, which caught exact cancellation and
    # nothing else — two eras of ±1% with a 0.02% residue produced a "contribution"
    # of 25, and that number went straight out as `era_dominant`.
    weight = sum(abs(e["n"] * e["mean"]) for e in by_era)
    if weight < 1e-9 or abs(total) < CANCEL_RATIO * weight:
        return None
    best = max(by_era, key=lambda e: abs(e["n"] * e["mean"]))
    return {"era": best["era"], "n": best["n"], "mean": best["mean"],
            "share": round(best["n"] * best["mean"] / total, 3),
            "total_mean": round(total / total_n, 4)}


# Below this, the eras cancel and a signed share is a ratio of noise.
CANCEL_RATIO = settings.num("research.cancel_ratio")

MIN_EPISODES = settings.integer("research.tape_min_episodes")

# 「近三年」和 state.py 的 fwd_20d_3y 同一口径：一段能被称为「现在」的长度。
RECENT_SESSIONS = settings.integer("research.recent_sessions")

# 分段看，不是分段拟合。2015 是杠杆牛，2021 是核心资产见顶，2024 之后是新规与量化
# 主导——把这些混成一个均值，得到的精度是假的。``None`` 作上界表示「一直到最新」，
# 所以这张表不需要每年改。
ERAS = ((2010, 2014), (2015, 2019), (2020, 2023), (2024, None))


def era_of(day: str) -> str:
    year = int(str(day)[:4])
    for lo, hi in ERAS:
        if year >= lo and (hi is None or year <= hi):
            return f"{lo}-{str(hi)[-2:]}" if hi else f"{lo}-今"
    return "?"


def era_means(tape: pd.DataFrame, bucket: str,
              column: str = "fwd_seal2d") -> list[dict]:
    """同一个桶按年代分开的均值。最近的一段排在最前。

    顺序是刻意的：最先读到的那个数就是当下适用的那一个。全样本均值排在最前，
    读的人会先看到 +0.9%，而那个数字是 2015-2019 撑起来的。

    每一项同时给日均与**段均 + 区间**。两者不是一回事：日均把相邻的同状态日
    当成独立观测，段均不会。`low|low` 近三年日均 −0.28%、段均 −0.09%，
    两个区间都横跨零——只报日均，那个 −0.28% 会被读成一个方向。
    段数不足 10 时 ``ci`` 为 ``None``。
    """
    if column not in tape.columns or tape.empty:
        return []
    rows = tape[tape["bucket"] == bucket]
    if rows.empty:
        return []
    table = episode_table(tape, bucket, column)
    table = table.assign(era=[era_of(d) for d in table["start"]]) if len(table) else table
    out = []
    labels = pd.Series([era_of(d) for d in rows.index], index=rows.index)
    for label in reversed(list(dict.fromkeys(labels.tolist()))):
        chunk = rows.loc[labels == label, column].dropna()
        if chunk.empty:
            continue
        episodes = (table.loc[table["era"] == label, "mean"].to_numpy(dtype=float)
                    if len(table) else np.array([]))
        out.append({"era": label, "n": len(chunk),
                    "mean": round(float(chunk.mean()), 4),
                    "win": round(float((chunk > 0).mean()), 3),
                    "episodes": len(episodes),
                    "episode_ci": episode_ci(episodes)})
    return out


def _side(ci: dict | None) -> str:
    """一个区间落在零的哪一侧。事实，不是判断。"""
    if not ci:
        return "unknown"
    if ci["ci_low"] <= 0 <= ci["ci_high"]:
        return "crosses"
    return "positive" if ci["ci_low"] > 0 else "negative"


def audit(tape: pd.DataFrame, bucket: str, alternatives: dict[str, pd.DataFrame] | None = None,
          *, column: str = "fwd_seal2d") -> dict:
    """一个桶的结论能不能被读成一个方向——三件事一起查，缺一件就不行。

    P37 修了区间，P38 修了符号，P39 修了口径，三次都是同一件事：一个算术上正确的
    数字被当成了它撑不住的那种断言。这个函数把那三次的修法合成一次运行：

    1. **区间**：段级 bootstrap（段是观测单位，相邻同状态日共享前向窗口）。
       ``low|low`` 近三年的日均是 −0.28%、段均 −0.09%，两个区间都横跨零。
    2. **换定义**：``alternatives`` 是同一批日子用不同成分/标签口径重建的序列。
       2024-今 在四个口径下都横跨零，2015-19 在四个口径下都远离零。
    3. **桶标签**：换了定义之后有多少天还叫同一个名字。实测 74.8%——
       **四分之三的标签会换，而结论不动，这才叫稳健**；只在一个口径下成立的不是。

    返回值里没有「可信 / 不可信」。给的是一侧的计数（``sides``）、每个口径的区间、
    以及标签一致率。要不要按这些数下结论，是读的人的事——和 ``claim.evaluate``
    不给 verdict 是同一条分工。

    ``alternatives`` 为空时只查区间，``sides`` 只有基准一票。
    """
    if tape is None or tape.empty or column not in tape.columns:
        return {"ok": False, "error": "no tape"}

    def measure(frame: pd.DataFrame) -> dict:
        episodes = episode_means(frame, bucket, column)
        ci = episode_ci(episodes)
        rows = frame[frame["bucket"] == bucket]
        return {"days": len(rows.dropna(subset=[column])),
                "episodes": len(episodes),
                "episode": ci,
                "side": _side(ci),
                "episode_mean": (round(float(episodes.mean()), 4)
                                 if len(episodes) else None)}

    base = measure(tape)
    out: dict = {"ok": True, "bucket": bucket, "column": column,
                 "base": base, "alternatives": {}, "label_agreement": {}}

    for name, frame in (alternatives or {}).items():
        got = measure(frame)
        out["alternatives"][name] = got
        joint = pd.DataFrame({"a": tape["bucket"], "b": frame["bucket"]}).dropna()
        joint = joint[(joint.a.str[0] != "?") & (joint.b.str[0] != "?")]
        if len(joint):
            out["label_agreement"][name] = {
                "days": len(joint),
                "whole": round(float((joint.a == joint.b).mean()), 3),
                "seal": round(float((joint.a.str[0] == joint.b.str[0]).mean()), 3),
                "count": round(float((joint.a.str[-1] == joint.b.str[-1]).mean()), 3),
            }

    sides = {"positive": 0, "negative": 0, "crosses": 0, "unknown": 0}
    sides[base["side"]] = 1
    for got in out["alternatives"].values():
        sides[got["side"]] += 1
    out["sides"] = sides
    out["definitions"] = 1 + len(out["alternatives"])
    out["note"] = ("能被读成一个方向，要求 sides 里只有一个非零且不是 crosses。"
                   "只在一个口径下成立、或区间横跨零的，都只能读成「不知道」。")
    return out


# 校准结果的位置：由 scripts/tape_calibration.py 写入。存在就读，不存在就明说未校准。
# 不把数字写死在代码里，是因为 P36 就写过一次「相关 0.990、中位比值 1.09」然后在
# 实测中变成了 0.984 / 1.145——一个没人复查的常数比没有常数更糟。
#
# 放在 reports/ 而不是 data/cache/：后者是 gitignore 的，一份能被删掉的测量等于没测过
# （P31 的试验账本踩过同一个坑）。这是一份带日期的测量，不是缓存，所以它入库。
CALIBRATION_PATH = Path(__file__).resolve().parents[2] / "reports" / "tape_calibration.json"


def load_calibration(path=None) -> dict:
    """读校准结果。读不到不是错误，只是「未校准」，由调用方决定要不要显示。"""
    target = Path(path) if path else CALIBRATION_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "note": "未校准：跑 scripts/tape_calibration.py"}
    if not isinstance(data, dict) or not data.get("ok"):
        return {"ok": False, "note": "校准文件不可用：重跑 scripts/tape_calibration.py"}
    return data


def _tercile_series(pct: pd.Series) -> pd.Series:
    return pct.map(_tercile)


def calibrate(derived: pd.DataFrame, vendor: pd.DataFrame, *,
              window: int = 120, min_periods: int | None = None) -> dict:
    """推导序列与上游序列在重叠期里，分桶是不是同一件事。

    这是「4,081 天的分位能不能代表上游那 243 天所描述的东西」的唯一直接答案。
    相关 0.99 只能说明两条线一起动，不能说明**同一天会被分进同一档**——而卡片
    用的正是档。

    **窗口必须一样。** 第一次做这个比较时，推导用 252 日窗口、上游只有 120–243 天，
    同桶率 60.5%；把两边都压到同一个 120 日窗口之后是 85.5%。差的那 25 个百分点
    是窗口长度，不是序列差异——两个序列被拿去和不同的参照期比较，本来就不该期待
    它们给出同一档。所以 ``window`` 在这里是参数而不是常量，默认值取上游撑得住
    的那个长度。

    返回值里没有「可信 / 不可信」。这里只报一致率、远桶率、尾档与中档分别的一致率、
    水平偏移和秩相关，够不够用由读的人判断。
    """
    if derived is None or derived.empty or vendor is None or vendor.empty:
        return {"ok": False, "error": "empty series"}
    mp = window if min_periods is None else min_periods

    def pct(series: pd.Series) -> pd.Series:
        return series.rolling(window, min_periods=mp).apply(
            lambda w: float((w.iloc[:-1] <= w.iloc[-1]).mean()) if len(w) > 1 else np.nan,
            raw=False)

    out: dict = {"ok": True, "window": window, "min_periods": mp,
                 "full_window": bool(mp == window), "effective_window": mp}
    for key, d_col, v_col in (("seal", "seal_rate", "seal_rate"),
                              ("up", "up_count", "up_count")):
        if d_col not in derived.columns or v_col not in vendor.columns:
            continue
        d_series = pd.to_numeric(derived[d_col], errors="coerce")
        v_series = pd.to_numeric(vendor[v_col], errors="coerce")
        d_pct, v_pct = pct(d_series), pct(v_series)
        both = pd.DataFrame({"d": d_pct, "v": v_pct,
                             "dl": d_series, "vl": v_series}).dropna(subset=["d", "v"])
        if both.empty:
            out[key] = {"n": 0, "note": "重叠期内两边都算不出分位"}
            continue

        dter = _tercile_series(both["d"])
        vter = _tercile_series(both["v"])
        same = dter == vter
        far = ((dter == "low") & (vter == "high")) | ((dter == "high") & (vter == "low"))
        tails = dter.isin(["low", "high"])
        mids = dter == "mid"
        levels = pd.DataFrame({"dl": both["dl"], "vl": both["vl"]}).dropna()

        out[key] = {
            "n": len(both),
            "start": str(both.index.min()),
            "end": str(both.index.max()),
            "agree": round(float(same.mean()), 4),
            "far": round(float(far.mean()), 4),
            # 尾档与中档分开报：边界附近的档最容易翻，把两者混成一个数会掩盖这一点。
            "tail_n": int(tails.sum()),
            "tail_agree": round(float(same[tails].mean()), 4) if tails.any() else None,
            "mid_n": int(mids.sum()),
            "mid_agree": round(float(same[mids].mean()), 4) if mids.any() else None,
            "contingency": {f"{a}|{b}": int(c) for (a, b), c in
                            pd.crosstab(dter, vter).stack().items() if c},
            "level_shift": (round(float((levels.dl - levels.vl).mean()), 4)
                            if len(levels) else None),
            "median_ratio": (round(float((levels.dl / levels.vl).median()), 4)
                             if len(levels) else None),
            "rank_corr": (round(float(levels.dl.rank().corr(levels.vl.rank())), 4)
                          if len(levels) > 2 else None),
        }
    out["note"] = ("窗口长度必须与上游可用历史一致；不同长度下的同桶率不能拿来比。"
                   "full_window=False 时，effective_window/min_periods 是最短实际历史，"
                   "滚动窗口上界仍是 window。水平可平移，分位不可——"
                   "中位比值远离 1 而秩相关高，就是这种情形。")
    return out


def block(tape: pd.DataFrame, day: str | None = None,
          calibration: dict | None = None) -> dict:
    """今天的涨停结构在什么位置，以及这个位置之后通常怎样。

    ``usable`` 是这里唯一重要的字段：段数不足就明说，而不是把 12 段当成分布。
    """
    if tape is None or tape.empty:
        return {"ok": False, "error": "no tape history"}
    day = day or str(tape.index.max())
    if day not in tape.index:
        return {"ok": False, "error": f"{day} 不在涨停结构序列里"}
    row = tape.loc[day]
    bucket = str(row["bucket"])
    if "?" in bucket:
        return {"ok": False, "day": day, "error": "预热期：任一分位历史不足"}

    # All distributions in a historical answer share the same information cut.
    tape = tape.loc[:day].copy()
    # fwd_seal2d needs the next two sessions; labels still open at D are not
    # observations, even when the caller passed a frame precomputed with future.
    if len(tape) < 2:
        tape["fwd_seal2d"] = np.nan
    else:
        tape.iloc[-2:, tape.columns.get_loc("fwd_seal2d")] = np.nan
    rows = tape[tape["bucket"] == bucket]
    dist = _dist(rows["fwd_seal2d"])
    episodes = episode_means(tape, bucket)
    base = _dist(tape["fwd_seal2d"])

    # 近三年另算一份：4,081 天给的是精度，不是时效。全样本均值 +0.9% 里，
    # 2015-2019 一段是 +2.5%，而 2024 之后是负的——把这两件事混成一个数，
    # 算术没错，读出来是错的。
    recent_cut = tape.index[max(0, len(tape) - RECENT_SESSIONS)]
    recent_rows = rows[rows.index >= recent_cut]
    recent_dist = _dist(recent_rows["fwd_seal2d"])
    recent_episodes = episode_means(tape, bucket, since=recent_cut)
    recent_base = _dist(tape.loc[tape.index >= recent_cut, "fwd_seal2d"])
    out = {
        "ok": True,
        "as_of": day,
        "bucket": bucket,
        "seal_rate": round(float(row["seal_rate"]), 4) if pd.notna(row["seal_rate"]) else None,
        "seal_pct": round(float(row["seal_rate_pct"]), 4) if pd.notna(row["seal_rate_pct"]) else None,
        "up_count": int(row["up_count"]) if pd.notna(row["up_count"]) else None,
        "up_pct": round(float(row["up_count_pct"]), 4) if pd.notna(row["up_count_pct"]) else None,
        "n": int(dist["n"]),
        "episodes": len(episodes),
        "fwd_seal2d": dist,
        "episode_ci": episode_ci(episodes),
        "baseline": {k: base.get(k) for k in ("mean", "p50", "win", "n")},
        "usable": bool(dist["n"] >= 30 and len(episodes) >= MIN_EPISODES),
        "recent": {
            "sessions": int(len(tape) - max(0, len(tape) - RECENT_SESSIONS)),
            "since": str(recent_cut),
            "fwd": recent_dist,
            "episodes": len(recent_episodes),
            "episode_ci": episode_ci(recent_episodes),
            "baseline": {k: recent_base.get(k) for k in ("mean", "win")},
            "usable": bool(recent_dist["n"] >= 30
                           and len(recent_episodes) >= MIN_EPISODES),
        },
        "by_era": era_means(tape, bucket),
        "era_dominant": dominant_era(era_means(tape, bucket)),
        "sessions": len(tape),
        "forward": "次日开盘买当天封住的票、第三日收盘卖，等权平均（T+1 持有口径，未含跌停顺延）",
        "execution_caveat": ("此前向分布未逐笔应用跌停无法卖出的顺延规则，不代表保证可成交；"
                             "可执行回测须使用 spec 的统一 sell gate"),
        "provenance": ("炸板由日线高点触及涨停价且收盘未封住推导（stk_limit 2010 起）；"
                       "上游池子只留一年，且从不含北交所（30% 板收录率恒为 0），"
                       "5% 板的收录率则逐年上升——水平不可互换，只用分位"),
        "calibration": (calibration if (calibration or {}).get("ok")
                        else {"ok": False,
                              "note": "未校准：跑 scripts/tape_calibration.py 生成，"
                                      "它比较推导序列与上游序列在重叠期是否落进同一档"}),
    }
    if not out["usable"]:
        out["note"] = (f"n={dist['n']}、段数={len(episodes)}；"
                       f"需要 n≥30 且段数≥{MIN_EPISODES} 才当一个分布用")
    return out


def cached_tape(store, *, start: str = "20100101", cache_dir=None,
                daily: pd.DataFrame | None = None,
                limits: pd.DataFrame | None = None,
                last: str | None = None, as_of: str | None = None) -> pd.DataFrame:
    """``build_tape_state`` 带一个按最后交易日 + schema 失效的缓存。

    和 state.py 同一个教训：缓存键里没有 schema，加一列就不会失效，
    然后某处去取那一列的时候才炸。

    ``last`` 让已经知道最后交易日的调用方跳过那次只为查缓存而做的全量读。
    """
    root = Path(__file__).resolve().parents[2]
    directory = Path(cache_dir) if cache_dir else root / "data" / "cache"
    directory.mkdir(parents=True, exist_ok=True)
    parquet = directory / f"tape_{start}.parquet"
    meta_path = directory / f"tape_{start}.meta.json"

    def _hit(want: str | None, source_digest: str | None = None) -> pd.DataFrame | None:
        if want is None or not (parquet.exists() and meta_path.exists()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if meta.get("last_date") != str(want) or meta.get("schema") != SCHEMA:
            return None
        if source_digest is not None and meta.get("source_digest") != source_digest:
            return None
        cached = pd.read_parquet(parquet)
        cached.index = cached.index.astype(str)
        return cached.loc[:str(as_of)] if as_of else cached

    hit = _hit(last) if as_of is None else None
    if hit is not None:
        return hit

    end = str(as_of) if as_of else "20991231"
    frame = daily if daily is not None else store.read("daily", start, end)
    lim = limits if limits is not None else store.read("stk_limit", start, end)
    if frame is None or frame.empty or lim is None or lim.empty:
        return pd.DataFrame()
    frame = frame.copy()
    lim = lim.copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    lim["trade_date"] = lim["trade_date"].astype(str)
    frame = frame[(frame["trade_date"] >= str(start)) & (frame["trade_date"] <= end)]
    lim = lim[(lim["trade_date"] >= str(start)) & (lim["trade_date"] <= end)]
    if frame.empty or lim.empty:
        return pd.DataFrame()
    last = str(frame["trade_date"].max())
    source_digest = _content_digest(store, frame, lim)
    hit = _hit(last, source_digest)
    if hit is not None:
        return hit

    tape = build_tape_state(frame, lim)
    if tape.empty:
        return tape
    out = tape.copy()
    out.index = out.index.astype(str)
    try:
        out.to_parquet(parquet)
        meta_path.write_text(json.dumps({"last_date": last, "start": start,
                                         "schema": SCHEMA, "source_digest": source_digest,
                                         "sessions": len(out)}), encoding="utf-8")
    except OSError:
        pass  # 写不了缓存不能让答案失败
    return out
