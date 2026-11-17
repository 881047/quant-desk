"""Cross-sectional factor reads: where an alpha could come from, measured.

This module answers the question the falsification ledger cannot: not "was this
setup rejected?" but "which cross-sectional signals have any measurable
predictive power at all, after costs, after neutralising size and industry, and
after correcting for the fact that we looked at several of them?"

It is a **read**, not a signal. There is no action, verdict, score, ranking,
target price, or buy/sell field anywhere in the output. A factor with a positive
RankIC is a number someone may use to write a pre-registered hypothesis; it is
not permission to trade, and it does not enter ``arena.edge``.

Three design rules, each the fix for a mistake that is easy to make here:

1. **No look-ahead in the forward return.** ``alphalens`` builds forward
   returns with ``pct_change(period).shift(-period)``, which starts at T's own
   price. A factor computed from T's close then appears to trade at that same
   close — impossible under T+1. Here a factor known at T's close is matched to
   a return from **T+1's open** to a later close. See ``_forward_returns``.

2. **Excess, not raw return.** The desk measures skill against the equal-weight
   market (``arena.py``, ``spec.py``). Scoring a factor against raw returns
   mostly measures beta. Each day's cross-sectional forward return is demeaned
   before the IC is computed.

3. **Every test is reported, and corrected.** Looking at N factors x H horizons
   and quoting the best p-value is the P22 mistake. All p-values go through
   Benjamini-Hochberg; the output carries ``n_tests`` even when a single factor
   was requested, because the reader still has to know how many were examined.

Point-in-time: factors use only T's close and earlier. ``daily_basic`` valuation
fields are the vendor's same-day snapshot (not a restated history); fundamentals
from ``fina_indicator`` are deliberately NOT used here — its PIT key is
``ann_date`` and mixing it into a close-based panel needs its own audit. The
absence is reported, not silently filled.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from service.research.registry import bh_adjust

#: Minimum names in one cross-section before that day is allowed to contribute.
#: The factor-research rule of thumb is 5; 30 is used here because a 5-name
#: Spearman correlation is almost entirely determined by one outlier.
MIN_CROSS_SECTION = 30

#: Minimum number of usable days before IC statistics are reported at all.
MIN_IC_DAYS = 20

#: Horizons measured in trading sessions, entry at T+1 open. This is the
#: DEFAULT family. Its size is part of the multiple-testing denominator, so it
#: must stay fixed: every delivered report (36 tests, 0 survivors) is keyed to
#: it. A different horizon set is a DIFFERENT experiment family, not a richer
#: view of the same one.
HORIZONS = (1, 3, 5, 10)

#: Explicit dense family for "1..5 days incl. 2 and 4". Requested via
#: ``horizons="dense"``; never silently swapped in as the default.
DENSE_HORIZONS = (1, 2, 3, 4, 5)
MAX_HORIZON = 20
MAX_FAMILY_SIZE = 8


def parse_horizons(text: str | tuple | list | None) -> tuple[int, ...]:
    """Resolve a caller-supplied horizon family, or return the default.

    ``default`` -> (1,3,5,10); ``dense`` -> (1,2,3,4,5); ``1,2,3,4,5`` ->
    the explicit list. Every extra horizon is an extra hypothesis test, so an
    unknown or malformed value is rejected instead of being guessed.
    """
    if text is None or text == "" or text == "default":
        return HORIZONS
    if text == "dense" or text == "1-5":
        return DENSE_HORIZONS
    if isinstance(text, (tuple, list)):
        raw = [int(v) for v in text]
    else:
        try:
            raw = [int(v.strip()) for v in str(text).split(",") if v.strip()]
        except ValueError as exc:
            raise ValueError("horizons 只接受 default / dense / 逗号分隔的整数，如 1,2,3,4,5") from exc
    if not raw:
        raise ValueError("horizons 不能为空")
    if any(h < 1 or h > MAX_HORIZON for h in raw):
        raise ValueError(f"每个持有期须在 1..{MAX_HORIZON} 个交易日")
    if len(set(raw)) != len(raw):
        raise ValueError("horizons 不能有重复期限")
    if len(raw) > MAX_FAMILY_SIZE:
        raise ValueError(f"一次最多 {MAX_FAMILY_SIZE} 个期限；更多期限等于更多检验")
    return tuple(sorted(raw))

#: Five buckets for the monotonicity read.
N_LAYERS = 5

#: The pre-registered factor set. Adding a factor is a research decision: it
#: increases the multiple-testing denominator. Keep the list fixed and do not
#: select a subset after seeing the results.
FACTOR_SPECS: tuple[dict[str, str], ...] = (
    {"key": "mom20", "label": "20日动量", "definition": "后复权 close / 20日前 close − 1",
     "source": "daily(close, pre_close)", "direction": "高值=涨得多"},
    {"key": "reversal_5", "label": "5日反转", "definition": "−(后复权 close / 5日前 close − 1)",
     "source": "daily(close, pre_close)", "direction": "高值=跌得多"},
    {"key": "vol20", "label": "20日波动", "definition": "20日日收益标准差（年化前）",
     "source": "daily(close, pre_close)", "direction": "高值=波动大"},
    {"key": "turnover20", "label": "20日换手", "definition": "20日 turnover_rate 均值",
     "source": "daily_basic(turnover_rate)", "direction": "高值=换手高"},
    {"key": "log_mcap", "label": "对数市值", "definition": "log(total_mv)",
     "source": "daily_basic(total_mv)", "direction": "高值=大市值"},
    {"key": "bp", "label": "账面市值比", "definition": "1 / pb，pb≤0 置缺失",
     "source": "daily_basic(pb)", "direction": "高值=便宜"},
    {"key": "ep", "label": "盈利收益率", "definition": "1 / pe_ttm，pe_ttm≤0 置缺失",
     "source": "daily_basic(pe_ttm)", "direction": "高值=便宜"},
    {"key": "net_mf_5", "label": "5日主力净流入比", "definition": "5日 net_mf_amount 合计 / 5日 amount 合计",
     "source": "moneyflow(net_mf_amount) + daily(amount)", "direction": "高值=净流入"},
    {"key": "amihud_20", "label": "20日非流动性", "definition": "mean(|日收益| / 成交额) × 1e8",
     "source": "daily(close, pre_close, amount)", "direction": "高值=流动性差"},
)


def _f(value: Any, ndigits: int = 6) -> float | None:
    """Finite floats only. A NaN must never be serialised as a measurement."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return round(out, ndigits) if np.isfinite(out) else None


