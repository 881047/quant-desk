"""The kernel gives facts and arithmetic — never a recommendation.

The boundary is not a writing style. A response that says "reduce exposure" has
made a judgement, and on this desk only the model is allowed to make one. So the
boundary is testable: walk the payload each tool actually returns and fail on
advice-shaped keys or prose.

What is *not* a violation, and why:

* ``limits`` / ``rules`` — policy. "Single name ≤ 30%" is a constraint the guard
  enforces, not a suggestion about what to buy.
* ``regime.label`` / ``sentiment.tone`` — a classification produced by named
  rules from numbers that are also in the payload. It is a fact about the
  measurement, and the thresholds are visible.
* ``candidates`` / ``rank_key`` — a deterministic ordering. The list is labelled
  "规则候选", the weights are in params.yaml, and the model may ignore it.
* ``regime.label`` 的阈值、``sentiment.tone`` 的阈值 — 公开的约定；标签本身
  是这几个数按这些线分类的结果。

2026-11-03 之后这里少了几项：``arena.verdict``（edge / no_edge /
insufficient / unproven）、``board.setup_table.verdict``（has_edge / no_edge）、
``regime.permission``（四档祈使句）、``cycle.surprise[].verdict``、
``analogy.match.quality``、``information.grade`` 全部拆掉了。理由是同一条：
它们把几个已知的量合成一个词，而那个词读起来像结论。量还在，词没了。
"""

from __future__ import annotations

import json

FORBIDDEN_KEYS = {
    "action", "recommendation", "recommend", "advice", "suggest", "should",
    "must_do", "target_price", "rating", "conviction", "stance", "view",
}
FORBIDDEN_PHRASES = (
    "建议", "应该买", "应该卖", "推荐买", "推荐卖", "该买", "该卖",
    "看多", "看空", "目标价", "必买", "必卖", "宜减仓", "宜加仓", "不宜",
)
# Sentences that *deny* advice are the opposite of advice. Stripping them keeps
# the test honest in both directions: a payload may say "this is not advice".
NEGATIONS = ("不是建议", "不给买卖建议", "没有任何建议", "不建议", "没有建议",
             "不给任何建议", "不需要回应任何'建议'")


def _clean(text: str) -> str:
    for phrase in NEGATIONS:
        text = text.replace(phrase, "")
    return text


def _scan(node, trail: str = "", hits: list | None = None) -> list:
    hits = hits if hits is not None else []
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                hits.append((f"{trail}/{key}", "KEY", json.dumps(value, ensure_ascii=False)[:80]))
            _scan(value, f"{trail}/{key}", hits)
    elif isinstance(node, list):
        for i, value in enumerate(node[:60]):
            _scan(value, f"{trail}[{i}]", hits)
    elif isinstance(node, str):
        cleaned = _clean(node)
        for phrase in FORBIDDEN_PHRASES:
            if phrase in cleaned:
                hits.append((trail, f"TEXT:{phrase}", cleaned[:100]))
    return hits


def _payloads(desk) -> dict[str, object]:
    from service.research.discover import discover
    from service.trading.arena import scoreboard
    from service.trading.context_pack import build_context_pack
    from service.trading.duty import build as build_duty
    from service.trading.observation import snapshot as observation_snapshot
    from service.trading.regime import compute as compute_regime

    day = desk.market.latest_bar_date()
    return {
        "context_pack": build_context_pack(desk),
        "duty": build_duty(desk),
        "observations": observation_snapshot(desk),
        "regime": compute_regime(desk.market, day).as_dict(),
        "arena": scoreboard(desk),
        "discover": discover(desk.data, desk.registry, as_of=day,
                             core_book=desk.core_book()),
    }


def test_no_tool_response_contains_advice(desk):
    """Walk all six payloads once.

    This used to be six parametrised cases that each rebuilt *every* payload —
    six full assemblies (screen, tape, arena, discover) to assert six things
    about one snapshot of the same desk. One build, six scans: same coverage,
    a sixth of the cost, and the failure now names every offending block at
    once instead of stopping at the first.
    """
    payloads = _payloads(desk)
    hits = {name: _scan(payload) for name, payload in payloads.items()}
    offending = {name: found for name, found in hits.items() if found}
    assert not offending, f"these blocks carry judgement: {offending}"


class _StressedRegime:
    label = "stressed"
    reasons = ("等权 1.032 < MA200 1.173", "20 日波动 23.1%，近一年 80% 分位", "宽度 19.8%")


def test_the_duty_list_states_regime_as_a_state_not_as_an_instruction(desk, monkeypatch):
    """`stressed` is a label with a consequence, not a recommendation to sell."""
    import service.trading.duty as duty_mod

    monkeypatch.setattr(duty_mod, "compute", lambda market, day: _StressedRegime())
    note = next(item for item in duty_mod.build(desk)["items"]
                if item["kind"] == "regime_note")

    detail = note["detail"]
    assert "不是准入条件" in detail, "还得说清风控并不会因此拒单"
    assert not any(phrase in detail for phrase in ("宜减仓", "宜加仓", "不宜追高"))
    assert not _scan({"detail": detail}), "状态标签不能写成操作建议"
