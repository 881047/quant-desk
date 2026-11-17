"""`scripts/` may import `service/`; `service/` may never import `scripts/`.

The direction matters and it was broken on 2026-11-05: the API's manual-refresh
path did ``from scripts.snapshot_intraday import snapshot``, which made the one
directory nobody deploys a runtime dependency of the service. Two consequences,
both silent:

* the capture could only be tested by loading a *file path*, not a module, so
  the API path and the scheduled path could drift without either side noticing;
* moving, renaming or splitting ``scripts/`` was no longer a refactor — it was a
  production change.

The capture now lives in ``service/trading/snapshot.py`` and the script is a
scheduler. This file keeps it that way.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "service"
SCRIPTS = ROOT / "scripts"


def _imports(path: Path, package: list[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = ".".join([*base, node.module]) if node.module else ".".join(base)
            else:
                module = node.module or ""
            if module:
                found.add(module)
                found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def _package_of(path: Path) -> list[str]:
    return list(path.relative_to(ROOT).parent.parts)


def test_the_service_never_imports_a_script():
    offenders: dict[str, list[str]] = {}
    for path in sorted(SERVICE.rglob("*.py")):
        hits = sorted(m for m in _imports(path, _package_of(path))
                      if m == "scripts" or m.startswith("scripts."))
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert not offenders, (
        "the service must not depend on scripts/: move the logic into service/ "
        f"and let the script call it — {offenders}"
    )


def test_the_scanner_sees_the_direction_that_is_allowed(tmp_path: Path):
    """Scripts import services all the time; a clean result must not mean "unparsed".

    Two checks: a synthetic violation is detected, and a real script that imports
    a real service module is parsed into the expected edge.
    """
    sample = tmp_path / "defect.py"
    sample.write_text("from scripts.snapshot_intraday import snapshot\n",
                      encoding="utf-8")
    assert "scripts.snapshot_intraday" in _imports(sample, [])

    seen = _imports(SCRIPTS / "daily_job.py", ["scripts"])
    assert "service.trading.snapshot" in seen, "the fixed call site must be visible"


def test_the_capture_is_reachable_without_a_script_path():
    """The API path and the scheduled path must be the same function."""
    from service.trading.snapshot import snapshot

    assert callable(snapshot)
    assert snapshot.__module__ == "service.trading.snapshot"
