"""Alerts: each rule fires on facts, once per day, and never on its own anchor."""

from __future__ import annotations

from service.trading import monitor
from service.trading.external import ExternalStore


def _ctx(**kw) -> monitor.PositionContext:
    base = {"symbol": "000700.SZ", "qty": 100, "cost": 13.12, "entry": 13.07,
                "prev_close": 13.07, "last": 13.07, "open": 13.0, "high": 13.1, "low": 12.9,
                "volume": 1e6, "amount": 1.3e7, "adv20": 5e6, "adv20_unit": "cny",
                "ma5": 12.8, "ma20": 12.5,
                "up_limit": 14.38, "down_limit": 11.76}
    base.update(kw)
    return monitor.PositionContext(**base)


def _rules(alerts) -> set[str]:
    return {a.rule for a in alerts}


def test_stop_breach_carries_the_numbers_it_fired_on():
    out = monitor.evaluate([_ctx(last=12.40)], day="20261027")
    assert "stop_breach" in _rules(out)
    alert = next(a for a in out if a.rule == "stop_breach")
    assert alert.severity == monitor.ALERT
    joined = " ".join(alert.facts)
    assert "12.42" in joined and "13.07" in joined, "止损与买入价都要写出来"


def test_pinned_stop_replaces_the_pct_rule():
    """``configs/stops.json`` 钉的价位优先。

    撤掉 -5% 硬止损的人是有意为之；监视器若还按 ``entry × 0.95`` 判定，
    就会每天早上一封「跌破 14.17」而账本上那条线早已是 9.90。
    """
    assert "stop_breach" in _rules(monitor.evaluate([_ctx(last=12.40)], day="20261027"))

    pinned = _ctx(last=12.40, stop_override=11.0)
    assert pinned.stop == 11.0
    assert "stop_breach" not in _rules(monitor.evaluate([pinned], day="20261027"))

    hit = [a for a in monitor.evaluate(
        [_ctx(last=10.90, prev_close=11.50, down_limit=10.0, stop_override=11.0)],
        day="20261027") if a.rule == "stop_breach"]
    assert hit, "钉住的价位被跌破仍然要报"
    assert "configs/stops.json" in " ".join(hit[0].facts), "文案要说清这条线是谁定的"


def test_limit_down_wins_over_stop_for_the_same_symbol():
    """One message per symbol per event: the strongest one."""
    out = monitor.evaluate([_ctx(last=11.76)], day="20261027")
    assert "limit_down" in _rules(out)
    assert "stop_breach" not in _rules(out), "跌停已经包含了破止损，不必发两条"


def test_near_stop_is_information_not_an_alert():
    out = monitor.evaluate([_ctx(last=12.60)], day="20261027")
    assert "near_stop" in _rules(out)
    assert next(a for a in out if a.rule == "near_stop").severity == monitor.INFO


def test_auction_breach_needs_a_real_auction_price():
    """A line derived from `last` compared to `last` can never fire."""
    without = monitor.evaluate([_ctx(last=13.07)], day="20261027", session="preopen")
    assert "auction_breach" not in _rules(without)

    with_price = monitor.evaluate([_ctx(last=13.07, auction_price=12.70)],
                                  day="20261027", session="preopen")
    assert "auction_breach" in _rules(with_price)
    assert with_price[0].severity == monitor.WARN


def test_big_drop_and_volume_breakdown_are_separate_claims():
    quiet = monitor.evaluate([_ctx(last=12.40, prev_close=13.07, amount=5e6)],
                             day="20261027")
    assert "big_drop" in _rules(quiet)
    assert "volume_breakdown" not in _rules(quiet), "缩量下跌只是没人买"

    heavy = monitor.evaluate([_ctx(last=12.40, prev_close=13.07, amount=1.2e7)],
                             day="20261027")
    assert {"big_drop", "volume_breakdown"} <= _rules(heavy)


