"""Declarative research specs -> trade tables -> daily return series.

The agent may not write code into the research path. A hypothesis carries a
small JSON spec, and this module turns it into a trade list with explicit fill
assumptions. Everything is data-driven: no eval, no user Python.

Fill realism that the first pass of this project got wrong:

- a next-open entry is **skipped** when the open sits at the upper limit
  (一字板 / 开盘涨停) because nobody could buy there;
- an entry suspended on the planned day rolls to the first actual later bar;
- ST names and Beijing listings are excluded by default (different rules);
- costs come from the **kernel's** ``CostModel``, per ticket: the CNY 5 minimum
  commission is the dominant term for a CNY 5,000 account, and a flat bps fee
  understates it by 2-6x. ``cost_bps`` is now slippage only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from service import settings
from service.kernel.costs import CostModel
from service.kernel.microstructure import PRICE_EPS as _PRICE_EPS_DEC
from service.kernel.models import Board, D, Instrument
from service.kernel.units import CB_SHARES_PER_LOT

# The tolerance is shared with the kernel so "opened at the limit" means the same
# thing on both sides of the desk. Float here only because the panel is float.
PRICE_EPS = float(_PRICE_EPS_DEC)
# How far a trade may be held, in trading sessions. This used to be 2, which
# quietly made the referee incapable of judging the only structure that ever
# survived it: the core ETF book holds for weeks. It also forced every research
# question into the zone where round-trip cost is 30–100% of the gross edge.
# 60 covers "swing" and "position" horizons; intraday is a different engine.
MAX_HOLD = settings.integer("research.max_hold")
# Only the **entry** bar is materialised as a forward column. Exits are resolved
# by walking each symbol's actual bars (`_resolve_exit`), and the attribution
# benchmark shifts on demand. Materialising every k up to MAX_HOLD would add 310
# columns to a 5M-row panel — about 12 GB — to answer a question that needs one
# number per event.
FORWARD_BARS = settings.integer("research.forward_bars")


class SpecError(ValueError):
    """Invalid research spec."""


def blocked_at_open_mask(open_px: pd.Series, up_limit: pd.Series) -> pd.Series:
    """Vectorised buy-side half of ``microstructure.blocked_at_open``.

    Same rule, evaluated over a column instead of one bar. The scalar version in
    ``service/kernel/microstructure.py`` is what the matcher uses; a parity test
    runs both across the daily band so the two cannot drift apart.
    """
    px = pd.to_numeric(open_px, errors="coerce")
    limit = pd.to_numeric(up_limit, errors="coerce")
    # ``limit > 0`` mirrors the scalar guard: an empty limit column means "unknown",
    # not "the ceiling is zero". Without it the two evaluations disagree exactly
    # on the rows a missing column produces.
    return limit.notna() & px.notna() & (limit > 0) & (px >= limit - PRICE_EPS)


@dataclass(frozen=True)
class EventStudy:
    """Buy a pool event, exit after ``hold`` trading days."""

    pool: str = "limit_up"
    boards: tuple[int, ...] = (1,)
    # T 日收盘买入：打板 / 尾盘抢筹唯一能用日线表达的入场点。
    #
    # 这个入场是**乐观**的：收盘封在涨停价时，`close` 就是 `limit_price`，
    # 而真实成交要求排到队。
    #
    # 2026-11-03 晚修正过一次：当时这里写"首板 T 收→T+1 收 +1.75%，隔夜 +2.00%"，
    # 那是**只统计收盘仍封的票**（`limit_up` 池）的结果，而封住恰恰是买入之后才知道
    # 的事。按"当日触板"的无偏口径，首板隔夜是 −0.34%，因为限价单只在价格跌破
    # 涨停价时成交——一字板（从未跌破，+7.01%）买不到，买得到的那批期望为负。
    # 详见 `board._base_rate` 的 buyable / one_word 分组。用这个 entry 测"打板"时，
    # 结论必须按 `fill_model` 读，不能按无条件均值读。
    entry: str = "next_open"          # next_open | next_close | t_close | next_limit
    # 出场在「入场那根之后」的第 hold 根 K 线：next_open 的入场在 T+1，所以
    # hold=1 的出场是 T+2；t_close 的入场在 T 本身，hold=1 的出场是 T+1。
    # 两端用同一条定义，才不会把「隔夜单」算成「持有两天」。
    hold: int = 1                     # 0 只对 T+0 品种（转债）合法
    exit_at: str = "close"            # close | open (open needs hold >= 1)
    # 限价挂单在 T+1 的最低价上：`limit_drop_pct` 是挂单价相对 T 收盘价的折让。
    # next_limit 用日线 OHLC 就能验证成交（`low_f1 <= 限价` 即成交），因为
    # **限价单是等价格来找你**，不是追着买。这和排板相反：排板要排队优先权，
    # 低吸不用。开盘已跌破挂单价时按开盘成交（更优价格）。
    limit_drop_pct: float = 0.03      # 只用 entry=next_limit 时读
    cost_bps: float = 10.0            # **slippage** per side; broker fees are added below
    cost_mult: float = 1.0            # stress multiplier on the whole cost, not just slippage
    ticket_cny: float = 0.0           # 0 = equity / signals-per-day, i.e. an equal-weight book
    start: str = "20230101"
    end: str = "20261023"
    exclude_st: bool = True
    exclude_bj: bool = True
    min_amount: float = 0.0           # minimum pool-day turnover, CNY
    max_per_day: int = 0              # 0 = every signal
    rank_by: str = "boards"           # boards | amount | pct_chg | close_to_limit
    rank_dir: str = "desc"
    gap_threshold: float = 0.03       # for pool=cb: keep names whose T0 return <= -this
    link_pool: str = "limit_up"       # for pool=cb_link: which stock event triggers the bond
    lot: int = 100                    # trading unit: 100 shares for stocks/ETFs, 10 for CBs
    label: str = ""

    def with_cost(self, cost_bps: float) -> EventStudy:
        """Change the slippage assumption. Kept for callers that mean "slippage"."""
        return EventStudy(**{**self.__dict__, "cost_bps": cost_bps, "label": self.label})

    def with_cost_mult(self, mult: float) -> EventStudy:
        """Stress the *whole* cost — fees and slippage — not just the bps term."""
        return EventStudy(**{**self.__dict__, "cost_mult": mult, "label": self.label})

    def with_variant(self, **kwargs) -> EventStudy:
        return EventStudy(**{**self.__dict__, **kwargs})

    def name(self) -> str:
        boards = "+".join(str(b) for b in self.boards)
        tail = "x" if self.exit_at == "open" else "c"
        # 折让要进名字：同一池子两档折让是两个假设，同名会在 registry 里
        # 互相覆盖，而"覆盖"看起来像"同一条判决"。
        drop = (f"@d{self.limit_drop_pct * 100:g}"
                if self.entry == "next_limit" else "")
        return self.label or f"{self.pool}:b{boards}:{self.entry}{drop}:h{self.hold}{tail}"


_ALLOWED = {
    "pool", "boards", "entry", "hold", "exit_at", "cost_bps", "cost_mult", "ticket_cny",
    "start", "end", "gap_threshold", "lot", "link_pool", "limit_drop_pct",
    "exclude_st", "exclude_bj", "min_amount", "max_per_day", "rank_by", "rank_dir", "label",
}
_POOLS = {"limit_up", "broken_board", "limit_down", "losers", "winners",
          "cb", "cb_link"}
# Convertible bonds settle T+0, so a same-day exit is legal for them.
T0_POOLS = {"cb", "cb_link"}
_LINKS = {"limit_up", "limit_down", "broken_board"}
_ENTRIES = {"next_open", "next_close", "t_close", "next_limit"}
_EXITS = {"close", "open"}
_RANKS = {"boards", "amount", "pct_chg", "close_to_limit", "cb_ret", "premium"}


def parse_spec(text: str | dict | EventStudy | None) -> EventStudy:
    """The single validation/normalisation entry for every research caller."""
    if isinstance(text, EventStudy):
        raw = dict(text.__dict__)
        # 对象再解析一次（build_trades 会这么做）时，每个字段都在——包括默认值。
        # "调用方写了这个键"和"dataclass 有默认值"要分开，否则守卫会把
        # `EventStudy(entry="next_open")` 判成"你多写了一个没用的参数"。
        explicit = False
    elif text in (None, "", {}):
        raw = {}
        explicit = True
    elif isinstance(text, str):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SpecError(f"test_spec must be JSON: {exc}") from exc
        explicit = True
    else:
        raw = dict(text)
        explicit = True
    if not isinstance(raw, dict):
        raise SpecError("test_spec must be a JSON object")

    unknown = set(raw) - _ALLOWED
    if unknown:
        raise SpecError(f"unknown spec keys: {sorted(unknown)}")
    if raw.get("pool", "limit_up") not in _POOLS:
        raise SpecError(f"pool must be one of {sorted(_POOLS)}")
    if raw.get("entry", "next_open") not in _ENTRIES:
        raise SpecError(f"entry must be one of {sorted(_ENTRIES)}")
    hold = int(raw.get("hold", 1))
    if not 0 <= hold <= MAX_HOLD:
        raise SpecError(f"hold must be within 0..{MAX_HOLD} trading days")
    entry = str(raw.get("entry", "next_open"))
    exit_at = str(raw.get("exit_at", "close"))
    if exit_at not in _EXITS:
        raise SpecError(f"exit_at must be one of {sorted(_EXITS)}")
    if exit_at == "open" and hold < 1:
        raise SpecError("exit_at=open needs hold>=1: the next open is the first exit you can reach")
    if entry == "next_open" and hold == 0 and str(raw.get("pool", "limit_up")) not in T0_POOLS:
        raise SpecError(
            "A-share T+1: buying at the next open and selling that day is not allowed. "
            "Use hold>=1 (exit the following day) or a T+0 instrument (pool=cb)."
        )
    if entry == "next_close" and hold == 0:
        raise SpecError("entry=next_close with hold=0 is a zero-length trade")
    # t_close 的入场在 T 收盘，hold=0 会退化成"同一根买入并卖出"，没有持仓段。
    if entry == "t_close" and hold == 0:
        raise SpecError("entry=t_close with hold=0 is a zero-length trade; use hold>=1")
    # next_limit 的入场在 T+1，hold=0 同样退化成同一根进出。
    if entry == "next_limit" and hold == 0:
        raise SpecError("entry=next_limit with hold=0 is a zero-length trade; use hold>=1")
    # 折让必须是正的小数：0 表示"挂在 T 收盘价"，那在 T+1 是市价单不是限价单；
    # >=1 表示挂在 0 元以下，永远不成交。
    drop = float(raw.get("limit_drop_pct", 0.03))
    if not 0.0 < drop < 1.0:
        raise SpecError("limit_drop_pct must be within (0, 1): it is the discount "
                        "below the signal-day close where the limit order sits")
    # 只在 next_limit 下有意义。允许它在别的 entry 上静默存在，等于让一个打错的
    # 参数看起来"已经调过了"——这类静默无操作是这个仓库反复吃亏的形态。
    if explicit and "limit_drop_pct" in raw and entry != "next_limit":
        raise SpecError("limit_drop_pct only applies to entry=next_limit; "
                        f"got entry={entry}")
    boards = raw.get("boards", [1])
    if isinstance(boards, int):
        boards = [boards]
    boards = tuple(int(b) for b in boards)
    if not boards or any(b < 0 or b > 20 for b in boards):
        raise SpecError("boards must be a non-empty list within 0..20")
    if str(raw.get("link_pool", "limit_up")) not in _LINKS:
        raise SpecError(f"link_pool must be one of {sorted(_LINKS)}")
    if raw.get("rank_by", "boards") not in _RANKS:
        raise SpecError(f"rank_by must be one of {sorted(_RANKS)}")
    if raw.get("rank_dir", "desc") not in {"desc", "asc"}:
        raise SpecError("rank_dir must be desc or asc")
    if float(raw.get("cost_mult", 1.0)) <= 0:
        raise SpecError("cost_mult must be positive")
    if float(raw.get("ticket_cny", 0.0)) < 0:
        raise SpecError("ticket_cny must be >= 0 (0 means auto from equity and signal count)")
    if float(raw.get("cost_bps", 10.0)) < 0:
        raise SpecError("cost_bps (slippage) must be >= 0")

    return EventStudy(
        pool=str(raw.get("pool", "limit_up")),
        boards=boards,
        entry=entry,
        hold=hold,
        exit_at=exit_at,
        limit_drop_pct=drop,
        cost_bps=float(raw.get("cost_bps", 10.0)),
        cost_mult=float(raw.get("cost_mult", 1.0)),
        ticket_cny=float(raw.get("ticket_cny", 0.0)),
        start=str(raw.get("start", "20230101")),
        end=str(raw.get("end", "20261023")),
        exclude_st=bool(raw.get("exclude_st", True)),
        exclude_bj=bool(raw.get("exclude_bj", True)),
        min_amount=float(raw.get("min_amount", 0.0)),
        max_per_day=int(raw.get("max_per_day", 0)),
        rank_by=str(raw.get("rank_by", "boards")),
        rank_dir=str(raw.get("rank_dir", "desc")),
        gap_threshold=float(raw.get("gap_threshold", 0.03)),
        link_pool=str(raw.get("link_pool", "limit_up")),
        lot=int(raw.get("lot", 10 if str(raw.get("pool")) in T0_POOLS else 100)),
        label=str(raw.get("label", "")),
    )


# ---------------------------------------------------------------------- costs
COST_MODEL = CostModel()
# Convertible bonds are not stocks for fee purposes: no stamp duty, no transfer
# fee, and a 10-unit lot. Those are zero *rates on the instrument*, which is now
# the only thing CostModel reads; `is_etf` no longer decides the tax.
_CB_LOT = CB_SHARES_PER_LOT


def fee_instrument(pool: str) -> Instrument:
    if pool in T0_POOLS:
        # ``board=CB`` is what makes ``asset`` say "cb" rather than "etf"; the
        # fee schedule is identical, but the slippage and position caps are not.
        return Instrument(symbol="CB", board=Board.CB, is_etf=True,
                          min_qty=_CB_LOT, qty_step=_CB_LOT, t_plus=0,
                          stamp_duty=D("0"), transfer_fee=D("0"))
    return Instrument(symbol="STOCK", board=Board.MAIN, min_qty=100, qty_step=100)


def effective_cost_bps(spec: EventStudy, ticket_cny: float) -> dict:
    """All-in cost of one round trip at this ticket size, in basis points.

    The CNY 5 minimum commission is 50bps on a CNY 1,000 ticket and 10bps on a
    CNY 5,000 one, so a flat bps assumption cannot be right for both. Fees come
    from the kernel; ``cost_bps`` is slippage only; ``cost_mult`` stresses both.
    """
    if ticket_cny <= 0:
        raise SpecError("ticket_cny must be positive")
    instrument = fee_instrument(spec.pool)
    notional = D(str(ticket_cny))
    fees = (COST_MODEL.buy_fees(notional, instrument)
            + COST_MODEL.sell_fees(notional, instrument))
    fee_bps = float(fees) / ticket_cny * 10_000
    slippage_bps = 2 * spec.cost_bps
    total_bps = (fee_bps + slippage_bps) * spec.cost_mult
    return {
        "fee_cny": float(fees),
        "fee_bps": round(fee_bps, 2),
        "slippage_bps": round(slippage_bps, 2),
        "total_bps": round(total_bps, 2),
        "ticket_cny": round(float(ticket_cny), 2),
        "cost_mult": spec.cost_mult,
        "model": "kernel",
    }


# --------------------------------------------------------------------- prices
def _adjusted_from_pre_close(bars: pd.DataFrame) -> pd.Series:
    """Back-adjusted close built from the ``pre_close`` chain.

    ``daily`` has no ``adj_close`` and no ``adj_factor`` column, so every study
    so far ran on raw prices while ``price_basis`` said "unadjusted estimation"
    and a guard that claimed to refuse corporate actions never fired — because
    the factor it compared was always NaN.

    ``pre_close`` is the *adjusted* previous close: on an ex-date it already
    reflects the dividend or the split, so ``close / pre_close`` is the true
    total return of that session with no jump. Compounding it and anchoring the
    last session to its own raw close produces a back-adjusted series whose
    ratio between two dates is a total return, in today's price units.

    The anchor is the last close so that ``adj_close`` stays comparable to the
    price a reader sees; the level is arbitrary, the ratio is not.
    """
    if bars.empty:
        return pd.Series(dtype="float64", index=bars.index)
    gross = pd.to_numeric(bars["close"], errors="coerce") / pd.to_numeric(
        bars["pre_close"], errors="coerce")
    # A missing or non-positive pre_close cannot describe a return; carry the
    # factor flat instead of inventing a jump.
    gross = gross.replace([np.inf, -np.inf], np.nan)
    gross = gross.where(np.isfinite(gross) & (gross > 0), 1.0).fillna(1.0)
    grouped = gross.groupby(bars["ts_code"], sort=False)
    cum = grouped.cumprod()
    last_cum = cum.groupby(bars["ts_code"], sort=False).transform("last")
    last_close = pd.to_numeric(bars["close"], errors="coerce").groupby(
        bars["ts_code"], sort=False).transform("last")
    scaled = last_close * cum / last_cum.replace(0, np.nan)
    return scaled.where(np.isfinite(scaled) & (scaled > 0))


def load_panel(store, start: str, end: str, dataset: str = "daily") -> pd.DataFrame:
    """Bars with forward bars per code, plus the limit price where it exists."""
    bars = store.read(dataset, start, end)
    if bars.empty:
        raise SpecError(f"no {dataset} bars in {start}..{end}")
    # `low` 只有在入场用限价挂单时才需要（next_limit 的成交判据是 low <= 挂单价），
    # 但它必须在这里就带上：前推列 low_f{k} 是在这个子集上算的，事后补不上。
    cols = ["ts_code", "trade_date", "open", "low", "close", "pre_close", "amount",
            "adj_close", "adj_factor"]
    bars = bars[[c for c in cols if c in bars.columns]].copy()
    bars["trade_date"] = bars["trade_date"].astype(str)
    # Some vendors ship open=0 for suspended/illiquid bars; a zero price would
    # make every ratio infinite, so treat it as missing. Inf is corrupt input,
    # not a missing bar, and must not be silently averaged out of a study.
    for col in ("open", "low", "close", "pre_close", "adj_close", "adj_factor"):
        if col in bars.columns:
            value = pd.to_numeric(bars[col], errors="coerce")
            if np.isinf(value.to_numpy(dtype=float)).any():
                raise SpecError(f"{col} contains Inf in {start}..{end}")
            bars[col] = value.where(value > 0)
    if "adj_close" not in bars:
        if "adj_factor" in bars:
            # Deliberate, and pinned by
            # `test_load_panel_preserves_and_builds_adjusted_close`: a frame that
            # ships its own factor is adjusted with it. No production dataset is
            # in that shape today (`daily`/`cb_daily` carry `pre_close` only,
            # `adj_factor`/`fund_adj` carry the factor without prices), so this
            # branch does not run — but it is a contract, not dead code.
            #
            # Worth knowing: it disagrees with `market._attach_adj`, which
            # refuses the vendor factor because it resets (920227.BJ fell from
            # 1073.15 to 1.1441 on 2026-10-26 while the price moved 47.96 ->
            # 47.91). If a dataset ever gains an `adj_factor` column, the
            # research layer would silently switch to the series the decision
            # layer rejects. Left as-is on purpose: changing it is a research
            # change, not a cleanup.
            bars["adj_close"] = bars["close"] * bars["adj_factor"]
        else:
            bars["adj_close"] = _adjusted_from_pre_close(bars)

    limits = store.read("stk_limit", start, end) if dataset == "daily" else pd.DataFrame()
    if not limits.empty:
        keep = [c for c in ("ts_code", "trade_date", "up_limit", "down_limit")
                if c in limits.columns]
        limits = limits[keep].copy()
        limits["trade_date"] = limits["trade_date"].astype(str)
        bars = bars.merge(limits, on=["ts_code", "trade_date"], how="left")
    else:
        bars["up_limit"] = np.nan
        bars["down_limit"] = np.nan
    if "down_limit" not in bars.columns:
        bars["down_limit"] = np.nan

    bars = bars.sort_values(["ts_code", "trade_date"], kind="mergesort").reset_index(drop=True)
    g = bars.groupby("ts_code", sort=False)
    # Forward columns serve entry and attribution. Actual exits walk the full
    # symbol history in _resolve_exit, so long suspensions are not truncated.
    for k in range(1, FORWARD_BARS + 1):
        bars[f"date_f{k}"] = g["trade_date"].shift(-k)
        bars[f"open_f{k}"] = g["open"].shift(-k)
        # `low_f{k}` 只有 next_limit 用得上。按存在性造，缺列时其它 entry 照常工作，
        # 而 next_limit 会在 build_trades 里得到一句"这个数据集没有 low"。
        if "low" in bars:
            bars[f"low_f{k}"] = g["low"].shift(-k)
        bars[f"close_f{k}"] = g["close"].shift(-k)
        bars[f"limit_f{k}"] = g["up_limit"].shift(-k)
        bars[f"down_f{k}"] = g["down_limit"].shift(-k)
        if "adj_close" in bars:
            bars[f"adj_f{k}"] = g["adj_close"].shift(-k)
    bars["close_p1"] = g["close"].shift(1)
    bars["pre_close_f1"] = g["pre_close"].shift(-1)
    if "adj_close" in bars:
        bars["adj_p1"] = g["adj_close"].shift(1)
    # A row whose ``pre_close`` differs from the previous raw close is an ex-date:
    # the exchange re-based the price overnight. Comparing the two is the exact
    # detector; comparing an adjustment *factor* only works when the vendor ships
    # one, which this store does not.
    if "close_p1" in bars.columns:
        diff = (pd.to_numeric(bars["pre_close"], errors="coerce")
                - pd.to_numeric(bars["close_p1"], errors="coerce"))
        bars["ex_date"] = diff.abs().gt(1e-6).fillna(False)
    return bars


# A whole-market event, not a limit-up pool: the classic short-term reversal
# (big down day) and momentum (big up day) tests, both T+1 feasible.
SCAN_THRESHOLD = settings.num("research.scan_threshold")
SCAN_MIN_AMOUNT_CNY = settings.num("research.scan_min_amount")


def scan_pool(panel: pd.DataFrame, kind: str, threshold: float | None = None,
              min_amount_cny: float | None = None) -> pd.DataFrame:
    """Build a ``losers`` / ``winners`` pool from daily bars.

    Liquidity floor keeps the trade doable for a small account; the move is
    measured close-to-close so it is known at T0 close.
    """
    thr = SCAN_THRESHOLD if threshold is None else threshold
    floor = SCAN_MIN_AMOUNT_CNY if min_amount_cny is None else min_amount_cny
    columns = ["ts_code", "trade_date", "close", "close_p1", "amount"]
    if "adj_close" in panel.columns and "adj_p1" in panel.columns:
        columns += ["adj_close", "adj_p1"]
    p = panel[columns].copy()
    current = p["adj_close"] if "adj_close" in p else p["close"]
    previous = p["adj_p1"] if "adj_p1" in p else p["close_p1"]
    p["ret"] = current / previous - 1
    p["amount_cny"] = pd.to_numeric(p["amount"], errors="coerce") * 1000
    p = p[p["amount_cny"] >= floor]
    if kind == "losers":
        p = p[p["ret"] <= -thr]
    else:
        p = p[p["ret"] >= thr]
    if p.empty:
        return pd.DataFrame()
    p["code"] = p["ts_code"]
    p["pool"] = kind
    p["pct_chg"] = p["ret"] * 100
    p["boards"] = 0
    p["name"] = ""
    p["limit_price"] = np.nan
    p["trade_date"] = p["trade_date"].astype(str)
    return p


def cb_pool(panel: pd.DataFrame, threshold: float = 0.03) -> pd.DataFrame:
    """Convertible bonds that fell hard on T0 — the T+0 version of "losers".

    A same-day exit is legal here, so the intraday reversal the stock book cannot
    harvest is testable on this pool.
    """
    p = panel[["ts_code", "trade_date", "close", "close_p1", "amount"]].copy()
    p["ret"] = p["close"] / p["close_p1"] - 1
    p = p[p["ret"] <= -threshold]
    if p.empty:
        return pd.DataFrame()
    p["code"] = p["ts_code"]
    p["pool"] = "cb"
    p["pct_chg"] = p["ret"] * 100
    p["boards"] = 0
    p["name"] = ""
    p["limit_price"] = np.nan
    p["trade_date"] = p["trade_date"].astype(str)
    return p


def cb_link_pool(panel: pd.DataFrame, store, link_pool: str = "limit_up",
                 boards: tuple[int, ...] = ()) -> pd.DataFrame:
    """Bonds whose *underlying stock* hit a limit today.

    The signal is the stock event; the tradable is the bond. Bond liquidity is
    thinner, so the question is whether the stock's forced flow spills into the
    bond the next morning — and whether that spill is a gift or a trap.
    """
    basic = store.read("cb_basic")
    pools = store.read("limit_pool_t0")
    if basic.empty or pools.empty:
        return pd.DataFrame()
    ev = pools[pools["pool"] == link_pool].copy()
    if link_pool == "limit_up" and boards:
        ev = ev[pd.to_numeric(ev["boards"], errors="coerce").isin(boards)]
    ev = ev[~ev["name"].astype(str).str.upper().str.contains("ST", na=False)]
    if ev.empty:
        return pd.DataFrame()

    ev = ev.rename(columns={"close": "stk_close"})
    m = basic[["ts_code", "stk_code", "list_date", "delist_date", "conv_price"]].merge(
        ev[["code", "trade_date", "boards", "pct_chg", "stk_close"]], left_on="stk_code",
        right_on="code", how="inner")
    m["trade_date"] = m["trade_date"].astype(str)
    listed = m["list_date"].astype(str).fillna("")
    delisted = m["delist_date"].astype(str).fillna("99999999")
    m = m[(listed <= m["trade_date"]) & (delisted >= m["trade_date"])]
    if m.empty:
        return pd.DataFrame()

    px = panel[["ts_code", "trade_date", "close", "close_p1"]].copy()
    px["trade_date"] = px["trade_date"].astype(str)
    m = m.merge(px, on=["ts_code", "trade_date"], how="left")
    m["cb_ret"] = m["close"] / m["close_p1"] - 1
    # Conversion parity: a low premium means the bond behaves like the stock,
    # which is where a bounce should be transmitted most directly.
    # A missing or zero conversion price makes parity 0/∞ and the premium ±inf.
    # ``rank_metric`` only drops NaN, so those rows sorted to the very top of a
    # ``rank_by="premium"`` study and entered the P&L as the best signal in the
    # book — a bond that is being redeemed, priced as the richest.
    conv = pd.to_numeric(m["conv_price"], errors="coerce").where(lambda v: v > 0)
    parity = 100.0 / conv * pd.to_numeric(m["stk_close"], errors="coerce")
    premium = m["close"] / parity - 1
    m["premium"] = premium.replace([np.inf, -np.inf], np.nan)
    m["pool"] = "cb_link"
    m["name"] = ""
    m["limit_price"] = np.nan
    return m[["ts_code", "trade_date", "pool", "boards", "pct_chg", "cb_ret",
              "premium", "close", "close_p1", "name", "limit_price"]]


def market_returns(panel: pd.DataFrame) -> pd.DataFrame:
    """Equal-weight market legs, so a benchmark can match a holding window exactly.

    ``intraday`` = open-to-close, ``overnight`` = previous-close-to-open. A trade
    that buys at the open must not be credited with the gap it never held.
    """
    columns = ["trade_date", "open", "close", "close_p1"]
    if "adj_close" in panel.columns and "adj_p1" in panel.columns:
        columns += ["adj_close", "adj_p1"]
    p = panel[columns].copy()
    p["trade_date"] = p["trade_date"].astype(str)
    if "adj_close" in p:
        factor = p["adj_close"] / p["close"]
        open_basis = p["open"] * factor
        close_basis = p["adj_close"]
        previous_basis = p["adj_p1"]
    else:
        open_basis, close_basis, previous_basis = p["open"], p["close"], p["close_p1"]
    p["intraday"] = close_basis / open_basis - 1
    p["overnight"] = open_basis / previous_basis - 1
    if np.isinf(p[["intraday", "overnight"]].to_numpy(dtype=float)).any():
        raise SpecError("non-finite market return; check price and adjustment inputs")
    g = p.groupby("trade_date")[["intraday", "overnight"]].mean()
    g["cc"] = (1 + g["overnight"]) * (1 + g["intraday"]) - 1
    return g.sort_index()


# --------------------------------------------------------------------- trades
def rank_metric(frame: pd.DataFrame, name: str) -> pd.Series:
    """The value used to pick the few signals a small account can afford."""
    if name == "boards":
        return pd.to_numeric(frame.get("boards"), errors="coerce")
    if name == "amount":
        return pd.to_numeric(frame.get("amount"), errors="coerce")
    if name == "pct_chg":
        return pd.to_numeric(frame.get("pct_chg"), errors="coerce")
    if name == "close_to_limit":
        close = pd.to_numeric(frame.get("close"), errors="coerce")
        limit = pd.to_numeric(frame.get("limit_price"), errors="coerce")
        return close / limit - 1
    if name == "cb_ret":
        return pd.to_numeric(frame.get("cb_ret"), errors="coerce")
    if name == "premium":
        return pd.to_numeric(frame.get("premium"), errors="coerce")
    raise SpecError(f"unknown rank_by: {name}")


def _col_or_nan(frame: pd.DataFrame, name: str) -> pd.Series:
    """Numeric view of a forward column, NaN when the panel does not carry it.

    Hand-built panels in tests only ship the columns the case needs, and a
    missing ``down_f3`` must read as "no limit data" rather than crash. The real
    panel always carries them (``load_panel`` shifts the whole set), so this only
    ever softens the synthetic path.
    """
    if name in frame.columns:
        return pd.to_numeric(frame[name], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype="float64")


def _resolve_exit(merged: pd.DataFrame, spec: EventStudy, last: int,
                  panel: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Walk actual symbol bars until the first fill, never a fixed shift count.

    Suspensions simply have no row: the position remains held and the next real
    bar is the first opportunity to sell. The returned ``path`` contains every
    actual holding leg so returns and benchmarks can later fill calendar gaps.
    """
    px_col = "open" if spec.exit_at == "open" else "close"
    out = pd.DataFrame({
        "exit_price": np.nan, "exit_date": "", "exit_prev_close": np.nan,
        "rolled": False, "stuck": False, "open_position": False,
        "mark_price": np.nan, "mark_date": "", "limit_data": False,
        "corporate_action": False,
    }, index=merged.index)
    paths: dict[object, list[dict]] = {}
    blocked_at_plan = 0
    grouped = panel.sort_values(["ts_code", "trade_date"], kind="mergesort").groupby(
        "ts_code", sort=False)

    for idx in merged.index:
        code = merged.at[idx, "ts_code"]
        signal_date = str(merged.at[idx, "trade_date"])
        if code not in grouped.groups:
            paths[idx] = []
            continue
        bars = grouped.get_group(code)
        dates = bars["trade_date"].astype(str).to_numpy()
        starts = np.flatnonzero(dates == signal_date)
        if not len(starts):
            paths[idx] = []
            continue
        start = int(starts[0])
        opens = pd.to_numeric(bars["open"], errors="coerce").to_numpy(dtype=float)
        closes = pd.to_numeric(bars["close"], errors="coerce").to_numpy(dtype=float)
        downs = (pd.to_numeric(bars["down_limit"], errors="coerce").to_numpy(dtype=float)
                 if "down_limit" in bars else np.full(len(bars), np.nan))
        prices = opens if px_col == "open" else closes
        pre_closes = pd.to_numeric(bars["pre_close"], errors="coerce").to_numpy(dtype=float)
        blocked_seen = False
        filled = False
        corporate = False
        path: list[dict] = []

        # t_close 的入场在 T 收盘，最早能出场的是 T+1（隔夜单）；其余入场
        # 已经在 T+1，最早出场是 T+2。起点早一根不会误成交——下面的
        # ``j - start >= last`` 才是真正的判据。
        first = start + 1 if spec.entry == "t_close" else start + 2
        for j in range(first, len(bars)):
            date = str(dates[j])
            previous = closes[j - 1]
            close = closes[j]
            price = prices[j]
            down = downs[j]
            if (np.isfinite(previous) and previous > 0 and np.isfinite(pre_closes[j])
                    and not np.isclose(pre_closes[j], previous, rtol=1e-9, atol=1e-6)):
                # The exchange re-based the price overnight — a dividend, a split,
                # a rights issue. ``close / previous close`` is then not a return,
                # and the trade is re-measured from the adjusted series below.
                corporate = True
            if j - start >= last:
                if j - start == last:
                    out.at[idx, "limit_data"] = bool(np.isfinite(down))
                if np.isfinite(price) and price > 0:
                    blocked = np.isfinite(down) and price <= down + PRICE_EPS
                    if j - start == last and blocked:
                        blocked_at_plan += 1
                    if not blocked:
                        out.at[idx, "exit_price"] = float(price)
                        out.at[idx, "exit_date"] = date
                        out.at[idx, "exit_prev_close"] = previous
                        out.at[idx, "rolled"] = j - start > last
                        path.append({"date": date, "ret": price / previous - 1,
                                     "kind": "open_exit" if px_col == "open" else "day"})
                        filled = True
                        break
                    blocked_seen = True

            # A blocked open exit still holds through that close. A suspension
            # has no row here and is filled as a flat calendar leg downstream.
            if np.isfinite(close) and close > 0 and np.isfinite(previous) and previous > 0:
                path.append({"date": date, "ret": close / previous - 1, "kind": "day"})
                out.at[idx, "mark_price"] = float(close)
                out.at[idx, "mark_date"] = date

        if corporate:
            out.at[idx, "corporate_action"] = True
        if not filled:
            out.at[idx, "open_position"] = True
            out.at[idx, "stuck"] = blocked_seen
            out.at[idx, "rolled"] = blocked_seen
        paths[idx] = path

    out["path"] = pd.Series(paths, dtype="object")
    stats = {
        "blocked": blocked_at_plan,
        "rolled": int(out["rolled"].sum()),
        "stuck": int(out["stuck"].sum()),
        "unrollable": int((out["open_position"] & ~out["stuck"]).sum()),
        "planned": len(out),
        "limit_data": int(out["limit_data"].sum()),
    }
    return out, stats


