"""F3 regressions: the auto-stop state machine, pinned against ``000aba3``.

The old ``autostop.run`` had no ``findings`` argument, used a module-level
``CONFIRM_SECONDS`` constant, measured breach age as a plain wall-clock delta,
and put ``-{qty}`` in the idempotency key. ``watch.run`` did not write failed
orders into the alerts file. Each case below asserts the *new* behaviour and is
red on the old code:

===== ============================================================= ==============================
F3-1  open mark survives the grace window, but ``findings=[]`` blocks       old sold on stale marks
F3-2  11:29 -> 13:00 is 60 session seconds, not 5460 wall-clock seconds     old sold immediately
F3-3  10:30 -> 10:33 still sells once; confirm window is read at run time   old constant / no findings
F3-4  ``client_request_id`` has no ``-{qty}``                               old key changed on unfreeze
F3-5  a failed order becomes an ``autostop_error`` alert, beat stays ``ok``   old only wrote stdout
F3-6  ``findings is None`` keeps legacy behaviour; ``findings=[]`` blocks    old had neither distinction
===== ============================================================= ==============================

All writes go to ``base=tmp_path`` (never the real ``reports/`` runtime dir),
all clocks are injected, no ``sleep``, no ``desk.quotes``/vendor call.
"""

from __future__ import annotations

import json

import pytest

from service import settings
from service.notify import alert_log
from service.trading import autostop, watch
from tests.trading.test_autostop import _held
from tests.trading.test_watch import _frame, _moment

SCOPE = "paper"


# ---------------------------------------------------------------------------
# helpers — environment simulation, not fabrication of a passing answer
# ---------------------------------------------------------------------------
def _prime_mark(base, day: str, symbol: str, at) -> None:
    """Write one open ``stop_breach`` mark through the *real* reconcile writer.

    ``at`` fixes ``first_at`` exactly, which is what lets F3-2 put the breach at
    11:29 and F3-3 at 10:30 without waiting for two real frames. The state file
    lands under ``base`` (tmp_path); nothing is written to reports/.
    """
    alert_log.reconcile(
        day,
        [_still_breached(symbol)[0]],
        at=at, base=base, scope=SCOPE,
    )


def _still_breached(symbol: str) -> list[dict]:
    return [{"code": "stop_breach", "severity": "critical",
             "subject": symbol, "detail": "收盘价跌破建仓票上写好的止损",
             "numbers": {}}]


def _open_first_at(base, day: str) -> str:
    marks = alert_log.open_marks(day, base, scope=SCOPE)
    assert marks, "setup 没写下 open mark"
    return next(iter(marks.values()))["first_at"]


def _capture_sells(desk, monkeypatch):
    """Stub the order path so a run can be repeated without mutating the book."""
    seen: list[dict] = []

    def fake_propose(req, actor=None):
        seen.append(req)
        return {"ticket_id": "td-phase3-sell", "status": "proposed"}

    def fake_approve(ticket_id, actor=None):
        return {"status": "filled"}

    monkeypatch.setattr(desk.decisions, "propose", fake_propose)
    monkeypatch.setattr(desk.decisions, "approve", fake_approve)
    return seen


# ---------------------------------------------------------------------------
# F3-1 · a recovered frame must not sell on a stale open mark
# ---------------------------------------------------------------------------
def test_recovered_frame_blocks_autosell(desk, tmp_path, monkeypatch):
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _prime_mark(tmp_path, day, symbol, _moment(day, 10, 30))
    now = _moment(day, 10, 33)
    seen = _capture_sells(desk, monkeypatch)

    # The current frame has recovered: no stop_breach finding at all. The old
    # run() looked only at open marks; reconcile's grace window kept the mark
    # open even though the price came back, so it sold a healthy position.
    acted = autostop.run(desk, day=day, scope=SCOPE, now=now, base=tmp_path,
                         findings=[])
    assert acted == [], "open mark 还在，但当前帧已恢复，不该卖"
    assert seen == [], "一次代下单都不该发起"

    # Control: with the legacy (no frame) call the very same stale mark would
    # trigger a sell. Without this half, "acted == []" could be a false green
    # caused by a broken setup.
    legacy = autostop.run(desk, day=day, scope=SCOPE, now=now, base=tmp_path)
    assert len(legacy) == 1, "旧口径下这张 stale mark 确实会触发一次代下单"
    assert seen, "控制组必须真的走到 _sell"


