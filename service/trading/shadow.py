"""离线影子重放：同一批 frozen decisions + PIT 数据，只产出观察行。

边界（比任何指标都重要）
------------------------
* **不写 book、不产生委托、不调用 approve/propose。** 影子引擎只回答
  「同一批决定、同一份当时可见的数据，会落在什么状态上」。
* 不读未来帧：帧可见性交给 ``replay.visible_frames`` 按 ``decision_ts`` 截断，
  ``dropped_future`` 逐条留在结果里供 G2 查前视。
* 缺失 provenance 返回 ``UNSCORABLE`` 而不是中性填充——「没查到」不能渲染成
  「一致」。fail-closed 是影子模式的底线。

这不是回测：回测挑策略，影子挑**实现是否与生产逐项一致**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from . import replay as pit

UNSCORABLE = "UNSCORABLE"


def replay_decisions(
    frozen: Iterable[Mapping[str, Any]] | None,
    *,
    root,
    quote_at=None,
) -> dict:
    """把冻结的决定重放成 shadow 行。只读，返回结构不做任何写入。"""
    lookup = quote_at or (lambda day, ts, symbol: pit.quote_at(root, day, ts, symbol))
    rows: list[dict] = []
    for decision in frozen or []:
        day = str(decision.get("trade_date") or "")
        ts = decision.get("decision_ts")
        symbol = str(decision.get("symbol") or "")
        if not day or not ts or not symbol:
            rows.append({**dict(decision), "shadow_status": UNSCORABLE,
                         "reason": "missing_provenance"})
            continue
        try:
            fact = lookup(day, str(ts), symbol)
        except Exception as exc:  # noqa: BLE001 - 影子失败要可见，不能静默跳过
            rows.append({**dict(decision), "shadow_status": UNSCORABLE,
                         "reason": f"replay_failed:{type(exc).__name__}"})
            continue
        view = pit.visible_frames(root, day, str(ts))
        rows.append({
            **dict(decision),
            "shadow_status": "scored" if fact.price is not None else UNSCORABLE,
            "shadow_decision_price": fact.price,
            "shadow_frame_id": fact.frame_id,
            "shadow_basis": fact.basis,
            "source_ts": fact.source_ts,
            "acquisition_ts": fact.acquisition_ts,
            "dropped_future_frames": len(view["dropped_future"]),
            "reason": fact.reason,
        })
    return {
        "ok": True,
        "n": len(rows),
        "unscorable": sum(1 for r in rows if r["shadow_status"] == UNSCORABLE),
        "rows": rows,
        "note": "只读观察行；不写账本、不产生委托。UNSCORABLE 不得按中性一致处理。",
    }
