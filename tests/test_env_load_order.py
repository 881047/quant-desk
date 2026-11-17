"""`.env` must be read *before* `service.settings` reads its values.

The bug this pins (2026-11-13 review, F-01): `service.settings` calls `load()`
at the bottom of the module, i.e. at import time, while `.env` was loaded from
`service.api.security` — which `service.api.app` imports *after* settings. So a
`QUANT__RISK__MAX_POSITIONS=1` written in `.env` (the place `.env.example`
tells operators to put it) was silently ignored, and `/health` could not even
report it: `provenance()` only marks a key `source="env"` if the override was
actually applied.

The assertion is a subprocess on purpose. Import order is the thing under test,
and the test process has already imported `service` (so `.env` is loaded and
`settings` is cached) — checking in-process would pass no matter what.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

#: A key that ships in `.env` and cannot plausibly be set by the test runner.
SENTINEL = "QUANT_TOKEN"


def _dotenv_has_sentinel() -> bool:
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.strip().startswith(f"{SENTINEL}=") for line in text.splitlines())


def test_settings_import_loads_dotenv_first():
    if not _dotenv_has_sentinel():
        import pytest

        pytest.skip("no .env with QUANT_TOKEN in this checkout")

    env = {k: v for k, v in os.environ.items() if k != SENTINEL}
    code = (
        "import os\n"
        "from service import settings\n"          # the import under test
        "assert os.environ.get(%r), 'dotenv not loaded before settings'\n"
        "print(settings.num('risk.initial_cash'))\n" % SENTINEL
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip(), "settings did not answer after the import"
