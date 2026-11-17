from __future__ import annotations

import json
import sys
import types
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts import (
    backfill,
    backfill_cb,
    core_job,
    daily_job,
    decay_check,
    fetch_text,
    ingest_text,
    prepare_session,
    recost,
    run_research,
    stop_check,
)
from service.research.spec import SpecError


class _Registry:
    def __init__(self, *_args):
        self.ingest = []

    def summary(self):
        return []

    def mark_ingest(self, *args, **kwargs):
        self.ingest.append((args, kwargs))


class _Store:
    def __init__(self, *_args):
        self.registry = None

    def read(self, *_args, **_kwargs):
        return pd.DataFrame()

    def read_partition(self, *_args, **_kwargs):
        return pd.DataFrame()


def _market_desk():
    return SimpleNamespace(
        data=object(),
        market=SimpleNamespace(latest_bar_date=lambda: "20240102"),
        book=SimpleNamespace(summary=lambda: {"positions": []}),
    )


def test_fetch_text_rejects_nonpositive_limits_before_building_desk(monkeypatch):
    monkeypatch.setattr(fetch_text, "build_desk", lambda _root: pytest.fail("desk built"))
    assert fetch_text.main(["--symbols", "600000.SH", "--per", "0"]) == 2
    assert fetch_text.main(["--symbols", "600000.SH", "--top", "-1"]) == 2


def test_fetch_text_vendor_failure_is_an_exit_failure(monkeypatch):
    monkeypatch.setattr(fetch_text, "build_desk", lambda _root: _market_desk())

    def fail(_symbols, _per):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(fetch_text, "fetch", fail)
    assert fetch_text.main(["--symbols", "600000.SH"]) == 1


def test_fetch_raises_after_all_symbols_fail_without_calling_real_network(monkeypatch):
    fake = types.ModuleType("akshare")

    def fail(symbol):
        raise OSError(f"down {symbol}")

    fake.stock_news_em = fail
    monkeypatch.setitem(sys.modules, "akshare", fake)
    with pytest.raises(RuntimeError, match="2 symbol"):
        fetch_text.fetch(["600000.SH", "000001.SZ"], 3)


