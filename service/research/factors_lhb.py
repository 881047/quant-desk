"""龙虎榜结构因子的横截面读数：B 层研究的第一个可执行族。

这是一个**独立 BH 族**（``family=ashare_lhb``）。它不复用 ``factors.py`` 的
36 检验分母，也不改那里的 ``FACTOR_SPECS``——那条口径是全部已交付报告的基准，
动它等于让历史判决失去可比性。两族的 q 值**不可跨族比较**，这一点写进
``family_scope``，与 dense 期限族的处理一致。

复用 ``factors.py`` 的四件工具：``_forward_returns``（T+1 open 入场，避开
``shift(-h)`` 的同收盘泄漏）、``_ic_series``（Spearman RankIC，按行 rank）、
``_ic_stats``（Newey-West t）、``_layer_returns``（五分层与单调性）。只替换
因子的来源。

三条设计边界，每条都是这台台子已经吃过亏的地方
----------------------------------------------
1. **样本是条件样本。** 龙虎榜每日 median 73 只（min 50），是当日异动股，
   不是全市场截面。IC 的读法是「上榜股**内部**的相对强弱」，不能外推成
   「全市场的选股能力」。实取 2026-10-27~11-12 的 4,438 个样本里 51.4% 当日
   涨停、12.2% 跌停——这个结构进 ``provenance``，不许省略。

2. **T+1 一字板买不到。** ``spec.py`` 已记录：一字板（从未跌破涨停价）
   +7.01% 买不到，买得到的那批期望为负。这里用 ``open/pre_close`` 与板块
   涨跌幅比较，把「T+1 开盘即涨停」的样本单独剔一遍，两个读数并列报。

3. **``org_*`` 全空。** 机构维度做不了（已验证 62/62 全空）。缺口显式报出，
   不用 0 填充，也不拿「游资」顶替「机构」。

只读：不写 store、不登记假设、不改 trial 分母、不进 arena.edge、不产生委托。
它回答「这几条龙虎榜结构信号有没有横截面预测力」，不回答「买什么」。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from service.adapters.symbols import limit_pct
from service.research import factors as fbase
from service.research.factors import (
    HORIZONS,
    MIN_CROSS_SECTION,
    MIN_IC_DAYS,
    _f,
    _forward_returns,
    _ic_series,
    _ic_stats,
    _layer_returns,
    _neutralise,
    _time_holdout,
    _wide,
    parse_horizons,
)
from service.research.registry import bh_adjust

#: 本族登记的因子。``hypothesis`` 指回 ``quant_hypothesis`` 的 id，
#: 使「跑了一次」能追溯到「哪条预注册」。
LHB_FACTOR_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "lhb_net_rate",
        "label": "龙虎榜净买入占比",
        "definition": "buy_value − sell_value 相对当日成交额的百分比（原字段 net_rate）",
        "source": "ths_lhb(net_rate)",
        "hypothesis": 8,
    },
    {
        "key": "lhb_net_value_pct",
        "label": "净买额日内百分位",
        "definition": "net_value 在当日全部上榜股中的百分位（0~1）",
        "source": "ths_lhb(net_value)",
        "hypothesis": 8,
    },
    {
        "key": "lhb_seat_top1_share",
        "label": "席位净买集中度",
        "definition": "最大正净买席位净买额 / 全部正净买席位合计；分母≤0 置缺失",
        "source": "ths_lhb_seat(net_value)",
        "hypothesis": 9,
    },
    {
        "key": "lhb_seat_n_pos",
        "label": "净买席位家数",
        "definition": "log(1 + 净买额>0 的席位数)",
        "source": "ths_lhb_seat(net_value)",
        "hypothesis": 9,
    },
)

FAMILY = "ashare_lhb"

#: 判定「T+1 开盘即涨停」的容差。涨跌停价在交易所是四舍五入到分的，
#: 用 0.2 个百分点吸收这个舍入，而不是把 10.02% 当成没涨停。
_ONE_WORD_TOL = 0.002

_LHB_COLUMNS = ("ts_code", "trade_date", "net_rate", "net_value",
                "buy_value", "sell_value", "pct", "range_days")
_SEAT_COLUMNS = ("ts_code", "trade_date", "seat", "net_value")


def _read(store, table: str, start: str, end: str, columns: tuple[str, ...]):
    """Read one T1 partition set, degrading to an explicitly empty frame.

    A missing dataset is an answer ("this family cannot be measured"), not a
    crash and not a zero-filled column.
    """
    try:
        frame = store.read(table, start=start, end=end, columns=list(columns))
    except Exception:  # noqa: BLE001 - missing partition set is a result
        return pd.DataFrame(columns=list(columns))
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(columns))
    out = frame.copy()
    out["trade_date"] = out["trade_date"].astype(str)
    out["ts_code"] = out["ts_code"].astype(str)
    return out


def _lhb_factors(lhb: pd.DataFrame, seat: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """One wide frame (day × symbol) per factor, from T-closed disclosures.

    龙虎榜在收盘后披露，所以 T 日的行本来就只有 T 日收盘后才知道。用到 T+1
    开盘的因子里去，不存在前视——这正是它适合做横截面因子的原因。
    """
    out: dict[str, pd.DataFrame] = {}
    if lhb.empty:
        return {s["key"]: pd.DataFrame() for s in LHB_FACTOR_SPECS}

    # 同一 (day, code) 理论上只有一行；真出现重复时按最后一条，
    # 而不是把两条净买额相加——相加会凭空造出一个更大的数。
    lhb = lhb.drop_duplicates(subset=["trade_date", "ts_code"], keep="last")

    out["lhb_net_rate"] = _wide(lhb, "net_rate")


    pct = lhb.groupby("trade_date")["net_value"].rank(pct=True, method="average")
    out["lhb_net_value_pct"] = (
        lhb.assign(_pct=pct.to_numpy())
        .pivot(index="trade_date", columns="ts_code", values="_pct")
        .sort_index()
    )

    if seat.empty:
        out["lhb_seat_top1_share"] = pd.DataFrame()
        out["lhb_seat_n_pos"] = pd.DataFrame()
        return out

    positive = seat.loc[seat["net_value"] > 0]
    if positive.empty:
        out["lhb_seat_top1_share"] = pd.DataFrame()
        out["lhb_seat_n_pos"] = pd.DataFrame()
        return out

    grouped = positive.groupby(["trade_date", "ts_code"])["net_value"]
    agg = pd.DataFrame({
        "total": grouped.sum(),
        "top1": grouped.max(),
        "n_pos": grouped.count(),
    }).reset_index()
    # 正净买合计必然 > 0（只取了 >0 的行），但保留 where 以防上游给 NaN。
    agg["top1_share"] = agg["top1"] / agg["total"].where(agg["total"] > 0)
    agg["log_n_pos"] = np.log1p(agg["n_pos"])

    out["lhb_seat_top1_share"] = _wide(agg, "top1_share")
    out["lhb_seat_n_pos"] = _wide(agg, "log_n_pos")
    return out


def _one_word_mask(daily: pd.DataFrame) -> pd.DataFrame:
    """True where the **next** session opens at its limit — i.e. unbuyable.

    ``stk_limit`` only has 17 partitions here, so the band is rebuilt from the
    board rule (``symbols.limit_pct``) rather than read. The mask is aligned to
    the signal day T: ``mask[t, s]`` means "s could not be bought at t+1's open".
    """
    open_ = _wide(daily, "open")
    pre = _wide(daily, "pre_close")
    if open_.empty or pre.empty:
        return pd.DataFrame()
    open_ret = (open_ / pre.where(pre > 0) - 1.0).replace([np.inf, -np.inf], np.nan)
    bands = pd.Series(
        {str(code): float(limit_pct(str(code))) for code in open_.columns},
        dtype="float64",
    )
    at_limit = open_ret.ge(bands - _ONE_WORD_TOL)
    # T+1 的「开盘即涨停」对齐到 T
    return at_limit.shift(-1).astype("boolean").fillna(False).astype(bool)


def _residual(factor: pd.DataFrame, control: pd.DataFrame) -> pd.DataFrame:
    """Per-day cross-sectional residual of ``factor`` on ``control`` (rank space).

    This is the test that decides whether a finding is new or just a proxy for
    something already known. 龙虎榜净买额与当日涨幅的截面秩相关是 0.46（见
    ``ic_vs_pct``），而「涨停次日反转」是 A 股最稳的异象之一——不控涨幅就报
    IC，等于把旧异象重新包装成新发现，正是 P22 那次事故的形状。

    两个变量都先按行取平均秩，再做一元 OLS 取残差；残差随后的 RankIC 即
    Spearman 偏相关。样本不足 10 名的行留空，不插值。
    """
    idx = factor.index.intersection(control.index)
    cols = factor.columns.intersection(control.columns)
    if len(idx) == 0 or len(cols) == 0:
        return pd.DataFrame()
    f = factor.loc[idx, cols].to_numpy(dtype="float64")
    c = control.loc[idx, cols].to_numpy(dtype="float64")
    fr, cr = fbase._row_ranks(f), fbase._row_ranks(c)
    out = np.full(fr.shape, np.nan, dtype="float64")
    for i in range(fr.shape[0]):
        y, x = fr[i], cr[i]
        mask = np.isfinite(y) & np.isfinite(x)
        if int(mask.sum()) < 10:
            continue
        xv, yv = x[mask], y[mask]
        xm, ym = float(xv.mean()), float(yv.mean())
        den = float(((xv - xm) ** 2).sum())
        if den <= 0:
            continue
        beta = float(((xv - xm) * (yv - ym)).sum()) / den
        out[i, mask] = yv - (ym + beta * (xv - xm))
    return pd.DataFrame(out, index=idx, columns=cols)


def _cross_corr(factor: pd.DataFrame, control: pd.DataFrame) -> float | None:
    """Mean per-day rank correlation — how much of this factor IS the control."""
    idx = factor.index.intersection(control.index)
    cols = factor.columns.intersection(control.columns)
    if len(idx) == 0 or len(cols) == 0:
        return None
    f = factor.loc[idx, cols].to_numpy(dtype="float64")
    c = control.loc[idx, cols].to_numpy(dtype="float64")
    fr, cr = fbase._row_ranks(f), fbase._row_ranks(c)
    vals: list[float] = []
    for i in range(fr.shape[0]):
        y, x = fr[i], cr[i]
        mask = np.isfinite(y) & np.isfinite(x)
        if int(mask.sum()) < MIN_CROSS_SECTION:
            continue
        xv, yv = x[mask], y[mask]
        xm, ym = xv - xv.mean(), yv - yv.mean()
        den = float(np.sqrt((xm * xm).sum() * (ym * ym).sum()))
        if den > 0:
            vals.append(float((xm * ym).sum()) / den)
    return _f(float(np.mean(vals))) if vals else None


def _industry(sw_table: pd.DataFrame | None) -> pd.Series | None:
    if sw_table is None or sw_table.empty or "l1_name" not in sw_table.columns:
        return None
    deduped = sw_table.drop_duplicates(subset=["ts_code"], keep="last")
    return pd.Series(
        deduped["l1_name"].to_numpy(),
        index=deduped["ts_code"].astype(str).to_numpy(),
    )


def panel_lhb(store, *, start: str, end: str, sw_table: pd.DataFrame | None = None,
              horizons=HORIZONS, detail: str = "summary",
              neutralise: bool = True) -> dict[str, Any]:
    """Measure the four registered LHB factors over [start, end].

    ``neutralise=True`` keeps the size and industry exposures out of the IC when
    the sample allows it. With a ~73-name cross-section the industry demeaning
    can eat most of the degrees of freedom, so both the raw and the neutralised
    read are reported side by side and the gap is its own number
    (``ic_attribution_gap``) — the same convention as ``factors.panel``.
    """
    horizon_tuple = parse_horizons(horizons)
    meta: dict[str, Any] = {
        "ok": True,
        "topic": "factors_lhb",
        "read_only": True,
        "family": FAMILY,
        "window": {"start": str(start), "end": str(end)},
        "horizons": list(horizon_tuple),
        "min_cross_section": MIN_CROSS_SECTION,
        "min_ic_days": MIN_IC_DAYS,
        "n_factors_in_panel": len(LHB_FACTOR_SPECS),
        "family_scope": {
            "horizon_set": list(horizon_tuple),
            "note": (
                f"本族 = {len(LHB_FACTOR_SPECS)} 因子 × {len(horizon_tuple)} 期限"
                f" = {len(LHB_FACTOR_SPECS) * len(horizon_tuple)} 次检验，"
                "独立 BH 分母；与 factors 默认族（9×4=36）及 dense 族（45）"
                "的 q 值**不可直接比较**，也不能拿本族幸存者去推翻别族的历史结论。"
            ),
        },
        "provenance": {
            "datasets": ["ths_lhb", "ths_lhb_seat", "daily"],
            "entry": "T+1 open",
            "exit": "T+h close",
            "benchmark": "same-day equal-weight cross-sectional mean over the FULL market (excess)",
            "sample_kind": "conditional: 当日登上龙虎榜的个股，不是全市场截面",
            "disclosure_timing": "龙虎榜收盘后披露，T 日因子到 T+1 开盘才可交易",
            "institutional_dimension": "not_measured",
            "institutional_note": (
                "org_net_rate / org_net_value 在 2026-11-12 的 62 行里 62 行为空，"
                "机构维度不做，缺口显式报出而不是用 0 填充，也不拿游资顶替"
            ),
        },
    }

    if start > end:
        return {**meta, "ok": False, "status": "bad_window", "error": "start 晚于 end"}

    frames = fbase._build_panel(store, start, end)
    if frames["daily"].empty:
        return {**meta, "ok": False, "status": "no_bars",
                "error": "窗口内没有日线；前向收益需要日线面板"}
    frames["daily"] = frames["daily"].assign(
        trade_date=frames["daily"]["trade_date"].astype(str))
    frames["basic"] = frames["basic"].assign(
        trade_date=frames["basic"]["trade_date"].astype(str)) if not frames["basic"].empty else frames["basic"]

    lhb = _read(store, "ths_lhb", start, end, _LHB_COLUMNS)
    seat = _read(store, "ths_lhb_seat", start, end, _SEAT_COLUMNS)
    # **只用当日榜（range_days == 1）。** 上游同一天会给同一只票同时挂 1 日、
    # 3 日、10 日、30 日四个榜，net_value 是各自窗口的累计值：德明利 20251027
    # 在 1 日榜净买 −1,508 万、3 日榜 −7.87 亿，差 50 倍。混在一起做横截面百分位，
    # 排名主要由"这行属于哪个窗口"决定，与资金强度无关——会造出一个假的显著因子。
    # 实取 19,433 行里 1/3/10/30 日榜分别为 13,365 / 5,866 / 147 / 55。
    if not lhb.empty and "range_days" in lhb.columns:
        span = pd.to_numeric(lhb["range_days"], errors="coerce")
        meta["range_days_all"] = {str(int(k)) if pd.notna(k) else "null": int(v)
                                  for k, v in span.value_counts(dropna=False).items()}
        kept = lhb.loc[span.eq(1)]
        meta["range_days_used"] = 1
        meta["rows_dropped_other_range"] = int(len(lhb) - len(kept))
        lhb = kept
    if not lhb.empty:
        n_before = len(lhb)
        lhb = lhb.drop_duplicates(subset=["trade_date", "ts_code"], keep="last")
        if len(lhb) != n_before:
            meta["deduplicated_rows"] = n_before - len(lhb)
    meta["availability"] = {
        "daily_rows": len(frames["daily"]),
        "lhb_rows": len(lhb),
        "seat_rows": len(seat),
        "lhb_days": int(lhb["trade_date"].nunique()) if not lhb.empty else 0,
    }
    if lhb.empty:
        return {**meta, "ok": False, "status": "no_lhb",
                "error": "窗口内没有龙虎榜分区；这是数据缺口，不是因子无效"}

    days = sorted(frames["daily"]["trade_date"].unique())
    if len(days) < 60:
        return {**meta, "ok": False, "status": "short_history",
                "error": f"仅 {len(days)} 个交易日 < 60；无法与前向收益对齐"}

    built = _lhb_factors(lhb, seat)
    fwd = _forward_returns(frames, horizons=horizon_tuple)
    unbuyable = _one_word_mask(frames["daily"])
    log_mcap = fbase._factors(frames).get("log_mcap", pd.DataFrame())
    industry = _industry(sw_table)
    # 当日涨幅是本族的头号混淆项：它既是上榜原因，也是「次日反转」这个
    # 已知异象的自变量。所有因子都要报与它的相关性，并在主期限上做正交读数。
    pct_wide = _wide(lhb, "pct") if "pct" in lhb.columns else pd.DataFrame()

    sample_note = _sample_composition(lhb)
    meta["sample_composition"] = sample_note

    rows: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    for spec in LHB_FACTOR_SPECS:
        key = spec["key"]
        raw = built.get(key, pd.DataFrame())
        if raw.empty:
            rows.append({**spec, "status": "unavailable",
                         "note": "所需数据列在窗口内不可读；不是因子无效"})
            continue
        raw = raw.sort_index()
        used = raw
        if neutralise:
            used = _neutralise(raw, log_mcap, industry)

        by_horizon: dict[str, Any] = {}
        decay: list[dict[str, Any]] = []
        primary_frame = fwd.get(horizon_tuple[0], pd.DataFrame())
        for h in horizon_tuple:
            target = fwd.get(h, pd.DataFrame())
            ic = _ic_series(used, target)
            stats = _ic_stats(ic, horizon=h)
            if h == horizon_tuple[0]:
                layers = _layer_returns(used, primary_frame)
                # 剔除 T+1 开盘即涨停后的同一个读数。样本只会变少，
                # 所以 n 一并报出来，读者能看出剔掉了多少。
                clean_target = target.mask(unbuyable) if not unbuyable.empty else target
                ic_clean = _ic_series(used, clean_target)
                clean_stats = _ic_stats(ic_clean, horizon=h)
                # 控制当日涨幅后的残差 IC：这个数才是「龙虎榜独有信息」的读数。
                # 若它归零，说明本因子只是涨幅的代理，不是新发现。
                if not pct_wide.empty:
                    resid = _residual(used, pct_wide)
                    ic_resid = _ic_series(resid, target) if not resid.empty else pd.Series(dtype="float64")
                else:
                    ic_resid = pd.Series(dtype="float64")
                resid_stats = _ic_stats(ic_resid, horizon=h)
            else:
                layers = {"layers": [], "monotonicity": None, "long_short": None,
                          "n_days": 0, "note": "分层、剔除与正交读数只算主期限；其它期限见 ic"}
                clean_stats = None
                resid_stats = None
            by_horizon[str(h)] = {
                "ic": stats,
                "layers": layers,
                "time_holdout": _time_holdout(ic) if h == horizon_tuple[0] else None,
                "ic_series_tail": [_f(v) for v in ic.tail(10)],
                "ex_unbuyable": clean_stats,
                "orthogonal_pct": resid_stats,
                "power": _power_note(ic, stats) if h == horizon_tuple[0] else None,
            }
            decay.append({"h": h, "ic_mean": stats["ic_mean"], "status": stats["status"]})
            if stats.get("p_value") is not None:
                tests.append({"factor": key, "horizon": h,
                              "p_value": stats["p_value"], "t_stat": stats["t_stat"]})

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
                                   if primary["ic_mean"] is not None and raw_mean is not None
                                   else None),
            "ic_vs_pct": _cross_corr(raw, pct_wide) if not pct_wide.empty else None,
            "by_horizon": by_horizon,
            "decay": decay,
        })

    n_tests = len(tests)
    adjusted = bh_adjust([t["p_value"] for t in tests]) if n_tests else []
    for t, q in zip(tests, adjusted if isinstance(adjusted, list) else []):
        t["p_adjusted"] = _f(q)
    survivors = sorted({t["factor"] for t in tests
                        if t.get("p_adjusted") is not None and t["p_adjusted"] < 0.05
                        and t.get("t_stat") is not None and abs(t["t_stat"]) > 3.0})

    out = {
        **meta,
        "multiple_testing": {
            "n_tests": n_tests,
            "family": FAMILY,
            "method": "Benjamini-Hochberg FDR",
            "survivors_q05_and_t3": survivors,
            "note": ("每个 (因子, 期限) 都是一次检验，期限轴与因子轴同属一个 FDR 家族；"
                     "BH 之后还要求 |t|>3 才算可继续研究。空名单是正常答案，"
                     "不是「这条路径被否掉」。"),
        },
        "rows": rows,
        "caveats": [
            "样本是条件样本：只含当日上榜股（median 73 只），IC 读的是上榜股内部的相对强弱",
            "51.4% 的上榜样本当日涨停、12.2% 跌停；T+1 open 入场对一字板不可执行，ex_unbuyable 是剔除后的读数",
            "RankIC 不等于可交易收益；未扣佣金、印花税、滑点与冲击",
            "席位名是上游外号（T王/温州帮等），不是营业部全名，同一资金可能拆多个席位，集中度会系统性低估",
            "中性化只有行业 demean + log 市值回归，不是 Barra；73 只的截面下行业 demean 会吃掉较多自由度，故 raw 与 neutral 并列报",
            "org_* 全空，机构维度未测",
            "|t|>3 只是研究参考线，不是判决；要变成仓位须另走 quant_hypothesis(result) 与门禁",
        ],
        "note": ("本层只读：不写 store、不登记假设、不改 trial 分母、不进 arena.edge、"
                 "不产生任何委托。"),
    }
    if detail == "summary" and len(rows) > 1:
        out["rows"] = [_slim(r) for r in rows]
        out["detail"] = "summary"
        out["detail_note"] = "逐期限明细见 detail=full 或 factors=<单个因子>"
    return out


def _power_note(ic: pd.Series, stats_block: dict[str, Any]) -> dict[str, Any]:
    """What IC this sample could have detected at |t| > 3.

    「没检出」只有在样本有能力检出时才是一条信息。这里给出那条线：
    用 HAC 有效标准误（``ic_mean / t_hac``，重叠持有期下比 iid 的
    ``std/sqrt(n)`` 宽）反推 |t|=3 对应的最小 IC。实测 IC 若远低于它，
    正确读法是「这个设计分辨不了这么小的效应」，不是「效应为零」。
    """
    ic_mean = stats_block.get("ic_mean")
    t_hac = stats_block.get("t_stat")
    n_days = int(len(ic) if ic is not None else 0)
    if not ic_mean or not t_hac or n_days < MIN_IC_DAYS:
        return {"status": "insufficient", "n_days": n_days}
    se_hac = abs(ic_mean / t_hac)
    min_ic_at_t3 = 3.0 * se_hac
    ratio = abs(ic_mean) / min_ic_at_t3 if min_ic_at_t3 > 0 else None
    return {
        "status": "measured",
        "n_days": n_days,
        "ic_std_iid": _f(float(ic.std(ddof=1))),
        "se_hac": _f(se_hac),
        "min_ic_at_t3": _f(min_ic_at_t3),
        "observed_ic": ic_mean,
        "observed_over_required": _f(ratio),
        "reading": ("observed_over_required < 1 表示实测效应小于本设计在 |t|>3 下"
                    "能分辨的最小值；这时『不显著』不能读成『没有效应』，"
                    "只能读成『这个样本量分辨不了』"),
    }


def _sample_composition(lhb: pd.DataFrame) -> dict[str, Any]:
    """How much of the sample is already at a limit — the thing that decides
    whether an IC here can be traded at all."""
    if lhb.empty or "pct" not in lhb.columns:
        return {"status": "not_measured"}
    pct = pd.to_numeric(lhb["pct"], errors="coerce").dropna()
    if pct.empty:
        return {"status": "not_measured"}
    return {
        "status": "measured",
        "n": len(pct),
        "mean_pct": _f(float(pct.mean())),
        "median_pct": _f(float(pct.median())),
        "share_at_limit_up": _f(float((pct >= 9.8).mean())),
        "share_at_limit_down": _f(float((pct <= -9.8).mean())),
        "share_quiet": _f(float((pct.abs() < 3).mean())),
        "note": "9.8 / −9.8 是主板 10% 档的近似线；创业板 20% 档会被低估，只作样本结构描述",
    }


def _slim(row: dict[str, Any]) -> dict[str, Any]:
    primary = (row.get("by_horizon") or {}).get(str(row.get("primary_horizon")), {})
    ic = primary.get("ic") or {}
    clean = primary.get("ex_unbuyable") or {}
    resid = primary.get("orthogonal_pct") or {}
    return {
        "key": row.get("key"),
        "label": row.get("label"),
        "status": row.get("status"),
        "hypothesis": row.get("hypothesis"),
        "ic_raw_mean": row.get("ic_raw_mean"),
        "ic_neutral_mean": row.get("ic_neutral_mean"),
        "ic_attribution_gap": row.get("ic_attribution_gap"),
        "ic_vs_pct": row.get("ic_vs_pct"),
        "t_stat_hac": ic.get("t_stat"),
        "p_value": ic.get("p_value"),
        "n_days": ic.get("n_days"),
        "monotonicity": (primary.get("layers") or {}).get("monotonicity"),
        "long_short": (primary.get("layers") or {}).get("long_short"),
        "ic_ex_unbuyable": clean.get("ic_mean"),
        "n_days_ex_unbuyable": clean.get("n_days"),
        "ic_orthogonal_pct": resid.get("ic_mean"),
        "t_orthogonal_pct": resid.get("t_stat"),
        "n_days_orthogonal": resid.get("n_days"),
        "min_ic_at_t3": (primary.get("power") or {}).get("min_ic_at_t3"),
        "observed_over_required": (primary.get("power") or {}).get("observed_over_required"),
        "time_holdout_sign_agrees": ((primary.get("time_holdout") or {})
                                     .get("sign_agrees")),
    }
