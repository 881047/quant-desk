"""逐帧日内特征：从**带 frame_id** 的 rt_quote 派生，零新数据源。

为什么这张表存在
----------------
P0/P1 解决了「意图可持久化、帧可回放」，但分析层仍以交易日为最小粒度。
本表把 `(day, frame_id, code)` 作为统计单元：同一收盘数、过程相反的两天，
在逐帧序列上必须能区分开。

数据来源与边界
--------------
* 唯一输入是 ``rt_quote`` 分区。``frame_id`` 由 ``snapshot.prune_frames`` 的
  上游 ``snapshot()`` 写入（P0-C），**旧分区没有这一列**。
* 缺列时返回显式 ``legacy_partition_no_frame_id``，**绝不静默拿
  ``snapshot_ts`` 或行号假装帧**：snapshot_ts 是每行报价自己的时刻，停牌股
  一整天停在 08:00，还原不出「某一批全市场」。
* 冷存上线（2026-11-07）之前的分区不可回补，这是永久缺口。

口径（钉死）
------------
1. 帧序按 ``frame_id``（= 实际 batch clock）排，**不按序号推时刻**；人工
   ``force_refresh`` 会插入非周期帧。5 分钟桶从帧时刻向下取整，不是第 n 帧。
2. ``shape`` 是 ``pct_chg`` 的**代理分档**，不是涨停池判定。真涨停态要以
   ``stk_limit`` / ``pool_tape`` 为准，本表不冒充。
3. ``forward_to_close_pct`` 是「该帧 → 当日最后一帧」的收益，用**实际最后一帧**
   而不是 15:00 的固定时刻；抓取提前停止时它就是停止时刻，不是收盘价。
4. 本模块是纯函数：不读文件、不写库、不产生委托、无 signal/score/ranking。
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

FRAME_ID_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})_\d{6}$")

DEPTH_COLUMNS = (
    "bid1_volume", "ask1_volume", "bid_volume_5", "ask_volume_5",
    "order_imbalance_1", "order_imbalance_5", "seal_change_rate",
    "open_times", "boards",
)

OUT_COLUMNS = (
    "day", "frame_id", "code", "frame_at", "bucket_5min", "shape",
    "last", "pct_chg", "volume_ratio", "amount",
    "frame_return_pct", "forward_to_close_pct", "snapshot_ts", "source",
    *DEPTH_COLUMNS,
)


def frame_bucket(frame_id: Any) -> str | None:
    """``HHMMSS_%f`` → 5 分钟桶标签（向下取整到整 5 分钟）。

    09:42:57 → ``0940``；10:00:00 → ``1000``。午休区间不产生帧，所以桶序列
    本来就不连续——按实际帧时间聚合，不按序号补洞。
    """
    match = FRAME_ID_RE.match(str(frame_id))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return f"{hour:02d}{minute // 5 * 5:02d}"


def frame_at(frame_id: Any) -> str | None:
    """帧时刻 ``HH:MM:SS``，只从文件名取，不读行里的 snapshot_ts。"""
    match = FRAME_ID_RE.match(str(frame_id))
    if not match:
        return None
    return f"{match.group(1)}:{match.group(2)}:{match.group(3)}"


def shape_of(pct_chg: Any) -> str:
    """pct_chg 代理分档。不是涨停池判定，见模块 docstring。"""
    try:
        value = float(pct_chg)
    except (TypeError, ValueError):
        return "unknown"
    if pd.isna(value):
        return "unknown"
    if value >= 9.8:
        return "limit_up_like"
    if value <= -9.8:
        return "limit_down_like"
    if value >= 3.0:
        return "strong"
    if value <= -3.0:
        return "weak"
    return "flat"


def _merge_depth(df: pd.DataFrame, tape: pd.DataFrame | None) -> tuple[pd.DataFrame, str, int]:
    """按 ``(frame_id, code)`` 关联五档量。绝不用 snapshot_ts 做跨表连接键。

    OBI 的分母为 0 时返回 None（cannot say），不填 0：两边都没挂单不是
    「买卖平衡」，是「这一刻没有可观测的盘口」。
    """
    columns = ("bid1_volume", "ask1_volume", "bid_volume_5", "ask_volume_5",
               "open_times", "boards")
    if tape is None or tape.empty:
        for column in DEPTH_COLUMNS:
            df[column] = None
        return df, "no_tape", 0
    if "frame_id" not in tape.columns:
        for column in DEPTH_COLUMNS:
            df[column] = None
        return df, "legacy_partition_no_frame_id", 0

    t = tape.copy()
    t["frame_id"] = t["frame_id"].astype(str)
    t["code"] = t["code"].astype(str) if "code" in t.columns else ""
    t = t[t["frame_id"].str.match(FRAME_ID_RE)].copy()
    for column in columns:
        if column not in t.columns:
            t[column] = None
    # 2026-11-12 实测：rt_quote 快照自己也带 bid1_volume / ask1_volume /
    # bid_volume_5 / ask_volume_5。直接 merge 同名列会变成 _x / _y，
    # merged["bid1_volume"] 抛 KeyError，整条 intraday_features 派生失败
    # （当天 17:30 日更就是这么把 gate 染红的）。重命名 tape 列后再合并：
    # tape 是加分项，命中不到时回退到快照自带的盘口，而不是静默填 0。
    renamed = {column: f"_tape_{column}" for column in columns}
    t = t.rename(columns=renamed)
    t = (t.loc[:, ["frame_id", "code", *renamed.values()]]
          .sort_values(["code", "frame_id"])
          .drop_duplicates(["frame_id", "code"], keep="last"))
    merged = df.merge(t, on=["frame_id", "code"], how="left")
    for column in columns:
        from_tape = pd.to_numeric(merged[renamed[column]], errors="coerce")
        if column in merged.columns:
            from_quote = pd.to_numeric(merged[column], errors="coerce")
            merged[column] = from_tape.fillna(from_quote)
        else:
            merged[column] = from_tape

    for suffix, bid, ask in (("1", "bid1_volume", "ask1_volume"),
                             ("5", "bid_volume_5", "ask_volume_5")):
        denom = merged[bid] + merged[ask]
        merged[f"order_imbalance_{suffix}"] = (
            (merged[bid] - merged[ask]) / denom).where(denom > 0)

    # 封单衰减：同一只票相邻两帧的买一量变化率。按实际帧序，不按序号推时刻。
    # 中间某帧没采到盘口时，用「最近一次有观测的帧」作基线（ffill），而不是
    # 让 shift 被 NaN 打断后把这一段判成不可测——那一帧本身就是没有观测。
    observed = merged.groupby("code")["bid1_volume"].ffill()
    prev = observed.groupby(merged["code"]).shift(1)
    merged["seal_change_rate"] = (
        (merged["bid1_volume"] - prev) / prev).where(prev > 0)
    # depth_rows 说的是「有多少帧真的从 pool_tape 拿到盘口」，不能把回退到
    # rt_quote 的那部分也算进去，否则 depth_available 会被虚报成 true。
    matched = int(pd.to_numeric(merged[renamed["bid1_volume"]],
                                errors="coerce").notna().sum())
    return merged, "ok", matched


def build_features(quotes: pd.DataFrame | None, day: str,
                   *, tape: pd.DataFrame | None = None) -> dict:
    """返回 ``{"ok", "status", "frame", "rows", "frames", "tape_status",
    "depth_available", "depth_rows", "note"}``。

    ``status`` ∈ ``ok / legacy_partition_no_frame_id / empty / no_source``。
    非 ok 时 ``frame`` 为 None，调用方必须把它当成「没有这一天的特征」，
    不许拿日线或别的基准顶替。``tape`` 缺 frame_id 不算失败，只是
    ``depth_available=False`` 并如实标 ``legacy_partition_no_frame_id``。
    """
    no_depth = {"tape_status": "no_tape", "depth_available": False, "depth_rows": 0}
    if quotes is None or quotes.empty:
        return {"ok": False, "status": "empty", "frame": None, "rows": 0,
                "frames": 0, **no_depth, "note": f"{day} 没有 rt_quote 分区。"}
    if "frame_id" not in quotes.columns:
        return {
            "ok": False, "status": "legacy_partition_no_frame_id", "frame": None,
            "rows": 0, "frames": 0, **no_depth,
            "note": (f"{day} 的 rt_quote 没有 frame_id（冷存/外键上线前的旧分区）。"
                     "不许用 snapshot_ts 或行号假装帧；这一天不可回补。"),
        }

    df = quotes.copy()
    df["frame_id"] = df["frame_id"].astype(str)
    df = df[df["frame_id"].str.match(FRAME_ID_RE)].copy()
    df = df.dropna(subset=["frame_id", "code"])
    if df.empty:
        return {"ok": False, "status": "no_source", "frame": None, "rows": 0,
                "frames": 0, **no_depth, "note": "分区里没有一行带合法 frame_id。"}

    for column in ("last", "pct_chg", "volume_ratio", "amount", "snapshot_ts", "source"):
        if column not in df.columns:
            df[column] = None
    df["code"] = df["code"].astype(str)
    df["day"] = str(day)
    df["frame_at"] = df["frame_id"].map(frame_at)
    df["bucket_5min"] = df["frame_id"].map(frame_bucket)
    df["shape"] = df["pct_chg"].map(shape_of)
    df["last"] = pd.to_numeric(df["last"], errors="coerce")
    # 0 不是价格：停牌或缺报价的行如果留着 0，前向收益会精确等于 -100%，帧收益
    # 也会从上一帧直接写成 -100%，两个假数字一起进研究均值。按项目口径
    # 「missing is null, never 0」统一改成缺失，而不是在输出端各自加一次守卫。
    df.loc[df["last"] <= 0, "last"] = float("nan")

    # 按 (code, 实际帧时间) 排序后去重：同一帧同一票只留一行。
    df = (df.sort_values(["code", "frame_id"])
            .drop_duplicates(["day", "frame_id", "code"], keep="last")
            .reset_index(drop=True))
    prev = df.groupby("code")["last"].shift(1)
    df["frame_return_pct"] = ((df["last"] / prev - 1) * 100).where(prev > 0)

    # 到「当日最后一帧」的前向收益。用实际最后一帧，不是 15:00 的固定时刻。
    by_time = df.sort_values("frame_id").drop_duplicates("code", keep="last")
    final_last = by_time.set_index("code")["last"]
    target = df["code"].map(final_last)
    df["forward_to_close_pct"] = ((target / df["last"] - 1) * 100).where(df["last"] > 0)

    enriched, tape_status, depth_rows = _merge_depth(df, tape)
    out = enriched.loc[:, list(OUT_COLUMNS)].copy()
    return {"ok": True, "status": "ok", "frame": out, "rows": len(out),
            "frames": int(out["frame_id"].nunique()),
            "tape_status": tape_status,
            "depth_available": tape_status == "ok" and depth_rows > 0,
            "depth_rows": depth_rows,
            "note": "逐帧特征；shape 是 pct_chg 代理分档，不是涨停池判定。"}