def test_ma5_break_needs_yesterday_above_and_today_below():
    assert "ma5_break" in _rules(
        monitor.evaluate([_ctx(last=12.70, prev_close=12.90, ma5=12.80)],
                         day="20261027"))
    assert "ma5_break" not in _rules(
        monitor.evaluate([_ctx(last=12.70, prev_close=12.70, ma5=12.80)],
                         day="20261027")), "一直在 MA5 下方不是新事件"


def test_reconcile_gap_becomes_an_alert():
    out = monitor.evaluate([], day="20261027",
                           reconcile={"ok": False, "mismatches": [
                               {"symbol": "000700.SZ", "broker_qty": 130,
                                "ledger_qty": 100, "delta": 30}]})
    assert _rules(out) == {"reconcile_gap"}
    assert "130" in " ".join(out[0].facts)


def test_alerts_are_claimed_once_per_day(tmp_path):
    store = ExternalStore(tmp_path / "external.sqlite")
    key = monitor.Alert(rule="stop_breach", severity=monitor.ALERT,
                        title="x", symbol="000700.SZ").key("20261027")
    kwargs = {"key": key, "day": "20261027", "rule": "stop_breach",
                  "symbol": "000700.SZ", "severity": monitor.ALERT, "title": "x"}
    assert store.claim_alert(**kwargs) is True
    assert store.claim_alert(**kwargs) is False, "五分钟一轮不能发六次同一件事"

    assert store.release_alerts([key], note="MailNotConfigured") == 1
    assert store.claim_alert(**kwargs) is True, "未发送的键释放后必须能在下轮重新 claim"

    store.mark_alert_sent([key])
    assert store.release_alerts([key], note="late failure") == 0
    assert store.claim_alert(**kwargs) is False, "已发送记录永久保留，不能被失败回收删除"

    tomorrow = dict(kwargs, key=key.replace("20261027", "20261028"), day="20261028")
    assert store.claim_alert(**tomorrow) is True, "第二天要重新提醒"


def test_rendered_mail_states_facts_and_refuses_to_advise():
    alerts = monitor.evaluate([_ctx(last=12.40)], day="20261027")
    text = monitor.render_text(alerts, day="20261027", stamp="10:30",
                               contexts=[_ctx(last=12.40)])
    assert "跌破止损" in text
    assert "不给买卖建议" in text
    assert "000700.SZ" in text


# ------------------------------------------------------- 规则层不许越权解读
_REF_SOURCES = ("计划：", "规程：", "算式：")


def test_every_quotation_names_what_it_is_quoting():
    """``ref`` 是引用，不是判断：它必须说清楚引的是计划、规程还是算式。

    这条约束是这套东西能被信任的原因——一条说不出出处的话，就是规则层在发表意见。
    """
    cases = [
        {"last": 12.40},                                   # stop_breach
        {"last": 11.76},                                   # limit_down
        {"last": 12.43},                                   # near_stop
        {"last": 12.30, "amount": 3.2e8, "adv20": 1.1e8},        # big_drop + volume
        {"last": 12.70, "ma5": 12.90},                        # ma5_break
        {"last": 13.00, "auction_price": 12.00},              # auction_breach
    ]
    seen = 0
    for kw in cases:
        for alert in monitor.evaluate([_ctx(**kw)], day="20261027"):
            if not alert.ref:
                continue
            seen += 1
            assert alert.ref.startswith(_REF_SOURCES), (alert.rule, alert.ref)
    assert seen >= 4, "至少要有几条带引用的规则被覆盖到"


def test_a_rule_with_nothing_to_quote_stays_silent():
    """「放量下跌和缩量下跌是两件事」是解读，不是引用——它现在归模型说。"""
    out = monitor.evaluate([_ctx(last=12.30, amount=3.2e8, adv20=1.1e8)],
                           day="20261027")
    by_rule = {a.rule: a for a in out}
    assert by_rule["volume_breakdown"].ref == ""
    assert by_rule["big_drop"].ref == ""
    assert by_rule["stop_breach"].ref.startswith("计划：")
    assert "先减风险" not in " ".join(by_rule["stop_breach"].facts)


