"""冻结 WorkBuddy 的短线 α 判断并做样本外结算；内核不替模型选票。

T 收盘只能做研究锚点：判断在收盘后登记，不能冒充当时已经成交。真正前瞻的
可执行评价从 T+1 开盘起算。日线只给机会代理，不给排队成交证明。
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from service import settings
from service.adapters.jsonio import write_json_atomic
from service.kernel.clock import SHANGHAI

from . import board
from .impact import estimate as impact_estimate
from .impact import round_trip_cost_bps
from .short_alpha import _f, _scenario, evidence_pack

VERSION = "short-alpha-oos-v2"
HORIZONS = (("t1_close", 1), ("t2_close", 2))
TERMINAL = frozenset({"settled", "no_fill", "invalid"})
MIN_DISTRIBUTION_N = 30


class TrackingConflict(RuntimeError):
    """A frozen judgment/hash conflict; callers must not repair it by rewriting."""


def _root(root: str | Path | None) -> Path:
    return Path(root) if root is not None else Path(settings.ROOT)


def _path(root: str | Path | None, day: str) -> Path:
    return _root(root) / "data" / "short_alpha_oos" / f"{day}.json"


def _day(value: str | None) -> str:
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        raise ValueError("day must be YYYYMMDD")
    datetime.strptime(text, "%Y%m%d")
    return text


def _now(now: datetime | None = None) -> datetime:
    return now.astimezone(SHANGHAI) if now is not None else datetime.now(SHANGHAI)


def _digest(frozen: dict) -> str:
    raw = json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _verify(record: dict) -> dict:
    frozen = record.get("frozen")
    if not isinstance(frozen, dict) or _digest(frozen) != record.get("frozen_sha256"):
        raise TrackingConflict("frozen judgment hash mismatch; settlement refused")
    return record


def _read(path: Path) -> dict:
    return _verify(json.loads(path.read_text(encoding="utf-8")))


def _records(root: str | Path | None) -> list[dict]:
    directory = _root(root) / "data" / "short_alpha_oos"
    if not directory.exists():
        return []
    return [_read(path) for path in sorted(directory.glob("*.json"))]


def _scenario_for_card(desk, card: dict, day: str) -> dict:
    existing = card.get("execution")
    if isinstance(existing, dict) and existing.get("impact_inputs"):
        return copy.deepcopy(existing)
    snap = desk.market.snapshot(card["symbol"], day)
    row = pd.Series({"close": float(snap.last) if snap is not None else 0.0,
                     "fill_kind": card.get("fill_kind")})
    return _scenario(desk, card["symbol"], day, row)


def _validate_judgment(judgment: dict, day: str) -> None:
    items = judgment.get("items") or []
    reviewed = judgment.get("reviewed_evidence") or []
    if not reviewed or len(reviewed) > 12 or len(set(reviewed)) != len(reviewed):
        raise ValueError("reviewed_evidence must contain 1..12 unique evidence ids")
    roles = [str(item.get("role") or "") for item in items]
    if roles.count("candidate") > 2 or roles.count("watch") > 3:
        raise ValueError("at most 2 candidates and 3 watch rows are allowed")
    if any(role not in {"candidate", "watch"} for role in roles):
        raise ValueError("item role must be candidate or watch")
    symbols = [str(item.get("symbol") or "") for item in items]
    if len(set(symbols)) != len(symbols):
        raise ValueError("the same symbol cannot appear twice")
    if roles.count("candidate") == 0 and len(str(judgment.get("no_candidate_reason") or "").strip()) < 3:
        raise ValueError("an explicit no-candidate judgment requires no_candidate_reason")
    if roles.count("candidate") > 0 and judgment.get("no_candidate_reason"):
        raise ValueError("no_candidate_reason must be empty when candidates exist")
    for ref in reviewed:
        if not str(ref).startswith(f"short-alpha-evidence:{day}:"):
            raise ValueError(f"evidence id does not belong to {day}: {ref}")
    for item in items:
        own = f"short-alpha-evidence:{day}:{item['symbol']}"
        refs = item.get("evidence") or []
        if own not in refs:
            raise ValueError(f"{item['symbol']} must cite its own evidence id {own}")
        if any(ref not in reviewed for ref in refs):
            raise ValueError("every item evidence id must also appear in reviewed_evidence")
        if len(str(item.get("thesis") or "").strip()) < 20:
            raise ValueError("each model judgment needs a thesis of at least 20 characters")
        if len(str(item.get("invalidation") or "").strip()) < 5:
            raise ValueError("each model judgment needs an invalidation condition")
        raw_weight = item.get("target_weight")
        if raw_weight is not None:
            try:
                weight = float(raw_weight)
            except (TypeError, ValueError) as exc:
                raise ValueError("target_weight must be a number in (0,1]") from exc
            if not np.isfinite(weight) or weight <= 0 or weight > 1:
                raise ValueError("target_weight must be a number in (0,1]")
    from . import strategy as strategy_mod

    cap = float(strategy_mod.REGISTRY["short_alpha"].max_weight)
    total_weight = sum(float(item["target_weight"]) for item in items
                       if item.get("target_weight") is not None)
    if total_weight > cap + 1e-9:
        raise ValueError(f"sum(target_weight)={total_weight:.6f} exceeds short_alpha cap {cap:.6f}")


def record_judgment(desk, judgment: dict, *, root: str | Path | None = None,
                    now: datetime | None = None) -> dict:
    """Freeze WorkBuddy's judgment once; the kernel never substitutes its own picks."""
    latest = _day(desk.market.latest_bar_date())
    desired = copy.deepcopy(judgment)
    signal_day = _day(desired.get("day") or latest)
    desired["day"] = signal_day
    desired["replay"] = bool(desired.get("replay", False))
    if signal_day > latest:
        raise ValueError(f"judgment day {signal_day} is after latest daily bar {latest}")
    path = _path(root, signal_day)
    if path.exists():
        existing = _read(path)
        if existing["frozen"].get("judgment") != desired:
            raise TrackingConflict(
                f"{signal_day} already has a frozen WorkBuddy judgment; different input refused")
        return {"ok": True, "created": False, "record": existing}
    _validate_judgment(desired, signal_day)
    reviewed = list(desired["reviewed_evidence"])
    symbols = [str(ref).rsplit(":", 1)[-1] for ref in reviewed]
    evidence = evidence_pack(desk, signal_day, symbols=symbols, limit=12)
    if not evidence.get("ok"):
        raise ValueError(f"evidence pack unavailable: {evidence.get('error')}")
    cards = {row["evidence_id"]: row for row in evidence.get("rows") or []}
    missing = [ref for ref in reviewed if ref not in cards]
    if missing:
        raise ValueError(f"evidence ids not in the signal-day universe: {missing}")
    stamp = _now(now).isoformat(timespec="seconds")
    prospective = bool(not desired["replay"] and signal_day == latest)
    items = []
    role_ranks = {"candidate": 0, "watch": 0}
    for raw in desired.get("items") or []:
        role = raw["role"]
        role_ranks[role] += 1
        card = cards[f"short-alpha-evidence:{signal_day}:{raw['symbol']}"]
        items.append({
            "item_id": f"{signal_day}:{role}:{raw['symbol']}",
            "symbol": raw["symbol"], "name": card.get("name") or "",
            "role": role, "rank": role_ranks[role],
            "thesis": raw["thesis"], "evidence": list(raw["evidence"]),
            "invalidation": raw["invalidation"],
            "target_weight": (float(raw["target_weight"])
                              if raw.get("target_weight") is not None else None),
            "execution": _scenario_for_card(desk, card, signal_day),
        })
    candidate_count = role_ranks["candidate"]
    frozen = {
        "version": VERSION,
        "signal_day": signal_day,
        "captured_at": stamp,
        "judgment_available_at": stamp,
        "latest_bar_at_capture": latest,
        "capture_kind": "prospective" if prospective else "replay",
        "prospective": prospective,
        "judgment_owner": "WorkBuddy",
        "kernel_substituted_selection": False,
        "judgment": desired,
        "evidence_pack": evidence,
        "entry_contract": {
            "research": "T_close_hypothetical_not_postclose_executable",
            "executable": "T+1_open_daily_opportunity_proxy_not_fill_proof",
            "horizons": ["T+1 close", "T+2 close"],
        },
        "items": items,
    }
    record = {
        "version": VERSION,
        "signal_day": signal_day,
        "judgment_status": "model_selection" if candidate_count else "explicit_no_candidate",
        "frozen_sha256": _digest(frozen),
        "frozen": frozen,
        "settlement_meta": {"status": "no_candidate" if not items else "pending",
                            "updated_at": None, "settled_at": None,
                            "latest_bar": latest},
        "settlements": [],
    }
    write_json_atomic(path, record)
    return {"ok": True, "created": True, "record": record,
            "order_created": False, "paper_ledger_modified": False}


