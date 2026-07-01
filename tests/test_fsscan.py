"""Tests for src.fsscan: deterministic directory-scan helpers."""

from __future__ import annotations

import pathlib

from src.fsscan import iter_author_bibs, iter_output_dirs


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