def test_the_mail_says_the_reading_happens_elsewhere():
    ctx = _ctx(last=12.40)
    alerts = monitor.evaluate([ctx], day="20261027")
    text = monitor.render_text(alerts, day="20261027", stamp="10:30", contexts=[ctx])
    assert "引用 计划：" in text
    assert "解读在 quant-desk 工作区里做" in text
    assert "不给买卖建议" in text


# ------------------------------------------------- 引用必须指向还在的条款
def _active_playbook() -> str:
    """当前生效的 playbook 正文；没有就不测（引用的对象不在仓库里）。"""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    meta_path = root / "playbook" / "meta.json"
    if not meta_path.exists():
        return ""
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    active = meta.get("active")
    entry = next((v for v in meta.get("versions", []) if v["version"] == active), None)
    if entry is None:
        return ""
    body = root / "playbook" / entry["file"]
    return body.read_text(encoding="utf-8") if body.exists() else ""


def test_every_citation_that_points_at_the_playbook_still_exists():
    """引用指向一段已经改掉的文本，比不引用更糟：它看起来仍然有权威。

    这条测试是唯一能让「计划：」跟着 playbook 一起改的机制——删掉那句条款，
    构建就会红，而不是继续引用一个已经不存在的承诺。
    """
    import pytest

    text = _active_playbook()
    if not text:
        pytest.skip("仓库里没有生效的 playbook")
    for key, entry in monitor.CITATIONS.items():
        phrase = entry.get("playbook")
        if phrase:
            assert phrase in text, f"{key} 引用的「{phrase}」已不在当前 playbook 里"


def test_cite_is_the_only_way_a_ref_gets_built():
    """标签和措辞必须绑在一起，否则「计划：」可以配上一句不是计划的话。"""
    assert monitor.cite("auction") == "计划：低开超 2% 属于 playbook 的「不隔夜死扛」一档"
    assert monitor.cite("stop", stop="12.42").startswith("计划：本仓硬止损 = 跌破 12.42")
    # 这个数来自 entry x 0.95（playbook 的固定硬止损），不是票据上的 stop_price。
    # 原文写「建仓时写入」，等于给一个从未写入过的数字编了出处；而 stops /
    # observation / duty 读的是票据，两边可能给出两个不同且都像权威的止损。
    assert "建仓时写入" not in monitor.CITATIONS["stop"]["text"]
    assert monitor.cite("stop_distance", gap=-0.97).endswith("-0.97%")
    # 跌停系数从跌停价本身反推，不写死：0.9 只对主板成立，创业板/科创板是 0.8、
    # 北交所 0.7、ST 0.95。算式算不出旁边那个数时，读者只会怀疑价格。
    assert monitor.cite("limit_down_price", prev="13.07", band="0.80") == (
        "算式：跌停价 = 昨收 13.07 × 0.80")
    assert "0.9}" not in monitor.CITATIONS["limit_down_price"]["text"]
    samples = {"stop": "12.42", "prev": "13.07", "gap": -0.97, "band": "0.90"}
    for key in monitor.CITATIONS:
        assert monitor.cite(key, **samples).startswith(monitor.REF_SOURCES), key


def test_auction_line_uses_previous_close_not_the_live_price_itself():
    ctx = _ctx(prev_close=13.07, last=12.70, auction_price=12.70)
    assert ctx.auction_line == 12.81
    assert "auction_breach" in _rules(monitor.evaluate([ctx], day="20261027"))


def test_adv20_thousand_cny_is_normalized_before_the_volume_rule():
    out = monitor.evaluate([
        _ctx(last=12.40, prev_close=13.07, amount=1.2e7,
             adv20=5000, adv20_unit="thousand_cny")], day="20261027")
    alert = next(a for a in out if a.rule == "volume_breakdown")
    assert "2.4 倍" in " ".join(alert.facts)


def test_invalid_entry_is_data_invalid_not_a_zero_division():
    out = monitor.evaluate([_ctx(entry=0)], day="20261027")
    assert _rules(out) == {"data_invalid"}
    out = monitor.evaluate([_ctx(entry=None)], day="20261027")
    assert _rules(out) == {"data_invalid"}


