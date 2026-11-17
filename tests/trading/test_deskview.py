"""「游资袖不可用」这句话，得说清楚是哪一种不可用。"""

from __future__ import annotations

import threading
import time

from service.trading import deskview


def _probe(**rt):
    base = {"ok": True, "tradable": False, "feed_available": True,
            "in_session": False, "session_label": "盘前/盘后"}
    return {"rt_quote": {**base, **rt}}


def test_a_closed_market_says_so_instead_of_naming_a_variable():
    text = deskview._blocked_text({"aggressive": {"missing": ["rt_quote"]}},
                                  _probe(tradable=False, feed_available=True))
    assert text and "盘前/盘后" in text[0]
    assert "rt_quote" not in text[0]
    assert "开盘后自动恢复" in text[0]


def test_a_dead_feed_is_not_described_as_a_closed_market():
    text = deskview._blocked_text({"aggressive": {"missing": ["rt_quote"]}},
                                  _probe(tradable=False, feed_available=False))
    assert "行情源不可用" in text[0]


def test_a_tradable_quote_needs_no_explanation():
    assert deskview._blocked_text({"aggressive": {"missing": []}},
                                  _probe(tradable=True)) == []


def test_missing_datasets_are_named_in_chinese():
    text = deskview._blocked_text({"aggressive": {"missing": ["limit_up_pool"]}},
                                  _probe(tradable=True))
    assert text == ["涨停池落后或缺失"]


def test_non_finite_numbers_become_null_and_are_reported():
    import json
    from decimal import Decimal

    quality = []
    value = deskview._plain({"x": [float("nan"), Decimal("Infinity"), 1.5]},
                            quality, "paper")
    assert value == {"x": [None, None, 1.5]}
    assert quality == ["paper.x[0]", "paper.x[1]"]
    json.dumps(value, allow_nan=False)
    assert deskview.plain(float("-inf")) is None


def test_system_cache_key_contains_desk_and_database_identity(tmp_path):
    from types import SimpleNamespace

    def desk(name):
        return SimpleNamespace(paper=SimpleNamespace(db_path=tmp_path / f"{name}-paper.db"),
                               registry=SimpleNamespace(db_path=tmp_path / "registry.db"))

    first, second = desk("a"), desk("b")
    assert deskview._cache_key(first, "20261027", tmp_path) != deskview._cache_key(
        second, "20261027", tmp_path)


def test_a_wall_clock_jump_does_not_expire_the_system_cache(desk, monkeypatch):
    """TTL 量的是经过的秒数，不是日历。

    用 ``time.time()`` 做差时，一次 NTP 校正（或手工改系统时间）会让一份刚装好的
    payload 看起来老了一小时，于是窗口白重建一次 7-8 秒；反方向的跳变则让旧数据
    永远不过期。``monotonic`` 两头都不受影响。
    """
    day = "20261105"
    key = deskview._cache_key(desk, day, deskview.ROOT)
    saved = dict(deskview._SYSTEM_CACHE)
    deskview._SYSTEM_CACHE.clear()
    try:
        deskview._SYSTEM_CACHE[key] = (time.monotonic(), {"cached": True})
        jump = time.time() + 3600
        # `deskview.time` 就是 time 模块本身，这一改在进程内全局生效，monkeypatch
        # 会在用例结束时还原；lambda 里不能再调 time.time()，会自己调自己。
        monkeypatch.setattr(deskview.time, "time", lambda: jump)
        assert deskview.system(desk, day, root=deskview.ROOT) == {"cached": True}
    finally:
        deskview._SYSTEM_CACHE.clear()
        deskview._SYSTEM_CACHE.update(saved)


