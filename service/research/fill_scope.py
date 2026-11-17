"""Read-only attribution of the *fill assumption* behind a measured hypothesis.

Why this module exists
----------------------
``hypotheses.result`` stores the verdict and the numbers the referee saw, but
not the execution assumption those numbers were computed under. id=6 is the
case that made this dangerous: ``pool=limit_up, entry=t_close`` produced
+0.7547%/trade over 44,446 trades, Sharpe 7.04 and a CPCV OOS Sharpe of 7.41,
yet ``service/research/spec.py`` documents that this entry is **optimistic** —
it only samples names that were still sealed at the close, which is something
you only learn *after* buying. A reader of the ledger had no way to know that
without opening the source, and ``fill_model`` / ``buyable`` / ``one_word`` did
not appear anywhere in the stored result.

This module is a **display layer only**. It reads the spec of an already-stored
hypothesis and attaches the documented fill assumption, the known bias and the
unbiased comparison read. It does not change ``reasons``, does not re-run any
verdict, does not touch thresholds, and never writes to the hypotheses table.

The numbers below are not new estimates. They are transcribed from two places
already in this repository, and ``tests/research/test_fill_scope.py`` reads the
source files to assert those phrases still exist — a changed fill model must
break the test rather than silently leave a stale claim here.
"""

from __future__ import annotations

from typing import Any

#: Where the quoted facts come from. Kept as data so a reader can verify them.
SOURCES = {
    "spec_entry": "service/research/spec.py:76-96",
    "spec_next_open": "service/research/spec.py:8-10",
    "spec_next_limit": "service/research/spec.py:92-94",
    "board_base_rate": "service/trading/board.py:292-303",
}

#: 2021-2026 measured split of how a limit-up board formed, from
#: ``board.py:299-303``. ``buyable`` is the kernel's own grouping: a one-word
#: board never traded below its limit, so no limit order could have filled.
BOARD_FILL_KINDS = (
    {"kind": "one_word", "share": 0.089, "next_open_mean_pct": 6.74,
     "p10_pct": 0.61, "buyable": False,
     "why": "开盘即封、全天没离开涨停价 → 买不到"},
    {"kind": "opened_after_seal", "share": 0.053, "next_open_mean_pct": 1.80,
     "p10_pct": -4.58, "buyable": True,
     "why": "开盘封住但被砸开过 → 开板瞬间可买"},
    {"kind": "sealed_intraday", "share": 0.858, "next_open_mean_pct": 1.87,
     "p10_pct": -2.29, "buyable": True,
     "why": "涨停是盘中封上的 → 封板之前就能买，不需要排队"},
)

_T_CLOSE_SEALED = {
    "status": "optimistic",
    "assumption": ("T 日以收盘价买入；收盘仍封在涨停价时 close 就是 limit_price，"
                   "回测按该价格成交，隐含『排板能排到』"),
    "known_bias": ("池子只统计『收盘仍封住』的票，而封住是买入之后才知道的事。"
                   "2026-11-03 的修正记录：早先按这个口径写出的『首板 T 收→T+1 收 "
                   "+1.75%、隔夜 +2.00%』是选择后的结果，不是可交易期望"),
    "unbiased_reading": {
        "metric": "首板隔夜（按『当日触板』的无偏口径）",
        "value_pct": -0.34,
        "note": ("限价单只在价格跌破涨停价时成交；一字板（从未跌破，+7.01%）"
                 "买不到，买得到的那批期望为负。见 board._base_rate 的 "
                 "buyable / one_word 分组"),
        "source": SOURCES["spec_entry"],
    },
    "unfillable_legs": list(BOARD_FILL_KINDS),
    "reading_guard": ("本条的 mean_excess / Sharpe / CPCV 必须按 fill_model 读，"
                      "不能按无条件均值读。若买入需要排队，这些数字不代表可成交收益；"
                      "判决里的 reasons 只列了成本 / DSR / 容量，没有列出这条口径风险。"),
    "source": [SOURCES["spec_entry"], SOURCES["board_base_rate"]],
    "sample_period": "board.py 的 2021-2026 实测；spec.py 的 2026-11-03 修正记录",
}


def _plain(entry: str, assumption: str, guard: str, source: str) -> dict:
    return {
        "status": "documented",
        "assumption": assumption,
        "known_bias": None,
        "unbiased_reading": None,
        "unfillable_legs": [],
        "reading_guard": guard,
        "source": [source],
        "sample_period": None,
    }


