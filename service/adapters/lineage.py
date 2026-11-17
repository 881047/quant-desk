"""Decision lineage: bind every decision to the exact data version behind it.

``snapshot_id`` is content-addressed, so a decision made on 2026-10-23 can be
replayed against the same bytes even after months of later ingestion. P2's
journal writes this record; P1 only needs to produce it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .parquet_store import DatasetStore

DECISION_DATASETS = (
    "daily", "adj_factor", "stk_limit",
    "limit_up_pool", "broken_board_pool", "limit_down_pool",
    "stock_basic", "trade_cal",
)


@dataclass(frozen=True)
class DataFingerprint:
    as_of: str
    captured_at: str
    snapshots: dict[str, str | None] = field(default_factory=dict)
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "captured_at": self.captured_at,
            "complete": self.complete,
            "missing": list(self.missing),
            "snapshots": self.snapshots,
        }


def collect(store: DatasetStore, as_of: str, datasets=None) -> DataFingerprint:
    # The default keeps the historical decision contract.  Consumers that know
    # their actual evidence set can add those datasets instead of mistaking a
    # fixed core list for complete provenance.
    extra = (datasets,) if isinstance(datasets, str) else (datasets or ())
    names = list(dict.fromkeys((*DECISION_DATASETS, *extra)))
    snapshots = {name: store.snapshot_for(name, as_of) for name in names}
    missing = tuple(name for name, snap in snapshots.items() if snap is None)
    return DataFingerprint(
        as_of=as_of,
        captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        snapshots=snapshots,
        missing=missing,
    )