# ---------------------------------------------------------------------------
# F3-2 · the lunch break does not count towards the confirmation window
# ---------------------------------------------------------------------------
def test_lunch_break_does_not_count_as_confirmation(desk, tmp_path, monkeypatch):
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    first_at = _moment(day, 11, 29)          # 整分时刻：SESSIONS 端点是闭区间
    now = _moment(day, 13, 0)
    _prime_mark(tmp_path, day, symbol, first_at)
    seen = _capture_sells(desk, monkeypatch)

    # Pure-function assertion: 60 session seconds, not 5460 wall-clock seconds.
    assert autostop._age_seconds(_open_first_at(tmp_path, day), now,
                                 desk.market.calendar) == 60.0
    assert (now - first_at).total_seconds() == 5460.0, (
        "墙钟差就是旧代码会误判成『已确认』的那个数")

    acted = autostop.run(desk, day=day, scope=SCOPE, now=now, base=tmp_path,
                         findings=_still_breached(symbol))
    assert acted == [], "11:29→13:00 只有 60s 会话时长，<120s 不卖"
    assert seen == [], "卖出 0 次"
    assert desk.book.summary()["positions"], "仓位必须还在"


# ---------------------------------------------------------------------------
# F3-3 · a genuinely confirmed breach still sells once (regression protection)
# ---------------------------------------------------------------------------
def test_confirmed_breach_still_sells_once(desk, tmp_path, monkeypatch):
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _prime_mark(tmp_path, day, symbol, _moment(day, 10, 30))
    now = _moment(day, 10, 33)
    findings = _still_breached(symbol)

    # The session-based rewrite must not break the ordinary confirmed sell.
    assert autostop._age_seconds(_open_first_at(tmp_path, day), now,
                                 desk.market.calendar) == 180.0

    # Drift simulation: the confirmation window is now read from settings at
    # run time (module-level CONFIRM_SECONDS was deleted). Making it
    # unreachable must suppress the sale. Old code used the import-time
    # constant, ignored this patch and would still sell -> this half is red
    # on old code.
    real_num = settings.num

    def unreachable_confirm(key, *args, **kwargs):
        if key == "autostop.confirm_seconds":
            return 999999.0
        return real_num(key, *args, **kwargs)

    with monkeypatch.context() as drift:
        drift.setattr(settings, "num", unreachable_confirm)
        assert autostop.run(desk, day=day, scope=SCOPE, now=now, base=tmp_path,
                            findings=findings) == []

    assert not hasattr(autostop, "CONFIRM_SECONDS"), (
        "旧代码把确认窗固化为模块级常量；修复后必须在 run() 内读配置")

    acted = autostop.run(desk, day=day, scope=SCOPE, now=now, base=tmp_path,
                         findings=findings)
    assert len(acted) == 1, "持续 180s ≥120s，卖 1 次"
    assert acted[0]["status"] != "blocked", acted[0]

    again = autostop.run(desk, day=day, scope=SCOPE, now=now, base=tmp_path,
                         findings=findings)
    assert again == [], "已经卖过，不该再来一笔"
    assert sum(1 for row in desk.paper.recent_fills(10)
               if row["side"] == "SELL") == 1