def test_a_cache_hit_takes_the_lock_before_touching_the_dict(desk):
    """窗口是 webview，它的 JS API 调用跑在线程里：刷新按钮与 60 秒自动刷新会撞。

    ``sorted(_SYSTEM_CACHE)`` 在另一个线程插入时抛 "dictionary changed size during
    iteration"。这里不制造那个概率窗口，只钉住"碰字典之前先拿锁"。
    """
    day = "20261105"
    key = deskview._cache_key(desk, day, deskview.ROOT)
    saved = dict(deskview._SYSTEM_CACHE)
    deskview._SYSTEM_CACHE.clear()
    deskview._SYSTEM_CACHE[key] = (time.monotonic(), {"cached": True})
    done = threading.Event()
    try:
        with deskview._SYSTEM_LOCK:
            thread = threading.Thread(
                target=lambda: (deskview.system(desk, day, root=deskview.ROOT),
                                done.set()),
                daemon=True)
            thread.start()
            assert not done.wait(0.2), "缓存命中也不能在锁外读字典"
        assert done.wait(5)
    finally:
        deskview._SYSTEM_CACHE.clear()
        deskview._SYSTEM_CACHE.update(saved)


def test_a_working_quote_does_not_block_the_sleeve(desk, monkeypatch):
    """15:01 那天窗口写着「游资袖不可用：rt_quote」，而行情源当时是通的。

    原因是 `build_brief(rt_age_seconds=None)`：None 的意思是"完全没有报价"，
    传它等于永远宣称报价缺失。
    """
    from service.trading import persona

    base = persona.probe(desk.market, desk.registry)
    monkeypatch.setattr(
        persona, "probe",
        lambda *_a, **_k: {**base,
                           "rt_quote": {**base["rt_quote"], "tradable": True,
                                        "feed_available": True, "in_session": True}})
    payload = deskview.system(desk, deskview.day_of(desk), ttl=0)
    assert "rt_quote" not in payload["brief"]["aggressive"]["missing"]
    assert "rt_quote" not in payload["aggressive_blocked"]


def test_no_bar_today_but_a_live_quote_is_not_a_prohibition(desk, monkeypatch):
    """「今天没有日线」和「今天不能下单」是两件事。

    待办里那句「此刻禁止下单」让 agent 整个交易日都在等数据，而
    `DecisionService._date()` 早就能用实时报价定价了。
    """
    from service.trading import duty, persona

    monkeypatch.setattr(persona, "probe", lambda *_a, **_k: {
        "rt_quote": {"tradable": True, "feed_available": True, "in_session": True,
                     "session_label": "连续竞价"}})
    payload = duty.build(desk, day="20261026", today="20261027")
    item = next(i for i in payload["items"] if i["kind"] == "intraday_pricing")
    # 位置不再断言：会挡住下单的条目现在排在最前（它们不自愈），
    # 而"可以开仓"这条恰好不挡任何东西。按 kind 找，不按下标找。
    # 用 item["blocks"] 而不是 .get()：后者在字段缺失时也返回 None，
    # 于是"声明了不挡"和"忘了声明"分不开——2026-11-04 就是这样漏掉一条的。
    assert item["blocks"] is None, "能开仓的提示不是阻塞项"
    assert "禁止下单" not in item["detail"]
    assert "可以开仓" in item["detail"]
    # note 现在要解释两个字段的分工，不再是"priority 0 最高"那句话。
    assert "blocks" in payload["note"] and "priority" in payload["note"]
    assert "不自愈" in payload["note"]


def test_stressed_is_a_state_not_a_gate(desk, monkeypatch):
    """guard 从来没有因为 stressed 拒过单；写成硬规则等于凭空少一个策略。

    It is also not advice: the note states the label and the rule that applies,
    and stops there. "宜减仓" was there until the purity audit (P44 §2.3).
    """
    from service.trading import duty

    payload = duty.build(desk, day="20261026", today="20261027")
    notes = [item for item in payload["items"] if item["kind"] == "regime_note"]
    for item in notes:
        assert "不是准入条件" in item["detail"]
        assert "不会因为 stressed 拒单" in item["detail"]


