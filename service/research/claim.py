"""A model's sentence -> a testable claim, or an honest refusal.

The desk has two halves that never met. The outbound half hands the model facts;
the inbound half judges `test_spec`s that a human wrote by hand. A claim like
"跌多了会反弹" sat in neither: too vague for a spec, too specific to ignore.

This module is the missing line. It reads a statement, decides which of three
forms it can take, and — for the cheapest form — answers it immediately:

    bucket        a market state; judged against the unconditional distribution
    overlay       a rule applied to an existing trade table (needs a base book)
    event_study   a full research spec (needs the referee, minutes not seconds)

Three rules, all of them about not cheating:

* **No `eval`, no generated code.** The output is a dict drawn from a closed
  vocabulary. A sentence that cannot be expressed in it is refused, not bent.
* **Assumptions are part of the answer.** "跌多了" does not name a bucket; this
  module picks ``low|low`` and says so in ``assumptions``. The reader must be
  able to disagree with the translation, not just the conclusion.
* **Refusing is a result.** ``Refusal.needs`` names what would have to be
  specified. The refusal rate is the first thing worth measuring: it is the
  share of the model's beliefs that were never testable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from service import settings

from . import overlay as overlay_mod
from . import state as state_mod
from . import stats as stats_mod

KINDS = ("bucket", "overlay", "event_study")
DEFAULT_HORIZON = settings.integer("research.default_horizon")
# The desk's bookkeeping threshold. Printed with every result so a reader can
# tell "the number says X" from "this desk would record X".
POLICY = {"name": "desk/v1",
          "p_max": settings.num("research.referee.p_max"),
          "min_episodes": settings.integer("research.state_min_episodes")}

# Direction words. They decide what "should" follow, not what is being tested:
# the test always asks whether the conditional distribution differs from the
# unconditional one, in the direction the claim asserts.
_UP_WORDS = ("反弹", "回升", "上涨", "走强", "修复", "抄底", "见底", "反包", "新高")
_DOWN_WORDS = ("回调", "下跌", "走弱", "破位", "杀跌", "见顶", "补跌", "新低")
_MARKET_WORDS = ("大盘", "市场", "全市场", "指数", "两市", "a股", "A股", "普涨", "普跌")

# Event vocabulary maps onto the pools the research layer already knows.
_EVENT_WORDS = {
    "涨停": ("limit_up", 1), "首板": ("limit_up", 1), "二板": ("limit_up", 2),
    "连板": ("limit_up", 2), "炸板": ("broken_board", 0), "跌停": ("limit_down", 0),
    "可转债": ("cb", 0), "转债": ("cb", 0),
}
# Overlay vocabulary maps onto rules that reuse an existing trade table.
# Longest phrase first. As a plain dict this was insertion-ordered, so "移动止损"
# matched the "止损" entry and was translated into a *fixed* stop — a different
# hypothesis from the one the user wrote, recorded with the wrong rule and no way
# for a reader to notice.
_RULE_WORDS = {
    "时间止损": "time_exit", "时间退出": "time_exit", "移动止损": "trailing_stop",
    "持有更久": "time_exit", "持有": "time_exit",
    "过滤": "regime_filter", "只做": "regime_filter",
    "止损": "stop_loss", "止盈": "take_profit",
}
_IMPROVE_WORDS = ("更好", "改善", "有效", "提高", "降低", "减少", "优于", "应该")
_HORIZONS = (("明天", 1), ("次日", 1), ("一周", 5), ("5日", 5), ("5 日", 5),
             ("两周", 10), ("10日", 10), ("一个月", 20), ("20日", 20), ("20 日", 20),
             ("一个季度", 60), ("季度", 60))

# "跌多了" is not a bucket until someone says which one. These are the defaults,
# and every one of them is reported in ``assumptions``.
_STATE_HINTS = (
    (("跌多了", "跌了很久", "超跌", "连续下跌", "跌惨"), "low", "low",
     "「跌多了」→ 宽度低分位 + 20 日收益低分位"),
    (("涨多了", "涨了很久", "高位", "连续上涨", "涨太多"), "high", "high",
     "「涨多了」→ 宽度高分位 + 20 日收益高分位"),
    (("缩量上涨", "窄幅上涨", "少数票", "分化上涨"), "low", "high",
     "「缩量上涨」→ 宽度低分位 + 20 日收益高分位"),
    (("普涨", "全面上涨", "普跌后的反弹"), "high", "high",
     "「普涨」→ 宽度高分位 + 20 日收益高分位"),
    (("磨底", "横盘", "缩量震荡"), "mid", "low",
     "「磨底」→ 宽度中分位 + 20 日收益低分位"),
)


@dataclass(frozen=True)
class Claim:
    text: str
    kind: str
    spec: dict
    horizon: int
    direction: str            # "up" | "down" | "two_sided"
    matched: list[str]
    assumptions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": True, "text": self.text, "kind": self.kind, "spec": self.spec,
                "horizon": self.horizon, "direction": self.direction,
                "matched": self.matched, "assumptions": self.assumptions}


@dataclass(frozen=True)
class Refusal:
    text: str
    reason: str
    needs: list[str]
    matched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": False, "text": self.text, "reason": self.reason,
                "needs": self.needs, "matched": self.matched}


def _hits(text: str, words) -> list[str]:
    return [w for w in words if w in text]


def _horizon(text: str) -> tuple[int, str | None]:
    for word, days in _HORIZONS:
        if word in text:
            return days, word
    return DEFAULT_HORIZON, None


def _direction(text: str) -> str:
    up, down = bool(_hits(text, _UP_WORDS)), bool(_hits(text, _DOWN_WORDS))
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return "two_sided"


def _has_direction(text: str) -> bool:
    return bool(_hits(text, _UP_WORDS) or _hits(text, _DOWN_WORDS))


_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|％|个点|点)")
_DAYS = re.compile(r"(\d+)\s*(?:个)?(?:交易日|天|日)")


def _threshold(text: str) -> float | None:
    """The level a stop or target sits at. Absent means the rule is unspecified."""
    m = _PCT.search(text)
    if not m:
        return None
    return round(float(m.group(1)) / 100.0, 6)


def _rule_params(rule: str, text: str) -> tuple[dict | None, list[str]]:
    """What is still missing before this rule can be run."""
    if rule in ("stop_loss", "take_profit", "trailing_stop"):
        level = _threshold(text)
        if level is None:
            what = {"stop_loss": "止损", "take_profit": "止盈",
                    "trailing_stop": "移动止损"}[rule]
            return None, [(f"{what}放在多少（例如 5%）？没有阈值就没有规则，"
                           "而阈值不同是两个不同的假设")]
        return {"threshold": level}, []
    if rule == "time_exit":
        m = _DAYS.search(text)
        if m:
            return {"days": int(m.group(1))}, []
        horizon, word = _horizon(text)
        if word:
            return {"days": horizon}, []
        return None, ["持有几天？（例如「3 天」）"]
    return None, ["「过滤」需要一个条件，而这句话没说过滤什么",
                  "写成「只做 low|low 的时候更好」这类条件句，会走 bucket 那条路"]


def compile_claim(text: str) -> Claim | Refusal:
    """Sentence -> Claim, or a Refusal that names what is missing."""
    raw = str(text or "").strip()
    if not raw:
        return Refusal(raw, "空主张", ["说一件具体会发生的事"])
    norm = raw.replace("，", " ").replace(",", " ")

    # 1) A rule that claims to improve something -> overlay.
    events = _hits(norm, _EVENT_WORDS)
    rules = _hits(norm, _RULE_WORDS)
    if rules and _hits(norm, _IMPROVE_WORDS):
        rule = _RULE_WORDS[rules[0]]
        # "炸板股持有 2 天会更好" names a pool, so it is an event study with a
        # different holding period, not a rule layered on someone else's book.
        if rule == "time_exit" and events:
            rules = []
    if rules and _hits(norm, _IMPROVE_WORDS):
        rule = _RULE_WORDS[rules[0]]
        params, missing = _rule_params(rule, norm)
        if params is None:
            # "加个止损会更好" is not a testable statement: a 3% stop and a 12%
            # stop are different hypotheses. Picking one for the model would
            # answer a question nobody asked.
            return Refusal(raw, f"「{rules[0]}」缺一个数", missing)
        return Claim(
            text=raw, kind="overlay",
            spec={"rule": rule, **params, "metric": "net_per_trade"},
            horizon=0, direction="up",
            matched=rules + _hits(norm, _IMPROVE_WORDS),
            assumptions=[f"「{rules[0]}」→ rule={rule}，参数 {params}",
                         "overlay 只改出场，不改入场：同一批交易跑两遍，样本不变",
                         "触发依据是收盘价（日线给不出盘中路径）"],
        )

    # 2) An event about a specific pool -> event_study.
    if events:
        pool, boards = _EVENT_WORDS[events[0]]
        horizon, hword = _horizon(norm)
        hold = 0 if pool in ("cb",) else max(1, min(2, horizon // 5 or 1))
        spec = {"pool": pool, "entry": "next_open", "hold": hold, "exit_at": "close"}
        if pool == "limit_up":
            spec["boards"] = [boards]
        assumptions = [f"「{events[0]}」→ pool={pool}"]
        if hword:
            assumptions.append(f"「{hword}」→ hold={hold} 个交易日")
        return Claim(text=raw, kind="event_study", spec=spec, horizon=horizon,
                     direction=_direction(norm), matched=events, assumptions=assumptions)

    # 3) A statement about the market's own state -> bucket.
    market = _hits(norm, _MARKET_WORDS)
    direction = _direction(norm)
    hint = None
    for words, breadth, ret, why in _STATE_HINTS:
        if _hits(norm, words):
            hint = (breadth, ret, why, words)
            break

    if hint or market:
        if hint is None:
            # "市场会涨" has no condition in it. A bucket test cannot answer an
            # unconditional forecast, and quietly picking ``low|low`` for it
            # would invent the very thing under test.
            return Refusal(
                raw, "这是无条件预测，不是条件命题",
                ["指明在什么状态下（跌多了 / 涨多了 / 缩量上涨 / 磨底）",
                 "无条件预测请走 memo：带概率、有到期结算、用 Brier 记分"])
        breadth, ret, why, words = hint
        # "缩量上涨之后会回调" names a state *and* a direction, and the state
        # words contain a direction word. Judge the prediction on what is left
        # after removing the state description, or the claim reads as two-sided.
        residual = norm
        for word in words:
            residual = residual.replace(word, " ")
        direction = _direction(residual) if _has_direction(residual) else _direction(norm)
        horizon, hword = _horizon(norm)
        assumptions = [why]
        assumptions.append("bucket 用宽度三分位 × 20 日收益三分位；只有 9 格")
        if hword:
            assumptions.append(f"「{hword}」→ 前向 {horizon} 个交易日")
        return Claim(
            text=raw, kind="bucket",
            spec={"bucket": f"{breadth}|{ret}", "dimensions": ["breadth20", "ew_ret_20d"]},
            horizon=horizon, direction=direction,
            matched=market + list(words) + _hits(norm, _UP_WORDS + _DOWN_WORDS),
            assumptions=assumptions)

    return Refusal(
        raw, "没有可识别的对象（市场 / 事件 / 规则）",
        ["指明主语：大盘、某类事件（涨停/炸板/转债）、或某条规则（止损/持有期）",
         "指明结果：涨/跌/改善"])


# ------------------------------------------------------------------ bucket judge
def _block_means(values: np.ndarray, block: int, n_boot: int, rng) -> np.ndarray:
    n = len(values)
    block = max(1, min(block, n // 2 or 1))
    n_blocks = math.ceil(n / block)
    # Match stats.block_bootstrap_mean: wrap the series so edge observations
    # have the same weight as interior ones and the H0 difference stays centred.
    starts = rng.integers(0, n, size=(n_boot, n_blocks))
    extended = np.concatenate([values, values[:block - 1]])
    windows = np.lib.stride_tricks.sliding_window_view(extended, block)
    return windows[starts.ravel()].mean(axis=1).reshape(
        n_boot, n_blocks).mean(axis=1)


def judge_bucket(state: pd.DataFrame, bucket: str, horizon: int = DEFAULT_HORIZON,
                 *, n_boot: int = 2000, seed: int = 7) -> dict:
    """Is this state's forward return different from an average day's?

    Two sampling problems, both handled: days inside one run overlap (so the
    conditional side is resampled by **episode**, not by day), and the
    unconditional series is serially correlated (so it is resampled in **blocks**
    of ``horizon`` sessions). The p-value compares the two means directly; it is
    not a t-test pretending the days are independent.
    """
    col = f"fwd_{horizon}d"
    if col not in state.columns:
        return {"ok": False, "error": f"state 里没有 {col}"}
    cond = state_mod.episode_means(state, bucket, horizon)
    base = state[col].dropna().to_numpy(dtype=float)
    if len(cond) < state_mod.MIN_EPISODES:
        return {"ok": False, "bucket": bucket, "horizon": horizon,
                "error": f"段数 {len(cond)} < {state_mod.MIN_EPISODES}，无法检验",
                "episodes": len(cond)}
    if len(base) < horizon * 4:
        return {"ok": False, "bucket": bucket, "horizon": horizon,
                "error": "无条件样本太短"}

    rng = np.random.default_rng(seed)
    diff = float(cond.mean() - base.mean())
    cond_means = cond[rng.integers(0, len(cond), size=(n_boot, len(cond)))].mean(axis=1)
    base_means = _block_means(base, horizon, n_boot, rng)
    diffs = cond_means - base_means
    # Confidence intervals use the observed bootstrap distribution. Tail tests
    # use the same draws recentered to H0: no conditional/unconditional difference.
    null_diffs = diffs - diff
    # Floored at 1/(n_boot+1): "no resample crossed zero" is evidence, not proof,
    # and a p of exactly 0.0 sorts to the top of any FDR family.
    p_gt = stats_mod.boot_p(-null_diffs, observed=-diff)
    p_lt = stats_mod.boot_p(null_diffs, observed=diff)
    p_two = round(min(1.0, 2 * min(p_gt, p_lt)), 6)

    return {
        "ok": True,
        "bucket": bucket,
        "horizon": horizon,
        "episodes": len(cond),
        "days": int((state["bucket"] == bucket).sum()),
        "conditional_mean": round(float(cond.mean()), 6),
        "conditional_median": round(float(np.median(cond)), 6),
        "unconditional_mean": round(float(base.mean()), 6),
        "diff": round(diff, 6),
        "ci_low": round(float(np.percentile(diffs, 2.5)), 6),
        "ci_high": round(float(np.percentile(diffs, 97.5)), 6),
        "p_two_sided": round(p_two, 4),
        "p_better_than_average": round(p_gt, 4),
        "basis": ("条件侧按段重采样，无条件侧按 block 重采样；"
                  "p 是两侧均值差的 bootstrap 双尾概率"),
        "caveat": ("桶里包含无条件样本本身，差异被轻微稀释；"
                   "p 只说「不同」，不说「可交易」——成本在裁判那一层"),
    }


def evaluate(claim: Claim, state: pd.DataFrame | None = None,
             trades: pd.DataFrame | None = None) -> dict:
    """Measure the claim. The interpretation is the caller's, not this module's.

    Deliberately no ``verdict`` field. Deciding whether p=0.136 supports a claim
    is reading, and reading belongs to whoever is going to act on it — the desk's
    job is to make the number, its sample and its basis impossible to misread.
    ``threshold_reading`` applies a named threshold when a record needs one; it is
    a convention of this desk, not a fact about the market, and it returns the
    number and the threshold next to the word.
    """
    if claim.kind == "bucket":
        if state is None or state.empty:
            return {"ok": False, "kind": "bucket", "spec": claim.spec,
                    "error": "没有状态序列，无法检验"}
        bucket = claim.spec["bucket"]
        test = judge_bucket(state, bucket, claim.horizon)
        if not test.get("ok"):
            return {**test, "kind": "bucket", "spec": claim.spec}
        dist = state_mod.bucket_history(state, bucket)
        evidence = dict(test)
        evidence["sign"] = _sign(claim, test["diff"])
        evidence["distribution"] = {
            "n": dist.get("n"), "episodes": dist.get("episodes"),
            (f"fwd_{claim.horizon}d"): dist.get(f"fwd_{claim.horizon}d"),
        }
        return {"ok": True, "kind": "bucket", "spec": claim.spec,
                "evidence": evidence,
                "assumptions": claim.assumptions,
                "policy": dict(POLICY),
                "note": ("evidence 是测量结果；要不要把它读成支持、反对或无结论，"
                         "由调用方决定。policy 只是本台记账时的约定阈值。")}

    if claim.kind == "event_study":
        return {"ok": True, "kind": "event_study", "spec": claim.spec,
                "status": "not_run", "assumptions": claim.assumptions,
                "next": ("这条要跑裁判（分钟级）："
                         f"scripts/run_research.py --spec '{_json(claim.spec)}'")}

    if trades is None or len(trades) == 0:
        return {"ok": True, "kind": "overlay", "spec": claim.spec,
                "status": "not_run", "assumptions": claim.assumptions,
                "next": ("overlay 需要一张基准交易表：先从一次 event_study 判决里拿 "
                         "best_trades（build_trades 的返回值，带 attrs['daily_rows']），"
                         "再用 --trades 传进来")}
    rule = claim.spec["rule"]
    params = {k: v for k, v in claim.spec.items()
              if k not in ("rule", "metric", "base")}
    try:
        result = overlay_mod.compare(trades, rule, **params)
    except ValueError as exc:
        return {"ok": False, "kind": "overlay", "spec": claim.spec,
                "error": str(exc)}
    evidence = dict(result)
    evidence["sign"] = _sign(claim, result["paired_diff"]["mean"])
    return {"ok": True, "kind": "overlay", "spec": claim.spec,
            "evidence": evidence,
            "assumptions": claim.assumptions,
            "policy": dict(POLICY),
            "note": ("paired_diff 是同一批交易的逐笔差；affected_diff 只含规则真正"
                     "改变了出场的那些笔。要不要采用由调用方决定。")}


def _sign(claim: Claim, diff: float) -> str:
    """Which way the point estimate points, relative to what was claimed."""
    if abs(diff) < 1e-9:
        return "flat"
    if claim.direction == "two_sided":
        return "nonzero"
    agrees = (claim.direction == "up" and diff > 0) or (claim.direction == "down" and diff < 0)
    return "agrees" if agrees else "contrary"


def threshold_reading(evidence: dict, policy: dict | None = None) -> dict:
    """把预注册阈值套在 p 值上——**连数和线一起返回**。

    原来叫 ``label_under``，只返回一个词（indistinguishable / contradicted /
    consistent）。词本身没算错，但它不带 p 值也不带阈值：读到 "contradicted"
    的人无从知道那是 p=0.04 还是 p=1e-6，也不知道线画在哪里，于是只能信这个词。
    裁判那边（``research.runner``）早就换成了 ``gate_reading`` + ``policy``
    的形状，这里跟上——同一个约定，两种输出形态只会让人以为它们是一回事。
    """
    pol = policy or POLICY
    p = float(evidence.get("p_two_sided", 1.0))
    p_max = float(pol["p_max"])
    if p >= p_max:
        reading = "indistinguishable"
    else:
        reading = "contradicted" if evidence.get("sign") == "contrary" else "consistent"
    return {
        "reading": reading,
        "p_two_sided": round(p, 6),
        "p_max": p_max,
        "policy": pol.get("name"),
        "rule": f"p < {p_max:g} 才区分得开；p ≥ {p_max:g} 记为 indistinguishable",
    }


def as_look(claim: Claim, evaluation: dict) -> dict | None:
    """What a registry row needs, or None when nothing was measured.

    This is identity and arithmetic, not judgement: which tail the claim asserted,
    how big the effect was, and how many observations it rests on. Whether to
    *book* the look is the caller's decision — keeping it out of ``evaluate`` is
    the same separation as keeping the verdict out of it.
    """
    ev = evaluation.get("evidence") or {}
    if claim.kind == "bucket" and ev:
        p_up = ev.get("p_better_than_average")
        if claim.direction == "two_sided" or p_up is None:
            p = ev.get("p_two_sided")
        else:
            p = p_up if claim.direction == "up" else round(1.0 - float(p_up), 6)
        return {"kind": "bucket", "bucket": claim.spec["bucket"],
                "horizon": claim.horizon, "p_value": p,
                "effect": ev.get("diff"), "n_episodes": ev.get("episodes")}
    if claim.kind == "overlay" and ev:
        paired = ev.get("paired_diff") or {}
        return {"kind": "overlay", "rule": claim.spec["rule"],
                "params": {k: v for k, v in claim.spec.items()
                           if k not in ("rule", "metric", "base")},
                "p_value": paired.get("p_value"),
                "p_one_sided": paired.get("p_value"),
                "effect": paired.get("mean"), "n_trades": ev.get("n_trades"),
                "n_fired": ev.get("n_fired")}
    return None


def _json(spec: dict) -> str:
    import json
    return json.dumps(spec, ensure_ascii=False, separators=(",", ":"))


def refusal_rate(texts: list[str]) -> dict:
    """The first number worth measuring: how many beliefs were never testable."""
    results = [compile_claim(t) for t in texts]
    refused = [r for r in results if isinstance(r, Refusal)]
    by_kind: dict[str, int] = {}
    for r in results:
        if isinstance(r, Claim):
            by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
    return {"total": len(results), "refused": len(refused),
            "refusal_rate": round(len(refused) / len(results), 3) if results else 0.0,
            "by_kind": by_kind,
            "refusals": [{"text": r.text, "reason": r.reason} for r in refused]}
