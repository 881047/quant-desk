"""quant-desk service package.

``.env`` is loaded **here**, before any submodule, because ``service.settings``
reads every parameter at import time (``settings.py`` ends with ``load()``).
It used to be loaded from ``service.api.security``, which is imported *after*
``service.settings`` by ``service.api.app`` — so a ``QUANT__RISK__...`` override
written in ``.env`` (the file the project tells operators to edit) was silently
ignored, while the very same variable exported in the shell worked. Loading it
in the package ``__init__`` makes "read .env" a precondition of "read config"
instead of a race between import order.

``load_dotenv`` does not override variables already present in the environment,
so a shell export still wins — the documented precedence is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

#: Repository root (``service/..``). Resolved from this file, not from the cwd:
#: the service is started by a scheduler with a different working directory.
ROOT = Path(__file__).resolve().parents[1]

load_dotenv(ROOT / ".env")
