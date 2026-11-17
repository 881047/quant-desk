"""The pack is where "you cannot conclude yet" has to be visible.

`session.data_ready` was a bare boolean for months and nothing read it: on
2026-10-29 it said `false` while a close report was being written anyway. The
field is now a coded verdict with an explicit block list, and the pipeline's own
failure rides along as `pipeline_alert`, because a scheduled task's exit code is
invisible from inside a session.
"""

from __future__ import annotations

import pytest

from service.trading.context_pack import build_context_pack

CODES = {"READY", "SESSION_OPEN", "AWAITING_PUBLISH", "THIN_COVERAGE",
         "NON_TRADING_DAY", "NO_CALENDAR"}


@pytest.fixture
def root(tmp_path):
    (tmp_path / "data").mkdir()
    return tmp_path


def test_data_ready_and_the_code_cannot_drift(desk, root):
    """Two fields, one meaning. If they disagree, one of them is lying."""
    session = build_context_pack(desk, root=root)["session"]

    assert session["readiness"] in CODES
    # `data_ready` is `readiness.ok`, and `ok` is also true for NON_TRADING_DAY:
    # "there is no session to conclude about" is not "cannot conclude". So the
    # comparison against READY only holds while the market is open today — the
    # code-agnostic statement, asserted below, is that a ready pack blocks
    # nothing. (This line used to fail whenever the suite ran on a weekend.)
    if session["market_open_today"]:
        assert session["data_ready"] is (session["readiness"] == "READY")
    assert isinstance(session["blocked"], list)
    assert (session["blocked"] == []) is session["data_ready"]


def test_a_failed_pipeline_rides_in_the_pack(desk, root):
    """No ALERT file, no alert; a file, and the session starts knowing."""
    assert build_context_pack(desk, root=root)["pipeline_alert"] is None

    (root / "data" / "ALERT.txt").write_text(
        "20261029 daily_job gate=not_ready\n- backfill rc=1\n", encoding="utf-8")
    pack = build_context_pack(desk, root=root)

    assert "gate=not_ready" in pack["pipeline_alert"]
    assert "backfill rc=1" in pack["pipeline_alert"]


def test_an_alert_cannot_blow_the_size_budget(desk, root):
    """The pack is capped at 4 KB; a runaway log must not be pasted whole."""
    (root / "data" / "ALERT.txt").write_text("x" * 5000, encoding="utf-8")
    alert = build_context_pack(desk, root=root)["pipeline_alert"]

    assert alert is not None and len(alert) <= 300


def test_a_readiness_alert_rides_beside_a_pipeline_alert(desk, root):
    """Two writers, one field: failed run vs day that could not be checked."""
    (root / "data" / "ALERT.txt").write_text("20261029 gate=not_ready\n", encoding="utf-8")
    (root / "data" / "READINESS_ALERT.txt").write_text(
        "20261029 not ready: AWAITING_PUBLISH\n", encoding="utf-8")

    alert = build_context_pack(desk, root=root)["pipeline_alert"]

    assert "gate=not_ready" in alert
    assert "AWAITING_PUBLISH" in alert


# --------------------------------------------------------------- 超预算时的取舍
#
# 这个 pack 存在的理由是回答两个问题：市场什么状态、盘面什么温度。旧的兜底
# 顺序是 sentiment → observations → market，也就是一旦候选裁完还超预算，先丢
# 的正是那两个答案，而返回的东西看起来仍然完整。

def _core(**extra):
    return {
        "as_of": "2026-10-31T17:00:00+08:00",
        "session": "closed",
        # 夹具的形状必须和生产一致：真实 pack 的 market 块没有 `permission`
        # （2026-11-03 拆掉了那句祈使句），有 label / label_basis / enforced。
        # 留着一个已经不存在的字段，等于让裁剪测试在量一份没人会返回的载荷。
        "market": {"label": "stressed", "label_basis": "breadth 0.20 < 0.35",
                   "enforced": False},
        "sentiment": {"tone": "退潮", "trade_date": "20261030"},
        "data": {"usable": True},
        "account": {"equity": "5007.51"},
        "risk": {"kill_switch": False},
        "candidates": [],
        "recent_tickets": [],
        **extra,
    }


