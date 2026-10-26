"""Tests for citeforge.fsscan: deterministic directory-scan helpers.

The ``iter_parsed_author_bibs`` section includes differential tests whose
oracles are verbatim replicas of the two inline scan loops the helper
replaced (``merge_utils.save_entry_to_file`` and the Phase 4 candidate-DOI
dedup in ``pipeline.article``), proving the shared scan+parse core visits the
same files, in the same order, with the same parse results and error
behavior.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

import pytest

from citeforge.bibtex_utils import parse_bibtex_to_dict
from citeforge.fsscan import iter_author_bibs, iter_output_dirs, iter_parsed_author_bibs

_BIB_A = "@article{keyA,\n  title = {Alpha Paper},\n  doi = {10.1000/a},\n}\n"
_BIB_C = "@article{keyC,\n  title = {Gamma Paper},\n  doi = {10.1000/c},\n}\n"
_BIB_Z = "@article{keyZ,\n  title = {Zeta Paper},\n  doi = {10.1000/z},\n}\n"


def test_iter_author_bibs_returns_sorted_bib_names_only(tmp_path: pathlib.Path) -> None:
    (tmp_path / "z.bib").write_text("", encoding="utf-8")
    (tmp_path / "a.bib").write_text("", encoding="utf-8")
    (tmp_path / "note.txt").write_text("", encoding="utf-8")

    assert iter_author_bibs(str(tmp_path)) == ["a.bib", "z.bib"]


def test_iter_output_dirs_returns_sorted_subdir_names_excluding_files(tmp_path: pathlib.Path) -> None:
    (tmp_path / "zeta").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "loose.bib").write_text("", encoding="utf-8")
    (tmp_path / "baseline.json").write_text("{}", encoding="utf-8")

    assert iter_output_dirs(str(tmp_path)) == ["alpha", "zeta"]


def _write_scan_fixture(tmp_path: pathlib.Path) -> None:
    """Author dir with parseable, unparseable, non-bib, and unreadable entries."""
    (tmp_path / "c.bib").write_text(_BIB_C, encoding="utf-8")
    (tmp_path / "a.bib").write_text(_BIB_A, encoding="utf-8")
    (tmp_path / "z.bib").write_text(_BIB_Z, encoding="utf-8")
    (tmp_path / "garbage.bib").write_text("not bibtex at all", encoding="utf-8")
    (tmp_path / "note.txt").write_text("ignored", encoding="utf-8")
    # A directory with a .bib name: open() raises IsADirectoryError (OSError)
    (tmp_path / "dir.bib").mkdir()


def test_iter_parsed_author_bibs_yields_sorted_parseable_entries(tmp_path: pathlib.Path) -> None:
    _write_scan_fixture(tmp_path)

    result = list(iter_parsed_author_bibs(str(tmp_path)))

    assert [fname for fname, _, _ in result] == ["a.bib", "c.bib", "z.bib"]
    assert [path for _, path, _ in result] == [os.path.join(str(tmp_path), f) for f, _, _ in result]
    assert [entry["fields"]["doi"] for _, _, entry in result] == ["10.1000/a", "10.1000/c", "10.1000/z"]


def test_iter_parsed_author_bibs_skip_basename(tmp_path: pathlib.Path) -> None:
    _write_scan_fixture(tmp_path)

    result = list(iter_parsed_author_bibs(str(tmp_path), skip_basename="c.bib"))

    assert [fname for fname, _, _ in result] == ["a.bib", "z.bib"]


def test_iter_parsed_author_bibs_skip_path_matches_by_absolute_identity(tmp_path: pathlib.Path) -> None:
    _write_scan_fixture(tmp_path)
    # Non-normalized spelling of the same file must still be skipped
    dotted = os.path.join(str(tmp_path), ".", "c.bib")

    result = list(iter_parsed_author_bibs(str(tmp_path), skip_path=dotted))

    assert [fname for fname, _, _ in result] == ["a.bib", "z.bib"]


def test_iter_parsed_author_bibs_read_error_invokes_callback_and_skips(tmp_path: pathlib.Path) -> None:
    _write_scan_fixture(tmp_path)
    seen: list[str] = []

    result = list(iter_parsed_author_bibs(str(tmp_path), on_read_error=seen.append))

    assert seen == ["dir.bib"]
    assert [fname for fname, _, _ in result] == ["a.bib", "c.bib", "z.bib"]


def test_iter_parsed_author_bibs_default_read_errors_do_not_swallow_decode_errors(tmp_path: pathlib.Path) -> None:
    (tmp_path / "bad.bib").write_bytes(b"@article{k,\n  title = {\xff\xfe},\n}\n")

    with pytest.raises(UnicodeDecodeError):
        list(iter_parsed_author_bibs(str(tmp_path)))

    result = list(iter_parsed_author_bibs(str(tmp_path), read_errors=(OSError, UnicodeDecodeError)))
    assert result == []


# ---------------------------------------------------------------------------
# Differential oracles: verbatim replicas of the inline loops the helper
# replaced. If these ever disagree with iter_parsed_author_bibs, the
# byte-identical-output invariant is at risk.
# ---------------------------------------------------------------------------


def _legacy_save_entry_scan(author_dir: str, prefer_basename: str | None) -> list[tuple[str, str, dict[str, Any]]]:
    """Replica of the pre-consolidation loop in merge_utils.save_entry_to_file."""
    visited = []
    for existing_filename in iter_author_bibs(author_dir):
        if existing_filename == prefer_basename:
            continue
        existing_path = os.path.join(author_dir, existing_filename)
        try:
            with open(existing_path, encoding="utf-8") as ef:
                existing_entry = parse_bibtex_to_dict(ef.read())
        except OSError:
            continue
        if existing_entry:
            visited.append((existing_filename, existing_path, existing_entry))
    return visited


def _legacy_phase4_scan(author_dir: str, path: str | None) -> list[tuple[str, str, dict[str, Any]]]:
    """Replica of the pre-consolidation Phase 4 candidate-DOI loop in pipeline.article."""
    visited = []
    for existing_bib in iter_author_bibs(author_dir):
        epath = os.path.join(author_dir, existing_bib)
        if path and os.path.abspath(epath) == os.path.abspath(path):
            continue
        try:
            with open(epath, encoding="utf-8") as ef:
                edict = parse_bibtex_to_dict(ef.read())
            if not edict:
                continue
        except (OSError, UnicodeDecodeError):
            continue
        visited.append((existing_bib, epath, edict))
    return visited


@pytest.mark.parametrize("prefer_basename", [None, "c.bib", "missing.bib"])
def test_iter_parsed_author_bibs_matches_legacy_save_entry_scan(
    tmp_path: pathlib.Path, prefer_basename: str | None
) -> None:
    _write_scan_fixture(tmp_path)

    legacy = _legacy_save_entry_scan(str(tmp_path), prefer_basename)
    shared = list(iter_parsed_author_bibs(str(tmp_path), skip_basename=prefer_basename))

    assert shared == legacy


@pytest.mark.parametrize("self_name", [None, "c.bib", "missing.bib"])
def test_iter_parsed_author_bibs_matches_legacy_phase4_scan(tmp_path: pathlib.Path, self_name: str | None) -> None:
    _write_scan_fixture(tmp_path)
    path = os.path.join(str(tmp_path), self_name) if self_name else None

    legacy = _legacy_phase4_scan(str(tmp_path), path)
    shared = list(
        iter_parsed_author_bibs(str(tmp_path), skip_path=path or None, read_errors=(OSError, UnicodeDecodeError))
    )

    assert shared == legacy