# ---------------------------------------------------------------------------
# F3-4 · the idempotency key does not contain the (changing) quantity
# ---------------------------------------------------------------------------
def test_idempotency_key_does_not_include_qty(desk, tmp_path, monkeypatch):
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _prime_mark(tmp_path, day, symbol, _moment(day, 10, 30))
    now = _moment(day, 10, 33)
    seen = _capture_sells(desk, monkeypatch)

    # No ``findings`` kwarg on purpose: the old run() signature accepts this
    # call too, so the case is red on old code for the *behavioural* reason
    # (key differs), not merely a TypeError.
    position = desk.book.ledger.account.positions[symbol]
    position.available_qty = 100
    autostop.run(desk, day=day, scope=SCOPE, now=now, base=tmp_path)
    position.available_qty = 300            # T+1 解冻 / 部分成交会改这个数
    autostop.run(desk, day=day, scope=SCOPE, now=now, base=tmp_path)

    assert len(seen) == 2
    assert seen[0]["qty"] != seen[1]["qty"], "两次的数量确实不同"
    key1 = seen[0]["client_request_id"]
    key2 = seen[1]["client_request_id"]
    assert key1 == key2, "旧键含 -{qty}，解冻后会生成第二个句柄、再卖一次"
    assert key1 == f"autostop-{day}-{symbol}-{entry}"
    assert "100" not in key1 and "300" not in key2
    assert len({row["client_request_id"] for row in seen}) == 1, (
        "同一 entry 只应有一个句柄；第二次 propose 命中同一张票")


# ---------------------------------------------------------------------------
# F3-5 · a failed auto-sell is written to the alerts file; beat stays ok
# ---------------------------------------------------------------------------
def test_autostop_error_is_written_to_alert_log(desk, tmp_path, monkeypatch):
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _prime_mark(tmp_path, day, symbol, _moment(day, 10, 30))
    now = _moment(day, 10, 33)

    def boom(req, actor=None):
        raise RuntimeError("phase3-propose-boom")

    monkeypatch.setattr(desk.decisions, "propose", boom)

    # The frame still shows a breach, so reconcile keeps the mark open and
    # autostop attempts the sale; propose blows up -> status="error".
    path = _frame(tmp_path, day=day, at=now, symbol=symbol,
                  last=float(stop) * 0.97)
    shot = watch.run(desk, path, day=day, now=now, base=tmp_path)

    errors = [row for row in shot["acted"] if row.get("status") == "error"]
    assert errors, "代下单失败必须原样留在 acted 里"
    assert any("RuntimeError" in reason for row in errors
               for reason in row["reasons"])

    alerts_path = alert_log.alerts_path(day, tmp_path, scope=SCOPE)
    assert alerts_path.is_file(), "旧代码把异常只留在 stdout，alerts 文件里没有记录"
    rows = [json.loads(line) for line in
            alerts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    written = [row for row in rows if row.get("event") == "autostop_error"]
    assert len(written) == 1, "一条 error 一行，不能一秒一行"
    assert written[0]["rule"] == "autostop_error"
    assert "RuntimeError" in written[0]["title"], (
        "title 必须带异常类型，读者才知道去看哪条链路")
    assert "phase3-propose-boom" in written[0]["title"]

    heart = json.loads(alert_log.heartbeat_path(
        day, tmp_path, scope=SCOPE).read_text(encoding="utf-8"))
    assert heart["outcome"] == "ok", "watch 的 beat outcome 不能被失败改写"


# ---------------------------------------------------------------------------
# F3-6 · team-lead's ruling: None keeps legacy behaviour, [] blocks everything
# ---------------------------------------------------------------------------
def test_findings_none_keeps_legacy_behavior(desk, tmp_path, monkeypatch):
    day = desk.market.latest_bar_date()
    symbol, entry, stop = _held(desk)
    _prime_mark(tmp_path, day, symbol, _moment(day, 10, 30))
    now = _moment(day, 10, 33)
    seen = _capture_sells(desk, monkeypatch)

    # Direct call, not via watch: an old caller with no frame to hand us must
    # still see the pre-fix behaviour (no intersection filtering).
    legacy = autostop.run(desk, day=day, scope=SCOPE, now=now, base=tmp_path,
                          findings=None)
    assert len(legacy) == 1, "findings is None 保持原行为：不过滤、仍卖 1 次"
    assert len(seen) == 1

    # An explicit empty current frame means "no breach right now": full block.
    seen.clear()
    blocked = autostop.run(desk, day=day, scope=SCOPE, now=now, base=tmp_path,
                           findings=[])
    assert blocked == [], "findings=[] 必须全挡"
    assert seen == []
