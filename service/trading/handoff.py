"""The alert handoff: the question the workspace is asked, and its answer.

The monitor runs unattended and cannot reach a model — the desktop profile
refuses to boot from a CLI, the shipped headless profile has no credential, and
the web server wants a token that is not on disk. So the pipeline hands the
*question* over and stops:

    rules  →  handoff file  →  quant-desk workspace answers  →  second mail

That is not a degraded path. It is the intended one: the model that answers is
the one already reading this desk, woken cheaply, asked one compact question.
Spawning a full agent to write three sentences is how a cheap loop becomes an
expensive one.

Nothing here decides anything. The rules produced the facts, the workspace
produced the sentences, this module sends them.
"""

from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import time
from datetime import datetime
from html import escape
from pathlib import Path

from service import settings
from service.kernel.clock import SHANGHAI
from service.notify.mail import MailDeliveryUnknown, MailNotConfigured, send_mail

from .analyst import HANDOFF

_ICON = {"alert": "🔴", "warn": "🟠", "info": "🔵"}
_HANDOFF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

# SMTP gives up after 20s, so a claim older than this cannot belong to a live
# send. It is not released automatically: the mail may already have gone out.
STALE_LOCK_SECONDS = settings.num("handoff.stale_lock_seconds")

# 解读窗口：多久以内的提醒还值得写三句话。
#
# 「当天」这个判据漏掉一整类条目：15:25 那次运行只读 created_at 是今天的，
# 而 15:25 之后到收盘、以及次日开盘前产生的提醒，在下一个 15:25 眼里已经
# 是历史，于是它们永远进不了窗口——不是没人管，是没有任何一次运行会看见。
# 96 小时覆盖「上一交易日 15:25 → 本次 15:25」再加上周末与平台 ~20 分钟抖动；
# 再早的事实拿来解释今天的持仓才会误导，那部分只报数量，不解读。
READ_WINDOW_HOURS = 96


def age_hours(created_at: str) -> float | None:
    """条目距今多少小时。解析不了返回 None——由读者当成过期，不当成 0。"""
    try:
        stamp = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=SHANGHAI)
    return round((datetime.now(SHANGHAI) - stamp).total_seconds() / 3600.0, 1)


def _path(handoff_id: str) -> Path:
    """Resolve one strict id below HANDOFF; never trust it as a path fragment."""
    if not isinstance(handoff_id, str) or not _HANDOFF_ID.fullmatch(handoff_id):
        raise ValueError(f"invalid handoff_id: {handoff_id!r}")
    root = HANDOFF.resolve()
    path = (root / f"{handoff_id}.json").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"handoff_id escapes its root: {handoff_id!r}") from exc
    return path


def files() -> list[Path]:
    if not HANDOFF.exists():
        return []
    return sorted(HANDOFF.glob("*.json"))


