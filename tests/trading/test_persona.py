"""Persona contract: switching is explicit, priced, scored — and never automatic.

The interesting assertions here are not "the happy path works". They are:

* a switch without a number cannot be recorded (it could never be scored);
* a style is refused when the data it needs is missing, instead of producing a
  ticket at a price that only exists in hindsight;
* the chooser cannot see PnL while choosing, because a selector that can read
  returns will eventually be tuned on them.
"""

from __future__ import annotations

import inspect

import pytest

from service.trading import persona as persona_mod
from service.trading.decisions import DecisionError

PROBE_READY = {"daily": {"ok": True}, "pools": {"ok": True},
               "sentiment": {"ok": True}, "rt_quote": {"ok": True}}
PROBE_NO_QUOTE = {"daily": {"ok": True}, "pools": {"ok": True},
                  "sentiment": {"ok": True}, "rt_quote": {"ok": False}}

REASON = "宽度只有 19.7%、等权 20 日 -4.8%，趋势向下时先活下来"
INVALIDATION = "等权指数 10 个交易日站回 MA20 且宽度 >40%，则防守判断错误"


def _switch(desk, to: str, **kw):
    args = {
        "to": to,
        "reason": REASON,
        "evidence": ["daily:20261023"],
        "expected_edge_bps": 50.0,
        "invalidation": INVALIDATION,
        "probe_map": PROBE_READY,
        "day": "20261026",
        "nav": 5000.0,
    }
    args.update(kw)
    return persona_mod.switch(desk.paper, **args)


def _propose(desk, **kw):
    req = {
        "symbol": "002966.SZ",
        "side": "BUY",
        "sleeve": "CORE",
        "thesis": "银行板块 20 日跌幅收敛，苏州银行是唯一正收益且量比放大，用防守仓替代现金",
        "evidence": ["daily:20261023"],
        "invalidation": "跌破 20 日区间下沿且量比回落到 1.0 以下则判断错误",
        "target_weight": 0.2,
    }
    req.update(kw)
    return desk.decisions.propose(req)


# --------------------------------------------------------------- registration
def test_default_persona_matches_the_documented_one(desk):
    assert persona_mod.DEFAULT_PERSONA == "retail"
    assert desk.paper.persona_current()["current"] == "retail"
    assert sorted(persona_mod.keys()) == ["hot_money", "retail", "trader"]


def test_every_persona_declares_what_makes_it_invalid(desk):
    """A style with no 'why not' is a costume, not a discipline.

    这条还钉住 evidence 的形状：它必须是**带日期的静态记录**，不是判决。
    原来每条有个 `verdict`，而 `unproven` 根本不是裁判的词汇（裁判只有
    testing / promoted / rejected / underpowered）——手写的标签看起来像
    结算过的结论，其实没有重算路径。
    """
    for meta in persona_mod.REGISTRY.values():
        assert len(meta.why_not) >= 30, meta.key
        assert meta.evidence.get("as_of"), f"{meta.key}: evidence 要标日期"
        assert meta.evidence.get("why"), meta.key
        assert "verdict" not in meta.evidence,             f"{meta.key}: 判决标签已删，数字与 why 留着"
        assert meta.sleeves, meta.key


def test_a_rejected_persona_says_which_entry_point_was_actually_measured(desk):
    """判决要带口径，否则它会被当成"这条路不行"。

    2026-11-03 之前 `hot_money.evidence.why` 只写"P5–P24 九个短线结构全部
    rejected：首板 -0.11%/笔"。而 P5–P24 全部用 `entry=next_open`（次日开盘
    接力）测的，游资这条人格做的是"T 日封板买入"——两个不同的动作。模型读到的
    是"打板已被证伪"，于是 reports/close/20261030.md 写下"追二板既越权限又无
    证据"，把一条从未被测过的路关掉了。
    """
    meta = persona_mod.REGISTRY["hot_money"]
    why = meta.evidence["why"]

    # 判决标签没了，但口径这一层必须留下——它才是防止误读的东西。
    assert "rejected" in why, "那些结构确实被判负，这一点不能为了给路而抹掉"
    assert "next_open" in why, "说清测的是哪个入场点"
    assert "t_close" in why, "说清哪个入场点还没测"
    assert "-0.11" in why, "两个数都要留：判决本身不变"


