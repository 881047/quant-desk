"""变了什么：把两天的可引用数字相减，而不是再念一遍今天的状态。

叫醒场景问的第一个问题不是"现在什么状态"，是"和上次相比，什么变了"。
`/watch` 原来只回答"有没有越过阈值"——它说明有事发生，不说明发生了什么；
`/facts` 说明今天有什么，不说明与昨天差在哪。

两条实现约定：

* **过去那一天优先读快照**（`reports/facts/<day>.json`）。重新算一遍会用**今天**的账本
  去渲染昨天的账户，那不是"昨天"，是"昨天形状、今天内容"。快照缺失时退化成只比市场层，
  并在 `basis` 里说出来——不静默把账户层换成一个错的数。
* **只报变化，不报状态**。没变的东西不进 `changes`，但 `unchanged` 给出条数，
  读者能判断"是不是压根没算"。
"""

from __future__ import annotations

import json
from pathlib import Path

#: 每项差多少才算"变了"。低于这个数的是噪声，写进去只会把真变化淹掉。
#: 单位跟着字段走：家数是"家"，收益率是小数（0.005 = 0.5 个点）。
MATERIAL = {
    "breadth.advancers": 20,
    "breadth.decliners": 20,
    "breadth.flat": 20,
    "breadth.equal_weight_return": 0.005,
    "breadth.median_return": 0.005,
    "pools.limit_up": 5,
    "pools.limit_down": 5,
    "pools.broken": 5,
    "affordable.in_cap": 3,
    "affordable.one_word": 3,
    "account.equity": 0.005,
    "account.positions": 0,
}

#: 行业名次移动多少位才值得单独列出来。31 个一级行业里挪 1-2 位每天都在发生。
RANK_MOVE = 3

_SKIP = {"rows", "members", "source", "basis", "note", "detail", "vendor_eastmoney",
         "missing", "blocks", "target", "today", "last_bar"}


def _snapshot(root: Path | None, day: str) -> dict | None:
    from service import settings

    base = Path(root) if root else Path(settings.ROOT)
    path = base / "reports" / "facts" / f"{day}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _scalars(block: dict, prefix: str, out: dict) -> None:
    for key, value in (block or {}).items():
        if key in _SKIP:
            continue
        if isinstance(value, dict):
            _scalars(value, f"{prefix}{key}.", out)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[f"{prefix}{key}"] = value


def _facts(desk, day: str) -> tuple[dict, bool]:
    """(facts, 是否来自快照)。快照缺失时现算，但账户层会失真。"""
    snap = _snapshot(getattr(desk, "root", None), day)
    if snap is not None:
        return snap, True
    from .facts import build_facts

    return build_facts(desk, day), False


def _sessions(market, since: str, until: str) -> int | None:
    try:
        days = [d for d in market.calendar.last_n(until, 400) or [] if since <= d <= until]
        return max(0, len(days) - 1)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def diff(desk, since: str, until: str | None = None) -> dict:
    until = until or desk.market.latest_bar_date()
    if not since:
        return {"ok": False, "error": "需要一个起点日期：since=YYYYMMDD",
                "example": {"since": until}, "note": "没有起点就没有差值。"}
    if since == until:
        return {"ok": True, "from": since, "to": until, "sessions": 0, "changes": [],
                "unchanged": 0, "note": "同一个日期，差值为空是正确结果。"}

    before, snapped = _facts(desk, since)
    after, _ = _facts(desk, until)

    left: dict = {}
    right: dict = {}
    for block in ("breadth", "pools", "affordable", "account"):
        _scalars(before.get(block) or {}, f"{block}.", left)
        _scalars(after.get(block) or {}, f"{block}.", right)

    changes = []
    unchanged = 0
    for key in sorted(set(left) & set(right)):
        old, new = left[key], right[key]
        if old is None or new is None or old == new:
            unchanged += 1
            continue
        delta = new - old
        limit = MATERIAL.get(key)
        if limit is None:
            material = True
        elif key == "account.equity":
            material = abs(delta) / old >= limit if old else True
        else:
            material = abs(delta) >= limit
        changes.append({
            "key": key, "from": round(old, 6), "to": round(new, 6),
            "delta": round(delta, 6), "material": material,
        })
    changes.sort(key=lambda row: (not row["material"], -abs(row["delta"])))

    return {
        "ok": True,
        "from": since,
        "to": until,
        "sessions": _sessions(desk.market, since, until),
        "changes": changes,
        "unchanged": unchanged,
        "industries": _industry_moves(desk.market, since, until),
        "basis": ("对比的是 quant_facts 的同一批键；过去那一天"
                  + ("读的是 reports/facts 的快照" if snapped
                     else "没有快照，现算的——账户层可能反映的是今天，不要用")),
        "material_rule": {k: v for k, v in MATERIAL.items()},
        "note": ("只列变了的项；`unchanged` 是两边都有、且完全相同的键数。"
                 "material=false 表示变化幅度低于噪声阈值，仍然给出来是因为"
                 "它可能连着几天朝一个方向走。"),
    }


def _industry_moves(market, since: str, until: str) -> list[dict]:
    """名次挪了 3 位以上的行业。只有名次，不带涨跌幅——那是一天的状态。"""
    from .industry import panel

    try:
        a = {row["l1"]: row.get("rank") for row in panel(market, since)["rows"]}
        b = {row["l1"]: row.get("rank") for row in panel(market, until)["rows"]}
    except (KeyError, TypeError, ValueError):
        return []
    moves = []
    for name in sorted(set(a) & set(b)):
        if a[name] is None or b[name] is None:
            continue
        delta = b[name] - a[name]
        if abs(delta) >= RANK_MOVE:
            moves.append({"l1": name, "rank_from": a[name], "rank_to": b[name],
                          "delta": delta})
    moves.sort(key=lambda row: row["delta"])
    return moves