def _label_row(labels: pd.DataFrame, symbol: str, day: str) -> pd.Series | None:
    hit = labels[(labels["ts_code"].astype(str) == symbol)
                 & (labels["trade_date"].astype(str) == day)]
    return None if hit.empty else hit.iloc[-1]


def _pending(track: str, horizon: str, reason: str) -> dict:
    return {"track": track, "horizon": horizon, "status": "pending",
            "reason": reason, "stock_return": None, "market_return": None,
            "cost_bps": None, "net_excess": None}


def _result(track: str, horizon: str, row: pd.Series, suffix: str,
            cost_bps: float, *, entry_day: str, exit_day: str) -> dict:
    stock = pd.to_numeric(pd.Series([row.get(f"session_ret_{suffix}")]), errors="coerce").iloc[0]
    market = pd.to_numeric(pd.Series([row.get(f"market_ret_{suffix}")]), errors="coerce").iloc[0]
    base = {"track": track, "horizon": horizon, "entry_day": entry_day,
            "exit_day": exit_day, "cost_bps": _f(cost_bps, 2)}
    if pd.isna(stock) or pd.isna(market) or not np.isfinite(cost_bps):
        return {**base, "status": "invalid", "reason": "missing_or_noncontiguous_or_corporate_action_label",
                "stock_return": None, "market_return": None, "net_excess": None}
    return {**base, "status": "settled", "reason": None,
            "stock_return": _f(stock), "market_return": _f(market),
            "net_excess": _f(float(stock) - float(market) - cost_bps / 10_000.0)}


