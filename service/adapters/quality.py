"""Data quality: detect, quantify, repair, and only then quarantine.

Policy (deliberate, documented in reports/P1_findings.md)
--------------------------------------------------------
- Row-level defects (zero/negative price, OHLC contradiction, duplicate key,
  limit breach) are **repaired**: bad rows go to ``data/rejects/<dataset>/`` and
  the clean rows are stored with status ``warn``. One vendor glitch should not
  freeze an entire year of trading.
- Structural defects (schema mismatch, >2% of rows bad, missing partition) mark
  the partition ``quarantine``, which removes it from ``DatasetStore.read()`` and
  from the agent's context.
- **Source divergence is measured, not asserted.** The rule above used to claim
  ">0.5% divergence quarantines" as if ``evaluate`` did it; it never did, because
  deciding it needs a second vendor and ``evaluate`` sees one frame.
  ``reconcile_with_baostock`` produces that comparison as a separate report and
  nothing feeds it into the verdict. Do not read a partition's status as evidence
  that two sources agreed — check the reconciliation output instead.
- Every check returns counts and samples, so the LLM sees "3 of 1,412,000 rows"
  instead of a vague "data looks fine".

No check toggles trading by itself; the risk layer reads the resulting status.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from service import settings

from .catalog import DatasetSpec, get_spec

PRICE_COLUMNS = ("open", "high", "low", "close")
QUARANTINE_RATIO = settings.num("quality.quarantine_ratio")

# 一次快照里"没有成交"的行最多能占多少。停牌是常态（A 股每天 100–400 只，
# 约 6%），所以 last<=0 不能一律当坏行；但全市场不会有五分之一同时停牌，超过
# 这条线说明是取数失败而不是停牌——豁免必须带一个上限，否则它会把一次全量失败
# 读成一片安静。
SUSPENDED_SHARE_CAP = 0.20

# Relative disagreement between the cumulative factor and the exchange pre_close
# that counts as a broken identity. Both describe the same overnight re-basing by
# different publishers and agree to ~1e-6 on 99.999% of rows, so 1% is far above
# their rounding and far below any real corporate action.
FACTOR_IDENTITY_TOLERANCE = settings.num("quality.factor_identity_tolerance")

# How long the baostock cross-check may spend before reporting what it has.
RECONCILE_BUDGET_SECONDS = settings.num("quality.reconcile_budget_seconds")


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str  # info | warn | error
    dataset: str
    count: int
    ratio: float
    detail: str
    sample: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "dataset": self.dataset,
            "count": self.count,
            "ratio": round(self.ratio, 6),
            "detail": self.detail,
            "sample": list(self.sample[:8]),
        }


@dataclass
class QualityResult:
    dataset: str
    partition: str
    rows: int
    status: str
    issues: list[Issue]
    # How many check functions actually ran for this partition: one for a pool or
    # a quote batch, one or two for a daily panel. It used to be the literal 12
    # for every dataset, which read as "twelve assertions passed" on a pool where
    # two had run. Nothing consumes it today, so the honest definition costs
    # nothing and cannot drift.
    checks_run: int
    checked_at: str

    @property
    def error_count(self) -> int:
        return max((i.count for i in self.issues if i.severity == "error"), default=0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "partition": self.partition,
            "rows": self.rows,
            "status": self.status,
            "checks_run": self.checks_run,
            "checked_at": self.checked_at,
            "error_count": self.error_count,
            "issues": [i.as_dict() for i in self.issues],
        }


def _sample(df: pd.DataFrame, mask: pd.Series, cols: Iterable[str], n: int = 8) -> tuple[dict, ...]:
    cols = [c for c in cols if c in df.columns]
    bad = df.loc[mask, cols].head(n)
    return tuple(
        {k: (None if pd.isna(v) else v) for k, v in row.items()}
        for row in bad.to_dict("records")
    )


def _issue(code: str, severity: str, dataset: str, df: pd.DataFrame, mask: pd.Series,
           detail: str, cols: Iterable[str] = ()) -> Issue:
    count = int(mask.sum())
    ratio = count / len(df) if len(df) else 0.0
    return Issue(
        code=code,
        severity=severity,
        dataset=dataset,
        count=count,
        ratio=ratio,
        detail=detail,
        sample=_sample(df, mask, cols),
    )


# --------------------------------------------------------------------- daily
def check_daily_panel(df: pd.DataFrame, dataset: str, spec: DatasetSpec,
                      calendar_days: set[str] | None = None,
                      suspensions: dict[str, set[str]] | None = None,
                      partition: str = "") -> list[Issue]:
    issues: list[Issue] = []
    if df.empty:
        # count=1, not 0. The verdict below already quarantines on the *code*
        # alone, but ``error_count`` is ``max(issue.count)`` — so a quarantined
        # partition reported "0 errors" on the ops board, which reads as a bug in
        # the board rather than in the data.
        return [Issue("empty", "error", dataset, 1, 1.0, "partition is empty")]

    missing = [c for c in spec.required if c not in df.columns]
    if missing:
        return [Issue("schema_missing", "error", dataset, len(df), 1.0,
                      f"missing required columns {missing}")]

    if spec.pk and all(c in df.columns for c in spec.pk):
        dup = df.duplicated(subset=list(spec.pk), keep=False)
        if dup.any():
            issues.append(_issue("duplicate_key", "error", dataset, df, dup,
                                 f"duplicate rows on {spec.pk}", spec.pk))

    price_cols = [c for c in PRICE_COLUMNS if c in df.columns]
    # 三样东西长得很像，混成一样就会把前两种报成 error：
    #   absent   整行没有观测——daily_basic 停牌日 close/pe/pb 全空、指数发布
    #            首日只有基点没有 OHLC；
    #   untraded 今天没成交——可转债未上市/停牌，厂商把 OHLC 填 0、成交量填 0；
    #   real     真的坏价。
    # 停牌豁免在 check_quotes 里早就有了（_untraded），日线面板一直没有：于是
    # cb_daily 的 4 个年度分区各报 600–1200 行"坏价"和同样一批 OHLC 矛盾，
    # 而 repair 又按坏行把它们删掉。同一个语义两套标准，改在这里。
    absent = pd.Series(False, index=df.index)
    untraded = pd.Series(False, index=df.index)
    if price_cols:
        prices = df[price_cols].apply(pd.to_numeric, errors="coerce")
        absent = prices.isna().all(axis=1)
        if "vol" in df.columns:
            # 价全空但量非零不是"没有观测"，是坏行：那一天有人成交。
            absent &= pd.to_numeric(df["vol"], errors="coerce").isna()
        untraded = _untraded(df) & ~absent
        nonpos = ((prices <= 0) | ~np.isfinite(prices)).any(axis=1) & ~absent & ~untraded
        nonpos &= df["ts_code"].notna() if "ts_code" in df.columns else True
        if absent.any():
            issues.append(_issue(
                "price_absent", "info", dataset, df, absent,
                "整行没有观测（价格字段全空），不计入坏价", ["ts_code", "trade_date", *price_cols]))
        if untraded.any():
            issues.append(_issue(
                "untraded_row", "info", dataset, df, untraded,
                "没有成交（open/high/low 为 0 且成交量为 0），不计入坏价",
                ["ts_code", "trade_date", *price_cols, "vol"]))
        if nonpos.any():
            issues.append(_issue("non_positive_price", "error", dataset, df, nonpos,
                                 "price <= 0, missing or non-finite",
                                 ["ts_code", "trade_date", *price_cols]))

    if {"high", "low", "open", "close"}.issubset(df.columns):
        hi = pd.to_numeric(df["high"], errors="coerce")
        lo = pd.to_numeric(df["low"], errors="coerce")
        op = pd.to_numeric(df["open"], errors="coerce")
        cl = pd.to_numeric(df["close"], errors="coerce")
        bad = (hi + 1e-9 < lo) | (hi + 1e-9 < np.maximum(op, cl)) | (lo - 1e-9 > np.minimum(op, cl))
        # open=0 的行必然"high=0 低于 close"，那是没成交的签名，不是矛盾。
        bad &= ~absent & ~untraded
        if bad.any():
            issues.append(_issue("ohlc_inconsistent", "error", dataset, df, bad,
                                 "high/low inconsistent with open/close",
                                 ["ts_code", "trade_date", "open", "high", "low", "close"]))

    if "vol" in df.columns:
        vol = pd.to_numeric(df["vol"], errors="coerce")
        invalid_vol = (vol.lt(0) | ~np.isfinite(vol)) & ~absent & ~untraded
        if invalid_vol.any():
            issues.append(_issue("volume_invalid", "error", dataset, df, invalid_vol,
                                 "volume is negative, missing or non-finite",
                                 ["ts_code", "trade_date", "vol"]))
        if {"close", "pre_close"}.issubset(df.columns):
            moved = (pd.to_numeric(df["close"], errors="coerce")
                     - pd.to_numeric(df["pre_close"], errors="coerce")).abs() > 1e-6
            zero_vol = vol.eq(0) & moved
            if zero_vol.any():
                issues.append(_issue("zero_volume_price_move", "warn", dataset, df, zero_vol,
                                     "volume is zero but close != pre_close",
                                     ["ts_code", "trade_date", "vol", "close", "pre_close"]))

    if {"up_limit", "down_limit"}.issubset(df.columns):
        up = pd.to_numeric(df["up_limit"], errors="coerce")
        down = pd.to_numeric(df["down_limit"], errors="coerce")
        valid = up.gt(0) & down.gt(0)
        if "close" in df.columns:
            cl = pd.to_numeric(df["close"], errors="coerce")
            breach = valid & ((cl > up * 1.001) | (cl < down * 0.999))
            if breach.any():
                issues.append(_issue("limit_breach", "error", dataset, df, breach,
                                     "close outside exchange limit band by >0.1%",
                                     ["ts_code", "trade_date", "close", "up_limit", "down_limit"]))
        # A non-positive limit is not a breach and not a bad row: first-day
        # listings and delisting-arrangement names legitimately have no band.
        # But "no band" and "the vendor missed the row" look identical here, so
        # the count is surfaced as info rather than silently passing. The three
        # cases are separated by scripts/limit_band_audit.py against stock_basic.
        # This must not require `close`: `stk_limit` itself has no close column,
        # and gating the count on it is why the partition used to report a clean
        # `ok` with 70 unexplained zeros.
        absent = ~valid
        if absent.any():
            issues.append(_issue(
                "limit_band_absent", "info", dataset, df, absent,
                "up_limit/down_limit non-positive: no band (first day/delisting) "
                "or a missing row — classify with scripts/limit_band_audit.py",
                ["ts_code", "trade_date", "up_limit", "down_limit"]))

    if "adj_factor" in df.columns:
        adj = pd.to_numeric(df["adj_factor"], errors="coerce")
        bad_adj = adj.le(0) | ~np.isfinite(adj)
        if bad_adj.any():
            issues.append(_issue("adj_factor_invalid", "error", dataset, df, bad_adj,
                                 "adj_factor <= 0, missing or non-finite",
                                 ["ts_code", "trade_date", "adj_factor"]))
        if "ts_code" in df.columns:
            ordered = df.assign(_adj=adj).sort_values(["ts_code", "trade_date"], kind="mergesort")
            # Tushare occasionally revises the last decimal of a cumulative
            # factor; only a >0.1% drop indicates a real restatement problem.
            grouped = ordered.groupby("ts_code")["_adj"]
            prev = grouped.shift(1)
            dec = (ordered["_adj"] / prev - 1) < -1e-3
            if dec.any():
                idx = ordered.index[dec.fillna(False)]
                # 2026-11-14 查过一次：全库 515 行、22 个年度分区，绝大多数是北交所
                # 代码。上游自己就是翻的——2017-04-26 的 920627.BJ 返回 1.0，
                # 04-27 返回 1.9987，日线收盘也在 3.92 / 6.60 之间跳。这不是我们的
                # 合并问题，也不是一次重述：供应商给北交所历史套了两条基准、按日
                # 交替。修不了，但要把"是谁"写进告警，否则每次都要重新查一遍。
                #
                # 2026-11-15 又量了一次，这次按"后来有没有回到跌前水平"分：
                # 515 处里 460 处（89%）在同一年内自愈，那几天因子是错的、之后
                # 又对上了；真正改写不回来的是 55 处。两者的处理不一样——自愈的
                # 只说明"这段序列可疑"，没恢复的说明"这段历史的复权被永久改写"。
                # 一律记 error 的结果是 18 个年度分区常年挂着红字，而它既不能
                # 修、也不影响已经对齐的那部分，读久了就没人看了。
                fwd = (ordered.loc[ordered.sort_values(
                    ["ts_code", "trade_date"], ascending=[True, False],
                    kind="mergesort").index]
                    .groupby("ts_code")["_adj"].cummax()
                    .reindex(ordered.index))
                hit = dec.fillna(False)
                healed = hit & (fwd >= prev)
                # 北交所不在可交易域（`_missing_days` 早就按同一条线剔除它们），
                # 它的因子翻转又是供应商按日交替的已知缺陷：留在 error 里等于让
                # 一列永远红着。不是不报，是不替不相干的事按最高级报警。
                is_bj = ordered["ts_code"].str.endswith(".BJ")
                unhealed = hit & ~(fwd >= prev) & ~is_bj
                codes = ordered.loc[idx, "ts_code"]
                bj = int(codes.str.endswith(".BJ").sum())
                detail = ("cumulative adj_factor dropped >0.1% (wrong back-adjustment); "
                          f"{codes.nunique()} 只代码，{bj} 处落在 .BJ")
                if bj:
                    detail += "（供应商历史因子按日翻转，非本台可用域）"
                if unhealed.any():
                    detail += (f"；可用域内 {int(unhealed.sum())} 处到分区末仍未恢复"
                               f"（{ordered.loc[unhealed, 'ts_code'].nunique()} 只代码）")
                elif healed.any():
                    detail += "，其余在本分区内回到跌前水平（短时翻值，已自愈）"
                issues.append(Issue(
                    # 自愈的只是"看着可疑"，可用域内没恢复的是真的改写：定级跟着
                    # 这件事走，否则每个年度分区都带一条永不消失的 error。
                    "adj_factor_decrease", "error" if unhealed.any() else "warn",
                    dataset, int(hit.sum()),
                    float(hit.sum() / max(1, len(ordered))), detail,
                    tuple(ordered.loc[idx, ["ts_code", "trade_date", "_adj"]].head(8)
                          .rename(columns={"_adj": "adj_factor"}).to_dict("records")),
                ))

    if "float_ratio" in df.columns:
        # A single unlock cannot exceed the whole share count. Tushare ships one
        # such row (002116.SZ 2026-12-29: float_ratio 14,845.7, float_share 8.3e10)
        # and left in, it turns "0.8% of the float unlocks in 180 days" into
        # 14,846%. Warn rather than quarantine: one bad row must not make the
        # table unreadable, but the number has to be visible somewhere.
        ratio = pd.to_numeric(df["float_ratio"], errors="coerce")
        impossible = ratio.gt(100)
        if impossible.any():
            issues.append(_issue(
                "unlock_ratio_impossible", "warn", dataset, df, impossible,
                "float_ratio > 100% of total shares (single unlock cannot exceed it)",
                ["ts_code", "float_date", "float_ratio", "float_share"]))

    if {"close", "adj_factor", "ts_code", "trade_date"}.issubset(df.columns):
        issues.extend(_adjusted_continuity(df, dataset))

    # Gap analysis only makes sense for a full price panel; sparse tables such as
    # suspend_d legitimately have no row on most days.
    if calendar_days and spec.date_column == "trade_date" and "ts_code" in df.columns \
            and "close" in df.columns:
        issues.extend(_missing_days(df, dataset, calendar_days, suspensions or {},
                                    partition=partition))

    return [i for i in issues if i.count > 0]


def _adjusted_continuity(df: pd.DataFrame, dataset: str) -> list[Issue]:
    """Adjusted close must not jump; a jump means the factor was applied wrong.

    Jumps across a suspension/resumption gap (or the first day of delisting) are
    legitimate: the stock reopens at a very different price. Only consecutive
    trading days are checked, which is where a factor error would show up.
    """
    adj_close = (pd.to_numeric(df["close"], errors="coerce")
                 * pd.to_numeric(df["adj_factor"], errors="coerce"))
    tmp = df[["ts_code", "trade_date"]].assign(_ac=adj_close)
    tmp["_dt"] = pd.to_datetime(tmp["trade_date"], format="%Y%m%d", errors="coerce")
    tmp = tmp.sort_values(["ts_code", "trade_date"], kind="mergesort")
    grouped = tmp.groupby("ts_code")
    chg = grouped["_ac"].pct_change(fill_method=None).abs()
    gap_days = grouped["_dt"].diff().dt.days
    prior_rows = grouped.cumcount()
    # First days after listing have no price limit and routinely move >35%.
    jump = (chg > 0.35) & (gap_days.fillna(1) <= 7) & (prior_rows >= 20)
    if not jump.any():
        return []
    idx = tmp.index[jump.fillna(False)]
    return [Issue(
        "adjusted_price_jump", "warn", dataset, int(jump.sum()),
        float(jump.sum() / max(1, len(tmp))),
        "adjusted close jumps >35% on consecutive trading days (possible factor error)",
        tuple(tmp.loc[idx, ["ts_code", "trade_date", "_ac"]].head(8).to_dict("records")),
    )]


def _factor_identity(merged: pd.DataFrame, dataset: str) -> list[Issue]:
    """The cumulative factor must agree with the exchange's own previous close.

    ``pre_close`` is the exchange's *adjusted* previous close: on an ex-date it
    is the post-adjustment price, which is exactly what the factor ratio
    describes. So on every session,

        pre_close / close_p1  ==  adj_factor_p1 / adj_factor

    holds as an identity. It compares two independent publishers of one fact,
    needs no threshold on how far a price may legally move, and therefore catches
    a factor that is wrong by 6% — invisible to a >35% jump test — while never
    firing on a Beijing listing that moved 30% or a name resuming after a long
    suspension.

    Measured on the live store: 122 rows against the jump test's 11. Of those,
    119 are ``920xxx.BJ`` codes whose factor restarts at 1.0 when the old 8xxxxx
    code is migrated to the new segment — a flat day would be booked as -99%.
    """
    needed = {"ts_code", "trade_date", "close", "pre_close", "adj_factor"}
    if not needed.issubset(merged.columns):
        return []
    frame = merged[["ts_code", "trade_date", "close", "pre_close", "adj_factor"]].copy()
    for col in ("close", "pre_close", "adj_factor"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.sort_values(["ts_code", "trade_date"], kind="mergesort")
    frame["_dt"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="coerce")
    grouped = frame.groupby("ts_code", sort=False)
    frame["_close_p1"] = grouped["close"].shift(1)
    frame["_fac_p1"] = grouped["adj_factor"].shift(1)
    # Only consecutive sessions. A name that resumes after a long halt keeps the
    # pre-halt price as ``pre_close`` (that is the exchange rule) while the factor
    # has absorbed every corporate action in between, so the identity does not
    # hold across the gap and does not mean anything is wrong. 000010.SZ on
    # 2013-07-19 is exactly that case: a 4x factor move over a multi-year halt.
    gap_days = grouped["_dt"].diff().dt.days

    usable = ((frame["pre_close"] > 0) & (frame["_close_p1"] > 0)
              & (frame["adj_factor"] > 0) & (frame["_fac_p1"] > 0)
              & (gap_days.fillna(1) <= 7))
    actual = frame["pre_close"] / frame["_close_p1"]   # the exchange's re-basing
    expect = frame["_fac_p1"] / frame["adj_factor"]    # the vendor's re-basing
    with np.errstate(divide="ignore", invalid="ignore"):
        err = (actual / expect - 1).abs()
    hit = usable & np.isfinite(err) & (err > FACTOR_IDENTITY_TOLERANCE)
    if not hit.any():
        return []
    rows = frame.loc[hit, ["ts_code", "trade_date", "close", "pre_close",
                           "_close_p1", "_fac_p1", "adj_factor"]].copy()
    rows["_err"] = err[hit].round(4)
    return [Issue(
        "adj_factor_conflict", "warn", dataset, int(hit.sum()),
        float(hit.sum() / max(1, len(frame))),
        "adj_factor disagrees with the exchange pre_close (identity broken: the "
        "factor restarted or skipped a corporate action)",
        tuple(rows.head(8).rename(columns={"_close_p1": "prev_close",
                                           "_fac_p1": "prev_factor",
                                           "_err": "rel_error"}
                                  ).to_dict("records")),
    )]


def check_adjustment_continuity(daily: pd.DataFrame, adj: pd.DataFrame,
                                dataset: str = "daily") -> list[Issue]:
    """Cross-dataset check: daily bars × adj_factor, aligned on ts_code/trade_date.

    Two independent readings of the same overnight re-basing, because they fail
    differently: the jump test catches a corrupt price, the identity test catches
    a corrupt factor.
    """
    cols = ["ts_code", "trade_date"]
    if daily.empty or adj.empty or not set(cols).issubset(daily.columns) \
            or not set(cols).issubset(adj.columns):
        return []
    right = adj[[*cols, "adj_factor"]].drop_duplicates(subset=cols, keep="last")
    merged = daily.merge(right, on=cols, how="inner", suffixes=("", "_adj"))
    if "adj_factor_adj" in merged.columns:
        merged["adj_factor"] = merged["adj_factor_adj"]
    issues = _adjusted_continuity(merged, dataset)
    issues.extend(_factor_identity(merged, dataset))
    return issues


def _missing_days(df: pd.DataFrame, dataset: str, calendar_days: set[str],
                  suspensions: dict[str, set[str]],
                  partition: str = "", as_of: str = "") -> list[Issue]:
    cal = sorted(calendar_days)
    index = {d: i for i, d in enumerate(cal)}
    issues: list[Issue] = []

    # Per-symbol min/max bounds are necessary for listings and delistings, but
    # they cannot see a whole partition truncated at either edge. Check that
    # separately instead of demanding every symbol span the full calendar.
    #
    # The edge check must run against the days **this partition owns**, not the
    # whole 2005-2026 calendar: comparing a year file to a 22-year calendar made
    # every year "missing" its neighbours' days, so `daily` came out `warn` on
    # 22 of 22 partitions and `ok` on none. A status that is always `warn`
    # carries no information, which is worse than no status at all.
    observed = set(df["trade_date"].astype(str)) & set(cal)
    first, last = (min(observed), max(observed)) if observed else (None, None)
    scope = cal
    if partition.isdigit() and len(partition) == 4:
        scope = [d for d in cal if d.startswith(partition)]
        cutoff = as_of or datetime.now().strftime("%Y%m%d")
        scope = [d for d in scope if d <= cutoff]
    edge_missing = [d for d in scope
                    if (first is None or d < first) or (last is None or d > last)]
    if edge_missing:
        issues.append(Issue(
            "missing_calendar_days", "warn", dataset, len(edge_missing),
            len(edge_missing) / max(1, len(scope)),
            "panel is truncated at the start/end of the window this partition owns",
            tuple({"trade_date": d, "position": "leading" if first is None or d < first
                   else "trailing"} for d in edge_missing[:8]),
        ))

    grouped = df.groupby("ts_code")["trade_date"].agg(["min", "max", "nunique"])
    # BSE names are excluded from the tradable universe and have different
    # halt conventions; keeping them would drown the signal in noise.
    grouped = grouped[~grouped.index.astype(str).str.endswith(".BJ")]
    bad: list[tuple[str, int, int]] = []
    for code, row in grouped.iterrows():
        lo, hi = str(row["min"]), str(row["max"])
        if lo not in index or hi not in index:
            continue
        expected = index[hi] - index[lo] + 1
        if expected <= 5:
            continue
        suspended = sum(
            1 for day in suspensions.get(code, ()) if day in index and lo <= day <= hi
        )
        missing = expected - int(row["nunique"]) - suspended
        if missing > max(2, int(0.02 * expected)):
            bad.append((code, missing, expected))
    if bad:
        total_codes = max(1, len(grouped))
        issues.append(Issue(
            "missing_trading_days", "warn", dataset, len(bad), len(bad) / total_codes,
            f"{len(bad)}/{total_codes} codes have >2% missing trading days",
            tuple({"ts_code": c, "missing": m, "expected": e} for c, m, e in bad[:8]),
        ))
    return issues


# ---------------------------------------------------------------------- pool
def check_pool(df: pd.DataFrame, dataset: str, spec: DatasetSpec,
               expected_limit_ups: int | None = None) -> list[Issue]:
    issues: list[Issue] = []
    if df.empty:
        return [Issue("pool_empty", "warn", dataset, 0, 1.0,
                      "empty pool (source gap or genuinely no limit-ups)")]
    missing = [c for c in spec.required if c not in df.columns]
    if missing:
        return [Issue("schema_missing", "error", dataset, len(df), 1.0,
                      f"missing required columns {missing}")]
    dup = df.duplicated(subset=list(spec.pk), keep=False)
    if dup.any():
        issues.append(_issue("duplicate_key", "error", dataset, df, dup, "duplicate pool rows", spec.pk))
    if "pct_chg" in df.columns:
        pct = pd.to_numeric(df["pct_chg"], errors="coerce")
        # 20% boards and first-day listings (up to 44% on the main board, no
        # limit on STAR/ChiNext) are legitimate; only flag impossible values.
        weird = (pct > 60) | (pct < -60)
        if weird.any():
            issues.append(_issue("pct_out_of_range", "error", dataset, df, weird,
                                 "|pct_chg| > 60%", ["code", "pct_chg"]))
    if expected_limit_ups is not None and expected_limit_ups > 0:
        gap = abs(len(df) - expected_limit_ups) / expected_limit_ups
        if gap > 0.20:
            issues.append(Issue(
                "pool_coverage_gap", "warn", dataset, len(df), gap,
                f"pool has {len(df)} rows vs {expected_limit_ups} limit-up closes from T0 data",
            ))
    return issues


def _capture_epoch(df: pd.DataFrame, ts: pd.Series) -> pd.Series | None:
    """When each row's batch was captured, as epoch seconds aligned to rows.

    `snapshot_ts` is the capture moment written by the snapshot script, so the
    honest per-row lag is ``snapshot_ts - quote_ts``. Without it there is only one
    batch, and the newest quote in the frame stands in for the capture moment.
    """
    if "snapshot_ts" in df.columns:
        stamp = pd.to_datetime(df["snapshot_ts"], errors="coerce", utc=True)
        if stamp.notna().any():
            return (stamp.astype("int64") / 1_000_000_000).where(stamp.notna())
    newest = ts.max()
    if pd.isna(newest):
        return None
    return pd.Series(float(newest), index=df.index)


def _untraded(df: pd.DataFrame) -> pd.Series:
    """停牌的签名：没有价、没有量、没有开盘价——今天根本没有成交过。

    三样里至少要有一样可查。``volume`` 与 ``open`` 都不在表里时返回全 False，
    也就是"按坏行处理"：宁可误报，也不能因为缺列而把一份坏数据认成一片停牌。
    """
    # ``volume`` 是快照的列名，``vol`` 是日线面板的列名。只认前者时，日线
    # 面板上的"今天没成交"永远返回全 False——这正是 cb_daily 每个年度分区
    # 都在报上千行坏价的原因（见 check_daily_panel 里那段）。
    present = [c for c in ("volume", "vol", "open") if c in df.columns]
    if not present:
        return pd.Series(False, index=df.index)
    mask = pd.Series(True, index=df.index)
    for col in present:
        values = pd.to_numeric(df[col], errors="coerce")
        mask &= values.fillna(0).le(0)
    return mask


def check_quotes(df: pd.DataFrame, dataset: str, *, max_stale_seconds: int = 120,
                 now_epoch: float | None = None,
                 partition: str | None = None) -> list[Issue]:
    """Quality of one snapshot partition.

    Staleness here is measured against **the partition's own capture moment**, not
    the wall clock. A snapshot is a sample of a tape: the newest quote in the file
    *is* the moment it was taken, so "how old is this row" can only mean "how far
    behind the rest of its own batch".

    The old rule compared against `datetime.now()`, which made it a permanent
    false positive: `verify_data` runs at 17:30 over a file whose last quote is
    14:55, so every single row failed `age > 120s` and all four `rt_quote`
    partitions sat at `warn` with `ratio=1.0`. An alarm that always fires is a
    blind spot — it hides the one case that matters, which is a whole batch
    stamped with somebody else's session. That case is now its own check
    (`quote_not_from_session`) rather than something to be inferred from ages.

    `now_epoch` is still honoured when a caller passes it, because that caller is
    saying "this is the live clock, judge against it" (a test, or a true live
    capture).
    """
    if df.empty:
        return [Issue("quotes_empty", "error", dataset, 0, 1.0, "no quotes returned")]
    issues: list[Issue] = []

    if "quote_ts" in df.columns:
        ts = pd.to_numeric(df["quote_ts"], errors="coerce")

        if now_epoch is not None:
            reference = pd.Series(float(now_epoch), index=df.index)
            stale = (reference - ts) > max_stale_seconds
            if stale.any():
                age = float((reference - ts).max())
                issues.append(_issue("stale_quote", "warn", dataset, df, stale,
                                     f"max quote age {age:.0f}s > {max_stale_seconds}s",
                                     ["code", "quote_ts", "last"]))
        else:
            capture = _capture_epoch(df, ts)
            if capture is not None:
                lag = capture - ts
                stale = lag > max_stale_seconds
                if stale.any():
                    age = float(lag.max())
                    issues.append(_issue(
                        "stale_quote", "warn", dataset, df, stale,
                        f"max lag behind its own batch {age:.0f}s > {max_stale_seconds}s",
                        ["code", "quote_ts", "last"]))

        if partition and "trade_date" in df.columns:
            # The failure the age test was reaching for: a partition dated today
            # holding yesterday's tape. Comparing dates catches it at any hour,
            # which is what the wall-clock version could never do.
            day = ts.dropna()
            if not day.empty:
                from datetime import datetime as _dt

                newest = _dt.fromtimestamp(float(day.max())).strftime("%Y%m%d")
                if newest != str(partition):
                    issues.append(Issue(
                        "quote_not_from_session", "error", dataset, len(df), 1.0,
                        f"newest quote is {newest}, partition is {partition}",
                    ))

    if "last" in df.columns:
        last = pd.to_numeric(df["last"], errors="coerce")
        bad = last.le(0) | ~np.isfinite(last)
        if bad.any():
            # 停牌股的 last 是 0，那不是坏价格，是"今天没有成交"。A 股每天停牌
            # 100–400 只，占全市场 6% 上下，而 QUARANTINE_RATIO 是 2%——把它们
            # 算成坏行，每一帧快照都会判 quarantine。一个每天都响的告警，等于
            # 把真正的喂价故障藏起来（这个文件上面 stale_quote 那段，当初就是
            # 同一个毛病：一条永远成立的条件冒充检查）。
            #
            # 实测 2026-11-02：5,560 行里 343 只 last<=0，全部只出现一次、从未
            # 再更新——那是停牌，不是坏数据。
            halted = bad & _untraded(df)
            broken = bad & ~halted
            if broken.any():
                issues.append(_issue("invalid_last", "error", dataset, df, broken,
                                     "last price <= 0, missing or non-finite",
                                     ["code", "last"]))
            # 停牌本身不是缺陷，但"全市场两成同时停牌"不是停牌，是取数坏了：
            # 那条界线让这个豁免不至于把一次全量失败读成一片安静。
            share = float(halted.sum()) / len(df)
            if share > SUSPENDED_SHARE_CAP:
                issues.append(_issue(
                    "suspended_share", "error", dataset, df, halted,
                    f"{share:.1%} of the tape has no price and no volume — "
                    f"that is not a normal halt level",
                    ["code", "last", "volume"]))
    return issues


# ------------------------------------------------------------------- verdict
def evaluate(dataset: str, partition: str, df: pd.DataFrame,
             *, calendar_days: set[str] | None = None,
             suspensions: dict[str, set[str]] | None = None,
             expected_limit_ups: int | None = None,
             now_epoch: float | None = None,
             adj_frame: pd.DataFrame | None = None) -> QualityResult:
    spec = get_spec(dataset)
    if dataset in ("limit_up_pool", "broken_board_pool", "limit_down_pool"):
        issues = check_pool(df, dataset, spec, expected_limit_ups)
        checks_run = 1
    elif dataset == "rt_quote":
        issues = check_quotes(df, dataset, now_epoch=now_epoch, partition=partition)
        checks_run = 1
    else:
        issues = check_daily_panel(df, dataset, spec, calendar_days, suspensions,
                                   partition=partition)
        checks_run = 1
        if adj_frame is not None and dataset == "daily":
            # Two checks, counted as two: the jump test reads the price series and
            # the identity test reads the factor series, and a report that says
            # "1 check ran" for both understates what the verdict is based on.
            issues.extend(check_adjustment_continuity(df, adj_frame, dataset))
            checks_run += 2

    worst_ratio = max((i.ratio for i in issues if i.severity == "error"), default=0.0)
    structural = any(i.code in {"schema_missing", "empty"} for i in issues)

    if structural or worst_ratio > QUARANTINE_RATIO:
        status = "quarantine"
    elif any(i.severity != "info" for i in issues):
        # An `info` finding is a count worth showing, not a defect: letting it
        # set `warn` would turn every partition with one first-day listing into
        # a warning and make the status column noise.
        status = "warn"
    else:
        status = "ok"

    return QualityResult(
        dataset=dataset,
        partition=partition,
        rows=len(df),
        status=status,
        issues=issues,
        checks_run=checks_run,
        checked_at=datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
    )


def repair(df: pd.DataFrame, spec: DatasetSpec) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Drop structurally unusable rows; return (clean, rejected).

    Only unambiguous defects are dropped. Warnings (gaps, jumps) are kept and
    reported, because dropping them would silently change the backtest sample.
    Above the structural-defect threshold the original rows are returned so the
    caller's ``evaluate`` still sees the true error ratio and quarantines the
    partition instead of certifying the cleaned remainder as healthy.
    """
    if df.empty:
        return df, df
    mask = pd.Series(False, index=df.index)

    price_cols = [c for c in PRICE_COLUMNS if c in df.columns]
    # 没成交的行（open/high/low 为 0、量为 0）不是坏行：删掉它，"这一天这只债
    # 没成交"就从库里消失了，而停牌天数、复权和换手率都要用这个事实。判据与
    # check_daily_panel 同一处，两边各写一遍就会出现"评估说不算错、入库把它删了"。
    untraded = _untraded(df)
    if price_cols:
        prices = df[price_cols].apply(pd.to_numeric, errors="coerce")
        mask |= ((prices <= 0) | ~np.isfinite(prices)).any(axis=1) & ~untraded
    if "vol" in df.columns:
        vol = pd.to_numeric(df["vol"], errors="coerce")
        mask |= (vol.lt(0) | ~np.isfinite(vol)) & ~untraded
    if "adj_factor" in df.columns:
        adj = pd.to_numeric(df["adj_factor"], errors="coerce")
        mask |= adj.le(0) | ~np.isfinite(adj)
    if {"high", "low", "open", "close"}.issubset(df.columns):
        hi = pd.to_numeric(df["high"], errors="coerce")
        lo = pd.to_numeric(df["low"], errors="coerce")
        op = pd.to_numeric(df["open"], errors="coerce")
        cl = pd.to_numeric(df["close"], errors="coerce")
        mask |= ((hi + 1e-9 < lo) | (hi + 1e-9 < np.maximum(op, cl))
                 | (lo - 1e-9 > np.minimum(op, cl))) & ~untraded
    quality_mask = mask.copy()
    if spec.pk and all(c in df.columns for c in spec.pk):
        mask |= df.duplicated(subset=list(spec.pk), keep="last")
        quality_mask |= df.duplicated(subset=list(spec.pk), keep=False)

    rejected = df.loc[mask].copy()
    if quality_mask.mean() > QUARANTINE_RATIO:
        return df.copy(), rejected
    clean = df.loc[~mask].copy()
    return clean, rejected


