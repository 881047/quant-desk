"""What usually happened after a market that looked like this.

`discover` reports today's readings. A reading is not a prior: "breadth 19.7%"
tells the model nothing about whether that is unusual, and it certainly does not
tell it what tends to follow. This module turns the reading into a retrieval key
— the market's own history, bucketed by state — and returns the forward
distribution that goes with the bucket.

Three rules, in order of how easy they are to get wrong:

* **Point in time.** The percentile of day ``d`` is computed from a trailing
  window that ends on ``d``. Cutting the panel after ``d`` must not change it;
  there is a test for exactly that.
* **You cannot buy the close you decided on.** Forward returns start at the
  *next* open, so the overnight gap after the signal is not credited to the
  trade. This is the same convention the research layer uses, and it is the
  difference between a prior and a flattering prior.
* **Report the sample.** Every bucket carries ``n``. A bucket with twelve
  observations is a story, not a distribution, and the payload says so.

The state is deliberately coarse: breadth tercile × 20-day return tercile gives
nine cells, which on the desk's 920-session sample is roughly a hundred days
each. Adding volatility as a third axis sounds better and produces cells with
``n < 30``, which is how a "conditional distribution" becomes a rumour.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from service import settings
from service.kernel.units import TRADING_DAYS_PER_YEAR

# Short windows first: a 1-2 day question asked of a 20-day distribution gets a
# confident answer to a different question. Personas pick from here, they do not
# invent their own.
HORIZONS = (1, 2, 5, 20)
# Bumped whenever the built columns change. The parquet cache is keyed on the last
# stored session only, so without this a schema change keeps serving a frame that
# is missing the new columns — silently, until something asks for one.
SCHEMA = "v2"
PCT_WINDOW = settings.integer("research.pct_window")
PCT_MIN = settings.integer("research.pct_min")
MIN_BUCKET_N = settings.integer("research.min_bucket_n")
# A bucket that appears for five consecutive days is not five observations of
# "what happens next". Requiring episodes keeps a long regime from impersonating
# a sample.
MIN_EPISODES = settings.integer("research.state_min_episodes")
TERCILE_LOW = 1 / 3
TERCILE_HIGH = 2 / 3


def _tercile(pct: float | None) -> str | None:
    if pct is None or not np.isfinite(pct):
        return None
    if pct <= TERCILE_LOW:
        return "low"
    if pct >= TERCILE_HIGH:
        return "high"
    return "mid"


def _rolling_pct(series: pd.Series, window: int = PCT_WINDOW,
                 min_periods: int = PCT_MIN) -> pd.Series:
    """Rank of each value inside its own trailing window, in [0, 1].

    ``raw=True`` keeps this on numpy: the panel is under a thousand rows, but a
    python-level lambda per window makes the whole build feel broken.
    """
    def rank_last(window_values: np.ndarray) -> float:
        current = window_values[-1]
        if not np.isfinite(current):
            return np.nan
        valid = window_values[np.isfinite(window_values)]
        if len(valid) < min_periods // 2:
            return np.nan
        return float((valid < current).mean())

    return series.rolling(window, min_periods=min_periods).apply(rank_last, raw=True)


def build_state(daily: pd.DataFrame, *, start: str | None = None) -> pd.DataFrame:
    """Daily market state with forward returns, indexed by trade date.

    Columns: the readings, their trailing percentiles and terciles, the bucket,
    and ``fwd_{h}d`` — the return from the next open to the close ``h`` sessions
    later.
    """
    cols = ["ts_code", "trade_date", "open", "close", "pre_close"]
    df = daily[[c for c in cols if c in daily.columns]].copy()
    df["trade_date"] = df["trade_date"].astype(str)
    if start:
        df = df[df["trade_date"] >= str(start)]
    for col in ("open", "close", "pre_close"):
        value = pd.to_numeric(df[col], errors="coerce")
        df[col] = value.where(value > 0)
    df = df.dropna(subset=["open", "close", "pre_close"])
    if df.empty:
        return pd.DataFrame()

    # Equal-weight market legs, same construction as research.market_returns:
    # a portfolio rebalanced daily, not a cap-weighted index.
    df["gap"] = df["open"] / df["pre_close"] - 1
    df["intra"] = df["close"] / df["open"] - 1
    legs = df.groupby("trade_date")[["gap", "intra"]].mean().sort_index()
    legs["cc"] = (1 + legs["gap"]) * (1 + legs["intra"]) - 1
    level = (1 + legs["cc"]).cumprod()

    # Breadth: share of listed stocks above their own moving average.
    #
    # Measured on an ex-rights-adjusted index, not on raw close. `pre_close` on
    # an ex-date is the exchange's reference price, so `close/pre_close` is the
    # true overnight return; chaining it gives a total-return price series per
    # symbol, which is what a moving average has to be computed on. Raw close
    # makes a 10-for-1 split read as a 90% collapse for the next 60 sessions.
    df = df.sort_values(["ts_code", "trade_date"], kind="mergesort")
    df["px"] = (1.0 + df["close"] / df["pre_close"] - 1.0).groupby(
        df["ts_code"], sort=False).cumprod()
    grouped = df.groupby("ts_code", sort=False)["px"]
    for days, name in ((20, "breadth20"), (60, "breadth60")):
        ma = grouped.transform(lambda s, d=days: s.rolling(d, min_periods=d).mean())
        above = (df["px"] > ma).astype(float)
        above[ma.isna()] = np.nan
        legs[name] = above.groupby(df["trade_date"]).mean()

    legs["ew_ret_20d"] = level / level.shift(20) - 1
    legs["vol20"] = legs["cc"].rolling(20).std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR)

    state = pd.DataFrame(index=legs.index)
    state["breadth20"] = legs["breadth20"]
    state["ew_ret_20d"] = legs["ew_ret_20d"]
    state["vol20"] = legs["vol20"]

    for name in ("breadth20", "ew_ret_20d", "vol20"):
        state[f"{name}_pct"] = _rolling_pct(state[name])
        state[f"{name}_tercile"] = state[f"{name}_pct"].map(_tercile)
    state["bucket"] = (state["breadth20_tercile"].fillna("?") + "|"
                       + state["ew_ret_20d_tercile"].fillna("?"))

    # Forward returns: buy at the next open, so skip that session's gap.
    nxt_open_gap = legs["gap"].shift(-1)
    for h in HORIZONS:
        ahead = level.shift(-h) / level - 1
        state[f"fwd_{h}d"] = (1 + ahead) / (1 + nxt_open_gap) - 1

    state["close_level"] = level
    return state


def available_horizons(state: pd.DataFrame) -> tuple[int, ...]:
    """Which forward windows this frame actually carries.

    ``HORIZONS`` is what the builder produces; a frame read from an older cache,
    or a hand-built one in a test, may carry fewer. Readers iterate this instead,
    so a schema bump cannot turn into a KeyError deep inside a percentile loop.
    """
    return tuple(h for h in HORIZONS if f"fwd_{h}d" in state.columns)


def episode_means(state: pd.DataFrame, bucket: str, horizon: int) -> np.ndarray:
    """One observation per contiguous run of a bucket.

    Days inside a run share almost the whole forward window, so a bucket that
    persists for forty sessions is one episode, not forty observations. Every
    inference about a bucket uses this, and so does the ``edge_*`` figure — two
    numbers for the same thing, weighted differently, is worse than one.
    """
    col = f"fwd_{horizon}d"
    mask = state["bucket"] == bucket
    if not mask.any() or col not in state.columns:
        return np.array([])
    runs = (mask != mask.shift(1, fill_value=False)).cumsum()
    values = state.loc[mask, col]
    grouped = values.groupby(runs[mask]).mean().dropna()
    return grouped.to_numpy(dtype=float)


def _distribution(values: pd.Series) -> dict:
    v = values.dropna()
    if v.empty:
        return {"n": 0}
    return {
        "n": len(v),
        "mean": round(float(v.mean()), 4),
        "p25": round(float(v.quantile(0.25)), 4),
        "p50": round(float(v.quantile(0.50)), 4),
        "p75": round(float(v.quantile(0.75)), 4),
        "win": round(float((v > 0).mean()), 3),
        "worst": round(float(v.min()), 4),
        "best": round(float(v.max()), 4),
    }


def bucket_history(state: pd.DataFrame, bucket: str, *, recent_days: int = 252,
                   regime_days: int = 756) -> dict:
    """Forward distribution for one bucket, and the same bucket in recent regimes.

    ``n`` counts sessions; ``episodes`` counts contiguous runs. They are not the
    same number and the second one is the honest one: five consecutive days in
    the same bucket share four days of the same forward window, so 250 sessions
    can be eight independent observations of "what happens next".

    The 1-year and 3-year slices exist because the full sample is not a single
    regime. ``low|low`` over 2005-2026 has a 20-day median of +3.2%; restricted to
    the last three years it is +1.5%. Both are true, and which one applies
    depends on whether 2026 looks like the average of twenty years — which is a
    judgement the model should make with the two numbers in front of it, not one.
    """
    rows = state[state["bucket"] == bucket]
    if rows.empty:
        return {"bucket": bucket, "n": 0}
    horizons = available_horizons(state) or HORIZONS
    mask = (state["bucket"] == bucket).sort_index()
    starts = mask & ~mask.shift(1, fill_value=False)
    fwd_key = f"fwd_{horizons[0]}d"

    # Slices are cut on the *calendar*, not on the bucket's own rows. Taking
    # ``tail(252)`` of a 76-row bucket keeps all 76 and reports "100% recent",
    # which is how a bucket spanning 2007-2026 claimed to be one year of data.
    sessions = state.index.sort_values()
    cut_recent = sessions[-recent_days] if len(sessions) >= recent_days else sessions[0]
    cut_regime = sessions[-regime_days] if len(sessions) >= regime_days else sessions[0]
    recent_rows = rows[rows.index >= cut_recent]
    regime_rows = rows[rows.index >= cut_regime]

    out: dict = {
        "bucket": bucket,
        "n": int(rows[fwd_key].notna().sum()),
        "episodes": int(starts.sum()),
        "span": [str(rows.index.min()), str(rows.index.max())],
        "last_seen": str(rows.index.max()),
    }
    for h in horizons:
        col = f"fwd_{h}d"
        out[col] = _distribution(rows[col])
        out[f"{col}_3y"] = _distribution(regime_rows[col])
    recent_n = int(recent_rows[fwd_key].notna().sum())
    out["n_recent"] = recent_n
    out["recent_share"] = round(recent_n / out["n"], 3) if out["n"] else 0.0

    ok = out["n"] >= MIN_BUCKET_N and out["episodes"] >= MIN_EPISODES
    out["usable"] = ok
    notes = []
    if out["n"] < MIN_BUCKET_N:
        notes.append(f"n={out['n']} < {MIN_BUCKET_N}")
    if out["episodes"] < MIN_EPISODES:
        notes.append(f"段数={out['episodes']} < {MIN_EPISODES}")
    if notes:
        notes.append("这是几次巧合，不是分布；把它当成先验会得到一个自信的错误。")
    if out["recent_share"] >= 0.7 and out["n"] >= MIN_BUCKET_N:
        notes.append(f"{out['recent_share']:.0%} 的样本落在最近一年，"
                     "这更接近一段行情的重复计数，而不是跨周期的历史。")
    if notes:
        out["note"] = " ".join(notes)
    return out


def reading(state: pd.DataFrame, day: str | None = None) -> dict:
    """Today's state, its percentiles, and the bucket it belongs to."""
    if state.empty:
        return {"ok": False, "error": "no state history"}
    day = day or str(state.index.max())
    if day not in state.index:
        return {"ok": False, "error": f"{day} 不在状态序列里（可能是非交易日或预热期）"}
    row = state.loc[day]
    bucket = str(row["bucket"])
    if "?" in bucket:
        return {"ok": False, "day": day,
                "error": "预热期：任一分位历史不足，这一天无法分类",
                "warmup_days": PCT_MIN}
    # A historical reading is a snapshot, not a row lookup. Forward distributions
    # and baselines must be cut at the same date or a request for D silently uses
    # outcomes that only became known after D.
    history_state = state.loc[:day].copy()
    for horizon in available_horizons(history_state):
        # Rows at the edge have labels ending after D. Even if a caller supplied
        # a full precomputed frame, those outcomes were not observable at D.
        if horizon:
            history_state.iloc[-horizon:, history_state.columns.get_loc(f"fwd_{horizon}d")] = np.nan

    def cell(name: str, digits: int = 4) -> dict:
        value = row[name]
        pct = row[f"{name}_pct"]
        out = {
            "value": None if pd.isna(value) else round(float(value), digits),
            "pct": None if pd.isna(pct) else round(float(pct), 3),
            "tercile": str(row[f"{name}_tercile"]),
        }
        return out

    hist = bucket_history(history_state, bucket)
    # The bucket's own distribution is not the answer to "does this state tell me
    # anything" — an average day also has a distribution, and in A-shares it
    # drifts up. Comparing against zero would make every bucket look like edge.
    horizons = available_horizons(history_state)
    baseline = {f"fwd_{h}d": _distribution(history_state[f"fwd_{h}d"]) for h in horizons}
    for h in horizons:
        episodes = episode_means(history_state, bucket, h)
        base_mean = baseline[f"fwd_{h}d"].get("mean")
        if len(episodes) and base_mean is not None:
            # Episode-weighted, exactly like ``claim.judge_bucket``: a state that
            # persists for forty sessions must not outvote forty short ones.
            hist[f"edge_{h}d"] = round(float(episodes.mean() - base_mean), 4)
    return {
        "ok": True,
        "as_of": day,
        "state": {
            "breadth20": cell("breadth20", 3),
            "ew_ret_20d": cell("ew_ret_20d"),
            "vol20": cell("vol20"),
        },
        "bucket": bucket,
        "history": hist,
        "baseline": {
            **baseline,
            "note": "无条件（全样本平均日）同口径分布；edge_* 是桶均值减它，不是减 0",
        },
        "coverage": {
            "sessions": len(history_state),
            "classified": int((~history_state["bucket"].str.contains(r"\?", regex=True)).sum()),
            "pct_window": PCT_WINDOW,
            "pct_min_periods": PCT_MIN,
        },
        "forward_basis": "次日开盘 → 第 h 个交易日收盘（不含信号日之后那根跳空）",
        "caveats": [
            "桶是粗的：宽度三分位 × 20 日收益三分位，共 9 格。粗是为了让每格有样本。",
            "分位只用当日及之前 252 个交易日，未来数据不参与。",
            "分布是等权市场的，不是某只票的；个股还要叠加自身条件。",
            "无条件的等权市场本身有正漂移，所以桶的绝对收益不能直接读成 edge。",
            ("edge_* 按段加权（同一状态的连续天数算一段）；按日加权可由 fwd_*.mean 减 "
             "baseline.fwd_*.mean 得到。两者差得越多，说明「状态持续越久越不一样」。"),
        ],
    }