def _open_cost(desk, symbol: str, day: str, price: float, execution: dict) -> dict:
    qty = int(execution.get("scenario_qty") or 0)
    inputs = execution.get("impact_inputs") or {}
    budget = float(execution.get("scenario_budget_cny") or 0)
    if not execution.get("account_ok") or qty <= 0:
        return {"executable_proxy": False, "status": "no_fill_account_scenario"}
    if qty * price > budget + 0.01:
        return {"executable_proxy": False, "status": "no_fill_account_budget_exceeded",
                "qty": qty, "scenario_budget_cny": _f(budget, 2)}
    try:
        adv = float(inputs["adv_cny"])
        sigma = float(inputs["sigma_daily"])
        instrument = desk.market.instrument(symbol, day)
        fee = round_trip_cost_bps(qty * price, instrument)
        impact = impact_estimate(qty, price, adv, sigma)
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"executable_proxy": False, "status": "invalid_frozen_cost_inputs"}
    total = float(fee["total_bps"]) + 2.0 * float(impact.impact_bps)
    return {
        "executable_proxy": True, "status": "open_auction_opportunity_proxy",
        "qty": qty, "entry_price": _f(price, 3), "scenario_budget_cny": _f(budget, 2),
        "cost_bps": _f(total, 2), "cost_cny": _f(qty * price * total / 10_000.0, 2),
        "cost_inputs_as_of": "frozen_at_T_close",
        "note": "开盘价低于涨停且成交量非零，只证明日线机会代理；不是委托或成交回报",
    }