# --------------------------------------------------------------- cross source
def reconcile_with_baostock(store, sample_codes: list[str] | None = None,
                            start: str | None = None, end: str | None = None,
                            tolerance: float = 0.005) -> dict[str, Any]:
    """Sample T0 (Tushare) closes against baostock. Optional, network-bound."""
    try:
        import baostock as bs
    except ImportError:
        return {"ok": False, "skipped": "baostock not installed"}

    daily = store.read("daily", start, end)
    if daily.empty:
        return {"ok": False, "skipped": "no daily data"}

    codes = sample_codes or sorted(daily["ts_code"].unique())[:30]
    sample_start = start or str(daily["trade_date"].min())
    sample_end = end or str(daily["trade_date"].max())
    ref = daily[daily["ts_code"].isin(codes)][["ts_code", "trade_date", "close"]]

    login = bs.login()
    if login.error_code != "0":
        return {"ok": False, "skipped": f"baostock login failed: {login.error_msg}"}
    rows: list[dict[str, Any]] = []
    skipped_bj = 0
    # One network round trip per code, and nothing in baostock takes a timeout.
    # A deadline is what keeps a scheduled verification from hanging for hours on
    # a stalled socket; whatever was compared by then is still reported.
    deadline = time.monotonic() + RECONCILE_BUDGET_SECONDS
    try:
        for code in codes:
            if time.monotonic() > deadline:
                break
            # ``.BJ`` was mapped to ``sz.`` and baostock has no Beijing exchange
            # prefix at all, so those names either returned nothing or matched
            # nothing; they are counted as skipped instead of silently dragging the
            # agreement rate down.
            if code.endswith(".BJ"):
                skipped_bj += 1
                continue
            bs_code = f"sh.{code[:6]}" if code.endswith(".SH") else f"sz.{code[:6]}"
            rs = bs.query_history_k_data_plus(
                bs_code, "date,close", start_date=_dash(sample_start),
                end_date=_dash(sample_end), frequency="d", adjustflag="3",
            )
            while rs.error_code == "0" and rs.next():
                d, c = rs.get_row_data()
                rows.append({"ts_code": code, "trade_date": d.replace("-", ""), "bs_close": float(c or "nan")})
    finally:
        bs.logout()

    if not rows:
        return {"ok": False, "skipped": "baostock returned no rows"}
    other = pd.DataFrame(rows)
    merged = ref.merge(other, on=["ts_code", "trade_date"], how="inner")
    merged = merged[merged["bs_close"].gt(0) & merged["close"].gt(0)]
    if merged.empty:
        return {"ok": False, "skipped": "no overlapping rows"}
    diff = (merged["close"] / merged["bs_close"] - 1).abs()
    bad = diff > tolerance
    return {
        "ok": True,
        "codes": len(codes),
        "compared": len(merged),
        "mismatch": int(bad.sum()),
        "max_diff": float(diff.max()),
        "p95_diff": float(diff.quantile(0.95)),
        "tolerance": tolerance,
        "skipped_bj": skipped_bj,
        "timed_out": time.monotonic() > deadline,
        "sample": merged.loc[bad, ["ts_code", "trade_date", "close", "bs_close"]].head(8).to_dict("records"),
    }


def _dash(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
