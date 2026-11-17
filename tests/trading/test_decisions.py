"""Decision protocol invariants: sizing is Python's job, approval is idempotent."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from service.kernel.clock import SHANGHAI
from service.kernel.models import D
from service.trading.decisions import DecisionError
from tests.trading.helpers import (
    pick_st_symbol,
    pick_symbols,
    pin_to_last_bar,
    proposal,
)


def test_propose_computes_size_price_and_stop(desk):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))

    assert ticket["status"] == "proposed"
    assert ticket["computed_by"] == "python"
    assert ticket["qty"] > 0 and ticket["qty"] % 100 == 0
    assert Decimal(ticket["limit_price"]) > 0
    assert Decimal(ticket["stop_price"]) < Decimal(ticket["price_ref"])
    assert ticket["stop_pct"] >= 0.03
    assert ticket["snapshot"]["complete"] is True


def test_snapshot_includes_dataset_evidence_but_not_other_evidence_kinds(desk):
    day = desk.market.latest_bar_date()

    snap = desk.decisions._snapshot(day, evidence=[
        f"limit_pool_t0:{day}", f"screen:{day}", "not_a_dataset:value",
    ])

    assert "limit_pool_t0" in snap["snapshots"]
    assert "screen" not in snap["snapshots"]
    assert "not_a_dataset" not in snap["snapshots"]


def test_model_may_choose_a_quantity_and_the_kernel_still_bounds_it(desk):
    """`qty` is an intent, not a hole in the guard.

    "One lot" used to be unspeakable: the only size field was target_weight, so a
    CNY 1,307 stock under a 20% target rounded to zero shares and was rejected
    with `size=0`. The quantity is honoured now — inside the same lot table,
    single-position cap and per-ticket risk budget.
    """
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol, qty=100, target_weight=None))
    assert ticket["qty"] == 100
    assert 0 < ticket["target_weight"] <= 0.40


def test_a_quantity_that_breaks_the_lot_table_names_the_legal_one(desk):
    symbol = pick_symbols(desk, 1)[0]
    with pytest.raises(DecisionError) as err:
        desk.decisions.propose(proposal(symbol, qty=150, target_weight=None))
    assert err.value.code == "BAD_SIZE"
    assert "合法数量" in str(err.value)


def test_a_quantity_beyond_the_cap_names_the_maximum(desk):
    symbol = pick_symbols(desk, 1)[0]
    with pytest.raises(DecisionError) as err:
        desk.decisions.propose(proposal(symbol, qty=1_000_000, target_weight=None))
    assert err.value.code in ("POSITION_CAP", "RISK_CAP")
    assert "最多" in str(err.value)


def test_two_size_fields_at_once_are_refused(desk):
    symbol = pick_symbols(desk, 1)[0]
    with pytest.raises(DecisionError) as err:
        desk.decisions.propose(proposal(symbol, qty=100, target_weight=0.2))
    assert err.value.code == "BAD_SIZE"


def test_target_weight_is_clamped_to_single_cap(desk):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol, target_weight=0.95))
    assert ticket["status"] == "proposed"
    assert ticket["target_weight"] <= 0.40


def test_approve_is_idempotent(desk):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))
    first = desk.decisions.approve(ticket["ticket_id"])
    second = desk.decisions.approve(ticket["ticket_id"])

    assert first["status"] == "filled"
    assert second["status"] == "already_filled"
    assert first["fill"]["fill_id"] == second["fill"]["fill_id"]
    assert len(desk.book.state["fills"]) == 1


def test_a_guard_that_loses_the_race_returns_the_committed_fill(desk, monkeypatch):
    """A stale guard answer must not overwrite or misreport another process's fill."""
    from types import SimpleNamespace

    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))

    def fill_then_veto(*_args, **_kwargs):
        current = desk.paper.get_ticket(ticket["ticket_id"])
        current.update(status="filled", fill={"qty": current["qty"], "price": "10.00"})
        desk.paper.save_ticket(current)       # the other process commits first
        return SimpleNamespace(ok=False, failures=["stale guard"],
                               as_dict=lambda: {"ok": False})

    monkeypatch.setattr(desk.guard, "preflight", fill_then_veto)
    result = desk.decisions.approve(ticket["ticket_id"])

    assert result["status"] == "already_filled"
    assert result["ticket"]["status"] == "filled"
    assert desk.paper.get_ticket(ticket["ticket_id"])["status"] == "filled"