# ------------------------------------------------------------------- switching
def test_switch_records_the_expectation_before_the_outcome(desk):
    out = _switch(desk, "trader")
    assert out["ok"] is True
    assert out["from"] == "retail" and out["to"] == "trader"
    rows = desk.paper.persona_switches()
    assert rows[0]["expected_edge_bps"] == 50.0
    assert rows[0]["invalidation"] == INVALIDATION
    assert rows[0]["nav_at_switch"] == 5000.0
    assert desk.paper.persona_current()["current"] == "trader"


def test_switch_without_a_number_is_refused(desk):
    with pytest.raises(persona_mod.PersonaError) as exc:
        _switch(desk, "trader", expected_edge_bps=None)
    assert exc.value.code == "NO_EXPECTATION"
    assert desk.paper.persona_current()["current"] == "retail"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_switch_rejects_a_non_finite_expectation(desk, value):
    with pytest.raises(persona_mod.PersonaError) as exc:
        _switch(desk, "trader", expected_edge_bps=value)
    assert exc.value.code == "BAD_EXPECTATION"
    assert desk.paper.persona_switches() == []
    assert desk.paper.persona_current()["current"] == "retail"


def test_switch_rolls_back_its_audit_row_if_activation_fails(desk):
    import sqlite3

    with desk.paper._conn() as conn:
        conn.execute("""
            CREATE TRIGGER reject_persona_activation
            BEFORE INSERT ON persona_state
            BEGIN SELECT RAISE(ABORT, 'synthetic activation failure'); END
        """)

    with pytest.raises(sqlite3.IntegrityError, match="synthetic activation failure"):
        _switch(desk, "trader")

    assert desk.paper.persona_switches() == []
    assert desk.paper.persona_current()["current"] == "retail"


def test_switch_without_evidence_is_refused(desk):
    with pytest.raises(persona_mod.PersonaError) as exc:
        _switch(desk, "trader", evidence=[])
    assert exc.value.code == "NO_EVIDENCE"


def test_switch_to_the_same_persona_is_refused(desk):
    with pytest.raises(persona_mod.PersonaError) as exc:
        _switch(desk, "retail")
    assert exc.value.code == "SAME_PERSONA"


def test_switch_is_capped_per_day(desk):
    _switch(desk, "trader")
    _switch(desk, "retail")
    with pytest.raises(persona_mod.PersonaError) as exc:
        _switch(desk, "trader")
    assert exc.value.code == "SWITCH_LIMIT"
    assert exc.value.status == 429
    assert desk.paper.persona_switch_count("20261026") == 2


def test_hot_money_switches_but_is_marked_blocked_without_quotes(desk):
    out = _switch(desk, "hot_money", probe_map=PROBE_NO_QUOTE)
    assert out["readiness"] == "blocked:rt_quote"
    assert out["persona"]["ready"] is False
    assert any("rt_quote" in w for w in out["warnings"])


def test_switching_against_the_regime_is_allowed_but_flagged(desk):
    out = _switch(desk, "hot_money", regime="stressed")
    assert out["ok"] is True
    assert any("stressed" in w for w in out["warnings"])


# --------------------------------------------------------------- proposal gate
def test_proposal_with_a_persona_that_is_not_active_is_refused(desk):
    with pytest.raises(DecisionError) as exc:
        _propose(desk, persona="trader")
    assert exc.value.code == "PERSONA_MISMATCH"
    assert exc.value.status == 409


def test_hot_money_cannot_spend_the_core_sleeve(desk):
    _switch(desk, "hot_money")
    with pytest.raises(DecisionError) as exc:
        _propose(desk, sleeve="CORE")
    assert exc.value.code == "PERSONA_SLEEVE"


def test_a_style_whose_data_is_missing_cannot_trade(desk):
    _switch(desk, "hot_money")
    with pytest.raises(DecisionError) as exc:
        _propose(desk, sleeve="AGGRESSIVE", persona="hot_money")
    assert exc.value.code == "PERSONA_NOT_READY"
    assert exc.value.status == 409
    assert "rt_quote" in str(exc.value)


def test_retail_can_still_trade_core(desk):
    ticket = _propose(desk)
    assert ticket["persona"] == "retail"
    assert ticket["sleeve"] == "CORE"
    logged = desk.paper.list_decisions(limit=5)
    assert any(str(d.get("kind")) == "propose" for d in logged)


