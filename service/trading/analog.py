"""Historical analog engine: the kernel's memory of "what happened next".

The kernel is not allowed to say "buy". It *is* allowed to say: "this setup, as
measured on the close of 20261023, has appeared 1,180 times since 2023 in the
liquid universe; entering at the next open and holding five sessions returned
+0.42% on average, beat an equal-weight basket 54% of the time, and the 95%
block-bootstrap interval on the daily-mean excess is [+0.05%, +0.79%]."

That is a knowledge statement, not a decision. The model reads it, weighs it
against news, position, and its own thesis, and then decides. Everything here is
retrieval plus arithmetic: no fitting, no hyper-parameter search, no model that
can overfit the answer.

Two conventions matter and are stated because they change the numbers:

* Features use only data up to and including ``T``'s close. A decision made after
  the close may use the close.
* Forward returns enter at ``T+1``'s **open**, not ``T``'s close, because the desk
  fills at the next open (``fill_mode=next_open``). Measuring close-to-close
  would credit the analog with an overnight gap the desk never gets.
* Prices are total-return: ``close/pre_close`` is chained, so an ex-dividend drop
  is not counted as a loss. ``adj_factor`` is deliberately not used because
  ``pre_close`` already carries the ex-rights adjustment and the two disagree
  around corporate actions.

The index is a snapshot of the liquid universe (top N by turnover each day), not
of every listed stock. A 5,000 CNY account cannot trade an illiquid name without
moving it, so analogs drawn from names it cannot trade would be entertainment.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

import numpy as np
import pandas as pd

from service import settings
from service.kernel.units import TRADING_DAYS_PER_YEAR

FEATURES = ("mom20", "mom5", "vol20", "dist_ma20", "amt_ratio", "ret1",
            "breadth", "mkt_mom20")
STOCK_FEATURES = FEATURES[:6]
MARKET_FEATURES = FEATURES[6:]
# Weights encode what makes two setups "the same kind of chart". Momentum and
# volatility dominate; the market context terms are there to prefer analogs from
# a similar tape rather than from a different regime.
# They live in configs/params.yaml (analog.weights) so the retrieval can be
# re-tuned without editing code. A length mismatch is a hard error: silently
# broadcasting a short vector would weight features by position, and every
# distance would be wrong in a way that still returns results.
_WEIGHTS = settings.seq("analog.weights")
if len(_WEIGHTS) != len(FEATURES):
    raise ValueError(f"analog.weights has {len(_WEIGHTS)} entries, "
                     f"FEATURES has {len(FEATURES)}: {FEATURES}")
WEIGHTS = np.array(_WEIGHTS, dtype=np.float32)
HORIZONS = settings.seq("analog.horizons")
TOP_N = settings.integer("analog.top_n")
MIN_AMOUNT = settings.num("analog.min_amount")   # CNY; below this an order is noise
MIN_HISTORY = settings.integer("analog.min_history")
MAX_ABS_RET = settings.num("analog.max_abs_ret")  # a 50% daily move is a data error
BOOTSTRAP_DRAWS = settings.integer("analog.bootstrap_draws")
SEED = settings.integer("analog.seed")

INDEX_VERSION = settings.integer("analog.index_version")

# How many sessions the index actually carries. More sessions mean tighter
# intervals; it also means a wider (sessions x symbols) matrix, and the build is
# one in-memory pass. 3,000 sessions is ~12 years: enough to include the 2015
# leverage cycle and the 2018 bear, bounded enough to fit in about a gigabyte.
MAX_SESSIONS = settings.integer("analog.max_sessions")


# --------------------------------------------------------------------- helpers
def _wide(panel: pd.DataFrame, column: str) -> pd.DataFrame:
    return panel.pivot_table(index="trade_date", columns="ts_code",
                             values=column, aggfunc="last").sort_index()


def _zscore(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    mean = frame.mean(axis=1)
    std = frame.std(axis=1)
    std = std.where(std > 1e-12)
    z = frame.sub(mean, axis=0).div(std, axis=0)
    return z.clip(-4, 4), mean, std.fillna(0.0)


def _regime_labels(ew: pd.Series, vol: pd.Series, breadth: pd.Series) -> pd.Series:
    """Same four labels and the same cut-offs as ``regime.py``.

    Recomputed here on the liquid pool so the whole history is available at once.
    It is a *proxy* for ``/regime``: same rules, slightly different universe and
    a total-return instead of a close-only series. The differences are reported in
    the response's caveats rather than hidden.
    """
    ma200 = ew.rolling(200).mean()
    trend_up = ew >= ma200
    pct = vol.rolling(250, min_periods=60).apply(lambda w: float((w <= w[-1]).mean()), raw=True)
    labels = []
    for day in ew.index:
        t = bool(trend_up.get(day, False))
        v = pct.get(day, np.nan)
        b = breadth.get(day, np.nan)
        b = 0.5 if not np.isfinite(b) else float(b)
        if np.isfinite(v) and v >= 0.75:
            labels.append("stressed")
        elif not t and b < 0.35:
            labels.append("risk_off")
        elif t and b >= 0.50:
            labels.append("risk_on")
        else:
            labels.append("neutral")
    return pd.Series(labels, index=ew.index)


def _json_float(value: float, ndigits: int = 6) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, np.integer)):
        return int(value)
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, ndigits)


# ----------------------------------------------------------------------- index
@dataclass
class AnalogIndex:
    dates: list[str]
    codes: list[str]
    x: np.ndarray                 # (n_rows, n_features) float32, z-scored
    fwd: np.ndarray               # (n_rows, n_horizons) float32
    day_of_row: np.ndarray        # (n_rows,) int32
    code_of_row: np.ndarray       # (n_rows,) int32
    day_mean: np.ndarray          # (n_days, n_features)
    day_std: np.ndarray           # (n_days, n_features)
    day_mkt: np.ndarray           # (n_days, 2) raw breadth, mkt_mom20
    day_mkt_fwd: np.ndarray       # (n_days, n_horizons) equal-weight forward
    day_regime: list[str]
    mkt_mean: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
    mkt_std: np.ndarray = field(default_factory=lambda: np.ones(2, dtype=np.float32))
    industries: dict[str, str] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ query
    def day_index(self, day: str) -> int | None:
        try:
            return self.dates.index(str(day))
        except ValueError:
            return None

    def cross_section(self, day: str) -> tuple[np.ndarray, np.ndarray] | None:
        i = self.day_index(day)
        if i is None:
            return None
        return self.day_mean[i], self.day_std[i]

    def query(self, raw: np.ndarray, day: str, *, k: int = 300,
              same_industry: str | None = None, horizon: int | None = None) -> dict:
        """Nearest neighbours with a realised return for one holding horizon.

        Excludes mechanically overlapping windows around ``day`` and (optionally)
        other industries. Stock features use that day's cross-section; market
        features use their own history. The default keeps the legacy longest-
        horizon pool; callers reporting several horizons should query each one.
        """
        i = self.day_index(day)
        if i is None:
            raise ValueError(f"{day} is not in the analog index (rebuild it)")
        mean, std = self.day_mean[i], self.day_std[i]
        raw = np.asarray(raw, dtype=np.float32)
        z_stock = np.clip((raw[:len(STOCK_FEATURES)] - mean) /
                          np.where(std > 1e-12, std, 1.0), -4, 4)
        z_mkt = np.clip((raw[len(STOCK_FEATURES):] - self.mkt_mean) /
                        np.where(self.mkt_std > 1e-12, self.mkt_std, 1.0), -4, 4)
        z = np.concatenate([z_stock, z_mkt]).astype(np.float32)
        z = np.where(np.isfinite(z), z, 0.0)

        diff = (self.x - z) ** 2
        dist = np.sqrt((diff * WEIGHTS).sum(axis=1)).astype(np.float32)
        dist = np.where(np.isfinite(dist), dist, np.inf)

        horizon = max(HORIZONS) if horizon is None else int(horizon)
        if horizon not in HORIZONS:
            raise ValueError(f"horizon must be one of {HORIZONS}")
        # A historical query may only use earlier setups whose requested forward
        # window is already realised, and the cached value must actually exist.
        keep = ((self.day_of_row < i)
                & (np.abs(self.day_of_row - i) >= horizon + 1)
                & np.isfinite(self.fwd[:, HORIZONS.index(horizon)]))
        if same_industry:
            wanted = {c for c, ind in self.industries.items() if ind == same_industry}
            if wanted:
                idx_codes = np.array([j for j, c in enumerate(self.codes) if c in wanted],
                                     dtype=np.int32)
                keep &= np.isin(self.code_of_row, idx_codes)
        dist = np.where(keep, dist, np.inf)

        available = int(np.isfinite(dist).sum())
        take = min(int(k), available)
        if take <= 0:
            return {"n": 0, "reason": "no candidate window with a realised forward return"}
        order = np.argpartition(dist, take - 1)[:take]
        order = order[np.argsort(dist[order])]

        rows = {
            "dist": dist[order],
            "day": self.day_of_row[order],
            "code": self.code_of_row[order],
            "fwd": self.fwd[order],
        }
        return {"z": z, "rows": rows, "available": available, "horizon": horizon,
                "nearest_distance": float(dist[order[0]]),
                "median_distance": float(np.median(dist[order]))}


# ------------------------------------------------------------------- building
def default_start(store, max_sessions: int = MAX_SESSIONS,
                  end: str | None = None) -> str:
    """Earliest session the index should carry, derived from the store.

    This used to be the literal "20230101", which meant every row backfilled
    before 2023 was invisible to the analog engine: the data was there, the
    queries never touched it, and nothing said so. Deriving it from the manifest
    keeps that failure from repeating, and the session cap keeps the build inside
    a predictable memory budget.
    """
    cover_start = ""
    cover_end = ""
    for row in store.registry.summary():
        if row.get("dataset") != "daily":
            continue
        first = str(row.get("cover_start") or "")
        last = str(row.get("cover_end") or "")
        if first and (not cover_start or first < cover_start):
            cover_start = first
        cover_end = max(cover_end, last)
    if end:
        cover_end = min(cover_end, str(end)) if cover_end else str(end)
    if not cover_start:
        return "19900101"
    try:
        cal = store.read("trade_cal", columns=["cal_date", "is_open"])
    except (KeyError, ValueError):
        return cover_start
    open_days = cal.loc[cal["is_open"] == 1, "cal_date"].astype(str)
    days = sorted(d for d in open_days if cover_start <= d <= cover_end)
    if len(days) <= max_sessions:
        return cover_start
    return days[-max_sessions]


def build(store, *, top_n: int = TOP_N, min_amount: float = MIN_AMOUNT,
          start: str | None = None, end: str | None = None,
          basic: pd.DataFrame | None = None) -> AnalogIndex:
    start = start or default_start(store, end=end)

    cols = ["ts_code", "trade_date", "close", "pre_close", "open", "amount"]
    # Bound the read as well as the frame: a replay must not even construct an
    # index from samples after its as_of date.
    daily = store.read("daily", start, end, columns=cols)
    if daily.empty:
        raise RuntimeError("daily is empty; run the P1 backfill")
    daily = daily[["ts_code", "trade_date", "close", "pre_close", "open", "amount"]].copy()
    daily["trade_date"] = daily["trade_date"].astype(str)
    daily = daily[(daily["trade_date"] >= start)]
    if end:
        daily = daily[daily["trade_date"] <= end]
    daily = daily[~daily["ts_code"].astype(str).str.endswith(".BJ")]
    daily = daily.sort_values(["trade_date", "ts_code"])

    close = _wide(daily, "close").astype("float32")
    pre = _wide(daily, "pre_close").astype("float32")
    open_ = _wide(daily, "open").astype("float32")
    amount = _wide(daily, "amount").astype("float32") * 1000.0  # CNY

    safe_pre = pre.where(pre > 0)
    ret = (close / safe_pre - 1).clip(-MAX_ABS_RET, MAX_ABS_RET).fillna(0.0)
    px = (1.0 + ret).cumprod()

    # ---- features (each defined on information available at T's close)
    mom20 = px / px.shift(20) - 1
    mom5 = px / px.shift(5) - 1
    vol20 = ret.rolling(20).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)
    ma20 = px.rolling(20).mean()
    dist_ma20 = px / ma20 - 1
    amt_avg = amount.rolling(20).mean().shift(1)
    amt_ratio = amount / amt_avg.where(amt_avg > 0)
    ret1 = ret

    ew_ret = ret.where(amount >= min_amount).mean(axis=1)
    ew = (1 + ew_ret.fillna(0)).cumprod()
    mkt_mom20 = ew / ew.shift(20) - 1
    ma20w = px.rolling(20).mean()
    breadth = (px > ma20w).where(px.notna() & ma20w.notna()).mean(axis=1)

    solid = amount.rank(axis=1, ascending=False) <= top_n
    solid &= amount >= min_amount

    frame = {
        "mom20": mom20, "mom5": mom5, "vol20": vol20, "dist_ma20": dist_ma20,
        "amt_ratio": amt_ratio, "ret1": ret1,
    }
    valid = solid.copy()
    for series in frame.values():
        valid &= np.isfinite(series)

    # Day-level statistics use every valid row, including the last few sessions
    # that have no realised forward return yet — the query day needs them.
    n_days = len(px.index)
    n_stock = len(STOCK_FEATURES)
    day_mean = np.zeros((n_days, n_stock), dtype=np.float32)
    day_std = np.zeros((n_days, n_stock), dtype=np.float32)
    zs: list[pd.DataFrame] = []
    for j, name in enumerate(STOCK_FEATURES):
        wide = frame[name].where(valid).astype("float64", copy=False)
        mu = wide.mean(axis=1).fillna(0.0)
        sd = wide.std(axis=1).fillna(0.0)
        day_mean[:, j] = mu.to_numpy(dtype=np.float32)
        day_std[:, j] = sd.to_numpy(dtype=np.float32)
        z = wide.sub(mu, axis=0).div(sd.replace(0, np.nan), axis=0).clip(-4, 4)
        # A zero-variance cross-section has zero distance on that feature, not a
        # NaN that would make every candidate unusable and poison cache checks.
        zs.append(z.fillna(0.0))

    # Market context is one number per day, so it is z-scored across time.
    day_mkt_raw = np.column_stack([
        breadth.astype("float64", copy=False).to_numpy(),
        mkt_mom20.astype("float64", copy=False).to_numpy(),
    ]).astype(np.float32)
    finite = np.isfinite(day_mkt_raw)
    mkt_mean = np.where(finite.any(axis=0),
                        np.nanmean(np.where(finite, day_mkt_raw, np.nan), axis=0), 0.0)
    mkt_std = np.where(finite.any(axis=0),
                       np.nanstd(np.where(finite, day_mkt_raw, np.nan), axis=0), 1.0)
    mkt_std = np.where(mkt_std > 1e-9, mkt_std, 1.0)
    mkt_z = np.clip((np.nan_to_num(day_mkt_raw, nan=0.0) - mkt_mean) / mkt_std, -4, 4)
    mkt_z = np.where(finite, mkt_z, 0.0).astype(np.float32)

    # ---- forward returns: next open -> close at T+h
    open_adj = px.shift(1) * (open_ / safe_pre)
    fwd = {}
    for h in HORIZONS:
        fwd[h] = px.shift(-h) / open_adj.shift(-1) - 1

    # Keep a setup when at least one horizon is realised. Requiring all four
    # used to discard valid 1d/3d/5d samples near the as_of boundary.
    realised = valid.copy()
    any_forward = np.zeros_like(realised, dtype=bool)
    for series in fwd.values():
        any_forward |= np.isfinite(series)
    realised &= any_forward

    mask = realised.to_numpy()
    day_idx, code_idx = np.nonzero(mask)
    n_rows = len(day_idx)
    if n_rows == 0:
        raise RuntimeError("analog index would be empty; check the date range")

    x = np.zeros((n_rows, len(FEATURES)), dtype=np.float32)
    for j, z in enumerate(zs):
        x[:, j] = z.to_numpy()[mask]
    x[:, n_stock:] = mkt_z[day_idx]
    fw = np.empty((n_rows, len(HORIZONS)), dtype=np.float32)
    for j, h in enumerate(HORIZONS):
        fw[:, j] = fwd[h].to_numpy()[mask]

    day_mkt_fwd = np.full((n_days, len(HORIZONS)), np.nan, dtype=np.float32)
    for j in range(len(HORIZONS)):
        usable = np.isfinite(fw[:, j])
        col = np.bincount(day_idx[usable], weights=fw[usable, j].astype(np.float64),
                          minlength=n_days)
        cnt = np.bincount(day_idx[usable], minlength=n_days)
        with np.errstate(invalid="ignore", divide="ignore"):
            day_mkt_fwd[:, j] = np.where(cnt > 0, col / np.maximum(cnt, 1), np.nan)

    industries: dict[str, str] = {}
    if basic is not None and not basic.empty and "industry" in basic.columns:
        industries = dict(zip(basic["ts_code"].astype(str), basic["industry"].astype(str)))

    regime = _regime_labels(ew, ew_ret.rolling(20).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR), breadth)

    # How close is "close"? A nearest distance means nothing on its own: with a
    # million candidates almost everything has a near neighbour. Probe 100 random
    # setups and store the distribution, so a query can be ranked against it.
    rng = np.random.default_rng(SEED)
    picks = rng.choice(n_rows, size=min(100, n_rows), replace=False)
    probe: list[float] = []
    for p in picks:
        dd = ((x - x[p]) ** 2 * WEIGHTS).sum(axis=1)
        dd[np.abs(day_idx - day_idx[p]) < max(HORIZONS) + 1] = np.inf
        best = float(np.sqrt(dd.min()))
        if math.isfinite(best):
            probe.append(best)
    dist_ref = sorted(probe)

    meta = {
        "version": INDEX_VERSION,
        "built_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        # ``start`` is the requested start, not the first bar: the cache key is
        # built from the request, and a key that changes with the data would
        # rebuild the index on every single call.
        "start": str(start),
        "first_bar": str(px.index.min()),
        "end": str(px.index.max()),
        "rows": int(n_rows),
        "days": int(n_days),
        "top_n": int(top_n),
        "min_amount": float(min_amount),
        "features": list(FEATURES),
        "weights": [float(w) for w in WEIGHTS],
        "horizons": list(HORIZONS),
        "entry": "next_open",
        "dist_ref": [round(float(d), 4) for d in dist_ref],
    }
    return AnalogIndex(
        dates=[str(d) for d in px.index],
        codes=[str(c) for c in px.columns],
        x=x, fwd=fw, day_of_row=day_idx.astype(np.int32),
        code_of_row=code_idx.astype(np.int32),
        day_mean=day_mean, day_std=day_std,
        day_mkt=day_mkt_raw,
        day_mkt_fwd=day_mkt_fwd,
        day_regime=[str(r) for r in regime],
        mkt_mean=mkt_mean.astype(np.float32), mkt_std=mkt_std.astype(np.float32),
        industries=industries, meta=meta,
    )


# --------------------------------------------------------------------- caching
def _cache_dir(root: Path) -> Path:
    return root / "data" / "cache" / "analog"


def _key(cover_end: str, top_n: int, min_amount: float, start: str) -> str:
    raw = f"v{INDEX_VERSION}:{cover_end}:{top_n}:{min_amount:.0f}:{start}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _validate_cache(arrays, meta: dict) -> None:
    """Reject truncated, incompatible or non-finite cache payloads."""
    if int(meta.get("version", 0)) != INDEX_VERSION:
        raise ValueError("unsupported analog cache version")
    n_days = len(arrays["dates"])
    n_rows = len(arrays["day_of_row"])
    if n_days <= 0 or n_rows <= 0:
        raise ValueError("empty analog cache")
    shapes = {
        "x": (n_rows, len(FEATURES)),
        "fwd": (n_rows, len(HORIZONS)),
        "day_of_row": (n_rows,),
        "code_of_row": (n_rows,),
        "day_mean": (n_days, len(STOCK_FEATURES)),
        "day_std": (n_days, len(STOCK_FEATURES)),
        "day_mkt": (n_days, len(MARKET_FEATURES)),
        "day_mkt_fwd": (n_days, len(HORIZONS)),
        "mkt_mean": (len(MARKET_FEATURES),),
        "mkt_std": (len(MARKET_FEATURES),),
        "dates": (n_days,), "codes": (len(arrays["codes"]),),
        "regimes": (n_days,),
    }
    for name, shape in shapes.items():
        if tuple(arrays[name].shape) != shape:
            raise ValueError(f"analog cache {name} has shape {arrays[name].shape}, expected {shape}")
    for name in ("day_of_row", "code_of_row"):
        if arrays[name].dtype.kind not in "iu":
            raise ValueError(f"analog cache {name} must contain integer indices")
    if ((arrays["day_of_row"] < 0) | (arrays["day_of_row"] >= n_days)).any():
        raise ValueError("analog cache day index out of range")
    n_codes = len(arrays["codes"])
    if ((arrays["code_of_row"] < 0) | (arrays["code_of_row"] >= n_codes)).any():
        raise ValueError("analog cache code index out of range")
    for name in ("x", "day_mean", "day_std", "mkt_mean", "mkt_std"):
        if not np.isfinite(arrays[name]).all():
            raise ValueError(f"analog cache {name} contains non-finite values")
    for name in ("fwd", "day_mkt", "day_mkt_fwd"):
        if np.isinf(arrays[name]).any():
            raise ValueError(f"analog cache {name} contains infinity")
    if not np.isfinite(arrays["fwd"]).any(axis=1).all():
        raise ValueError("analog cache row has no realised horizon")
    if (arrays["mkt_std"] <= 0).any():
        raise ValueError("analog cache market standard deviation must be positive")
    if int(meta.get("rows", n_rows)) != n_rows or int(meta.get("days", n_days)) != n_days:
        raise ValueError("analog cache metadata does not match array shapes")


def save(index: AnalogIndex, root: Path) -> Path:
    key = _key(index.meta["end"], index.meta["top_n"],
               index.meta["min_amount"], index.meta["start"])
    path = _cache_dir(root) / f"{key}.npz"
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {
        "x": index.x, "fwd": index.fwd, "day_of_row": index.day_of_row,
        "code_of_row": index.code_of_row, "day_mean": index.day_mean,
        "day_std": index.day_std, "day_mkt": index.day_mkt,
        "day_mkt_fwd": index.day_mkt_fwd, "mkt_mean": index.mkt_mean,
        "mkt_std": index.mkt_std, "dates": np.array(index.dates),
        "codes": np.array(index.codes), "regimes": np.array(index.day_regime),
    }
    _validate_cache(arrays, index.meta)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w+b", dir=path.parent, prefix=f".{key}.",
                suffix=".npz", delete=False) as temp:
            temp_name = temp.name
            np.savez_compressed(
                temp, **arrays, meta=np.array(json.dumps(index.meta, ensure_ascii=False)),
                industries=np.array(json.dumps(index.industries, ensure_ascii=False)))
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)
    return path


def load(path: Path) -> AnalogIndex:
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["meta"]))
        _validate_cache(z, meta)
        return AnalogIndex(
            dates=[str(d) for d in z["dates"]],
            codes=[str(c) for c in z["codes"]],
            x=z["x"], fwd=z["fwd"], day_of_row=z["day_of_row"],
            code_of_row=z["code_of_row"], day_mean=z["day_mean"],
            day_std=z["day_std"], day_mkt=z["day_mkt"], day_mkt_fwd=z["day_mkt_fwd"],
            mkt_mean=z["mkt_mean"], mkt_std=z["mkt_std"],
            day_regime=[str(r) for r in z["regimes"]],
            industries=json.loads(str(z["industries"])), meta=meta,
        )


def _usable(path: Path) -> bool:
    try:
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            _validate_cache(z, meta)
            return True
    except (OSError, ValueError, KeyError, TypeError, EOFError, BadZipFile):
        return False


_INDEX: AnalogIndex | None = None
_INDEX_KEY: str | None = None
# A rebuild takes 40-50s (3.4M rows over 3,000 sessions) while the host tool
# timeout is 20s. Without this lock, every request landing inside the rebuild
# window starts its own build and they run one after another: three callers wait
# two and a half minutes for the same index and all of them time out. The lock
# makes latecomers wait for the first result instead of duplicating the work.
_INDEX_LOCK = threading.Lock()
# One npz is ~130MB and the key carries cover_end, so every trading day writes a
# new file. Keeping a few covers a historical replay; the rest is pure leak.
_CACHE_KEEP = settings.integer("cache.analog_keep")


def _prune_cache(root: Path, keep: int = _CACHE_KEEP) -> list[str]:
    """Drop the oldest index caches, keeping the newest ``keep`` files.

    A failed unlink is not an error: on Windows another reader may hold the file
    open, and the next rebuild will try again.
    """
    removed: list[str] = []
    try:
        files = sorted(_cache_dir(root).glob("*.npz"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return removed
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            continue
        removed.append(old.name)
    return removed


def get_index(store, root: Path, *, top_n: int = TOP_N, min_amount: float = MIN_AMOUNT,
              start: str | None = None, end: str | None = None) -> AnalogIndex:
    """Disk-cached index, rebuilt when the data cover or the window moves.

    ``end`` is the point-in-time boundary for historical replays. It participates
    in the cache key so an old query cannot load a present-day index from disk.

    The rebuild is serialised: the fast path returns the in-memory index without
    touching the lock, and only a cache miss takes it.
    """
    global _INDEX, _INDEX_KEY
    cover = "unknown"
    for row in store.registry.summary():
        if row.get("dataset") == "daily" and row.get("cover_end"):
            cover = str(row["cover_end"])
    if end:
        cover = min(cover, str(end)) if cover != "unknown" else str(end)
    start = start or default_start(store, end=end)
    key = _key(cover, top_n, min_amount, start)
    if _INDEX is not None and _INDEX_KEY == key:
        return _INDEX
    with _INDEX_LOCK:
        # Another thread may have finished the same rebuild while we waited.
        if _INDEX is not None and _INDEX_KEY == key:
            return _INDEX
        path = _cache_dir(root) / f"{key}.npz"
        if path.exists() and _usable(path):
            _INDEX = load(path)
        else:
            basic = store.read("stock_basic")
            built = build(store, top_n=top_n, min_amount=min_amount, start=start,
                          end=end, basic=basic)
            # Save before publishing: if the write fails the in-memory index
            # stays None, so the next caller retries instead of trusting a
            # half-written cache.
            save(built, root)
            _INDEX = built
            _prune_cache(root)
        _INDEX_KEY = key
        return _INDEX


def reset_cache() -> None:
    global _INDEX, _INDEX_KEY
    _INDEX = None
    _INDEX_KEY = None


# --------------------------------------------------------------------- querying
def features_for(market, symbol: str, as_of: str) -> dict | None:
    """The same six stock features the index uses, computed for one symbol."""
    hist = market.history(symbol, as_of, 61)
    if len(hist) < MIN_HISTORY:
        return None
    close = pd.to_numeric(hist["close"], errors="coerce").astype(float)
    pre = pd.to_numeric(hist["pre_close"], errors="coerce").astype(float)
    amount = pd.to_numeric(hist["amount"], errors="coerce").astype(float) * 1000.0
    safe = pre.where(pre > 0)
    ret = (close / safe - 1).clip(-MAX_ABS_RET, MAX_ABS_RET).fillna(0.0)
    px = (1 + ret).cumprod()
    if len(px) < 61:
        return None
    amt_avg = amount.iloc[-21:-1].mean()
    return {
        "mom20": float(px.iloc[-1] / px.iloc[-21] - 1),
        "mom5": float(px.iloc[-1] / px.iloc[-6] - 1),
        "vol20": float(ret.iloc[-20:].std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)),
        "dist_ma20": float(px.iloc[-1] / px.iloc[-20:].mean() - 1),
        "amt_ratio": float(amount.iloc[-1] / amt_avg) if amt_avg > 0 else 1.0,
        "ret1": float(ret.iloc[-1]),
    }


def _stats(fwd: np.ndarray, excess: np.ndarray, days: np.ndarray) -> dict:
    n = len(fwd)
    if n == 0:
        return {"n": 0}
    uniq, inv = np.unique(days, return_inverse=True)
    day_means = np.bincount(inv, weights=excess, minlength=len(uniq)) / \
        np.maximum(np.bincount(inv, minlength=len(uniq)), 1)
    rng = np.random.default_rng(SEED)
    draws = rng.integers(0, len(uniq), size=(BOOTSTRAP_DRAWS, len(uniq)))
    boot = day_means[draws].mean(axis=1)
    sd = float(day_means.std(ddof=1)) if len(uniq) > 1 else 0.0
    se = sd / math.sqrt(max(len(uniq), 1))
    return {
        "n": n,
        "n_days": len(uniq),
        "mean": _json_float(fwd.mean()),
        "median": _json_float(float(np.median(fwd))),
        "win_rate": _json_float(float((fwd > 0).mean()), 4),
        "excess_mean": _json_float(excess.mean()),
        "excess_day_mean": _json_float(float(day_means.mean())),
        "excess_ci95": [_json_float(float(np.percentile(boot, 2.5))),
                        _json_float(float(np.percentile(boot, 97.5)))],
        "beat_market_rate": _json_float(float((excess > 0).mean()), 4),
        "t_stat": _json_float(day_means.mean() / se, 2) if se > 0 else None,
        "worst": _json_float(float(np.percentile(fwd, 5))),
        "best": _json_float(float(np.percentile(fwd, 95))),
    }


def explain(desk, symbol: str, *, as_of: str | None = None, k: int = 300,
            same_industry: bool = False, root: Path | None = None) -> dict:
    """Full analog report for one symbol — the kernel's knowledge answer."""
    root = root or Path(__file__).resolve().parents[2]
    day = as_of or desk.market.latest_bar_date()
    raw = features_for(desk.market, symbol, day)
    if raw is None:
        try:
            is_etf = desk.market.instrument(symbol, day or "").is_etf
        except (ValueError, KeyError):
            is_etf = False
        reason = ("ETF 不在类比索引里：索引只覆盖股票（daily），ETF 在 fund_daily；"
                  "用 quant_bars 看它的形态，或用 /core 看目标权重"
                  if is_etf else
                  f"{symbol} 在 {day} 之前的有效交易日不足 {MIN_HISTORY} 天")
        return {"ok": False, "symbol": symbol, "as_of": day, "error": reason}

    index = get_index(desk.market.store, root, end=day)
    i = index.day_index(day)
    if i is None:
        return {"ok": False, "symbol": symbol, "as_of": day,
                "error": f"{day} 不在类比索引里（索引覆盖 {index.meta['start']}..{index.meta['end']}）；"
                         f"数据更新后索引会自动重建"}

    breadth, mkt_mom = float(index.day_mkt[i][0]), float(index.day_mkt[i][1])
    raw_vec = np.array([raw.get(name, 0.0) for name in STOCK_FEATURES] +
                       [breadth, mkt_mom], dtype=np.float32)
    industry = index.industries.get(symbol) or desk.market.industry_of(symbol)
    hits = {h: index.query(raw_vec, day, k=k,
                           same_industry=industry if same_industry else None, horizon=h)
            for h in HORIZONS}
    # Nearest examples prefer the longest available pool, but a missing 10d pool
    # must not suppress valid 1d/3d/5d evidence near the as_of boundary.
    hit = next((hits[h] for h in reversed(HORIZONS) if hits[h].get("rows") is not None),
               {"n": 0, "reason": "no candidate window with a realised forward return"})
    rows = hit.get("rows")
    if rows is None:
        return {"ok": False, "symbol": symbol, "as_of": day,
                "error": hit.get("reason", "no analog")}

    report = {}
    for j, h in enumerate(HORIZONS):
        horizon_hit = hits[h]
        horizon_rows = horizon_hit.get("rows")
        if horizon_rows is None:
            report[f"{h}d"] = {"n": 0}
            continue
        mkt = index.day_mkt_fwd[horizon_rows["day"], j]
        fwd = horizon_rows["fwd"][:, j]
        usable = np.isfinite(fwd) & np.isfinite(mkt)
        excess = fwd[usable] - mkt[usable]
        report[f"{h}d"] = _stats(fwd[usable], excess, horizon_rows["day"][usable])

    nearest = []
    for pos in range(min(5, len(rows["dist"]))):
        r = int(rows["day"][pos])
        c = int(rows["code"][pos])
        nearest.append({
            "date": index.dates[r],
            "symbol": index.codes[c],
            "industry": index.industries.get(index.codes[c], ""),
            "distance": _json_float(float(rows["dist"][pos]), 3),
            "regime": index.day_regime[r] if r < len(index.day_regime) else "",
            "fwd_5d": _json_float(float(rows["fwd"][pos][HORIZONS.index(5)]), 4),
        })

    mean, std = index.day_mean[i], index.day_std[i]
    z_stock = np.clip((raw_vec[:len(STOCK_FEATURES)] - mean) /
                      np.where(std > 1e-12, std, 1.0), -4, 4)
    z_mkt = np.clip((raw_vec[len(STOCK_FEATURES):] - index.mkt_mean) /
                    np.where(index.mkt_std > 1e-12, index.mkt_std, 1.0), -4, 4)
    zs = np.concatenate([z_stock, z_mkt])

    ref = np.array(index.meta.get("dist_ref") or [], dtype=float)
    pctl = (float((ref < hit["nearest_distance"]).mean()) if len(ref)
            else None)
    # 名字从 `quality` 改成 `distance_band`：它说的是最近邻距离落在随机标的的
    # 哪个分位段（tight / normal / far），是几何事实，不是"这次类比好不好"。
    # 把它叫 quality，读者会以为有人评过这次匹配，而实际上只有距离。
    band = ("unknown" if pctl is None else
            "tight" if pctl <= 0.25 else "normal" if pctl <= 0.60 else "far")

    caveats = [
        "horizons 里的收益按次日开盘买入计算，与台面的 next_open 成交一致，且未计佣金、印花税与滑点——扣完成本的版本在 net 块（按账本一笔的大小算）。",
        "置信区间以交易日为重采样单位（同一天的多个样本不独立），所以 n 很大但区间不一定窄。",
          ("同一天常有多个邻居：per-trade 统计（excess_mean / win_rate / beat_market_rate）会被"
           "样本密集的交易日主导，和按日重采样的 excess_ci95 可能符号相反。"
           "超额只引用 excess_day_mean 与 excess_ci95。"),
        "类比来自流动性前 1200 只股票，且要求成交额>=2000 万；小票、ST、北交所不在其中。",
        "regime 由本索引的等权收益序列复算，与 /regime 的口径接近但不完全一致。",
    ]
    if band == "far":
        caveats.insert(0, f"最近邻距离 {hit['nearest_distance']:.2f}，比 {pctl:.0%} 的随机标的都远："
                          "今天这个形态找不到像样的历史片段，这组统计应当降权甚至忽略。")
    if hit["available"] < 500:
        caveats.insert(0, f"可用样本只有 {hit['available']} 条，统计不稳。")

    return {
        "ok": True,
        "symbol": symbol,
        "name": desk.market.names().get(symbol, ""),
        "as_of": day,
        "industry": industry,
        "setup": {key: _json_float(value, 4) for key, value in raw.items()},
        "setup_z": {name: _json_float(float(zs[j]), 2) for j, name in enumerate(FEATURES)},
        "regime": index.day_regime[i] if i < len(index.day_regime) else None,
        "match": {
            "k": len(rows["dist"]),
            "pool": int(hit["available"]),
            "nearest_distance": _json_float(hit["nearest_distance"], 3),
            "median_distance": _json_float(hit["median_distance"], 3),
            "nearest_percentile": _json_float(pctl, 2),
            "distance_band": band,
            "same_industry": industry if same_industry else None,
            "nearest_horizon": hit.get("horizon"),
            "index_window": [index.meta.get("first_bar") or index.meta["start"],
                             index.meta["end"]],
        },
        "horizons": report,
        # The one number no vendor can produce: this history's excess after
        # this book's own fees at the size it actually trades.
        "net": net_of_cost(desk, symbol, day, report),
        "nearest": nearest,
        "caveats": caveats,
        "provenance": {
            "index_rows": index.meta["rows"],
            "index_days": index.meta["days"],
            "built_at": index.meta["built_at"],
            "entry": index.meta["entry"],
            "datasets": ["daily"],
            "method": "weighted kNN on cross-sectional z-scores; block bootstrap by day",
        },
    }