def test_buy_is_t_plus_one(desk):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))
    desk.decisions.approve(ticket["ticket_id"])

    pos = desk.book.ledger.account.position(symbol)
    assert pos.total_qty == ticket["qty"]
    assert pos.available_qty == 0

    sell = desk.decisions.propose(proposal(symbol, side="SELL", sleeve="CORE"))
    assert sell["status"] == "rejected"
    assert any("t_plus_one" in r or "size" in r for r in sell["reasons"])


def test_max_positions_blocks_the_fourth_name(desk):
    symbols = pick_symbols(desk, 4)
    assert len(symbols) == 4
    for symbol in symbols[:3]:
        ticket = desk.decisions.propose(proposal(symbol, target_weight=0.2))
        assert ticket["status"] == "proposed", ticket.get("reasons")
        assert desk.decisions.approve(ticket["ticket_id"])["status"] == "filled"

    fourth = desk.decisions.propose(proposal(symbols[3], target_weight=0.2))
    assert fourth["status"] == "rejected"
    assert any("max_positions" in r for r in fourth["reasons"])


def test_st_stock_is_blocked(desk):
    symbol = pick_st_symbol(desk)
    if symbol is None:
        pytest.skip("no active ST name in the dataset")
    date = desk.market.last_trading_day()
    snap = desk.market.snapshot(symbol, date)
    if snap is None:
        pytest.skip("ST name has no bar today")
    result = desk.guard.preflight(
        desk.book, symbol=symbol, side="BUY", sleeve="AGGRESSIVE",
        qty=100, price=snap.last, date=date,
    )
    assert any(c.name == "st" and not c.ok for c in result.checks)


def test_an_st_position_can_still_be_sold(desk):
    """股票被 ST 之后，持仓必须还能卖出去。

    这是本次审查发现的一个真缺陷：guard 只拦 BUY（文案就写着 "ST: sells
    allowed"），而内核的 ``risk`` 与 ``oms`` 对买卖一律拒绝。于是"股票被 ST
    之后"这一格，持仓卖不掉——止损单、减仓单、清仓单一律 ST 拒绝。风控在
    最需要你跑的时候把你锁住。
    """
    symbol = pick_st_symbol(desk)
    if symbol is None:
        pytest.skip("no active ST name in the dataset")
    date = desk.market.last_trading_day()
    snap = desk.market.snapshot(symbol, date)
    if snap is None:
        pytest.skip("ST name has no bar today")

    # Give the desk a position in the ST name, as if it had been bought before
    # the flag appeared.
    account = desk.book.ledger.account
    account.position(symbol)
    pos = account.positions[symbol]
    pos.total_qty = 100
    pos.available_qty = 100
    pos.avg_cost = D(snap.last)

    ticket = desk.decisions.propose(
        proposal(symbol, side="SELL", target_weight=None, qty=100))
    assert ticket["status"] != "rejected", (
        f"ST 持仓被拒了：{ticket.get('reasons')}")
    assert not any("st" in str(reason) for reason in ticket.get("reasons", [])), (
        f"拒绝理由里有 ST：{ticket.get('reasons')}")


def test_kill_switch_blocks_new_tickets(desk):
    symbol = pick_symbols(desk, 1)[0]
    desk.book.set_kill_switch(True)
    ticket = desk.decisions.propose(proposal(symbol))
    assert ticket["status"] == "rejected"
    assert any("kill_switch" in r for r in ticket["reasons"])


def test_drawdown_stop_blocks_buys(desk):
    symbol = pick_symbols(desk, 1)[0]
    desk.book.state["day_start_equity"] = str(Decimal(7000))  # -28.6% vs 5000
    ticket = desk.decisions.propose(proposal(symbol))
    assert ticket["status"] == "rejected"
    assert any("daily_loss" in r or "reduce_only" in r or "drawdown" in r
               for r in ticket["reasons"])


