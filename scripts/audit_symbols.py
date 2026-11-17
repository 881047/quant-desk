"""Who reads this symbol — nobody, or only the tests?

Coverage answers "was this line executed". It cannot answer "does anything call
this function": ``index.js`` once had 91% line coverage and 48% function
coverage at the same time, and this repository's own review found ten symbols
with zero readers *and* green tests.

``vulture`` was tried first and is not usable here: at 70% confidence it prints
nothing for this tree, and at 60% it floods the output with same-score noise
(``row_factory`` assignments, dataclass fields). What is wanted is a small,
readable list with a stated rule, not a score.

    python scripts/audit_symbols.py           # summaries + test-only list
    python scripts/audit_symbols.py --dead    # only the zero-reader ones
    python scripts/audit_symbols.py --json    # machine-readable

Two results are expected and must not be "fixed":

* ``scripts/external.py:parse_text`` —— 由 ``scripts/panel_ui/app.js`` 按**字符串名**
  调用（``call('parse_text', ...)``）。按名字分发的入口在任何 AST 扫描里都是死的。
* ``service/adapters/catalog.py:TRUST_TIERS`` —— 数据集的 T0–T3 唯一的文字定义，
  没有代码读它，删了字段值就没有解释处。

The rule, stated so it can be argued with:

* a **read** is a ``Name`` in load context, an attribute access with that name,
  or an import of that name (relative imports included);
* the definition itself is not a read;
* a symbol with production reads is alive, whatever else is true of it.

Two things this deliberately does not do:

* **resolve names across modules.** Two modules defining ``load`` share one
  entry. That direction only ever over-reports "alive", so a name that shows up
  here as dead really has no reader anywhere.
* **judge.** ``core.plan`` is read by nobody in production and is still correct:
  ``/core/*`` answers 409 by product decision, and the tests are the ones that
  exercise the retirement. That is what ``test-only`` means.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("service", "scripts")
TEST_DIR = "tests"


def _files(root: Path) -> tuple[list[Path], list[Path]]:
    production = [p for name in SOURCE_DIRS
                  for p in sorted((root / name).rglob("*.py"))
                  if "__pycache__" not in p.parts]
    tests = [p for p in sorted((root / TEST_DIR).rglob("*.py"))
             if "__pycache__" not in p.parts]
    return production, tests


def _definitions(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A decorated function is registered, not called by name:
            # ``@router.get("/x")`` and ``@app.exception_handler(E)`` both hand
            # the object to a framework that keeps its own reference. Counting
            # these as "no reader" once produced 30 false positives out of 47.
            if node.decorator_list:
                continue
            found.append((node.name, node.lineno))
        elif isinstance(node, ast.Assign):
            found.extend((t.id, node.lineno) for t in node.targets
                         if isinstance(t, ast.Name) and t.id.isupper())
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id.isupper():
                found.append((node.target.id, node.lineno))
    return found


def _reads(path: Path) -> Counter:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    counts: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            counts[node.id] += 1
        elif isinstance(node, ast.Attribute):
            counts[node.attr] += 1
        elif isinstance(node, ast.ImportFrom):
            counts.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            counts.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
    return counts


def scan(root: Path = ROOT) -> dict[str, list[str]]:
    production, tests = _files(root)
    prod_reads: Counter = Counter()
    for path in production:
        prod_reads.update(_reads(path))
    test_reads: Counter = Counter()
    for path in tests:
        test_reads.update(_reads(path))

    defs = [(path, name, line) for path in production
            for name, line in _definitions(path)]
    if len(defs) < 1_000:
        # 元断言：解析器坏掉时，结论会从"三千个定义"变成"几十个全死"或"一个都没有"。
        raise RuntimeError(f"only {len(defs)} definitions found; parser is broken")

    dead: list[str] = []
    test_only: list[str] = []
    for path, name, line in defs:
        where = f"{path.relative_to(root).as_posix()}:{line} {name}"
        if prod_reads[name] == 0:
            (test_only if test_reads[name] else dead).append(where)
    return {"dead": sorted(dead), "test_only": sorted(test_only)}


def main(argv: list[str]) -> int:
    report = scan()
    if "--json" in argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    wanted = ("dead",) if "--dead" in argv else ("dead", "test_only")
    print(f"definitions with no production reader: "
          f"{len(report['dead'])} dead, {len(report['test_only'])} test-only")
    for bucket in wanted:
        for line in report[bucket]:
            print(f"  {bucket:9} {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