def _open_entry(desk, symbol: str, entry_day: str | None, execution: dict) -> dict:
    if not isinstance(entry_day, str) or not entry_day:
        return {"status": "pending", "reason": "T+1 daily bar not available",
                "executable_proxy": False}
    snap = desk.market.snapshot(symbol, entry_day)
    if snap is None:
        return {"status": "no_fill_missing_open_bar", "entry_day": entry_day,
                "executable_proxy": False}
    price = float(snap.open)
    base = {"entry_day": entry_day, "entry_price": _f(price, 3),
            "up_limit": _f(float(snap.up_limit), 3), "volume_shares": _f(snap.volume, 0)}
    if snap.suspended or snap.volume <= 0:
        return {**base, "status": "no_fill_suspended", "executable_proxy": False}
    if price <= 0:
        return {**base, "status": "invalid_open_price", "executable_proxy": False}
    limit = float(snap.up_limit)
    if limit > 0 and price >= limit - 0.000001 and float(snap.low) >= limit - 0.000001:
        return {**base, "status": "no_fill_open_one_word", "executable_proxy": False}
    if limit > 0 and price >= limit - 0.000001:
        return {**base, "status": "queue_required_fill_unknown", "executable_proxy": False,
                "note": "开盘涨停后开板也无法证明集合竞价排队成交，不能按开盘价计入"}
    return {**base, **_open_cost(desk, symbol, entry_day, price, execution)}


def _settle_record(desk, record: dict, labels: pd.DataFrame, latest: str,
                   now: datetime | None) -> dict:
    updated = copy.deepcopy(record)
    frozen = updated["frozen"]
    market_days = sorted(str(d) for d in labels["trade_date"].dropna().unique())
    signal_day = str(frozen["signal_day"])
    entry_day = next((d for d in market_days if signal_day < d <= latest), None)
    old_settlements = updated.get("settlements") or []
    old_by_id = {str(x.get("item_id")): x for x in old_settlements}
    settlements = []
    for item in frozen["items"]:
        symbol, day = item["symbol"], frozen["signal_day"]
        row = _label_row(labels, symbol, day)
        research_cost = float(item["execution"].get("total_round_trip_cost_bps") or np.nan)
        if row is None:
            outcomes = {name: {
                "research_t_close": _pending("research_t_close", name, "signal day daily bar unavailable"),
                "executable_t1_open": _pending("executable_t1_open", name, "T+1 daily bar unavailable"),
            } for name, _ in HORIZONS}
            open_entry = {"status": "pending", "executable_proxy": False}
        else:
            open_entry = _open_entry(desk, symbol, entry_day, item["execution"])
            outcomes = {}
            for horizon, sessions in HORIZONS:
                exit_key = "next_trade_date" if sessions == 1 else "next2_trade_date"
                exit_day = row.get(exit_key)
                if not isinstance(exit_day, str) or not exit_day:
                    outcomes[horizon] = {
                        "research_t_close": _pending("research_t_close", horizon,
                                                     f"T+{sessions} daily bar not available"),
                        "executable_t1_open": _pending("executable_t1_open", horizon,
                                                       f"T+{sessions} daily bar not available"),
                    }
                    continue
                outcomes[horizon] = {
                    "research_t_close": _result(
                        "research_t_close", horizon, row, f"close_{sessions}", research_cost,
                        entry_day=day, exit_day=exit_day),
                }
                if not open_entry.get("executable_proxy"):
                    outcomes[horizon]["executable_t1_open"] = {
                        "track": "executable_t1_open", "horizon": horizon,
                        "status": "pending" if open_entry.get("status") == "pending" else "no_fill",
                        "reason": open_entry.get("status"), "entry_day": open_entry.get("entry_day"),
                        "exit_day": exit_day, "stock_return": None, "market_return": None,
                        "cost_bps": None, "net_excess": None,
                    }
                else:
                    outcomes[horizon]["executable_t1_open"] = _result(
                        "executable_t1_open", horizon, row, f"open_to_close_{sessions}",
                        float(open_entry["cost_bps"]), entry_day=open_entry["entry_day"],
                        exit_day=exit_day)
        previous = old_by_id.get(item["item_id"], {})
        settlements.append({"item_id": item["item_id"], "symbol": symbol,
                            "role": item["role"], "open_entry": open_entry,
                            "outcomes": outcomes,
                            "first_settled_at": previous.get("first_settled_at")})
    flat = [(s, name, track, s["outcomes"][name][track])
            for s in settlements for name, _ in HORIZONS
            for track in ("research_t_close", "executable_t1_open")]
    if not settlements:
        status = "no_candidate" if updated.get("judgment_status") == "explicit_no_candidate" else "empty"
    elif all(result["status"] in TERMINAL for _, _, _, result in flat):
        status = "settled"
    elif any(result["status"] == "settled" for _, _, _, result in flat):
        status = "partial"
    else:
        status = "pending"
    stamp = _now(now).isoformat(timespec="seconds")
    changed = settlements != old_settlements
    old_meta = updated.get("settlement_meta") or {}
    first_stamp = old_meta.get("updated_at")
    for settled in settlements:
        if settled["first_settled_at"] is None and any(
                settled["outcomes"][h][t]["status"] == "settled"
                for h, _ in HORIZONS for t in ("research_t_close", "executable_t1_open")):
            settled["first_settled_at"] = first_stamp or stamp
            changed = True
    updated["settlements"] = settlements
    updated["settlement_meta"] = {
        "status": status, "latest_bar": latest,
        "updated_at": stamp if changed else old_meta.get("updated_at"),
        "settled_at": (old_meta.get("settled_at") or stamp) if status == "settled" else None,
    }
    return updated


