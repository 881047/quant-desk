"""Deterministic candidate screen.

The screen answers "what is worth looking at today", not "what to buy". It
applies hard tradability filters (universe, price, liquidity, not at the limit)
and ranks by 20-day momentum with a liquidity tie-breaker. The LLM may pick from
the list or reject all of them — but it cannot screen by inventing numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from service import settings
from service.adapters.symbols import board_of, limit_pct
from service.kernel.units import SHARES_PER_LOT

from .market import MarketRepository


def _num(key: str):
    return field(default_factory=lambda: settings.num(f"screen.{key}"))


def _int(key: str):
    return field(default_factory=lambda: settings.integer(f"screen.{key}"))


@dataclass(frozen=True)
class ScreenConfig:
    """The screen's thresholds, read from configs/params.yaml (screen.*).

    These were a dataclass with literal defaults *and* a ``configs/universe.yaml``
    holding the same numbers. The yaml was never read, so editing it did nothing;
    the dataclass won every time. There is now one place, and it is the yaml.
    """

    max_price: float = _num("max_price")      # a 5,000 CNY account must afford 100 shares
    min_adv_cny: float = _num("min_adv_cny")
    min_list_days: int = _int("min_list_days")
    exclude_st: bool = field(default_factory=lambda: settings.flag("screen.exclude_st"))
    exclude_boards: tuple[str, ...] = field(
        default_factory=lambda: settings.seq("screen.exclude_boards"))
    max_candidates: int = _int("max_candidates")
    momentum_days: int = _int("momentum_days")
    max_abs_pct: float = _num("max_abs_pct")  # don't chase a limit-up close
    rank_weight_momentum: float = _num("rank_weight_momentum")


def affordable_price(equity: float, weight: float | None = None) -> float:
    """Highest share price where one 100-share lot fits the target weight."""
    share = settings.num("screen.affordable_weight") if weight is None else weight
    return max(1.0, equity * share / SHARES_PER_LOT)


def screen(market: MarketRepository, date: str,
           config: ScreenConfig | None = None) -> list[dict]:
    """Cached per (day, config): the screen reads 60 sessions of bars."""
    cfg = config or ScreenConfig()
    if hasattr(market, "_cached"):
        return market._cached(f"screen:{date}:{cfg}", lambda: _screen(market, date, cfg))
    return _screen(market, date, cfg)


def _screen(market: MarketRepository, date: str, cfg: ScreenConfig) -> list[dict]:
    daily = market.daily(date)
    if daily.empty:
        return []
    limits = market.limits(date)
    basic = market.basic()
    adv = market.adv_map(date, 20)
    panel = market.panel(date, cfg.momentum_days + 1)

    df = daily.copy()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["pre_close"] = pd.to_numeric(df["pre_close"], errors="coerce")
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce")
    df = df[(df["close"] > 0) & (df["vol"] > 0) & (df["close"] <= cfg.max_price)]
    codes = df["ts_code"].astype(str)
    excluded = pd.Series(False, index=df.index)
    for token in cfg.exclude_boards:
        excluded |= codes.str.endswith(token) if token.startswith(".") else codes.str.startswith(token)
    df = df[~excluded]
    if limits is not None and not limits.empty:
        # ``validate="m:1"`` and a de-dup on the right frame: a metadata table
        # re-synced (or backfilled twice) carries two rows for one code, the
        # merge fans out, and ``set_index("ts_code")`` below then refuses a
        # non-unique index — a whole screen failing on a duplicate it never uses.
        df = df.merge(limits[["ts_code", "up_limit", "down_limit"]],
                      on="ts_code", how="left", validate="m:1")
        up = pd.to_numeric(df["up_limit"], errors="coerce")
        df = df[~(up.gt(0) & (df["close"] >= up * 0.999))]

    if not basic.empty:
        meta = basic[["ts_code", "name", "list_date"]].copy()
        meta["list_date"] = meta["list_date"].astype(str)
        df = df.merge(meta, on="ts_code", how="left", validate="m:1")
        cutoff = (pd.Timestamp(date) - pd.Timedelta(days=cfg.min_list_days)).strftime("%Y%m%d")
        valid_list_date = df["list_date"].str.fullmatch(r"\d{8}", na=False)
        df = df[valid_list_date & (df["list_date"] <= cutoff)]

    df["adv20"] = df["ts_code"].map(adv)
    df = df[df["adv20"].fillna(0) >= cfg.min_adv_cny]

    pct = (df["close"] / df["pre_close"] - 1) * 100
    df = df[pct.abs() <= cfg.max_abs_pct]

    # One pass over the name-change table instead of one per code: the loop
    # asked 2,000 times and spent 4.7s doing it. Computed even when ST names are
    # kept, because the tag is also what decides the 5% band on the row below —
    # and a caller reading `band: 0.05` next to a 10% board is the only way to
    # see that "down 4%" is already close to the floor.
    flags = market.universe.st_map(df["ts_code"], date)
    if cfg.exclude_st:
        keep = [not flags.get(code, False) for code in df["ts_code"]]
        df = df[pd.Series(keep, index=df.index)]

    if df.empty or panel.empty:
        return []

    # Momentum is two dates, so it has to run on the adjusted series; the
    # `close` reported back to the caller stays the price an order pays.
    mom_col = "adj_close" if "adj_close" in panel.columns else "close"
    window = panel.pivot_table(index="trade_date", columns="ts_code",
                               values=mom_col, aggfunc="last").sort_index()
    window = window.reindex(columns=[c for c in df["ts_code"] if c in window.columns])
    if len(window) < 2:
        return []
    first = window.iloc[0]
    last = window.iloc[-1]
    momentum = (last / first - 1).replace([float("inf"), float("-inf")], pd.NA)

    df = df.set_index("ts_code")
    df["mom20"] = pd.to_numeric(momentum, errors="coerce")
    df = df.dropna(subset=["mom20"])
    if df.empty:
        return []

    df["mom_rank"] = df["mom20"].rank(pct=True)
    df["liq_rank"] = df["adv20"].rank(pct=True)
    # 排序键，不是质量评分。它决定「哪 N 只进候选」，不回答「哪只更值得买」。
    # 所以对外叫 rank_key，并把两个原始分位一起给：读者可以自己按任何一个
    # 维度重排，也可以不同意这个权重。
    #
    # 权重现在在 configs/params.yaml（screen.rank_weight_momentum）。写死在这里
    # 的那一版是一份没有出处的意见——看不见、改不动、也没法被审计引用。
    w_mom = cfg.rank_weight_momentum
    df["rank_key"] = w_mom * df["mom_rank"] + (1.0 - w_mom) * df["liq_rank"]

    df = df.sort_values(["rank_key", "adv20"], ascending=False).head(cfg.max_candidates)
    out = []
    for symbol, row in df.iterrows():
        board = board_of(symbol)
        out.append({
            "symbol": symbol,
            "name": str(row.get("name", "")),
            # The row's price is the close of ``as_of``, not the live tape. On
            # 2026-10-30 a 14:58 run read 600825 at 14.84 from here while it was
            # locked limit-down at 13.36 — the run only caught it because the
            # automation memory happened to record the trap. Carrying the date on
            # every row means a reader can see the staleness without that luck.
            "as_of": date,
            "close": round(float(row["close"]), 2),
            "pct_chg": round(float((row["close"] / row["pre_close"] - 1) * 100), 2),
            "mom20": round(float(row["mom20"]) * 100, 2),
            "adv20_cny": int(row["adv20"]),
            # 板块与涨跌幅：候选里 600 开头和 300 开头不是同一笔交易——一个
            # 10% 天花板、一个 20%，止损距离与"离涨停还有多远"都要按它算。
            # 不写出来，读者只能从代码前缀推——而 ST 在 2026-07-06 之前确实
            # 破掉了这条捷径（主板 ST 曾是 5%，现在与主板同为 10%）。
            "board": board,
            "band": float(limit_pct(
                symbol,
                is_st=bool(flags.get(symbol, False)),
                name=str(row.get("name", "")),
                as_of=date,
            )),
            "rank_key": round(float(row["rank_key"]), 3),
            "mom20_pct": round(float(row["mom_rank"]), 3),
            "adv20_pct": round(float(row["liq_rank"]), 3),
            "why": f"20日动量 {float(row['mom20'])*100:+.1f}%，20日均额 {row['adv20']/1e8:.1f} 亿",
        })
    return out