def test_every_duty_item_declares_whether_it_blocks(desk):
    """`blocks` 是 `duty` 里唯一的结构化约束，不能有条目忘了它。

    漏一条的后果不是崩溃，是**读的人按字段判断时把那一条当成"不挡"**。
    2026-11-04 实测 `intraday_pricing` 就没有这个键，而它恰好是"现在可以开仓"
    那条——最不该被误读的一条。原来的测试写成 `item.get("blocks")`，
    `.get()` 对缺失返回 None，于是"声明了不挡"和"没声明"看起来一样。
    """
    from service.trading import duty

    payload = duty.build(desk, day=desk.market.latest_bar_date(),
                         today=desk.market.latest_bar_date())

    for item in payload["items"]:
        assert "blocks" in item, f"{item['kind']} 没声明 blocks"
        assert item["blocks"] in (None, "all_new_entries", "entries_in_subject"), \
            f"{item['kind']} 的 blocks 取值不在约定内：{item['blocks']}"
        assert isinstance(item["priority"], int), f"{item['kind']} 的 priority 不是整数"


def test_a_handoff_that_is_not_due_yet_is_not_a_gap(desk, tmp_path):
    """08:00 的队列不该抱怨 19:30 那份复盘还没写。

    这个检查最容易写成"永远在响"：只要不看时钟，"今天没有 close 报告"从
    00:01 就成立。永远响的告警等于没有告警——同一个教训在 `quality.check_quotes`
    里已经踩过一次。
    """
    from service.trading import duty

    today = "20261027"
    assert desk.market.calendar.is_trading_day(today)

    payload = duty.build(desk, day="20261026", today=today,
                         reports_root=tmp_path, now_hm="08:00")
    assert not [i for i in payload["items"] if i["kind"] == "upstream_missing"]


def test_a_chain_that_did_not_run_names_every_missing_handoff(desk, tmp_path):
    """上游没跑成，要在下游读它的那一刻被点名，而不是靠人想起来去看。"""
    from service.trading import duty

    today = "20261027"
    prev = desk.market.calendar.prev(today)

    payload = duty.build(desk, day="20261026", today=today,
                         reports_root=tmp_path, now_hm="20:00")
    gaps = {i["artifact"] for i in payload["items"] if i["kind"] == "upstream_missing"}

    assert gaps == {
        f"reports/review/{prev}.md",
        f"reports/market/{prev}.md",
        f"reports/open/{today}.md",
        f"reports/noon/{today}.md",
        f"reports/close/{today}.md",
        f"reports/market/{today}.md",
    }
    # 每一条都要说清"谁在等它"，否则读的人只知道缺文件，不知道缺了哪一步。
    for item in payload["items"]:
        if item["kind"] == "upstream_missing":
            assert item["due_by"] in item["detail"]
            assert "§5" in item["detail"], "没指向已定好的降级动作"


def test_reports_on_disk_turn_the_gap_off(desk, tmp_path):
    """文件在就不该有这条待办——否则它就成了"每天都有"的噪音。"""
    from service.trading import duty

    today = "20261027"
    prev = desk.market.calendar.prev(today)
    for folder, day in (("review", prev), ("market", prev), ("open", today),
                        ("noon", today), ("close", today), ("market", today)):
        path = tmp_path / folder / f"{day}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# ok\n", encoding="utf-8")

    payload = duty.build(desk, day="20261026", today=today,
                         reports_root=tmp_path, now_hm="20:00")
    assert not [i for i in payload["items"] if i["kind"] == "upstream_missing"]


def test_a_holiday_has_no_chain_to_be_missing(desk, tmp_path):
    """非交易日没有"谁该跑过"这回事。"""
    from service.trading import duty

    closed = next(d for d in ("20261024", "20261025", "20261031", "20261101")
                  if not desk.market.calendar.is_trading_day(d))
    payload = duty.build(desk, day="20261026", today=closed,
                         reports_root=tmp_path, now_hm="20:00")
    assert not [i for i in payload["items"] if i["kind"] == "upstream_missing"]
