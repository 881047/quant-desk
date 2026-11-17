"""The handoff: rules write the question, the session writes the answer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from service.kernel.clock import SHANGHAI
from service.notify.mail import MailDeliveryUnknown
from service.trading import analyst, monitor


def _alert_and_context():
    ctx = monitor.PositionContext(symbol="000700.SZ", qty=100, cost=13.12, entry=13.07,
                                  prev_close=13.07, last=12.30)
    return monitor.evaluate([ctx], day="20261027"), [ctx]


def test_handoff_is_pending_and_carries_the_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(analyst, "HANDOFF", tmp_path)
    alerts, contexts = _alert_and_context()
    prompt = analyst.build_prompt(alerts, contexts)
    path = analyst.write_handoff(alerts, contexts, prompt, source="monitor")

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["status"] == "pending", "未回答的交接必须能被 pending 找到"
    assert doc["prompt"].startswith("你是持仓提醒的解读段")
    assert doc["alerts"] and doc["positions"]
    assert doc["positions"][0]["stop"] == 12.42


def test_prompt_forbids_inventing_numbers():
    alerts, contexts = _alert_and_context()
    prompt = analyst.build_prompt(alerts, contexts)
    assert "不得引入任何未出现的价格" in prompt
    assert "不要给买卖建议" in prompt


def test_answer_mail_body_keeps_facts_and_analysis_apart():
    from scripts.handoff import _render_text

    alerts, contexts = _alert_and_context()
    doc = {
        "created_at": "2026-10-27T09:30:00+08:00",
        "alerts": [{"severity": a.severity, "title": a.title, "facts": a.facts}
                   for a in alerts],
        "positions": [{"symbol": c.symbol, "qty": c.qty, "last": c.last,
                       "pct": round(c.pct * 100, 2), "cost": c.cost, "pnl": c.pnl,
                       "stop": c.stop, "auction_line": c.auction_line} for c in contexts],
    }
    text = _render_text(doc, "这是解读的正文。")
    assert "这是解读的正文。" in text
    assert "跌破止损" in text
    assert "不给买卖建议" in text


def test_answering_a_missing_id_fails_loudly(capsys):
    from scripts.handoff import main

    assert main(["show", "nope-000000"]) == 2
    assert "没有" in capsys.readouterr().out


def test_the_default_backend_leaves_the_question_for_the_workspace():
    """默认不生成解读：规则写完问题就停，解读发生在 quant-desk 工作区。

    以前默认是 ``harness`` → ``llm``：要么为一个三句话的包装起一整个 agent turn，
    要么偷偷换一个从没见过这张 desk 的模型。
    """
    alerts, contexts = _alert_and_context()
    text, reason = analyst.note(alerts, contexts)
    assert text is None
    assert reason.startswith("handoff"), reason


def test_an_explicit_backend_still_reports_why_it_failed(monkeypatch):
    alerts, contexts = _alert_and_context()
    monkeypatch.setattr(analyst, "_via_llm", lambda p, t: (None, "no key"))
    text, reason = analyst.note(alerts, contexts, prefer="llm")
    assert text is None and "no key" in reason
    # The removed harness backend must say so rather than silently doing nothing.
    text, reason = analyst.note(alerts, contexts, prefer="harness")
    assert text is None and "未知解读后端" in reason


# ------------------------------------------------- 问题卡：小到能一次答完
def test_the_prompt_numbers_its_facts_so_the_answer_can_cite_them():
    alerts, contexts = _alert_and_context()
    prompt = analyst.build_prompt(alerts, contexts)
    assert "触发项：" in prompt
    assert "1. [alert]" in prompt
    assert "引用 计划：" in prompt, "带出处的引用必须出现在问题里"
    assert "每句都必须引用上面出现过的编号或数字" in prompt
    assert "不要用常识补齐" in prompt


def test_the_prompt_stays_small_enough_for_a_cheap_model():
    """问题卡的大小是设计约束：需要多轮工具调用才能答的问题，小模型一定答坏。

    上限按**最长的那一种**测——游资人格多两行涨停结构，那才是 2000 字节真正
    在约束的形状。只测最矮的那种，等于没有测。
    """
    ctx = monitor.PositionContext(
        symbol="000700.SZ", qty=100, cost=13.12, entry=13.07,
        prev_close=13.07, last=12.30, amount=3.2e8, adv20=1.1e8,
        adv20_unit="cny", ma5=12.9)
    alerts = monitor.evaluate([ctx], day="20261027")
    # 最坏形状：游资（多两行涨停结构）+ 中档（多一行档位把握）。
    # 实测这一版是 1,981 字节——离上限只剩 19 字节，所以这个上限还有牙齿：
    # 下一次想往卡上加东西，必须先决定拿掉什么。
    biggest = {
        "市场状态": "low|low（宽度分位 16%、20 日收益分位 10%）",
        "该状态之后 1 日": "p25 -0.9% / p50 +0.3% / p75 +1.3%，胜率 58%（n=1343，205 段，较平均日 +0.4%）",
        "涨停结构": "封板率 67%（34% 分位）·涨停 59 家（26% 分位）",
        "档位把握": "封板率处中档边界（与上游同桶 63%），两侧都可能翻",
        "之后两日": ("买当天封住的票、隔日收盘卖 — 近 3 年段均 +0.1%[-0.7%~+1.0%]（56 段）；"
                     "全历史 +0.9%，其中 2015-19 贡献 88%（该段 +2.3%），未扣成本"),
        "口径": "分位与分布截至 20261026；前向收益自次日开盘计",
    }
    prompt = analyst.build_prompt(alerts, [ctx], background=biggest)
    assert len(prompt.encode("utf-8")) < 2000, len(prompt.encode("utf-8"))


def test_the_background_block_is_optional_and_labelled_as_calibration_only():
    alerts, contexts = _alert_and_context()
    without = analyst.build_prompt(alerts, contexts)
    with_bg = analyst.build_prompt(alerts, contexts, background={"状态": "low|low"})
    assert "背景" not in without
    assert "不可据此推导新结论" in with_bg
    assert "low|low" in with_bg


# ------------------------------------------------------- 工作区侧的读写
def test_answer_writes_the_reading_and_sends_once(monkeypatch, tmp_path):
    from service.trading import handoff as handoff_mod

    monkeypatch.setattr(analyst, "HANDOFF", tmp_path)
    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)
    sent = {}
    monkeypatch.setattr(handoff_mod, "send_mail",
                        lambda subject, text, html, **_: sent.update(
                            subject=subject, text=text, html=html) or {"to": "me"})

    alerts, contexts = _alert_and_context()
    path = analyst.write_handoff(alerts, contexts,
                                 analyst.build_prompt(alerts, contexts),
                                 source="monitor")
    hid = path.stem
    assert len(handoff_mod.pending()) == 1

    result = handoff_mod.answer(hid, "第一句引用第 1 项；第二句 12.42；第三句确认成交额。")
    assert result["sent"] is True and sent["subject"].startswith("[持仓解读]")
    assert "第一句引用第 1 项" in sent["text"]
    assert handoff_mod.pending() == [], "答过之后不能再出现在待办里"

    doc = handoff_mod.load(hid)
    assert doc["status"] == "answered" and doc["analysis"].startswith("第一句")


def test_answering_nothing_is_an_error_not_an_empty_mail(monkeypatch, tmp_path):
    from service.trading import handoff as handoff_mod

    monkeypatch.setattr(analyst, "HANDOFF", tmp_path)
    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)
    alerts, contexts = _alert_and_context()
    path = analyst.write_handoff(alerts, contexts,
                                 analyst.build_prompt(alerts, contexts))
    try:
        handoff_mod.answer(path.stem, "   ")
    except ValueError as exc:
        assert "空" in str(exc)
    else:
        raise AssertionError("空解读必须被拒绝")
    assert len(handoff_mod.pending()) == 1, "拒绝之后仍然是待解读"


# --------------------------------------------------- 背景块：可缺，不可坏
def _canned_block(*horizons):
    """一个最小但完整的 block() 返回值：格式化要的字段一个不少。"""
    def dist(n, p25, p50, p75, win):
        return {"n": n, "mean": p50, "p25": p25, "p50": p50, "p75": p75,
                "win": win, "worst": -0.3, "best": 0.4}

    full = {1: dist(1343, -0.009, 0.003, 0.013, 0.59),
            2: dist(1342, -0.014, 0.004, 0.021, 0.57),
            5: dist(1339, -0.021, 0.008, 0.034, 0.57),
            20: dist(1329, -0.032, 0.032, 0.094, 0.65)}
    return {
        "ok": True, "as_of": "20261027", "bucket": "low|low",
        "state": {"breadth20": {"pct": 0.03}, "ew_ret_20d": {"pct": 0.01}},
        "horizons": list(horizons),
        "history": {"n": 1343, "episodes": 205, "usable": True,
                    **{f"fwd_{h}d": full[h] for h in horizons},
                    **{f"edge_{h}d": 0.003 for h in horizons}},
    }


def test_the_background_block_is_small_and_says_what_it_is(monkeypatch):
    import importlib
    import json

    from service.research import state as state_mod

    mod = importlib.import_module("scripts.monitor")
    canned = _canned_block(5, 20)
    monkeypatch.setattr(state_mod, "block",
                        lambda *a, **k: {**canned, "horizons": list(k.get("horizons") or (5, 20)),
                                         "history": {**canned["history"]}})
    monkeypatch.setattr(state_mod, "cached_state", lambda *a, **k: object())

    from types import SimpleNamespace

    bg = mod._state_background(SimpleNamespace(data=None), "20261027")
    assert bg["市场状态"].startswith("low|low（宽度分位 3%")
    assert "205 段" in bg["该状态之后 20 日"]
    assert bg["口径"].endswith("前向收益自次日开盘计")
    # 预算不是 400 而是 460：加上「较平均日」之后每行长约 20 字节，而那一项是
    # P28 学到的：桶的绝对收益不是 edge，必须和平均日比。真正的上限在整张卡上。
    assert len(json.dumps(bg, ensure_ascii=False).encode()) < 460, "背景块也要小"


def test_the_background_window_follows_the_persona(monkeypatch):
    """游资问的是 1-2 日，散户问的是 5-20 日——同一天，不该看同一份分布。"""
    import importlib

    from service.research import state as state_mod

    mod = importlib.import_module("scripts.monitor")

    def fake_block(_series, _day=None, horizons=None, **_kw):
        wanted = tuple(horizons or (5, 20))
        base = _canned_block(*wanted)
        base["horizons"] = list(wanted)
        base["history"] = {"n": 1343, "episodes": 205, "usable": True,
                           **{f"fwd_{h}d": _canned_block(h)["history"][f"fwd_{h}d"]
                              for h in wanted},
                           **{f"edge_{h}d": 0.003 for h in wanted}}
        return base

    monkeypatch.setattr(state_mod, "block", fake_block)
    monkeypatch.setattr(state_mod, "cached_state", lambda *a, **k: object())
    from types import SimpleNamespace

    desk = SimpleNamespace(data=None)
    hot = mod._state_background(desk, "20261027", persona="hot_money")
    retail = mod._state_background(desk, "20261027", persona="retail")

    assert "该状态之后 1 日" in hot and "该状态之后 20 日" not in hot
    assert "该状态之后 20 日" in retail and "该状态之后 1 日" not in retail
    # 游资只留等权 1 日那一行：它的第二行是「买封住的票、两日后卖」，
    # 那比等权 2 日更贴它的持有期，也更贴它真正会做的交易。两张 2 日表会互相稀释。
    assert mod.PERSONA_HORIZONS["hot_money"] == (1,)


def _tape_block(**over):
    base = {
        "ok": True, "as_of": "20261027", "bucket": "low|low",
        "seal_rate": 0.5946, "seal_pct": 0.1155, "up_count": 44, "up_pct": 0.0956,
        "n": 684, "episodes": 504, "usable": True,
        "fwd_seal2d": {"n": 684, "mean": 0.0095, "p25": -0.0103, "p50": 0.0072,
                       "p75": 0.03, "win": 0.618},
        "recent": {
            "sessions": 756, "since": "20230907", "episodes": 93, "usable": True,
            "fwd": {"n": 115, "mean": -0.0026, "p25": -0.0211, "p50": 0.0017,
                    "p75": 0.016, "win": 0.53},
            "episode_ci": {"n": 93, "mean": -0.0008,
                           "ci_low": -0.0062, "ci_high": 0.0044},
            "baseline": {"mean": -0.0009, "win": 0.499},
        },
        "by_era": [{"era": "2024-今", "n": 102, "mean": -0.0028, "win": 0.52},
                   {"era": "2020-23", "n": 198, "mean": 0.0026, "win": 0.591},
                   {"era": "2015-19", "n": 201, "mean": 0.0247, "win": 0.726}],
        "era_dominant": {"era": "2015-19", "n": 201, "mean": 0.0247,
                         "share": 0.758, "total_mean": 0.0096},
        "baseline": {"mean": 0.0074, "p50": 0.0071, "win": 0.605, "n": 4079},
        "forward": "次日开盘买当天封住的票、第三日收盘卖（T+1 可执行）",
    }
    base.update(over)
    return base


def test_the_tape_block_appears_only_for_hot_money(monkeypatch):
    import importlib

    from service.research import state as state_mod
    from service.research import tape_state

    mod = importlib.import_module("scripts.monitor")
    monkeypatch.setattr(state_mod, "block", lambda *a, **k: _canned_block(1))
    monkeypatch.setattr(state_mod, "cached_state", lambda *a, **k: object())
    monkeypatch.setattr(tape_state, "cached_tape", lambda *a, **k: object())
    monkeypatch.setattr(tape_state, "block", lambda *a, **k: _tape_block())
    from types import SimpleNamespace

    desk = SimpleNamespace(data=None, market=SimpleNamespace(latest_bar_date=lambda: None))
    hot = mod._state_background(desk, "20261027", persona="hot_money")
    retail = mod._state_background(desk, "20261027", persona="retail")

    assert "封板率 59%（12% 分位）" in hot["涨停结构"]
    assert "涨停 44 家（10% 分位）" in hot["涨停结构"]
    # 近端报的是**段均 + 区间**，不是日均点估计：low|low 近三年日均 −0.28%、
    # 段均 −0.08%，两个区间都横跨零。点估计会被读成一个方向。
    assert "近 3 年段均 -0.1%" in hot["之后两日"], "近期那个数必须排在前面"
    assert "[-0.6%~+0.4%]" in hot["之后两日"], "点估计必须带区间"
    assert "2015-19 贡献 76%" in hot["之后两日"], "全历史均值要说清是哪一段挣的"
    assert "未扣成本" in hot["之后两日"], "不写这句就是在暗示净收益"
    assert "涨停结构" not in retail


def test_a_thin_tape_sample_says_so_instead_of_showing_a_distribution(monkeypatch):
    """样本不足说的是**那个窗口**，不是整条序列——近端不够就不能拿全历史顶上。"""
    import importlib

    from service.research import state as state_mod
    from service.research import tape_state

    mod = importlib.import_module("scripts.monitor")
    monkeypatch.setattr(state_mod, "block", lambda *a, **k: _canned_block(1))
    monkeypatch.setattr(state_mod, "cached_state", lambda *a, **k: object())
    monkeypatch.setattr(tape_state, "cached_tape", lambda *a, **k: object())
    thin = _tape_block(
        recent={"sessions": 756, "since": "20230907", "episodes": 5, "usable": False,
                "fwd": {"n": 12, "mean": 0.02, "p25": -0.03, "p50": 0.01,
                        "p75": 0.05, "win": 0.6},
                "baseline": {"mean": 0.001, "win": 0.5}})
    monkeypatch.setattr(tape_state, "block", lambda *a, **k: thin)
    from types import SimpleNamespace

    desk = SimpleNamespace(data=None, market=SimpleNamespace(latest_bar_date=lambda: None))
    hot = mod._state_background(desk, "20261027", persona="hot_money")
    assert "近 3 年样本不足" in hot["之后两日"]
    assert "近 3 年 +2.0%" not in hot["之后两日"], "12 天不能当成一个近端分布"

    ok = _tape_block()
    monkeypatch.setattr(tape_state, "block", lambda *a, **k: ok)
    assert "近 3 年段均" in mod._state_background(
        desk, "20261027", persona="hot_money")["之后两日"]


def test_a_broken_tape_series_falls_back_to_the_vendor_position(monkeypatch):
    """推导线拿不到时只报位置，并说明为什么没有后半句——不是静默省略。"""
    import importlib

    from service.research import state as state_mod
    from service.research import tape_state
    from service.trading import tape

    mod = importlib.import_module("scripts.monitor")
    monkeypatch.setattr(state_mod, "block", lambda *a, **k: _canned_block(1))
    monkeypatch.setattr(state_mod, "cached_state", lambda *a, **k: object())
    monkeypatch.setattr(tape_state, "cached_tape",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no data")))
    monkeypatch.setattr(tape, "limit_history", lambda *a, **k: {
        "ok": True,
        "limit_up": {"today": 42, "percentile": 0.26, "sessions": 1430},
        "seal_rate": {"today": 0.594, "percentile": 0.066, "sessions": 243},
    })
    from types import SimpleNamespace

    desk = SimpleNamespace(data=None, market=SimpleNamespace(latest_bar_date=lambda: None))
    hot = mod._state_background(desk, "20261027", persona="hot_money")
    assert "涨停 42 家" in hot["涨停结构"]
    assert "不报之后会怎样" in hot["之后两日"]


def test_an_unreadable_persona_falls_back_to_the_shorter_window():
    import importlib
    from types import SimpleNamespace

    mod = importlib.import_module("scripts.monitor")

    class Broken:
        @property
        def paper(self):
            raise RuntimeError("no db")

    assert mod._persona(Broken()) == "retail"
    assert mod._persona(SimpleNamespace(
        paper=SimpleNamespace(persona_current=lambda: {"current": None}))) == "retail"


def test_a_broken_state_series_never_blocks_the_alert(monkeypatch):
    """背景取不到就不写背景——一条止损提醒不该因为缓存被锁住而发不出去。"""
    import importlib

    from service.research import state as state_mod

    mod = importlib.import_module("scripts.monitor")

    def boom(*args, **kwargs):
        raise RuntimeError("cache locked")

    monkeypatch.setattr(state_mod, "cached_state", boom)
    from types import SimpleNamespace

    assert mod._state_background(SimpleNamespace(data=None), "20261027") == {}


def test_the_handoff_carries_the_background_into_the_question(monkeypatch, tmp_path):
    """接线检查：monitor 脚本取到的背景必须真的进到那段 prompt 里。"""
    import importlib

    mod = importlib.import_module("scripts.monitor")
    monkeypatch.setattr(analyst, "HANDOFF", tmp_path)

    alerts, contexts = _alert_and_context()
    note, source, path = mod._interpret(
        alerts, contexts, background={"市场状态": "low|low（宽度分位 3%）"})

    # 第一封邮件带一句占位说明（不是 None）：告诉读者解读随后补发。
    assert note and "本次只发事实" in note
    assert source.startswith("handoff")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "背景" in doc["prompt"]
    assert "low|low" in doc["prompt"]
    assert "不可据此推导新结论" in doc["prompt"]


def test_a_mid_bucket_carries_a_warning_and_a_tail_bucket_does_not(monkeypatch):
    """中档与尾档差 30 个百分点：尾档 95% 同桶、中档 63%。

    不平铺这句，是因为尾档的日子不该为它付字节，也不该被它稀释成「都不太准」。
    """
    import importlib
    from types import SimpleNamespace

    from service.research import state as state_mod
    from service.research import tape_state

    mod = importlib.import_module("scripts.monitor")
    monkeypatch.setattr(state_mod, "block", lambda *a, **k: _canned_block(1))
    monkeypatch.setattr(state_mod, "cached_state", lambda *a, **k: object())
    monkeypatch.setattr(tape_state, "cached_tape", lambda *a, **k: object())

    calib = {"ok": True, "window": 120,
             "seal": {"n": 124, "agree": 0.855, "far": 0.0,
                      "tail_agree": 0.953, "mid_agree": 0.632}}
    monkeypatch.setattr(tape_state, "load_calibration", lambda *a, **k: calib)

    desk = SimpleNamespace(data=None, market=SimpleNamespace(latest_bar_date=lambda: None))

    monkeypatch.setattr(tape_state, "block",
                        lambda *a, **k: _tape_block(bucket="low|low",
                                                    calibration=calib))
    tail = mod._state_background(desk, "20261027", persona="hot_money")
    assert "档位把握" not in tail, "尾档不该背一条关于中档的免责"

    monkeypatch.setattr(tape_state, "block",
                        lambda *a, **k: _tape_block(bucket="mid|low",
                                                    calibration=calib))
    mid = mod._state_background(desk, "20261027", persona="hot_money")
    assert "中档边界" in mid["档位把握"]
    assert "63%" in mid["档位把握"]
    assert "两侧都可能翻" in mid["档位把握"]


def test_an_uncalibrated_tape_says_nothing_rather_than_guessing(monkeypatch):
    """没有校准文件时，宁可不提把握，也不许编一个数出来。"""
    import importlib
    from types import SimpleNamespace

    from service.research import state as state_mod
    from service.research import tape_state

    mod = importlib.import_module("scripts.monitor")
    monkeypatch.setattr(state_mod, "block", lambda *a, **k: _canned_block(1))
    monkeypatch.setattr(state_mod, "cached_state", lambda *a, **k: object())
    monkeypatch.setattr(tape_state, "cached_tape", lambda *a, **k: object())
    monkeypatch.setattr(tape_state, "load_calibration",
                        lambda *a, **k: {"ok": False, "note": "未校准"})
    monkeypatch.setattr(tape_state, "block",
                        lambda *a, **k: _tape_block(bucket="mid|mid",
                                                    calibration={"ok": False}))

    desk = SimpleNamespace(data=None, market=SimpleNamespace(latest_bar_date=lambda: None))
    bg = mod._state_background(desk, "20261027", persona="hot_money")
    assert "档位把握" not in bg
    assert "涨停结构" in bg


def test_handoff_ids_cannot_escape_the_root_and_saves_are_atomic(monkeypatch, tmp_path):
    from service.trading import handoff as handoff_mod

    root = tmp_path / "handoff"
    monkeypatch.setattr(handoff_mod, "HANDOFF", root)
    outside = tmp_path / "outside.json"
    outside.write_text('{"status": "outside"}', encoding="utf-8")

    assert handoff_mod.load("../outside") is None
    with pytest.raises(ValueError):
        handoff_mod.save("../outside", {"status": "overwrite"})
    assert json.loads(outside.read_text(encoding="utf-8")) == {"status": "outside"}

    replacements = []
    real_replace = handoff_mod.os.replace

    def record_replace(src, dst):
        replacements.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(handoff_mod.os, "replace", record_replace)
    path = handoff_mod.save("a" * 64, {"status": "pending"})

    assert handoff_mod.load("a" * 64) == {"status": "pending"}
    assert replacements and replacements[-1][1] == path
    assert not list(root.glob("*.tmp"))


def test_handoff_html_escapes_every_untrusted_text_field():
    from service.trading import handoff as handoff_mod

    doc = {
        "created_at": "<b>date</b>",
        "alerts": [{"severity": "alert", "title": "<script>title()</script>",
                    "facts": ["<img src=x>"], "ref": "<svg>ref</svg>"}],
        "positions": [{"symbol": "<script>symbol()</script>", "qty": "<b>100</b>",
                       "last": 1.0, "pct": 1.0, "cost": 1.0, "pnl": 1.0,
                       "stop": 1.0, "auction_line": 1.0}],
    }
    rendered = handoff_mod.render_html(doc, "<script>analysis()</script>")

    assert "<script>" not in rendered
    assert "<img src=x>" not in rendered
    assert "&lt;script&gt;analysis()&lt;/script&gt;" in rendered
    assert "&lt;img src=x&gt;" in rendered


def test_answer_is_idempotent_and_an_existing_send_claim_blocks_duplicates(
        monkeypatch, tmp_path):
    from service.trading import handoff as handoff_mod

    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)
    handoff_mod.save("20260107-120000", {"status": "pending", "alerts": []})
    calls = []
    monkeypatch.setattr(handoff_mod, "send_mail",
                        lambda subject, text, html, **_: calls.append(subject) or {"to": "me"})

    first = handoff_mod.answer("20260107-120000", "已确认第 1 项。")
    second = handoff_mod.answer("20260107-120000", "重复调用不应再发。")

    assert first["sent"] is True
    assert second["sent"] is False and second["already_answered"] is True
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.send-lock"))

    handoff_mod.save("20260107-120001", {"status": "pending", "alerts": []})
    (tmp_path / "20260107-120001.send-lock").write_text("sending", encoding="utf-8")
    blocked = handoff_mod.answer("20260107-120001", "已有发送中的认领。")
    assert blocked["sent"] is False and blocked["status"] == "sending"
    assert blocked["subject"] == "[持仓解读] 持仓提醒"
    assert len(calls) == 1


def test_post_send_state_failure_keeps_claim_to_prevent_duplicate_mail(
        monkeypatch, tmp_path):
    from service.trading import handoff as handoff_mod

    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)
    handoff_mod.save("20260107-120002", {"status": "pending", "alerts": []})
    calls = []
    monkeypatch.setattr(handoff_mod, "send_mail",
                        lambda subject, text, html, **_: calls.append(subject) or {"to": "me"})

    def fail_save(handoff_id, doc):
        raise OSError("injected state failure")

    monkeypatch.setattr(handoff_mod, "save", fail_save)
    with pytest.raises(OSError, match="injected"):
        handoff_mod.answer("20260107-120002", "邮件已发但状态失败。")

    assert len(calls) == 1
    assert (tmp_path / "20260107-120002.send-lock").exists()
    monkeypatch.undo()
    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)
    monkeypatch.setattr(handoff_mod, "send_mail",
                        lambda subject, text, html, **_: calls.append(subject) or {"to": "me"})
    retry = handoff_mod.answer("20260107-120002", "不能重复发信。")
    assert retry["status"] == "send_unknown", "邮件已发出但状态没写，不能报成普通的发送中"
    assert retry["may_have_sent"] is True
    assert len(calls) == 1


# ------------------------------------------------- 投递结果未知时不能靠重试解决
def _pending(monkeypatch, tmp_path, handoff_id):
    from service.trading import handoff as handoff_mod

    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)
    handoff_mod.save(handoff_id, {"status": "pending", "alerts": [],
                                  "created_at": "2026-10-29T09:30:00+08:00"})
    return handoff_mod


def _claim(handoff_id, tmp_path, *, minutes_ago):
    stamp = datetime.now(SHANGHAI) - timedelta(minutes=minutes_ago)
    (tmp_path / f"{handoff_id}.send-lock").write_text(json.dumps({
        "pid": 4242, "host": "somewhere", "phase": "sending",
        "created_at": stamp.isoformat(timespec="seconds"),
    }), encoding="utf-8")


def test_a_claim_older_than_any_live_send_reads_as_unknown(monkeypatch, tmp_path):
    handoff_mod = _pending(monkeypatch, tmp_path, "20260107-130000")
    _claim("20260107-130000", tmp_path, minutes_ago=120)
    calls = []
    monkeypatch.setattr(handoff_mod, "send_mail",
                        lambda subject, text, html, **_: calls.append(subject) or {"to": "me"})

    out = handoff_mod.answer("20260107-130000", "这条不该被发第二次。")

    assert out["status"] == "send_unknown" and out["may_have_sent"] is True
    assert out["lock"]["age_seconds"] >= 7200
    assert out["lock"]["pid"] == 4242, "锁要说明是谁留下的"
    assert calls == []
    assert (tmp_path / "20260107-130000.send-lock").exists(), "陈旧认领不能自动删除"


def test_a_fresh_claim_still_reads_as_sending(monkeypatch, tmp_path):
    handoff_mod = _pending(monkeypatch, tmp_path, "20260107-130001")
    _claim("20260107-130001", tmp_path, minutes_ago=0)
    calls = []
    monkeypatch.setattr(handoff_mod, "send_mail",
                        lambda subject, text, html, **_: calls.append(subject) or {"to": "me"})

    out = handoff_mod.answer("20260107-130001", "另一个进程正在发。")

    assert out["status"] == "sending" and calls == []


def test_unknown_delivery_keeps_the_claim_and_refuses_a_blind_retry(
        monkeypatch, tmp_path):
    handoff_mod = _pending(monkeypatch, tmp_path, "20260107-130002")
    calls = []
    ids = []

    def accepted_then_lost(subject, text, html, **kwargs):
        calls.append(subject)
        ids.append(kwargs.get("message_id"))
        raise MailDeliveryUnknown("SMTP 已接受邮件，但随后连接异常")

    monkeypatch.setattr(handoff_mod, "send_mail", accepted_then_lost)

    first = handoff_mod.answer("20260107-130002", "第一次投递结果未知。")
    assert first["status"] == "send_unknown" and first["may_have_sent"] is True
    assert (tmp_path / "20260107-130002.send-lock").exists(), "未知投递必须保留认领"
    assert handoff_mod.load("20260107-130002")["send_state"] == "unknown"

    second = handoff_mod.answer("20260107-130002", "普通重试不能变成第二封。")
    assert second["status"] == "send_unknown" and second["needs_confirmation"] is True
    assert len(calls) == 1, "没有人工确认就不能重发"
    assert ids == ["<20260107-130002@quant-desk.local>"]

    monkeypatch.setattr(handoff_mod, "send_mail",
                        lambda subject, text, html, **_: {"to": "me",
                                                          "message_id": "<x>"})
    forced = handoff_mod.answer("20260107-130002", "已确认没收到，重发一次。", force=True)
    assert forced["sent"] is True
    assert handoff_mod.load("20260107-130002")["status"] == "answered"
    assert not (tmp_path / "20260107-130002.send-lock").exists()


def test_release_archives_the_claim_and_still_demands_confirmation(monkeypatch, tmp_path):
    handoff_mod = _pending(monkeypatch, tmp_path, "20260107-130003")
    _claim("20260107-130003", tmp_path, minutes_ago=300)
    calls = []
    monkeypatch.setattr(handoff_mod, "send_mail",
                        lambda subject, text, html, **_: calls.append(subject) or {"to": "me"})

    out = handoff_mod.release_lock("20260107-130003", note="进程已被杀，邮箱没收到")

    assert out["status"] == "released" and out["lock"]["age_seconds"] >= 18000
    assert not (tmp_path / "20260107-130003.send-lock").exists()
    assert list(tmp_path.glob("*.send-lock.released-*")), "认领要留档，不是删掉"
    assert handoff_mod.load("20260107-130003")["send_state"] == "unknown"

    blocked = handoff_mod.answer("20260107-130003", "释放之后也不能顺手重发。")
    assert blocked["status"] == "send_unknown" and calls == []


def test_drop_refuses_while_a_claim_exists(monkeypatch, tmp_path):
    handoff_mod = _pending(monkeypatch, tmp_path, "20260107-130004")
    _claim("20260107-130004", tmp_path, minutes_ago=1)

    with pytest.raises(RuntimeError, match="发送认领"):
        handoff_mod.drop("20260107-130004")
    assert handoff_mod.load("20260107-130004")["status"] == "pending"
    assert (tmp_path / "20260107-130004.send-lock").exists()


def test_releasing_without_a_claim_says_so_instead_of_marking_unknown(monkeypatch, tmp_path):
    handoff_mod = _pending(monkeypatch, tmp_path, "20260107-130005")

    with pytest.raises(RuntimeError, match="没有发送认领"):
        handoff_mod.release_lock("20260107-130005")

    assert handoff_mod.load("20260107-130005").get("send_state") is None, (
        "没有认领就不该把这条提醒标成投递未知")


def test_dropping_keeps_the_record_and_the_reason(monkeypatch, tmp_path):
    """丢弃是状态迁移，不是删除；理由必须留在文档里。

    2026-11-09 清理 18 条 pending 时，8 条的成因（跨日账户比对、午休误判）
    在当天被修掉。只留 `dropped_at` 的话，日后无法区分「误报」与「人决定不发」。
    """
    from service.trading import handoff as handoff_module

    monkeypatch.setattr(analyst, "HANDOFF", tmp_path)
    monkeypatch.setattr(handoff_module, "HANDOFF", tmp_path)
    alerts, contexts = _alert_and_context()
    prompt = analyst.build_prompt(alerts, contexts)
    path = analyst.write_handoff(alerts, contexts, prompt, source="monitor")
    handoff_id = path.stem
    assert [d["id"] for d in handoff_module.pending()] == [handoff_id]

    handoff_module.drop(handoff_id, reason="成因已修")

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["status"] == "dropped"
    assert doc["drop_reason"] == "成因已修"
    assert doc["dropped_at"]
    assert doc["prompt"], "提示词要留着，否则日后无法复核当时问了什么"
    assert handoff_module.pending() == []


def test_pending_reports_age_so_the_reader_does_not_subtract_clocks(monkeypatch, tmp_path):
    """解读窗口的判据必须是服务端算好的小时数。

    2026-11-09 发现的结构性空转：提示词让模型「只挑 created_at 是今天的条目」，
    而 15:25 之后入库的提醒到次日已是历史，次日仍只看今天——这批条目没有任何
    一次运行会读到。判据改成 `age_hours <= READ_WINDOW_HOURS`，而这个数只能
    由服务端给：让模型自己减时刻，就是让它猜。
    """
    from service.trading import handoff as handoff_mod

    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)
    now = datetime.now(SHANGHAI)
    for ident, hours in (("fresh", 3), ("border", 95), ("stale", 200)):
        handoff_mod.save(ident, {
            "status": "pending", "alerts": [],
            "created_at": (now - timedelta(hours=hours)).isoformat(timespec="seconds")})
    handoff_mod.save("broken", {"status": "pending", "alerts": [], "created_at": "昨天下午"})

    rows = {row["id"]: row for row in handoff_mod.pending()}
    assert rows["fresh"]["age_hours"] == pytest.approx(3.0, abs=0.2)
    assert rows["border"]["age_hours"] == pytest.approx(95.0, abs=0.2)
    assert rows["stale"]["age_hours"] > handoff_mod.READ_WINDOW_HOURS
    assert rows["broken"]["age_hours"] is None, (
        "解析不了的时刻算过期，不能默认成 0 小时——那会让一条两年前的提醒看起来刚到")


def test_the_inbox_says_how_many_items_are_inside_the_window(monkeypatch, tmp_path):
    from service.api import trading as api
    from service.trading import handoff as handoff_mod

    monkeypatch.setattr(handoff_mod, "HANDOFF", tmp_path)
    now = datetime.now(SHANGHAI)
    for ident, hours in (("a", 2), ("b", 40), ("c", 500)):
        handoff_mod.save(ident, {
            "status": "pending", "alerts": [],
            "created_at": (now - timedelta(hours=hours)).isoformat(timespec="seconds")})

    out = api.monitor_handoff()

    assert out["pending"] == 3
    assert out["readable"] == 2, "两天内的两条在窗口内"
    assert out["stale"] == 1
    assert out["read_window_hours"] == handoff_mod.READ_WINDOW_HOURS
    assert f"age_hours ≤ {out['read_window_hours']}" in out["note"]
