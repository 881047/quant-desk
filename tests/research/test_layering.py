"""The research layer must sit *below* trading, and that has to be enforced.

Until 2026-11-05 the dependency ran the other way in five places, every one of
them hidden inside a function body:

    discover.py      from service.trading.core import CoreBook   # inside _core_regime
    information.py   from service.trading import get_desk        # inside probe_quotes
    information.py   from service.trading.realtime import ...    # inside probe_quotes
    information.py   from service.trading.tape import ...        # inside _reconcile
    discover.py      from service.trading.core import RISK_ETFS  # at module scope

The function-local ones worked only because the import was deferred. Hoisting
any of them during a refactor turns a running service into an ImportError at
startup, and `trading.arena` already imports `research.stats` at module level,
so the cycle is real — it is simply timed to miss. The module-scope one had no
such protection; it worked because `service/research/__init__.py` happens to
import nothing.

Two checks, because they fail differently:

* the static one catches an import however deep it is nested;
* the dynamic one catches the thing static analysis cannot see — a module that
  gets pulled in two hops away, and the load-order damage that comes with it.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT / "service" / "research"

FORBIDDEN = "service.trading"


def _imports(path: Path, package: list[str] | None = None) -> set[str]:
    """Every module this file imports, with relative levels resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if package is None:
        package = ["service", "research"] if path.parent == RESEARCH else []
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
                # `from service.trading import get_desk` names a module, not a symbol.
                found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def test_no_research_module_imports_trading():
    offenders = {
        path.name: sorted(m for m in _imports(path) if m == FORBIDDEN
                          or m.startswith(f"{FORBIDDEN}."))
        for path in sorted(RESEARCH.glob("*.py"))
    }
    offenders = {name: hits for name, hits in offenders.items() if hits}
    assert not offenders, (
        "research is layer 2 and trading is layer 3: pass the object in instead "
        f"of importing it — {offenders}"
    )


def test_the_scanner_actually_sees_imports(tmp_path: Path):
    """A gate that cannot fail is worse than no gate.

    The first version of this file walked only ``ast.Import``, so every
    ``from service.trading import ...`` looked like a clean file. Proving the
    scanner sees a known edge is the only way to tell "no offenders" from
    "nothing was parsed".
    """
    sample = tmp_path / "defect.py"
    sample.write_text(
        "from service.trading import get_desk\n"
        "from service.trading.core import CoreBook\n"
        "from ..trading.tape import promotion_series\n",
        encoding="utf-8",
    )
    seen = _imports(sample, package=["service", "research"])
    assert "service.trading" in seen
    assert "service.trading.core" in seen
    assert "service.trading.tape" in seen, "relative imports must be resolved"

    known = _imports(RESEARCH / "information.py")
    assert "service.adapters.catalog" in known, "the real tree must parse too"


def test_importing_research_does_not_load_trading():
    """The failure mode is an import cycle, so check it where cycles happen.

    A subprocess, because this test process has the whole app loaded already.
    """
    code = (
        "import sys, service.research.discover, service.research.information, "
        "service.research.pools; "
        "print(','.join(sorted(m for m in sys.modules if m.startswith('service.trading'))))"
    )
    out = subprocess.check_output([sys.executable, "-c", code], cwd=ROOT, text=True)
    assert out.strip() == "", f"research dragged in the trading layer: {out.strip()}"