def test_old_ticket_expires_instead_of_executing(desk):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))
    ticket["trade_date"] = "20200102"
    desk.paper.save_ticket(ticket)

    result = desk.decisions.approve(ticket["ticket_id"])
    assert result["status"] == "expired"
    assert not desk.book.state["fills"]


def test_evidence_and_invalidation_are_required(desk):
    symbol = pick_symbols(desk, 1)[0]
    with pytest.raises(DecisionError):
        desk.decisions.propose(proposal(symbol, evidence=[]))
    with pytest.raises(DecisionError):
        desk.decisions.propose(proposal(symbol, invalidation=""))
    with pytest.raises(DecisionError):
        desk.decisions.propose(proposal(symbol, thesis="太短"))


def test_a_position_exactly_at_its_cap_is_not_rejected(desk):
    """Decimal("0.3") <= 0.3 is False, so an exact-cap ticket used to fail."""
    from decimal import Decimal as D

    from service.trading.guard import ratio_within

    assert ratio_within(D("1500.00"), D("5000.00"), 0.30) is True
    assert ratio_within(D("1501.00"), D("5000.00"), 0.30) is False
    assert ratio_within(D("2000.00"), D("5000.00"), 0.40) is True


def test_star_lot_is_at_least_200_shares(desk):
    """A STAR ticket must never come out as a 100-share order."""
    day = desk.market.last_trading_day()
    daily = desk.market.daily(day)
    px = pd.to_numeric(daily["close"], errors="coerce")
    codes = daily[(daily["ts_code"].astype(str).str.startswith("688")) & (px <= 12)]
    if codes.empty:
        pytest.skip("no affordable STAR name in the dataset")
    symbol = str(codes.iloc[0]["ts_code"])
    ticket = desk.decisions.propose(proposal(symbol, target_weight=0.3))
    qty = ticket.get("qty") or 0
    assert qty == 0 or qty >= 200


def test_an_unknown_strategy_cannot_place_a_ticket(desk):
    symbol = pick_symbols(desk, 1)[0]
    with pytest.raises(DecisionError):
        desk.decisions.propose(proposal(symbol, strategy="no_such_strategy"))


def test_a_ticket_carries_its_strategy(desk, monkeypatch):
    from service.trading.strategy import REGISTRY, StrategyMeta

    # P21 retired the only probation strategy in the real registry; a ticket must
    # still record which strategy asked for it, so the rule is tested on a
    # temporary one rather than a live key.
    monkeypatch.setitem(REGISTRY, "test_sig", StrategyMeta(
        key="test_sig", name="测试信号", kind="signal",
        regimes=("risk_on", "neutral", "risk_off", "stressed"),
        status="probation", max_weight=0.10))
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol, strategy="test_sig",
                                             target_weight=0.1))
    assert ticket["status"] == "proposed", ticket.get("reasons")
    assert ticket["strategy"] == "test_sig"


def test_fill_basis_follows_the_book_not_a_constant(desk):
    """The label used to be the literal "close" whatever mode the book ran."""
    desk.book.fill_mode = "next_open"
    ticket = desk.decisions.propose(proposal(pick_symbols(desk, 1)[0]))
    assert ticket["fill_basis"] == "next_open"
    desk.decisions.reject(ticket["ticket_id"], "切换执行模式，结束当前票据")

    desk.book.fill_mode = "close"
    ticket = desk.decisions.propose(proposal(pick_symbols(desk, 1)[0]))
    assert ticket["fill_basis"] == "close"


def test_a_late_close_is_labelled_backfill(desk):
    """A price that was public before the decision is not an execution.

    `propose` now refuses this outright ("20261026 的日线还没发布"), which is the
    stronger fix. The label still matters for any path that reaches `_fill_basis`
    with a stale session: on 2026-10-25 a proposal made at 23:32 was booked
    against the 2026-10-23 close and labelled `close`, so the account held a
    two-day-old price that looked like a fill.
    """
    bar = desk.market.latest_bar_date()
    session = datetime.strptime(bar, "%Y%m%d").date()
    calendar = desk.market.calendar
    for step in range(1, 15):
        candidate = session + timedelta(days=step)
        if calendar.is_trading_day(candidate.strftime("%Y%m%d")):
            break
    else:  # pragma: no cover - a two-week holiday would be its own bug
        pytest.skip("no trading day within two weeks of the last bar")
    desk.decisions._now = lambda: datetime(
        candidate.year, candidate.month, candidate.day, 17, tzinfo=SHANGHAI)

    desk.book.fill_mode = "close"
    assert desk.decisions._fill_basis(bar) == "backfill"

    # Under next_open the ticket is queued for a *future* open, so the label is
    # the mode, not "the price is already known".
    desk.book.fill_mode = "next_open"
    assert desk.decisions._fill_basis(bar) == "next_open"


