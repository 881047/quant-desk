"""The daytime watchers' log: what fired, whether anyone looked, and what is true now.

Two watchers write here, and they are not the same thing:

* ``scripts/monitor.py`` runs every five minutes over the **live** account
  (``data/external.sqlite``) and mails a human. Its findings are somebody else's
  positions.
* ``scripts/snapshot_intraday.py`` captures the whole market every 30 s–3 min and,
  on every captured frame, runs the tripwire over the **paper** book. No mail —
  the reader is the model at its next wake.

Until 2026-11-05 the second one did not exist. The paper book was checked only
when the model happened to call ``/watch``, which is to say only at the moment it
was already awake and looking. A stop could be broken at 10:12 and nobody would
know until 13:35, when the price had long since moved on.

An earlier version of this file did two things with a finding: mailed a human and
appended a line to ``reports/external/<day>_alerts.jsonl`` that nothing read. So
the model waking at 13:35 had no way to learn that a stop was breached at 10:12 —
the rule fired, the mail went out, and the one reader in the system that could
have acted on it never saw the line. The 5-minute clock and the 30-minute clock
existed; nothing carried anything between them.

Three files per watcher, because one cannot answer all three questions:

* ``<day>_alerts.jsonl`` — append-only, one line per *transition*. It grows only
  when something happens, so its emptiness is ambiguous.
* ``<day>_heartbeat.json`` — rewritten by every completed run. Its whole job is
  to separate "nothing happened today" from "nobody was watching", which look
  identical in the first file. A missing heartbeat is a third answer again.
* ``<day>_watch.json`` — the findings that are true *now*, rewritten with them.
  The log says a stop was breached at 10:12; only this file says whether it still
  is. Without it, the 13:35 reader sees a critical line about a position that
  recovered at 10:14 and sells something that is fine.

``scope`` names the directory (``reports/<scope>``), which is where provenance
lives: a line copied between logs does not claim an account it never watched.

The base directory is a parameter rather than a constant because
``tests/trading/test_monitor.py`` already redirects it; a module that both writes
and reads this file has to honour that redirect or the two halves drift quietly
and only in tests.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from service.kernel.clock import SHANGHAI

#: 5 分钟一轮，连跑 5h30m。超过这个间隔而时段内没有任何完成记录，
#: 就不是"没事"，是监控停了——和 ``frame.FRAME_STALE_AFTER`` 同一个道理：
#: 阈值要按写入方的节奏定，不能按愿望定。
STALE_WATCH_SECONDS = 15 * 60

#: 抓取循环 30 秒一帧（首封/尾盘）到 3 分钟一帧（盘中），午休不抓但照样写心跳。
#: 十分钟＝最慢节奏下漏掉三帧，比这更久就不是"这一轮慢"，是循环死了。
STALE_FRAME_WATCH_SECONDS = 10 * 60

#: 一条 finding 消失多久才算真的解除。停损价往往就压在整数关口上，价格在它旁边
#: 抖动会让"破线／回到线上"以帧为单位来回翻。按帧数做迟滞会让阈值随抓取节奏变
#: （30 秒档 4 帧＝2 分钟，3 分钟档 1 帧），所以迟滞用的是**时间**，一个数一个意思。
CLEAR_AFTER_SECONDS = 120


def default_base(scope: str = "external") -> Path:
    """``<repo>/reports/<scope>`` — one directory per watcher."""
    return Path(__file__).resolve().parents[2] / "reports" / str(scope)


def _base(base: Path | str | None, scope: str) -> Path:
    """``base`` is an **exact directory**, not a root to be split by scope.

    This is deliberate, not a bug: ``tests/trading/test_monitor.py`` redirects a
    whole watcher with ``base=tmp_path``, and appending ``/paper`` here would
    silently move its three files somewhere else. ``scope`` only picks the
    default ``reports/<scope>`` directory when ``base`` is omitted.

    Measured 2026-11-07: ``_base(base, "shadow")`` and ``_base(base, "paper")``
    therefore resolve to the same file whenever ``base`` is given. Callers that
    hold an explicit base *and* need per-scope isolation must use
    :func:`scoped_base` instead. Do not "fix" this by concatenating scope.
    """
    return Path(base) if base is not None else default_base(scope)


def scoped_base(base: Path | str | None, scope: str) -> Path:
    """Give ``scope`` its own directory even when an explicit ``base`` is supplied.

    Unlike :func:`_base`, this treats ``base`` as a **root**. Production passes
    ``None`` and gets ``reports/<scope>``; tests or a caller holding a paper
    directory get ``<base>/<scope>``, so a shadow trace can never land in the
    paper account's alerts. Idempotent: a base already named after the scope is
    returned as-is rather than producing ``shadow/shadow``.
    """
    if base is None:
        return default_base(scope)
    root = Path(base)
    return root if root.name == str(scope) else root / str(scope)


def alerts_path(day: str, base: Path | str | None = None, *, scope: str = "external") -> Path:
    return _base(base, scope) / f"{day}_alerts.jsonl"


def heartbeat_path(day: str, base: Path | str | None = None, *,
                   scope: str = "external") -> Path:
    return _base(base, scope) / f"{day}_heartbeat.json"


def state_path(day: str, base: Path | str | None = None, *,
               scope: str = "external") -> Path:
    return _base(base, scope) / f"{day}_watch.json"


def append(day: str, records: Iterable[Mapping], base: Path | str | None = None, *,
           scope: str) -> int:
    """Append fired alerts. Returns how many lines were written."""
    rows = list(records)
    if not rows:
        return 0
    path = alerts_path(day, base, scope=scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def beat(
    day: str, *, scope: str, outcome: str = "ok", now: datetime | None = None,
    base: Path | str | None = None,
) -> None:
    """Record that one run **finished**. Overwrites; a heartbeat is not history.

    Written after the run, not before: a crash halfway through must leave the
    previous timestamp standing, because "last completed run was 10:15" is the
    whole signal. Stamping on entry would report every attempt as a success.

    ``scope`` has no default on the writers on purpose. ``watch.run`` once called
    ``reconcile(scope="paper")`` and ``beat()`` without a scope, so the state file
    landed in ``reports/paper`` and the heartbeat in ``reports/external`` — a
    watcher that had just judged the book looked, to every reader, like it had
    never run. Two files of one run must be unable to disagree about where they
    live, so a writer has to say whose log it is writing.
    """
    path = heartbeat_path(day, base, scope=scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(SHANGHAI)).isoformat(timespec="seconds")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"last_run_at": stamp, "outcome": outcome}, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, path)


def _seconds_since(stamp: str | None, now: datetime) -> int | None:
    if not stamp:
        return None
    try:
        return round((now - datetime.fromisoformat(stamp)).total_seconds())
    except ValueError:
        return None


def _load_state(day: str, base: Path, scope: str) -> dict:
    """The open set as of the last reconcile. A corrupt file reads as empty.

    Empty is the safe direction here: the next reconcile re-fires everything that
    is still true, which costs one duplicate line. Treating a corrupt file as
    "everything is fine" would cost the reader the whole afternoon.
    """
    path = state_path(day, base, scope=scope)
    if not path.is_file():
        return {"at": None, "open": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"at": None, "open": {}}
    opened = payload.get("open")
    return {"at": payload.get("at"), "open": opened if isinstance(opened, dict) else {}}


def _write_state(day: str, state: dict, base: Path, scope: str) -> None:
    path = state_path(day, base, scope=scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _fingerprint(finding: Mapping) -> str:
    """What makes two findings "the same one".

    标的 + 规则，不含严重度：同一个破线从 high 升到 critical 是**同一件事变严重**，
    要再写一行，但状态里仍然是同一条，不该在解除时冒出两条。
    """
    return f"{finding.get('code') or finding.get('rule') or '?'}|{finding.get('subject') or ''}"


def _record(entry: Mapping, stamp: str, event: str) -> dict:
    numbers = entry.get("numbers") or {}
    facts = [f"{key}={value}" for key, value in list(numbers.items())[:2]]
    detail = str(entry.get("detail") or "")
    return {
        "ts": stamp,
        "event": event,
        "rule": entry.get("code"),
        "severity": entry.get("severity"),
        "symbol": entry.get("subject"),
        "title": detail if event == "fired" else f"解除：{detail}",
        "facts": facts,
    }


def reconcile(
    day: str, findings: Iterable[Mapping], *, scope: str, at: datetime | None = None,
    base: Path | str | None = None, clear_after: float | None = None,
) -> dict:
    """Turn a fresh set of findings into log transitions, and record what is open.

    One run of this is one *observation*. Two rules:

    * a finding that was already open is not written again — otherwise a breach
      that lasts all afternoon would append a line every thirty seconds and push
      everything else out of the reader's last-8 window;
    * a finding that has been absent for ``clear_after`` seconds is written once
      as ``cleared``. Absent for less than that is treated as still open, and not
      written at all — the tape is noisy at a stop price and flapping is not news.

    Returns what this observation changed. Always writes the state file, even when
    nothing is open: "judged at 14:28, nothing open" is an answer.
    """
    at = at or datetime.now(SHANGHAI)
    stamp = at.isoformat(timespec="seconds")
    grace = CLEAR_AFTER_SECONDS if clear_after is None else float(clear_after)
    directory = _base(base, scope)
    previous = _load_state(day, directory, scope)["open"]

    current: dict[str, dict] = {}
    records: list[dict] = []
    for finding in findings:
        key = _fingerprint(finding)
        if key in current:
            # 同一个 (规则, 标的) 一次只算一条。tripwire 目前不会重复，但这里
            # 依赖它不重复就等于把"一行一条"的承诺押在别人的实现上。
            continue
        old = previous.get(key)
        entry = {
            "code": finding.get("code"),
            "severity": finding.get("severity"),
            "subject": finding.get("subject"),
            "detail": finding.get("detail"),
            "numbers": dict(finding.get("numbers") or {}),
            "first_at": (old or {}).get("first_at") or stamp,
            "last_seen": stamp,
        }
        current[key] = entry
        # A severity change is the same fact getting worse: the reader who saw
        # "high" this morning must learn it is now "critical".
        if old is None or old.get("severity") != entry["severity"]:
            records.append(_record(entry, stamp, "fired"))

    cleared: list[dict] = []
    for key, old in previous.items():
        if key in current:
            continue
        since = old.get("absent_since") or stamp
        if _age_seconds(since, at) < grace:
            # 还在迟滞窗口里：算它仍然成立，但不写行。
            held = dict(old)
            held["absent_since"] = since
            current[key] = held
            continue
        cleared.append(_record(old, stamp, "cleared"))
    records.extend(cleared)

    append(day, records, directory, scope=scope)
    _write_state(day, {"at": stamp, "open": current}, directory, scope)
    return {
        "at": stamp,
        "written": len(records),
        "fired": [row for row in records if row["event"] == "fired"],
        "cleared": cleared,
        "open": len(current),
    }


def _age_seconds(stamp: str, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return 0.0


def open_marks(day: str, base: Path | str | None = None, *, scope: str) -> dict[str, dict]:
    """What the last reconcile left open, each with its ``first_at``.

    ``read`` answers the reader's question — what happened, is anyone watching.
    This answers the actor's: *how long has this been true*. The two are not the
    same and must not be conflated: an executor that sells on ``count > 0`` sells
    on the first frame that prints below a stop, which is the tape touching a
    round number rather than a breach.

    Keyed by fingerprint (``code|subject``) exactly as the file stores it, so a
    caller never has to reconstruct how a key is built.
    """
    return _load_state(day, _base(base, scope), scope)["open"]


def read(
    day: str, *, limit: int = 8, now: datetime | None = None,
    base: Path | str | None = None, scope: str = "external",
    stale_after: float | None = None, as_of: str | None = None,
) -> dict:
    """Read watcher state, optionally reconstructed at an explicit time cutoff.

    The append-only transition log is the historical source of truth. The current
    ``*_watch.json`` snapshot cannot answer a past ``open`` set, so ``as_of``
    replays transitions rather than leaking the latest snapshot. Heartbeats are
    overwritten and therefore cannot be reconstructed when their last timestamp
    is later than the cutoff; that limitation is reported explicitly.
    """
    now = now or datetime.now(SHANGHAI)
    cutoff: datetime | None = None
    if as_of:
        try:
            cutoff = datetime.fromisoformat(str(as_of))
        except ValueError:
            return {"ok": False, "available": False, "scope": scope,
                    "error": "bad_as_of", "as_of": str(as_of)}
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=SHANGHAI)
        else:
            cutoff = cutoff.astimezone(SHANGHAI)
        now = cutoff
    directory = _base(base, scope)
    heart = heartbeat_path(day, directory, scope=scope)
    watch: dict | None = None
    if heart.is_file():
        try:
            payload = json.loads(heart.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        stamp = payload.get("last_run_at")
        watch = {
            "last_run_at": stamp,
            "seconds_since": _seconds_since(stamp, now),
            "outcome": payload.get("outcome"),
        }
        if cutoff is not None:
            try:
                heart_at = datetime.fromisoformat(str(stamp))
                if heart_at.tzinfo is None:
                    heart_at = heart_at.replace(tzinfo=SHANGHAI)
            except (TypeError, ValueError):
                heart_at = None
            # The heartbeat file has no history. A later heartbeat proves only
            # that the watcher ran later, not whether it was alive at the cutoff.
            if heart_at is None or heart_at > cutoff:
                watch = None
        if watch is not None:
            since = watch["seconds_since"]
            threshold = _stale_after(scope) if stale_after is None else float(stale_after)
            # 收盘退出是正常结束，不是"没人看"。把它算成 stale，每个工作日 15:05
            # 之后这个字段恒为真——永远响的告警等于没有告警。
            watch["is_stale"] = bool(
                since is None or (since > threshold and watch["outcome"] != "closed")
            )
    else:
        watch = None

    path = alerts_path(day, directory, scope=scope)
    items: list[dict] = []
    rows: list[dict] = []
    fired = 0
    cleared = 0
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        if cutoff is not None:
            visible: list[dict] = []
            for row in rows:
                try:
                    row_at = datetime.fromisoformat(str(row.get("ts") or ""))
                    if row_at.tzinfo is None:
                        row_at = row_at.replace(tzinfo=SHANGHAI)
                except (TypeError, ValueError):
                    continue
                if row_at <= cutoff:
                    visible.append(row)
            rows = visible
        for row in rows:
            if row.get("event") == "cleared":
                cleared += 1
            else:
                fired += 1
        for row in rows[-max(1, int(limit)) :]:
            items.append(
                {
                    "ts": row.get("ts"),
                    "event": row.get("event") or "fired",
                    "rule": row.get("rule"),
                    "severity": row.get("severity"),
                    "symbol": row.get("symbol"),
                    "title": row.get("title"),
                    # 只留前两条事实：这里是"叫你去看一眼"，不是报警正文。
                    "facts": list(row.get("facts") or [])[:2],
                }
            )

    if cutoff is not None:
        reconstructed: dict[str, dict] = {}
        for row in rows:
            code = row.get("code") or row.get("rule")
            subject = row.get("subject") or row.get("symbol")
            key = f"{code or '?'}|{subject or ''}"
            if row.get("event") == "cleared":
                reconstructed.pop(key, None)
                continue
            previous = reconstructed.get(key) or {}
            facts = row.get("facts") or []
            numbers = dict(row.get("numbers") or {})
            if not numbers:
                for fact in facts:
                    if "=" in str(fact):
                        name, value = str(fact).split("=", 1)
                        numbers[name] = value
            reconstructed[key] = {
                "code": code,
                "severity": row.get("severity"),
                "subject": subject,
                "detail": row.get("title"),
                "numbers": numbers,
                "first_at": previous.get("first_at") or row.get("ts"),
                "last_seen": row.get("ts"),
            }
        state = {"at": (rows[-1].get("ts") if rows else (watch or {}).get("last_run_at")),
                 "open": reconstructed}
        open_basis = "reconstructed_from_transitions_at_cutoff"
    else:
        state = _load_state(day, directory, scope)
        open_basis = "current_watch_snapshot"
    opened = sorted(
        state["open"].values(),
        key=lambda item: (_SEVERITY_ORDER.get(item.get("severity"), 9),
                          item.get("code") or ""),
    )
    note = _note(watch, fired=fired, open_count=len(opened),
                 shown=len(items), open_at=state["at"], scope=scope)
    if cutoff is not None:
        note = (f"历史回放截到 {cutoff.isoformat(timespec='seconds')}；open 由该时点之前的"
                "fired/cleared 转换重建，不读取最新 watch 快照。" + note)
        if watch is None:
            note += " 该时点的 heartbeat 不可回补；watcher=null 在这里不表示监控从未运行。"
    return {
        "available": watch is not None,
        "historical": cutoff is not None,
        "as_of": cutoff.isoformat(timespec="seconds") if cutoff is not None else None,
        "open_basis": open_basis,
        "transitions_pit_safe": True if cutoff is not None else None,
        "heartbeat_pit_available": (watch is not None) if cutoff is not None else None,
        "watcher": watch,
        "count": fired + cleared,
        "fired": fired,
        "cleared": cleared,
        "items": items,
        "open": [
            {
                "code": entry.get("code"),
                "severity": entry.get("severity"),
                "subject": entry.get("subject"),
                "detail": entry.get("detail"),
                "numbers": entry.get("numbers") or {},
                "first_at": entry.get("first_at"),
                "last_seen": entry.get("last_seen"),
            }
            for entry in opened
        ],
        "open_at": state["at"],
        "scope": scope,
        "scope_note": _SCOPE_NOTE.get(scope),
        "basis": f"{path}",
        "note": note,
    }


_SEVERITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "warn": 2, "info": 3}

#: 读者最需要的第一句话：这份日志是谁的持仓。两个账户的票可以完全不同
#: （2026-11-05：实盘 002185、模拟台空仓），不写清楚会把别人的持仓读成自己的。
_SCOPE_NOTE = {
    "external": ("属于实盘账户（scripts/monitor.py 每 5 分钟一轮，触发会发邮件），"
                 "不是本模拟台的 paper 持仓。"),
    "paper": ("就是本模拟台的 paper 账户本身：抓取循环每落一帧就过一遍 tripwire，"
              "不发邮件，等模型下次醒来读。"),
}


def _stale_after(scope: str) -> float:
    return STALE_FRAME_WATCH_SECONDS if scope == "paper" else STALE_WATCH_SECONDS


def _note(
    watch: dict | None, *, fired: int, open_count: int, shown: int,
    open_at: str | None, scope: str,
) -> str:
    paper = scope == "paper"
    who = "巡检" if paper else "监控"
    if watch is None:
        if paper:
            return (
                "今天没有巡检心跳：抓取循环（quant-desk-snapshot-loop）没跑过，"
                "paper 持仓今天一次都没被看过。这个空不是「持仓没事」，是没人在看。"
            )
        return (
            "今天没有心跳文件：监控进程没跑过。这个空不是「没有告警」，"
            "是没人在看——盘中还这样的话，先查 quant-desk-external-monitor 这个计划任务。"
        )
    stamp = (watch.get("last_run_at") or "?")[11:19]
    outcome = watch.get("outcome")
    if outcome == "closed":
        return (f"最后一轮 {stamp} 随收盘退出（正常结束）。今天共触发 {fired} 次，"
                "之后不会再更新。")
    if outcome == "idle":
        return (f"{stamp} 进程在跑，但当前不在抓取窗口（午休或窗口之间），"
                "没有新帧可判；持仓的价在这一段也不会动。")
    if outcome == "no_frame":
        return (f"{stamp} 这一轮没有写出新帧（抓取失败），没有可判的行情。"
                "心跳在，但下面这些判断来自更早的帧。")
    if outcome and outcome != "ok":
        return (f"最后一轮 {stamp} 结束状态是 {outcome}：这一轮没跑完，"
                "下面的数字来自更早的一轮。")
    if watch.get("is_stale"):
        return (
            f"最后一轮停在 {stamp}（{watch.get('seconds_since')} 秒前），"
            f"此后没有人看过盘。下面 {shown} 条是停机之前留下的，不是刚刚发生的。"
        )
    if not fired:
        return f"{who}跑到 {stamp}，今天还没有触发过任何规则。"
    if open_count:
        judged = (open_at or "?")[11:19]
        return (
            f"{who}跑到 {stamp}，今天共触发 {fired} 次；{judged} 判定仍有 "
            f"{open_count} 条成立（见 open）。items 是发生过的事，"
            "只看 items 会把已经解除的破线当成现在。"
        )
    return f"{who}跑到 {stamp}，今天共触发 {fired} 次，现在没有仍然成立的。"
