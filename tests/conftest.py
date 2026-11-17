"""Process-wide test paths, configured before any test module imports the API."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_TMP = tempfile.TemporaryDirectory(prefix="quant-desk-tests-")
_TEST_ROOT = Path(_TEST_TMP.name)
_TEST_DATA = _TEST_ROOT / "data"

# Force overrides rather than inheriting a developer's production paths. The API
# constructs stores on first access, so every import during collection is safe.
os.environ["PAPER_DB"] = str(_TEST_ROOT / "paper.sqlite")
os.environ["QUANT_DATA_ROOT"] = str(_TEST_DATA)
os.environ["QUANT_REGISTRY_DB"] = str(_TEST_DATA / "registry.sqlite")
# `security.py` reads QUANT_TOKEN at import time and 17 test modules hard-code the
# default that ships in the repo. Rotating the operator's real token in `.env`
# therefore turned the suite red for a reason that had nothing to do with the
# code — the exact false alarm that makes people distrust a rotation. Pin the
# test token here, before any module imports the API.
os.environ["QUANT_TOKEN"] = "change-me-local-only"

# ``desk`` and its two session-scoped dependencies are declared in
# ``tests/trading/conftest.py``. When pytest is handed an explicit list of files
# spanning several directories, that conftest can be registered *after* a
# trading test module's fixture closure was computed, leaving the module with
# only its own fixtures (``fixture 'desk' not found``, 12 errors — measured
# 2026-11-07; reversing the same six arguments gave 47 passed). Importing the
# fixture objects here registers them on the root plugin, which every node sees
# regardless of collection order. A fixture is a decorated function object, so
# importing it is enough; the same fixtures still win by proximity in their own
# directory. No fixture changes scope or behaviour.
from tests.trading.conftest import (  # noqa: F401
    desk,
    real_registry_copy,
    shared_market,
)