def fill_scope(entry: str | None, pool: str | None = None) -> dict[str, Any]:
    """Attach the documented fill assumption for one measured hypothesis.

    Pure function of the stored spec. Returns a structured *unknown* when the
    old-format row has no spec, so a missing assumption is never mistaken for
    "there was no bias".
    """
    key = str(entry or "").strip()
    pool_key = str(pool or "").strip()
    if not key:
        return {"status": "unknown_spec",
                "assumption": None,
                "known_bias": None,
                "unbiased_reading": None,
                "unfillable_legs": [],
                "reading_guard": ("这条旧格式假设没存 spec，成交口径无法判断；"
                                  "不要把它读成『口径没问题』，也不要读成『口径有问题』"),
                "source": [], "sample_period": None,
                "note": "只读展示，不改判决"}

    if key == "t_close" and pool_key == "limit_up":
        out = dict(_T_CLOSE_SEALED)
    elif key == "t_close":
        out = _plain(
            key,
            "T 日收盘价买入（非涨停池也按收盘价成交）",
            "若当日收盘价恰在涨跌停价上，收盘价买入同样需要排队，日线无法知道成交与否",
            SOURCES["spec_entry"],
        )
    elif key == "next_open":
        out = _plain(
            key,
            "T+1 开盘价买入；开盘价触及涨停（一字板 / 开盘涨停）时该笔被跳过",
            ("内核已显式跳过开盘一字板，所以本条不含『买不到的一字板』；"
             "但开盘可成交价对跳空的缓冲由 execution.next_open_buffer 决定"),
            SOURCES["spec_next_open"],
        )
        out["unfillable_legs"] = [next(k for k in BOARD_FILL_KINDS
                                        if k["kind"] == "one_word")]
        out["source"] = [SOURCES["spec_next_open"], SOURCES["board_base_rate"]]
    elif key == "next_limit":
        out = _plain(
            key,
            ("T+1 在最低价挂限价单（相对 T 收盘折让 limit_drop_pct）；"
             "low_f1 <= 限价 即成交，日线 OHLC 可验证"),
            ("限价单是等价格来找你，不是追着买，所以排队优先权不影响本条；"
             "代价是只在下跌到挂单价的样本里成交，样本本身是条件化的"),
            SOURCES["spec_next_limit"],
        )
    elif key == "next_close":
        out = _plain(
            key, "T+1 收盘价买入",
            "收盘价成交同样隐含『该价位有对手盘』；日线无法验证排队",
            SOURCES["spec_entry"],
        )
    else:
        return {"status": "unsupported_entry", "entry": key,
                "assumption": None, "known_bias": None,
                "unbiased_reading": None, "unfillable_legs": [],
                "reading_guard": "未知入场点，成交口径没有登记，不能推断",
                "source": [], "sample_period": None,
                "note": "只读展示，不改判决"}

    out.update({"entry": key, "pool": pool_key or None,
                 "note": ("只读展示层：不改 reasons、不重跑判决、不改阈值。"
                          "本字段是成交口径的事实，不是新的判决理由")})
    return out


def slim_scope(scope: dict) -> dict[str, Any]:
    """Per-row summary. The long prose and the 3 board legs live once in
    ``ledger.fill_scope_reference`` instead of being repeated on every row."""
    status = scope.get("status")
    return {
        "status": status,
        "entry": scope.get("entry"),
        "pool": scope.get("pool"),
        "unbiased_reading": (scope.get("unbiased_reading")
                             if status == "optimistic" else None),
        "detail": "完整口径见 fill_scope_reference[<entry>|<pool>]",
    }


def scope_key(entry: str | None, pool: str | None) -> str:
    """Key used by ``ledger.fill_scope_reference``."""
    return f"{entry or ''}|{pool or ''}"


def scope_gap(rows: list[dict]) -> dict[str, Any]:
    """Which stored hypotheses rest on an optimistic fill assumption."""
    optimistic: list[dict] = []
    for row in rows:
        scope = row.get("fill_scope") or {}
        if scope.get("status") == "optimistic":
            optimistic.append({
                "id": row.get("id"),
                "entry": scope.get("entry"),
                "pool": scope.get("pool"),
                "status": row.get("status"),
                "mean_excess": (row.get("evidence") or {}).get("mean_excess"),
                "reading": ("见 fill_scope_reference 的 reading_guard / "
                            "unbiased_reading；reasons 里没有这一项"),
            })
    return {
        "optimistic_fill_count": len(optimistic),
        "hypotheses": optimistic,
        "note": ("这些判决的 reasons 里没有成交口径这一项。看到高 mean_excess / 高 Sharpe "
                 "时先读对应行的 fill_scope，再决定要不要把它当成可交易 α。"
                 "本字段只读，不改动任何在案判决"),
    }
