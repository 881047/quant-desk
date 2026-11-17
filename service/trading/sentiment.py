"""The sentiment panel: one label a hot-money decision actually needs.

Why this is its own module: the panel used to be computed only inside
``scripts/snapshot_intraday.py``, which runs during a session. The daily job runs
at 17:30, so for the whole evening — the hours when a decision for tomorrow is
actually made — ``/duty`` and the dashboard reported "no sentiment panel for
today", and the hot-money sleeve was left with prices and no temperature.

Every input here is a daily-frequency fact that is already in the store: counts
of the three pools, the highest streak, sealed amount, break rate. Nothing needs
a live quote, so nothing should have required one.

The label is deliberately crude and transparent. ``_tone`` is four if-statements;
the model is free to disagree with it, but it must disagree with a number rather
than with a vibe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from service import settings

# ``tone`` has six values: 未成形 / 冰点 / 退潮 / 分歧 / 修复 / 主升. English aliases
# are never produced: an early version of persona.py said "tone=ice/ebb" while
# this module returned 冰点/退潮, so the documented gate named values that never
# appeared.
#
# ``未成形`` is not a market state: it means the pools have not finished loading.
# It exists because the three pools are filled progressively during the session
# — on 2026-10-30 the 09:41 snapshot read limit_up=0 / limit_down=1 while the
# close was 49 / 86, and the label "冰点" was quoted as a fact for eleven minutes.
# Anything consuming ``tone`` must treat this value as "cannot judge", never as
# "cold tape".


def read_snapshot(directory: Path, day: str) -> tuple[dict | None, str]:
    """最近一份情绪快照（不晚于 ``day``）与它自己的日期，或 ``(None, "")``。

    两个消费者各写过一遍这段读文件的逻辑：``context_pack``（每 60 秒被
    ``snapshot_intraday.py`` 重写，读到半个 JSON 是常态）和 ``data.py`` 的
    ``_sentiment``（只认当天，于是周六报 ``stage: unknown``，而上一交易日的完整
    情绪就在磁盘上）。同一件事写两遍，第二次就忘了回退。

    ``day`` 之后的不看：用一份"未来的"快照等于把后视镜当挡风玻璃。
    """
    if not directory.is_dir():
        return None, ""
    candidates = sorted((p for p in directory.glob("*.json") if p.stem <= day),
                        reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue          # 正在被重写：换上一份，不把半个 JSON 当结论
        if not isinstance(payload, dict):
            continue
        if not isinstance(payload.get("sentiment"), dict):
            continue
        # 整个 payload 一起给：``as_of`` 与 ``quality`` 在块外面，调用方两个都要。
        return payload, str(payload.get("trade_date") or path.stem)
    return None, ""


PROVISIONAL_TONE = "未成形"
# 剧本写的是「炸板率 < 35%」，而那个数是在**厂商口径**下标定的：厂商的炸板池只算
# 「封住后打开」，派生池把「盘中触及即回落」也算进去。两者不是同一个量。
#
# 246 个重叠交易日（2025-10-27 ~ 2026-10-30）上的实测：派生比厂商高 **+4.84pp**
# （均值 0.3026 vs 0.2542）。拿同一个 35% 去卡两个定义，判定一致率只有 87.8%；
# 把派生阈值放到 **38%**，一致率 95.1%——这不是放宽闸门，是把剧本那句话换算到
# 现在这个数的单位上。分歧阈值 45% 同理换成 47.5%（一致率 98.8%）。
#
# **能不能用日线把两个定义统一？试过了，不能。** 厂商炸板数稳定是派生的 **75%**
# （6,245 / 8,333），逐日相关 0.953——它数的确实是"触及后没封住"的一个子集，少的
# 那 25% 是"碰一下就下来、从没封住"的票，日线看不出来。三个候选代理都不成立：
#   * 开盘即封（`open == up_limit`）只占炸板的 8.2%，差 22pp；
#   * 剔掉北交所只去掉 0.6%，不解释任何东西；
#   * "收盘距涨停价 ≤5%"聚合上刚好 72.8%，但逐日平均差 4.7 只（基数约 30），
#     那是一条拟合出来的线，不是定义——换个窗口就变。
# 所以这里保留两个口径 + 各自的阈值，不再试图合并。要真合并，需要分钟线判定
# "封了多久才算封住"，而那是盘中数据的事。
# 四个阈值在 configs/params.yaml（sentiment.break_rate_*）。它们在下面按调用点
# 读取，不是模块常量：改配置重启即生效，不必回来改这个文件。此前是四个字面量，
# 与报告中"炸板率 38%"的文案各写一遍，改一处就会出现代码与文案不一致。
def _pool_len(pool: Any) -> int:
    if pool is None:
        return 0
    try:
        return len(pool)
    except TypeError:
        return 0


def _pool_max(pool: Any, column: str) -> float:
    """Read a numeric column from a DataFrame or from a list of dicts."""
    if pool is None:
        return 0.0
    if hasattr(pool, "columns"):
        if column not in pool.columns:
            return 0.0
        series = pool[column].fillna(0)
        return float(series.max() or 0.0)
    values = [(row.get(column) or 0) for row in pool]
    return float(max(values) if values else 0.0)


def _pool_sum(pool: Any, column: str) -> float:
    if pool is None:
        return 0.0
    if hasattr(pool, "columns"):
        if column not in pool.columns:
            return 0.0
        return float(pool[column].fillna(0).sum())
    return float(sum((row.get(column) or 0) for row in pool))


def tone(limit_up: int, broken: int, limit_down: int, max_boards: int,
         *, phase: str = "settled", basis: str = "vendor") -> str:
    """A crude, transparent label; the model may disagree, with evidence.

    ``phase="opening_window"`` returns ``未成形`` instead of any state label: in
    the first thirty minutes the pools hold a fraction of the day's boards, so
    every downstream judgement built on them is about the loader, not the tape.

    ``basis`` picks which definition of 炸板 this count came from. The two differ
    by 4.84pp over 246 sessions, so a single 45% would label the same tape
    differently depending on which pool happened to be available — 分歧 on the
    derived basis, 主升 on the vendor one. Default is ``vendor`` because that is
    what a bare ``tone(...)`` call with three counts has always meant.
    """
    if phase == "opening_window":
        return PROVISIONAL_TONE
    if limit_up == 0:
        return "冰点"
    if limit_down > limit_up:
        return "退潮"
    attempts = limit_up + broken
    break_rate = broken / attempts if attempts else 0.0
    limit = settings.num("sentiment.break_rate_divergence" if basis == "derived"
                         else "sentiment.break_rate_divergence_vendor")
    if break_rate > limit:
        return "分歧"
    if (max_boards >= settings.integer("sentiment.tone_max_boards")
            and limit_up >= settings.integer("sentiment.tone_min_limit_up")):
        return "主升"
    return "修复"


def panel(pools: dict, *, phase: str = "settled", breadth: dict | None = None) -> dict:
    """The panel from three pools. Accepts DataFrames or lists of dicts.

    ``breadth`` overrides the three counts and the height with numbers from the
    derived pool. That is not a preference: the vendor pools understate the tape.
    Over the 38 sessions from 2026-09-01 to 10-30 同花顺's limit-up count matched
    the exchange ceilings on only 16 days — 58% of sessions were low, by 1.6 names
    on average and by 16 on 2026-10-26 — and on 2026-09-02 it reported a 4-board
    ladder on a day whose tallest name was a 5. ``max_boards >= 4`` is a gate
    input here, so a missed name can flip 主升 to 修复.

    The vendor numbers are not discarded: they come back under ``vendor_counts``
    so a reader can see the gap instead of having to trust one source.
    """
    limit_up = pools.get("limit_up_pool")
    broken = pools.get("broken_board_pool")
    limit_down = pools.get("limit_down_pool")
    n_up, n_broken, n_down = _pool_len(limit_up), _pool_len(broken), _pool_len(limit_down)
    max_boards = int(_pool_max(limit_up, "boards"))
    # 封单额只有厂商池有，派生池没有这一列，所以它永远来自 pools。
    seal_amount = _pool_sum(limit_up, "seal_amount")
    vendor_counts = {"limit_up": n_up, "broken_board": n_broken,
                     "limit_down": n_down, "max_boards": max_boards}
    source = "vendor"
    if breadth:
        n_up = int(breadth["limit_up"])
        n_broken = int(breadth["broken"])
        n_down = int(breadth["limit_down"])
        max_boards = int(breadth["max_boards"])
        source = str(breadth.get("source") or "derived:limit_pool_t0")
    attempts = n_up + n_broken
    out = {
        "limit_up": n_up,
        "broken_board": n_broken,
        "limit_down": n_down,
        "max_boards": max_boards,
        "seal_amount_cny": round(seal_amount, 2),
        "break_rate": round(n_broken / attempts, 4) if attempts else None,
        "break_rate_basis": ("touched: 盘中触及涨停价即算炸板" if breadth else "vendor"),
        "height_ok": max_boards >= 3,
        "tone": tone(n_up, n_broken, n_down, max_boards, phase=phase,
                     basis=("derived" if breadth else "vendor")),
        "phase": phase,
        "provisional": phase != "settled",
        "counts_source": source,
    }
    if breadth:
        out["vendor_counts"] = vendor_counts
        v_attempts = vendor_counts["limit_up"] + vendor_counts["broken_board"]
        out["break_rate_vendor"] = (round(vendor_counts["broken_board"] / v_attempts, 4)
                                    if v_attempts else None)
    return out


def close_panel(store, day: str) -> dict:
    """The official close-of-day panel: derived counts first, vendor as fallback.

    ``panel`` has accepted a ``breadth`` override since the vendor pools were
    found to understate the tape, but the 17:30 job never passed one — it built
    the panel from the three vendor pools *before* ``derive_pools`` had written
    the day's partition. The close file was therefore vendor-based every day
    while ``facts`` and ``limit_structure`` used the derived pool: 2026-11-13
    published 0.3621 (vendor) and 0.387 (derived) for one session, and a reader
    had to know which ruler applied to which number.

    Reading the store here rather than in the job keeps the policy in one place:
    "prefer the counts that can be checked name by name, fall back to the vendor
    pool when the derived one is not there, and say which one it was" is a rule
    about the panel, not about the scheduler. A missing or unreadable derived
    partition is not an error — ``panel`` labels the vendor basis, and ``gates``
    then applies the vendor threshold.
    """
    from .tape import breadth as derived_breadth

    def _pool(name: str):
        frame = store.read(name)
        if frame is None or frame.empty or "trade_date" not in frame.columns:
            return None
        return frame[frame["trade_date"].astype(str) == day]

    try:
        breadth = derived_breadth(store, day)
    except Exception:  # noqa: BLE001 - a bad derived read must not cost the panel
        breadth = None
    return panel({
        "limit_up_pool": _pool("limit_up_pool"),
        "broken_board_pool": _pool("broken_board_pool"),
        "limit_down_pool": _pool("limit_down_pool"),
    }, breadth=breadth)


def gates(sentiment: dict | None) -> dict:
    """Whether the playbook's hot-money conditions hold, and which do not.

    The playbook's 情绪 line says 主升/修复, 炸板率 < 35%, 最高板 >= 3; its 入场 line
    then says entries happen on 分歧回封. Those two sentences only agree if 分歧 is
    allowed through the gate — it is the state the entry pattern is *for* — so
    ``tone_ok`` accepts it too. The docstring used to quote only the first line,
    which made the code look like it was loosening a rule it was actually
    reconciling with the next paragraph.
    Returning the individual checks — rather than one boolean — is what lets a
    reader see "the tape is warm but the height is missing", which is a different
    decision from "the tape is cold".

    A check that cannot be computed is reported as ``None`` and listed in
    ``indeterminate``, not folded into ``failed``. Folding them together is how
    "the pool had not loaded yet" got written up as "the gate failed" on
    2026-10-30. ``ok`` stays conservative when *something* was computable —
    anything indeterminate means not ok — but the reader can now tell the two
    apart.

    When **nothing** was computable (the whole panel is missing) ``ok`` is
    ``None``, not ``False``. ``ok=False`` is this desk's documented sentence for
    "there is no short-term trade today"; it must not also be the sentence for
    "the pipeline did not run". 2026-10-30 produced a day of "今天没有短线" whose
    real cause was a missing partition, and the reading is only distinguishable
    if the two states look different.
    """
    if not sentiment:
        return {"ok": None, "checks": {}, "failed": [],
                "indeterminate": ["no_panel"], "phase": None,
                "note": ("这一天的三池没有入库，情绪闸门无法判断。它不是"
                         "'闸门不过'（没有短线可做），是'没得判'（管道没跑完）。"
                         "先补数据，再谈行情")}
    break_rate = sentiment.get("break_rate")
    tone_value = sentiment.get("tone")
    phase = sentiment.get("phase", "settled")
    unformed = tone_value == PROVISIONAL_TONE or phase == "opening_window"
    # Which definition of 炸板 produced ``break_rate`` decides which threshold
    # judges it. ``panel`` labels the basis; a snapshot without the label is an
    # old one, written before the field existed, and those were gated with the
    # derived number — so the default keeps them on the same rule as before.
    #
    # 2026-11-13 measured the cost of not reading the label: the vendor rate
    # 0.3621 (37 涨停 / 21 炸板) was compared against the derived gate 0.38 and
    # passed, while the derived pool's own 0.387 failed the same 0.38. The two
    # numbers are 4.84pp apart on average over 246 sessions, so one threshold
    # cannot judge both. With the vendor gate 0.35 both sources reject that day.
    basis = str(sentiment.get("break_rate_basis") or "")
    vendor_basis = basis == "vendor"
    gate_key = ("sentiment.break_rate_gate_vendor" if vendor_basis
                else "sentiment.break_rate_gate")
    gate_value = settings.num(gate_key)
    checks = {
        "tone_ok": None if unformed else tone_value in ("主升", "修复", "分歧"),
        "break_rate_ok": (None if (unformed or break_rate is None)
                          else float(break_rate) < gate_value),
        "height_ok": None if unformed else bool(sentiment.get("height_ok")),
    }
    failed = [k for k, v in checks.items() if v is False]
    indeterminate = [k for k, v in checks.items() if v is None]
    # 有一份 sentiment 就仍按保守走 False：盘中"还没成形"是时序状态，答案是
    # "现在别开新仓"，不是"今天没有短线"。只有整份面板缺失（上面的分支）
    # 才是"没得判"。
    out = {"ok": not failed and not indeterminate, "checks": checks,
           "failed": failed, "indeterminate": indeterminate,
           "tone": tone_value, "break_rate": break_rate,
           # The verdict travels with the ruler that produced it: a bare
           # 0.3621 tells a reader nothing about whether 0.35 or 0.38 applies.
           "break_rate_basis": basis or None,
           "break_rate_gate": gate_value,
           "max_boards": sentiment.get("max_boards"), "phase": phase}

    # tone 与 gates 用的是**两把尺子**，中间有一段会互相打脸：
    #   tone 判「分歧」要炸板率 > sentiment.break_rate_divergence（派生 0.475）
    #   gates 判「能不能做」用与口径配套的闸门（派生 0.38 / 厂商 0.35）
    # 炸板率落在 0.38~0.475 之间时，两边都按自己的定义算对了，但结论相反：
    # 2026-11-03 的 0.4146 就是这样——tone=主升、gates.ok=false。
    #
    # 原来这句话以"**以 gates.ok 为准**"收尾。那是在替读者裁决，而且裁决的理由
    # 只是"gates 更像闸门"。两个门槛都是预注册的、都算对了；该听哪个取决于你
    # 要回答什么问题（"市场强不强" vs "能不能开新仓"），那是读的人的活。
    # 现在只把矛盾和解法摆出来，不给仲裁。
    if tone_value in ("主升", "修复") and failed:
        divergence = settings.num(
            "sentiment.break_rate_divergence_vendor" if vendor_basis
            else "sentiment.break_rate_divergence")
        out["tone_note"] = (
            f"tone={tone_value} 与 gates.ok=false 不矛盾：tone 判「分歧」的门槛是"
            f"炸板率 > {divergence:.3f}，"
            f"而 gates 判「能不能做」的门槛是 {gate_value:.3f}。"
            f"当前 {float(break_rate):.4f} 落在两者之间"
            f"——强到还能封板，弱到不宜开新仓。两个门槛都是预注册的，"
            f"该听哪个取决于你问的是「市场强不强」还是「能不能开新仓」"
            if break_rate is not None else
            "tone 与 gates.ok 不一致；两个门槛都按各自的定义算对了，"
            "引用其中一个时说明你在回答哪个问题"
        )

    # 两个炸板率口径把同一个阈值夹在中间时，答案取决于你信哪一版。这种情况不该
    # 由代码替读者选：2026-10-20 就是 0.3494（厂商）vs 0.3596（派生），
    # 一个过闸门、一个不过，而两者的差别只是「盘中跳一下就下来」算不算炸板。
    #
    # 只有 ``break_rate`` 是派生口径时这里才有两个数可比：厂商口径的 panel 没有
    # ``break_rate_vendor``（它就是自己），硬比会把同一个数拿两把尺子量一遍。
    rate_vendor = sentiment.get("break_rate_vendor")
    if (not unformed and not vendor_basis and break_rate is not None
            and rate_vendor is not None):
        mine = float(break_rate) < gate_value
        theirs = float(rate_vendor) < settings.num("sentiment.break_rate_gate_vendor")
        if mine != theirs:
            out["borderline"] = {
                "metric": "break_rate",
                "gate": {"derived": settings.num("sentiment.break_rate_gate"),
                         "vendor": settings.num("sentiment.break_rate_gate_vendor")},
                "derived": break_rate,
                "vendor": rate_vendor,
                "bases": {"derived": sentiment.get("break_rate_basis"),
                          "vendor": "vendor: 封住后打开才算炸板"},
                "note": ("两个口径分别落在阈值的两侧，所以这个闸门在这天没有唯一答案。"
                         "派生池把「盘中触及即回落」算作炸板，厂商只算「封住后打开」；"
                         "0.35 这个阈值当初是按厂商口径定的。要下判断就自己选一版并写明。"),
            }
    if indeterminate:
        out["why_indeterminate"] = (
            "开盘三十分钟内三池只加载了一部分，家数会远低于收盘值"
            "（10-30 实测 09:41 涨停 0 → 收盘 49，跌停 1 → 86）；"
            "这不是行情，是加载进度。用昨日收盘的终值做判断，等收盘后再用当日值。"
            if unformed else
            "炸板率的分母（涨停+炸板）为 0，无法计算；这是数据缺失，不是炸板率高。")
    return out