def test_a_long_pack_sheds_commentary_before_the_tape():
    """先丢解说，最后才动盘面。"""
    from service.trading.context_pack import MAX_BYTES, _fit, _size

    pack = _core(rules="规" * (MAX_BYTES + 200),
                 candidates_basis="方法说明" * 50,
                 exposure={"invested": 0.0},
                 observations=[{"note": "测" * 400}])

    out = _fit(pack)

    assert _size(out) <= MAX_BYTES
    assert "market" in out, "市场状态不能被静默丢掉"
    assert "sentiment" in out, "盘面温度不能被静默丢掉"
    assert "rules" in out["omitted"]


def test_an_oversized_tape_says_so_instead_of_returning_a_quiet_pack():
    """连骨架都装不下时报错，不要发出一个缺了盘的"正常"包。"""
    from service.trading.context_pack import MAX_BYTES, _fit

    out = _fit(_core(market={"label": "stressed", "pad": "x" * (MAX_BYTES + 500)},
                     observations=[{"note": "y" * 800}]))

    assert out.get("degraded") is True
    assert "error" in out


# ---------------------------------------------------------------- 读者视角
#
# 下面四条防的都是同一类故障：包里的字段**看着像**某个意思，实际是另一个意思，
# 而且两处说法互相矛盾时没有任何一句话说明该信哪个。断言不看实现，只看一个
# 只读这个 JSON 的读者能不能得出正确结论。


def test_data_booleans_do_not_read_as_permission(desk, root):
    """`*_ok` 被读成准入：2026-11-01 同一个包里既说「游资数据可用」又说「不新开多头」。

    这两项量的是「支撑这个袖的数据在不在」，不是「准不准交易」。名字必须说这件事，
    而且要有一句话说清它和 market.permission 的分工，否则读者只能挑一个信。
    """
    pack = build_context_pack(desk, root=root)
    data = pack["data"]

    assert "core_usable" in data and "aggressive_usable" in data
    assert "core_ok" not in data and "aggressive_ok" not in data
    assert "不是准入" in data["meaning"]
    # 空列表是纯占位：4KB 的包里不放一个永远为 [] 的键
    assert ("core_missing" in data) is bool(data.get("core_missing"))
    assert ("aggressive_missing" in data) is bool(data.get("aggressive_missing"))


def test_a_state_label_carries_its_formula_and_whether_anything_enforces_it(desk, root):
    """标签必须能被核对，而且必须说清有没有人执行它。

    原来这里是 `permission`：一句"只允许减仓与对冲，不新开多头"。它是祈使句，
    而 guard 不读这个标签（`enforced: False` 就在旁边）。一句话像规则又不被执行，
    读者要么高估它（以为会被拒单），要么低估它（以为纯装饰），差别是一整个仓位。

    现在换成 `label_basis`：趋势方向、波动率分位对哪个阈值、宽度对哪个阈值。
    同样的信息，但读者可以同意也可以反对那个阈值——判断不再被预先做完。
    """
    market = build_context_pack(desk, root=root)["market"]

    assert market["enforced"] is False, "没有任何检查读这个标签，这一条不能变"
    assert market["label_basis"], "标签必须带它的算式，否则只是个词"
    assert "vol分位" in market["label_basis"] and "宽度" in market["label_basis"]
    assert "permission" not in market, "祈使句不该回到上下文包里"


def test_observation_counts_and_items_are_the_same_set(desk, root):
    """`deviations: 1` 配 `items: []`：数了，但看不见。

    被计数却不出现在 items 里的记录，读者无法判断值不值得再调一次工具；
    裁掉多少也必须写出来，不能留给读者做减法。
    """
    obs = build_context_pack(desk, root=root)["observations"]
    counted = obs["constraints"] + obs["deviations"]

    assert len(obs["items"]) + obs.get("items_dropped", 0) >= min(counted, 3)
    if counted <= 3:
        assert len(obs["items"]) == counted
        assert "items_dropped" not in obs