def test_stale_quotes_suppress_all_price_rules():
    out = monitor.evaluate([_ctx(last=12.40)], day="20261027", quote_age=1000)
    assert _rules(out) == {"data_stale"}
    assert "unknown" in " ".join(out[0].facts)


def test_html_escapes_every_dynamic_text_field():
    alert = monitor.Alert(
        rule="x", severity=monitor.WARN, title="<img src=x>",
        symbol="<b>symbol</b>", facts=["<script>fact</script>"], ref="计划：<b>ref</b>")
    ctx = _ctx(symbol="<svg>symbol</svg>")
    rendered = monitor.render_html(
        [alert], day="<b>day</b>", stamp="<i>stamp</i>", contexts=[ctx],
        note="<script>note</script>")
    for raw in ("<img src=x>", "<script>fact</script>", "<b>ref</b>",
                "<svg>symbol</svg>", "<b>day</b>", "<i>stamp</i>", "<script>note</script>"):
        assert raw not in rendered
    assert "&lt;script&gt;note&lt;/script&gt;" in rendered


# ------------------------------------------------------- monitor 命令的副作用
def _once_harness(monkeypatch, tmp_path):
    """One real alert in a temporary store; no desk, network, or real mailbox."""
    import importlib

    mod = importlib.import_module("scripts.monitor")
    db = tmp_path / "external.sqlite"
    store = ExternalStore(db)
    alert = monitor.Alert(rule="stop_breach", severity=monitor.ALERT,
                          title="跌破止损", symbol="000700.SZ", facts=["现价 12.40"])
    monkeypatch.setattr(mod, "DB", db)
    monkeypatch.setattr(mod, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(mod.analyst, "HANDOFF", tmp_path / "handoff")
    monkeypatch.setattr(mod, "_account_alerts", lambda *args, **kwargs: [alert])
    return mod, store


def _once_args(*, dry_run=False):
    from types import SimpleNamespace

    return SimpleNamespace(force=False, dry_run=dry_run)


def test_once_dry_run_does_not_claim_and_the_next_real_run_sends(monkeypatch, tmp_path):
    mod, store = _once_harness(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(mod, "send_mail", lambda subject, text, html: sent.append(subject) or {
        "sent": True, "to": ["b@qq.com"], "host": "test"})

    assert mod.cmd_once(_once_args(dry_run=True)) == 0
    assert store.alerts() == []
    assert not (tmp_path / "handoff").exists()
    assert sent == []

    assert mod.cmd_once(_once_args()) == 0
    assert len(sent) == 1
    assert [row["status"] for row in store.alerts()] == ["sent"]


def test_once_dry_run_does_not_create_the_database_or_schema(monkeypatch, tmp_path):
    import importlib

    mod = importlib.import_module("scripts.monitor")
    db = tmp_path / "missing" / "external.sqlite"
    monkeypatch.setattr(mod, "DB", db)

    assert mod.cmd_once(_once_args(dry_run=True)) == 0
    assert not db.exists()
    assert not db.parent.exists()


def test_render_failure_releases_claims_so_the_next_run_sends(monkeypatch, tmp_path):
    mod, store = _once_harness(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(mod, "send_mail", lambda subject, text, html: sent.append(subject) or {
        "sent": True, "to": ["b@qq.com"], "host": "test"})
    render_text = mod.monitor.render_text
    monkeypatch.setattr(mod.monitor, "render_text",
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad render")))

    assert mod.cmd_once(_once_args()) == 2
    assert store.alerts() == []
    assert sent == []

    monkeypatch.setattr(mod.monitor, "render_text", render_text)
    assert mod.cmd_once(_once_args()) == 0
    assert len(sent) == 1


def test_mail_failure_releases_claims_so_the_next_run_retries(monkeypatch, tmp_path):
    mod, store = _once_harness(monkeypatch, tmp_path)
    attempts = []

    def flaky_mail(subject, text, html):
        attempts.append(subject)
        if len(attempts) == 1:
            raise RuntimeError("SMTP unavailable")
        return {"sent": True, "to": ["b@qq.com"], "host": "test"}

    monkeypatch.setattr(mod, "send_mail", flaky_mail)
    assert mod.cmd_once(_once_args()) == 2
    assert store.alerts() == []

    assert mod.cmd_once(_once_args()) == 0
    assert len(attempts) == 2
    assert [row["status"] for row in store.alerts()] == ["sent"]


def test_unknown_delivery_keeps_the_dedupe_key_and_does_not_blindly_resend(
        monkeypatch, tmp_path):
    mod, store = _once_harness(monkeypatch, tmp_path)
    attempts = []

    def unknown_mail(subject, text, html):
        attempts.append(subject)
        raise mod.MailDeliveryUnknown("accepted, then disconnected")

    monkeypatch.setattr(mod, "send_mail", unknown_mail)
    assert mod.cmd_once(_once_args()) == 2
    assert [row["status"] for row in store.alerts()] == ["claimed"]

    assert mod.cmd_once(_once_args()) == 0
    assert len(attempts) == 1


def test_external_job_checks_native_exit_codes_and_fails_fast():
    from pathlib import Path

    script = (Path(__file__).resolve().parents[2] / "scripts" / "external_job.ps1").read_text(
        encoding="utf-8-sig")
    assert "$LASTEXITCODE" in script
    assert "exit $code" in script
    assert "exit 0" not in script

    close = script.split("'close' {", 1)[1].split("'preopen' {", 1)[0]
    steps = [close.index(f"-Name '{name}'") for name in ("import", "report", "digest")]
    assert steps == sorted(steps)
    assert close.count("Invoke-LoggedStep") == 3, "import 失败时不得继续 report/digest"


def test_external_job_powershell_parser_accepts_the_script():
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is not installed")
    script = Path(__file__).resolve().parents[2] / "scripts" / "external_job.ps1"
    quoted_path = str(script).replace("'", "''")
    command = (
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{quoted_path}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }")
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False)
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")


def test_the_lunch_break_is_its_own_session_not_intraday():
    """11:30–13:00 没有成交，报价天然 `is_realtime=false`。

    旧分档把 09:30–15:00 全算 intraday，于是每个交易日 11:35 那一轮都会得到
    `002185.SZ 的报价不是实时的（source=ths，age=None）`，配上 WARN 和一份假的
    解读交接。11-06 与 11-09 的 11:35 留下了字面完全一样的两条。
    """
    from datetime import datetime

    from scripts.monitor import _session

    assert _session(datetime(2026, 11, 9, 9, 29)) == "preopen"
    assert _session(datetime(2026, 11, 9, 11, 29)) == "intraday"
    assert _session(datetime(2026, 11, 9, 11, 30)) == "lunch"
    assert _session(datetime(2026, 11, 9, 12, 59)) == "lunch"
    assert _session(datetime(2026, 11, 9, 13, 0)) == "intraday"
    assert _session(datetime(2026, 11, 9, 15, 0)) == "intraday"
    assert _session(datetime(2026, 11, 9, 15, 1)) == "close"


def test_a_lunch_quote_is_the_last_print_not_a_stale_feed():
    """午休不是「行情未更新」。同一个非实时报价，盘中要报警，午休照常判。

    11:30 后行情源不再出 tick，`is_realtime=false`、`age=None` 是正常状态；
    旧分档把 11:35 也算盘中，于是每天中午固定产出一条假 WARN 和一份假交接。
    """
    from types import SimpleNamespace

    from scripts import monitor as script

    class _Quote:
        symbol = "000700.SZ"
        last, pre_close, open = 13.0, 13.0, 13.0
        high, low = 13.1, 12.9
        volume, amount = 1e6, 1.3e7
        up_limit, down_limit = 14.3, 11.7
        source, age_seconds, is_realtime = "ths", None, False

    class _Desk:
        market = SimpleNamespace(
            latest_bar_date=lambda: "20261106",
            history=lambda *a: None,
            snapshot=lambda s: (_ for _ in ()).throw(AssertionError("不该读日线")),
        )

        def quotes(self, symbols):
            return {"000700.SZ": _Quote()}

    held = {"000700.SZ": SimpleNamespace(qty=100, cost=13.1, avg_price=13.05)}

    contexts, stale = script._contexts(_Desk(), held, "lunch")
    assert stale is None, stale
    assert [c.symbol for c in contexts] == ["000700.SZ"]

    contexts, stale = script._contexts(_Desk(), held, "intraday")
    assert contexts == []
    assert "不是实时的" in stale


def _account_store(as_of: str, market_value: float = 2994.0):

    class _Store:
        def days(self, n):
            return [{"as_of": as_of, "cash": 4695.95, "equity": 7689.95,
                     "market_value": market_value, "source": "manual:user"}]

    return _Store()


def test_an_account_number_from_another_day_is_not_a_bookkeeping_gap(capsys):
    """11-05 的市值是 11-05 的价格算出来的，不能拿去比今天的实时市值。

    实测两次：11-06 报「差 +78」、11-09 报「差 +90」，措辞都是「可能有买入
    没有记进账本」。股数其实一直是 200，`reconcile` 按持仓快照核对通过；
    差额只是 2994 与 200×现价 的差，完全由行情决定。
    """
    from types import SimpleNamespace

    from scripts import monitor as script

    held = [SimpleNamespace(qty=200, last=14.52, quote_source="ths")]
    alerts = script._account_alerts(held, session="intraday",
                                    store=_account_store("20261105"))
    assert alerts == []
    assert "不是今天的" in capsys.readouterr().out


def test_todays_account_number_still_reports_a_real_gap():
    """同一天的号码对不上，才是真的「有买入没记进账本」。"""
    from datetime import datetime
    from types import SimpleNamespace

    from scripts import monitor as script
    from service.kernel.clock import SHANGHAI

    today = datetime.now(SHANGHAI).strftime("%Y%m%d")
    held = [SimpleNamespace(qty=200, last=14.52, quote_source="ths")]
    alerts = script._account_alerts(held, session="intraday",
                                    store=_account_store(today))
    assert [a.rule for a in alerts] == ["holdings_drift"]


def test_alert_history_reconstructs_open_from_cutoff_not_latest_snapshot(tmp_path):
    import json

    from service.notify import alert_log

    rows = [
        {"ts": "2026-11-13T09:40:00+08:00", "event": "fired", "rule": "stop",
         "symbol": "X", "severity": "high", "title": "early"},
        {"ts": "2026-11-13T10:40:00+08:00", "event": "cleared", "rule": "stop",
         "symbol": "X", "severity": "high", "title": "cleared"},
        {"ts": "2026-11-13T14:40:00+08:00", "event": "fired", "rule": "stop",
         "symbol": "X", "severity": "critical", "title": "future"},
    ]
    (tmp_path / "20261113_alerts.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    (tmp_path / "20261113_heartbeat.json").write_text(
        json.dumps({"last_run_at": "2026-11-13T15:00:00+08:00", "outcome": "closed"}),
        encoding="utf-8")

    early = alert_log.read("20261113", base=tmp_path, scope="paper",
                           as_of="2026-11-13T10:00:00+08:00")
    assert early["fired"] == 1 and early["cleared"] == 0
    assert [row["title"] for row in early["items"]] == ["early"]
    assert len(early["open"]) == 1
    assert early["open_basis"] == "reconstructed_from_transitions_at_cutoff"
    assert early["heartbeat_pit_available"] is False
    assert "不表示监控从未运行" in early["note"]

    later = alert_log.read("20261113", base=tmp_path, scope="paper",
                           as_of="2026-11-13T11:00:00+08:00")
    assert later["fired"] == 1 and later["cleared"] == 1
    assert later["open"] == []
    assert all(row["title"] != "future" for row in later["items"])