def _sizing_cap_key(desk) -> str:
    """Which single-position cap sizes this trade — the live sleeve's, not CORE's.

    Until 2026-11-13 this was ``max_core_single`` unconditionally, while CORE had
    been retired since 11-01 and every order went through AGGRESSIVE. Every net
    number was therefore computed at 1,500 元 (91.93bps) instead of 2,500 元
    (65.2bps): 27bps of cost no order would ever pay, inherited by §9.3's gate.
    """
    try:
        from service.trading.persona import current as persona_current

        sleeves = persona_current(desk.paper).get("open_sleeves") or []
    except (AttributeError, KeyError, TypeError, ValueError, ImportError):
        sleeves = []
    if "AGGRESSIVE" in sleeves:
        return "max_aggressive_single"
    return "max_core_single"


def net_of_cost(desk, symbol: str, day: str, report: dict) -> dict | None:
    """The excess this name's history shows, minus what *this book* pays to trade it.

    The caveats have always said "未计佣金、印花税与滑点", and every structure the
    ledger killed was killed **after** subtracting cost — "扣 20bps 后无可交易
    残余". The two numbers lived in two different tools and the subtraction was
    left to the reader, so "-0.36% over 5 days" reads as a neutral reading when at
    this account size it is not: a 1,500 元 round trip pays 10 元 of commission
    (the 5 元 minimum twice) plus stamp duty, which is ~70 bps before slippage.

    That is the one number no vendor can give: it is this account's commission
    schedule, this security's stamp duty, this model's slippage, at the size this
    book actually takes.

    Returns ``None`` when the inputs are not available — a missing equity or an
    unpriced instrument is "cannot say", not "zero cost".
    """
    from service.trading.impact import estimate as impact_estimate
    from service.trading.impact import finite as impact_finite
    from service.trading.impact import round_trip_cost_bps

    try:
        equity = float(desk.book.summary()["equity"])
        cap_key = _sizing_cap_key(desk)
        cap = float(desk.limits()[cap_key])
        instrument = desk.market.instrument(symbol, day)
    except (AttributeError, KeyError, TypeError, ValueError):
        # A desk without a book is a test double or an unbuilt kernel — either
        # way this block cannot be computed, and "cannot say" must not be
        # rendered as "costs nothing".
        return None
    notional = equity * cap
    if notional <= 0:
        return None
    charged = round_trip_cost_bps(notional, instrument)
    cost = charged["total_bps"] / 10_000

    net: dict[str, float | None] = {}
    survives: dict[str, bool | None] = {}
    for label, row in report.items():
        excess = row.get("excess_day_mean")
        if excess is None:
            net[label], survives[label] = None, None
            continue
        net[label] = _json_float(excess - cost)
        ci = row.get("excess_ci95") or [None, None]
        # "Survives" is the strict statement: the *whole* interval sits above
        # zero once cost is paid. A point estimate above cost with an interval
        # that straddles zero is noise, and calling that "profitable after fees"
        # is the mistake this block exists to prevent.
        survives[label] = (None if ci[0] is None
                           else bool(ci[0] - cost > 0))
    # Impact is an ESTIMATE, not a charge. ``service/trading/impact.py`` says so
    # in its module docstring: the matcher books fees + a fixed slippage, and a
    # daily bar cannot know the book depth when the order arrived. Folding the
    # estimate into ``cost`` would silently re-price every on-book verdict, so
    # it is reported as a clearly labelled parallel premium instead: the reader
    # sees both numbers and decides how much to trust the estimate.
    impact: dict[str, Any] | None = None
    try:
        snap = desk.market.snapshot(symbol, day)
        hist = desk.market.history(symbol, day, 30)
        col = "adj_close" if "adj_close" in hist.columns else "close"
        sigma = impact_finite(hist[col].astype(float).pct_change().tail(20).std(ddof=1))
        adv = float(desk.market.adv_map(day).get(symbol, 0) or 0.0)
        price = float(snap.last) if snap is not None else 0.0
        if adv > 0 and price > 0 and sigma is not None:
            qty = max(100, int(notional / price // 100 * 100))
            est = impact_estimate(qty, price, adv, sigma)
            impact = {
                "estimated": True,
                "charged": False,
                "impact_bps_one_way": _json_float(est.impact_bps),
                "impact_bps_roundtrip": _json_float(2 * est.impact_bps),
                "impact_cny": _json_float(2 * est.cost_cny, 2),
                "participation": _json_float(est.participation),
                "days_needed": est.days_needed,
                "model": "c * sigma_daily * sqrt(Q / ADV)（平方根律，c=0.4 A股小盘）",
                "note": ("单边估计按双边并列展示；这是估算溢价，不进收费口径，"
                         "也不改变任何在案判决的成本。日线无法知道下单时的盘口深度。"),
            }
        else:
            impact = {"estimated": False, "charged": False,
                      "note": "ADV / 价格 / 波动率不全，冲击不可估（不是 0）"}
    except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError):
        impact = {"estimated": False, "charged": False,
                  "note": "冲击估算所需数据不可读，按『不知道』处理，不是 0"}

    net_if_impact: dict[str, float | None] = {}
    for label, value in net.items():
        extra = (impact.get("impact_bps_roundtrip") or 0) / 10_000 if impact else 0
        net_if_impact[label] = None if value is None else _json_float(value - extra)

    return {
        "notional_cny": round(notional, 2),
        "size_basis": f"账本权益 × {cap_key}（活袖上限），即这一笔真实会下的规模",
        "size_cap": cap_key,
        "cost_bps_roundtrip": charged["total_bps"],
        "fees_bps": charged["fees_bps"],
        "slippage_bps": charged["slippage_bps"],
        "by_horizon": net,
        "survives_cost": survives,
        "impact_premium": impact,
        "by_horizon_if_impact_charged": net_if_impact,
        "spread_bps": None,
        "spread_note": ("买卖价差未建模：日线无逐笔/盘口历史，rt_quote 深度只有当日快照。"
                        "缺数据时正确的回答是『不知道』，不填一个编造的半价差"),
        "note": ("净超额 = excess_day_mean − 往返成本；survives_cost 为 true 才算"
                 "「扣完你自己的费用仍有正超额」（要求整个 95% 区间在 0 上方）。"
                 "impact_premium 是并列的估算溢价、charged=false，不进 by_horizon；"
                 "by_horizon_if_impact_charged 只是『若把它也扣掉会怎样』的敏感性，"
                 "不是账本口径。换规模或换成冲击更大的票，用 "
                 "quant_knowledge(topic=liquidity, symbol=…)。"),
    }


def _episodes(labels: np.ndarray) -> dict[str, int]:
    """How many separate runs each state came from.

    A run of 300 consecutive stressed days is one event, not 300 observations.
    Counting days without counting episodes is how a single crash becomes a
    "sample".
    """
    out: dict[str, int] = {}
    for label in set(labels.tolist()):
        pos = np.flatnonzero(labels == label)
        out[str(label)] = int(1 + np.sum(np.diff(pos) > 1)) if len(pos) else 0
    return out


def regime_table(desk, *, root: Path | None = None) -> dict:
    """What each regime has historically paid, from the same index.

    This is the cheapest useful piece of knowledge in the whole desk: instead of
    asking the model to imagine what "stressed" implies, show it the realised
    distribution over the index window (``MAX_SESSIONS``, about twelve years).
    """
    root = root or Path(__file__).resolve().parents[2]
    index = get_index(desk.market.store, root)
    labels = np.array(index.day_regime)
    out: dict[str, dict] = {}
    for label in ("risk_on", "neutral", "risk_off", "stressed"):
        mask = labels == label
        cnt = int(mask.sum())
        if cnt == 0:
            continue
        row = {"days": cnt,
               "share": _json_float(float(cnt / len(labels)), 3)}
        for j, h in enumerate(HORIZONS):
            series = index.day_mkt_fwd[mask, j]
            series = series[np.isfinite(series)]
            if len(series) == 0:
                continue
            row[f"fwd_{h}d_mean"] = _json_float(float(series.mean()), 4)
            row[f"fwd_{h}d_win"] = _json_float(float((series > 0).mean()), 3)
        out[label] = row
    # How many *separate* episodes each state came from. The caveat has to say so
    # with the real number: hardcoding "281 days" here meant the note kept
    # describing a sample that no longer existed once the window widened.
    episodes = _episodes(labels)

    current = None
    try:
        from .regime import compute

        current = compute(desk.market).label
    except (ValueError, KeyError):
        current = None
    stressed_days = int((labels == "stressed").sum())
    return {
        "ok": True,
        "as_of": index.meta["end"],
        "current": current,
        "by_regime": out,
        "episodes": episodes,
        "note": ("等权全市场（流动性前 1200）在每种状态下的前瞻收益分布；"
                 "这是背景知识，不是你该不该开仓的依据。"),
        "caveats": [
            (f"同一段行情会被切成连续很多天，样本不独立：{stressed_days} 个 stressed "
             f"交易日其实只来自 {episodes.get('stressed', 0)} 段连续的波动事件。"),
            (f"窗口是 {index.meta['start']} 到 {index.meta['end']}"
             f"（{len(labels)} 个交易日），这不是'长期规律'，只是这些年。"),
            "状态由等权序列的 MA200/波动分位/宽度复算，与 /regime 的口径接近但不完全一致。",
        ],
        "provenance": {"index_rows": index.meta["rows"], "built_at": index.meta["built_at"]},
    }