def build_trades(panel: pd.DataFrame, pools: pd.DataFrame, spec: EventStudy,
                 market: pd.Series | None = None, *, equity: float = 5000.0) -> pd.DataFrame:
    """Turn pool events into trades with entry/exit prices and per-day returns.

    Returns one row per day per trade (a trade held h days contributes h+1 rows),
    which is what the portfolio series needs. The first row carries the cost.
    """
    spec = parse_spec(spec)
    pool = pools[pools["pool"] == spec.pool].copy()
    if pool.empty:
        return pd.DataFrame()
    if "ts_code" not in pool.columns and "code" in pool.columns:
        pool = pool.rename(columns={"code": "ts_code"})
    pool["trade_date"] = pool["trade_date"].astype(str)
    if spec.pool == "limit_up":
        pool = pool[pool["boards"].isin(spec.boards)]
    if spec.exclude_st and "name" in pool.columns:
        pool = pool[~pool["name"].astype(str).str.upper().str.contains("ST", na=False)]
    if spec.exclude_bj:
        pool = pool[~pool["ts_code"].astype(str).str.endswith(".BJ")]
    if spec.min_amount > 0 and "amount" in pool.columns:
        # Tushare amount is in thousand CNY.
        pool = pool[pd.to_numeric(pool["amount"], errors="coerce") * 1000 >= spec.min_amount]
    if pool.empty:
        return pd.DataFrame()

    keep = ["ts_code", "trade_date", "boards", "name", "amount", "pct_chg",
            "limit_price", "cb_ret", "premium"]
    keys = pool[[c for c in keep if c in pool.columns]].copy()
    # Forward columns are taken by prefix, not by name: the exit roll needs
    # ``down_f{k}`` and ``date_f{last+1}``, and a hand-maintained list silently
    # dropped them once already (the sell gate then read "no limit data" for
    # every row and did nothing).
    forward = [c for c in panel.columns
               if c.startswith(("date_f", "open_f", "low_f", "close_f", "limit_f",
                                "down_f", "adj_f"))]
    adjusted = ["adj_close"] if "adj_close" in panel.columns else []
    priced = ["open"] + (["low"] if "low" in panel.columns else []) + ["close"]
    bars = panel[["ts_code", "trade_date", *priced, *adjusted, *forward]]
    merged = keys.merge(bars, on=["ts_code", "trade_date"], how="left")
    merged = merged.dropna(subset=["date_f1"])
    if merged.empty:
        return pd.DataFrame()

    # date_f1 is the symbol's first actual later bar. If the planned entry is
    # suspended, the order rolls to that bar rather than disappearing or using a
    # calendar shift's stale price; no later bar at sample end means no entry.
    calendar = pd.Index(sorted(panel["trade_date"].astype(str).unique()))
    position = {day: i for i, day in enumerate(calendar)}

    # Selection must use only what was known at T0 close. Picking the top-k
    # *before* the fill check matters: otherwise a name is skipped because of how
    # it opened the next morning, which is information the trader never had.
    if spec.max_per_day > 0:
        value = rank_metric(merged, spec.rank_by)
        merged = merged.assign(_rank_value=value)
        merged = merged[merged["_rank_value"].notna()]
        merged["_rank"] = merged.groupby("trade_date")["_rank_value"].rank(
            method="first", ascending=(spec.rank_dir == "asc"))
        merged = merged[merged["_rank"] <= spec.max_per_day]

    if spec.entry == "t_close":
        # T 日收盘价买入。封住的板在这里是"按涨停价成交"——那是排队的假设，
        # 不是事实；本函数只保证价格与时点不错，成交概率归 fill_model。
        entry_price = pd.to_numeric(merged["close"], errors="coerce")
        merged = merged[entry_price.gt(0)]
        entry_price = entry_price[merged.index]
    elif spec.entry == "next_open":
        entry_price = pd.to_numeric(merged["open_f1"], errors="coerce")
        # Cannot buy a stock that opens at its upper limit. Same predicate the
        # matcher applies to a single bar, vectorised over the panel.
        blocked = blocked_at_open_mask(entry_price, merged["limit_f1"])
        merged = merged[~blocked & entry_price.gt(0)]
        entry_price = entry_price[merged.index]
    elif spec.entry == "next_limit":
        # T+1 挂限价买单：挂单价 = T 收盘 × (1 − 折让)，成交条件是 T+1 的最低价
        # 触及它。**这一条能用日线精确验证**，因为限价单坐在下方等价格过来，
        # 而排板要在这个价位排队——前者只需要 low，后者需要委托队列。
        #
        # 开盘已在挂单价下方时按开盘成交：那是更优的价格，也是真实撮合的顺序
        # （开盘前集合竞价先成交，你的限价单在开盘价低于限价时按开盘价成交）。
        if "low_f1" not in merged.columns:
            raise SpecError(
                "entry=next_limit needs a `low` column in the price dataset; "
                "this dataset does not ship one, so fills cannot be verified")
        ref = pd.to_numeric(merged["close"], errors="coerce")
        limit = ref * (1.0 - float(spec.limit_drop_pct))
        low_f1 = pd.to_numeric(merged["low_f1"], errors="coerce")
        open_f1 = pd.to_numeric(merged["open_f1"], errors="coerce")
        filled = limit.gt(0) & low_f1.notna() & low_f1.le(limit)
        merged = merged[filled]
        limit = limit[merged.index]
        open_f1 = open_f1[merged.index]
        entry_price = open_f1.where(open_f1.gt(0) & open_f1.le(limit), limit)
        merged = merged[entry_price.gt(0)]
        entry_price = entry_price[merged.index]
    else:
        entry_price = pd.to_numeric(merged["close_f1"], errors="coerce")
        merged = merged[entry_price.gt(0)]
        entry_price = entry_price[merged.index]

    merged = merged.copy()
    merged["entry_date"] = (merged["trade_date"].astype(str) if spec.entry == "t_close"
                            else merged["date_f1"].astype(str))
    merged["entry_price"] = entry_price.astype(float)

    # T+1 stocks exit the next trading day; T+0 instruments may exit the same day.
    sell_gate: dict = {}
    if spec.hold == 0:
        merged["exit_date"] = merged["date_f1"].astype(str)
        merged["exit_price"] = pd.to_numeric(merged["close_f1"], errors="coerce")
        merged["exit_prev_close"] = np.nan
        merged["rolled"] = False
        merged["stuck"] = False
        merged["open_position"] = False
        merged["mark_price"] = np.nan
        merged["mark_date"] = ""
        merged["limit_data"] = _col_or_nan(merged, "down_f1").notna()
        merged["path"] = [[] for _ in range(len(merged))]
        # A same-session holding cannot cross an overnight re-base.
        merged["corporate_action"] = False
    else:
        # 出场位置相对信号日 T：next_* 的入场已经用掉一根（T+1），所以要走
        # hold+1 根；t_close 的入场就在 T，走 hold 根。少算这一根，隔夜单会
        # 被当成"持有两天"，多算一根则会在入场当天就卖。
        last = spec.hold if spec.entry == "t_close" else spec.hold + 1
        resolved, sell_gate = _resolve_exit(merged, spec, last, panel)
        for col in ("exit_price", "exit_date", "exit_prev_close", "rolled", "stuck",
                    "open_position", "mark_price", "mark_date", "limit_data", "path",
                    "corporate_action"):
            merged[col] = resolved[col]

    has_exit = (pd.to_numeric(merged["exit_price"], errors="coerce").gt(0)
                & merged["exit_date"].astype(str).str.len().eq(8))
    has_mark = (merged["open_position"].astype(bool)
                & pd.to_numeric(merged["mark_price"], errors="coerce").gt(0)
                & merged["mark_date"].astype(str).str.len().eq(8))
    merged = merged[has_exit | has_mark].copy()
    if merged.empty:
        return pd.DataFrame()
    has_exit = has_exit.loc[merged.index]
    merged["trade_id"] = np.arange(len(merged))
    merged["gross"] = np.nan
    merged.loc[has_exit, "gross"] = (
        merged.loc[has_exit, "exit_price"] / merged.loc[has_exit, "entry_price"] - 1)
    # A holding that crossed an ex-date is *marked*, not re-measured — yet.
    #
    # The correct return for those trades is the total-return ratio
    # ``adj_close_exit / adj_close_entry``, and `adj_close` now exists. Applying
    # it here would move roughly 1% of every study's trades and invalidate the
    # verdicts already in the registry, so it is a separate, deliberate step:
    # the flag makes the affected rows countable (`attrs["corporate_action"]`)
    # and the next full re-run can switch the basis with a number to compare
    # against, instead of an untracked edit.
    corporate_count = (int(merged["corporate_action"].sum())
                       if "corporate_action" in merged.columns else 0)
    merged["mark_gross"] = np.nan
    merged.loc[~has_exit, "mark_gross"] = (
        merged.loc[~has_exit, "mark_price"] / merged.loc[~has_exit, "entry_price"] - 1)

    # Auto sizing is a signal-date decision. A full-sample median lets a signal
    # added on D2 retroactively change D1's ticket and minimum-commission cost.
    merged["cost"] = np.nan
    cost_by_date = {}
    for day, block in merged.groupby("trade_date", sort=False):
        names = max(1, len(block))
        lot_notional = float(block["entry_price"].median()) * spec.lot
        auto_ticket = min(equity, max(equity / names, lot_notional))
        ticket = spec.ticket_cny if spec.ticket_cny > 0 else auto_ticket
        day_cost = effective_cost_bps(spec, ticket)
        cost_by_date[str(day)] = day_cost
        merged.loc[block.index, "cost"] = day_cost["total_bps"] / 10_000.0
    merged["net"] = merged["gross"] - merged["cost"]
    merged["mark_net"] = merged["mark_gross"] - merged["cost"]
    cost_values = list(cost_by_date.values())
    cost = {key: float(np.mean([c[key] for c in cost_values]))
            for key in ("fee_cny", "fee_bps", "slippage_bps", "total_bps", "ticket_cny")}
    cost.update({"cost_mult": spec.cost_mult, "model": "kernel",
                 "basis": "signal_date", "by_date": cost_by_date})

    # Each blocked session is a full holding day. Only an executable open exit
    # is an overnight-only leg; an unresolved open is valued through that close.
    final_date = merged["exit_date"].where(has_exit, merged["mark_date"]).astype(str)
    records = []
    for idx, row in merged.iterrows():
        # t_close 的入场腿是 T 收盘那一刻，本身没有持仓收益；T 收盘→T+1 的
        # 那一段由 path 里的第一根日线承担。不置零就会把同一段算两遍
        # （总收益会变成 (1+r)² 的近似）。
        entry_ret = ((0.0 if spec.entry == "t_close"
                      else float(row["close_f1"]) / float(row["entry_price"]) - 1)
                     - float(row["cost"]))
        if not np.isfinite(entry_ret):
            raise SpecError(f"non-finite entry return for {row['ts_code']} {row['entry_date']}")
        records.append({"trade_id": int(row["trade_id"]), "date": str(row["entry_date"]),
                        "ret": entry_ret, "leg": 0, "kind": "entry"})
        for leg in row["path"]:
            if not np.isfinite(leg["ret"]):
                raise SpecError(f"non-finite holding return for {row['ts_code']} {leg['date']}")
            records.append({"trade_id": int(row["trade_id"]), "date": leg["date"],
                            "ret": float(leg["ret"]), "leg": -1, "kind": leg["kind"]})
    daily_rows = pd.DataFrame(records)

    # A suspended name still occupies a slot. Fill missing sessions with a flat
    # mark-to-last-close leg, so other names are not silently re-weighted and
    # the benchmark sees exactly the same holding window.
    expected = final_date.map(position) - merged["entry_date"].map(position) + 1
    counts = daily_rows.groupby("trade_id").size()
    additions = []
    for tid, expected_count in expected.items():
        trade_id = int(merged.at[tid, "trade_id"])
        if int(counts.get(trade_id, 0)) >= int(expected_count):
            continue
        start = position[merged.at[tid, "entry_date"]]
        stop = position[final_date.at[tid]]
        existing = set(daily_rows.loc[daily_rows["trade_id"] == trade_id, "date"].astype(str))
        for date in calendar[start:stop + 1]:
            if str(date) not in existing:
                additions.append({"trade_id": trade_id, "date": str(date), "ret": 0.0,
                                  "leg": -1, "kind": "day"})
    if additions:
        daily_rows = pd.concat([daily_rows, pd.DataFrame(additions)], ignore_index=True)
    daily_rows = daily_rows.sort_values(["trade_id", "date"], kind="mergesort").reset_index(drop=True)
    daily_rows["leg"] = daily_rows.groupby("trade_id").cumcount()

    # Benchmark legs mirror the trade legs exactly, including forward-filled
    # suspension days and unresolved marks. Only realised trades get `excess`.
    if market is not None and not market.empty and "intraday" in market.columns:
        intra = daily_rows["date"].map(market["intraday"]).astype(float).fillna(0.0)
        over = daily_rows["date"].map(market["overnight"]).astype(float).fillna(0.0)
        kind = daily_rows["kind"].to_numpy()
        full_day = (1 + over.to_numpy()) * (1 + intra.to_numpy()) - 1
        # next_open 在 T+1 开盘成交，所以入场那天只吃日内；next_limit 在 T+1
        # 盘中某个时刻成交，成交时点未知，按整天日内计入——这是**偏保守**的
        # 近似：它假设你的基准敞口比实际更长，于是超额被低估而不是高估。
        # 判一个 setup 有没有边际时，宁可低估。
        entry_leg = (intra.to_numpy()
                     if spec.entry in ("next_open", "next_limit")
                     else np.zeros(len(daily_rows)))
        bench_leg = np.where(kind == "entry", entry_leg,
                             np.where(kind == "open_exit", over.to_numpy(), full_day))
        bench = pd.Series(bench_leg, index=daily_rows.index)
        per_trade = (1 + bench).groupby(daily_rows["trade_id"]).prod() - 1
        merged["benchmark"] = merged["trade_id"].map(per_trade)
        merged["excess"] = (1 + merged["net"]) / (1 + merged["benchmark"]) - 1
        merged["mark_excess"] = (1 + merged["mark_net"]) / (1 + merged["benchmark"]) - 1

    trade_cols = ["trade_id", "ts_code", "trade_date", "entry_date", "exit_date",
                  "boards", "entry_price", "exit_price", "gross", "net", "cost",
                  "rolled", "stuck", "open_position", "limit_data", "mark_price", "mark_date",
                  "mark_gross", "mark_net", "corporate_action"]
    if "benchmark" in merged.columns:
        trade_cols += ["benchmark", "excess", "mark_excess"]
    trades = merged[trade_cols].copy()
    trades.attrs["daily_rows"] = daily_rows
    trades.attrs["cost"] = cost
    trades.attrs["corporate_action"] = corporate_count
    trades.attrs["price_basis"] = (
        "raw fills; trades whose holding crossed an ex-date are flagged in "
        "``corporate_action`` and still priced raw — switch to adj_close before "
        "quoting their returns"
        if "adj_close" in panel.columns else "raw_unadjusted_estimation")
    # 成交口径要跟着入场点走：t_close 在封板上是"假设排到队"，与在开盘价
    # 成交不是同一种乐观。写进 attrs，报告才能声明它测的是哪一种。
    trades.attrs["execution_basis"] = (
        "close_assumed" if spec.entry == "t_close"
        else ("estimation" if spec.pool in T0_POOLS else "daily_bar_model"))
    # The sessions the sample spans, not the sessions that happened to hold a
    # position: the difference is the exposure, and it belongs in every CAGR.
    trades.attrs["calendar"] = [str(d) for d in calendar]
    if sell_gate:
        trades.attrs["sell_gate"] = sell_gate
    return trades


def portfolio_series(daily_rows: pd.DataFrame,
                     calendar: list[str] | pd.Index | None = None) -> pd.Series:
    """Equal-weight across open positions; no position means cash (0 return).

    ``calendar`` is the full list of sessions in the sample. It is **required**
    for an honest annualisation: a strategy that trades on 469 of 920 days is in
    cash for the other 451, and dropping them from the series does not make the
    remaining days a year. Before this, ``metrics`` divided by ``len(series)/252``,
    so a half-time strategy had its CAGR computed over half the elapsed time —
    the reported 35.96% was really 16.34% (P27).
    """
    if daily_rows is None or daily_rows.empty:
        return pd.Series(dtype="float64")
    per_day = daily_rows.groupby("date")["ret"].mean()
    per_day.index = per_day.index.astype(str)
    per_day = per_day.sort_index()
    if calendar is not None and len(calendar):
        idx = pd.Index([str(d) for d in calendar])
        per_day = per_day.reindex(idx.union(per_day.index)).fillna(0.0)
        per_day = per_day.sort_index()
    return per_day
