"""Fixtures and synthetic frames for the data layer tests (no network).

**Fixtures only.** Plain helpers live in :mod:`tests.data.helpers`; see
``tests/trading/helpers.py`` for why a conftest must not double as a helper
library. The names are re-exported for backwards compatibility.
"""

from __future__ import annotations

import pytest

from service.adapters.parquet_store import DatasetStore
from service.adapters.sqlite_store import Registry
from tests.data.helpers import make_adj, make_daily  # noqa: F401 - re-exported


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "registry.sqlite")


@pytest.fixture
def store(tmp_path, registry):
    return DatasetStore(tmp_path, registry)
