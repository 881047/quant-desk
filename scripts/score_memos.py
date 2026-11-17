"""Score open memos against later data and print the calibration summary."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from service.research.memo import score_memos
from service.trading.state import PaperStore


def main() -> int:
    registry = Registry(ROOT / "data" / "registry.sqlite")
    store = DatasetStore(ROOT / "data", registry)
    paper = PaperStore(Path(os.getenv("PAPER_DB", ROOT / "data" / "paper.sqlite")))
    result = score_memos(store, paper)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
