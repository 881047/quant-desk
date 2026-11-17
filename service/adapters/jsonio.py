"""Atomic text/JSON writes for files that another process may be reading.

Every writer in this repository used ``path.write_text(...)``, which truncates
first and writes second. The readers are:

* ``service/api/data.py`` — ``/data/status``, ``/data/brief``, ``/data/sentiment``;
* ``service/api/dashboard.py`` — the desktop window and the evening snapshot;
* ``scripts/daily_job.py`` itself, on the next day's run.

So a read that lands between the truncate and the write parses an empty or
half-written file. Before this module existed that surfaced as a 500 from the API
(``JSONDecodeError`` straight out of the handler), which the breaker then counted
against the route.

``os.replace`` is atomic within a directory on both POSIX and Windows, so a
reader either sees the old file or the new one. The temp name is hidden and
suffixed so a crashed writer leaves something obviously disposable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_text_atomic(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    """Replace ``path`` with ``text`` without ever showing a partial file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        with tmp.open("w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def write_json_atomic(path: str | Path, payload: Any, *, indent: int = 1) -> Path:
    """``write_text_atomic`` for a JSON-serialisable payload."""
    return write_text_atomic(
        path, json.dumps(payload, ensure_ascii=False, indent=indent, default=str))
