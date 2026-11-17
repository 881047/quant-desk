"""推导出的涨停结构，和上游那一条是不是同一件事——以及结论是否依赖口径。

P36 把炸板从「等上游补数据」改成「从 stk_limit 算」，样本从 243 天变成 4,081 天。

    python scripts/tape_calibration.py                # 校准 + 构成，写入 reports/
    python scripts/tape_calibration.py --check        # 只比，不写；用于定期复核
    python scripts/tape_calibration.py --robustness    # 再加一次成分口径的稳健性检查
    python scripts/tape_calibration.py --audit 'low|low' --era 2024-今   # 查一个结论

四节回答四个不同的问题：

1. ``calibrate`` — **分档对不对**。同一天两边是否落进同一档。相关 0.99 只说明
   两条线一起动，不说明这个。
2. ``_composition`` — **水平差在哪**。分档可以完全一致而水平差 15%；这一节给出
   每一档、每一年的上游收录率，于是「推导比上游多两成」能落到具体年份上。
3. ``_robustness`` — **换一个定义，年代结论还在不在**。前两节都用「全部封板」这一个
   口径；这一节按更制度稳定的口径（只主板 / 主板且非ST / 再剔首日）重算。
4. ``_audit`` — **一个桶的结论能不能被读成一个方向**。把前三节用到的三种修法
   （区间、符号、口径）合成一次运行，外加桶内外的成分对照。它不产出判决，
   只给 ``sides`` 的计数与每个口径的区间。

前三节问的是「这块数据可不可信」，第四节问的是「基于它的这句话能不能读成一个方向」。
分开是因为它们会独立失效：数据可信而不构成方向，或者方向稳但档位本身就不可比。

上游的封板率不是现成字段：`ths_limit_stat` 只有两天、而且 seal_rate 是空的，
真实来源是 `ths_limit_up`（涨停池）与 `ths_limit_break`（炸板池）的家数之比。
炸板池只留一年，所以第 1 节最多只有 243 天。

**窗口长度是第 1 节唯一的陷阱。** 推导序列的分位建在 252 日窗口上，上游撑不住 252 天，
两边被拿去和不同的参照期比较，同桶率会被窗口差异压低——不是序列差异。所以统一压到
上游撑得住的长度（120 日），并且把这个长度写进结果文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import numpy as np
import pandas as pd

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.research import tape_state

OUT = ROOT / "reports" / "tape_calibration.json"

_STORE: DatasetStore | None = None


def _store() -> DatasetStore:
    """The versioned store, so a calibration reads the partition a reader would.

    This script used to glob ``data/parquet/<dataset>/*.parquet``. That layout is
    the pre-versioning one: `retire_legacy_files.py` moved those files into
    ``data/parquet/.legacy/``, so the glob matched only files up to the migration
    and **silently dropped the current year** — the calibration then reported a
    panel that stopped in 2025 and gave no error at all.
    """
    global _STORE
    if _STORE is None:
        _STORE = DatasetStore(ROOT / "data", Registry(ROOT / "data" / "registry.sqlite"))
    return _STORE


def _load(dataset: str, cols: list[str]) -> pd.DataFrame:
    try:
        frame = _store().read(dataset, columns=cols)
    except (KeyError, ValueError):
        return pd.DataFrame(columns=cols)
    if frame.empty:
        return pd.DataFrame(columns=cols)
    frame["trade_date"] = frame["trade_date"].astype(str)
    return frame


def _vendor_pool() -> pd.DataFrame:
    """上游的涨停名单，股票级。构成分析要它，聚合序列不能替代。"""
    return _load("ths_limit_up", ["trade_date", "ts_code"])


def _vendor_series() -> pd.DataFrame:
    """上游的每日涨停家数与封板率。

    两条池子的日期覆盖不同（涨停池 1,430 天、炸板池 243 天），所以封板率只在
    两者都有的日子上有定义；涨停家数可以单独用满 1,430 天。
    """
    up = _vendor_pool()
    if up.empty:
        return pd.DataFrame()
    brk = _load("ths_limit_break", ["trade_date", "ts_code"])

    out = up.groupby("trade_date").size().rename("up_count").to_frame()
    if not brk.empty:
        out["brk_count"] = brk.groupby("trade_date").size()
        out["seal_rate"] = out["up_count"] / (out["up_count"] + out["brk_count"])
    out.index = out.index.astype(str)
    return out.sort_index()


def _derived() -> pd.DataFrame:
    path = ROOT / "data" / "cache" / "tape_20100101.parquet"
    if not path.exists():
        raise SystemExit("没有推导序列缓存；先跑一次 scripts/monitor.py 或直接调 cached_tape")
    frame = pd.read_parquet(path)
    frame.index = frame.index.astype(str)
    return frame


def render(result: dict) -> str:
    if not result.get("ok"):
        return f"[校准失败] {result.get('error')}"

    window = result.get("window")
    effective = result.get("effective_window", window)
    if result.get("full_window", True):
        header = f"窗口 {window} 日"
    else:
        header = (f"窗口 {window} 日，但实际最短历史只有 {effective} 日"
                  f"（分位从第 {effective} 个交易日才起算，同桶率不能与满窗读数直接比）")
    lines = [f"{header}（两边一致，否则同桶率会被窗口差异压低）", ""]
    for key, label, sample in (("seal", "封板率", "炸板池 + 涨停池，243 天"),
                               ("up", "涨停家数", "涨停池，1,430 天")):
        block = result.get(key)
        if not block or not block.get("n"):
            lines.append(f"{label}: 无重叠")
            continue
        lines += [
            f"{label}（{sample}）",
            f"  可比 {block['n']} 天  {block['start']}..{block['end']}",
            (f"  同桶 {block['agree']:.1%}   远桶 {block['far']:.1%}"
             f"   （随机基线 33.3% / 22.2%）"),
            (f"  尾档 {block['tail_agree']:.1%}（n={block['tail_n']}）"
             f"   中档 {block['mid_agree']:.1%}（n={block['mid_n']}）"),
            (f"  水平偏移 {block['level_shift']:+.4f}   中位比 {block['median_ratio']:.3f}"
             f"   秩相关 {block['rank_corr']:.3f}"),
        ]
        pairs = block.get("contingency") or {}
        messy = {k: v for k, v in pairs.items()
                 if k.split("|")[0] != k.split("|")[1]}
        if messy:
            top = sorted(messy.items(), key=lambda kv: -kv[1])
            lines.append("  分歧方向 " + "、".join(f"{k}×{v}" for k, v in top[:4]))
        lines.append("")
    lines.append("读法：尾档可靠、中档本来就模糊（33% 分位附近的日子两边都能翻）；"
                 "水平会平移，分位不会。")
    return "\n".join(lines)


def _panel() -> pd.DataFrame:
    """逐年读日线 + 涨停价，拼成一张带 band / board / sealed / broken / fwd_2d 的长表。

    一次扫描供构成与稳健性两节共用。两节问的是同一批事实的不同问题，
    重扫一遍只会让它们有机会给出互相矛盾的结论。
    """
    frames = []
    store = _store()
    years = sorted({rec.partition for rec in store.partitions("daily")
                    if rec.partition.isdigit()})
    for year in years:
        start, end = f"{year}0101", f"{year}1231"
        daily = store.read("daily", start, end, columns=["ts_code", "trade_date", "open",
                                                         "high", "close", "pre_close"])
        limits = store.read("stk_limit", start, end,
                            columns=["ts_code", "trade_date", "up_limit"])
        if daily.empty or limits.empty:
            continue
        for frame in (daily, limits):
            frame["trade_date"] = frame.trade_date.astype(str)
        merged = daily.merge(limits, on=["ts_code", "trade_date"], how="inner")
        merged = merged[(merged.up_limit > 0) & (merged.pre_close > 0)]
        if merged.empty:
            continue
        merged["band"] = [(tape_state.band_of(u, p) or "其他")
                          for u, p in zip(merged.up_limit, merged.pre_close, strict=True)]
        merged["board"] = merged.ts_code.map(tape_state.board_of)
        frames.append(merged)
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    # 上市首日：新股首日的涨幅上限是 44%（发行价 × 1.44），不是 10/20/30 里的任何一档，
    # 所以它们全落在「其他」里。而首日是一笔买不到的成交——不该和普通封板混在一档。
    listing = store.read("stock_basic", columns=["ts_code", "list_date"])
    listing["list_date"] = listing.list_date.astype(str)
    panel = panel.merge(listing, on="ts_code", how="left")
    panel["first_day"] = panel.trade_date == panel.list_date
    panel = panel.sort_values(["ts_code", "trade_date"], kind="mergesort")
    grouped = panel.groupby("ts_code", sort=False)
    panel["fwd_2d"] = grouped["close"].shift(-2) / grouped["open"].shift(-1) - 1
    panel["sealed"] = panel.close >= panel.up_limit - 1e-6
    panel["broken"] = (panel.high >= panel.up_limit - 1e-6) & ~panel.sealed
    return panel


def _composition(panel: pd.DataFrame, vendor: pd.DataFrame) -> dict:
    """推导封住的每一只票，上游同一天有没有收录——按涨停幅度分档、按年。

    这一节回答的是「水平差在哪里」，而 ``calibrate`` 回答「分档对不对」。两者不同：
    分档可以完全一致而水平差 15%。实测下来差在两处，方向相反：

    * **5% 板（ST）**：上游的收录率从 2021 年的 39% 升到 2026 年的 82%——
      不是「不收」，是**覆盖率在变**。所以「推导比上游多两成」这个说法在
      不同年份指的不是同一件事。
    * **30% 板（北交所）**：每一年都是 0%。那是一整类稳定排除，不是漂移。

    只报有上游数据的那几年，否则 2010-2020 会显示 0%（那时上游还没有池子），
    看起来像「完全不收录」，其实是「那时候没有数据」。
    """
    if vendor.empty or panel.empty:
        return {"ok": False, "note": "没有可比样本"}
    sealed = panel[panel.sealed]
    rows = []
    for year in sorted({d[:4] for d in vendor.trade_date.unique()}):
        chunk = sealed[sealed.trade_date.str[:4] == year]
        if chunk.empty:
            continue
        pool = vendor[vendor.trade_date.str[:4] == year][["trade_date", "ts_code"]]
        joined = chunk[["trade_date", "ts_code", "band"]].merge(
            pool.assign(hit=1), on=["trade_date", "ts_code"], how="left")
        joined["hit"] = joined.hit.fillna(0).astype(int)
        # 上游那一年覆盖了多少天。2020 年池子从 12 月才开始，按全年当分母会算出
        # 5.9%，看起来像「完全不收录」——那一年必须被标出来，不能直接读。
        vendor_days = pool.trade_date.nunique()
        derived_days = chunk.trade_date.nunique()
        partial = vendor_days < 0.6 * derived_days
        for label, part in joined.groupby("band"):
            rows.append({"year": int(year), "band": label, "seals": len(part),
                         "vendor_days": int(vendor_days),
                         "derived_days": int(derived_days),
                         "partial": bool(partial),
                         "in_vendor": round(float(part.hit.mean()), 3)})

    if not rows:
        return {"ok": False, "note": "重叠期内没有可比样本"}
    table = pd.DataFrame(rows)
    pivot = table.pivot(index="year", columns="band", values="in_vendor")
    partial = table.groupby("year")["partial"].any()
    listing = (sealed.groupby(sealed.trade_date.str[:4])["first_day"]
               .agg(seals="size", first_day="sum"))
    listing["share"] = (listing.first_day / listing.seals).round(4)
    return {
        "ok": True,
        "years": [int(y) for y in pivot.index],
        "partial_years": [int(y) for y in partial.index if partial[y]],
        "covered_by_vendor": {str(y): {b: pivot.loc[y, b] for b in pivot.columns
                                       if pd.notna(pivot.loc[y, b])}
                              for y in pivot.index},
        "listing_day": {str(y): {"seals": int(row.seals),
                                 "first_day": int(row.first_day),
                                 "share": float(row.share)}
                        for y, row in listing.iterrows()},
        "note": ("5% 板的收录率逐年上升（2021 39% → 2026 82%），30% 板（北交所）"
                 "始终为 0；所以「推导比上游多」的比例在不同年份不是同一件事。"
                 "标 * 的年份上游只覆盖了部分交易日（2020 年池子从 12 月才有），"
                 "那一行不能当成年度覆盖率读。"
                 "首日封板（幅度 44% 的新股首日）占比从 2015-19 的 1.3% 降到 0，"
                 "这是第三条漂移：它们落在「其他」里，而且是一笔买不到的成交。"),
    }


def render_composition(comp: dict) -> str:
    if not comp.get("ok"):
        return f"构成：{comp.get('note')}"
    lines = ["", "上游对推导封板的收录率（按幅度）："]
    bands = sorted({b for row in comp["covered_by_vendor"].values() for b in row},
                   key=lambda b: (b == "其他", b))
    lines.append("  年份   " + "  ".join(f"{b:>6s}" for b in bands))
    partial = {str(y) for y in comp.get("partial_years") or []}
    for year, row in comp["covered_by_vendor"].items():
        cells = "".join(f"  {row[b]:6.1%}" if b in row else "       —" for b in bands)
        mark = " *" if year in partial else "  "
        lines.append(f"  {year}{mark} {cells}")
    listing = comp.get("listing_day") or {}
    if listing:
        recent = list(listing.items())[-6:]
        lines.append("  首日封板占比  " + "  ".join(
            f"{y} {v['share']:.2%}" for y, v in recent))
    lines.append("  " + comp["note"])
    return "\n".join(lines)


def _tape_from(panel: pd.DataFrame, keep: pd.Series) -> pd.DataFrame:
    """按一份样本子集重建「涨停家数 / 封板率 / 分位 / 档 / 前向」。"""
    sub = panel[keep]
    per = sub.groupby("trade_date").agg(up=("sealed", "sum"),
                                        brk=("broken", "sum"))
    per["seal_rate"] = per.up / (per.up + per.brk).replace(0, np.nan)
    tape = pd.DataFrame({"up_count": per.up.astype(float),
                         "seal_rate": per.seal_rate})
    for name in ("seal_rate", "up_count"):
        tape[f"{name}_pct"] = tape_state._rolling_pct(tape[name])
        tape[f"{name}_tercile"] = tape[f"{name}_pct"].map(tape_state._tercile)
    tape["bucket"] = (tape.seal_rate_tercile.fillna("?") + "|"
                      + tape.up_count_tercile.fillna("?"))
    tape["fwd_seal2d"] = sub[sub.sealed].groupby("trade_date")["fwd_2d"].mean()
    return tape


# 四个口径，从宽到窄。窄的不是「更正确」，是**制度更稳定**：
# 只有主板在 2010-2026 全程都是 10%，创业板 2020-08 才改成 20%；
# 而首日封板（幅度 44% 的新股）是一笔买不到的成交。
DEFINITIONS = (
    ("全部（现状）", lambda panel: pd.Series(True, index=panel.index)),
    ("只主板", lambda panel: panel.board.isin(("沪主板", "深主板"))),
    ("主板且非ST", lambda panel: (panel.board.isin(("沪主板", "深主板"))
                                  & (panel.band != "5%"))),
    ("再剔首日", lambda panel: (panel.board.isin(("沪主板", "深主板"))
                                & (panel.band != "5%") & ~panel.first_day)),
)


def _robustness(panel: pd.DataFrame) -> dict:
    """年代结论是不是靠某个成分口径撑起来的。

    这是 ``calibrate`` 与 ``composition`` 都答不了的问题：那两节说「档位对不对」
    和「水平差在哪」，这一节说**「换一个定义，结论还在不在」**。

    实测各口径下的 ``low|low`` 段均：

        口径          2024-今                2015-19
        全部        -0.09% [-0.7,+0.5]    +2.62% [+2.0,+3.3]
        只主板      +0.11% [-0.4,+0.7]    +2.84% [+2.2,+3.5]
        主板且非ST  +0.17% [-0.5,+0.8]    +3.02% [+2.3,+3.7]
        再剔首日    +0.17% [-0.5,+0.8]    +2.87% [+2.2,+3.6]

    2015-19 在每一个口径下都远离零，2024-今 都横跨零——而桶标签只一致 74.8%。
    结论不依赖定义，这才是稳健；单口径下的一个点估计不是。

    四个口径从宽到窄，窄的不是「更正确」，是**制度更稳定**：只有主板在
    2010-2026 全程都是 10%（创业板 2020-08 才改成 20%），而首日封板
    （幅度 44% 的新股）是一笔买不到的成交。
    """
    if panel.empty:
        return {"ok": False, "note": "面板为空"}
    eras = ("2024-今", "2020-23", "2015-19", "2010-14")
    out: dict = {"ok": True, "eras": list(eras), "definitions": {}}
    tapes = {}
    for label, keep in DEFINITIONS:
        tape = _tape_from(panel, keep(panel))
        tapes[label] = tape
        tape["era"] = [tape_state.era_of(d) for d in tape.index]
        entry = {"days": len(tape[tape.bucket == "low|low"]), "by_era": {},
                 "all": tape_state.episode_ci(tape_state.episode_means(tape, "low|low"))}
        for era in eras:
            slice_ = tape[tape.era == era]
            episodes = tape_state.episode_means(slice_, "low|low")
            entry["by_era"][era] = {"ci": tape_state.episode_ci(episodes)}
        out["definitions"][label] = entry

    joint = pd.DataFrame({"a": tapes["全部（现状）"].bucket,
                          "b": tapes["只主板"].bucket}).dropna()
    joint = joint[(joint.a.str[0] != "?") & (joint.b.str[0] != "?")]
    out["bucket_overlap"] = {
        "days": len(joint),
        "whole": round(float((joint.a == joint.b).mean()), 3),
        "seal": round(float((joint.a.str[0] == joint.b.str[0]).mean()), 3),
        "count": round(float((joint.a.str[-1] == joint.b.str[-1]).mean()), 3),
    }
    out["note"] = ("结论若在所有口径下同向且区间同侧，才算不依赖定义。"
                   "2024-今 的区间在每一个口径下都横跨零，所以它不能被说成一个方向；"
                   "2020-23 只在最宽的那个口径下勉强越过零，同样不能。")
    return out


def render_robustness(rob: dict) -> str:
    if not rob.get("ok"):
        return f"稳健性：{rob.get('note')}"
    eras = rob["eras"]
    lines = ["", "年代结论是否依赖成分口径（low|low 段均 [95% 区间]）:"]
    lines.append("  口径          " + "  ".join(f"{e:>22s}" for e in eras))
    for label, entry in rob["definitions"].items():
        cells = []
        for era in eras:
            ci = entry["by_era"][era]["ci"]
            if not ci:
                cells.append(f"{'样本不足':>22s}")
                continue
            mark = "≈0 " if ci["ci_low"] <= 0 <= ci["ci_high"] else "   "
            cells.append(f"{ci['mean']:+6.2%}[{ci['ci_low']:+.1%},{ci['ci_high']:+.1%}]{mark}")
        lines.append(f"  {label:<11s}  " + "  ".join(cells))
    overlap = rob["bucket_overlap"]
    lines.append(f"  桶标签一致（全部 vs 只主板）：整桶 {overlap['whole']:.1%} / "
                 f"封板率档 {overlap['seal']:.1%} / 涨停家数档 {overlap['count']:.1%}"
                 f"（{overlap['days']} 天）")
    lines.append("  ≈0 = 区间横跨零，即该段不能被说成一个方向。")
    lines.append("  " + rob["note"])
    return "\n".join(lines)


def _audit(panel: pd.DataFrame, bucket: str, era: str | None = None) -> dict:
    """一个桶的结论能不能被读成一个方向——三件事一起查。

    这是 P37/P38/P39 三次修正合成的一次运行：

    * **区间**（P37/P38）：段级 bootstrap。段是观测单位，相邻同状态日共享前向窗口。
    * **符号与精度**（P38）：点估计横跨零就不能当方向，哪怕它看起来是负的。
    * **口径**（P39）：同一个问题在四个成分定义下重算。实测 2024-今 全部横跨零、
      2015-19 全部远离零，而桶标签只一致 74.8%——四分之三的标签会换，结论不动。
    * **成分**：桶里的日子与全样本的封板构成差多少。差得多就说明这个桶可能在
      回答另一个问题（游资的桶里如果 ST 板占三成，它测的不是游资的交易）。

    输出没有「可信 / 不可信」字段。``sides`` 只是计数，怎么读是人的事。
    """
    if panel.empty:
        return {"ok": False, "note": "面板为空"}
    tapes = {label: _tape_from(panel, keep(panel)) for label, keep in DEFINITIONS}
    if era:
        for label, tape in tapes.items():
            tapes[label] = tape[[tape_state.era_of(d) == era for d in tape.index]]

    base = tapes["全部（现状）"]
    result = tape_state.audit(base, bucket, {k: v for k, v in tapes.items()
                                             if k != "全部（现状）"})
    if not result.get("ok"):
        return result
    result["era"] = era
    result["composition"] = _composition_gap(panel, bucket, era)
    return result


def _composition_gap(panel: pd.DataFrame, bucket: str, era: str | None) -> dict:
    """桶内日子与全样本的封板构成差。用「当天封板里各档占比」比较，不用绝对家数。"""
    sealed = panel[panel.sealed].copy()
    if era:
        sealed = sealed[[tape_state.era_of(d) == era for d in sealed.trade_date]]
    if sealed.empty:
        return {"ok": False, "note": "没有封板样本"}
    tape = _tape_from(panel, pd.Series(True, index=panel.index))
    mask = (tape["bucket"] == bucket).reindex(sealed.trade_date.unique()).fillna(False)

    per_day = sealed.groupby("trade_date").agg(
        st=("band", lambda s: (s == "5%").mean()),
        wide=("band", lambda s: (s == "20%").mean()),
        bse=("band", lambda s: (s == "30%").mean()),
        ipo=("first_day", "mean"))
    flags = mask.reindex(per_day.index).fillna(False).astype(bool)
    inside = per_day[flags]
    outside = per_day[~flags]
    if inside.empty or outside.empty:
        return {"ok": False, "note": "桶内或桶外没有样本"}
    rows = {}
    for key, label in (("st", "5% 板"), ("wide", "20% 板"),
                       ("bse", "30% 板"), ("ipo", "首日封板")):
        rows[label] = {"in_bucket": round(float(inside[key].mean()), 4),
                       "elsewhere": round(float(outside[key].mean()), 4)}
    return {"ok": True, "days_in": len(inside), "days_out": len(outside),
            "shares": rows,
            "note": "占比是「当天封板里该档的份额」，不是家数——家数会被行情整体放大缩小。"}


def render_audit(res: dict) -> str:
    if not res.get("ok"):
        return f"audit：{res.get('note') or res.get('error')}"
    head = f"一个桶的结论能不能读成一个方向：{res['bucket']}"
    if res.get("era"):
        head += f"（{res['era']}）"
    lines = ["", head]

    def cell(entry: dict) -> str:
        ci = entry.get("episode")
        if not ci:
            return f"{'样本不足':>10s}（{entry['days']} 天）"
        return (f"{ci['mean']:+7.2%}[{ci['ci_low']:+.1%},{ci['ci_high']:+.1%}]"
                f" {entry['days']:5d}天/{ci['n']:4d}段")

    lines.append(f"  全部（现状）   {cell(res['base'])}")
    for label, entry in res["alternatives"].items():
        lines.append(f"  {label:<11s}   {cell(entry)}")

    sides = res["sides"]
    total = res["definitions"]
    parts = [f"{k} {v}" for k, v in sides.items() if v]
    lines.append(f"  sides（共 {total} 个口径）：" + " / ".join(parts))

    overlap = res.get("label_agreement") or {}
    if overlap:
        worst = min(overlap.items(), key=lambda kv: kv[1]["whole"])
        lines.append(f"  桶标签一致（vs 全部）：最差 {worst[0]} 整桶 {worst[1]['whole']:.1%}"
                     f" / 封板率 {worst[1]['seal']:.1%} / 家数 {worst[1]['count']:.1%}"
                     f"（{worst[1]['days']} 天）")

    comp = res.get("composition") or {}
    if comp.get("ok"):
        bits = "、".join(f"{k} {v['in_bucket']:.1%} vs {v['elsewhere']:.1%}"
                         for k, v in comp["shares"].items() if v["in_bucket"] or v["elsewhere"])
        lines.append(f"  桶内 vs 桶外（封板份额）：{bits}")
    lines.append("  " + res["note"])
    return "\n".join(lines)


def _check_rc(result: dict) -> int:
    """0 when both agreement rates clear 0.80, 1 otherwise.

    Defined once. It used to be written twice and the ``--audit`` branch returned
    early, so ``--check --audit 'low|low'`` exited 0 no matter how badly the two
    sources disagreed — a check that could not fail.
    """
    seal = (result.get("seal") or {}).get("agree")
    up = (result.get("up") or {}).get("agree")
    worst = min(v for v in (seal, up) if v is not None) if (seal or up) else None
    return 0 if worst is not None and worst >= 0.80 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=120,
                        help="两边的分位窗口；不能超过上游可用天数")
    parser.add_argument("--check", action="store_true",
                        help="只比不写；退出码 1 表示同桶率低于 0.80")
    parser.add_argument("--robustness", action="store_true",
                        help="再跑一次成分口径的稳健性检查（全量扫面板，约一分钟）")
    parser.add_argument("--audit", metavar="BUCKET", default="",
                        help="检查一个桶的结论能否读成一个方向，例如 'low|low'")
    parser.add_argument("--era", default="",
                        help="配合 --audit：只查某个年代，例如 2024-今")
    args = parser.parse_args(argv)

    derived = _derived()
    vendor = _vendor_series()
    result = tape_state.calibrate(derived, vendor, window=args.window)
    if result.get("ok"):
        result["derived_sessions"] = len(derived)
        result["vendor_sessions"] = len(vendor)
    print(render(result))

    if args.check and not (args.robustness or args.audit):
        return _check_rc(result)

    if not result.get("ok"):
        return 1

    panel = _panel()
    result["composition"] = _composition(panel, _vendor_pool())
    print(render_composition(result["composition"]))

    if args.robustness:
        result["robustness"] = _robustness(panel)
        print(render_robustness(result["robustness"]))

    if args.audit:
        # 审计是一次**查询**，不是常设测量：它问的是「这一句话现在能不能读成一个
        # 方向」，答案会随问的桶和年代变。写进快照会让两次运行互相覆盖，
        # 也会把一份带日期的测量变成「最后一次问的那个问题」。
        print(render_audit(_audit(panel, args.audit, args.era or None)))
        return _check_rc(result) if args.check else 0

    if args.check:
        return _check_rc(result)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n写入 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
