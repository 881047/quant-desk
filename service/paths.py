"""Where mutable state lives — one resolver, because two is how it splits.

Three places used to answer "where is the ledger" on their own:

* ``build_desk``            — ``root / "data" / "paper.sqlite"``
* ``app._get_usage``        — ``<repo>/data/paper.sqlite`` unless ``PAPER_DB``
* ``service/api/data.py``   — ``QUANT_DATA_ROOT`` else ``<repo>/data``

The first two agreed only while ``QUANT_DATA_ROOT`` was unset — which is the
case in production, so nothing ever failed. Point the variable elsewhere (the
documented way to move the data) and the datasets move, the ledger moves, and
the request counters keep writing to the old file: ``/usage`` reads
``desk.paper``, so it reports "this endpoint was never called" while the rows
pile up in a database nobody reads. No exception, no log line.

``paper_db`` is a function rather than a constant because both variables are
allowed to be set after import; the test suite depends on that, and so does any
future caller that builds a desk against a temporary root.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def data_dir(root: Path | None = None) -> Path:
    """The directory dataset readers use: ``QUANT_DATA_ROOT``, else ``<root>/data``.

    ``service/api/data.py`` has always read the variable as *the data
    directory*. ``build_desk`` used to treat it as the parent of one, so unless
    the value happened to end in ``data`` the API and the desk opened two
    different registries and two different parquet trees.
    """
    return Path(os.getenv("QUANT_DATA_ROOT") or ((root or ROOT) / "data"))


def paper_db(directory: Path | None = None) -> Path:
    """The ledger file: ``PAPER_DB`` if set, else ``<data dir>/paper.sqlite``.

    ``directory`` is the already-resolved data root, for callers that were
    handed one explicitly (tests, the samples book). Passing it keeps the
    ledger next to the datasets it was built from.
    """
    override = os.getenv("PAPER_DB")
    if override:
        return Path(override)
    base = Path(directory) if directory is not None else data_dir()
    return base / "paper.sqlite"