def test_the_book_can_re_run_the_guard_when_it_fills(desk):
    """Queued tickets are executed by tomorrow's account, so the book needs the guard."""
    assert desk.book.guard is desk.guard


def test_ticket_risk_is_measured_at_the_worst_fill(desk):
    """`est_risk_cny` must be the risk if the fill lands on the limit, not on the close."""
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol, target_weight=0.3))
    assert ticket["status"] == "proposed", ticket.get("reasons")

    qty = Decimal(ticket["qty"])
    limit = Decimal(ticket["limit_price"])
    stop = Decimal(ticket["stop_price"])
    assert ticket["risk_basis"] == "worst_fill_at_limit"
    assert Decimal(ticket["est_risk_cny"]) == (qty * (limit - stop)).quantize(Decimal("0.01"))
    assert float(Decimal(ticket["est_risk_cny"])) <= 5000 * desk.guard.limits.max_trade_risk


def test_propose_remarks_prices_before_it_reads_risk(desk, monkeypatch):
    """The breakers read last_price; the write path must refresh it first."""
    seen = {}
    original = desk.book.mark_prices

    def spy(date=None, **kwargs):
        seen.update(kwargs, date=date)
        return original(date, **kwargs)

    monkeypatch.setattr(desk.book, "mark_prices", spy)
    desk.decisions.propose(proposal(pick_symbols(desk, 1)[0]))

    assert seen.get("live") is True, "a session price cannot come from yesterday's bar"


class _PoolRegistry:
    """Just enough registry for the readiness check: one pool row at a given date."""

    def __init__(self, dataset: str, cover_end: str):
        self._rows = [{"dataset": dataset, "cover_end": cover_end}]

    def summary(self):
        return self._rows


def test_aggressive_readiness_accepts_the_previous_session(desk, monkeypatch):
    """A pool that covers yesterday is current intraday: it is derived after the close."""
    import service.trading.guard as guard_mod

    date = "20261028"
    prev = desk.market.calendar.prev(date)
    assert prev
    monkeypatch.setattr(guard_mod, "build_brief",
                        lambda **kw: {"core": {"usable": True, "missing": []}})
    guard = guard_mod.Guard(desk.market, _PoolRegistry("limit_up_pool", prev),
                            guard_mod.RiskLimits())

    live = {c.name: c.ok for c in guard.data_checks(date, "AGGRESSIVE", live=True)}
    closed = {c.name: c.ok for c in guard.data_checks(date, "AGGRESSIVE", live=False)}

    assert live["data.aggressive"] is True, "intraday, yesterday's pool is the latest one"
    assert closed["data.aggressive"] is False, "after the close, today's pool is required"


def test_the_author_of_a_decision_is_recorded(desk):
    """A user-directed exit is a trade but not evidence about the model."""
    symbol = pick_symbols(desk, 1)[0]
    own = desk.decisions.propose(proposal(symbol))
    assert own["directed_by"] == "agent", "the default is the model's own judgement"

    directed = desk.decisions.propose(proposal(symbol, directed_by="user",
                                               intent_type="user_directive"))
    assert directed["directed_by"] == "user"


def test_arena_does_not_count_a_user_directed_case(desk):
    from service.trading.arena import _author_of

    symbol = pick_symbols(desk, 1)[0]
    mine = desk.decisions.propose(proposal(symbol))
    theirs = desk.decisions.propose(proposal(symbol, directed_by="user"))
    tickets = {t["ticket_id"]: t for t in desk.paper.list_tickets(limit=50)}

    def case(closing):
        return {"ticket_ids": ["td-open", closing["ticket_id"]]}

    assert _author_of(case(mine), tickets) == "agent"
    assert _author_of(case(theirs), tickets) == "user"
    assert _author_of({"ticket_ids": []}, tickets) == "unknown", "missing is not implied agent"


