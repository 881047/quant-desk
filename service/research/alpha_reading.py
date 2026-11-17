"""Read-only display split: signal-book alpha vs account-implementable alpha.

A stored hypothesis result carries both kinds of numbers, mixed together:
``best.metrics.sharpe`` is the *signal book* (every signal of the day averaged
into an equal-weight portfolio), while ``stress`` / ``capacity`` are the account
constraints. ``service/research/capital.py`` documents what happens when the two
are confused: a 30-signal day was scored as a fully invested 30-name portfolio
this ¥5,000 / 3-position account cannot buy, cannot finance and cannot hold;
``broken_board`` once reported a 190% CAGR that way, and only the capacity check
rescued the verdict.

Public methodology agrees on the reporting rule: report performance at several
cost levels rather than one, and the *shape* of that collapse is more
informative than any single headline figure (omnidatatec, "Building a
Walk-Forward Backtesting Harness That Does Not Lie to You", 2026). A fill that
was never received is not a cost, either — modelling it as a cost keeps a
position that could not have been taken and merely charges for it.

This module only reads an already-stored ``result`` dict and lays the two
readings side by side. It does not recompute, re-run, re-judge or write anything.
"""

from __future__ import annotations

from typing import Any


def _get(mapping: Any, *keys, default=None):
    for key in keys:
        if not isinstance(mapping, dict):
            return default
        mapping = mapping.get(key)
    return default if mapping is None else mapping


def alpha_reading(result: dict | None) -> dict[str, Any]:
    """Split one stored verdict into signal-book and account-implementable reads."""
    if not isinstance(result, dict) or not result:
        return {"status": "not_stored", "read_only": True,
                "note": "这条没存下判决明细；不能把缺数据读成『没有超额』"}

    best = result.get("best") or {}
    metrics = best.get("metrics") or {}
    trades = best.get("trades") or {}
    cap = result.get("capacity") or {}
    stress = result.get("stress") or {}
    cpcv = result.get("cpcv") or {}
    pbo = result.get("pbo") or {}

    has_signal = bool(metrics) or bool(trades)
    has_account = bool(cap) or bool(stress)
    if not has_signal and not has_account:
        return {"status": "not_stored", "read_only": True,
                "note": ("旧格式只存了 metrics 与 notes，没有 best/capacity/stress；"
                         "要这两个读数须重跑同一条 spec，本层不会补编数字")}

    s2 = stress.get("2x") or {}
    s3 = stress.get("3x") or {}
    sharpe_signal = _get(metrics, "sharpe")
    sharpe_2x = s2.get("sharpe")
    collapses = bool(sharpe_signal is not None and sharpe_2x is not None
                     and sharpe_signal > 0 > sharpe_2x)

    signal_book = {
        "basis": ("信号账本口径：当日所有信号按等权组合计入，未受 max_positions / "
                  "可用现金 / 整手 / 最低佣金约束"),
        "sharpe": sharpe_signal,
        "total_return": metrics.get("total_return"),
        "hit_rate": metrics.get("hit_rate"),
        "mean_excess_per_trade": trades.get("mean_excess"),
        "trades_n": trades.get("n"),
        "cpcv_oos_sharpe_mean": cpcv.get("oos_sharpe_mean"),
        "pbo": pbo.get("pbo"),
        "warning": ("这是策略级统计量，不是这个账户能实现的净值曲线。"
                    "capital.py 自述：30 个信号曾被当成满仓 30 名组合打分，"
                    "broken_board 报过 190% CAGR，是容量检查救回了判决"),
    } if has_signal else {"status": "not_stored"}

    account = {
        "basis": ("账户口径：max_positions / 现金 / 整手 / ¥5 最低佣金约束下的可实现性；"
                  "stress 是同一套收费模型的 2× / 3× 成本压力测试"),
        "implementable": cap.get("implementable"),
        "account_equity_cny": cap.get("account_equity"),
        "capital_one_lot_each_cny": cap.get("capital_one_lot_each"),
        "median_signals_per_day": cap.get("median_signals_per_day"),
        "max_names_affordable": cap.get("max_names_affordable"),
        "sharpe_at_2x_cost": sharpe_2x,
        "sharpe_at_3x_cost": s3.get("sharpe"),
        "hit_rate_at_2x_cost": s2.get("hit_rate"),
    } if has_account else {"status": "not_stored"}

    out = {
        "status": "measured",
        "read_only": True,
        "signal_book": signal_book,
        "account_implementable": account,
        "cost_sensitivity": {
            "collapses_between_1x_and_2x": collapses,
            "note": ("只看 1× 成本下的 headline Sharpe 会漏掉这件事。"
                     "按公开做法应并列报 1× / 2× / 3×，崩塌的形状比单点数字更有信息；"
                     "未成交的信号不是成本，不能只给它加一笔费用就当作持有过"),
        },
        "reading": ("gate_reading=rejected 是预注册门禁的机械读数，不是『短线没有 α』。"
                    "这里分列的是『信号级测到了什么』与『这个账户能不能实现』，"
                    "两个问题各有各的答案"),
        "source": ["hypotheses.result (best/capacity/stress/cpcv)",
                   "service/research/capital.py:1-20"],
    }
    return out


def slim_reading(reading: dict) -> dict[str, Any]:
    """Per-row numeric summary; the repeated prose lives once in
    ``ledger.alpha_reading_note``."""
    if reading.get("status") != "measured":
        return {"status": reading.get("status")}
    signal = reading.get("signal_book") or {}
    account = reading.get("account_implementable") or {}
    sensitivity = reading.get("cost_sensitivity") or {}
    return {
        "status": "measured",
        "signal_sharpe": signal.get("sharpe"),
        "signal_mean_excess": signal.get("mean_excess_per_trade"),
        "cpcv_oos_sharpe_mean": signal.get("cpcv_oos_sharpe_mean"),
        "implementable": account.get("implementable"),
        "account_equity_cny": account.get("account_equity_cny"),
        "capital_one_lot_each_cny": account.get("capital_one_lot_each_cny"),
        "max_names_affordable": account.get("max_names_affordable"),
        "sharpe_at_2x_cost": account.get("sharpe_at_2x_cost"),
        "sharpe_at_3x_cost": account.get("sharpe_at_3x_cost"),
        "collapses_between_1x_and_2x": sensitivity.get("collapses_between_1x_and_2x"),
        "detail": "完整 basis / warning / reading 见 alpha_reading_note",
    }