def _wide(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    if frame is None or frame.empty or column not in frame.columns:
        return pd.DataFrame()
    return (frame.pivot(index="trade_date", columns="ts_code", values=column)
            .sort_index())


def _adjusted(close: pd.DataFrame, pre_close: pd.DataFrame) -> pd.DataFrame:
    """Chain close/pre_close into a split-adjusted level, PIT-safe.

    ``close / close.shift(20)`` is wrong across an ex-date: the dividend gap is
    read as a −10% return. The spec engine already does this (``spec.py``
    ``_adjusted_from_pre_close``); the factor panel must use the same basis or
    momentum and reversal are contaminated.
    """
    if close.empty or pre_close.empty:
        return pd.DataFrame()
    ratio = close / pre_close.where(pre_close > 0)
    ratio = ratio.replace([np.inf, -np.inf], np.nan)
    # ``close / pre_close`` is already a GROSS return (1 + r), because
    # ``pre_close`` is the adjusted previous close. Compounding it directly
    # gives a split-adjusted level, exactly as ``spec._adjusted_from_pre_close``
    # does. Adding 1 here would compound (2 + r) and turn a 20-day momentum
    # into a near-constant ~2**20 scale artifact.
    gross = ratio.where(np.isfinite(ratio) & (ratio > 0), 1.0)
    return gross.cumprod()


def _build_panel(store, start: str, end: str) -> dict[str, pd.DataFrame]:
    """Read the four datasets that have never entered an alpha estimate before.

    All are T0 daily partitions keyed by (ts_code, trade_date), so a plain
    range read is point-in-time correct at the daily bar level.
    """
    daily = store.read(
        "daily", start=start, end=end,
        columns=["ts_code", "trade_date", "open", "close", "pre_close", "amount"],
    )
    basic = store.read(
        "daily_basic", start=start, end=end,
        columns=["ts_code", "trade_date", "total_mv", "pb", "pe_ttm", "turnover_rate"],
    )
    try:
        flow = store.read(
            "moneyflow", start=start, end=end,
            columns=["ts_code", "trade_date", "net_mf_amount"],
        )
    except (KeyError, ValueError, FileNotFoundError):
        flow = pd.DataFrame()
    return {"daily": daily, "basic": basic, "flow": flow}


def _factors(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """One wide frame (day x symbol) per factor, computed from T-closed data."""
    daily, basic, flow = frames["daily"], frames["basic"], frames["flow"]
    close = _wide(daily, "close")
    pre = _wide(daily, "pre_close")
    amount = _wide(daily, "amount")
    if close.empty:
        return {}
    adj = _adjusted(close, pre)
    ret = close / pre.where(pre > 0) - 1.0
    ret = ret.replace([np.inf, -np.inf], np.nan)

    out: dict[str, pd.DataFrame] = {
        "mom20": adj / adj.shift(20) - 1.0,
        "reversal_5": -(adj / adj.shift(5) - 1.0),
        "vol20": ret.rolling(20, min_periods=15).std(),
    }

    if not basic.empty:
        turn = _wide(basic, "turnover_rate")
        mv = _wide(basic, "total_mv")
        pb = _wide(basic, "pb")
        pe = _wide(basic, "pe_ttm")
        out["turnover20"] = turn.rolling(20, min_periods=15).mean() if not turn.empty else pd.DataFrame()
        out["log_mcap"] = np.log(mv.where(mv > 0)) if not mv.empty else pd.DataFrame()
        out["bp"] = (1.0 / pb.where(pb > 0)) if not pb.empty else pd.DataFrame()
        out["ep"] = (1.0 / pe.where(pe > 0)) if not pe.empty else pd.DataFrame()
    else:
        for key in ("turnover20", "log_mcap", "bp", "ep"):
            out[key] = pd.DataFrame()

    if not flow.empty:
        mf = _wide(flow, "net_mf_amount")
        # Units differ (moneyflow is 万元, daily amount is 千元). The constant
        # factor cancels in a cross-sectional rank, which is all this factor is
        # used for. Provenance says so explicitly; do not read the level as a
        # percentage of turnover.
        num = mf.rolling(5, min_periods=4).sum()
        den = amount.rolling(5, min_periods=4).sum()
        out["net_mf_5"] = (num / den.where(den > 0)).replace([np.inf, -np.inf], np.nan)
    else:
        out["net_mf_5"] = pd.DataFrame()

    illiq = (ret.abs() / amount.where(amount > 0)).replace([np.inf, -np.inf], np.nan)
    out["amihud_20"] = illiq.rolling(20, min_periods=15).mean() * 1e8

    return {k: v.replace([np.inf, -np.inf], np.nan) for k, v in out.items()}


def _forward_returns(frames: dict[str, pd.DataFrame], horizons=HORIZONS) -> dict[int, pd.DataFrame]:
    """Equal-weight-excess forward return, **entry at T+1 open**.

    ``fwd_h[t] = close[t+h] / open[t+1] − 1``. The factor at t is known at t's
    close, so the first tradable price is t+1's open. Using close[t] as the
    entry (what a naive ``shift(-h)`` does) would hand the factor information
    it could not have traded on. Each row is then demeaned across names, so the
    IC measures stock selection rather than market direction.
    """
    daily = frames["daily"]
    close = _wide(daily, "close")
    open_ = _wide(daily, "open")
    if close.empty or open_.empty:
        return {}
    # Align on the union of dates/symbols; the entry is the NEXT session's open.
    entry = open_.shift(-1)
    out: dict[int, pd.DataFrame] = {}
    for h in horizons:
        # close[t+h] is h sessions after t; entry is t+1 open. For h=1 that is
        # buy at t+1 open, sell at t+1 close — one intraday session.
        exit_ = close.shift(-h)
        raw = (exit_ / entry.where(entry > 0) - 1.0).replace([np.inf, -np.inf], np.nan)
        out[h] = raw.sub(raw.mean(axis=1), axis=0)
    return out


def _neutralise(factor: pd.DataFrame, log_mcap: pd.DataFrame,
                industry: pd.Series | None) -> pd.DataFrame:
    """Cross-sectional residual after industry demeaning and log-size regression.

    This is the cheap, coefficient-free version of what ``docs/07`` §2.5 asked
    for. It is NOT a Barra model: no covariance matrix, no estimated factor
    returns, no residual risk. It answers only "does the IC survive after the
    two most obvious exposures are removed", which is the first question, not
    the last one.
    """
    values = factor.to_numpy(dtype="float64")
    f = np.where(np.isfinite(values), values, 0.0)
    present = np.isfinite(values)
    if industry is not None and not industry.empty:
        labels = industry.reindex(factor.columns).to_numpy()
        mapped = pd.notna(labels)
        codes = np.full(len(labels), -1, dtype="int64")
        if mapped.any():
            codes[mapped], uniques = pd.factorize(labels[mapped], sort=False)
            onehot = np.zeros((len(labels), len(uniques)), dtype="float64")
            onehot[np.flatnonzero(mapped), codes[mapped]] = 1.0
            counts = present @ onehot                   # days x industries
            sums = f @ onehot
            means = np.divide(sums, counts, out=np.zeros_like(sums), where=counts > 0)
            # Unmapped names keep their raw value: giving them a one-member
            # "industry" would demean them to exactly zero.
            demean = np.where(mapped[None, :], means @ onehot.T, 0.0)
            f = f - demean
        # A name with no finite factor value must stay missing, not become the
        # negative of its industry mean.
        f = np.where(present, f, np.nan)
    if log_mcap is not None and not log_mcap.empty:
        x = log_mcap.reindex(index=factor.index, columns=factor.columns).to_numpy(dtype="float64")
        finite_x = np.isfinite(x)
        row_count = finite_x.sum(axis=1, keepdims=True)
        row_mean = np.divide(np.where(finite_x, x, 0.0).sum(axis=1, keepdims=True),
                             row_count, out=np.zeros_like(x), where=row_count > 0)
        xc = np.where(finite_x, x - row_mean, 0.0)
        valid = np.isfinite(x) & np.isfinite(f)
        fv = np.where(valid, f, 0.0)
        num = (fv * xc).sum(axis=1)
        den = (xc * xc * valid).sum(axis=1)
        beta = np.divide(num, den, out=np.zeros_like(num), where=den > 0)
        f = f - beta[:, None] * xc
        f = np.where(present, f, np.nan)
    return pd.DataFrame(f, index=factor.index, columns=factor.columns)


def _row_ranks(values: np.ndarray) -> np.ndarray:
    """Average ranks per row, ignoring NaN. Vectorised (no scipy, no loops)."""
    out = np.full(values.shape, np.nan, dtype="float64")
    for i in range(values.shape[0]):
        row = values[i]
        mask = np.isfinite(row)
        n = int(mask.sum())
        if n < 2:
            continue
        vals = row[mask]
        order = np.argsort(vals, kind="mergesort")
        sorted_vals = vals[order]
        fresh = np.r_[True, sorted_vals[1:] != sorted_vals[:-1]]
        starts = np.flatnonzero(fresh)
        ends = np.r_[starts[1:], n]
        group_id = np.cumsum(fresh) - 1
        # 1-based average rank of the tied block [start, end).
        avg = (starts[group_id] + ends[group_id] + 1) / 2.0
        ranks = np.empty(n, dtype="float64")
        ranks[order] = avg
        out[i, mask] = ranks
    return out


def _ic_series(factor: pd.DataFrame, fwd: pd.DataFrame,
               min_names: int = MIN_CROSS_SECTION) -> pd.Series:
    """Per-day Spearman IC between factor ranks and forward-excess ranks.

    Pearson on average ranks == Spearman. Working on raw numpy arrays (rather
    than pandas ``where`` on a 5,500-column frame) is ~50x faster here; a full
    9-factor x 4-horizon panel otherwise spends most of its life in pandas'
    block manager.
    """
    if factor.empty or fwd.empty:
        return pd.Series(dtype="float64")
    idx = factor.index.intersection(fwd.index)
    cols = factor.columns.intersection(fwd.columns)
    if len(idx) == 0 or len(cols) == 0:
        return pd.Series(dtype="float64")
    f = factor.loc[idx, cols].to_numpy(dtype="float64")
    y = fwd.loc[idx, cols].to_numpy(dtype="float64")
    valid = np.isfinite(f) & np.isfinite(y)
    count = valid.sum(axis=1)
    f = np.where(valid, f, np.nan)
    y = np.where(valid, y, np.nan)
    fr, yr = _row_ranks(f), _row_ranks(y)
    fv = np.where(valid, fr, 0.0)
    yv = np.where(valid, yr, 0.0)
    n = np.maximum(count, 1).astype("float64")[:, None]
    fc = np.where(valid, fr - fv.sum(axis=1, keepdims=True) / n, 0.0)
    yc = np.where(valid, yr - yv.sum(axis=1, keepdims=True) / n, 0.0)
    num = (fc * yc).sum(axis=1)
    den = np.sqrt((fc * fc).sum(axis=1) * (yc * yc).sum(axis=1))
    ic = np.divide(num, den, out=np.full_like(num, np.nan), where=den > 0)
    ic[count < min_names] = np.nan
    return pd.Series(ic, index=idx, dtype="float64").dropna()


def _layer_returns(factor: pd.DataFrame, fwd: pd.DataFrame,
                   n_layers: int = N_LAYERS) -> dict[str, Any]:
    """Equal-weight forward excess by factor quintile, plus monotonicity."""
    empty = {"layers": [], "monotonicity": None, "long_short": None, "n_days": 0}
    if factor.empty or fwd.empty:
        return empty
    idx = factor.index.intersection(fwd.index)
    cols = factor.columns.intersection(fwd.columns)
    f, y = factor.loc[idx, cols], fwd.loc[idx, cols]
    means = {i: [] for i in range(n_layers)}
    coverage: list[int] = []
    for day in idx:
        row, target = f.loc[day], y.loc[day]
        pair = pd.concat([row, target], axis=1).dropna()
        if len(pair) < MIN_CROSS_SECTION:
            continue
        try:
            bucket = pd.qcut(pair.iloc[:, 0].rank(method="first"), n_layers,
                             labels=False, duplicates="drop")
        except ValueError:
            continue
        coverage.append(len(pair))
        for i in range(n_layers):
            vals = pair.iloc[:, 1][bucket == i]
            means[i].append(float(vals.mean()) if len(vals) else np.nan)
    layer_means = []
    for i in range(n_layers):
        arr = np.asarray(means[i], dtype="float64") if means[i] else np.array([])
        finite = arr[np.isfinite(arr)]
        layer_means.append(_f(finite.mean()) if finite.size else None)
    valid = [(i, v) for i, v in enumerate(layer_means) if v is not None]
    if len(valid) < 3:
        return {**empty, "layers": layer_means, "n_days": len(coverage)}
    xs = pd.Series([i for i, _ in valid], dtype="float64")
    ys = pd.Series([v for _, v in valid], dtype="float64")
    # Spearman == Pearson on ranks. pandas' method="spearman" imports scipy,
    # which is not a dependency here, so do the rank transform explicitly.
    mono = _f(xs.rank().corr(ys.rank())) if len(valid) >= 3 else None
    return {
        "layers": layer_means,
        "monotonicity": mono,
        "long_short": (_f(valid[-1][1] - valid[0][1])),
        "n_days": len(coverage),
        "median_names_per_day": int(np.median(coverage)) if coverage else None,
    }


def _normal_two_sided_p(t: float) -> float:
    """Two-sided p under N(0,1), via erfc (no scipy in this project).

    With >= 20 daily IC observations the normal approximation is adequate for a
    *descriptive* read. It is not a Student-t tail: at small n it understates
    the p-value, which is why the caveats spell out that this is a reference
    line, not a significance verdict.
    """
    from math import erfc, sqrt
    return float(erfc(abs(float(t)) / sqrt(2.0)))


def _hac_t(ic: pd.Series, lags: int) -> float | None:
    """t-stat of the IC mean with a Newey-West (Bartlett) standard error.

    An h-session forward return overlaps its neighbours on h-1 days, so the IC
    series is strongly autocorrelated even when the underlying signal is noise.
    The iid t-stat then grows roughly with sqrt(h): measured on this panel,
    h=10 produced |t| ~ 6.5 for factors whose h=1 t was below 1. Reporting that
    as "significant" is exactly the false-positive machinery this desk exists
    to prevent. Bartlett weights with `lags = h` are the conservative fix.
    """
    x = np.asarray(ic, dtype="float64")
    n = len(x)
    if n < MIN_IC_DAYS or lags < 0:
        return None
    centred = x - x.mean()
    gamma0 = float(centred @ centred) / n
    variance = gamma0
    for lag in range(1, min(lags, n - 1) + 1):
        gamma = float(centred[lag:] @ centred[:-lag]) / n
        weight = 1.0 - lag / (min(lags, n - 1) + 1.0)
        variance += 2.0 * weight * gamma
    # A negative HAC variance can arise from a short, strongly mean-reverting
    # series. Fail closed rather than taking sqrt of a clipped number.
    if not np.isfinite(variance) or variance <= 0:
        return None
    se = np.sqrt(variance / n)
    if not np.isfinite(se) or se <= 0:
        return None
    return float(x.mean() / se)


def _ic_stats(ic: pd.Series, horizon: int = 1) -> dict[str, Any]:
    """IC mean / ICIR / t / positive rate, with an autocorrelation-safe t."""
    if ic is None or len(ic) < MIN_IC_DAYS:
        return {
            "n_days": len(ic) if ic is not None else 0,
            "status": "insufficient",
            "ic_mean": None, "icir": None, "t_stat": None, "positive_rate": None,
            "p_value": None, "t_stat_iid": None, "hac_lags": None,
            "note": (f"可用交易日 {0 if ic is None else len(ic)} < {MIN_IC_DAYS}；"
                     "不报 IC，避免把几天噪声写成预测力"),
        }
    mean = float(ic.mean())
    std = float(ic.std(ddof=1))
    icir = mean / std if std > 0 else None
    t_iid = icir * np.sqrt(len(ic)) if icir is not None else None
    # Overlapping forward returns create up to horizon-1 days of dependence.
    hac_lags = max(0, int(horizon))
    t_hac = _hac_t(ic, hac_lags)
    p = _normal_two_sided_p(t_hac) if t_hac is not None else None
    return {
        "n_days": len(ic),
        "status": "measured",
        "ic_mean": _f(mean),
        "icir": _f(icir),
        "t_stat": _f(t_hac, 4),
        "t_stat_iid": _f(t_iid, 4),
        "hac_lags": hac_lags,
        "positive_rate": _f(float((ic > 0).mean()), 4),
        "p_value": _f(p),
        # Harvey-Liu: with multiple testing, |t| > 3.0 is the working bar.
        "passes_t3": bool(t_hac is not None and abs(t_hac) > 3.0),
        "note": ("t_stat 是 Newey-West(Bartlett, lags=h) 校正后的值；t_stat_iid "
                 "仅作对照，重叠持有期下它会系统性偏大，不能用于判决。"
                 "读数不是结论，|t|>3 只是公开文献里的多重检验参考线"),
    }


def _time_holdout(ic: pd.Series) -> dict[str, Any]:
    """First-half vs second-half IC: a cheap, honest out-of-time stability read.

    This is NOT walk-forward and does NOT replace CPCV. The research referee
    already runs combinatorially purged CV with label-span purge + embargo,
    which is a stronger evidence class than a handful of rolling windows.
    What CPCV does not give a factor is a simple "did the second half of
    history agree with the first" check, and that is the failure mode a
    look-back-heavy factor panel is most exposed to. No re-fit happens here:
    the factor set is pre-registered and fixed, so splitting the IC series in
    time is a genuine, if coarse, holdout.
    """
    empty = {"status": "insufficient", "first_half": None, "second_half": None,
             "sign_agrees": None, "n_first": 0, "n_second": 0}
    if ic is None or len(ic) < MIN_IC_DAYS * 2:
        return {**empty, "n_first": 0 if ic is None else len(ic),
                "note": "IC 序列不足以分成两个 >= MIN_IC_DAYS 的半段"}
    half = len(ic) // 2
    first, second = ic.iloc[:half], ic.iloc[half:]
    a, b = float(first.mean()), float(second.mean())
    return {
        "status": "measured",
        "n_first": len(first), "n_second": len(second),
        "first_half": _f(a), "second_half": _f(b),
        "sign_agrees": bool(a * b > 0),
        "shrinkage": _f(b / a) if abs(a) > 1e-12 else None,
        "note": ("按时间前后各半切分 IC；sign_agrees=false 表示该因子的方向在后半段"
                 "翻了，不能当作稳定 alpha 候选。这是描述性读数，不参与任何判决。"
                 "CPCV 已在研究裁判层提供更强的样本外证据，此处不重复实现 walk-forward"),
    }


def panel(store, *, start: str, end: str, sw_table: pd.DataFrame | None = None,
          horizons=HORIZONS, factors: tuple[str, ...] | None = None,
          neutralise: bool = True, detail: str = "summary") -> dict:
    """Measure a fixed panel of cross-sectional factors over [start, end].

    Read-only. Never writes to the store, the hypothesis table, the trial
    registry, or the account. Selecting a factor from this output and trading
    it is a NEW experiment and must go through ``quant_hypothesis(add)`` so the
    search is counted in the Deflated-Sharpe denominator.
    """
    requested = list(factors) if factors else [s["key"] for s in FACTOR_SPECS]
    unknown = [k for k in requested if k not in {s["key"] for s in FACTOR_SPECS}]
    horizon_tuple = parse_horizons(horizons)
    is_default = horizon_tuple == HORIZONS
    meta = {
        "ok": True, "topic": "factors", "read_only": True,
        "window": {"start": str(start), "end": str(end)},
        "horizons": list(horizon_tuple),
        "family_scope": {
            "horizon_set": list(horizon_tuple),
            "is_default_family": is_default,
            "comparable_to_default_family": is_default,
            "note": (
                f"默认族 {list(HORIZONS)} = {len(FACTOR_SPECS)}×{len(HORIZONS)}"
                f" = {len(FACTOR_SPECS) * len(HORIZONS)} 次检验，是全部已交付报告的口径，保持不变。"
                if is_default else
                f"本族 {list(horizon_tuple)} = {len(FACTOR_SPECS)}×{len(horizon_tuple)}"
                f" = {len(FACTOR_SPECS) * len(horizon_tuple)} 次检验（按全因子面板计）；"
                f"默认族 {list(HORIZONS)} = {len(FACTOR_SPECS) * len(HORIZONS)} 次。"
                "两族 BH 分母不同，p/q 值不可跨族直接比较，也不能拿本族的幸存者"
                "去推翻默认族的历史结论。新增期限 = 新增检验，不是同一批读数的细化。"
            ),
        },
        "min_cross_section": MIN_CROSS_SECTION,
        "min_ic_days": MIN_IC_DAYS,
        "neutralised": bool(neutralise),
        "n_factors_in_panel": len(FACTOR_SPECS),
        "provenance": {
            "datasets": ["daily", "daily_basic", "moneyflow", "index_member(industry only)"],
            "entry": "T+1 open", "exit": "T+h close",
            "benchmark": "same-day equal-weight cross-sectional mean (excess)",
            "price_basis": "close/pre_close chain (split-adjusted)",
            "fundamentals_used": False,
            "fundamentals_note": ("fina_indicator 的 PIT 键是 ann_date，与收盘面板不同源；"
                                  "本次不纳入，缺口显式报出而不是用 0 填充"),
        },
    }
    if unknown:
        return {**meta, "ok": False, "status": "unknown_factor",
                "error": f"未知因子 {unknown}", "available": [s["key"] for s in FACTOR_SPECS]}
    if start > end:
        return {**meta, "ok": False, "status": "bad_window", "error": "start 晚于 end"}

    try:
        frames = _build_panel(store, start, end)
    except Exception as exc:  # noqa: BLE001 - a missing partition is an answer
        return {**meta, "ok": False, "status": "read_failed",
                "error": f"{type(exc).__name__}: {exc}"}
    if frames["daily"].empty:
        return {**meta, "ok": False, "status": "no_bars",
                "error": "窗口内没有日线；因子读数需要至少 60 个交易日"}

    days = sorted(str(d) for d in frames["daily"]["trade_date"].astype(str).unique())
    availability = {
        "daily": len(frames["daily"]),
        "daily_basic": len(frames["basic"]),
        "moneyflow": len(frames["flow"]),
        "sessions": len(days),
    }
    meta["availability"] = availability
    if len(days) < 60:
        return {**meta, "ok": False, "status": "short_history",
                "error": f"仅 {len(days)} 个交易日 < 60；动量/波动/非流动性窗口无法成形"}

    built = _factors(frames)
    fwd = _forward_returns(frames, horizons=horizon_tuple)
    industry = None
    if sw_table is not None and not sw_table.empty and "l1_name" in sw_table.columns:
        deduped = sw_table.drop_duplicates(subset=["ts_code"], keep="last")
        industry = pd.Series(deduped["l1_name"].to_numpy(),
                             index=deduped["ts_code"].astype(str).to_numpy())
    # 当前 index_member 分区每票只有一行、out_date 全空，不能复原历史行业迁移。
    # 保留这组结果作为敏感性分析，同时给出 raw IC；绝不把静态行业 demean
    # 标成严格 PIT。真正按 in_date/out_date 逐日连接后再切换 pit_safe=true。
    meta["neutralisation"] = {
        "enabled": bool(neutralise),
        "industry_enabled": industry is not None,
        "industry_basis": "current_membership_snapshot" if industry is not None else "not_applied",
        "industry_pit_safe": False if industry is not None else None,
        "raw_ic_available": True,
        "note": ("行业中性化使用当前行业快照，只表示当前分类下的敏感性分析；"
                 "行业迁移可能影响历史 demean、IC 与覆盖。严格 PIT 结论请看 raw IC，"
                 "或等待历史成员区间接入。" if industry is not None else "未应用行业中性化。"),
    }
    log_mcap = built.get("log_mcap", pd.DataFrame())

    rows: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    for spec in FACTOR_SPECS:
        key = spec["key"]
        if key not in requested:
            continue
        raw = built.get(key, pd.DataFrame())
        if raw.empty:
            rows.append({**spec, "status": "unavailable",
                         "note": "所需数据列在窗口内不可读；不是因子无效"})
            continue
        used = _neutralise(raw, log_mcap, industry) if neutralise else raw
        by_horizon: dict[str, Any] = {}
        decay: list[dict[str, Any]] = []
        primary_frame = fwd.get(horizon_tuple[0], pd.DataFrame())
        for h in horizon_tuple:
            ic = _ic_series(used, fwd.get(h, pd.DataFrame()))
            stats = _ic_stats(ic, horizon=h)
            # The layer read walks every day and is by far the most expensive
            # part. Compute it for the primary horizon only; the others already
            # have their IC, which is what the decay read needs.
            if h == horizon_tuple[0]:
                layers = _layer_returns(used, primary_frame)
            else:
                layers = {"layers": [], "monotonicity": None, "long_short": None,
                          "n_days": 0, "note": "分层只算主期限；其它期限见 ic"}
            by_horizon[str(h)] = {"ic": stats, "layers": layers,
                                  "time_holdout": _time_holdout(ic) if h == horizon_tuple[0] else None,
                                  "ic_series_tail": [_f(v) for v in ic.tail(10)]}
            decay.append({"h": h, "ic_mean": stats["ic_mean"], "status": stats["status"]})
            if stats.get("p_value") is not None:
                tests.append({"factor": key, "horizon": h, "p_value": stats["p_value"],
                              "t_stat": stats["t_stat"]})

        # Raw (un-neutralised) IC on the primary horizon: the difference is the
        # answer to "was this just size / industry exposure?"
        raw_ic = _ic_series(raw, fwd.get(horizon_tuple[0], pd.DataFrame()))
        raw_mean = _f(raw_ic.mean()) if len(raw_ic) >= MIN_IC_DAYS else None
        primary = by_horizon[str(horizon_tuple[0])]["ic"]
        rows.append({
            **spec,
            "status": "measured" if primary["status"] == "measured" else "insufficient",
            "primary_horizon": horizon_tuple[0],
            "ic_raw_mean": raw_mean,
            "ic_neutral_mean": primary["ic_mean"],
            "ic_attribution_gap": (_f(primary["ic_mean"] - raw_mean)
                                   if primary["ic_mean"] is not None and raw_mean is not None else None),
            "by_horizon": by_horizon,
            "decay": decay,
        })

    # Multiple-testing correction over every (factor, horizon) pair actually
    # reported. p=None (insufficient sample) is excluded, and the exclusion is
    # stated, so a reader cannot mistake "not tested" for "passed".
    n_tests = len(tests)
    adjusted = bh_adjust([t["p_value"] for t in tests]) if n_tests else []
    for t, q in zip(tests, adjusted if isinstance(adjusted, list) else []):
        t["p_adjusted"] = _f(q)
    survivors = [t["factor"] for t in tests
                 if t.get("p_adjusted") is not None and t["p_adjusted"] < 0.05
                 and t.get("t_stat") is not None and abs(t["t_stat"]) > 3.0]

    # A full 9-factor x 4-horizon payload is ~27 KB, past the host's single-
    # response ceiling: an oversized body is dropped whole, not truncated.
    # Default to one line per factor; `detail="full"` (or naming one factor)
    # returns the horizon series, decay and layer detail for inspection.
    if detail == "summary" and len(rows) > 1:
        slim = []
        for r in rows:
            if r.get("status") != "measured":
                slim.append({k: r.get(k) for k in
                             ("key", "label", "status", "note", "definition", "source")})
                continue
            primary = r["by_horizon"][str(horizon_tuple[0])]["ic"]
            slim.append({
                "key": r["key"], "label": r["label"], "status": r["status"],
                "definition": r["definition"], "source": r["source"],
                "ic_raw_mean": r.get("ic_raw_mean"),
                "ic_neutral_mean": r.get("ic_neutral_mean"),
                "ic_attribution_gap": r.get("ic_attribution_gap"),
                "primary_horizon": horizon_tuple[0],
                "t_stat_hac": primary.get("t_stat"),
                "t_stat_iid": primary.get("t_stat_iid"),
                "p_value": primary.get("p_value"),
                "passes_t3": primary.get("passes_t3"),
                "layer_monotonicity": r["by_horizon"][str(horizon_tuple[0])]["layers"].get("monotonicity"),
                "long_short": r["by_horizon"][str(horizon_tuple[0])]["layers"].get("long_short"),
                "time_holdout": r["by_horizon"][str(horizon_tuple[0])].get("time_holdout"),
                "decay": r.get("decay"),
            })
        meta["factors"] = slim
        meta["detail"] = "summary"
        meta["detail_note"] = ("每个因子只给主期限摘要；要逐期限 IC / 分层 / 衰减明细，"
                               "传 detail=full 或 factors=<单个因子>。")
    else:
        meta["factors"] = rows
        meta["detail"] = "full"

    meta.update({
        "multiple_testing": {
            "n_tests": n_tests,
            "method": "Benjamini-Hochberg FDR",
            "survivors_q05_and_t3": sorted(set(survivors)),
            "excluded_insufficient": sum(1 for r in rows for h in horizon_tuple
                                         if r.get("by_horizon", {}).get(str(h), {})
                                         .get("ic", {}).get("p_value") is None),
            "family": list(horizon_tuple),
            "family_is_default": is_default,
            "note": ("每个 (因子, 期限) 都是一次检验，期限轴与因子轴同属一个 FDR 家族；"
                     "挑出最小 p 值再讲故事的那一刻，分母就是这里全部的检验数。"
                     "BH 之后还要求 |t|>3 才算可继续研究。"
                     + ("" if is_default else
                        f" 本族 {list(horizon_tuple)} 比默认族 {list(HORIZONS)} 多出期限，"
                        "属不同的搜索家族，q 值不可与默认族横向比较。")),
        },
        "caveats": [
            "这是描述性读数：不含 action / verdict / signal / score / ranking / 目标价",
            "RankIC 不等于可交易收益；未扣佣金、印花税、滑点、冲击与涨跌停不可成交",
            "T 日因子对 T+1 开盘起的前向收益，已避开 alphalens 的 shift(-h) 同收盘泄漏",
            "超额是当日等权横截面 demean，不是指数/行业基准，也不是风险模型残差 alpha",
            "中性化只做了行业 demean + log 市值回归，不是 Barra；没有因子协方差与残差风险",
            "IC 的 t 值经 Newey-West(Bartlett, lags=h) 校正，跨日重叠不再抬高 t；|t|>3 只是参考线不是判决",
            "time_holdout 只是 IC 前后半段的方向一致性，不是 walk-forward；样本外证据以裁判层 CPCV 为准",
            "daily_basic 是厂商当日快照而非重述历史；fina_indicator 的 ann_date PIT 未接入",
            "moneyflow 与 daily.amount 单位不同，net_mf_5 的绝对值没有经济含义，只用于横截面排序",
            "从这里挑因子去构造策略，必须通过 quant_hypothesis(add) 预注册并计入试验分母",
        ],
        "note": ("本层只读：不写 store、不登记假设、不改 trial 分母、不进 arena.edge、"
                 "不产生任何委托。它回答「哪些横截面信号值得写一条假设」，不回答「买什么」。"),
    })
    if industry is not None:
        meta["caveats"].append(
            "行业中性化不是严格 PIT：当前 index_member 只有静态快照；"
            "neutralisation.industry_pit_safe=false，ic_raw_mean 是未中性化对照")
    return meta
