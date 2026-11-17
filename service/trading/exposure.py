"""Portfolio exposure: what the book is actually exposed to, not just how much.

A position count and a cash ratio do not answer "am I accidentally long one
theme?". This module answers it with industry/board weights, concentration and a
beta against the equal-weight market, using only stored data.

The guard uses ``industry_weight`` so a single ticket cannot push one industry
past its cap; the API uses ``describe`` so the model can see the whole picture.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from service.kernel.models import D, money


def board_of(symbol: str) -> str:
    """Which board a name belongs to, for the exposure buckets.

    ETF and convertible-bond codes used to fall through to 沪主板/深主板, so an
    `Exposure.by_board` table counted 510300.SH as a Shanghai main-board equity and
    a convertible as a Shenzhen one — three asset classes in two buckets.
    """
    code = symbol.split(".")[0]
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    if code.startswith(("11", "12", "13")):
        return "可转债"
    if code.startswith(("15", "16", "18", "50", "51", "52", "53", "56", "58")):
        return "ETF"
    return "沪主板" if symbol.endswith(".SH") else "深主板"


@dataclass(frozen=True)
class Exposure:
    as_of: str
    equity: float
    invested: float
    cash_weight: float
    by_industry: dict[str, float]
    by_board: dict[str, float]
    positions: list[dict]
    top1: float
    top3: float
    hhi: float
    beta: float | None

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "equity": round(self.equity, 2),
            "invested_weight": round(self.invested, 4),
            "cash_weight": round(self.cash_weight, 4),
            "by_industry": {k: round(v, 4) for k, v in sorted(
                self.by_industry.items(), key=lambda kv: -kv[1])},
            "by_board": {k: round(v, 4) for k, v in sorted(
                self.board_items(), key=lambda kv: -kv[1])},
            "positions": self.positions,
            "concentration": {"top1": round(self.top1, 4), "top3": round(self.top3, 4),
                              "hhi": round(self.hhi, 4)},
            "beta_vs_equal_weight": None if self.beta is None else round(self.beta, 3),
            **({} if self.beta is not None else {"beta_vs_equal_weight_note":
               # 空仓没有仓位可加权，beta 不适用——和"beta 算不出来"是两件事。
               "空仓：没有仓位可加权，beta 不适用" if not self.positions
               else "持仓的历史不足以估 beta（次新股 / 停牌太久）"}),
            "notes": [
                "weights are market value / equity, cash included in the denominator",
                "beta uses the equal-weight market over 60 sessions; it is a proxy, not a factor model",
            ],
        }

    def board_items(self) -> list[tuple[str, float]]:
        return list(self.by_board.items())


def _industry_map(market) -> dict[str, str]:
    basic = market.basic()
    if basic.empty:
        return {}
    return dict(zip(basic["ts_code"].astype(str), basic["industry"].astype(str)))


def _beta(market, as_of: str, symbols: list[str], positions: dict[str, float],
          lookback: int = 60) -> float | None:
    if not symbols:
        return None
    # Rounded to three decimals before it becomes a key. At eight, every mark moved
    # the string, so each profile/risk call created a new entry in the shared
    # 512-slot cache — and evicting the least-recently-used quarter of that cache
    # threw out the year partitions that cost 1.2 s each to decode.
    weights = ','.join(f"{symbol}:{weight:.3f}" for symbol, weight in sorted(positions.items()))
    key = f"beta:{as_of}:{lookback}:{weights}"
    if hasattr(market, "_cached"):
        return market._cached(key, lambda: _beta_uncached(market, as_of, positions, lookback))
    return _beta_uncached(market, as_of, positions, lookback)


def _beta_uncached(market, as_of: str, positions: dict[str, float],
                   lookback: int = 60) -> float | None:
    panel = market.panel(as_of, lookback + 1)
    if panel.empty:
        return None
    col = "adj_close" if "adj_close" in panel.columns else "close"
    p = panel[["trade_date", "ts_code", col]].copy().rename(columns={col: "px"})
    p["trade_date"] = p["trade_date"].astype(str)
    p["ret"] = p.groupby("ts_code")["px"].pct_change()
    wide = p.pivot_table(index="trade_date", columns="ts_code", values="ret", aggfunc="last")
    wide = wide.sort_index().dropna(how="all")
    if len(wide) < 20:
        return None
    mkt = wide.mean(axis=1)
    held = [s for s in positions if s in wide.columns]
    if not held:
        return None
    port = sum(positions[s] * wide[s].fillna(0.0) for s in held)
    var = float(mkt.var())
    if var <= 0:
        return None
    return float(np.cov(port.fillna(0.0), mkt)[0, 1] / var)


def describe(book, market, as_of: str | None = None) -> Exposure:
    day = as_of or market.latest_bar_date()
    summary = book.summary()
    equity = float(summary["equity"]) or 1.0
    industries = _industry_map(market)
    by_industry: dict[str, float] = {}
    by_board: dict[str, float] = {}
    positions: list[dict] = []
    weights: dict[str, float] = {}
    for pos in summary["positions"]:
        mv = float(pos["market_value"])
        w = mv / equity
        symbol = pos["symbol"]
        industry = industries.get(symbol, "未知")
        board = board_of(symbol)
        by_industry[industry] = by_industry.get(industry, 0.0) + w
        by_board[board] = by_board.get(board, 0.0) + w
        weights[symbol] = w
        pnl = float(pos.get("unrealized_pnl") or 0.0)
        cost = float(pos.get("avg_cost") or 0.0) * int(pos.get("qty") or 0)
        positions.append({"symbol": symbol, "name": pos.get("name"), "industry": industry,
                          "board": board, "weight": round(w, 4),
                          "unrealized_pnl_pct": round(pnl / cost, 4) if cost else None,
                          "available": pos.get("available_qty")})
    ordered = sorted((p["weight"] for p in positions), reverse=True)
    cash = float(summary["cash_available"]) + float(summary.get("cash_frozen") or 0)
    return Exposure(
        as_of=str(day),
        equity=float(summary["equity"]),
        invested=1.0 - cash / equity,
        cash_weight=cash / equity,
        by_industry=by_industry,
        by_board=by_board,
        positions=positions,
        top1=ordered[0] if ordered else 0.0,
        top3=sum(ordered[:3]),
        hhi=float(sum(w * w for w in ordered)),
        beta=_beta(market, str(day), list(weights), weights),
    )


def industry_weight(summary: dict, market, industry: str,
                    extra_value: D | None = None) -> float:
    """Share of equity in one industry, including a proposed order."""
    equity = float(summary["equity"]) or 1.0
    if not industry or industry == "未知":
        return 0.0
    industries = _industry_map(market)
    total = 0.0
    for pos in summary["positions"]:
        if industries.get(pos["symbol"], "未知") == industry:
            total += float(pos["market_value"])
    return (total + float(money(extra_value if extra_value is not None else D("0")))) / equity