def bucket_table(state: pd.DataFrame) -> list[dict]:
    """Every bucket with its sample size — the check before trusting any of them."""
    rows = [b for b in state["bucket"].unique() if "?" not in str(b)]
    out = []
    for bucket in sorted(rows):
        hist = bucket_history(state, bucket)
        out.append({
            "bucket": bucket,
            "n": hist.get("n", 0),
            "episodes": hist.get("episodes", 0),
            "n_recent": hist.get("n_recent", 0),
            "usable": hist.get("usable", False),
            "fwd_5d_p50": (hist.get("fwd_5d") or {}).get("p50"),
            "fwd_20d_p50": (hist.get("fwd_20d") or {}).get("p50"),
            "fwd_20d_win": (hist.get("fwd_20d") or {}).get("win"),
        })
    return out


# ------------------------------------------------------------------- caching
def _manifest_digest(store) -> str:
    """Fingerprint of every partition the store admits to holding.

    ``checksum`` and ``snapshot_id`` are content-addressed — the writer derives
    them from the frame it stored — so this string changes exactly when the data
    does. That makes it a *sufficient* key for a read of the store's own data over
    a fixed window, and it costs one SQLite query instead of hashing every cell.
    """
    registry = getattr(store, "registry", None)
    if registry is None:
        return ""
    try:
        if hasattr(registry, "list_manifest"):
            manifest = [{key: getattr(row, key, None) for key in (
                "dataset", "partition", "checksum", "schema_hash", "snapshot_id",
                "cover_start", "cover_end", "status", "ingested_at")}
                for row in registry.list_manifest()]
        else:
            manifest = registry.summary()
    except Exception:  # noqa: BLE001 - a manifest cannot disable cache reads
        return ""
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _window_digest(manifest_digest: str, start: str, end: str) -> str:
    """Cache key for "the store's own daily rows between start and end".

    Nothing else enters it, and nothing else needs to: the manifest pins the
    bytes, and the window pins which of them are returned. The previous key
    hashed the 16-million-row frame on every call, which is 2.6 seconds to
    re-derive information the writer already computed once.
    """
    return hashlib.sha256(f"{manifest_digest}|{start}|{end}".encode()).hexdigest()


