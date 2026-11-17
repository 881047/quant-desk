"""短线 α 台账：把每天的研究候选记下来，到期用真实行情结算。

为什么需要一个独立脚本，而不是让 agent 每天现场做算术：台账里每一个数都要能在
事后复核。谁记的、按哪个版本的口径、当时预测多少、后来实际多少——这些必须是
同一段代码写出来的，不能是三次对话里三次不同的四舍五入。

它**不产生委托**。`quant_short_alpha` 是信息-only 的工具，这份台账是它的账本：
记录（signal）→ 结算（settle）→ 汇总（report）。买入与否、买多少，仍然只走
`/decision/propose + approve`，那两条门的记录在 paper.sqlite 里，不在这里。

用法::

    python scripts/short_alpha_ledger.py record --as-of 20261113
    python scripts/short_alpha_ledger.py settle --upto 20261114
    python scripts/short_alpha_ledger.py tick          # record + settle，定时任务用这个
    python scripts/short_alpha_ledger.py report

存储：``data/short_alpha/ledger.jsonl``，只追加，不改写。同一 ``as_of`` 只允许
一条 signal（重复跑不会多记）；结算是一条条独立的 settle，补跑只会追加更完整的腿，
汇总时按 (as_of, symbol) 取最后一笔。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service import paths  # noqa: E402
from service.trading import board, get_desk  # noqa: E402
from service.trading.sentiment import gates as sentiment_gates, read_snapshot  # noqa: E402
from service.trading.short_alpha import VERSION, analyze  # noqa: E402

LEDGER = "short_alpha/ledger.jsonl"
LEGS = ("open_1", "close_1", "close_2")
LEG_LABEL = {"open_1": "T+1 开盘", "close_1": "T+1 收盘", "close_2": "T+2 收盘"}
# 两个模块对同一件事用了两套键名：board 面板上是 excess_open_1 / excess_close_1 /
# excess_close_2，short_alpha 的证据块写成 t1_open / t1_close / t2_close。台账要
# 把"预测"和"实现"对齐，所以这一层映射必须只写一次。
HORIZON_KEY = {"open_1": "t1_open", "close_1": "t1_close", "close_2": "t2_close"}


def ledger_path() -> Path:
    return paths.data_dir() / LEDGER


# ------------------------------------------------------------------ 读写
def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            # 半行（写到一半断电）不值得让整本账读不出来；跳过并留证据。
            out.append({"kind": "corrupt", "raw": line[:200]})
    return out


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------ 记录
def _gates(day: str) -> dict:
    payload, _trade_date = read_snapshot(paths.data_dir() / "sentiment", day)
    block = (payload or {}).get("sentiment")
    out = sentiment_gates(block)
    return {
        "ok": out.get("ok"),
        "failed": out.get("failed"),
        "indeterminate": out.get("indeterminate"),
        "tone": (block or {}).get("tone"),
        "break_rate": (block or {}).get("break_rate"),
        "max_boards": (block or {}).get("max_boards"),
    }


def _compact(item: dict, *, kind: str) -> dict:
    """只留能复核的部分：预测、样本量、成本、一手金额、成交边界。"""
    evidence = item.get("evidence") or {}
    horizons = evidence.get("horizons") or {}
    base = item.get("base_rate") or {}
    execution = item.get("execution") or {}
    predicted = {}
    for leg in LEGS:
        block = horizons.get(HORIZON_KEY[leg]) or {}
        predicted[leg] = {
            "net_excess_mean": block.get("net_excess_mean"),
            "ci95": block.get("net_excess_mean_ci95"),
            "n": block.get("n"),
            "n_days": block.get("n_days"),
        }
    return {
        "kind": kind,
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "boards": item.get("boards"),
        "seal_ratio": item.get("seal_ratio"),
        "seal_window": item.get("seal_window"),
        "theme": item.get("reason"),
        "cost_bps": (horizons.get("t1_close") or {}).get("cost_bps"),
        "predicted": predicted,
        "base_rate": {
            "reseal_rate": base.get("reseal_rate"),
            "required": base.get("required_reseal_rate"),
            "gap_pp": base.get("gap_pp"),
            "n": base.get("n"),
            "n_days": base.get("n_days"),
        },
        "execution": {
            "one_lot_cny": execution.get("one_lot_cny"),
            "cash_free": execution.get("cash_free"),
            "fillability": execution.get("fillability_status"),
            "total_cost_bps": execution.get("total_round_trip_cost_bps"),
        },
        "counter_evidence": item.get("counter_evidence"),
    }


def record(desk, day: str, *, limit: int = 2, path: Path | None = None) -> dict:
    path = path or ledger_path()
    if any(r.get("kind") == "signal" and r.get("as_of") == day for r in _load(path)):
        return {"ok": True, "recorded": False, "as_of": day,
                "reason": "already_recorded"}

    result = analyze(desk, day, limit=limit)
    if not result.get("ok"):
        return {"ok": False, "recorded": False, "as_of": day,
                "error": result.get("error")}

    rows = [_compact(item, kind="candidate") for item in result.get("candidates") or []]
    rows += [_compact(item, kind="watch") for item in result.get("watch") or []]
    cash = next((r["execution"].get("cash_free") for r in rows
                 if r["execution"].get("cash_free") is not None), None)
    entry = {
        "kind": "signal",
        "as_of": day,
        "recorded_at": _now(),
        "version": result.get("version") or VERSION,
        "pit_safe": result.get("signal_pit_safe"),
        "gates": _gates(day),
        "account": {"cash_free": cash},
        "universe": (result.get("universe") or {}).get("excluded_reasons"),
        "rows": rows,
        "no_candidate_reason": result.get("no_candidate_reason"),
    }
    _append(path, entry)
    return {"ok": True, "recorded": True, "as_of": day, "rows": len(rows),
            "candidates": sum(1 for r in rows if r["kind"] == "candidate")}


# ------------------------------------------------------------------ 结算
def _net(gross: float | None, cost_bps: float | None) -> float | None:
    if gross is None:
        return None
    if cost_bps is None:
        return None
    return gross - cost_bps / 10_000.0


def open_signals(records: list[dict]) -> list[dict]:
    """还缺腿的 (signal, symbol) 组合；已补齐三腿的不再重复请求面板。"""
    latest: dict[tuple[str, str], dict] = {}
    for row in records:
        if row.get("kind") != "settle":
            continue
        latest[(row.get("as_of"), row.get("symbol"))] = row
    out = []
    for row in records:
        if row.get("kind") != "signal":
            continue
        for item in row.get("rows") or []:
            done = latest.get((row.get("as_of"), item.get("symbol"))) or {}
            if done.get("complete"):
                continue
            out.append({"as_of": row["as_of"], "item": item,
                        "settled": (done.get("legs") or {})})
    return out


def settle(desk, *, upto: str | None = None, path: Path | None = None,
           limit: int = 40) -> dict:
    path = path or ledger_path()
    upto = upto or desk.market.latest_bar_date()
    records = _load(path)
    pending = open_signals(records)[:limit]
    written, done, partial, failed = [], 0, 0, []
    for task in pending:
        symbol = task["item"].get("symbol")
        cost = task["item"].get("cost_bps")
        realized = board.realized_forward(desk, task["as_of"], symbol, upto=upto)
        if not realized.get("ok"):
            failed.append({"as_of": task["as_of"], "symbol": symbol,
                           "error": realized.get("error")})
            continue
        legs = {leg: _net(realized["gross_excess"].get(leg), cost) for leg in LEGS}
        merged = dict(task["settled"])
        for leg, value in legs.items():
            if value is not None:
                merged[leg] = {"net": value,
                               "gross": realized["gross_excess"].get(leg)}
            else:
                merged.setdefault(leg, None)
        complete = all(isinstance(merged.get(leg), dict) for leg in LEGS)
        _append(path, {
            "kind": "settle",
            "as_of": task["as_of"],
            "symbol": symbol,
            "settled_at": _now(),
            "upto": upto,
            "legs": merged,
            "complete": complete,
            "closes": {"close": realized.get("close"),
                       "next_open": realized.get("next_open"),
                       "next_close": realized.get("next_close"),
                       "next2_close": realized.get("next2_close")},
        })
        written.append({"symbol": symbol, "complete": complete})
        done += 1 if complete else 0
        partial += 0 if complete else 1
    return {"ok": True, "upto": upto, "checked": len(pending),
            "complete": done, "partial": partial, "failed": failed,
            "written": written}


# ------------------------------------------------------------------ 汇总
def fold(records: list[dict]) -> list[dict]:
    """把 signal + 若干次 settle 折成一行一票。"""
    settles: dict[tuple[str, str], dict] = {}
    for row in records:
        if row.get("kind") == "settle":
            settles[(row.get("as_of"), row.get("symbol"))] = row
    out = []
    for row in records:
        if row.get("kind") != "signal":
            continue
        for item in row.get("rows") or []:
            key = (row.get("as_of"), item.get("symbol"))
            done = settles.get(key) or {}
            legs = done.get("legs") or {}
            out.append({"as_of": row["as_of"], "kind": item.get("kind"),
                        "symbol": item.get("symbol"), "name": item.get("name"),
                        "boards": item.get("boards"),
                        "predicted": {leg: (item.get("predicted") or {}).get(leg, {})
                                      .get("net_excess_mean") for leg in LEGS},
                        "realized": {leg: (legs.get(leg) or {}).get("net")
                                     for leg in LEGS},
                        "complete": bool(done.get("complete"))})
    return out


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(rows: list[dict]) -> dict:
    out = {"signals": len(rows), "settled": 0, "by_kind": {}}
    for kind in ("candidate", "watch"):
        subset = [r for r in rows if r["kind"] == kind]
        block = {"n": len(subset)}
        for leg in LEGS:
            pairs = [(r["predicted"].get(leg), r["realized"].get(leg))
                     for r in subset if r["realized"].get(leg) is not None]
            block[leg] = {
                "n": len(pairs),
                "predicted_mean": _mean([p for p, _ in pairs if p is not None]),
                "realized_mean": _mean([a for _, a in pairs]),
                # 同号 = 方向对了。数值大小是另一件事，两者一起看。
                "hit_rate": (sum(1 for p, a in pairs
                                 if p is not None and (p > 0) == (a > 0)) / len(pairs))
                if pairs else None,
            }
        out["by_kind"][kind] = block
    out["settled"] = sum(1 for r in rows if r["complete"])
    return out


def report(path: Path | None = None, *, as_json: bool = False) -> dict:
    rows = fold(_load(path or ledger_path()))
    summary = summarize(rows)
    if as_json:
        return {"summary": summary, "rows": rows}
    return {"summary": summary, "rows": rows}


# ------------------------------------------------------------------ CLI
def _print_report(payload: dict) -> None:
    summary = payload["summary"]
    print(f"[ledger] 信号 {summary['signals']} 条，三腿结清 {summary['settled']} 条")
    for kind in ("candidate", "watch"):
        block = summary["by_kind"][kind]
        if not block["n"]:
            continue
        line = [f"  {kind:9s} n={block['n']:3d}"]
        for leg in LEGS:
            cell = block[leg]
            if not cell["n"]:
                line.append(f"{LEG_LABEL[leg]} 待结算")
                continue
            pred = cell["predicted_mean"]
            real = cell["realized_mean"]
            hit = cell["hit_rate"]
            line.append(f"{LEG_LABEL[leg]} 预测 {pred * 100:+.2f}% / 实现 {real * 100:+.2f}%"
                        f"（{cell['n']} 笔，同号 {hit:.0%}）")
        print("；".join(line))
    for row in payload["rows"][:12]:
        cells = " / ".join(
            "—" if row["realized"].get(leg) is None
            else f"{row['realized'][leg] * 100:+.2f}%"
            for leg in LEGS)
        print(f"    {row['as_of']} {row['symbol']} {row['kind']:9s} {cells}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="短线 α 台账：记录与结算")
    parser.add_argument("command", choices=("record", "settle", "tick", "report"))
    parser.add_argument("--as-of", dest="as_of", default=None, help="信号日 YYYYMMDD")
    parser.add_argument("--upto", default=None, help="结算读取日 YYYYMMDD")
    parser.add_argument("--limit", type=int, default=2, help="候选上限 0-2")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--ledger", default=None, help="账本路径（测试用）")
    args = parser.parse_args(argv)

    path = Path(args.ledger) if args.ledger else ledger_path()
    if args.command == "report":
        payload = report(path)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        else:
            _print_report(payload)
        return 0

    desk = get_desk()
    out: dict = {"ok": True}
    if args.command in ("record", "tick"):
        day = args.as_of or desk.market.latest_bar_date()
        out["record"] = record(desk, day, limit=max(0, min(args.limit, 2)), path=path)
    if args.command in ("settle", "tick"):
        out["settle"] = settle(desk, upto=args.upto, path=path)
    out["report"] = report(path)["summary"]

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        if "record" in out:
            rec = out["record"]
            if rec.get("recorded"):
                print(f"[record] {rec['as_of']} 记下 {rec['rows']} 条"
                      f"（候选 {rec['candidates']}）")
            else:
                print(f"[record] {rec['as_of']} 未写入：{rec.get('reason') or rec.get('error')}")
        if "settle" in out:
            st = out["settle"]
            print(f"[settle] upto={st['upto']} 检查 {st['checked']} 条，"
                  f"结清 {st['complete']}，待后续 {st['partial']}"
                  + (f"，失败 {len(st['failed'])}" if st["failed"] else ""))
        _print_report({"summary": out["report"], "rows": report(path)["rows"]})
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