def test_readiness_says_which_pool_satisfied_it(desk, monkeypatch):
    """两者都能过闸门，但差 1.6 只／天。detail 必须写明用的是哪一个。

    以前两条路径共用一句 "limit-up pool covers X"，于是退回厂商池的那些天，
    读起来和用已审计派生池的那些天一模一样。
    """
    import service.trading.guard as guard_mod

    date = "20261028"
    prev = desk.market.calendar.prev(date)
    monkeypatch.setattr(guard_mod, "build_brief",
                        lambda **kw: {"core": {"usable": True, "missing": []}})

    audited = guard_mod.Guard(desk.market, _PoolRegistry("limit_pool_t0", prev),
                              guard_mod.RiskLimits())
    vendor = guard_mod.Guard(desk.market, _PoolRegistry("limit_up_pool", prev),
                             guard_mod.RiskLimits())

    def detail(g):
        return {c.name: c.detail for c in g.data_checks(date, "AGGRESSIVE", live=True)}

    assert "derived: audited" in detail(audited)["data.aggressive"]
    assert "vendor only" in detail(vendor)["data.aggressive"]


def test_the_ticket_states_what_the_round_trip_costs(desk):
    """票据上要有"这一笔来回付多少 bps"，而不是让读者拿两个 Decimal 心算。

    `est_fees` 是单边手续费：1,500 元的一笔是 5 元（最低佣金），看着很小。
    但来回要收两次，加上印花税与双边滑点，比例会变成 9 成——这个数在中枢里
    是"值不值得做"的那把尺子，必须在读者按下 approve 之前就在场。
    """
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))

    bps = ticket["round_trip_cost_bps"]
    one_side_cny = Decimal(ticket["est_fees"])
    notional = Decimal(ticket["est_value"])

    assert 0 < bps < 1000
    # 往返至少是两个单边佣金，所以比例上必须比"单边费用 / 名义额"更大
    assert bps >= float(one_side_cny * 2 / notional * 10_000) - 1e-6


def test_a_stock_whose_one_lot_breaks_the_cap_says_so(desk):
    """外行想买一手，撞上单票上限时不能只回一句"最多 0 股"。

    2026-11-02 实测：001219.SZ（21.93 元、6 连板）一手 2,193 元＝43.8% 权益，
    超过 30% 的单票上限，系统回的是「这只票最多 0 股」。0 在这里不是"不许买"，
    是"一手就超了"——同一句话的两种意思，不写清楚，人就停在那了。
    """
    day = desk.market.latest_bar_date()
    daily = desk.market.daily(day)
    basic = desk.market.basic().set_index("ts_code")
    equity = float(desk.book.summary()["equity"])
    cap = float(desk.guard.limits.max_core_single)

    pick = None
    for row in daily.itertuples():
        price = float(row.close)
        # 一手超上限、但整只票本身还买得起 —— 正是"最多 0 股"的那个区间
        if not (equity * cap < price * 100 <= equity):
            continue
        code = str(row.ts_code)
        if code.endswith(".BJ") or code not in basic.index:
            continue
        if "ST" in str(basic.loc[code, "name"]):
            continue
        pick = code
        break
    if pick is None:
        pytest.skip("今天没有落在这个区间的票")

    with pytest.raises(DecisionError) as raised:
        desk.decisions.propose(proposal(pick, qty=100, target_weight=None),
                               actor="test")
    message = str(raised.value)
    assert raised.value.code == "POSITION_CAP", message
    assert "一手" in message, f"必须说清是一手买不进，而不是只给一个 0：{message}"


