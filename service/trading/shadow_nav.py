"""双净值（现金腿）：理想决策价 vs 严格实际成交。只给上界，不给 alpha。

为什么只算现金腿
----------------
Perold 的 paper portfolio 是「按决策价、零摩擦、全数成交」的假想组合。本台
的账本（`paper.sqlite`）是唯一真相，本模块**不重算账本、不估持仓市值、
不构造净值曲线**。它只把每笔已成交的现金腿并列摆出来：

* ideal  = 按 arm 时冻结的帧价成交、零费用；
* strict = 按实际成交价 + 实际费用。

两者之差是「想法 → 成交」的**现金漏损上界**。它不含未成交余量（那部分
`opportunity_cost` 明确 not measurable），也不含持仓后续涨跌；所以它既不是
策略收益，也不是「时机贡献」的点估计——delay 与 impact 在当前数据下不可分离
（`tca.py:137-140`）。

arena 双阈值（不许笼统写「n<20 无置信区间」）
--------------------------------------------
* `n < 10`：`measured=False`，连区间都不给（`arena.py:228-239` 明说零宽区间
  比不打印更糟）。
* `n < 20`：不得称 edge（`arena.py:265-266` / `:350`）。
* 两个阈值分别标注；本模块不下 verdict，是否称 edge 由读的人按 arena 的
  bootstrap 结果判断。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

NO_INTERVAL_BELOW = 10
NO_EDGE_BELOW = 20
BPS = 10_000.0


def arena_guard(n: int) -> dict:
    """双阈值的机械读数。与 arena.py 的常量对齐，不重造判据。"""
    return {
        "n": int(n),
        "measured": int(n) >= NO_INTERVAL_BELOW,
        "may_claim_edge": int(n) >= NO_EDGE_BELOW,
        "ci_low": None,
        "ci_high": None,
        "ci_position": "sample_below_20" if int(n) < NO_EDGE_BELOW else "needs_bootstrap",
        "thresholds": {
            "no_interval_below": NO_INTERVAL_BELOW,
            "no_edge_below": NO_EDGE_BELOW,
            "promotion_min": NO_EDGE_BELOW,
        },
        "note": ("n<10 连区间都不给；n<20 不得称 edge。区间由 arena 的 bootstrap 给，"
                 "本模块不算、也不下 verdict。"),
    }


def dual_nav(fills: Iterable[Mapping[str, Any]] | None,
             *, initial_nav: float = 5000.0) -> dict:
    """并列两条现金腿。只读，不写账本，不产生委托。

    每笔 ``fill`` 需要 ``side/qty/price``；``decision_quote`` 与
    ``decision_quote_basis="frame"`` 齐全才进 ideal 腿，否则如实计入
    ``no_quote_reference``（cannot say 绝不渲染成零）。
    """
    ideal_delta = 0.0
    strict_delta = 0.0
    measured = 0
    no_quote = 0
    for record in fills or []:
        try:
            qty = int(record["qty"])
            price = float(record["price"])
            side = str(record.get("side", "BUY")).upper()
        except (KeyError, TypeError, ValueError):
            no_quote += 1
            continue
        if qty <= 0 or price <= 0:
            no_quote += 1
            continue
        fees = float(record.get("fees") or 0)
        sign = 1.0 if side == "BUY" else -1.0
        strict_delta -= sign * (price * qty) + fees
        quote = record.get("decision_quote")
        if (quote not in (None, "")
                and str(record.get("decision_quote_basis")) == "frame"):
            try:
                decision_price = float(quote)
            except (TypeError, ValueError):
                decision_price = 0.0
            if decision_price > 0:
                ideal_delta -= sign * (decision_price * qty)
                measured += 1
            else:
                no_quote += 1
        else:
            no_quote += 1

    gap = ideal_delta - strict_delta
    base = abs(ideal_delta) if ideal_delta else abs(strict_delta)
    return {
        "ok": True,
        "initial_nav": float(initial_nav),
        "ideal_nav": round(float(initial_nav) + ideal_delta, 2),
        "strict_nav": round(float(initial_nav) + strict_delta, 2),
        "ideal_cash_delta_cny": round(ideal_delta, 2),
        "strict_cash_delta_cny": round(strict_delta, 2),
        "gap_cny": round(gap, 2),
        "gap_bps": round(gap / base * BPS, 2) if base else None,
        "n_fills": measured + no_quote,
        "n_with_quote": measured,
        "no_quote_reference": no_quote,
        "arena": arena_guard(measured),
        "denominator": {
            "fills": measured + no_quote,
            "untriggered_intents_excluded": True,
            "partial_fill_remainder": "not measurable",
            "note": ("未触发 intent 从未进 OMS，不进 tca.summary；partial-fill "
                     "remainder 已进 OMS、余量 retired，是另一类样本。"),
        },
        "caveats": [
            "只有现金腿：不含持仓市值变化、不含未成交余量，净值曲线不由本模块构造。",
            "gap 是「想法→成交」漏损的上界，不是 alpha，也不是时机收益的点估计。",
            "delay 与 impact 在当前数据下不可分离；opportunity_cost 仍是 'not measurable'。",
            "不以净值曲线变好作为改造成功标准。",
        ],
    }
