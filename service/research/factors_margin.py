"""两融因子的横截面读数：B 层的第二个可执行族（``family=ashare_margin``）。

为什么有这一族
--------------
龙虎榜族（``factors_lhb``）的结论是"0 幸存"，而功效诊断说明了那个 0 的含义：
254 天 × 52 只的截面只能分辨 |IC| ≥ 0.025 的效应，实测 0.011 只有门槛的 43%。
继续在龙虎榜上挖，分子不变、分母不变，欠功效不会自己变好。

两融是同一个方法第一次有分辨力的地方：449 个交易日（20250102–20261111）、
每日 median 4,268 只两融标的。IC 的标准误 ≈ √(4268/52) ≈ 9 倍更小。
而 ``margin_detail`` 在 ``breadth`` 里自述「未做横截面因子」——它从未进过 α 估计。

三条口径边界
------------
1. **不依赖 ``rzmre − rzche``**。``rzche`` 存在负值（min −1.13e9），融资偿还额
   为负在会计上说不通，说明上游有过口径调整或错记。主因子用
   ``rzye[t] / rzye[t-1] − 1``，余额之差天然等于净买入，绕开这个字段。
   净买入强度仍然报，但加 ``|值| > 1 置缺失`` 的守卫。

2. **余额变化率含涨幅成分**。融资余额 = 持股数 × 价格 × 折算率，价格涨余额就涨。
   所以这不是纯粹的"资金流入"，它是涨幅的影子。控制当日涨幅的残差 IC 是
   **主读数**，不是辅助读数。

3. **只覆盖两融标的**（4,268 / 全市场 5,300）。截面不是全市场，IC 的读法是
   「两融标的内部」，与 ``factors.py`` 默认族的全市场口径不可直接对比。

只读：不写 store、不登记假设、不改 trial 分母、不进 arena.edge、不产生委托。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

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
from service.research.factors_lhb import (
    _cross_corr,
    _one_word_mask,
    _power_note,
    _residual,
    _slim,
)
from service.research.registry import bh_adjust

MARGIN_FACTOR_SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": "margin_bal_chg_1",
        "label": "融资余额 1 日变化率",
        "definition": "rzye[t] / rzye[t-1] − 1；不经过 rzmre/rzche，绕开负值异常",
        "source": "margin_detail(rzye)",
        "hypothesis": 11,
    },
    {
        "key": "margin_bal_chg_5",
        "label": "融资余额 5 日变化率",
        "definition": "rzye[t] / rzye[t-5] − 1",
        "source": "margin_detail(rzye)",
        "hypothesis": 11,
    },
    {
        "key": "margin_buy_intensity",
        "label": "融资净买入强度",
        "definition": "(rzmre − rzche) / rzye；|值| > 1 置缺失，防上游口径错记",
        "source": "margin_detail(rzmre, rzche, rzye)",
        "hypothesis": 11,
    },
    {
        "key": "margin_short_share",
        "label": "融券余额占比",
        "definition": "rqye / rzye；rqye 为 0（占 34.7%）置缺失，不填 0",
        "source": "margin_detail(rqye, rzye)",
        "hypothesis": 11,
    },
)

FAMILY = "ashare_margin"

#: 合成因子。不属于默认 16 检验族——它是一个独立的诊断（`ashare_margin_composite`），
#: 用来分开「三因子同源」与「三因子各测各的」这两种解释。等权 rank 平均，
#: 不拟合权重、不选子集，所以它没有参数自由度。
COMPOSITE_KEY = "margin_composite"
COMPOSITE_SPEC: dict[str, Any] = {
    "key": COMPOSITE_KEY,
    "label": "融资杠杆合成因子",
    "definition": ("margin_bal_chg_1 / margin_bal_chg_5 / margin_buy_intensity 的"
                   "逐日截面平均秩等权平均；至少 2 个有值才合成"),
    "source": "margin_detail(rzye, rzmre, rzche) 三因子合成",
    "hypothesis": 12,
}

_MARGIN_COLUMNS = ("ts_code", "trade_date", "rzye", "rzmre", "rzche", "rqye")


def _read_margin(store, start: str, end: str) -> pd.DataFrame:
    try:
        frame = store.read("margin_detail", start=start, end=end,
                           columns=list(_MARGIN_COLUMNS))
    except Exception:  # noqa: BLE001 - missing partition set is a result
        return pd.DataFrame(columns=list(_MARGIN_COLUMNS))
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(_MARGIN_COLUMNS))
    out = frame.copy()
    out["trade_date"] = out["trade_date"].astype(str)
    out["ts_code"] = out["ts_code"].astype(str)
    return out.drop_duplicates(subset=["trade_date", "ts_code"], keep="last")


def _margin_factors(margin: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """One wide frame (day × symbol) per factor, from T-closed disclosures."""
    if margin.empty:
        return {s["key"]: pd.DataFrame() for s in MARGIN_FACTOR_SPECS}

    margin = margin.sort_values(["trade_date", "ts_code"])
    bal = _wide(margin, "rzye")

    out: dict[str, pd.DataFrame] = {}
    # 余额之比本身就是净买入 / 期初余额，不需要碰 rzmre/rzche。
    out["margin_bal_chg_1"] = (bal / bal.shift(1).where(bal.shift(1) > 0) - 1.0)
    out["margin_bal_chg_5"] = (bal / bal.shift(5).where(bal.shift(5) > 0) - 1.0)

    rzmre = _wide(margin, "rzmre")
    rzche = _wide(margin, "rzche")
    if not rzmre.empty and not rzche.empty:
        intensity = (rzmre - rzche) / bal.where(bal > 0)
        # |净买入| > 余额 意味着余额口径和流量口径至少有一个错了。留空，
        # 不截尾——截尾会把一个数据错误伪装成一个极值观测。
        out["margin_buy_intensity"] = intensity.where(intensity.abs() <= 1.0)
    else:
        out["margin_buy_intensity"] = pd.DataFrame()

    short = _wide(margin, "rqye")
    if not short.empty:
        # rqye == 0 是「没有融券余额」，不是「融券占比为 0」：前者是缺失信息，
        # 后者是一个会排到分位最左端的有意义观测。43.7% 的零值按缺失处理。
        share = short / bal.where(bal > 0)
        out["margin_short_share"] = share.where(short > 0)
    else:
        out["margin_short_share"] = pd.DataFrame()

    # 合成因子：逐日截面平均秩的等权平均。至少两个子因子有值才合成——
    # 拿一个因子的值冒充三个，会让「合成」在部分缺失的日子退化成单因子，
    # 而那正是需要它降噪的日子。
    parts = [out[k] for k in ("margin_bal_chg_1", "margin_bal_chg_5",
                              "margin_buy_intensity") if not out[k].empty]
    if len(parts) >= 2:
        idx = parts[0].index
        cols = parts[0].columns
        for part in parts[1:]:
            idx = idx.union(part.index)
            cols = cols.union(part.columns)
        ranked = [p.reindex(index=idx, columns=cols).rank(axis=1, pct=True)
                  for p in parts]
        stack = np.stack([r.to_numpy(dtype="float64") for r in ranked])
        finite = np.isfinite(stack)
        count = finite.sum(axis=0)
        total = np.where(finite, stack, 0.0).sum(axis=0)
        avg = np.where(count >= 2, total / np.maximum(count, 1), np.nan)
        out[COMPOSITE_KEY] = pd.DataFrame(avg, index=idx, columns=cols)
    else:
        out[COMPOSITE_KEY] = pd.DataFrame()

    return {k: v.replace([np.inf, -np.inf], np.nan) for k, v in out.items()}


def panel_margin(store, *, start: str, end: str, sw_table: pd.DataFrame | None = None,
                 horizons=HORIZONS, detail: str = "summary",
                 neutralise: bool = True,
                 factors: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Measure the registered margin factors over [start, end].

    ``factors=("margin_composite",)`` measures the composite alone, and the
    family name switches to ``ashare_margin_composite`` so its q-values are not
    read as if they came from the 16-test family.
    """
    horizon_tuple = parse_horizons(horizons)
    all_specs = [*MARGIN_FACTOR_SPECS, COMPOSITE_SPEC]
    known = {s["key"] for s in all_specs}
    requested = list(factors) if factors else [s["key"] for s in MARGIN_FACTOR_SPECS]
    unknown = [k for k in requested if k not in known]
    active = [s for s in all_specs if s["key"] in requested]
    family = ("ashare_margin_composite" if requested == [COMPOSITE_KEY] else FAMILY)
    meta: dict[str, Any] = {
        "ok": True,
        "topic": "factors_margin",
        "read_only": True,
        "family": family,
        "window": {"start": str(start), "end": str(end)},
        "horizons": list(horizon_tuple),
        "min_cross_section": MIN_CROSS_SECTION,
        "min_ic_days": MIN_IC_DAYS,
        "n_factors_in_panel": len(active),
        "family_scope": {
            "horizon_set": list(horizon_tuple),
            "note": (
                f"本族 = {len(active)} 因子 × {len(horizon_tuple)} 期限"
                f" = {len(active) * len(horizon_tuple)} 次检验，独立 BH 分母；"
                "与 ashare_daily(36) / ashare_lhb(16) / ashare_margin(16) / "
                "ashare_intraday(12) 的 q 值不可比。"
            ),
        },
        "provenance": {
            "datasets": ["margin_detail", "daily"],
            "entry": "T+1 open",
            "exit": "T+h close",
            "benchmark": "same-day equal-weight cross-sectional mean over the FULL market (excess)",
            "sample_kind": "两融标的（约 4,300 只 / 全市场 5,300），非两融股不在截面内",
            "disclosure_timing": "两融数据 T 日晚间披露，T+1 开盘入场合法",
            "rzche_anomaly": "rzche 存在负值（min −1.13e9），故主因子不经过 rzmre/rzche",
        },
    }

    if unknown:
        return {**meta, "ok": False, "status": "unknown_factor",
                "error": f"未知因子 {unknown}", "available": sorted(known)}
    if start > end:
        return {**meta, "ok": False, "status": "bad_window", "error": "start 晚于 end"}

    margin = _read_margin(store, start, end)
    if margin.empty:
        return {**meta, "ok": False, "status": "no_margin",
                "error": "窗口内没有两融分区；这是数据缺口，不是因子无效"}

    frames = fbase._build_panel(store, start, end)
    if frames["daily"].empty:
        return {**meta, "ok": False, "status": "no_bars",
                "error": "窗口内没有日线；前向收益需要日线面板"}
    frames["daily"] = frames["daily"].assign(
        trade_date=frames["daily"]["trade_date"].astype(str))
    if not frames["basic"].empty:
        frames["basic"] = frames["basic"].assign(
            trade_date=frames["basic"]["trade_date"].astype(str))

    days = sorted(frames["daily"]["trade_date"].unique())
    per_day = margin.groupby("trade_date")["ts_code"].nunique()
    meta["availability"] = {
        "margin_rows": len(margin),
        "margin_days": int(margin["trade_date"].nunique()),
        "margin_codes_median": int(per_day.median()) if len(per_day) else 0,
        "daily_rows": len(frames["daily"]),
        "sessions": len(days),
    }
    if len(days) < 60:
        return {**meta, "ok": False, "status": "short_history",
                "error": f"仅 {len(days)} 个交易日 < 60；16 个检验的 BH 分母无法成形"}

    built = _margin_factors(margin)
    fwd = _forward_returns(frames, horizons=horizon_tuple)
    unbuyable = _one_word_mask(frames["daily"])
    log_mcap = fbase._factors(frames).get("log_mcap", pd.DataFrame())
    industry = None
    if sw_table is not None and not sw_table.empty and "l1_name" in sw_table.columns:
        deduped = sw_table.drop_duplicates(subset=["ts_code"], keep="last")
        industry = pd.Series(deduped["l1_name"].to_numpy(),
                             index=deduped["ts_code"].astype(str).to_numpy())
    # 涨幅是这一族的头号混淆项：融资余额随价格同向变动。
    pct_wide = _wide(frames["daily"], "close") / _wide(frames["daily"], "pre_close").where(
        _wide(frames["daily"], "pre_close") > 0) - 1.0

    rows: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    for spec in active:
        key = spec["key"]
        raw = built.get(key, pd.DataFrame())
        if raw.empty:
            rows.append({**spec, "status": "unavailable",
                         "note": "所需数据列在窗口内不可读；不是因子无效"})
            continue
        raw = raw.sort_index()
        used = _neutralise(raw, log_mcap, industry) if neutralise else raw

        by_horizon: dict[str, Any] = {}
        decay: list[dict[str, Any]] = []
        primary_frame = fwd.get(horizon_tuple[0], pd.DataFrame())
        for h in horizon_tuple:
            target = fwd.get(h, pd.DataFrame())
            ic = _ic_series(used, target)
            stats = _ic_stats(ic, horizon=h)
            if h == horizon_tuple[0]:
                layers = _layer_returns(used, primary_frame)
                clean_target = target.mask(unbuyable) if not unbuyable.empty else target
                clean_stats = _ic_stats(_ic_series(used, clean_target), horizon=h)
                resid = _residual(used, pct_wide) if not pct_wide.empty else pd.DataFrame()
                resid_stats = (_ic_stats(_ic_series(resid, target), horizon=h)
                               if not resid.empty else None)
            else:
                layers = {"layers": [], "monotonicity": None, "long_short": None,
                          "n_days": 0, "note": "分层、剔除与正交读数只算主期限"}
                clean_stats, resid_stats = None, None
            by_horizon[str(h)] = {
                "ic": stats,
                "layers": layers,
                "time_holdout": _time_holdout(ic) if h == horizon_tuple[0] else None,
                "ex_unbuyable": clean_stats,
                "orthogonal_pct": resid_stats,
                "power": _power_note(ic, stats) if h == horizon_tuple[0] else None,
            }
            decay.append({"h": h, "ic_mean": stats["ic_mean"], "status": stats["status"]})
            if stats.get("p_value") is not None:
                tests.append({"factor": key, "horizon": h,
                              "p_value": stats["p_value"], "t_stat": stats["t_stat"]})

        raw_ic = _ic_series(raw, primary_frame)
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
    # 残差口径下也过一遍校正：这才是「杠杆资金独有的信息」能不能站住的答案。
    resid_survivors = sorted({r["key"] for r in rows
                              if (r.get("by_horizon") or {})
                              .get(str(horizon_tuple[0]), {})
                              .get("orthogonal_pct", {}) is not None
                              and ((r["by_horizon"][str(horizon_tuple[0])]["orthogonal_pct"] or {})
                                   .get("t_stat") is not None)
                              and abs(r["by_horizon"][str(horizon_tuple[0])]["orthogonal_pct"]["t_stat"]) > 3.0})

    out = {
        **meta,
        "multiple_testing": {
            "n_tests": n_tests,
            "family": FAMILY,
            "method": "Benjamini-Hochberg FDR",
            "survivors_q05_and_t3": survivors,
            "survivors_orthogonal_pct_t3": resid_survivors,
            "note": ("BH 在 (因子, 期限) 上做；残差存活者只要求 |t|>3，"
                     "因为它是一次事后检查而非族内检验，不进 BH 分母——"
                     "所以它只能当『值得再看一眼』，不能当判决。"),
        },
        "rows": rows,
        "caveats": [
            "样本是两融标的（约 4,300 / 5,300），非两融股不在截面内；与默认族的全市场口径不可直接对比",
            "融资余额随价格同向变动，余额变化率天然含涨幅成分，orthogonal_pct 是主读数",
            "rzche 存在负值（min −1.13e9），主因子绕开 rzmre/rzche；买入强度因子有 |值|>1 守卫",
            "rqye 有 34.7% 为零值，按缺失处理而不是填 0——0 会把它们排到分位最左端",
            "RankIC 不等于可交易收益；未扣佣金、印花税、滑点与冲击",
            "中性化只有行业 demean + log 市值回归，不是 Barra",
            "|t|>3 只是研究参考线；要变成仓位须另走 quant_hypothesis(result) 与门禁",
        ],
        "note": ("本层只读：不写 store、不登记假设、不改 trial 分母、"
                 "不进 arena.edge、不产生任何委托。"),
    }
    if detail == "summary" and len(rows) > 1:
        out["rows"] = [_slim(r) for r in rows]
        out["detail"] = "summary"
        out["detail_note"] = "逐期限明细见 detail=full"
    return out
