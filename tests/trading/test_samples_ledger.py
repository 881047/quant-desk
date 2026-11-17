"""样本账本：20 笔平仓这个 KPI 的唯一路径，之前一个测试都没有。

这个账本是独立的 2,000 元账户、独立的 sqlite、独立的风控（1 只持仓、满仓、
每天一买一卖）。主账户的 ``arena``、``portfolio``、净值都不读它——所以它坏了，
主账户的测试全绿也看不出来，而"凑够 20 笔平仓"正是这个台子唯一在意的 KPI。

这个文件覆盖四件事：

1. **闭环**：propose → approve → 成交 → 卖出 → 结算一笔（closed_count +1）。
2. **拒绝**：reject 写理由，提案不进队列。
3. **隔离**：样本账本的成交不进入主账户的 cases。
4. **它自己的纪律**：1 只持仓、每天一买一卖，以及"只买不卖不算一笔"。

它跑在临时目录里，**不碰** ``data/samples.sqlite``——那本账是真实 KPI 的计数。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from service.api import app as app_module
from service.api import trading as trading_api
from service.api.app import app
from service.trading import build_samples_desk
from tests.trading.helpers import pick_symbols, pin_to_last_bar

TOKEN = {"X-Quant-Token": "change-me-local-only"}


@pytest.fixture
def samples_desk(tmp_path, monkeypatch, shared_market):
    """A sample ledger of its own: 2,000 元, temp file, real market data."""
    monkeypatch.setenv("FILL_MODE", "close")
    monkeypatch.setenv("REALTIME_QUOTES", "0")
    desk = build_samples_desk(root=tmp_path, market=shared_market)
    pin_to_last_bar(desk)
    return desk


@pytest.fixture
def client(samples_desk, monkeypatch):
    monkeypatch.setattr(trading_api, "get_samples_desk", lambda: samples_desk)
    # Telemetry must land in the throwaway DB, not the real account file.
    monkeypatch.setattr(app_module, "_usage", samples_desk.paper)
    with TestClient(app) as c:
        yield c


def _proposal(symbol: str, side: str = "BUY") -> dict:
    """A proposal the sample ledger accepts.

    ``qty=100`` 而不是 ``target_weight``：样本策略有 60% 的仓位上限
    （2,000 元买一手就占七成，所以传 llm 会被挡），而按权重算出来的股数要
    凑整手，很容易一落地就超过上限。100 股在 9.5 元以下的票上是 950 元，
    占 2,000 元的 47.5%——稳稳在上限之内。
    """
    return {
        "symbol": symbol,
        "side": side,
        "sleeve": "CORE",
        "intent_type": "sample",
        "thesis": "样本轮换：一天一买一卖，用最小账户检验一条可证伪的结构。",
        "evidence": ["daily:latest", "quality:ok"],
        "invalidation": "次日开盘前若不再满足则放弃",
        "qty": 100,
    }


def _symbol(desk, n: int = 1) -> str:
    symbols = pick_symbols(desk, n)
    assert symbols, "本地数据里没有可用标的"
    return symbols[0]


def _next_day(day: str) -> str:
    """The calendar day after a YYYYMMDD string; used only to unlock T+1."""
    from datetime import datetime, timedelta

    return (datetime.strptime(day, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")


# ── 闭环 ────────────────────────────────────────────────────────────────────
def test_the_ledger_reports_its_own_account_and_its_target(client):
    body = client.get("/samples", headers=TOKEN).json()

    assert body["target"] == 20, "KPI 是 20 笔平仓"
    assert body["closed_count"] == 0
    assert body["remaining"] == 20
    assert body["account"]["cash_available"], "样本账本是独立账户"
    assert body["note"], "账本要自己说清它与主账户无关"


def test_a_proposal_becomes_a_ticket(client, samples_desk):
    symbol = _symbol(samples_desk)
    response = client.post("/samples/propose", json=_proposal(symbol), headers=TOKEN)

    assert response.status_code == 200, response.text
    ticket = response.json()
    assert ticket["symbol"] == symbol
    assert ticket["status"] in {"proposed", "queued", "filled"}
    assert ticket["qty"] == 100


def test_the_whole_cycle_settles_one_trade(client, samples_desk):
    """买 → 卖 → closed_count 变成 1。

    这是这个台子唯一在意的数字，所以它必须能被测出来。
    """
    symbol = _symbol(samples_desk)

    buy = client.post("/samples/propose", json=_proposal(symbol), headers=TOKEN)
    assert buy.status_code == 200, buy.text
    approve = client.post(f"/samples/{buy.json()['ticket_id']}/approve",
                          json={"confirm": True}, headers=TOKEN)
    assert approve.status_code == 200, approve.text
    assert approve.json()["status"] == "filled"

    # T+1：当日买入的股票今天不可卖。解锁之前卖要拿一个说得清的拒绝。
    locked = client.post("/samples/propose", json=_proposal(symbol, "SELL"),
                         headers=TOKEN)
    assert locked.status_code == 400, locked.text
    assert "T+1" in locked.text, "拒绝要说得清是 T+1 还是别的"

    # 次日：解锁、卖出、结算。
    #
    # "次日"由最后有行情的那一天推出来，不能写死：写死 20261031 之后，只要
    # 数据往前长一天，那个日期就变成了"昨天"，rollover 不再解锁 T+1，测试会
    # 以一个和它想问的问题无关的理由红掉。
    samples_desk.book.rollover(_next_day(samples_desk.market.latest_bar_date()))
    sell = client.post("/samples/propose", json=_proposal(symbol, "SELL"),
                       headers=TOKEN)
    assert sell.status_code == 200, sell.text
    assert client.post(f"/samples/{sell.json()['ticket_id']}/approve",
                       json={"confirm": True}, headers=TOKEN).status_code == 200

    body = client.get("/samples", headers=TOKEN).json()
    assert body["closed_count"] >= 1, "一买一卖之后必须结算一笔"
    assert body["remaining"] <= 19
    assert body["recent_cases"], "结算记录要能被读回来"


# ── 拒绝 ────────────────────────────────────────────────────────────────────
def test_a_rejection_records_the_reason_and_queues_nothing(client, samples_desk):
    symbol = _symbol(samples_desk)
    buy = client.post("/samples/propose", json=_proposal(symbol), headers=TOKEN)
    ticket_id = buy.json()["ticket_id"]

    reject = client.post(f"/samples/{ticket_id}/reject",
                          json={"reason": "样本账本测试：这条结构已被证伪。"},
                          headers=TOKEN)
    assert reject.status_code == 200, reject.text
    assert reject.json()["status"] == "rejected"

    body = client.get("/samples", headers=TOKEN).json()
    assert body["closed_count"] == 0, "被拒绝的提案不算一笔"
    assert not [t for t in body.get("pending", [])
                if t.get("ticket_id") == ticket_id]


def test_approving_an_unknown_ticket_is_a_clean_error(client):
    response = client.post("/samples/no-such-ticket/approve", json={"confirm": True}, headers=TOKEN)
    assert response.status_code in (404, 409)
    assert response.json()["detail"], "拒绝要说得清为什么"


# ── 隔离 ────────────────────────────────────────────────────────────────────
def test_the_sample_ledger_does_not_touch_the_main_account(client, samples_desk,
                                                           desk):
    """样本账本的成交不进主账户的 cases。

    两个账本混在一起之后，主账户的净值和 arena 就都读不懂了——所以这条是
    隔离，不是"顺手"。
    """
    symbol = _symbol(samples_desk)
    before = len(desk.paper.list_cases(limit=1000))

    buy = client.post("/samples/propose", json=_proposal(symbol), headers=TOKEN)
    client.post(f"/samples/{buy.json()['ticket_id']}/approve", json={"confirm": True}, headers=TOKEN)

    assert len(desk.paper.list_cases(limit=1000)) == before, \
        "主账户的账本不能出现样本账本的成交"
    assert samples_desk.paper is not desk.paper, "两个账本必须是两个文件"


# ── 它自己的纪律 ────────────────────────────────────────────────────────────
def test_the_ledger_holds_one_position_at_a_time(client, samples_desk):
    """1 只持仓——这是采样装置的机械约束，不是风格。

    注意 guard 的失败**不是**一个 HTTP 错误码：``propose`` 会写一张
    ``status="rejected"`` 的票据并带上 ``reasons``，HTTP 仍然是 200。
    这正是技能文件里那句"被拒绝不要换参数重试"说的那种拒绝。
    """
    symbols = pick_symbols(samples_desk, 2)
    assert len(symbols) == 2

    first = client.post("/samples/propose", json=_proposal(symbols[0]), headers=TOKEN)
    assert first.status_code == 200, first.text
    client.post(f"/samples/{first.json()['ticket_id']}/approve",
                json={"confirm": True}, headers=TOKEN)

    second = client.post("/samples/propose", json=_proposal(symbols[1]), headers=TOKEN)
    assert second.status_code == 200, second.text
    ticket = second.json()
    assert ticket["status"] == "rejected", "第二只票在第一只还在时必须被拒"
    assert any("max_positions" in reason for reason in ticket.get("reasons", [])), (
        f"拒绝要说得清是哪一条：{ticket.get('reasons')}")


def test_only_a_closed_round_trip_counts_as_a_settled_trade(client, samples_desk):
    """只买不卖不算一笔——挂着不算，结算才算。"""
    body = client.get("/samples", headers=TOKEN).json()
    assert body["closed_count"] == 0

    symbol = _symbol(samples_desk)
    buy = client.post("/samples/propose", json=_proposal(symbol), headers=TOKEN)
    client.post(f"/samples/{buy.json()['ticket_id']}/approve", json={"confirm": True}, headers=TOKEN)

    body = client.get("/samples", headers=TOKEN).json()
    assert body["closed_count"] == 0, "只买不卖不算一笔"