def load(handoff_id: str) -> dict | None:
    try:
        path = _path(handoff_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save(handoff_id: str, doc: dict) -> Path:
    path = _path(handoff_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{handoff_id}.", suffix=".tmp", delete=False) as temp:
            temp_name = temp.name
            json.dump(doc, temp, ensure_ascii=False, indent=1)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)
    return path


def pending() -> list[dict]:
    """Unanswered questions, oldest first. This is the model's inbox."""
    out = []
    for path in files():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("status") != "pending":
            continue
        out.append({
            "id": doc.get("id") or path.stem,
            "created_at": doc.get("created_at", ""),
            # 服务端算好年龄，读者不必自己减时间：模型对时刻的算术不可靠，
            # 而"这条该不该现在解读"完全取决于它。
            "age_hours": age_hours(doc.get("created_at", "")),
            "analyst": doc.get("analyst", ""),
            "prompt": doc.get("prompt", ""),
            "alerts": len(doc.get("alerts") or []),
            "positions": len(doc.get("positions") or []),
            # Present only when a send attempt's outcome is unknown. It tells the
            # reader not to answer again until a human has checked the mailbox.
            "send_state": doc.get("send_state"),
        })
    return out


def render_text(doc: dict, analysis: str) -> str:
    lines = [f"持仓解读 · {doc.get('created_at', '')}", ""]
    for alert in doc.get("alerts") or []:
        lines.append(f"{_ICON.get(alert['severity'], '·')} {alert['title']}")
        for fact in alert.get("facts") or []:
            lines.append(f"    · {fact}")
        if alert.get("ref"):
            lines.append(f"    引用 {alert['ref']}")
        lines.append("")
    lines += ["—— 解读 ——", analysis, ""]
    if doc.get("positions"):
        lines += ["持仓与纪律位", ""]
        for pos in doc["positions"]:
            lines.append(
                f"  {pos['symbol']} {pos['qty']} 股 现价 {pos['last']:.2f} "
                f"(个股 {pos['pct']:+.2f}%) 成本 {pos['cost']:.3f} "
                f"盈亏 {pos['pnl']:+,.0f} | 止损 {pos['stop']:.2f} "
                f"竞价线 {pos['auction_line']:.2f}")
        lines.append("")
    lines += ["—— 说明 ——",
              "触发与纪律位由规则层计算，解读来自 quant-desk 会话；",
              "这封邮件不给买卖建议，执行与否由你决定。"]
    return "\n".join(lines)


def render_html(doc: dict, analysis: str) -> str:
    color = {"alert": "#b00020", "warn": "#a15c00", "info": "#0b5cad"}
    blocks = []
    for alert in doc.get("alerts") or []:
        facts = "".join(f"<li>{escape(str(f))}</li>" for f in alert.get("facts") or [])
        quote = (f"<div style='margin-top:6px;font-size:12px;color:#666'>引用 "
                 f"{escape(str(alert['ref']))}</div>" if alert.get("ref") else "")
        blocks.append(
            f"<div style='margin:0 0 12px;padding:10px 12px;border-left:3px solid "
            f"{color.get(alert['severity'], '#888')};background:#fafafa'>"
            f"<b>{_ICON.get(alert['severity'], '·')} {escape(str(alert['title']))}</b>"
            f"<ul style='margin:6px 0 0 18px;font-size:13px'>{facts}</ul>{quote}</div>")
    rows = "".join(
        f"<tr><td>{escape(str(p['symbol']))}</td>"
        f"<td align='right'>{escape(str(p['qty']))}</td>"
        f"<td align='right'>{p['last']:.2f}</td><td align='right'>{p['pct']:+.2f}%</td>"
        f"<td align='right'>{p['cost']:.3f}</td><td align='right'>{p['pnl']:+,.0f}</td>"
        f"<td align='right'>{p['stop']:.2f}</td>"
        f"<td align='right'>{p['auction_line']:.2f}</td></tr>"
        for p in doc.get("positions") or [])
    return f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#222">
<h2 style="font-size:16px">持仓解读 · {escape(str(doc.get('created_at', '')))}</h2>
{''.join(blocks)}
<h3 style="font-size:14px">解读</h3>
<p style="font-size:13px;line-height:1.7;white-space:pre-wrap">{escape(str(analysis))}</p>
<h3 style="font-size:14px">持仓与纪律位</h3>
<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px">
<tr style="background:#f0f0f0"><th>代码</th><th>股数</th><th>现价</th><th>个股</th>
<th>成本</th><th>盈亏</th><th>止损</th><th>竞价线</th></tr>{rows}</table>
<p style="margin-top:16px;font-size:12px;color:#666">
触发与纪律位由规则层计算，解读来自 quant-desk 会话；这封邮件不给买卖建议。</p>
</div>"""


def _lock_path(handoff_id: str) -> Path:
    return _path(handoff_id).with_suffix(".send-lock")


def _lock_info(lock: Path) -> dict:
    """What the claim says about itself: who took it, when, and how far it got.

    Older claims are a bare timestamp (or nothing), so the file mtime is the
    fallback. Age is what separates "a process is sending right now" from "a
    process died holding this".
    """
    try:
        raw = lock.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    payload: dict = {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed
    created = payload.get("created_at")
    age: float | None = None
    if isinstance(created, str):
        try:
            age = (datetime.now(SHANGHAI) - datetime.fromisoformat(created)).total_seconds()
        except ValueError:
            age = None
    if age is None:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = None
    phase = payload.get("phase")
    # "unknown" and "sent" were written by a sender that is no longer running:
    # one lost the delivery result, the other lost the state save. Neither is a
    # process to wait for, however young the file is.
    live = phase not in ("unknown", "sent") and (
        age is None or age <= STALE_LOCK_SECONDS)
    return {
        "pid": payload.get("pid"),
        "host": payload.get("host"),
        "phase": phase,
        "created_at": created,
        "age_seconds": None if age is None else int(max(age, 0.0)),
        "live": live,
    }


def _claim_send(handoff_id: str) -> Path | None:
    """Atomically claim one handoff across processes with O_CREAT|O_EXCL."""
    path = _path(handoff_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(handoff_id)
    payload = {"pid": os.getpid(), "host": socket.gethostname(),
               "created_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
               "phase": "claimed"}
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except OSError:
        lock.unlink(missing_ok=True)
        raise
    return lock


def _set_phase(lock: Path, phase: str) -> None:
    """Best-effort progress marker; a claim that cannot be updated still holds."""
    info = _lock_info(lock)
    payload = {"pid": info.get("pid") or os.getpid(),
               "host": info.get("host") or socket.gethostname(),
               "created_at": info.get("created_at")
               or datetime.now(SHANGHAI).isoformat(timespec="seconds"),
               "phase": phase}
    try:
        lock.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _message_id(handoff_id: str) -> str:
    """Deterministic, so a duplicate send of one handoff is recognisable."""
    return f"<{handoff_id}@quant-desk.local>"


def _log_mail(event: dict) -> None:
    """Append-only record: what was sent, to whom, and what we could not know."""
    try:
        path = HANDOFF / "_mail_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _archive_lock(lock: Path) -> None:
    """Keep the claim as evidence; a bare unlink would erase who held it."""
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%d-%H%M%S")
    target = lock.with_name(f"{lock.name}.released-{stamp}-{os.getpid()}")
    try:
        os.replace(lock, target)
    except OSError:
        lock.unlink(missing_ok=True)


def _blocked(lock: Path, subject: str) -> dict:
    """Describe an existing claim without pretending it is a fresh send."""
    info = _lock_info(lock)
    return {"sent": False, "status": "sending" if info["live"] else "send_unknown",
            "may_have_sent": True, "subject": subject, "lock": info}


def answer(handoff_id: str, analysis: str, *, dry_run: bool = False,
           force: bool = False) -> dict:
    """Send at most once; an atomic send claim prevents concurrent duplicates.

    Three outcomes are deliberately distinct from "sent": ``sending`` (a live
    claim exists), ``send_unknown`` (a claim outlived its sender, or SMTP
    accepted the mail and then the connection died) and ``already_answered``.
    None of them may be retried into a second mail.
    """
    text_in = (analysis or "").strip()
    if not text_in:
        raise ValueError("解读内容为空")
    doc = load(handoff_id)
    if doc is None:
        raise KeyError(handoff_id)

    alerts = doc.get("alerts") or []
    subject = f"[持仓解读] {alerts[0]['title'] if alerts else '持仓提醒'}"
    if not dry_run and doc.get("status") == "answered":
        return {"sent": False, "already_answered": True, "subject": subject,
                "to": doc.get("mail_to")}
    if not dry_run and doc.get("status") == "dropped":
        return {"sent": False, "status": "dropped", "subject": subject}
    body = render_text(doc, text_in)
    html = render_html(doc, text_in)
    if dry_run:
        return {"sent": False, "subject": subject, "text": body, "html": html}
    if doc.get("send_state") == "unknown" and not force:
        return {"sent": False, "status": "send_unknown", "needs_confirmation": True,
                "may_have_sent": True, "subject": subject,
                "note": "上一次投递结果未知，可能已经发出；核对邮箱后确认没收到，"
                        "再用 force/--force 重发。"}

    existing = _lock_path(handoff_id)
    if existing.exists():
        if force and not _lock_info(existing)["live"]:
            # The operator has confirmed nothing arrived, and the claim's owner
            # is gone. Archive it: the same handoff must not carry two claims.
            _archive_lock(existing)
        else:
            return _blocked(existing, subject)

    lock = _claim_send(handoff_id)
    if lock is None:
        return {"sent": False, "status": "sending", "may_have_sent": True,
                "subject": subject}
    release_lock = True
    try:
        # Re-read after claiming: another sender may have completed just before
        # the claim was removed. Once mail succeeds, a failed state save leaves
        # the claim in place rather than allowing a retry to send a duplicate.
        doc = load(handoff_id)
        if doc is None:
            raise KeyError(handoff_id)
        if doc.get("status") == "answered":
            return {"sent": False, "already_answered": True, "subject": subject,
                    "to": doc.get("mail_to")}
        if doc.get("status") == "dropped":
            return {"sent": False, "status": "dropped", "subject": subject}
        if doc.get("send_state") == "unknown" and not force:
            return {"sent": False, "status": "send_unknown",
                    "needs_confirmation": True, "may_have_sent": True,
                    "subject": subject}
        _set_phase(lock, "sending")
        try:
            result = send_mail(subject, body, html,
                               message_id=_message_id(handoff_id))
        except MailDeliveryUnknown as exc:
            # The server may already have accepted this mail. Keep the claim and
            # record why, so the next attempt cannot silently send a duplicate.
            release_lock = False
            _set_phase(lock, "unknown")
            doc["send_state"] = "unknown"
            doc["send_attempted_at"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
            doc["send_error"] = str(exc)
            try:
                save(handoff_id, doc)
            except OSError:
                pass
            _log_mail({"at": doc["send_attempted_at"], "id": handoff_id,
                       "event": "unknown", "subject": subject, "error": str(exc)})
            return {"sent": False, "status": "send_unknown", "may_have_sent": True,
                    "subject": subject, "error": str(exc)}
        except (MailNotConfigured, RuntimeError) as exc:
            # Pre-delivery failure: nothing reached the server, so the claim is
            # released and a retry is safe.
            raise RuntimeError(str(exc)) from exc
        release_lock = False
        _set_phase(lock, "sent")
        doc["status"] = "answered"
        doc["analysis"] = text_in
        doc["mail_to"] = result.get("to")
        doc["mail_message_id"] = result.get("message_id")
        doc["answered_at"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        doc.pop("send_state", None)
        try:
            save(handoff_id, doc)
        except OSError:
            _log_mail({"at": doc["answered_at"], "id": handoff_id,
                       "event": "sent_state_failed", "subject": subject,
                       "message_id": result.get("message_id")})
            raise
        _log_mail({"at": doc["answered_at"], "id": handoff_id, "event": "sent",
                   "subject": subject, "to": result.get("to"),
                   "message_id": result.get("message_id")})
        release_lock = True
        return {"sent": True, "subject": subject, "to": result.get("to"),
                "message_id": result.get("message_id")}
    finally:
        if release_lock:
            lock.unlink(missing_ok=True)


def release_lock(handoff_id: str, *, note: str = "") -> dict:
    """Clear a claim left behind by a dead process. Never resends by itself.

    The claim is archived rather than deleted, and the handoff is marked
    ``send_state=unknown``, so answering again still needs an explicit force.
    """
    doc = load(handoff_id)
    if doc is None:
        raise KeyError(handoff_id)
    lock = _lock_path(handoff_id)
    info = _lock_info(lock) if lock.exists() else None
    if info is None and doc.get("send_state") != "unknown":
        raise RuntimeError("没有发送认领可释放；这条提醒本来就能直接答")
    if info is not None:
        _archive_lock(lock)
    released_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    doc["send_state"] = "unknown"
    doc["lock_released_at"] = released_at
    if note:
        doc["lock_released_note"] = note
    save(handoff_id, doc)
    _log_mail({"at": released_at, "id": handoff_id, "event": "released",
               "note": note, "lock": info})
    return {"id": handoff_id, "status": "released", "send_state": "unknown",
            "lock": info}


def drop(handoff_id: str, reason: str = "") -> dict:
    """不再发信地终止一条提醒。文档保留，只改 status。

    ``reason`` 不是装饰：2026-11-09 清理 19 条 pending 时，有 8 条的成因
    （跨日账户比对、午休误判）在当天被修掉，如果不写下来，三个月后看到的
    只是一批 ``dropped`` 时间戳，无法区分「误报」与「人决定不发」。
    """
    doc = load(handoff_id)
    if doc is None:
        raise KeyError(handoff_id)
    if _lock_path(handoff_id).exists():
        raise RuntimeError("该提醒仍有发送认领，投递结果未知时不能丢弃；"
                           "先核对邮箱，再 release 或 force")
    doc["status"] = "dropped"
    doc["dropped_at"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    if reason:
        doc["drop_reason"] = reason
    save(handoff_id, doc)
    return {"id": handoff_id, "status": "dropped"}
