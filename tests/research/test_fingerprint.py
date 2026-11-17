"""A fingerprint that changes for the wrong reason is worse than none.

The point is to answer "is this the same panel as last time", so the tests that
matter are the negative ones: a re-save of a decade-old slice must **not** move
it, because that noise is what teaches a reader to ignore the column.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from service.research import fingerprint


class _Registry:
    def __init__(self, records):
        self._records = records

    def list_manifest(self, dataset=None):
        return [r for r in self._records if dataset in (None, r.dataset)]


def _rec(dataset, partition, checksum, start, end):
    return SimpleNamespace(dataset=dataset, partition=partition,
                           checksum=checksum, cover_start=start, cover_end=end)


def _store(records):
    return SimpleNamespace(registry=_Registry(records))


BASE = [
    _rec("daily", "2023", "aaa", "20230101", "20231231"),
    _rec("daily", "2026", "bbb", "20260101", "20261029"),
    _rec("limit_pool_t0", "2023", "ccc", "20230101", "20231231"),
    # Outside every window used below.
    _rec("daily", "2015", "ddd", "20150101", "20151231"),
]


def test_the_same_panel_gives_the_same_identity():
    a = fingerprint.of(_store(BASE), "20230101", "20261023")
    b = fingerprint.of(_store(list(BASE)), "20230101", "20261023")

    assert a == b and len(a) == 16


def test_a_change_inside_the_window_moves_it():
    moved = [r if r.partition != "2026" and r.dataset != "daily"
             else (r if r.partition != "2026"
                   else _rec("daily", "2026", "ZZZ", "20260101", "20261029"))
             for r in BASE]
    assert fingerprint.of(_store(moved), "20230101", "20261023") != \
        fingerprint.of(_store(BASE), "20230101", "20261023")


def test_a_resave_of_an_out_of_window_slice_does_not_move_it():
    """2015 was rewritten on 2026-10-26; a 2023+ study must not care."""
    touched = [r if r.partition != "2015"
               else _rec("daily", "2015", "ZZZ", "20150101", "20151231")
               for r in BASE]

    assert fingerprint.of(_store(touched), "20230101", "20261023") == \
        fingerprint.of(_store(BASE), "20230101", "20261023")


def test_a_store_without_a_manifest_is_not_an_error():
    """Provenance is diagnostic: it must never take a verdict down with it."""
    assert fingerprint.of(SimpleNamespace(), "20230101", "20261023") == ""


def test_explain_names_the_slices_it_hashed():
    got = fingerprint.explain(_store(BASE), "20230101", "20261023")

    assert got["fingerprint"] == fingerprint.of(_store(BASE), "20230101", "20261023")
    assert sorted(got["datasets"]["daily"]) == ["2023", "2026"]   # not 2015
    assert got["datasets"]["limit_pool_t0"] == {"2023": "ccc"}


def test_runtime_identity_hashes_file_contents_and_effective_config(tmp_path):
    source = tmp_path / "service"
    source.mkdir()
    module = source / "engine.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    config = {"costs.stamp_duty_sell": 0.0005, "research.grid": [1, 2]}
    first = fingerprint.implementation(root=tmp_path, config_values=config)
    stat = module.stat()

    # Same size and same mtime: a shape fingerprint cannot see this edit, while
    # a reproducibility fingerprint must. Config identity remains unchanged.
    module.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(module, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = fingerprint.implementation(root=tmp_path, config_values=config)

    assert first["code_fingerprint"] != second["code_fingerprint"]
    assert first["config_fingerprint"] == second["config_fingerprint"]
    third = fingerprint.implementation(
        root=tmp_path, config_values={**config, "research.grid": [1, 2, 3]})
    assert third["code_fingerprint"] == second["code_fingerprint"]
    assert third["config_fingerprint"] != second["config_fingerprint"]
    assert all(len(third[key]) == 16 for key in ("code_fingerprint", "config_fingerprint"))
