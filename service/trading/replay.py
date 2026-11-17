"""PIT 回放器：回答「这个决定当时看到了什么」，绝不载入决策之后的帧。

为什么单独一层
--------------
``frame.build`` 是**同一套代码的 runtime mode**（换输入源，不换引擎），负责把
一帧翻译成模型可读的盘口。本模块负责更窄的一件事：给定 ``decision_ts``，
从冷存 + 热目录的完整帧序列里挑出当时可见的最后一帧，把那只票的价格与
两个时钟一起冻结成事实，供票据回填与离线 parity 复核。

两个时钟，不许合并
------------------
* ``acquisition_ts``：文件名 ``HHMMSS_%f`` 的 batch clock，是**系统收到这一批**
  的时刻。PIT 的可见性判据用它——模型不可能使用尚未进入自身系统的数据。
* ``source_ts``：帧内 ``snapshot_ts``，是**每行报价自己**的时刻；停牌股一整天
  都停在 08:00。它回答「这个价有多旧」，不回答「这一批什么时候到的」。

拿 max(snapshot_ts) 当批次时刻会把停牌股的旧报价当成最新观测；拿文件名当
报价时刻则会抹掉逐行新鲜度。两者都留，谁也不顶替谁。

口径（钉死）
------------
1. ``frame_time > decision_ts`` 的帧**一律不载入**，宁可少给也不给未来。
2. 秒级 ``decision_ts`` 按「该秒末」比较（``frame.parse_as_of`` 的口径）：
   文件名微秒只用于同秒两帧防撞名，不是可分辨的经济时刻。
3. 取不到真实帧价就返回 ``basis="unavailable"`` / ``price=None``，**不许拿
   日线 ``snap.last`` 或别的基准假装**。``price_ref`` 与本字段互不相干。
4. 本模块**只读**：不写票据、不改账本、不产生委托。回填字段由调用方决定
   是否写回，保持「内核给事实、模型给判断、执行层给合法性」的分工。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import frame as frame_view


@dataclass(frozen=True)
class FrameClock:
    path: Path
    frame_id: str
    trade_date: str
    acquisition_ts: datetime


@dataclass(frozen=True)
class QuoteFact:
    symbol: str
    trade_date: str
    frame_id: str | None
    price: float | None
    source_ts: str | None
    acquisition_ts: str | None
    basis: str                 # "frame" | "unavailable"
    reason: str | None = None


def visible_frames(root: Path | str, day: str, decision_ts: str) -> dict:
    """不晚于 ``decision_ts`` 的帧序列，附带被丢弃的未来帧清单供审计。"""
    cutoff = frame_view.parse_as_of(decision_ts, day)
    if cutoff is None:
        raise ValueError(f"decision_ts 无法解析：{decision_ts!r}")
    all_paths = frame_view.replay_frames(root, day)
    visible: list[FrameClock] = []
    dropped: list[str] = []
    for path in all_paths:
        at = frame_view.frame_time(day, path.name)
        if at is not None and at <= cutoff:
            visible.append(FrameClock(path=path, frame_id=path.stem,
                                      trade_date=str(day), acquisition_ts=at))
        else:
            dropped.append(path.name)
    return {
        "ok": True,
        "day": str(day),
        "decision_ts": decision_ts,
        "cutoff": cutoff.isoformat(timespec="microseconds"),
        "frames": visible,
        "dropped_future": dropped,
        "total_frames": len(all_paths),
    }


def quote_at(root: Path | str, day: str, decision_ts: str,
             symbol: str) -> QuoteFact:
    """决策时刻可见的最后一帧里，这只票的价。取不到就如实说取不到。"""
    view = visible_frames(root, day, decision_ts)
    frames = view["frames"]
    if not frames:
        return QuoteFact(symbol, str(day), None, None, None, None,
                         "unavailable", "no_frame_at_or_before_decision_ts")
    latest = frames[-1]
    try:
        data = frame_view.load(latest.path)
    except (OSError, ValueError) as exc:
        return QuoteFact(symbol, str(day), latest.frame_id, None, None,
                         latest.acquisition_ts.isoformat(timespec="microseconds"),
                         "unavailable", f"frame_unreadable:{type(exc).__name__}")

    code = str(symbol).split(".")[0]
    rows = data[data["code"].astype(str) == code] if "code" in data.columns else data.iloc[0:0]
    if rows.empty:
        return QuoteFact(symbol, str(day), latest.frame_id, None, None,
                         latest.acquisition_ts.isoformat(timespec="microseconds"),
                         "unavailable", "symbol_not_in_frame")

    row = rows.iloc[0]
    price: float | None = None
    if "last" in data.columns:
        try:
            value = float(row["last"])
            # 停牌 / 缺报价常写成 0；0 不是价格。
            price = value if value > 0 else None
        except (TypeError, ValueError):
            price = None
    source_ts = (str(row["snapshot_ts"])
                 if "snapshot_ts" in data.columns and row.get("snapshot_ts") is not None
                 else None)
    acquisition_ts = latest.acquisition_ts.isoformat(timespec="microseconds")
    if price is None:
        return QuoteFact(symbol, str(day), latest.frame_id, None, source_ts,
                         acquisition_ts, "unavailable", "no_positive_price_in_frame")
    return QuoteFact(symbol, str(day), latest.frame_id, price, source_ts,
                     acquisition_ts, "frame", None)


def backfill_fields(ticket: Mapping[str, Any], root: Path | str) -> dict:
    """只读：给出可写回票据的字段，不写库。

    ``basis="frame"`` 只在真的从帧里读到价时写；否则保持 unavailable 并附
    原因。``ticket`` 必须带 ``decision_ts`` / ``trade_date`` / ``symbol``。
    """
    ts, day, symbol = (ticket.get("decision_ts"), ticket.get("trade_date"),
                       ticket.get("symbol"))
    empty = {"decision_quote": None, "decision_quote_basis": "unavailable",
             "decision_frame_id": None}
    if not ts or not day or not symbol:
        return {**empty, "reason": "ticket_missing_decision_keys"}
    fact = quote_at(root, str(day), str(ts), str(symbol))
    if fact.price is None:
        return {**empty, "reason": fact.reason}
    return {
        "decision_quote": str(fact.price),
        "decision_quote_basis": "frame",
        "decision_frame_id": fact.frame_id,
        "source_ts": fact.source_ts,
        "acquisition_ts": fact.acquisition_ts,
        "reason": None,
    }


def cross_check_shadow(observed: Any, fact: QuoteFact) -> dict:
    """比对 shadow 轨迹的 observed 与回放帧价。只报差异，不自动改数。

    差异不是「谁错了」的结论：轨迹记录的是意图求值时那一帧的观测，回放取的
    是决策时刻可见的最后一帧，两者本来就可能不是同一帧。它供 G3 parity 复核，
    不许用来悄悄覆盖任何一个来源。
    """
    if fact.price is None or observed is None:
        return {"comparable": False, "observed": observed, "replayed": fact.price}
    try:
        seen = float(observed)
    except (TypeError, ValueError):
        return {"comparable": False, "observed": observed, "replayed": fact.price}
    gap_bps = round((seen - fact.price) / fact.price * 10_000, 4)
    return {"comparable": True, "observed": seen, "replayed": fact.price,
            "gap_bps": gap_bps, "same_frame_price": abs(gap_bps) < 1e-9}