def settle(desk, *, root: str | Path | None = None, day: str | None = None,
           dry_run: bool = False, now: datetime | None = None) -> dict:
    """到期后写结算字段；冻结哈希不符时整份拒绝，绝不修复性重写。"""
    records = _records(root)
    if day is not None:
        wanted = _day(day)
        records = [r for r in records if r["signal_day"] == wanted]
    if not records:
        return {"ok": True, "checked": 0, "changed": 0, "pending": 0,
                "settled": 0, "dry_run": dry_run}
    latest = _day(desk.market.latest_bar_date())
    start = min(r["signal_day"] for r in records)
    bars = desk.data.read("daily", start=start, end=latest,
                          columns=["ts_code", "trade_date", "open", "close", "pre_close"])
    if bars is None or bars.empty:
        raise ValueError("daily bars unavailable for short-alpha settlement")
    labels = board.session_returns(bars)
    changed = 0
    statuses = []
    for record in records:
        updated = _settle_record(desk, record, labels, latest, now)
        _verify(updated)
        status = updated["settlement_meta"]["status"]
        statuses.append(status)
        if updated != record:
            changed += 1
            if not dry_run:
                write_json_atomic(_path(root, record["signal_day"]), updated)
    return {"ok": True, "checked": len(records), "changed": changed,
            "pending": statuses.count("pending") + statuses.count("partial"),
            "settled": statuses.count("settled"), "dry_run": dry_run,
            "latest_bar": latest}


def _distribution(rows: list[dict]) -> dict:
    clean = [r for r in rows if r.get("status") == "settled"
             and r.get("net_excess") is not None]
    values = pd.Series([float(r["net_excess"]) for r in clean], dtype=float)
    days = pd.Series([r["signal_day"] for r in clean], dtype=str)
    result = {"n": len(clean), "n_days": int(days.nunique()) if len(days) else 0,
              "net_excess_mean": None, "median": None, "win_rate": None,
              "net_excess_mean_ci95": [None, None],
              "ci_status": "no_sample", "ci_method": "day_block_bootstrap_event_weighted_mean",
              "insufficient": True, "minimum_n_for_distribution": MIN_DISTRIBUTION_N}
    if not len(values):
        return result
    interval = board._day_block_interval(values, days)
    result.update({
        "net_excess_mean": _f(values.mean()), "median": _f(values.median()),
        "win_rate": _f((values > 0).mean(), 4),
        "net_excess_mean_ci95": [_f(v) if v is not None else None for v in interval["ci95"]],
        "ci_status": interval["status"], "insufficient": len(values) < MIN_DISTRIBUTION_N,
    })
    return result


def _summary(items: list[dict]) -> dict:
    groups: dict = {}
    for role in ("candidate", "watch"):
        groups[role] = {}
        for track in ("research_t_close", "executable_t1_open"):
            groups[role][track] = {}
            for horizon, _ in HORIZONS:
                rows = [{"signal_day": i["signal_day"],
                         **i["outcomes"][horizon][track]}
                        for i in items if i.get("outcomes") and i["role"] == role]
                groups[role][track][horizon] = _distribution(rows)
    open_states: dict[str, int] = {}
    for item in items:
        state = str((item.get("open_entry") or {}).get("status") or "pending")
        open_states[state] = open_states.get(state, 0) + 1
    return {
        "signal_days": len({i["signal_day"] for i in items}),
        "signals": len(items),
        "candidate_signals": sum(i["role"] == "candidate" for i in items),
        "watch_signals": sum(i["role"] == "watch" for i in items),
        "open_entry_states": open_states,
        "groups": groups,
    }