def test_prepare_returns_failure_when_analog_warmup_fails(monkeypatch, capsys):
    monkeypatch.setattr(prepare_session, "build_desk", lambda _root: _market_desk())
    monkeypatch.setattr(prepare_session.analog, "get_index",
                        lambda *_args: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(prepare_session, "snapshot", lambda *_args: {})
    assert prepare_session.main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["analog_index"]["error"]


def test_backfill_validates_numeric_arguments_before_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(backfill, "ROOT", tmp_path)
    monkeypatch.setattr(backfill, "Registry", _Registry)
    monkeypatch.setattr(backfill, "DatasetStore", _Store)
    monkeypatch.setattr(backfill, "ensure_calendar",
                        lambda *_args: pytest.fail("calendar setup ran"))
    assert backfill.main(["--flush-every", "0"]) == 2
    assert backfill.main(["--rate", "0"]) == 2
    assert backfill.main(["--start", "20240103", "--end", "20240102"]) == 2
    assert backfill.main(["--start", "not-a-date"]) == 2


def test_ingest_failure_is_nonzero_and_keeps_file_for_retry(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    path = inbox / "20240102.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(ingest_text, "ROOT", tmp_path)
    monkeypatch.setattr(ingest_text, "INBOX", inbox)
    monkeypatch.setattr(ingest_text, "DONE", inbox / "done")
    monkeypatch.setattr(ingest_text, "Registry", _Registry)
    monkeypatch.setattr(ingest_text, "DatasetStore", _Store)
    monkeypatch.setattr(ingest_text, "ingest",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    assert ingest_text.main([]) == 1
    assert path.exists()


def test_ingest_rejections_are_nonzero_and_not_archived_as_success(monkeypatch, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    path = inbox / "20240102.json"
    path.write_text('[{"title": "bad"}]', encoding="utf-8")
    monkeypatch.setattr(ingest_text, "ROOT", tmp_path)
    monkeypatch.setattr(ingest_text, "INBOX", inbox)
    monkeypatch.setattr(ingest_text, "DONE", inbox / "done")
    monkeypatch.setattr(ingest_text, "Registry", _Registry)
    monkeypatch.setattr(ingest_text, "DatasetStore", _Store)
    monkeypatch.setattr(ingest_text, "ingest", lambda *_args, **_kwargs: {
        "written": 0, "rejected": 1, "details": [], "by_date": {},
    })
    assert ingest_text.main([]) == 1
    assert path.exists()
    assert not (inbox / "done" / path.name).exists()


def _bind_daily(monkeypatch, tmp_path, *, trading: bool, verify_rc: int = 0,
                text_rc: int = 0):
    monkeypatch.setattr(daily_job, "ROOT", tmp_path)
    registry = _Registry()
    monkeypatch.setattr(daily_job, "Registry", lambda _path: registry)
    monkeypatch.setattr(daily_job, "DatasetStore", _Store)
    monkeypatch.setattr(daily_job, "backup_databases", lambda *_args: [])
    monkeypatch.setattr(daily_job.backfill, "main", lambda _argv: 0)
    monkeypatch.setattr(daily_job.verify_data, "main", lambda _argv: verify_rc)
    monkeypatch.setattr(daily_job, "build_brief", lambda **_kwargs: {"core": {"usable": True}})

    class Calendar:
        days = ("20240102",)

        @staticmethod
        def is_trading_day(_day):
            return trading

        @staticmethod
        def prev(_day):
            return "20240102"

    monkeypatch.setattr(daily_job.TradingCalendarStore, "from_frame", lambda *_args: Calendar())

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 1, 2, 18, 0, tzinfo=tz)

    monkeypatch.setattr(daily_job, "datetime", FixedDatetime)

    dashboard = types.ModuleType("service.api.dashboard")
    dashboard.collect = lambda _desk: {}
    monkeypatch.setitem(sys.modules, "service.api.dashboard", dashboard)
    import service.trading

    monkeypatch.setattr(service.trading, "build_desk", lambda **_kwargs: object())

    if not trading:
        return

    for name, rc in (("fetch_ths_special", 0), ("derive_pools", 0),
                     ("backfill_cb", 0), ("fetch_text", text_rc)):
        module = types.ModuleType(f"scripts.{name}")
        module.main = lambda _argv, rc=rc: rc
        monkeypatch.setitem(sys.modules, f"scripts.{name}", module)
        import scripts

        monkeypatch.setattr(scripts, name, module, raising=False)

    sentiment = types.ModuleType("service.trading.sentiment")
    sentiment.panel = lambda _pools, **_kwargs: {
        "limit_up": {}, "broken_board": {}, "limit_down": {},
    }
    # ``daily_job`` builds the close panel through ``close_panel`` (derived
    # counts first); the stub has to expose the same door, or the import inside
    # the step raises and the test reads a "sentiment failed" it never meant to
    # create.
    sentiment.close_panel = lambda _store, _day: sentiment.panel({})
    monkeypatch.setitem(sys.modules, "service.trading.sentiment", sentiment)


def test_daily_verification_failure_marks_gate_not_ready(monkeypatch, tmp_path):
    _bind_daily(monkeypatch, tmp_path, trading=False, verify_rc=1)
    assert daily_job.main([]) == 1
    payload = json.loads((tmp_path / "reports" / "daily" / "20240102.json").read_text())
    assert payload["verify_rc"] == 1
    assert payload["gate"] == "not_ready"


def test_daily_supplementary_step_failure_is_visible_without_blocking_ready_facts(
        monkeypatch, tmp_path):
    _bind_daily(monkeypatch, tmp_path, trading=True, text_rc=1)
    assert daily_job.main([]) == 0
    payload = json.loads((tmp_path / "reports" / "daily" / "20240102.json").read_text())
    assert payload["text"]["rc"] == 1
    assert payload["gate"] == "ready"
    assert payload["failed_steps"] == []
    assert payload["supplementary_failed"] == ["text"]


def _bind_cb(monkeypatch, tmp_path, *, daily_error=False, write_error=False):
    monkeypatch.setattr(backfill_cb, "ROOT", tmp_path)
    monkeypatch.setenv("TUSHARE_TOKEN", "fake")
    registry = _Registry()
    monkeypatch.setattr(backfill_cb, "Registry", lambda _path: registry)

    class Store(_Store):
        def write(self, dataset, _partition, _frame, **_kwargs):
            if dataset == "cb_daily" and write_error:
                raise OSError("read-only partition")

    monkeypatch.setattr(backfill_cb, "DatasetStore", Store)
    monkeypatch.setattr(backfill_cb.TradingCalendarStore, "from_frame",
                        lambda *_args: SimpleNamespace(days=["20240102"]))

    class Pro:
        @staticmethod
        def cb_basic():
            return pd.DataFrame([{"ts_code": "110001.SH"}])

        @staticmethod
        def cb_daily(trade_date):
            if daily_error:
                raise OSError(f"vendor down {trade_date}")
            return pd.DataFrame([{"ts_code": "110001.SH", "trade_date": trade_date}])

    monkeypatch.setattr(backfill_cb.ts, "pro_api", lambda _token: Pro())
    monkeypatch.setattr(backfill_cb.time, "sleep", lambda _seconds: None)
    return registry


def test_cb_vendor_failure_returns_two_and_never_marks_done(monkeypatch, tmp_path):
    registry = _bind_cb(monkeypatch, tmp_path, daily_error=True)
    assert backfill_cb.main(["--start", "20240102", "--end", "20240102"]) == 2
    assert not any(args[3] == "done" for args, _kwargs in registry.ingest)


def test_cb_partition_write_failure_returns_two(monkeypatch, tmp_path):
    registry = _bind_cb(monkeypatch, tmp_path, write_error=True)
    assert backfill_cb.main(["--start", "20240102", "--end", "20240102"]) == 2
    assert not any(args[3] == "done" for args, _kwargs in registry.ingest)


def test_cb_success_is_the_only_zero_exit(monkeypatch, tmp_path):
    registry = _bind_cb(monkeypatch, tmp_path)
    assert backfill_cb.main(["--start", "20240102", "--end", "20240102"]) == 0
    assert any(args[3] == "done" for args, _kwargs in registry.ingest)


def _stop_record(*, intraday=False, ticket="ticket-1"):
    return {
        "id": "stop:600000.SH", "kind": "stop_breach", "subject": "600000.SH",
        "fact": {"close": "9", "stop": "10", "intraday_only": intraday,
                 "ticket_id": ticket},
        "measures": {}, "constraints": [],
    }


def test_stop_check_alerts_only_once_for_the_same_confirmed_breach(monkeypatch, tmp_path):
    monkeypatch.setattr(stop_check, "ROOT", tmp_path)
    monkeypatch.setattr(stop_check, "build_desk", lambda _root: _market_desk())
    monkeypatch.setattr(stop_check, "stop_constraints", lambda *_args: [_stop_record()])
    assert stop_check.main(["--json"]) == 1
    assert stop_check.main(["--json"]) == 0


def test_stop_check_ignores_intraday_only_pierce(monkeypatch, tmp_path):
    monkeypatch.setattr(stop_check, "ROOT", tmp_path)
    monkeypatch.setattr(stop_check, "build_desk", lambda _root: _market_desk())
    monkeypatch.setattr(stop_check, "stop_constraints",
                        lambda *_args: [_stop_record(intraday=True)])
    assert stop_check.main(["--json"]) == 0


def test_recost_records_only_new_canonical_specs(monkeypatch, tmp_path):
    monkeypatch.setattr(recost, "ROOT", tmp_path)
    monkeypatch.setattr(recost, "Registry", _Registry)
    monkeypatch.setattr(recost, "DatasetStore", _Store)
    monkeypatch.setattr(recost, "PRESETS", {
        "cf": {"pool": "cb"},
        "fresh": {"pool": "limit_up"},
        "seen": {"pool": "limit_down"},
    })

    class Trials:
        @staticmethod
        def looks(spec):
            return int(spec.pool == "limit_down")

        @staticmethod
        def program_status():
            return {"family": "f", "trials_total": 1, "min_p": 0.1,
                    "program_fdr": 0.1, "survivors_q10": 0}

    monkeypatch.setattr(recost, "TrialRegistry", lambda _path: Trials())
    calls = []

    def run(_store, spec, **kwargs):
        calls.append((spec.pool, kwargs.get("record", True)))
        return {"verdict": "testing", "best": {"trades": {}}, "shadow_blockers": []}

    monkeypatch.setattr(recost.runner, "run", run)
    assert recost.main(["--record", "--json", "recost.json"]) == 0
    assert calls == [("cb", False), ("limit_up", True), ("limit_down", False)]


def test_run_research_no_valid_variant_returns_two_without_report(monkeypatch, tmp_path):
    monkeypatch.setattr(run_research, "ROOT", tmp_path)
    monkeypatch.setattr(run_research, "Registry", _Registry)
    monkeypatch.setattr(run_research, "DatasetStore", _Store)
    monkeypatch.setattr(run_research, "PaperStore", _Registry)
    monkeypatch.setattr(run_research, "TrialRegistry", _Registry)
    monkeypatch.setattr(run_research.runner, "run",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(SpecError("no variants")))
    out = tmp_path / "empty.json"
    assert run_research.main(["--spec", "{}", "--json-out", str(out)]) == 2
    assert not out.exists()


@pytest.mark.parametrize("mode", ["unknown", "no_baseline", "no_samples"])
def test_decay_without_metrics_exits_one(monkeypatch, mode):
    monkeypatch.setattr(decay_check, "PaperStore",
                        lambda _path: SimpleNamespace(list_cases=lambda limit: [],
                                                      core_nav_history=lambda limit: []))
    if mode != "unknown":
        evidence = {} if mode == "no_baseline" else {"mean_excess": 0.01, "std": 0.02}
        meta = SimpleNamespace(status="live", evidence=evidence, book="paper")
        monkeypatch.setattr(decay_check.strategy_mod, "REGISTRY", {"demo": meta})
    assert decay_check.main(["--strategy", "demo", "--window", "2"]) == 1


def test_core_job_rejects_invalid_date_before_bookkeeping(monkeypatch, tmp_path):
    monkeypatch.setattr(core_job, "ROOT", tmp_path)
    monkeypatch.setattr(core_job, "Registry", _Registry)
    monkeypatch.setattr(core_job, "DatasetStore", _Store)
    monkeypatch.setattr(core_job, "PaperStore", _Registry)
    monkeypatch.setattr(core_job, "CoreBook",
                        lambda *_args: pytest.fail("book used for invalid date"))
    assert core_job.main(["--date", "20241340"]) == 2


def _config_probe(monkeypatch, returncode, keys):
    """跑 audit_config_keys.main()，但把 pytest 换成"写多少键、退几"的桩。"""
    from scripts import audit_config_keys

    def fake_run(_argv, **kwargs):
        out = kwargs["env"]["KEYPROBE_OUT"]
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(sorted(keys), handle)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(audit_config_keys.subprocess, "run", fake_run)
    return audit_config_keys


def test_config_probe_refuses_to_conclude_when_the_suite_failed(monkeypatch):
    """套件挂了，`seen` 只是"跑到的那部分测试读过什么"。

    2026-11-04 之前这条路是 exit 0：`pytest_sessionfinish` 在测试失败时照样写
    seen.json，于是 0 个未读键被打印成 `never read 0`——最坏的一种绿灯。
    """
    from service import settings

    probe = _config_probe(monkeypatch, 1, settings.SPECS)
    assert probe.main([]) == 2
    assert probe.main(["--strict"]) == 2, "strict 也不能把没跑完的套件说成通过"


def test_config_probe_is_green_only_after_a_green_run_with_every_key_read(monkeypatch):
    from service import settings

    probe = _config_probe(monkeypatch, 0, settings.SPECS)
    assert probe.main([]) == 0
    assert probe.main(["--strict"]) == 0


def test_config_probe_reports_unread_keys_only_when_the_run_completed(monkeypatch):
    from service import settings

    half = list(settings.SPECS)[: len(settings.SPECS) // 2]
    probe = _config_probe(monkeypatch, 0, half)

    assert probe.main([]) == 0, "默认模式：未读是问题，不是构建失败"
    assert probe.main(["--strict"]) == 1
