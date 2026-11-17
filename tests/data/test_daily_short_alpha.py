"""The close job settles existing judgments; it never selects stocks itself."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import daily_job
import service.trading as trading_module
from service.trading import short_alpha, short_alpha_tracking


def test_non_trading_day_does_not_even_build_a_desk(monkeypatch, tmp_path):
    monkeypatch.setattr(trading_module, "build_desk",
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not run")))
    result = daily_job._settle_short_alpha_oos("20261115", False, root=tmp_path)
    assert result["settled"] is False
    assert result["reason"] == "not a trading day"
    assert not list(tmp_path.rglob("*.json"))


def test_close_settles_and_reports_without_calling_any_selector(monkeypatch, tmp_path):
    desk = object()
    seen = {}
    monkeypatch.setattr(trading_module, "build_desk", lambda *, root: desk)

    def settle(desk_arg, *, root):
        seen["settle_root"] = root
        return {"checked": 2, "changed": 1}

    def report(desk_arg, *, root, scope, limit):
        seen.update(report_root=root, scope=scope, limit=limit)
        return {"ok": True, "days": [], "scope": scope}

    monkeypatch.setattr(short_alpha_tracking, "settle", settle)
    monkeypatch.setattr(short_alpha_tracking, "report", report)
    monkeypatch.setattr(short_alpha_tracking, "record_judgment",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no auto judgment")))
    monkeypatch.setattr(short_alpha, "analyze",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no auto candidates")))
    monkeypatch.setattr(short_alpha, "evidence_pack",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no auto evidence")))

    result = daily_job._settle_short_alpha_oos("20261113", True, root=tmp_path)
    assert result["settled"] is True and result["records"] == 2
    assert seen["settle_root"] == tmp_path
    assert seen["scope"] == "all" and seen["limit"] == 30
    path = tmp_path / "reports" / "daily" / "20261113_short_alpha_oos.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["judgment_capture"] == "external_workbuddy_only"
    assert payload["order_created"] is False
    assert payload["paper_ledger_modified"] is False


def test_settlement_failure_is_visible_but_does_not_crash_close(monkeypatch, tmp_path):
    monkeypatch.setattr(trading_module, "build_desk", lambda *, root: object())
    monkeypatch.setattr(short_alpha_tracking, "settle",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("measurement gap")))
    result = daily_job._settle_short_alpha_oos("20261113", True, root=tmp_path)
    assert result["settled"] is False
    assert "RuntimeError: measurement gap" in result["reason"]
    assert result["judgment_capture"] == "external_workbuddy_only"
