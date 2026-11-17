"""WorkBuddy judgment freezing and honest short-alpha OOS settlement."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from service.kernel.models import D, Instrument
from service.trading.market import Snapshot
from service.trading import short_alpha_tracking as tracking

T0 = "20261112"
DAY = "20261113"
T1 = "20261116"
T2 = "20261117"
A = "000001.SZ"
B = "000002.SZ"
C = "000003.SZ"


def _execution() -> dict:
    return {
        "account_ok": True, "scenario_qty": 100, "scenario_budget_cny": 1500.0,
        "total_round_trip_cost_bps": 100.0,
        "impact_inputs": {"adv_cny": 100_000_000.0, "sigma_daily": 0.02},
        "order_created": False,
    }


def _bars() -> pd.DataFrame:
    rows = [
        # A third stock makes the equal-weight benchmark independent of A/B.
        (C, T0, 5.0, 5.0, 5.0, 5.0, 1000),
        (C, DAY, 5.0, 5.0, 5.0, 5.0, 1000),
        (C, T1, 5.0, 5.1, 5.0, 5.0, 1000),
        (C, T2, 5.1, 5.2, 5.0, 5.1, 1000),
        (A, T0, 9.8, 9.8, 9.8, 9.8, 1000),
        (A, DAY, 9.8, 10.0, 9.8, 9.8, 1000),
        (A, T1, 10.0, 10.4, 10.1, 10.2, 1000),
        (A, T2, 10.4, 10.6, 10.4, 10.4, 1000),
        (B, T0, 19.6, 19.6, 19.6, 19.6, 1000),
        (B, DAY, 19.6, 20.0, 19.6, 19.6, 1000),
        # B is one-word at the T+1 open: research return exists, fill does not.
        (B, T1, 20.0, 22.0, 22.0, 22.0, 1000),
        (B, T2, 22.0, 22.4, 22.0, 22.0, 1000),
    ]
    return pd.DataFrame(rows, columns=[
        "ts_code", "trade_date", "pre_close", "close", "low", "open", "vol",
    ])


class _Data:
    def __init__(self, frame):
        self.frame = frame

    def read(self, dataset, *, start, end, columns):
        assert dataset == "daily"
        out = self.frame[(self.frame["trade_date"] >= start)
                         & (self.frame["trade_date"] <= end)]
        return out[columns].copy()


class _Market:
    def __init__(self, frame, latest=DAY):
        self.frame = frame
        self.day = latest

    def latest_bar_date(self):
        return self.day

    def instrument(self, symbol, day):
        return Instrument(symbol=symbol)

    def snapshot(self, symbol, day):
        hit = self.frame[(self.frame["ts_code"] == symbol)
                         & (self.frame["trade_date"] == day)]
        if hit.empty:
            return None
        row = hit.iloc[-1]
        inst = self.instrument(symbol, day)
        up, down = inst.band(D(str(row["pre_close"])))
        return Snapshot(
            symbol=symbol, trade_date=day, last=D(str(row["close"])),
            pre_close=D(str(row["pre_close"])), open=D(str(row["open"])),
            high=D(str(max(row["open"], row["close"]))), low=D(str(row["low"])),
            up_limit=up, down_limit=down, volume=float(row["vol"]) * 100,
            amount=1_000_000.0, suspended=bool(row["vol"] <= 0), source="daily",
        )


def _desk(frame=None, latest=DAY):
    frame = _bars() if frame is None else frame
    return SimpleNamespace(data=_Data(frame), market=_Market(frame, latest))


def _judgment(day=DAY) -> dict:
    return {
        "day": day,
        "reviewed_evidence": [f"short-alpha-evidence:{day}:{A}",
                              f"short-alpha-evidence:{day}:{B}"],
        "items": [
            {"symbol": A, "role": "candidate",
             "thesis": "WorkBuddy独立判断甲的封板结构与成本后收益值得进入候选观察。",
             "evidence": [f"short-alpha-evidence:{day}:{A}"],
             "invalidation": "开盘结构失效或流动性不足"},
            {"symbol": B, "role": "watch",
             "thesis": "WorkBuddy独立判断乙存在不确定性因此仅放入观察而非正式候选。",
             "evidence": [f"short-alpha-evidence:{day}:{B}"],
             "invalidation": "不能形成可执行开盘机会"},
        ],
        "no_candidate_reason": "", "replay": False,
        "client_request_id": "sa-test-0001",
    }


def _fake_evidence(monkeypatch, universe=(A, B)):
    def fake(desk, day, *, symbols, limit, offset=0):
        rows = []
        for symbol in symbols:
            if symbol not in universe:
                continue
            rows.append({
                "evidence_id": f"short-alpha-evidence:{day}:{symbol}",
                "symbol": symbol, "name": "甲" if symbol == A else "乙",
                "execution": _execution(),
                "historical_facts": {"net_excess_mean": -0.05},
            })
        return {"ok": True, "rows": rows,
                "decision_contract": {"contains_kernel_alpha_verdict": False}}

    monkeypatch.setattr(tracking, "evidence_pack", fake)


def test_freezes_workbuddy_choice_not_kernel_return_ranking(tmp_path, monkeypatch):
    _fake_evidence(monkeypatch)
    desk = _desk()
    result = tracking.record_judgment(desk, _judgment(), root=tmp_path)
    frozen = result["record"]["frozen"]
    assert frozen["judgment_owner"] == "WorkBuddy"
    assert frozen["kernel_substituted_selection"] is False
    assert [i["symbol"] for i in frozen["items"]] == [A, B]
    assert frozen["items"][0]["execution"]["order_created"] is False
    # The negative historical number is evidence, not a server-side rejection rule.
    assert frozen["evidence_pack"]["rows"][0]["historical_facts"]["net_excess_mean"] < 0
    assert "candidates" not in frozen["evidence_pack"]


def test_same_judgment_is_idempotent_but_a_different_one_cannot_replace_it(tmp_path, monkeypatch):
    _fake_evidence(monkeypatch)
    desk = _desk()
    first = tracking.record_judgment(desk, _judgment(), root=tmp_path)
    same = tracking.record_judgment(desk, _judgment(), root=tmp_path)
    assert same["created"] is False
    assert same["record"]["frozen_sha256"] == first["record"]["frozen_sha256"]
    changed = _judgment()
    changed["items"][0]["thesis"] = "事后换一个完全不同的理由试图覆盖当时已经冻结的判断。"
    with pytest.raises(tracking.TrackingConflict, match="different input refused"):
        tracking.record_judgment(desk, changed, root=tmp_path)


def test_tampered_frozen_part_is_never_repaired_or_settled(tmp_path, monkeypatch):
    _fake_evidence(monkeypatch)
    desk = _desk(latest=T2)
    result = tracking.record_judgment(desk, _judgment(), root=tmp_path)
    path = tmp_path / "data" / "short_alpha_oos" / f"{DAY}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["frozen"]["items"][0]["thesis"] = "tampered"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(tracking.TrackingConflict, match="hash mismatch"):
        tracking.settle(desk, root=tmp_path)
    assert result["record"]["frozen_sha256"] != tracking._digest(payload["frozen"])


def test_no_future_at_freeze_then_t1_and_t2_settle_independently(tmp_path, monkeypatch):
    _fake_evidence(monkeypatch)
    desk = _desk()
    tracking.record_judgment(desk, _judgment(), root=tmp_path)
    early = tracking.settle(desk, root=tmp_path)
    assert early["changed"] >= 1
    detail = tracking.report(desk, root=tmp_path, as_of=DAY)["record"]
    for item in detail["settlements"]:
        for horizon in ("t1_close", "t2_close"):
            assert item["outcomes"][horizon]["research_t_close"]["status"] == "pending"
            assert item["outcomes"][horizon]["executable_t1_open"]["status"] == "pending"

    desk.market.day = T1
    tracking.settle(desk, root=tmp_path)
    partial = tracking.report(desk, root=tmp_path, as_of=DAY)["record"]
    a1 = partial["settlements"][0]["outcomes"]
    b1 = partial["settlements"][1]["outcomes"]
    assert a1["t1_close"]["research_t_close"]["status"] == "settled"
    assert a1["t1_close"]["executable_t1_open"]["status"] == "settled"
    assert a1["t2_close"]["research_t_close"]["status"] == "pending"
    assert b1["t1_close"]["research_t_close"]["status"] == "settled"
    assert b1["t1_close"]["executable_t1_open"]["status"] == "no_fill"
    assert b1["t1_close"]["executable_t1_open"]["reason"] == "no_fill_open_one_word"
    assert partial["settlement_meta"]["status"] == "partial"

    desk.market.day = T2
    tracking.settle(desk, root=tmp_path)
    final = tracking.report(desk, root=tmp_path, as_of=DAY)["record"]
    assert final["settlement_meta"]["status"] == "settled"
    a_open = final["settlements"][0]["outcomes"]["t1_close"]["executable_t1_open"]
    assert a_open["net_excess"] == pytest.approx(
        a_open["stock_return"] - a_open["market_return"] - a_open["cost_bps"] / 10_000,
        abs=2e-6)
    b_open = final["settlements"][1]["outcomes"]["t2_close"]["executable_t1_open"]
    assert b_open["net_excess"] is None
    summary = tracking.report(desk, root=tmp_path)["summary"]["prospective"]
    assert summary["candidate_signals"] == 1
    assert summary["watch_signals"] == 1
    assert summary["groups"]["candidate"]["executable_t1_open"]["t1_close"]["n"] == 1
    assert summary["groups"]["watch"]["executable_t1_open"]["t1_close"]["n"] == 0
    assert summary["open_entry_states"]["no_fill_open_one_word"] == 1


def test_open_limit_after_seal_is_queue_unknown_not_open_auction_fill(tmp_path, monkeypatch):
    _fake_evidence(monkeypatch)
    frame = _bars()
    frame.loc[(frame["ts_code"] == B) & (frame["trade_date"] == T1), "low"] = 21.5
    desk = _desk(frame, latest=T2)
    tracking.record_judgment(desk, _judgment(), root=tmp_path)
    tracking.settle(desk, root=tmp_path)
    record = tracking.report(desk, root=tmp_path, as_of=DAY)["record"]
    entry = record["settlements"][1]["open_entry"]
    assert entry["status"] == "queue_required_fill_unknown"
    assert entry["executable_proxy"] is False
    assert record["settlements"][1]["outcomes"]["t1_close"]["executable_t1_open"]["net_excess"] is None


def test_corporate_action_or_missing_bar_is_invalid_not_zero(tmp_path, monkeypatch):
    _fake_evidence(monkeypatch)
    frame = _bars()
    frame.loc[(frame["ts_code"] == A) & (frame["trade_date"] == T1),
              ["pre_close", "open", "close", "low"]] = [9.0, 9.0, 9.2, 8.9]
    frame.loc[(frame["ts_code"] == A) & (frame["trade_date"] == T2),
              ["pre_close", "open", "close"]] = [9.2, 9.2, 9.4]
    desk = _desk(frame, latest=T2)
    tracking.record_judgment(desk, _judgment(), root=tmp_path)
    tracking.settle(desk, root=tmp_path)
    record = tracking.report(desk, root=tmp_path, as_of=DAY)["record"]
    outcomes = record["settlements"][0]["outcomes"]["t1_close"]
    assert outcomes["research_t_close"]["status"] == "invalid"
    assert outcomes["research_t_close"]["net_excess"] is None
    assert outcomes["executable_t1_open"]["status"] == "invalid"


def test_missing_t1_bar_is_invalid_or_no_fill_never_zero(tmp_path, monkeypatch):
    _fake_evidence(monkeypatch)
    frame = _bars()
    frame = frame[~((frame["ts_code"] == A) & (frame["trade_date"] == T1))].copy()
    desk = _desk(frame, latest=T2)
    tracking.record_judgment(desk, _judgment(), root=tmp_path)
    tracking.settle(desk, root=tmp_path)
    record = tracking.report(desk, root=tmp_path, as_of=DAY)["record"]
    outcomes = record["settlements"][0]["outcomes"]["t1_close"]
    assert outcomes["research_t_close"]["status"] == "invalid"
    assert outcomes["research_t_close"]["net_excess"] is None
    assert outcomes["executable_t1_open"]["status"] == "no_fill"
    assert outcomes["executable_t1_open"]["reason"] == "no_fill_missing_open_bar"
    assert outcomes["executable_t1_open"]["net_excess"] is None


def test_explicit_no_candidate_is_not_an_unregistered_day(tmp_path, monkeypatch):
    _fake_evidence(monkeypatch)
    desk = _desk()
    empty = tracking.report(desk, root=tmp_path)
    assert empty["latest_bar_judgment_registered"] is False
    judgment = _judgment()
    judgment["items"] = []
    judgment["no_candidate_reason"] = "WorkBuddy阅读全部证据后明确认为今天没有值得登记的候选。"
    result = tracking.record_judgment(desk, judgment, root=tmp_path)
    assert result["record"]["judgment_status"] == "explicit_no_candidate"
    out = tracking.report(desk, root=tmp_path)
    assert out["latest_bar_judgment_registered"] is True
    assert out["days"][0]["status"] == "no_candidate"
    assert out["days"][0]["candidate_count"] == 0
    assert out["summary"]["prospective"]["signal_days"] == 1


def test_historical_backfill_is_replay_and_never_enters_prospective(tmp_path, monkeypatch):
    _fake_evidence(monkeypatch)
    desk = _desk(latest=T2)
    judgment = _judgment(day=T0)
    judgment["items"] = []
    judgment["no_candidate_reason"] = "仅作历史回放，不计入真正的前瞻样本外结论。"
    result = tracking.record_judgment(desk, judgment, root=tmp_path)
    assert result["record"]["frozen"]["capture_kind"] == "replay"
    prospective = tracking.report(desk, root=tmp_path, scope="prospective")
    replay = tracking.report(desk, root=tmp_path, scope="replay")
    assert prospective["days"] == []
    assert replay["summary"]["replay"]["signal_days"] == 1
    assert replay["summary"]["prospective"]["signals"] == 0
