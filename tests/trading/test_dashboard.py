"""看板只剩数据层：一个 payload，窗口和 /dashboard.json 都读它。

这里钉两件事：
* ``/dashboard.json`` 仍然给出完整的块，模型可以直接引用；
* 某一块坏掉时整份 payload 不能跟着消失——账户出问题的时候恰好最需要看它。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from service.api import app as app_module
from service.api import dashboard as dash
from service.api import trading as trading_api
from service.api.app import app

TOKEN = {"X-Quant-Token": "change-me-local-only"}


@pytest.fixture
def client(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    with TestClient(app) as c:
        yield c


def test_json_mirror_has_the_same_blocks(client):
    body = client.get("/dashboard.json", headers=TOKEN).json()
    assert set(body) >= {"day", "summary", "arena", "duty", "persona",
                         "events", "usage", "brief", "external"}
    assert body["day"]


def test_the_html_page_is_gone(client):
    """桌面窗口是唯一的人类界面；留一个浏览器页面就是留一份要同步的判断。"""
    assert client.get("/dashboard").status_code == 404


def test_timeline_collects_acts_from_more_than_one_table(desk):
    day = desk.market.latest_bar_date()
    desk.paper.log_decision("memo", {"id": 99, "topic": "测试用备忘录"}, actor="agent")
    desk.paper.add_journal("今天的复盘：什么都没做，因为闸门是关的", kind="review")
    kinds = {e["kind"] for e in dash.collect_timeline(desk, day)}
    assert "memo" in kinds
    assert "review" in kinds, "journal rows keep their own kind (review/plan/note)"


def test_timeline_reads_past_page_caps_before_filtering_the_session():
    from types import SimpleNamespace

    class History:
        def list_decisions(self, limit):
            newer = [{"ts": "2026-12-01T09:30:00+08:00", "kind": "note",
                      "payload": {"text": "outside"}} for _ in range(400)]
            target = {"ts": "2026-10-26T09:30:00+08:00", "kind": "memo",
                      "payload": {"id": 1, "topic": "目标决策"}}
            return (newer + [target])[:limit]

        def persona_switches(self, limit):
            newer = [{"ts": "2026-12-01T09:30:00+08:00", "day": "20261201"}
                     for _ in range(100)]
            target = {"ts": "2026-10-26T09:31:00+08:00", "day": "20261026",
                      "from_key": "retail", "to_key": "trader"}
            return (newer + [target])[:limit]

        def search_journal(self, limit):
            newer = [{"ts": "2026-12-01T09:30:00+08:00", "kind": "note",
                      "text": "outside"} for _ in range(100)]
            target = {"ts": "2026-10-26T09:32:00+08:00", "kind": "review",
                      "text": "目标复盘"}
            return (newer + [target])[:limit]

    events = dash.collect_timeline(SimpleNamespace(paper=History()), "20261026")
    assert {event["kind"] for event in events} == {"memo", "persona_switch", "review"}
    assert any(event["title"].endswith("目标决策") for event in events)
    assert any(event["detail"] == "目标复盘" for event in events)


def test_one_broken_block_does_not_blank_the_payload(desk, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("regime store unavailable")

    monkeypatch.setattr("service.trading.regime.compute", boom)
    body = dash.collect(desk)
    assert body["day"]
    assert body["regime"] == ""
    assert body["block_status"]["regime"] == "error"
    assert "regime store unavailable" in body["errors"]["regime"]
    assert body["status"] == "degraded"
    assert "summary" in body


def test_failed_blocks_are_distinguishable_from_normal_empty_blocks(desk, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("block unavailable")

    monkeypatch.setattr("service.trading.arena.scoreboard", boom)
    monkeypatch.setattr("service.trading.duty.build", boom)
    monkeypatch.setattr("service.trading.regime.compute", boom)
    monkeypatch.setattr("service.trading.persona.probe", boom)
    monkeypatch.setattr("service.adapters.brief.build_brief", boom)

    body = dash.collect(desk)

    for name in ("arena", "duty", "regime", "persona", "brief"):
        assert body["block_status"][name] == "error"
        assert "block unavailable" in body["errors"][name]
    assert body["duty"]["items"] == [], "兼容字段保留，但不再独自承担状态语义"
    assert body["status"] == "degraded"


def test_an_empty_duty_list_is_empty_not_an_error(desk, monkeypatch):
    monkeypatch.setattr("service.trading.duty.build", lambda *_args: {"items": []})

    body = dash.collect(desk)

    assert body["block_status"]["duty"] == "empty"
    assert "duty" not in body.get("errors", {})


def test_dashboard_duty_matches_the_duty_route(client):
    """Two answers to "what is urgent" would drift; they share one function."""
    from service.trading.duty import build

    page_duty = build(trading_api.get_desk())
    route_duty = client.get("/duty", headers=TOKEN).json()
    assert [i["kind"] for i in page_duty["items"]] == [i["kind"] for i in route_duty["items"]]


def test_slim_drops_only_what_has_its_own_endpoint(desk):
    """Full page runs ~24.5KB, and an MCP host drops an oversized body whole.

    Slim must keep every field a decision reads — including the data-readiness
    verdict — and drop only the two blocks that already have their own route:
    ``usage`` -> /usage, and the optional-dataset detail -> /data/brief.
    Trimming anything else would trade one silence for another.

    The timeline is the one block slim shortens rather than drops: it is
    unbounded (each entry carries a note's full text, ~450 B), so a busy
    afternoon would have taken the page past the 16 KB ceiling and the host
    would have dropped the whole body. Slim keeps the last ten acts and says
    how many there were.
    """
    full = dash.collect(desk)
    slim = dash.collect(desk, slim=True)
    assert "usage" in full and "usage" not in slim
    assert slim["slim"] is True
    # Same blocks, minus `usage`, plus the two keys that declare the trim;
    # values are not compared because `summary` carries a wall-clock `as_of`
    # that moves between two calls.
    assert set(slim) - {"slim", "events_total", "events_truncated"} == (
        set(full) - {"usage"})
    if full["brief"]:
        assert slim["brief"]["core"] == full["brief"]["core"]
        assert slim["brief"]["aggressive"] == full["brief"]["aggressive"]
        assert "optional" not in slim["brief"]
    assert len(json.dumps(slim, ensure_ascii=False, indent=1)) < 16000


def test_slim_says_how_many_acts_the_timeline_had(desk, monkeypatch):
    """截短的时间线要自己说短了——否则读者会把 10 条当成全部。"""
    from service.trading import timeline as timeline_mod

    real = timeline_mod.collect

    def many(desk_, day, limit=60):
        items = real(desk_, day, limit=limit)
        return items + [{"ts": "2026-11-02T15:00:00+08:00", "kind": "note",
                         "label": "笔记", "tone": "note", "title": "",
                         "detail": "x" * 400} for _ in range(20)]

    monkeypatch.setattr(timeline_mod, "collect", many)
    slim = dash.collect(desk, slim=True)

    assert slim["events_total"] >= 20, "总数要写出来"
    assert slim.get("events_truncated") is True, "截短了就要说"
    assert len(slim["events"]) <= 10, "只留最后几条"
    for item in slim["events"]:
        if isinstance(item.get("detail"), str):
            assert len(item["detail"]) <= 241, "正文也要截短"


def test_acts_after_the_reported_session_still_show():
    """`day` is yesterday for most of the day; today's acts must not disappear."""
    from types import SimpleNamespace

    desk = SimpleNamespace()

    assert dash._belongs_to(desk, "2026-10-26T23:59:00+08:00", "20261026") is True
    assert dash._belongs_to(desk, "2026-10-27T00:05:00+08:00", "20261026") is True
    assert dash._belongs_to(desk, "2026-10-27T09:30:00+08:00", "20261026") is True, \
        "today's session, whose own data has not landed yet"
    assert dash._belongs_to(desk, "2026-10-29T09:30:00+08:00", "20261026") is True, \
        "a weekend or holiday in between is fine"
    assert dash._belongs_to(desk, "2026-11-20T09:30:00+08:00", "20261026") is False, \
        "a month of drift means the pipeline is broken; do not pile it up here"
    assert dash._belongs_to(desk, "2026-10-20T09:30:00+08:00", "20261026") is False
