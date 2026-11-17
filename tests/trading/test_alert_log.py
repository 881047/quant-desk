"""告警日志：两种"空"必须分得开。

``<day>_alerts.jsonl`` 为空有三种完全不同的意思，而它们长得一模一样：

* 监控一直在跑，今天什么都没触发；
* 监控跑到 10:15 就停了，此后没人在看；
* 监控今天根本没启动过。

模型下次醒来只看这个文件，会把三种都读成"没事"。心跳文件存在的唯一理由
就是把后两种和第一种分开。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from service.kernel.clock import SHANGHAI
from service.notify import alert_log

DAY = "20261105"


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 5, hour, minute, second, tzinfo=SHANGHAI)


def _one_alert(title: str = "600000 距止损 1.2%") -> dict:
    return {
        "ts": _at(10, 12).isoformat(timespec="seconds"),
        "rule": "stop_proximity",
        "severity": "warn",
        "symbol": "600000.SH",
        "title": title,
        "facts": ["止损 9.20", "现价 9.31", "第三条不该出现在这里"],
    }


def test_no_heartbeat_is_not_a_quiet_day(tmp_path):
    """没有心跳 = 没人在看。把它报成"今天没事"是这个模块最坏的失败方式。"""
    out = alert_log.read(DAY, now=_at(14, 30), base=tmp_path)

    assert out["available"] is False
    assert out["watcher"] is None
    assert out["count"] == 0
    assert "没人在看" in out["note"]
    assert "external-monitor" in out["note"]


def test_a_heartbeat_with_no_alerts_is_a_quiet_day(tmp_path):
    """跑过、没触发——这才是"没事"，而且现在能证明了。"""
    alert_log.beat(DAY, now=_at(14, 25), base=tmp_path, scope="external")

    out = alert_log.read(DAY, now=_at(14, 30), base=tmp_path)

    assert out["available"] is True
    assert out["count"] == 0
    assert out["watcher"]["seconds_since"] == 300
    assert out["watcher"]["is_stale"] is False
    assert "还没有触发过" in out["note"]


def test_a_stopped_watcher_says_how_long_nothing_was_watched(tmp_path):
    """心跳停在上午，而下面还列着几条告警——那些是停机之前留下的，不是刚发生的。"""
    alert_log.beat(DAY, now=_at(10, 15), base=tmp_path, scope="external")
    alert_log.append(DAY, [_one_alert()], base=tmp_path, scope="external")

    out = alert_log.read(DAY, now=_at(14, 30), base=tmp_path)

    assert out["watcher"]["is_stale"] is True
    assert out["watcher"]["seconds_since"] == 4 * 3600 + 15 * 60
    assert out["count"] == 1
    assert "此后没有人看过盘" in out["note"]
    assert "不是刚刚发生的" in out["note"]


def test_a_crash_leaves_the_previous_heartbeat_standing(tmp_path):
    """心跳在跑完之后写。写到一半崩掉的那一轮，不能留下"跑过了"的记录。"""
    alert_log.beat(DAY, now=_at(9, 35), base=tmp_path, scope="external")
    first = alert_log.read(DAY, now=_at(9, 40), base=tmp_path)["watcher"]["last_run_at"]

    # 这一轮开始但没跑完——没有 beat 调用。
    second = alert_log.read(DAY, now=_at(9, 45), base=tmp_path)["watcher"]["last_run_at"]

    assert first == second
    assert second[11:19] == "09:35:00"


def test_only_the_last_n_alerts_come_back_in_order(tmp_path):
    """日志是追加的，读者要的是最近几条，不是最早几条。"""
    rows = [_one_alert(f"第 {index} 条") for index in range(1, 6)]
    for row in rows:
        alert_log.append(DAY, [row], base=tmp_path, scope="external")

    out = alert_log.read(DAY, limit=2, now=_at(10, 30), base=tmp_path)

    assert out["count"] == 5
    assert [item["title"] for item in out["items"]] == ["第 4 条", "第 5 条"]
    # 每条只带两条事实：这是"叫你去看一眼"，不是报警正文。
    assert len(out["items"][0]["facts"]) == 2


def test_a_corrupt_line_does_not_hide_the_good_ones(tmp_path):
    """追加式日志会被外部工具碰坏。一行坏了不该让整份日志读不出来。"""
    path = alert_log.alerts_path(DAY, tmp_path)
    alert_log.append(DAY, [_one_alert("好的那条")], base=tmp_path, scope="external")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{ 这不是 json\n")
    alert_log.append(DAY, [_one_alert("坏行之后的那条")], base=tmp_path, scope="external")

    out = alert_log.read(DAY, now=_at(10, 30), base=tmp_path)

    assert [item["title"] for item in out["items"]] == ["好的那条", "坏行之后的那条"]


def test_the_day_boundary_is_the_file_name_not_a_timestamp_in_the_row(tmp_path):
    """跨日的告警不会串到今天的列表里——文件名就是分区键。"""
    alert_log.append("20261104", [_one_alert("昨天的")], base=tmp_path, scope="external")
    alert_log.append(DAY, [_one_alert("今天的")], base=tmp_path, scope="external")

    out = alert_log.read(DAY, now=_at(10, 30), base=tmp_path)

    assert [item["title"] for item in out["items"]] == ["今天的"]
    assert out["count"] == 1


def test_the_written_line_is_valid_json_with_the_reader_s_keys(tmp_path):
    """写端与读端是两段代码，格式必须能被单独验证，否则分叉时没人会红。"""
    alert_log.append(DAY, [_one_alert()], base=tmp_path, scope="external")

    line = alert_log.alerts_path(DAY, tmp_path).read_text(encoding="utf-8").strip()
    row = json.loads(line)

    assert set(row) == {"ts", "rule", "severity", "symbol", "title", "facts"}


def test_a_heartbeat_older_than_the_cadence_is_stale(tmp_path):
    """阈值来自写入方的节奏（5 分钟一轮），不是愿望。三倍留够两轮错过。"""
    alert_log.beat(
        DAY,
        now=_at(14, 30) - timedelta(seconds=alert_log.STALE_WATCH_SECONDS),
        base=tmp_path,
    scope="external")

    fresh = alert_log.read(DAY, now=_at(14, 30), base=tmp_path)
    stale = alert_log.read(DAY, now=_at(14, 30) + timedelta(seconds=1), base=tmp_path)

    assert fresh["watcher"]["is_stale"] is False
    assert stale["watcher"]["is_stale"] is True


def test_a_crashed_run_leaves_no_heartbeat(monkeypatch, tmp_path):
    """心跳在**跑完之后**写。写到一半崩掉的那一轮不能留下"跑过了"。

    这条钉的是 ``main()`` 里的调用顺序，不是 ``beat()`` 本身——顺序错了，
    函数级用例全绿而现场每天都是"刚跑过"。
    """
    from scripts import monitor as mon

    monkeypatch.setattr(mon, "REPORTS", tmp_path)

    def boom(_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(mon, "cmd_once", boom)
    with pytest.raises(RuntimeError):
        mon.main(["--once"])

    assert not list(tmp_path.glob("*_heartbeat.json"))


def test_a_finished_run_writes_exactly_one_heartbeat(monkeypatch, tmp_path):
    from scripts import monitor as mon

    monkeypatch.setattr(mon, "REPORTS", tmp_path)
    monkeypatch.setattr(mon, "cmd_once", lambda _args: 0)

    assert mon.main(["--once"]) == 0

    beats = list(tmp_path.glob("*_heartbeat.json"))
    assert len(beats) == 1
    assert json.loads(beats[0].read_text(encoding="utf-8"))["outcome"] == "ok"


def test_a_dry_run_writes_no_heartbeat(monkeypatch, tmp_path):
    """干跑没有看过真账户，不能把"跑过了"记在账上。"""
    from scripts import monitor as mon

    monkeypatch.setattr(mon, "REPORTS", tmp_path)
    monkeypatch.setattr(mon, "cmd_once", lambda _args: 0)

    assert mon.main(["--once", "--dry-run"]) == 0

    assert not list(tmp_path.glob("*_heartbeat.json"))


# ---- 状态机：一条 finding 何时算"新事"，何时算"还在"，何时算"解除" -------------
#
# 30 秒一帧的巡检如果每帧都写一行，破线持续一个下午就是 200 行，把读者那 8 条
# 窗口挤满；反过来只写"发生过"又不写"解除了"，模型 13:35 醒来会看到 10:12 的
# 破线，去卖一个 10:14 就已经回到止损上方的仓位。所以日志记转变，状态记现在。


def _finding(code="stop_breach", severity="critical", subject="600000.SH",
             detail="600000 现价 9.10 已跌破止损 9.20"):
    return {"code": code, "severity": severity, "subject": subject,
            "detail": detail, "numbers": {"last": "9.10", "stop": "9.20"}}


def test_a_finding_that_stays_true_is_written_once(tmp_path):
    first = alert_log.reconcile(DAY, [_finding()], at=_at(10, 12), base=tmp_path, scope="paper")
    again = alert_log.reconcile(DAY, [_finding()], at=_at(10, 13), base=tmp_path, scope="paper")

    assert [row["event"] for row in first["fired"]] == ["fired"]
    assert again["fired"] == []
    assert again["open"] == 1
    assert alert_log.read(DAY, base=tmp_path, scope="paper")["count"] == 1


def test_a_severity_escalation_is_written_but_is_still_one_finding(tmp_path):
    """同一条破线从 high 升到 critical 是同一件事变严重：写一行，但不变成两条。"""
    alert_log.reconcile(DAY, [_finding(severity="high")], at=_at(10, 12), base=tmp_path, scope="paper")
    out = alert_log.reconcile(DAY, [_finding(severity="critical")], at=_at(10, 20),
                              base=tmp_path, scope="paper")

    assert [row["severity"] for row in out["fired"]] == ["critical"]
    assert out["open"] == 1


def test_a_clear_is_written_only_after_the_grace(tmp_path):
    alert_log.reconcile(DAY, [_finding()], at=_at(10, 12), base=tmp_path, scope="paper")

    early = alert_log.reconcile(DAY, [], at=_at(10, 13), base=tmp_path, scope="paper")
    assert early["cleared"] == []
    assert early["open"] == 1, "一分钟不在不算解除，可能只是这一帧没读到"

    late = alert_log.reconcile(DAY, [], at=_at(10, 15), base=tmp_path, scope="paper")
    assert [row["event"] for row in late["cleared"]] == ["cleared"]
    assert late["open"] == 0


def test_a_flap_shorter_than_the_grace_is_not_news(tmp_path):
    """停损价常压在整数关口上，价格在它旁边抖一帧不是"解除了又破了"。"""
    alert_log.reconcile(DAY, [_finding()], at=_at(10, 12), base=tmp_path, scope="paper")
    alert_log.reconcile(DAY, [], at=_at(10, 13), base=tmp_path, scope="paper")          # 抖一下
    back = alert_log.reconcile(DAY, [_finding()], at=_at(10, 13, 30), base=tmp_path, scope="paper")

    assert back["fired"] == [], "回到线上又回来，读者不需要第二条"
    assert back["cleared"] == []
    assert back["open"] == 1
    assert alert_log.read(DAY, base=tmp_path, scope="paper")["count"] == 1


def test_the_open_set_answers_what_is_true_now(tmp_path):
    alert_log.reconcile(
        DAY,
        [_finding(), _finding(code="quotes_unavailable", severity="normal",
                              subject=None, detail="没有实时报价")],
        at=_at(10, 12), base=tmp_path,
    scope="paper")
    alert_log.reconcile(DAY, [_finding()], at=_at(10, 13), base=tmp_path, scope="paper")
    final = alert_log.reconcile(DAY, [_finding()], at=_at(10, 16), base=tmp_path, scope="paper")
    alert_log.beat(DAY, now=_at(10, 16), base=tmp_path, scope="paper")

    out = alert_log.read(DAY, now=_at(10, 16), base=tmp_path, scope="paper")

    assert [row["event"] for row in final["cleared"]] == ["cleared"]
    assert out["open_at"] == "2026-11-05T10:16:00+08:00"
    assert [item["code"] for item in out["open"]] == ["stop_breach"]
    assert "1 条成立" in out["note"]


def test_the_open_set_is_read_back_from_disk_not_from_memory(tmp_path):
    """巡检进程每帧都可能重启。状态必须在文件里，否则重启后第一条破线被当成新的。"""
    alert_log.reconcile(DAY, [_finding()], at=_at(10, 12), base=tmp_path, scope="paper")

    state = json.loads(alert_log.state_path(DAY, tmp_path).read_text(encoding="utf-8"))
    assert list(state["open"]) == ["stop_breach|600000.SH"]

    again = alert_log.reconcile(DAY, [_finding()], at=_at(10, 13), base=tmp_path, scope="paper")
    assert again["fired"] == []


def test_a_transition_line_carries_the_readers_keys(tmp_path):
    alert_log.reconcile(DAY, [_finding()], at=_at(10, 12), base=tmp_path, scope="paper")

    line = alert_log.alerts_path(DAY, tmp_path).read_text(encoding="utf-8").strip()
    row = json.loads(line)

    assert set(row) == {"ts", "event", "rule", "severity", "symbol", "title", "facts"}
    assert row["symbol"] == "600000.SH"
    assert row["facts"] == ["last=9.10", "stop=9.20"], "只留两条，且要能读"


def test_the_two_scopes_do_not_share_a_directory_or_a_threshold(tmp_path):
    """实盘 5 分钟一轮（阈值 15 分钟），paper 每帧一轮（阈值 10 分钟）。

    同一个数会让其中一个永远正常或永远告警：按 15 分钟判 paper，抓取停了三帧
    还显示"刚刚跑过"；按 10 分钟判实盘，一轮慢一点就报"没人在看"。
    """
    assert alert_log.default_base("external").name == "external"
    assert alert_log.default_base("paper").name == "paper"

    alert_log.beat(DAY, now=_at(10, 12), base=tmp_path / "external", scope="external")
    alert_log.beat(DAY, now=_at(10, 12), base=tmp_path / "paper", scope="paper")

    at = _at(10, 23)
    external = alert_log.read(DAY, now=at, base=tmp_path / "external", scope="external")
    paper = alert_log.read(DAY, now=at, base=tmp_path / "paper", scope="paper")

    assert external["watcher"]["is_stale"] is False
    assert paper["watcher"]["is_stale"] is True