# ── 缺列的带宽不能改写价格 ──────────────────────────────────────────────────
def test_a_missing_band_does_not_produce_a_zero_limit_price():
    """``snap.up_limit == 0`` 是"涨停列没有"，不是"涨停价 0 元"。

    无条件 ``min(up_limit, price*(1+buffer))`` 会把限价写成 0，票据随后被内核
    以 PRICE_LIMIT 拒绝——缺数据被记成了非法价格。
    """
    from types import SimpleNamespace

    from service.trading.decisions import DecisionService

    no_band = SimpleNamespace(up_limit=D("0"), down_limit=D("0"))
    limit = DecisionService._limit_price("BUY", D("10.00"), no_band, D("0.02"))
    assert limit == D("10.20")

    sell = DecisionService._limit_price("SELL", D("10.00"), no_band, D("0.02"))
    assert sell == D("9.80")


def test_a_real_band_still_caps_the_buffered_limit():
    from types import SimpleNamespace

    from service.trading.decisions import DecisionService

    band = SimpleNamespace(up_limit=D("10.05"), down_limit=D("9.95"))
    assert DecisionService._limit_price("BUY", D("10.00"), band, D("0.02")) == D("10.05")
    assert DecisionService._limit_price("SELL", D("10.00"), band, D("0.02")) == D("9.95")


# ------------------------------------------------------------ 重试与幂等句柄
def test_a_retry_with_the_same_request_id_returns_the_same_ticket(desk):
    """超时重提不能变成第二张票。

    服务端按意图指纹去重，而指纹把 ``thesis`` / ``evidence`` 的自由文本一起哈希：
    换个说法就是新指纹、新票据，同一笔钱被承诺两次。调用方句柄是唯一不受措辞影响的
    东西，所以重试必须原样带上它。
    """
    symbol = pick_symbols(desk, 1)[0]
    first = desk.decisions.propose(proposal(symbol, client_request_id="propose-abc12345"))

    again = desk.decisions.propose(proposal(
        symbol,
        client_request_id="propose-abc12345",
        thesis="重试时换了措辞、也少给了一条证据，但这仍然是同一个请求，不该生成第二张票。",
        evidence=["daily:latest"],
    ))

    assert again["ticket_id"] == first["ticket_id"]
    assert again["client_request_id"] == "propose-abc12345"
    pending = [t for t in desk.paper.list_tickets(limit=50) if t["status"] == "proposed"]
    assert len(pending) == 1, "同一请求只应有一张待批票据"


def test_a_reworded_retry_without_the_handle_is_a_second_ticket(desk):
    """反证：没有句柄时，措辞一变就是第二张票——这就是要修的那个入口。"""
    symbol = pick_symbols(desk, 1)[0]
    first = desk.decisions.propose(proposal(symbol))
    second = desk.decisions.propose(proposal(
        symbol, thesis="同一笔交易，换了一套措辞重新提交，指纹因此不同，于是有了第二张票。"))

    assert second["ticket_id"] != first["ticket_id"]


def test_a_refused_proposal_returns_the_same_refusal_on_retry(desk):
    """拒绝也是一个答案：重试应当拿到那张拒绝票，而不是第二张拒绝票。

    `preflight` 拒绝（而不是抛 DecisionError）才有票据，ST 买入是这种路径。
    """
    symbol = pick_st_symbol(desk)
    if symbol is None:
        pytest.skip("no active ST name in the dataset")

    body = proposal(symbol, sleeve="CORE", target_weight=0.2,
                    client_request_id="propose-bad00001")
    first = desk.decisions.propose(body)
    assert first["status"] == "rejected"

    again = desk.decisions.propose(dict(
        body, thesis="换一套措辞重试一笔已经被风控拒绝的买入，应该还是同一张拒绝票。"))
    assert again["ticket_id"] == first["ticket_id"]


# ── A quantity is a whole number of shares, or it is a refusal ──────────────
# ``_size_buy`` called ``int(requested_qty)``: 100.7 became 100, silently, and
# ``10**400`` reached ``float(qty)`` and left ``/decision/propose`` as a 500.
# Both are now the same BAD_SIZE the order manager has always returned.


def test_a_fractional_buy_quantity_is_refused_not_truncated(desk):
    symbol = pick_symbols(desk, 1)[0]
    with pytest.raises(DecisionError) as err:
        desk.decisions.propose(proposal(symbol, qty=100.7, target_weight=None))
    assert err.value.code == "BAD_SIZE"
    assert "100.7" in str(err.value) and "整数" in str(err.value)


