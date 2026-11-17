"""HTTP contract: the schema is the first line of defence against a creative model."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from service import settings
from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app
from service.trading.state import (
    OPEN_TICKET_STATUS,
    TERMINAL_TICKET_STATUSES,
    TICKET_STATUSES,
)
from tests.trading.helpers import pick_symbols, proposal

TOKEN = {"X-Quant-Token": "change-me-local-only"}


@pytest.fixture
def client(desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    # Telemetry must land in the throwaway DB, not the real account file.
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    with TestClient(app) as c:
        yield c


def test_requires_token(client):
    assert client.get("/portfolio").status_code == 401
    assert client.get("/portfolio", headers={"X-Quant-Token": "nope"}).status_code == 401


def test_price_stays_out_of_reach_while_quantity_is_bounded(client, desk):
    """The split is intentional: price is never the model's, quantity is.

    `qty` was refused outright until it became clear that "buy one lot" is a
    judgment, not an attempt to bypass the guard — a CNY 1,307 stock under a 20%
    target rounded to zero shares and could not be expressed at all. Price fields
    remain unknown to the schema, so they are still a 422.
    """
    symbol = pick_symbols(desk, 1)[0]

    body = proposal(symbol, target_weight=None)
    body["price"] = 9.5
    assert client.post("/decision/propose", json=body, headers=TOKEN).status_code == 422

    body = proposal(symbol, target_weight=None)
    body["qty"] = 100
    r = client.post("/decision/propose", json=body, headers=TOKEN)
    assert r.status_code == 200
    assert r.json()["qty"] == 100

    other = pick_symbols(desk, 2)[-1]
    body = proposal(other, target_weight=None)
    body["qty"] = 1_000_000
    r = client.post("/decision/propose", json=body, headers=TOKEN)
    assert r.status_code in (400, 409), "an unbounded quantity must never book"
    assert "最多" in r.text


def test_full_ticket_lifecycle_over_http(client, desk):
    symbol = pick_symbols(desk, 1)[0]
    r = client.post("/decision/propose", json=proposal(symbol), headers=TOKEN)
    assert r.status_code == 200
    ticket = r.json()
    assert ticket["status"] == "proposed"

    path = f"/decision/{ticket['ticket_id']}/approve"
    assert client.post(path, headers=TOKEN).status_code == 422
    assert client.post(path, json={"confirm": False}, headers=TOKEN).status_code == 422
    assert client.post(path, json={"confirm": True, "price": 1}, headers=TOKEN).status_code == 422

    r = client.post(path, json={"confirm": True}, headers=TOKEN)
    assert r.status_code == 200
    assert r.json()["status"] == "filled"

    r = client.get(f"/tickets/{ticket['ticket_id']}", headers=TOKEN)
    assert r.json()["status"] == "filled"

    r = client.get("/orders", headers=TOKEN)
    assert len(r.json()["orders"]) == 1
    assert r.json()["status"] == "ok"
    assert r.json()["count"] == 1


def test_empty_orders_are_an_anchored_empty_state(client):
    body = client.get("/orders", headers=TOKEN).json()

    assert body["orders"] == []
    assert body["status"] == "empty"
    assert body["count"] == 0
    assert body["as_of"]
    assert "不是读取失败" in body["reason"]


def test_a_listed_ticket_is_an_index_entry_not_the_whole_record(client, desk):
    """列表形态不是美观问题，是这个工具能不能用的问题。

    全额票据实测 2.8–3.8 KB，其中大半是 guard 的 `checks`（~1.4 KB）、
    `fillability`（0.5–0.9 KB）和 `snapshot`（~0.4 KB）。20 张就是 56 KB，
    越过工具输出 16 KB 上限后会被压到五行左右——而"我提了但没执行的票"
    恰恰需要一次看全。所以列表给简报，全量按 id 取。
    """
    symbol = pick_symbols(desk, 1)[0]
    ticket = client.post("/decision/propose", json=proposal(symbol), headers=TOKEN).json()

    brief = client.get("/tickets", params={"status": "open"}, headers=TOKEN).json()["tickets"]
    row = next(t for t in brief if t["ticket_id"] == ticket["ticket_id"])
    assert "checks" not in row and "snapshot" not in row and "fillability" not in row
    for key in ("ticket_id", "status", "symbol", "side", "thesis", "limit_price", "qty"):
        assert key in row, key

    full = client.get("/tickets", params={"status": "open", "view": "full"}, headers=TOKEN).json()[
        "tickets"
    ]
    assert "checks" in next(t for t in full if t["ticket_id"] == ticket["ticket_id"])
    assert len(json.dumps(full, ensure_ascii=False)) > len(json.dumps(brief, ensure_ascii=False))


def test_open_means_not_terminal_so_a_finished_ticket_leaves_the_list(client, desk):
    """`open` 是伪状态，不是存下来的值；终态一旦到达就必须从列表里消失。"""
    symbol = pick_symbols(desk, 1)[0]
    ticket = client.post("/decision/propose", json=proposal(symbol), headers=TOKEN).json()
    tid = ticket["ticket_id"]

    assert any(
        t["ticket_id"] == tid
        for t in client.get("/tickets", params={"status": "open"}, headers=TOKEN).json()["tickets"]
    )

    assert (
        client.post(
            f"/decision/{tid}/reject", json={"reason": "改主意了"}, headers=TOKEN
        ).status_code
        == 200
    )

    opened = client.get("/tickets", params={"status": "open"}, headers=TOKEN).json()["tickets"]
    assert not any(t["ticket_id"] == tid for t in opened)
    rejected = client.get("/tickets", params={"status": "rejected"}, headers=TOKEN).json()[
        "tickets"
    ]
    assert any(t["ticket_id"] == tid for t in rejected)


def test_a_misspelled_status_is_refused_rather_than_answered_with_nothing(client):
    """`status="propose"` 返回空列表，读起来就是"没有未结票据"。

    这个工具存在的理由就是让人看见漏掉的提案，所以拼错必须报错，而错误里要
    带着合法取值——否则调用方只能再猜一次。
    """
    r = client.get("/tickets", params={"status": "propose"}, headers=TOKEN)
    assert r.status_code == 422
    assert "proposed" in r.text


def test_an_empty_ticket_list_says_why_it_is_empty(client):
    """空数组不是一个答案。

    `as_of` 说这份清单是什么时候的，`note` 说为什么是空的——少任一句，读的人就
    分不清"真的没有"和"你没查对"。交付审计对这条有专门一格（F 全空且无说明），
    它是在工具层跑起来之后才抓到的：`quant_tickets(limit=15)` 当时返回 `{"tickets":[]}`。
    """
    body = client.get("/tickets", params={"status": "cancelled"}, headers=TOKEN).json()
    assert body["tickets"] == []
    assert body["count"] == 0
    assert body["as_of"], "空清单也要说自己是什么时候的"
    assert "cancelled" in body["note"]


def test_the_status_enum_in_the_contract_is_the_one_the_store_writes():
    """契约里列的取值必须与状态机一致，否则提示词会按一个不存在的状态去找。"""
    schema = app.openapi()["paths"]["/tickets"]["get"]
    status = next(p for p in schema["parameters"] if p["name"] == "status")
    # `Literal[...] | None` 在 OpenAPI 里是 anyOf: [枚举, null]，枚举不在一层。
    variants = status["schema"].get("anyOf") or [status["schema"]]
    listed = {value for variant in variants for value in variant.get("enum", [])}
    assert listed == set(TICKET_STATUSES) | {OPEN_TICKET_STATUS}
    assert TERMINAL_TICKET_STATUSES <= set(TICKET_STATUSES), "终态必须是真实状态"


def test_context_pack_stays_within_its_byte_budget(client):
    """一份开工契约要能被模型一次读完，不是一次数据倾倒。

    预算原为 4 KB。2026-11-09 午休实测 4134 字节：① 前一日新增的
    `risk.fill_mode`（8 个 prompt 都读它，删不得）；② `session.note` 在午休
    最长（要解释「没有可成交的实时价」）。两条都是该有的内容，所以抬到
    4.5 KB 留出约 10% 余量——**再破就别抬了，去删重复的散文**：
    `session.note` 与 `pipeline_alert` 的第一条目前是同一句话。
    """
    r = client.get("/context_pack", headers=TOKEN)
    assert r.status_code == 200
    raw = json.dumps(r.json(), ensure_ascii=False, separators=(",", ":")).encode()
    assert len(raw) <= 4608, len(raw)
    assert r.json()["account"]["equity"]


def test_journal_roundtrip(client):
    r = client.post(
        "/journal", json={"text": "今日不追高，等回踩", "tags": "discipline"}, headers=TOKEN
    )
    assert r.status_code == 200
    r = client.get("/journal", params={"q": "回踩"}, headers=TOKEN)
    assert r.json()["entries"]
    assert "回踩" in r.json()["entries"][0]["text"]


def test_memory_and_hypothesis_endpoints(client, desk):
    client.post(
        "/journal", json={"text": "回踩十日线再加仓，不追高", "tags": "plan"}, headers=TOKEN
    )
    r = client.get("/memory/search", params={"q": "回踩十日线"}, headers=TOKEN)
    assert r.status_code == 200
    assert r.json()["journal"]

    r = client.get("/cases", headers=TOKEN)
    assert r.status_code == 200
    assert r.json()["cases"] == []

    r = client.post(
        "/research/hypotheses",
        json={
            "statement": "首板次日开盘买入的日内期望为负",
            "rationale": "2026 年样本",
            "test_spec": "hold 1 day, mean open-to-close",
        },
        headers=TOKEN,
    )
    assert r.status_code == 200
    hyp_id = r.json()["id"]

    r = client.post(
        f"/research/hypotheses/{hyp_id}/result",
        json={
            "status": "testing",
            "metrics": {"mean": -0.0014},
        },
        headers=TOKEN,
    )
    assert r.json()["hypothesis"]["status"] == "testing"

    r = client.get("/research/hypotheses", headers=TOKEN)
    assert len(r.json()["hypotheses"]) == 1


def test_playbook_exposes_version_and_history(client):
    r = client.get("/playbook", headers=TOKEN)
    assert r.status_code == 200
    body = r.json()
    # 不钉具体版本号：v1/v2 是仓库当下的状态，不是契约。契约是「active 必须在
    # history 里，且 text 非空」——原先写死 ("v1", None)，一激活新版本就红，
    # 而这种红只在提醒人去改测试，不说明任何真实故障。
    versions = {v["version"] for v in body["history"]}
    assert body["version"] in versions
    assert body["text"].strip()
    assert body["draft_count"] == len(body["drafts"])


def test_duty_lists_waiting_work(client):
    body = client.get("/duty", headers=TOKEN).json()
    assert body["count"] == len(body["items"])
    kinds = {item["kind"] for item in body["items"]}
    assert "review_missing" in kinds, "a fresh book has no review for the session"
    assert all("priority" in item and "detail" in item for item in body["items"])


def test_usage_counts_what_was_called(client):
    client.get("/portfolio", headers=TOKEN)
    body = client.get("/usage", headers=TOKEN).json()
    assert any("/portfolio" in key for key in body["by_endpoint"])
    assert body["total"] >= 1, "/usage itself is excluded on purpose"


def test_strategies_explains_allocation(client):
    body = client.get("/strategies", headers=TOKEN).json()
    assert body["regime"] in {"risk_on", "neutral", "risk_off", "stressed"}
    plan = body["allocation"]
    assert abs(sum(plan["weights"].values()) + plan["cash_weight"] - 1.0) < 1e-6
    assert all(k in plan["reasons"] for k in plan["weights"])
    assert body["strategies"], "the registry must not be empty"


def test_attribution_lists_every_strategy_even_with_no_trades(client):
    body = client.get("/attribution", headers=TOKEN).json()
    keys = {row["strategy"] for row in body["strategies"]}
    assert "llm" in keys and "core_etf" in keys
    assert all("closed" in row and "win_rate" in row for row in body["strategies"])


def test_duty_reports_a_table_no_ddl_declares(client, desk):
    """A table left behind by a deleted feature must not become invisible.

    P14 created `advice`; P15 deleted the module, the DDL, the four store methods
    and the routes, but never ran `DROP TABLE`. The last row was written at 10:24
    on 2026-10-26, 33 minutes before the feature died — so the book kept a pending
    SET_BUDGET that no code could read, expire, or reject.
    """
    import sqlite3

    with sqlite3.connect(desk.paper.db_path) as conn:
        conn.execute("CREATE TABLE advice (advice_id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO advice VALUES ('adv-1', 'pending')")

    body = client.get("/duty", headers=TOKEN).json()
    hits = [i for i in body["items"] if i["kind"] == "orphan_table"]
    assert hits, body["items"]
    assert "advice" in hits[0]["detail"]
    assert body["count"] == len(body["items"])


# ------------------------------------------------------- 持仓提醒的问答接口
def test_the_handoff_inbox_is_readable_and_empty_is_not_an_error(client, monkeypatch, tmp_path):
    from service.trading import analyst
    from service.trading import handoff as handoff_mod

    monkeypatch.setattr(analyst, "HANDOFF", tmp_path)
    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)

    out = client.get("/monitor/handoff", headers=TOKEN).json()
    assert out["ok"] is True and out["pending"] == 0
    assert "没有待解读项时 pending=0" in out["note"]
    assert "POST /monitor/handoff/answer" in out["answer_with"]


def test_the_question_and_the_answer_round_trip_through_http(client, monkeypatch, tmp_path):
    from service.trading import analyst, monitor
    from service.trading import handoff as handoff_mod

    monkeypatch.setattr(analyst, "HANDOFF", tmp_path)
    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)
    monkeypatch.setattr(handoff_mod, "send_mail", lambda subject, text, html, **_: {"to": "me"})

    ctx = monitor.PositionContext(
        symbol="000700.SZ", qty=100, cost=13.12, entry=13.07, prev_close=13.07, last=12.30
    )
    alerts = monitor.evaluate([ctx], day="20261027")
    path = analyst.write_handoff(
        alerts, [ctx], analyst.build_prompt(alerts, [ctx]), source="monitor"
    )

    inbox = client.get("/monitor/handoff", headers=TOKEN).json()
    assert inbox["pending"] == 1
    item = inbox["items"][0]
    assert item["id"] == path.stem
    assert "触发项：" in item["prompt"], "接口给的是完整问题，不是摘要"

    bad = client.post(
        "/monitor/handoff/answer",
        headers=TOKEN,
        json={"id": "nope-000000", "analysis": "一二三四五六七八九十"},
    )
    assert bad.status_code == 404

    empty = client.post(
        "/monitor/handoff/answer", headers=TOKEN, json={"id": path.stem, "analysis": "   "}
    )
    assert empty.status_code == 422, "空解读不能变成一封空邮件"

    ok = client.post(
        "/monitor/handoff/answer",
        headers=TOKEN,
        json={"id": path.stem, "analysis": "第 1 项跌破 12.42，浮亏 -82 元，下一步确认成交额。"},
    )
    assert ok.status_code == 200 and ok.json()["sent"] is True
    assert client.get("/monitor/handoff", headers=TOKEN).json()["pending"] == 0


def test_journal_is_compact_by_default_and_can_be_expanded(client):
    """Eleven entries were 22 KB of prose; a session paid that to read one fact.

    The rule is recency, not length: the newest entries keep their text because
    a past decision can still bear on today; older ones keep a summary and their
    size so the model knows something long exists and can ask for it.
    """
    for i in range(5):
        client.post(
            "/journal", json={"text": f"第{i}条：这是一段很长的复盘文字" * 30}, headers=TOKEN
        )

    r = client.get("/journal", params={"limit": 5}, headers=TOKEN)
    entries = r.json()["entries"]
    assert len(entries) == 5
    assert all("text" in e for e in entries[:3]), "前三条应带全文"
    assert all("text" not in e for e in entries[3:]), "其余只给摘要"
    for e in entries[3:]:
        # 摘要不再是"开头 120 字 + 省略号"：复盘开头是战果、结尾是"下次改一条"，
        # 只给开头会把每一条行动项都切掉。省略号在中间，末尾必须留着。
        assert "…" in e["summary"], "截断就要明说"
        assert len(e["summary"]) < e["chars"]

    full = client.get("/journal", params={"limit": 5, "full": True}, headers=TOKEN)
    assert all("text" in e for e in full.json()["entries"])
    assert len(full.content) > len(r.content), "full=1 必须更大，否则参数名不实"


def test_journal_summary_keeps_the_last_paragraph_not_just_the_opening():
    """复盘是"战果在前、行动项在后"写的，摘要必须两头都留。

    这条钉的是 2026-11-03 之前的形态：``text[:120]`` 只留开头，于是
    "下次改一条：…" 和 "缺口：…" 这两句——一条复盘里唯一能改变下一轮行为的
    东西——从来没出现在任何摘要里。
    """
    from service.api.trading import _journal_entry

    opening = "19:30 收盘对账：12 条可结算 → 对 9 / 错 2 / 运气差 1。"
    middle = "差在哪：① 002343 被写成现价 7.86 = 涨停价而剔除。" * 12
    lesson = "下次改一条：09:26 的否决权只留 regime 与 excess_ci95。"
    row = {
        "id": 36,
        "ts": "2026-11-03T19:35:50+08:00",
        "kind": "review",
        "symbol": None,
        "tags": None,
        "snapshot": {},
        "text": opening + middle + lesson,
    }

    summary = _journal_entry(row, keep_text=False)["summary"]

    assert summary.startswith(opening[:20]), "开头要留：那是这一天的战果"
    assert summary.endswith(lesson), "结尾要留：那是下一轮唯一能改的东西"
    assert len(summary) < len(row["text"])


def test_a_short_entry_is_summarised_without_an_ellipsis():
    """300 字以内 head+tail 就是全文，加省略号只会让人以为还有下文。"""
    from service.api.trading import _journal_entry

    text = "13:30 巡检：无异常，持仓 0 只，挂单 0 笔。"
    row = {
        "id": 34,
        "ts": "2026-11-03T13:32:12+08:00",
        "kind": "note",
        "symbol": None,
        "tags": None,
        "snapshot": {},
        "text": text,
    }

    out = _journal_entry(row, keep_text=False)
    assert out["summary"] == text
    assert "text" not in out, "compact 模式不给 text 键——调用方靠这个判断拿没拿到全文"


def test_memory_search_compacts_journal_but_not_cases(client):
    for i in range(4):
        client.post("/journal", json={"text": f"银行仓位第{i}次复盘" * 10}, headers=TOKEN)
    r = client.get("/memory/search", params={"limit": 4}, headers=TOKEN)
    body = r.json()
    assert body["journal"]
    assert all("text" not in e for e in body["journal"][2:])


def test_a_handoff_blocked_by_a_claim_never_answers_200(client, monkeypatch, tmp_path):
    """200 with ok:true would read as "accepted" while nothing was sent."""
    from service.trading import handoff as handoff_mod

    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)
    body = {
        "id": "20261029-093012",
        "analysis": "第 1 项跌破止损，浮亏约 82 元，下一步确认成交额。",
    }

    handoff_mod.save(body["id"], {"status": "pending", "alerts": []})
    (tmp_path / f"{body['id']}.send-lock").write_text(
        '{"pid": 99, "host": "here", "phase": "sending", '
        '"created_at": "2099-01-01T00:00:00+08:00"}',
        encoding="utf-8",
    )
    live = client.post("/monitor/handoff/answer", headers=TOKEN, json=body)
    assert live.status_code == 409
    assert live.json()["detail"]["code"] == "SENDING"

    (tmp_path / f"{body['id']}.send-lock").write_text(
        '{"pid": 99, "host": "here", "phase": "sending", '
        '"created_at": "2020-01-01T00:00:00+08:00"}',
        encoding="utf-8",
    )
    stale = client.post("/monitor/handoff/answer", headers=TOKEN, json=body)
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "MAIL_UNKNOWN" and detail["may_have_sent"] is True
    assert "force" in detail["message"]


def test_pools_report_an_unformed_loader_instead_of_a_cold_tape(client, desk, monkeypatch):
    """`limit_up=0` at 09:41 is a loading state, and the payload has to say so.

    The 2026-10-30 run read 涨停 0 / 跌停 1 / tone 冰点 at 09:41 and wrote it up as
    market structure; the close was 49 / 86. Pinning the clock to 09:41 exercises
    the branch that now answers 未成形 and leaves the gate checks null.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    day = desk.market.latest_bar_date()
    real = trading_api._pool_phase

    def pinned(date, *frames, **kw):
        now = datetime(
            int(day[:4]), int(day[4:6]), int(day[6:]), 9, 41, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        return real(date, *frames, now=now)

    monkeypatch.setattr(trading_api, "_pool_phase", pinned)
    body = client.get("/market/pools", headers=TOKEN, params={"date": day}).json()
    assert body["pool_state"]["phase"] == "opening_window"
    assert body["pool_state"]["settled"] is False
    if not body.get("sentiment"):
        pytest.skip("no pool rows for the pinned bar; the empty branch has no panel")
    assert body["sentiment"]["tone"] == "未成形"
    assert body["sentiment"]["provisional"] is True
    assert body["gates"]["failed"] == []
    assert set(body["gates"]["indeterminate"]) == {"tone_ok", "break_rate_ok", "height_ok"}
    assert body["gates"]["why_indeterminate"]


def test_pools_accept_both_date_spellings(client, desk):
    """`date` and `as_of` are aliases; the tools split 14 to 4 and it cost a round trip."""
    day = desk.market.latest_bar_date()
    a = client.get("/market/pools", headers=TOKEN, params={"date": day}).json()
    b = client.get("/market/pools", headers=TOKEN, params={"as_of": day}).json()
    assert a["as_of"] == b["as_of"] == day
    assert a["counts"] == b["counts"]


def test_the_account_route_speaks_the_same_language_as_portfolio(client):
    """两个账户视图必须是同一个数，不是两份各算一遍的近似。

    ``/account`` 以前自己算 equity/drawdown 并把它们 ``str()`` 掉：那边是
    ``-0.00228731278603862530110``，``/portfolio`` 这边是 ``-0.0023``。同一个量、
    两种类型、两种精度——读的人无从判断哪个是值、哪个是格式残渣。现在
    ``/account`` 直接返回 ``book.summary()``。
    """
    account = client.get("/account", headers=TOKEN).json()
    portfolio = client.get("/portfolio", headers=TOKEN).json()

    for key in (
        "equity",
        "cash_available",
        "cash_frozen",
        "market_value",
        "realized_pnl",
        "peak_equity",
        "initial_cash",
        "position_count",
    ):
        assert account[key] == portfolio[key], key
    assert account["drawdown"] == portfolio["drawdown"]
    assert isinstance(account["drawdown"], float)
    assert account["position_count"] == len(account["positions"])
    # 订单与成交的快照留着：它回答的是另一个问题——这些余额由哪些单子产生。
    assert "snapshot" in account


def test_a_stale_account_write_answers_409_not_500():
    """账本被另一个进程改过是设计内的情形，不是服务器故障。

    账本是 sqlite + 乐观锁。服务、定时脚本和手工补录都会写它，所以"读到之后、
    写回之前有人改过"会发生。以前它一路冒泡成 500，调用方会去查自己的参数——
    而参数是对的，它需要的是"重读账本再试"。
    """
    from service.api.app import _execution_exists, _state_conflict
    from service.trading.state import ExecutionExists, StateConflict

    assert StateConflict in app.exception_handlers
    assert ExecutionExists in app.exception_handlers

    stale = _state_conflict(None, StateConflict("paper_state revision 3 is stale"))
    assert stale.status_code == 409
    assert b"STATE_CONFLICT" in stale.body

    dup = _execution_exists(None, ExecutionExists("ticket already committed"))
    assert dup.status_code == 409
    assert b"EXECUTION_EXISTS" in dup.body


def test_the_hypothesis_list_is_a_summary_unless_asked_otherwise(client):
    """五条假设 11 KB；默认 limit 曾设成 50，那是一次 100 KB 的回答。

    而整个会话简报才 4 KB。列表要回答的是"有哪些、怎么结的"；``test_spec``
    和完整 ``result`` 只在打算重跑某一条时才需要。
    """
    client.post(
        "/research/hypotheses",
        headers=TOKEN,
        json={
            "statement": "验证列表摘要：这条假设只为测试而建，不会有结论",
            "rationale": "r" * 600,
            "test_spec": "s" * 600,
        },
    )

    summary = client.get("/research/hypotheses", headers=TOKEN).json()
    full = client.get("/research/hypotheses?full=1", headers=TOKEN).json()

    assert summary["hypotheses"], "刚建的那条必须出现"
    row = summary["hypotheses"][0]
    assert set(row) == {"id", "status", "statement", "updated_at", "verdict"}
    assert "test_spec" in full["hypotheses"][0]
    assert len(json.dumps(summary, ensure_ascii=False)) * 3 < len(
        json.dumps(full, ensure_ascii=False)
    )


def test_the_screen_endpoint_cannot_be_asked_for_names_it_cannot_buy(client, desk):
    """Affordability is a ceiling, not a default the caller may raise.

    ``max_price`` used to be ``Query(default=45.0)`` — a second home for
    ``screen.max_price`` — and the affordability rule was spelled out as
    ``equity * 0.30 / 100``, duplicating ``affordable_price`` with a hardcoded
    weight. A caller could therefore ask for a ceiling the account cannot fund,
    and a config change would not have reached this endpoint at all.
    """
    from service.trading.screen import affordable_price

    ceiling = affordable_price(float(desk.book.equity()))
    body = client.get("/research/screen", params={"max_price": 999}, headers=TOKEN).json()

    assert body["max_price"] == pytest.approx(round(ceiling, 2))


def test_the_screen_endpoint_lets_a_caller_tighten_the_ceiling(client):
    body = client.get("/research/screen", params={"max_price": 3.5}, headers=TOKEN).json()

    assert body["max_price"] == pytest.approx(3.5)


def test_the_screen_endpoint_starts_from_the_configured_thresholds(client, desk):
    """With no parameters it must use ``screen.*``, not a literal of its own."""
    from service.trading.screen import ScreenConfig, affordable_price

    expected = min(ScreenConfig().max_price, affordable_price(float(desk.book.equity())))
    body = client.get("/research/screen", headers=TOKEN).json()

    assert body["max_price"] == pytest.approx(round(expected, 2))


def test_the_screen_limit_bounds_the_candidate_list(client):
    body = client.get("/research/screen", params={"limit": 2}, headers=TOKEN).json()

    assert len(body["candidates"]) <= 2
    assert body["max_price"] > 0


def test_a_retired_core_sleeve_refuses_work_with_a_code(client):
    """409, not 200-with-no-orders.

    An empty order list reads as "the rule had nothing to do today" and gets
    retried tomorrow. Six sessions of that is how a sleeve whose defensive leg
    costs 2.8x the account went unnoticed. The refusal has to name itself.
    """
    for path in ("/core/plan", "/core/execute"):
        body = (
            None
            if path.endswith("plan")
            else {
                "targets": {"510300.SH": 1.0},
                "thesis": "测试用：把全部资金配置到沪深300，验证退休的袖拒绝下单",
                "evidence": ["daily"],
                "invalidation": "若该端点返回 200，则本测试失效",
            }
        )
        response = (
            client.post(path, headers=TOKEN)
            if body is None
            else client.post(path, json=body, headers=TOKEN)
        )
        assert response.status_code == 409, f"{path} 应拒绝退休的袖"
        assert response.json()["detail"]["code"] == "CORE_RETIRED"

    status = client.get("/core", headers=TOKEN).json()
    assert status["state"] == "retired"
    # Still readable on purpose: the reason must be checkable, not remembered.
    assert status["executable"]
    assert status["target_weights"]


def test_a_retired_sleeve_is_not_a_third_column_in_the_arena(client):
    payload = client.get("/arena", headers=TOKEN).json()

    assert payload["core_retired"] is True
    assert "core" not in payload["sleeves"], "退休的袖不该与真正交易过的袖并排比较"
    assert {"llm", "market"} <= set(payload["sleeves"])
    assert payload["core_note"]


def test_a_journal_entry_reports_which_numbers_the_session_cannot_reproduce(
    client, tmp_path, monkeypatch
):
    """复盘里编的数会变成记忆——写的时候就要说哪些数对不上。

    不拦：拒绝写入会教模型把数字删掉，而复盘的价値一半在数字上。
    """
    import json as _json

    # 指向一个临时事实目录：测试不该往仓库的 reports/facts 里写文件。
    monkeypatch.setattr("service.api.trading.ROOT", tmp_path)
    # 当日价格集合会并入真实行情（9,779 个价），912 可能正好是某只票的收盘价。
    # 这条测的是"编造的家数被报出来"，所以把价格那一半关掉，只留 facts。
    monkeypatch.setattr("service.trading.journal_check.market_numbers", lambda desk, day: set())
    day = client.get("/facts", headers=TOKEN).json()["day"]
    (tmp_path / "reports" / "facts").mkdir(parents=True)
    (tmp_path / "reports" / "facts" / f"{day}.json").write_text(
        _json.dumps(
            {
                "day": day,
                "breadth": {"advancers": 893},
                "pools": {"limit_up": 49},
            }
        ),
        encoding="utf-8",
    )

    r = client.post("/journal", json={"text": "今天 912 家上涨，涨停 49 家"}, headers=TOKEN)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    verify = body["verification"]
    assert set(verify) >= {"checked", "matched", "unmatched"}
    # 912 是编的（当日 893），49 是真的；两边都要出现在结果里才说明它真比过
    assert "912" in verify["unmatched"]
    assert verify["matched"] >= 1


def test_a_journal_entry_without_numbers_says_so_rather_than_passing_silently(client):
    r = client.post("/journal", json={"text": "今日不追高，等回踩"}, headers=TOKEN)

    body = r.json()["verification"]
    assert body["checked"] == 0
    assert "没有数字" in body["note"]


def test_the_pool_payload_says_how_old_the_snapshot_is(client):
    """2026-11-02：10:30 抓的一次快照，13:18 还被引用算封单/成交比，中间 2.8 小时。

    响应里有两个时间戳，但没有人去减。现在 `stale` / `age_seconds` /
    `derived_from_stale` 是顶层字段——第一屏就能看见。
    """
    body = client.get("/market/pools", headers=TOKEN).json()

    for key in ("stale", "age_seconds", "derived_from_stale", "refresh"):
        assert key in body, key
    assert isinstance(body["stale"], bool)
    # 2026-11-07 口径修正：这里原本断言整个字典等于 {"attempted": False}。
    # 但 trading.py:1121-1131 明确定义了三种 refresh.skipped 分支——历史日
    # 已是终值、快照仍新鲜、当天允许刷新——且每种都保持 attempted=False。
    # 精确字典断言把「没抓网络」和「没有其它说明字段」绑成了一个条件，
    # 于是周末 / 查历史日必然误报，而产品行为是对的。只断言不变量：
    # 没人要求刷新就不许抓网络；skipped 若出现必须是文字说明。
    refresh = body["refresh"]
    assert refresh["attempted"] is False, "没人要求刷新时不许抓网络"
    assert "skipped" not in refresh or isinstance(refresh["skipped"], str)


def test_asking_for_a_refresh_on_a_past_day_is_a_no_op(client):
    """只有"今天"才值得刷新；历史某天的池子已经是终值，重抓是白花一次请求。"""
    day = "20200102"
    body = client.get(
        "/market/pools", headers=TOKEN, params={"date": day, "force_refresh": "true"}
    ).json()

    # 不是"没抓"就算，要说清为什么不抓。
    assert body["refresh"]["attempted"] is False
    assert "终值" in body["refresh"]["skipped"]


def test_force_refresh_will_not_fire_on_a_fresh_snapshot(client, monkeypatch):
    """一次刷新是 5,500 只票的请求。快照还新鲜时，这个开关必须是空转。

    2026-11-02 当天东财熔断 5 次，就是为了抓这种全市场快照。

    抓取本身在这里被换成一个标记：这条测试要证明的是**闸门**的判断，不是采集器
    的行为（那是 test_collector_safety 的事）。让它真的抓一次会有两个后果——全
    市场请求，以及**写进生产的 parquet 目录而清单落进测试用的注册表副本**，
    随后清理就会删掉生产清单正在指向的那个版本。2026-11-03 这个副作用把
    `pool_tape` 的分区弄坏过两次。
    """
    calls: list[str] = []

    def fake_refresh(_desk):
        calls.append("refresh")
        return {"attempted": True, "ok": True, "stubbed": True}

    monkeypatch.setattr(trading_api, "_refresh_pools", fake_refresh)

    body = client.get("/market/pools", headers=TOKEN, params={"force_refresh": "true"}).json()

    refresh = body["refresh"]
    if refresh.get("attempted") is True:
        # 闸门开了：本地池子确实过期，此刻就该抓——抓取已由上面的桩替代。
        assert calls == ["refresh"]
        return
    # 闸门关着：必须说清为什么没动，而不是静默返回。
    assert calls == []
    assert "skipped" in refresh, refresh


def test_a_quote_says_whose_clock_its_timestamp_is(client):
    """`quote_ts` 是取数时刻，不是报价自己的时间——盘后这一点最要命。

    2026-11-02 20:54 实测：收盘价被标成 `quote_ts=20:54:03`、`age_seconds=4.2`，
    6 小时前的价格看起来像 4 秒前成交的。三个源都不提供逐条行情时间（同花顺快照
    报文只有 data.timestamp），所以响应必须自己说清 basis，盘后 age 必须是 null。
    """
    response = client.get("/market/quote?symbols=600000.SH&live=false", headers=TOKEN)
    assert response.status_code == 200, response.text
    body = response.json()

    assert "取数" in body["timestamp_note"], "响应上必须说清 quote_ts 是什么时刻"
    if not body["quotes"]:
        # 开盘前（或非交易日）没有当天报价，端点返回空表——这是对的，不是缺陷。
        # 这条测的是"有报价时怎么标注"，没有样本时跳过，别让它每天凌晨红一次。
        pytest.skip("当前没有可用报价（开盘前/非交易日），测不到标注分支")
    quote = body["quotes"][0]
    # live=false 走的是已存日线：它的 quote_ts 是交易日，不是取数时刻——
    # 两种 basis 必须在每条上分开，否则"日线日期"会被当成"4 秒前的报价"。
    assert quote["timestamp_basis"] == "daily_bar"
    assert quote["age_seconds"] is None
    assert quote["is_realtime"] is False


def test_a_vendor_quote_declares_that_its_timestamp_is_a_fetch_time():
    """厂商报价那条路径：basis=fetched，且盘后 age 必须是 null。

    这一条不走网络，直接构造 Quote —— 盘后把 6 小时前的收盘价写成"4 秒前"
    这个问题，正是从 Quote.as_dict() 这个出口漏出去的。
    """
    from decimal import Decimal as D

    from service.trading.realtime import Quote

    quote = Quote(
        symbol="600000.SH",
        name="浦发银行",
        last=D("10.06"),
        pre_close=D("10.02"),
        open=D("9.98"),
        high=D("10.22"),
        low=D("9.93"),
        volume=1.0,
        amount=1.0,
        up_limit=D("11.02"),
        down_limit=D("9.02"),
        quote_ts="2026-11-02T20:54:03+08:00",
        age_seconds=None,
        source="ths",
        is_realtime=False,
        realtime_reason="outside_session",
    )
    payload = quote.as_dict()
    assert payload["timestamp_basis"] == "vendor_response_time"
    assert payload["age_seconds"] is None
    for source in ("eastmoney", "tencent"):
        vendor = Quote(**{**quote.__dict__, "source": source, "timestamp_basis": None})
        assert vendor.as_dict()["timestamp_basis"] == "vendor_quote_time"


def test_the_limit_up_rows_say_what_one_lot_costs(client, desk):
    """短线最先要回答的不是"这票强不强"，是"我这点钱参不参与得了"。

    A 股整手 100 股。5,007 元的账户上，21.93 元的青岛食品一手 2,193 元＝43.8%，
    一手就已经过了 30% 的单票上限——这个数不摆在票旁边，读者要先乘 100 再除净值，
    而"外行"不会做这两步。涨停池给，跌停池不给：那一边没有买入决策。
    """
    day = desk.market.latest_bar_date()
    body = client.get("/market/pools", headers=TOKEN, params={"date": day, "limit": 5}).json()
    if not body.get("pools", {}).get("limit_up"):
        pytest.skip("no limit-up rows for this bar")

    equity = float(body.get("equity") or desk.book.summary()["equity"])
    cap = settings.num("risk.max_core_single")
    for row in body["pools"]["limit_up"]:
        assert row["lot_cost_cny"] == round(row["close"] * 100, 2)
        assert row["pct_of_equity"] == round(row["lot_cost_cny"] / equity * 100, 1)
        # 一手顶破单票上限的票要自称买不进，别让模型去提案再收 "最多 0 股"。
        assert bool(row["exceeds_core_cap"]) == bool((row["lot_cost_cny"] / equity) > cap), row.get(
            "code"
        )

    for row in body["pools"].get("limit_down") or []:
        assert "lot_cost_cny" not in row, "买入决策才需要一手金额，别给三个池子都加一遍"


def test_account_reads_balances_and_orders_under_one_lock(desk, monkeypatch):
    """`/account` 的余额与订单必须来自同一时刻。

    `summary()` 一直在锁内，而 `ledger.snapshot()` 在锁外读：中间落一笔成交，
    返回的就是"持仓已计、现金未扣"的两半，两半各自都像真的。
    """
    from service.api import app as app_module

    held: list[bool] = []
    real_snapshot = desk.book.ledger.snapshot

    def probe():
        held.append(desk.book.lock._is_owned())
        return real_snapshot()

    monkeypatch.setattr(desk.book.ledger, "snapshot", probe)
    monkeypatch.setattr(app_module.trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module.trading_api, "refreshed_book", lambda d: d)

    body = app_module.account()

    assert held == [True], "snapshot 必须在 book.lock 内读"
    assert "snapshot" in body and "cash_available" in body


# ── A what-if price must announce itself ────────────────────────────────────
# `/execution/estimate` accepts a caller price for scenario work ("what would
# this order cost at 12.30"), and the reply carried it as a bare `price` field.
# A model reading that back has no way to tell a quote from a hypothesis, and
# the price rule is that only quant_* quotes are facts. `price_source` is the
# difference between the two in one word.

ESTIMATE = "/execution/estimate"


def test_estimate_without_a_price_says_it_used_the_last_close(client, desk):
    symbol = pick_symbols(desk, 1)[0]
    body = client.get(ESTIMATE, params={"symbol": symbol, "qty": 100}, headers=TOKEN).json()
    assert body["price_source"] == "server_last"
    assert "收盘价" in body["price_note"]


def test_estimate_with_a_caller_price_says_it_is_a_scenario(client, desk):
    symbol = pick_symbols(desk, 1)[0]
    body = client.get(
        ESTIMATE, params={"symbol": symbol, "qty": 100, "price": 12.34}, headers=TOKEN
    ).json()
    assert body["price_source"] == "caller"
    assert "假设" in body["price_note"]
    assert "quant_quote" in body["price_note"]
    assert body["price"] == pytest.approx(12.34, abs=0.001)


def test_estimate_separates_liquidity_schedule_from_cash_fillability(client, desk, monkeypatch):
    monkeypatch.setattr(desk.book, "summary", lambda: {"cash_free": "1.00"})
    symbol = pick_symbols(desk, 1)[0]
    body = client.get(ESTIMATE, params={"symbol": symbol, "qty": 100}, headers=TOKEN).json()
    assert body["fillability"]["status"] == "not_executable_as_requested"
    assert body["fillability"]["cash_notional_affordable"] is False
    assert body["fillability"]["schedule_is_fill_proof"] is False
    assert body["fillability"]["final_authority"] == "proposal_ticket_guard"
    assert body["cost_scope"] == "single_side_modelled_impact_only_excludes_fees_taxes_slippage"
    assert body["round_trip_cost"]["combined_estimate_bps"] >= body["impact_bps"]


def test_estimate_marks_a_limit_price_as_queue_not_a_fill(client, desk, monkeypatch):
    monkeypatch.setattr(desk.book, "summary", lambda: {"cash_free": "1000000.00"})
    symbol = pick_symbols(desk, 1)[0]
    day = desk.market.latest_bar_date()
    snap = desk.market.snapshot(symbol, day)
    body = client.get(ESTIMATE, params={
        "symbol": symbol, "qty": 100, "price": float(snap.up_limit), "side": "BUY"
    }, headers=TOKEN).json()
    assert body["fillability"]["queue_required_at_reference_price"] is True
    assert body["fillability"]["queue_position_known"] is False
    assert body["fillability"]["fill_probability"] is None
    assert body["fillability"]["status"] == "queue_required_fill_unknown"
    assert body["execution_scope"]["authorization"] == "read_only_estimate_no_order_created"


def test_estimate_sell_does_not_apply_the_buy_cash_check(client, desk, monkeypatch):
    monkeypatch.setattr(desk.book, "summary", lambda: {"cash_free": "0.00"})
    symbol = pick_symbols(desk, 1)[0]
    day = desk.market.latest_bar_date()
    snap = desk.market.snapshot(symbol, day)
    body = client.get(ESTIMATE, params={
        "symbol": symbol, "qty": 100, "price": float(snap.down_limit), "side": "SELL"
    }, headers=TOKEN).json()
    assert body["fillability"]["cash_notional_affordable"] is None
    assert body["fillability"]["position_and_t1_guard_required"] is True
    assert body["fillability"]["status"] == "queue_required_fill_unknown"


def test_the_frame_endpoint_carries_the_watchers_log(client, monkeypatch, tmp_path):
    """模型问"有没有事"时，答案横跨两个进程。

    5 分钟一轮的监控进程把命中写进 ``reports/external``，模型只在 ``/market/frame``
    看盘。这两段时钟之间原本没有任何人在传话：规则响了、邮件发了、模型不知道。
    """
    from service.notify import alert_log

    # scope 决定默认目录，所以桩要给出"每个 scope 一个目录"——两份日志放在同一个
    # 目录里，paper 那份就会读出实盘的告警。
    monkeypatch.setattr(alert_log, "default_base", lambda scope="external": tmp_path / scope)
    day = "20261105"
    external = tmp_path / "external"

    empty = client.get("/market/frame", params={"day": day}, headers=TOKEN).json()
    assert empty["alerts"]["available"] is False
    assert "没人在看" in empty["alerts"]["note"]

    alert_log.beat(day, base=external, scope="external")
    alert_log.append(
        day,
        [
            {
                "ts": "2026-11-05T10:12:00+08:00",
                "rule": "stop_proximity",
                "severity": "warn",
                "symbol": "600000.SH",
                "title": "600000 距止损 1.2%",
                "facts": ["止损 9.20", "现价 9.31"],
            }
        ],
        base=external,
    scope="external")

    body = client.get("/market/frame", params={"day": day}, headers=TOKEN).json()
    assert body["alerts"]["available"] is True
    assert body["alerts"]["count"] == 1
    assert body["alerts"]["items"][0]["symbol"] == "600000.SH"
    # 归属必须写明：这份日志看的是实盘，不是本模拟台。两个账户的票可以完全不同。
    assert body["alerts"]["scope"] == "external"
    assert "实盘" in body["alerts"]["scope_note"]


def test_the_frame_endpoint_also_carries_the_paper_books_watch(client, monkeypatch, tmp_path):
    """实盘那份回答"别人有没有事"，book_watch 回答"我的持仓有没有事"。

    两个账户的票可以完全不同（2026-11-05：实盘 002185、模拟台空仓）。混成一份
    日志，模型会把别人的破线读成自己的止损。
    """
    from datetime import datetime

    from service.kernel.clock import SHANGHAI
    from service.notify import alert_log

    monkeypatch.setattr(alert_log, "default_base", lambda scope="external": tmp_path / scope)
    day = "20261105"
    alert_log.reconcile(
        day,
        [{
            "code": "stop_breach", "severity": "critical", "subject": "600000.SH",
            "detail": "600000 现价 9.10 已跌破止损 9.20",
            "numbers": {"last": "9.10", "stop": "9.20"},
        }],
        at=datetime(2026, 11, 5, 10, 30, tzinfo=SHANGHAI),
        base=tmp_path / "paper", scope="paper",
    )
    alert_log.beat(day, now=datetime(2026, 11, 5, 10, 30, tzinfo=SHANGHAI),
                   base=tmp_path / "paper", scope="paper")

    body = client.get("/market/frame", params={"day": day}, headers=TOKEN).json()

    assert body["book_watch"]["scope"] == "paper"
    assert body["book_watch"]["available"] is True
    assert [item["code"] for item in body["book_watch"]["open"]] == ["stop_breach"]
    assert "paper" in body["book_watch"]["scope_note"]
    # 实盘那份不能跟着出现这条：它今天没跑过，空就是空。
    assert body["alerts"]["count"] == 0
    assert body["alerts"]["open"] == []


def test_the_frame_endpoint_resolves_the_day_before_it_reads_a_log(client, monkeypatch,
                                                                   tmp_path):
    """不带 `?day=` 时，日志文件名必须是今天，不是 `None_alerts.jsonl`。

    2026-11-05 实测：`/market/frame` 不带 day 时 `basis` 指向
    `reports/paper/None_alerts.jsonl`，于是 `book_watch` 永远报"巡检没跑过"——
    把"没人看"错报成默认状态，比不报更坏。
    """
    from service.notify import alert_log

    monkeypatch.setattr(alert_log, "default_base", lambda scope="external": tmp_path / scope)

    body = client.get("/market/frame", headers=TOKEN).json()

    for key, scope in (("alerts", "external"), ("book_watch", "paper")):
        assert "None" not in body[key]["basis"]
        assert body[key]["basis"].endswith("_alerts.jsonl")
        assert scope in body[key]["basis"]


def test_the_frame_endpoint_passes_the_lookback_through(client, monkeypatch):
    """调用方要 30 分钟窗口，就得真的拿到 30 分钟的对比。"""
    seen = {}

    def fake_build(_desk, **kwargs):
        seen.update(kwargs)
        return {"ok": False, "error": "no_frames", "day": kwargs.get("day")}

    monkeypatch.setattr("service.trading.frame.build", fake_build)
    client.get("/market/frame?lookback=30", headers=TOKEN)

    assert seen["lookback"] == 30


def test_market_status_publishes_the_quote_age_the_fill_gate_enforces(client):
    """09:26 的前置检查读这个数，不再自己写 300 秒。

    实测过的一次不一致：提示词放行 90–300 秒的报价，`approve` 却按
    `live_fill.max_quote_age` 在 90 秒拒掉，白跑一次提案。
    """
    body = client.get("/market/status", headers=TOKEN).json()
    assert body["fill"]["max_quote_age_seconds"] == float(
        settings.num("live_fill.max_quote_age"))


def test_playbook_says_which_drafts_are_waiting_and_that_they_are_not_live(client):
    """草案必须出现在 `quant_playbook` 的返回里，并显式标记未生效。

    2026-11-04 到 11-06 每天生成一版草案，`/playbook` 只返回生效版本，于是
    每个窗口看到的都是「v1」，没有任何一处输出说明「有三条规则在等人批」。
    连续 4 天未激活不是没人管，是看不见。
    """
    body = client.get("/playbook", headers=TOKEN).json()
    assert "draft_count" in body and "drafts" in body
    assert body["draft_count"] == len(body["drafts"])
    for draft in body["drafts"]:
        assert draft["in_effect"] is False
        assert draft["draft_id"] and draft["note"]
        assert draft["draft_id"] not in [v["version"] for v in body["history"]]


def test_root_page_points_at_the_real_frontend(client):
    """根路径是遗留调试页，必须自己把人指到 /desk/，否则会被当成前端坏了。

    2026-11-10 排查：/ 返回 200 但只有 token 输入框，主前端挂在 /desk/；
    不带令牌直接开 /desk/ 又会 401 显示「口令失效了」。两件事叠加最像故障，
    实际都是使用路径问题。把这层说明钉在页面上，并锁进测试。
    """
    res = client.get("/")
    assert res.status_code == 200
    html = res.text
    assert '/desk/' in html
    assert 'QUANT_TOKEN' in html
    assert 'panel.bat' in html
    assert 'is_realtime=false' in html