def _compact_item(frozen_item: dict, settled: dict) -> dict:
    outcomes = settled.get("outcomes") or {}
    return {
        "symbol": frozen_item["symbol"], "name": frozen_item.get("name") or "",
        "role": frozen_item["role"], "rank": frozen_item["rank"],
        "thesis": frozen_item.get("thesis") or "",
        "evidence": list(frozen_item.get("evidence") or []),
        "invalidation": frozen_item.get("invalidation") or "",
        "target_weight": frozen_item.get("target_weight"),
        "open_entry": settled.get("open_entry") or {"status": "pending"},
        "outcomes": outcomes,
    }


def report(desk, *, root: str | Path | None = None, scope: str = "prospective",
           limit: int = 12, as_of: str | None = None) -> dict:
    """只读报告。prospective 与 replay 永远分组，不把回填混入真正 OOS。"""
    if scope not in {"prospective", "replay", "all"}:
        raise ValueError("scope must be prospective, replay or all")
    records = _records(root)
    if as_of is not None:
        day = _day(as_of)
        hit = next((r for r in records if r["signal_day"] == day), None)
        return {"ok": hit is not None, "version": VERSION, "record": hit,
                "error": None if hit is not None else "signal_day_not_frozen"}
    scoped = [r for r in records if scope == "all"
              or (r["frozen"]["capture_kind"] == scope)]
    scoped.sort(key=lambda r: r["signal_day"], reverse=True)
    total = len(scoped)
    scoped = scoped[:max(0, int(limit))]
    all_items = []
    days = []
    for record in scoped:
        frozen = record["frozen"]
        settled_by_id = {s["item_id"]: s for s in record.get("settlements") or []}
        items = [_compact_item(i, settled_by_id.get(i["item_id"], {}))
                 for i in frozen["items"]]
        for item in items:
            all_items.append({"signal_day": record["signal_day"],
                              "capture_kind": frozen["capture_kind"], **item})
        days.append({
            "signal_day": record["signal_day"], "captured_at": frozen["captured_at"],
            "capture_kind": frozen["capture_kind"], "prospective": frozen["prospective"],
            "judgment_owner": frozen.get("judgment_owner", "WorkBuddy"),
            "judgment_status": record.get("judgment_status"),
            "no_candidate_reason": (frozen.get("judgment") or {}).get("no_candidate_reason") or "",
            "reviewed_evidence": list((frozen.get("judgment") or {}).get("reviewed_evidence") or []),
            "status": record["settlement_meta"]["status"],
            "candidate_count": sum(i["role"] == "candidate" for i in frozen["items"]),
            "watch_count": sum(i["role"] == "watch" for i in frozen["items"]),
            "frozen_sha256": record["frozen_sha256"], "items": items,
        })
    summaries = {
        kind: _summary([i for i in all_items if i["capture_kind"] == kind])
        for kind in ("prospective", "replay")
    }
    for kind in ("prospective", "replay"):
        summaries[kind]["signal_days"] = len({
            r["signal_day"] for r in scoped if r["frozen"]["capture_kind"] == kind})
    latest_bar = desk.market.latest_bar_date()
    return {
        "ok": True, "version": VERSION, "as_of": latest_bar,
        "latest_bar_judgment_registered": any(
            r["signal_day"] == latest_bar and r["frozen"]["capture_kind"] == "prospective"
            for r in records),
        "scope": scope, "window": {"limit": limit, "total_matching_records": total,
                                   "returned_records": len(scoped)},
        "contract": {
            "information_provider": "quant_kernel",
            "judgment_owner": "WorkBuddy",
            "kernel_substituted_selection": False,
            "research_t_close": "T收盘研究锚点；盘后生成信号，不能称为实际买入",
            "executable_t1_open": "T+1开盘机会代理；一字/排队/缺bar不记成交也不记0收益",
            "prospective": "冻结时就是最新完整日线；历史回填一律replay，分组结算",
            "order_created": False, "paper_ledger_modified": False,
        },
        "summary": summaries,
        "days": days,
    }