def test_a_rejected_ticket_carries_its_reason(desk, root, monkeypatch):
    """只有状态没有原因 = 下一单还是同一个错。

    `reasons` 本来就存在票据里，是包里把它丢了。
    """
    tickets = [{
        "ticket_id": "td-20261027-59b9127a", "symbol": "000700.SZ", "side": "BUY",
        "status": "rejected", "qty": 100,
        "reasons": ["涨停价封死，排队也买不到——换一只，或等开板"],
    }]
    monkeypatch.setattr(desk.paper, "list_tickets", lambda *a, **k: tickets)

    row = build_context_pack(desk, root=root)["recent_tickets"][0]

    assert row["status"] == "rejected"
    assert "排队" in row["why"]
    assert len(row["why"]) <= 40, "4KB 的包里原因只给一句话"


def test_the_tape_gate_arrives_with_its_threshold(desk, root):
    """0.3974 这个数单独摆着没法判断：它是 1.7pp 超过 0.38，读者不可能知道。

    `height_ok` 一直在包里，另外两项和阈值从来没有——同一个闸门的三个检查
    分居两个端点，读者会以为自己看到的是全部。
    """
    import json

    from service import settings

    day = desk.market.latest_bar_date()
    folder = root / "data" / "sentiment"
    folder.mkdir(parents=True)
    (folder / f"{day}.json").write_text(json.dumps({
        "trade_date": day,
        "sentiment": {"break_rate": 0.3974, "tone": "退潮", "max_boards": 5,
                      "height_ok": True, "phase": "settled"},
    }), encoding="utf-8")

    gate = build_context_pack(desk, root=root)["sentiment"]["gate"]

    assert gate["break_rate_gate"] == settings.num("sentiment.break_rate_gate")
    assert gate["ok"] is False
    assert "break_rate_ok" in gate["failed"], "炸板率过闸门必须被判失败"
    assert gate["ok"] is False


def test_a_candidate_row_does_not_spend_the_budget_on_the_next_step(desk, root):
    """候选行只买"值得展开哪几个名字"，不买下单前才用得上的字段。

    `screen` 在 2026-11-01 多了 `board` 和 `band`（涨跌幅），实测每行 13 字节。
    5 行就是 65 字节，而这个包只剩 56——第一次改完候选直接从 5 条掉到 3 条，
    `omitted: ["candidates:2"]` 看得见，但那是拿测量换信息，方向反了。
    所以包里改成显式白名单：新增字段必须有人做一次决定，而不是静默挤掉候选。
    """
    from service.trading.context_pack import CANDIDATE_FIELDS
    from service.trading.screen import ScreenConfig, screen

    rows = screen(desk.market, desk.market.latest_bar_date(),
                  ScreenConfig(max_candidates=5, max_price=45))
    assert rows, "没有候选就证明不了这条投影在删东西"

    extra = set(rows[0]) - set(CANDIDATE_FIELDS)
    assert extra == {"as_of", "board", "band"}, (
        f"screen 多出了 {sorted(extra)}：要么写进白名单，要么写清为什么它属于下一跳")

    pack = build_context_pack(desk, root=root)
    for cand in pack["candidates"]:
        assert set(cand) <= set(CANDIDATE_FIELDS)
    assert len(pack["candidates"]) == 5, "白名单没生效，候选又被挤掉了"


def test_risk_fill_mode_is_visible_at_the_path_the_prompts_read(desk, root):
    """8 个 prompt 读 `quant_context.risk.fill_mode`，而它原本只在 `risk.limits` 里。

    读到 None 的模型会自己猜成交口径（按开盘价排队 vs 实时成交），
    两者的报告写法与 19:30 核账口径都不同。
    """
    risk = build_context_pack(desk, root=root)["risk"]
    assert risk["fill_mode"] == risk["limits"]["fill_mode"]


def test_observation_failure_is_not_reported_as_zero_constraints(desk, monkeypatch):
    from service.trading import context_pack, observation

    def boom(*_args, **_kwargs):
        raise RuntimeError("observation inputs unavailable")

    monkeypatch.setattr(observation, "snapshot", boom)
    out = context_pack._observations(desk, desk.market.latest_bar_date())

    assert out["status"] == "error"
    assert out["constraints"] is None
    assert out["high_attention"] is None
    assert out["deviations"] is None
    assert "observation inputs unavailable" in out["reason"]


