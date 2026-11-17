"""What a verdict was computed from, as one short string.

Two runs of the same spec can disagree for exactly three reasons: the code or
configuration changed, the window changed, or the data changed. The window is
already recorded (``window_start`` / ``window_end``); this module records the
other identities, including the datasets the run actually read.

The gap it closes: on 2026-10-26 `broken_board_deepest` was recorded as
`effect=-0.0096, n=915`. Re-run on 10-29 with the *old* constants it produced
`-0.0150, n=914`, and P23 had a third number (`n=868`). Because the row carried
no data identity, there was no way to tell whether the engine had drifted or the
panel had been rebuilt — and the answer mattered, because `limit_pool_t0`'s 2026
partition *had* been rewritten that afternoon.

Scope matters more than precision here. Hashing every partition would flip the
fingerprint whenever a decade-old slice is re-saved, which is exactly the noise
that trains a reader to ignore the signal. Only partitions that overlap the
window are included.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from service import settings

# Compatibility fallback for direct callers. The research runner uses the real
# reads observed by ReadTracker instead: stock studies also read stk_limit and
# attribution reads daily_basic, so a hand-maintained list cannot be the truth.
DATASETS = ("daily", "limit_pool_t0")
ROOT = Path(__file__).resolve().parents[2]


class ReadTracker:
    """A transparent store proxy that records every dataset actually read."""

    def __init__(self, store):
        self._store = store
        self._datasets: set[str] = set()

    @property
    def datasets(self) -> tuple[str, ...]:
        return tuple(sorted(self._datasets))

    def read(self, dataset: str, *args, **kwargs):
        name = str(dataset)
        self._datasets.add(name)
        return self._store.read(dataset, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._store, name)


def implementation(*, root: Path | None = None, config_values: dict | None = None) -> dict:
    """Content identity of the code and complete effective configuration.

    ``/health`` deliberately uses path/mtime/size to answer a cheap operational
    question. Research needs a different guarantee: a same-size edit with a
    preserved timestamp is still a different implementation. Hash file contents
    for that reason. Configuration covers every file under ``configs`` plus the
    resolved ``settings.load()`` values after local and environment overrides.
    Only hashes are returned; configuration values and secrets are not exposed.
    """
    base = Path(root) if root is not None else ROOT
    code = hashlib.sha256()
    service = base / "service"
    if service.exists():
        for path in sorted(service.rglob("*.py")):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            code.update(path.relative_to(base).as_posix().encode("utf-8"))
            code.update(b"\0")
            code.update(path.read_bytes())
            code.update(b"\0")

    config = hashlib.sha256()
    config_dir = base / "configs"
    if config_dir.exists():
        files = [p for p in config_dir.rglob("*")
                 if p.is_file() and p.suffix.lower() in {".yaml", ".yml", ".json"}]
        for path in sorted(files):
            config.update(path.relative_to(base).as_posix().encode("utf-8"))
            config.update(b"\0")
            config.update(path.read_bytes())
            config.update(b"\0")
    values = settings.load() if config_values is None else config_values
    resolved = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str,
                          separators=(",", ":"))
    config.update(b"effective-settings\0")
    config.update(resolved.encode("utf-8"))
    return {"code_fingerprint": code.hexdigest()[:16],
            "config_fingerprint": config.hexdigest()[:16]}


def of(store, start: str, end: str, datasets: tuple[str, ...] = DATASETS) -> str:
    """A stable 16-character identity for the data a window actually touches.

    Built from content checksums, not snapshot ids: `make_snapshot_id` mixes in
    the ingest time, so a byte-identical re-write would look like a change. The
    question this answers is "is the panel different", not "was it saved again".
    """
    registry = getattr(store, "registry", None)
    if not datasets:
        return ""
    if registry is None:
        # A verdict must not fail because its provenance could not be fingerprinted.
        # Test doubles and older callers pass a store without a manifest database.
        return ""
    parts: list[str] = []
    for name in datasets:
        try:
            records = registry.list_manifest(name)
        except (OSError, ValueError):
            continue
        for rec in records:
            if not (rec.cover_start and rec.cover_end):
                continue
            if rec.cover_end < start or rec.cover_start > end:
                continue
            parts.append(f"{name}:{rec.partition}:{rec.checksum[:16]}")
    parts.sort()
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def explain(store, start: str, end: str, datasets: tuple[str, ...] = DATASETS) -> dict:
    """The parts behind :func:`of`, for when a fingerprint needs reading."""
    registry = getattr(store, "registry", None)
    out: dict[str, dict[str, str]] = {}
    for name in datasets:
        if registry is None:
            break
        try:
            records = registry.list_manifest(name)
        except (OSError, ValueError):
            continue
        slices = {rec.partition: rec.checksum[:16] for rec in records
                  if rec.cover_start and rec.cover_end
                  and not (rec.cover_end < start or rec.cover_start > end)}
        if slices:
            out[name] = dict(sorted(slices.items()))
    return {"start": start, "end": end, "fingerprint": of(store, start, end, datasets),
            "datasets": out,
            "without_window_slices": [name for name in datasets if name not in out]}


def reproducibility(store, start: str, end: str, datasets: tuple[str, ...]) -> dict:
    """All identities needed to inspect one run, including empty/error runs."""
    names = tuple(sorted(set(datasets)))
    data = explain(store, start, end, names)
    return {
        **implementation(),
        "data_fingerprint": data["fingerprint"],
        "data_inputs": list(names),
        "data_slices": data["datasets"],
        "data_without_window_slices": data["without_window_slices"],
        "window": [start, end],
    }
