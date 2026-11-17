"""A config key can be read *and* still never reach behaviour.

``audit_config_keys`` runs the suite with a probe on ``settings._get`` and reports
keys nobody asked for. That catches "the yaml line is inert". It does not catch
this shape, which is what 2026-11-04 actually shipped:

    DEFAULT_PARTICIPATION = settings.num("impact.default_participation")

    def estimate(...):
        ...

Nothing reads ``DEFAULT_PARTICIPATION`` — the participation rate is computed as
``qty / adv_shares`` and there is no default entry point. The key *was* read, so
the probe was satisfied and the gate was green. Editing that yaml line changed
nothing, and no tool said so.

The static check in ``scripts/audit_config_keys.py`` answers the missing half:
every module-level constant assigned from ``settings.*(...)`` must be read at
least once outside ``tests/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from audit_config_keys import settings_constants_without_readers


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


CONSTANT = "from service import settings\n\nVALUE = settings.num('a.b')\n"


def test_a_constant_nobody_reads_is_dead(tmp_path):
    root = _tree(tmp_path, {"service/impact.py": CONSTANT})
    dead, test_only = settings_constants_without_readers(root, min_sites=1)
    assert dead == ["service/impact.py:3 VALUE"]
    assert test_only == []


def test_a_constant_read_elsewhere_is_alive(tmp_path):
    root = _tree(tmp_path, {
        "service/impact.py": CONSTANT,
        "service/sizing.py": "from service.impact import VALUE\nprint(VALUE)\n",
    })
    assert settings_constants_without_readers(root, min_sites=1) == ([], [])


def test_an_attribute_read_counts_too(tmp_path):
    """``impact.VALUE`` is a read even though the name is not imported."""
    root = _tree(tmp_path, {
        "service/impact.py": CONSTANT,
        "service/api.py": "from service import impact\nprint(impact.VALUE)\n",
    })
    assert settings_constants_without_readers(root, min_sites=1) == ([], [])


def test_read_only_by_tests_is_reported_separately(tmp_path):
    """A test asserting on the constant is not a consumer of the value."""
    root = _tree(tmp_path, {
        "service/impact.py": CONSTANT,
        "tests/test_impact.py": "from service.impact import VALUE\nassert VALUE\n",
    })
    dead, test_only = settings_constants_without_readers(root, min_sites=1)
    assert dead == []
    assert test_only == ["service/impact.py:3 VALUE"]


def test_a_broken_parser_cannot_report_success(tmp_path):
    """The meta-assertion: finding nothing must not look like "all clean"."""
    root = _tree(tmp_path, {"service/empty.py": ""})
    with pytest.raises(RuntimeError, match="parser is broken"):
        settings_constants_without_readers(root)


def test_the_real_tree_has_no_dead_settings_constants():
    dead, test_only = settings_constants_without_readers(ROOT)
    assert dead == [], f"these config values reach a constant nothing reads: {dead}"
    assert test_only == [], f"only tests read these: {test_only}"
