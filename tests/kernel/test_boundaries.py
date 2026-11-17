"""What the kernel is allowed to depend on, enforced rather than documented.

`service/kernel/` is a library of financial semantics, not a service. Three
properties keep it that way, and each one has already been broken once by a
well-meaning change:

1. **No I/O and no dataframes.** The matching rule must be importable from a bare
   script, which is why `microstructure.py` carries no pandas even though the
   research layer evaluates the same rule vectorised.
2. **No wall clock in business code.** A rule that reads `datetime.now()` cannot
   be replayed, and a replay that disagrees with the original is not an audit.
   `clock.py` is the single door: `LiveClock` may read the clock, nothing else.
3. **The reference engine stays a reference.** `engine.py` + `risk.py` exist for
   the golden scenarios. Production composes `Ledger` + `BarExecutionModel`
   inside `PaperBook`. When a production module imports the engine by accident,
   the desk silently gets a second, differently-configured risk path — which is
   exactly the state the 2026-10-28 review found.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KERNEL = ROOT / "service" / "kernel"
SERVICE = ROOT / "service"

FORBIDDEN_IMPORTS = {
    "pandas", "numpy", "requests", "httpx", "urllib", "sqlite3",
    "fastapi", "starlette", "yaml",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("path", sorted(KERNEL.glob("*.py")), ids=lambda p: p.name)
def test_the_kernel_imports_no_io_layer(path: Path):
    offending = _imports(path) & FORBIDDEN_IMPORTS
    assert not offending, f"{path.name} pulled in {sorted(offending)}"


def test_the_kernel_imports_without_pandas():
    """A subprocess, because this process may already have pandas loaded."""
    code = (
        "import sys; import service.kernel.execution, service.kernel.ledger, "
        "service.kernel.oms, service.kernel.risk, service.kernel.calendar; "
        "print(','.join(sorted(m for m in ('pandas','numpy') if m in sys.modules)))"
    )
    out = subprocess.check_output([sys.executable, "-c", code], cwd=ROOT, text=True)
    assert out.strip() == "", f"kernel dragged in {out.strip()}"


@pytest.mark.parametrize("path", sorted(KERNEL.glob("*.py")), ids=lambda p: p.name)
def test_only_the_clock_reads_the_wall_clock(path: Path):
    if path.name == "clock.py":
        pytest.skip("LiveClock is the one sanctioned reader")
    source = path.read_text(encoding="utf-8")
    for forbidden in ("datetime.now(", "datetime.utcnow(", "time.time(", "date.today("):
        assert forbidden not in source, f"{path.name} reads the wall clock: {forbidden}"


def test_no_production_module_imports_the_reference_engine():
    offenders = []
    for path in SERVICE.rglob("*.py"):
        if KERNEL in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if "kernel.engine" in text or "kernel.risk" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        "production must go through PaperBook + Ledger, not the reference engine: "
        f"{offenders}"
    )


def test_the_unused_facades_stay_deleted():
    """EventStore and SimBroker had zero callers but read like live layers."""
    for name in ("events.py", "sim_broker.py"):
        assert not (KERNEL / name).exists(), f"{name} came back without a caller"
