"""Announcement text: one home for the cache path, the read and the write.

The text of a filing lives beside the data rather than inside it — an annual
report is 200+ pages of prose, and rewriting a 230k-row parquet to append it
would cost far more than the text is worth. But "beside" needs a single owner:
`market.announcement_text()` used to compose the path itself while
`scripts/fetch_anns_text.py` composed it again, so the read and the write could
disagree about the directory without either one failing. This module is that
owner (the same lesson `board_of` taught in P68: one fact, one implementation).

Nothing here talks to the desk. `read` is a disk read; `fetch` is the only
function that touches the network, and it is called deliberately — by the
backfill script in a loop, or by the filing reader for one document the reader
asked about.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

#: cninfo ids are decimal and bounded. Anything else is a path-traversal
#: attempt or a caller bug; neither may reach the filesystem.
ANN_ID = re.compile(r"[0-9]{1,32}")

#: An annual report with scanned exhibits is 40 MB; twelve pages of text from
#: it is not worth that download inside a request.
MAX_BYTES = 8_000_000
MAX_PAGES = 12


def cache_dir(root: Path | str) -> Path:
    """``<root>/cache/anns_text``, resolved once."""
    return (Path(root) / "cache" / "anns_text").resolve()


def cache_path(root: Path | str, announcement_id: str) -> Path | None:
    """Path for one filing, or None when the id cannot name a file.

    Resolved and re-checked against the directory so that an id like ``..`` or
    ``../../etc/passwd`` — which the regex already rejects, but a future caller
    might relax — still cannot escape.
    """
    if not isinstance(announcement_id, str) or not ANN_ID.fullmatch(announcement_id):
        return None
    folder = cache_dir(root)
    path = (folder / f"{announcement_id}.json").resolve()
    try:
        path.relative_to(folder)
    except ValueError:
        return None
    return path


def read(root: Path | str, announcement_id: str) -> dict | None:
    """Cached payload for one filing, or None when it was never downloaded."""
    path = cache_path(root, announcement_id)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write(root: Path | str, payload: dict) -> Path:
    """Write one payload atomically, so a killed process leaves no half file."""
    path = cache_path(root, str(payload.get("announcement_id") or ""))
    if path is None:
        raise ValueError(f"invalid announcement_id: {payload.get('announcement_id')!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def fetch_one(client, row: dict, root: Path | str, *, force: bool = False,
              max_pages: int = MAX_PAGES) -> dict:
    """Download and cache one filing. Returns ``{id, status, chars}``.

    Statuses are distinct on purpose: ``no_text_layer`` means the PDF is a scan
    and no amount of retrying will help, while ``download_failed`` might. The
    reader who asks again deserves to be told which one it was.
    """
    from .cninfo_adapter import pdf_to_text

    ann_id = str(row.get("announcement_id") or "")
    path = cache_path(root, ann_id)
    if path is None:
        return {"id": ann_id, "status": "bad_id", "chars": None}
    if path.exists() and not force:
        return {"id": ann_id, "status": "cached", "chars": None}

    url = str(row.get("pdf_url") or "")
    if not url:
        return {"id": ann_id, "status": "no_url", "chars": None}
    size_kb = row.get("size_kb")
    try:
        oversized = bool(size_kb) and float(size_kb) * 1024 > MAX_BYTES
    except (TypeError, ValueError):
        oversized = False
    if oversized:
        return {"id": ann_id, "status": "too_large", "chars": None}

    try:
        blob = client.download_pdf(url)
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        return {"id": ann_id, "status": f"download_failed:{type(exc).__name__}",
                "chars": None}
    try:
        text, pages_read, pages_empty, pages_total = pdf_to_text(
            blob, max_pages=max_pages)
    except Exception as exc:                       # noqa: BLE001
        return {"id": ann_id, "status": f"parse_failed:{type(exc).__name__}",
                "chars": None}
    if not text:
        # A scanned filing has no text layer. Recording it prevents the next
        # run from downloading the same 5 MB to learn the same thing again.
        return {"id": ann_id, "status": "no_text_layer", "chars": 0}

    write(root, {
        "announcement_id": ann_id,
        "ts_code": str(row.get("ts_code") or ""),
        "title": str(row.get("title") or ""),
        "category": str(row.get("category") or ""),
        "pub_date": str(row.get("pub_date") or ""),
        "pdf_url": url,
        "pages_read": pages_read,
        "pages_empty": pages_empty,
        "pages_total": pages_total,
        "chars": len(text),
        "bytes": len(blob),
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": text,
    })
    return {"id": ann_id, "status": "ok", "chars": len(text)}