def test_a_quantity_no_company_could_issue_is_a_refusal_not_a_500(desk):
    symbol = pick_symbols(desk, 1)[0]
    with pytest.raises(DecisionError) as err:
        desk.decisions.propose(proposal(symbol, qty=10 ** 400, target_weight=None))
    assert err.value.code == "BAD_SIZE"


def test_a_fractional_sell_quantity_is_refused(desk):
    """Sizing runs before the position lookup, so no holding is needed here."""
    symbol = pick_symbols(desk, 1)[0]
    with pytest.raises(DecisionError) as err:
        desk.decisions._size_sell({"qty": 100.5}, symbol)
    assert err.value.code == "BAD_SIZE"
    assert "100.5" in str(err.value)


def test_the_pricing_gate_asks_the_feed_that_carries_the_symbol(desk, monkeypatch):
    """A bond is priced by ``cb_daily``, not by the stock calendar.

    The feeds land a few minutes apart on the same evening, so "the latest bar"
    is four dates, not one. While the gate read ``daily`` for every instrument,
    a bond whose own feed was current got refused whenever the stock file was a
    session ahead — with a message that could not be true ("20261104 的日线还
    没发布（最新 20261105）").
    """
    today = pin_to_last_bar(desk)
    yesterday = desk.market.calendar.prev(today)
    monkeypatch.setattr(desk.market, "dataset_cover_end", lambda _dataset: yesterday)

    with pytest.raises(DecisionError) as bond:
        desk.decisions._date("110081.SH")
    assert bond.value.code == "DATA_NOT_READY"
    assert "cb_daily" in str(bond.value)

    with pytest.raises(DecisionError) as stock:
        desk.decisions._date("600000.SH")
    assert "daily" in str(stock.value)
    assert "cb_daily" not in str(stock.value)


def test_a_feed_ahead_of_the_clock_is_not_reported_as_a_missing_bar(desk, monkeypatch):
    """``bar > today`` means the clock is behind, not that today's bar is gone.

    Comparing with ``==`` fell through to the same "还没有发布" refusal, whose
    "latest" was *later* than the date it claimed was missing. An NTP correction
    (or a partition written with tomorrow's session) is enough to produce it.
    """
    today = pin_to_last_bar(desk)
    monkeypatch.setattr(desk.market, "dataset_cover_end",
                        lambda _dataset: desk.market.calendar.next(today))

    assert desk.decisions._date("600000.SH") == today


def test_expired_shadow_intent_is_not_an_open_ticket_but_stays_auditable(desk):
    """B1: an armed ticket with expired_untriggered must not pollute `open`."""
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))
    ticket["status"] = "armed"
    ticket["intent_terminal_state"] = "expired_untriggered"
    ticket["intent_terminal_at"] = "2026-11-10T14:55:00+08:00"
    desk.decisions.store.save_ticket(ticket)

    open_ids = [t["ticket_id"] for t in
                desk.decisions.list_tickets(limit=50, status="open")]
    assert ticket["ticket_id"] not in open_ids
    # The record is not deleted or rewritten: it stays readable for audit.
    armed_ids = [t["ticket_id"] for t in
                 desk.decisions.list_tickets(limit=50, status="armed")]
    all_ids = [t["ticket_id"] for t in desk.decisions.list_tickets(limit=50)]
    assert ticket["ticket_id"] in armed_ids
    assert ticket["ticket_id"] in all_ids


def test_denied_shadow_intent_stays_in_open(desk):
    """`denied` can recover on a later frame; only expiry retires the intent."""
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))
    ticket["status"] = "armed"
    ticket["intent_terminal_state"] = "denied"
    desk.decisions.store.save_ticket(ticket)

    open_ids = [t["ticket_id"] for t in
                desk.decisions.list_tickets(limit=50, status="open")]
    assert ticket["ticket_id"] in open_ids


def test_a_plain_proposed_ticket_is_still_open(desk):
    symbol = pick_symbols(desk, 1)[0]
    ticket = desk.decisions.propose(proposal(symbol))
    open_ids = [t["ticket_id"] for t in
                desk.decisions.list_tickets(limit=50, status="open")]
    assert ticket["ticket_id"] in open_ids
