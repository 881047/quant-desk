"""Which declared parameters does the code never ask for?

``configs/params.yaml`` is the only place values live; ``settings.SPECS`` is the
only place they are declared. A key that is declared and never read is a trap:
someone edits the yaml, nothing happens, and nothing says so.

Grep cannot answer this. Most readers build the key dynamically —
``settings.num(f"risk.{name}")`` — so "no string in the source" and "never read"
are different questions. This script answers the second one by recording every
``settings._get`` call made while the test suite runs, then diffing against SPECS.

    python scripts/audit_config_keys.py              # full suite (~5 min)
    python scripts/audit_config_keys.py tests/kernel # a subset, for a quick look
    python scripts/audit_config_keys.py --strict     # non-zero exit if anything is unread

By default the exit code is 0: "unread" is a question to look into, not a build
failure. A key read only on a path the suite does not cover is *not* a defect but
is worth knowing — it means nobody has verified the branch that consumes it.

The exception is the probe itself not working. ``pytest_sessionfinish`` runs even
when tests fail, so a red suite still writes ``seen.json`` — and the diff against
SPECS then looks exactly like a green one. Until 2026-11-04 that path ended in
``exit 0`` with ``never read 0`` printed, which is the worst kind of green: the
number is real, but it is a measurement of a suite that did not pass.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Directories whose `.py` files can read a config constant.
_SOURCE_DIRS = ("service", "scripts", "tests")

PLUGIN = '''
import json, os, pathlib

_SEEN = set()

def pytest_configure(config):
    from service import settings
    original = settings._get

    def recording(key):
        _SEEN.add(key)
        return original(key)

    settings._get = recording

def pytest_sessionfinish(session, exitstatus):
    pathlib.Path(os.environ["KEYPROBE_OUT"]).write_text(
        json.dumps(sorted(_SEEN)), encoding="utf-8")
'''


def _python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for name in _SOURCE_DIRS:
        base = root / name
        if base.exists():
            files.extend(p for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts)
    return files


def _settings_constants(path: Path) -> list[tuple[str, int]]:
    """Module-level ``NAME = settings.<fn>(...)`` in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id == "settings"):
            continue
        found.extend((target.id, node.lineno) for target in targets
                     if isinstance(target, ast.Name))
    return found


def _read_counts(root: Path = ROOT) -> tuple[Counter, Counter]:
    """How often each bare name is *read*, split into production and tests.

    Attribute access counts too (``impact.DEFAULT_PARTICIPATION``) and only by
    attribute name: resolving which module an object came from needs a type
    checker, and a false positive here would hide a real dead constant. Counting
    an attribute read that belongs to some other object is the safe direction.
    """
    production: Counter = Counter()
    tests: Counter = Counter()
    for path in _python_files(root):
        is_test = "tests" in path.relative_to(root).parts
        bucket = tests if is_test else production
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                bucket[node.id] += 1
            elif isinstance(node, ast.Attribute):
                bucket[node.attr] += 1
    return production, tests


def settings_constants_without_readers(
        root: Path = ROOT, *, min_sites: int = 20) -> tuple[list[str], list[str]]:
    """Config values that reach a module constant which nothing ever reads.

    ``audit_config_keys`` answers "was this key asked for". It cannot answer
    "did the answer go anywhere": ``impact.default_participation`` was read into
    ``DEFAULT_PARTICIPATION`` on 2026-11-04, the constant had no reader, and the
    gate stayed green because the read *did* happen. See ``_measure`` below for
    why "read" and "reaches behaviour" are different questions.

    Returns ``(dead, test_only)``.
    """
    production, tests = _read_counts(root)
    sites = [(path, name, line) for path in _python_files(root)
             for name, line in _settings_constants(path)]
    if len(sites) < min_sites:
        # 元断言：解析器一旦失效，这里会从"一百多个常量"变成"两三个"，
        # 而结论会静悄悄地变成"没有死常量"。
        raise RuntimeError(f"only {len(sites)} settings constants found; parser is broken")
    dead: list[str] = []
    test_only: list[str] = []
    for path, name, line in sites:
        where = f"{path.relative_to(root).as_posix()}:{line} {name}"
        if production[name] == 0:
            (test_only if tests[name] else dead).append(where)
    return dead, test_only


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    static_only = "--static-only" in argv
    targets = [a for a in argv if not a.startswith("--")] or ["tests"]

    # The static half needs no test run: it is a question about the source, and
    # it answers the one thing the probe below cannot — whether the value that
    # was read landed anywhere.
    dead, test_only = settings_constants_without_readers()
    print(f"settings constants: {len(dead)} with no reader, "
          f"{len(test_only)} read only by tests")
    for line in dead:
        print(f"  DEAD       {line}")
    for line in test_only:
        print(f"  TEST-ONLY  {line}")
    static_rc = 1 if (strict and dead) else 0
    if static_only:
        return static_rc

    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp) / "probe"
        plugin_dir.mkdir()
        (plugin_dir / "keyprobe.py").write_text(PLUGIN, encoding="utf-8")
        out = Path(tmp) / "seen.json"

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT), str(plugin_dir), env.get("PYTHONPATH", "")]
        ).strip(os.pathsep)
        env["KEYPROBE_OUT"] = str(out)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "keyprobe", "--tb=no",
             *targets],
            cwd=ROOT, env=env, check=False,
        )
        # A failing test must not be reported as "keys read": the probe only
        # measures coverage of the config surface, not correctness. `pytest`
        # runs ``sessionfinish`` even when tests fail, so the file exists in
        # both cases — the exit code is what separates them.
        if not out.exists():
            print("probe produced no output; the suite did not finish", file=sys.stderr)
            return 2
        seen = set(json.loads(out.read_text(encoding="utf-8")))
        red = completed.returncode

    sys.path.insert(0, str(ROOT))
    from service import settings

    declared = set(settings.SPECS)
    unread = sorted(declared - seen)
    if red != 0:
        # 不给半份结论：这里列出来的 "never read" 是"跑到的那部分测试没读到"，
        # 不是"代码没读"。打印 171 行清单只会让人以为那是真的。
        print(f"probe incomplete: pytest exited {red}；"
              f"{len(seen)} 个键的读数来自没跑完的套件，不作结论", file=sys.stderr)
        return 2
    print(f"declared {len(declared)}  read {len(seen)}  never read {len(unread)}")
    for key in unread:
        print(f"  {key:44} {settings.SPECS[key].doc}")
    return max(static_rc, 1 if (strict and unread) else 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