def test_selling_is_never_blocked_by_a_persona(desk):
    """Liquidation is a risk action, not a style choice."""
    src = inspect.getsource(type(desk.decisions).propose)
    assert 'if side != "BUY"' in src
    # The three persona refusals must all be scoped to BUY.
    for code in ("PERSONA_MISMATCH", "PERSONA_SLEEVE", "PERSONA_NOT_READY"):
        block = src.split(code, 1)[0]
        assert 'side == "BUY"' in block or 'side != "BUY"' in block, code


# ------------------------------------------------------ the chooser is blind
def _identifiers(fn) -> set[str]:
    """Names an actual function body touches — docstrings do not count."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            names.add(sub.attr.lower())
        elif isinstance(sub, ast.Name):
            names.add(sub.id.lower())
    return names


def test_suggest_uses_declared_regimes_not_sleeve_membership(desk):
    # retired=() 把"袖开着"这个世界固定住，单测状态筛本身。
    out = persona_mod.eligibility("risk_on", PROBE_READY, retired=())

    assert out["consistent_with_tape"] == ["hot_money", "trader"]
    assert out["not_suited_to_regime"] == [
        {"key": "retail", "regimes": ["neutral", "risk_off", "stressed"]}
    ]
    assert out["blocked_by_sleeve"] == [], "没有关闭的袖时这一栏必须是空的"


def test_a_persona_whose_every_sleeve_is_closed_is_not_a_candidate(desk):
    """2026-11-01 之后 retail 只持有 CORE，而 CORE 关了。

    在那之前 `eligibility` 只筛两道（数据、regime），于是 `retail` 在 stressed
    下被列为 eligible——而它的每一笔提案都失败（自己那个袖 409 CORE_RETIRED，
    另一个袖 400 PERSONA_SLEEVE）。"这个风格适合这个盘面"和"这个风格能不能
    下单"是两个问题，当时只问了前一个。
    """
    out = persona_mod.eligibility("stressed", PROBE_READY, retired=("CORE",))

    assert out["blocked_by_sleeve"] == [
        {"key": "retail", "sleeves": ["CORE"], "retired": ["CORE"]}
    ]
    assert "retail" not in out["consistent_with_tape"]
    assert out["consistent_with_tape"] == ["trader"], "stressed 下只有 trader"


def test_sleeve_blocking_wins_over_regime_fit(desk):
    """袖关了是"哪天都不能用"，regime 不对口是"今天不合适"——不能混成一句。

    混在一起会让一个结构性死路读成"今天时机不好"，而这两种情况的补救办法
    完全不同（换人格 vs 等盘面）。
    """
    out = persona_mod.eligibility("risk_on", PROBE_READY, retired=("CORE",))

    blocked = [row["key"] for row in out["blocked_by_sleeve"]]
    unsuited = [row["key"] for row in out["not_suited_to_regime"]]
    assert "retail" in blocked and "retail" not in unsuited


def test_current_says_when_the_active_persona_cannot_place_an_order(desk):
    row = persona_mod.current(desk.paper, "stressed", PROBE_READY, retired=("CORE",))

    assert row["key"] == "retail"
    assert row["open_sleeves"] == []
    assert row["tradable"] is False
    assert "CORE_RETIRED" in row["dead_end"] and "PERSONA_SLEEVE" in row["dead_end"]


def test_current_is_tradable_when_a_sleeve_is_open(desk):
    row = persona_mod.current(desk.paper, "stressed", PROBE_READY, retired=())

    assert row["tradable"] is True
    assert row["open_sleeves"] == ["CORE"]
    assert "dead_end" not in row


def test_suggest_does_not_read_pnl_while_choosing(desk):
    """A selector that can see returns gets optimised on them.

    ``switch`` writes, ``describe`` reports metadata, ``suggest`` filters on two
    hard facts. None of them may touch NAV; only ``scorecard`` (post-hoc) may.
    """
    touched = _identifiers(persona_mod.eligibility)
    forbidden = {"nav", "equity", "pnl", "summary", "equity_pct"}
    assert not (touched & forbidden), f"suggest() must not read {touched & forbidden}"


def test_persona_module_never_switches_on_its_own(desk):
    src = inspect.getsource(persona_mod)
    for word in ("auto_switch", "rotate(", "pick_best", "argmax"):
        assert word not in src, f"persona.py must not contain {word}"


# ------------------------------------------------------------------- core nav
def test_core_nav_records_a_day_spent_entirely_in_cash(desk):
    """The whole point of the fix: no trade is still a position to mark."""
    core = desk.core_book()
    day = core.last_day()
    out = core.record_nav(day)
    assert out["recorded"] is True
    assert out["positions"] == 0
    rows = desk.paper.core_nav_history(limit=5)
    assert [r["trade_date"] for r in rows][-1] == day
    assert float(rows[-1]["nav"]) == pytest.approx(5000.0)
    assert float(rows[-1]["cash"]) == pytest.approx(5000.0)


def test_core_nav_is_idempotent(desk):
    core = desk.core_book()
    day = core.last_day()
    core.record_nav(day)
    core.record_nav(day)
    rows = [r for r in desk.paper.core_nav_history(limit=10) if r["trade_date"] == day]
    assert len(rows) == 1


def test_core_nav_refuses_to_rewrite_the_past(desk):
    core = desk.core_book()
    day = core.last_day()
    core.record_nav(day)
    earlier = [str(i) for i in core.prices().index][-10]
    out = core.record_nav(earlier)
    assert out["recorded"] is False
    assert "refusing to rewrite history" in out["reason"]


# ------------------------------------------------------------------ attribution
def test_unlabelled_cases_are_not_attributed_to_todays_persona(desk):
    from service.trading.attribution import by_persona

    desk.paper.add_case({
        "case_id": "case-legacy", "symbol": "002966.SZ",
        "opened_at": "2026-10-23T14:55:00+08:00",
        "closed_at": "2026-10-24T14:55:00+08:00",
        "qty": 100, "entry": 9.0, "exit": 9.2, "pnl": 20.0, "pnl_pct": 0.0222,
        "thesis": "legacy row without a persona", "invalidation": "n/a",
        "ticket_ids": [], "note": "", "strategy": "llm",
    })
    out = by_persona(desk)
    rows = {r["persona"]: r for r in out["personas"]}
    assert rows["unlabelled"]["closed"] == 1
    assert rows["retail"]["closed"] == 0
    assert "不按今天的风格回填" in out["note"]


# ------------------------------------------------------------------ over HTTP
TOKEN = {"X-Quant-Token": "change-me-local-only"}


@pytest.fixture
def client(desk, monkeypatch):
    from fastapi.testclient import TestClient

    from service.api import app as app_module
    from service.api import trading as trading_api
    from service.api.app import app

    monkeypatch.setattr(trading_api, "get_desk", lambda: desk)
    monkeypatch.setattr(app_module, "_usage", desk.paper)
    with TestClient(app) as c:
        yield c


def test_persona_endpoint_reports_but_does_not_pick(client):
    r = client.get("/persona", headers=TOKEN)
    assert r.status_code == 200
    body = r.json()
    assert body["current"]["key"] == "retail"
    assert set(body["eligibility"]) >= {"consistent_with_tape", "blocked_by_data"}
    assert "recommended" not in body["eligibility"]
    assert body["scorecard"]["switches"] == 0


def test_persona_switch_over_http_is_recorded(client, desk):
    r = client.post("/persona/switch", headers=TOKEN, json={
        "to": "trader",
        "reason": "等权 20 日 -4.8%、宽度 21.9%，被动持有没有催化，改为事件驱动",
        "evidence": ["daily:20261026"],
        "expected_edge_bps": 80,
        "invalidation": "等权指数站回 MA200 且宽度回到 40% 以上则判断错误",
        "horizon_days": 5,
        "regime": "stressed",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["to"] == "trader" and body["from"] == "retail"
    assert body["switches_today"] == 1
    assert desk.paper.persona_current()["current"] == "trader"
    rows = desk.paper.persona_switches()
    assert rows[0]["expected_edge_bps"] == 80.0


def test_persona_switch_needs_the_expectation_over_http(client, desk):
    r = client.post("/persona/switch", headers=TOKEN, json={
        "to": "trader",
        "reason": "等权 20 日 -4.8%、宽度 21.9%，被动持有没有催化，改为事件驱动",
        "evidence": ["daily:20261026"],
        "invalidation": "等权指数站回 MA200 则判断错误",
    })
    assert r.status_code == 422
    assert desk.paper.persona_current()["current"] == "retail"


def test_quote_feed_and_fill_price_are_different_facts(desk, monkeypatch):
    """The feed answers after the close; the price it returns cannot be filled.

    Conflating the two produced "no realtime quote" at 22:00, which reads as a
    broken integration while the vendor was answering with the session's close.
    """
    import service.trading.realtime as rt

    monkeypatch.setenv("REALTIME_QUOTES", "1")
    monkeypatch.setattr(rt, "session_state", lambda now=None, **_: {
        "now": "2026-10-26T22:00:00+08:00", "weekday": 0,
        "in_session": False, "label": "盘前/盘后",
    })
    quote = persona_mod.probe(desk.market, desk.registry)["rt_quote"]
    assert quote["feed_available"] is True       # the source is wired
    assert quote["tradable"] is False            # but no order can fill on it
    assert quote["in_session"] is False
    assert "不可成交" in quote["label"]


def test_during_the_session_the_quote_is_tradable(desk, monkeypatch):
    import service.trading.realtime as rt

    monkeypatch.setenv("REALTIME_QUOTES", "1")
    # `**_` on purpose: probe now passes the trading calendar, because
    # without it a weekday public holiday reports `in_session=True`. A stub
    # that only accepts `now` turns that into a TypeError, which probe
    # swallows into an empty session — the test then fails for a reason
    # that has nothing to do with the behaviour it asserts.
    monkeypatch.setattr(rt, "session_state", lambda now=None, **_: {
        "now": "2026-10-26T10:30:00+08:00", "weekday": 0,
        "in_session": True, "label": "连续竞价",
    })
    quote = persona_mod.probe(desk.market, desk.registry)["rt_quote"]
    assert quote["tradable"] is True
    assert quote["ok"] is True


def test_bounded_benchmark_measures_the_same_window_as_the_full_series(desk):
    """Bounding the read must change the cost, not the number.

    Attribution rebuilt twenty years of market returns to look up one holding
    window. The bounded read is derived from the cases themselves; the risk is
    that ``_window_return`` anchors on the last close *before* entry, so starting
    the series at the entry day would silently change every denominator.
    """
    from service.trading.arena import _market_returns, _window_return
    from service.trading.attribution import _market_for

    full = _market_returns(desk.data)
    assert len(full) > 100, "夹具应有完整日线"

    start, end = str(full.index[-30]), str(full.index[-1])
    cases = [{"opened_at": f"{start[:4]}-{start[4:6]}-{start[6:]} 09:30:00",
              "closed_at": f"{end[:4]}-{end[4:6]}-{end[6:]} 15:00:00"}]

    bounded = _market_for(desk, cases)
    assert 0 < len(bounded) < len(full), "必须真的裁过"
    assert str(bounded.index.min()) < start, "必须包含入场前的那根收盘"
    assert (_window_return(bounded, start, end)
            == pytest.approx(_window_return(full, start, end), rel=1e-12))


def test_a_case_outside_the_bounded_window_falls_back_to_the_full_series(desk):
    from service.trading.arena import _market_returns
    from service.trading.attribution import _market_for

    got = _market_for(desk, [{"opened_at": "1990-01-01", "closed_at": "1990-01-02"}])
    assert len(got) == len(_market_returns(desk.data)), \
        "取不到窗口时必须退回全量，而不是返回空让超额变成 None"


def test_switches_today_is_counted_for_today_not_read_from_the_last_switch(desk):
    """`persona_state.switches_today` 只在切换时写一次，跨日不归零。

    2026-11-09 实读：`persona_state.day=20261105`、`switches_today=1`，而当天
    一次切换都没发生。任何读到该字段的窗口都会以为日限 2 次已用掉一半，
    从而不敢做本该做的切换——配额判定本身是按 `persona_switch.day` 现算的，
    所以这只是显示层说谎，但说谎的方向恰好让账户更僵。
    """
    _switch(desk, "trader", day="20200101")

    state = desk.paper.persona_current()
    assert desk.paper.persona_switch_count("20200101") == 1
    assert state["switches_today"] == 0, "昨天（乃至更早）的切换不算今天的配额"