def test_observation_success_with_no_findings_is_explicitly_empty(desk, monkeypatch):
    from service.trading import context_pack, observation

    monkeypatch.setattr(observation, "snapshot", lambda *_args: {
        "constraints": [], "deviations": [],
        "counts": {"constraints": 0, "high_attention": 0, "deviations": 0},
    })
    out = context_pack._observations(desk, desk.market.latest_bar_date())

    assert out["status"] == "empty"
    assert out["constraints"] == 0
    assert out["deviations"] == 0
    assert out["items"] == []


def test_context_read_uses_a_preview_without_mutating_or_filling_the_real_book(
        desk, root, monkeypatch):
    """A generic read may preview maintenance, never perform it on the account."""
    from copy import deepcopy

    from service.trading.book import PaperBook

    real_book = desk.book
    before_state = deepcopy(real_book.state)
    before_ledger = deepcopy(real_book.ledger.snapshot())
    before_revision = real_book._revision
    original_rollover = PaperBook.rollover
    original_mark = PaperBook.mark_prices

    def guarded_rollover(self, *args, **kwargs):
        if self is real_book:
            raise AssertionError("context read advanced the durable book")
        return original_rollover(self, *args, **kwargs)

    def guarded_mark(self, *args, **kwargs):
        if self is real_book:
            raise AssertionError("context read marked the durable book")
        return original_mark(self, *args, **kwargs)

    def no_order_settlement(*_args, **_kwargs):
        raise AssertionError("a read must not settle or fill queued orders")

    monkeypatch.setattr(PaperBook, "rollover", guarded_rollover)
    monkeypatch.setattr(PaperBook, "mark_prices", guarded_mark)
    monkeypatch.setattr(PaperBook, "settle_pending", no_order_settlement)
    monkeypatch.setattr(PaperBook, "settle_same_session_due", no_order_settlement)

    pack = build_context_pack(desk, root=root)

    assert pack["account"]["read_only"] is True
    assert real_book.state == before_state
    assert real_book.ledger.snapshot() == before_ledger
    assert real_book._revision == before_revision


def test_read_view_previews_t1_settlement_without_unlocking_the_real_position(desk):
    from tests.trading.test_autostop import _held

    day = desk.market.latest_bar_date()
    symbol, _ticket_id, _stop = _held(desk, sellable=False)
    original = desk.book.ledger.account.positions[symbol]
    original.available_qty = 0
    desk.book.state["day"] = "20000101"
    desk.book.state["actions_through"] = day

    view = desk.book.read_view(day)
    preview = view.ledger.account.positions[symbol]

    assert preview.available_qty == preview.total_qty
    assert original.available_qty == 0
    assert desk.book.state["day"] == "20000101"


def test_a_vendor_break_rate_arrives_with_the_vendor_threshold(desk, root):
    """The pack must show the ruler that produced the verdict, not the other one.

    2026-11-13 shipped `break_rate: 0.3621, break_rate_basis: vendor` next to
    `break_rate_gate: 0.38`: the number was vendor-based, the threshold derived,
    and the pair said "passed" while the derived pool's 0.387 said "failed".
    """
    import json

    from service import settings

    day = desk.market.latest_bar_date()
    folder = root / "data" / "sentiment"
    folder.mkdir(parents=True)
    (folder / f"{day}.json").write_text(json.dumps({
        "trade_date": day,
        "sentiment": {"break_rate": 0.3621, "break_rate_basis": "vendor",
                      "counts_source": "vendor", "tone": "退潮", "max_boards": 4,
                      "height_ok": True, "phase": "settled"},
    }), encoding="utf-8")

    gate = build_context_pack(desk, root=root)["sentiment"]["gate"]

    assert gate["break_rate_gate"] == settings.num("sentiment.break_rate_gate_vendor")
    assert gate["break_rate_basis"] == "vendor"
    assert "break_rate_ok" in gate["failed"]