def _content_digest(store, *frames: pd.DataFrame) -> str:
    """Fingerprint a caller-supplied frame, plus the manifest it came from.

    Still hashes every row, because a frame handed in by a caller has no manifest
    of its own: two different slices can carry the same columns and the same last
    date, and a cache hit on the wrong one would report a state built from other
    data. Cheap where it can be, exact where it must be.
    """
    digest = hashlib.sha256()
    for frame in frames:
        digest.update("|".join(map(str, frame.columns)).encode("utf-8"))
        digest.update(pd.util.hash_pandas_object(frame, index=True).values.tobytes())
    digest.update(_manifest_digest(store).encode("utf-8"))
    return digest.hexdigest()


def cached_state(store, *, start: str = "20050101", cache_dir=None,
                 daily: pd.DataFrame | None = None,
                 last: str | None = None, as_of: str | None = None) -> pd.DataFrame:
    """``build_state`` with a one-file cache keyed on the last stored session.

    The build reads 16M rows and takes about seven seconds; a desk that asks
    "what does today look like" several times an hour should not pay that each
    time. The cache is invalidated by the *last trade date in the store*, not by
    wall-clock time: a stale cache would report yesterday's state as today's,
    which is the one thing this module exists to prevent.

    ``last`` lets a caller that already knows the last stored session skip the
    read it would otherwise need just to check the cache. Passing it wrong is not
    a correctness hazard — the cache stamp is still compared field by field, and
    a mismatch falls back to the full build.
    """
    root = Path(__file__).resolve().parents[2]
    directory = Path(cache_dir) if cache_dir else root / "data" / "cache"
    directory.mkdir(parents=True, exist_ok=True)
    parquet = directory / f"state_{start}.parquet"
    meta_path = directory / f"state_{start}.meta.json"

    def _hit(want: str | None, source_digest: str | None = None) -> pd.DataFrame | None:
        if want is None or not (parquet.exists() and meta_path.exists()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if meta.get("last_date") != str(want) or meta.get("schema") != SCHEMA:
            return None
        if source_digest is not None and meta.get("source_digest") != source_digest:
            return None
        cached = pd.read_parquet(parquet)
        cached.index = cached.index.astype(str)
        return cached.loc[:str(as_of)] if as_of else cached

    end = str(as_of) if as_of else "20991231"
    supplied = daily is not None
    manifest_digest = _manifest_digest(store)
    # A caller that knows the last session and reads the store's own data can be
    # answered without touching a row: the manifest pins the bytes and the window
    # pins the slice. This is the path `discover` takes, and the docstring above
    # promises it is free — before this it hashed 16M rows to check.
    window_key = (_window_digest(manifest_digest, start, end)
                  if manifest_digest and not supplied else None)
    # ``_hit`` is *always* asked for a content digest now. It used to accept a
    # last-date-only stamp when ``as_of`` was None, which meant a corrected or
    # backfilled partition whose final session had not moved was served from the
    # old frame: the percentiles, the bucket and the ``usable`` flags all stayed
    # wrong until the last date changed. A cache that cannot tell "same bytes"
    # from "same end date" is a cache that lies once a month.
    if last is not None and (window_key is not None or not supplied):
        hit = _hit(str(last), window_key)
        if hit is not None:
            return hit

    frame = daily if supplied else store.read("daily", start, end)
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.copy()
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame = frame[(frame["trade_date"] >= str(start)) & (frame["trade_date"] <= end)]
    if frame.empty:
        return pd.DataFrame()
    last = str(frame["trade_date"].max())
    # A supplied frame is opaque, so it is hashed in full; a frame this function
    # read itself is already described by the window key.
    source_digest = (window_key if window_key is not None
                     else _content_digest(store, frame))
    hit = _hit(last, source_digest)
    if hit is not None:
        return hit

    state = build_state(frame, start=start)
    if state.empty:
        return state
    out = state.copy()
    out.index = out.index.astype(str)
    try:
        out.to_parquet(parquet)
        meta_path.write_text(json.dumps({"last_date": last, "start": start,
                                         "schema": SCHEMA, "source_digest": source_digest,
                                         "sessions": len(out)}), encoding="utf-8")
    except OSError:
        pass  # a cache that cannot be written must not stop the answer
    return out


def block(state: pd.DataFrame, day: str | None = None,
          horizons: tuple[int, ...] | list[int] | None = None) -> dict:
    """The compact form that goes into a context pack.

    Answers the three questions a reading cannot: how unusual is this, what
    followed, and how much of that is sample rather than story.

    ``horizons`` trims it to the windows the caller's question is actually about.
    A one-day question does not need a twenty-day distribution: carrying it costs
    bytes *and* attention, and a small model reads whatever it is given.
    """
    present = available_horizons(state)
    wanted = tuple(present if horizons is None else horizons)
    unknown = [h for h in wanted if h not in HORIZONS]
    if unknown:
        raise ValueError(f"unknown horizons {unknown}; built: {list(HORIZONS)}")
    wanted = tuple(h for h in wanted if h in present)

    result = reading(state, day)
    if not result.get("ok"):
        return result
    base = result.get("baseline") or {}
    src = result["history"]

    compact_base: dict = {}
    compact_hist: dict = {
        "n": src.get("n"),
        "episodes": src.get("episodes"),
        "usable": src.get("usable"),
    }
    for h in wanted:
        dist = base.get(f"fwd_{h}d") or {}
        if dist:
            compact_base[f"fwd_{h}d"] = {k: dist.get(k) for k in ("mean", "p50", "win")}
        compact_hist[f"fwd_{h}d"] = src.get(f"fwd_{h}d")
        compact_hist[f"edge_{h}d"] = src.get(f"edge_{h}d")
    if 20 in wanted:
        compact_hist["fwd_20d_3y"] = src.get("fwd_20d_3y")
    compact_hist["recent_share"] = src.get("recent_share")
    compact_hist["note"] = src.get("note")

    return {
        "ok": True,
        "as_of": result["as_of"],
        "bucket": result["bucket"],
        "state": result["state"],
        "horizons": list(wanted),
        "history": compact_hist,
        "baseline": compact_base,
        "forward_basis": result["forward_basis"],
        # Dropped in the first version, and the one thing a reader needs to not
        # over-trust the field: `edge_1d: 0.0037` with no definition reads as
        # "0.37% of free money". It is a bucket mean minus an unconditional mean,
        # episode-weighted, with no interval attached.
        "edge_definition": ("edge_h = 同桶各段均值 − 全样本无条件均值（按连续段加权，不是按日）；"
                            "不是可赚收益，没有区间，也没有扣成本。看 distribution 的 p25/p75 再说话。"),
    }
