"""On-disk survivor-decision contracts for ``merge_utils.save_entry_to_file``.

These tests drive the real save surface (``merge_utils.py`` ~966-1248): they
write actual ``.bib`` files with :func:`tests.factories.write_bib`, then call
``save_entry_to_file`` and assert the *real* on-disk outcome (file count, which
file survived, its bytes, the ``was_written`` flag). No mock stands in for the
function under test and the decision logic is never re-implemented here; every
golden verdict was captured by running the live function.

The surface under test had zero coverage before this file. The behaviours
pinned below are:

* preprint/published survivor selection (published always wins, work stays once);
* DOI precedence (a DOI-carrying record beats its DOI-less twin either way);
* orphan safety (same title + distinct DOIs => two files, never collapsed);
* DOI url-vs-bare normalization dedup via ``_norm_doi``;
* the ``OSError`` scan branch (an unreadable ``.bib`` never aborts the dedup);
* the composite-dedup threshold (a single-signal flip across
  ``SIM_DEDUP_COMPOSITE_THRESHOLD`` toggles merge vs no-merge).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from citeforge.config import SIM_DEDUP_COMPOSITE_THRESHOLD
from citeforge.merge_utils import save_entry_to_file
from citeforge.text_utils import compute_dedup_score, format_author_dirname
from tests import factories

AUTHOR_ID = "TestScholar01"

# Golden DOIs used across the boundary/precedence fixtures. Preprint vs
# published classification was confirmed against the live ``_is_preprint_doi``.
PREPRINT_DOI = "10.48550/arxiv.2401.00001"
PUBLISHED_DOI = "10.1145/3580305"
BOUNDARY_TITLE = "Neural Operators for Turbulent Flow Prediction"


def _author_dir(out_dir: Path, author_id: str = AUTHOR_ID) -> Path:
    """Return the author subdirectory ``save_entry_to_file`` writes into."""
    return out_dir / format_author_dirname(None, author_id)


def _bib_files(author_dir: Path) -> list[str]:
    """Sorted ``.bib`` *file* names in *author_dir* (directories excluded)."""
    if not author_dir.exists():
        return []
    return sorted(p.name for p in author_dir.iterdir() if p.is_file() and p.suffix == ".bib")


def _read(author_dir: Path, name: str) -> str:
    """Read a single ``.bib`` file's text."""
    return (author_dir / name).read_text(encoding="utf-8")


def _dois_on_disk(author_dir: Path) -> set[str]:
    """Collect the lowercased DOI substrings present across all ``.bib`` files."""
    found: set[str] = set()
    for name in _bib_files(author_dir):
        text = _read(author_dir, name).lower()
        for doi in (PREPRINT_DOI, PUBLISHED_DOI):
            if doi in text:
                found.add(doi)
    return found


# --- SURVIVOR: preprint vs published of the same work ----------------------


def test_published_on_disk_kept_against_incoming_preprint(tmp_path: Path) -> None:
    """Published record already on disk survives an incoming preprint twin.

    ``was_written`` is False (SKIP_WRITE), exactly one ``.bib`` remains, and it
    is the published file untouched.
    """
    author_dir = _author_dir(tmp_path)
    preprint, published_enricher = factories.arxiv_published_twin()
    published = published_enricher[1]
    factories.write_bib(author_dir, published, "published.bib")

    path, was_written = save_entry_to_file(str(tmp_path), AUTHOR_ID, preprint)

    assert was_written is False
    assert _bib_files(author_dir) == ["published.bib"]
    assert Path(path).name == "published.bib"
    surviving = _read(author_dir, "published.bib").lower()
    assert "10.1109/cvpr.2016.90" in surviving
    assert "10.48550/arxiv.1512.03385" not in surviving


def test_preprint_on_disk_replaced_by_incoming_published(tmp_path: Path) -> None:
    """A preprint on disk is removed when its published twin arrives.

    Exactly one ``.bib`` remains, carrying the published DOI (not the preprint
    arXiv DOI), and ``was_written`` is True.
    """
    author_dir = _author_dir(tmp_path)
    preprint, published_enricher = factories.arxiv_published_twin()
    published = published_enricher[1]
    factories.write_bib(author_dir, preprint, "preprint.bib")

    path, was_written = save_entry_to_file(str(tmp_path), AUTHOR_ID, published)

    assert was_written is True
    remaining = _bib_files(author_dir)
    assert len(remaining) == 1
    surviving = _read(author_dir, remaining[0]).lower()
    assert "10.1109/cvpr.2016.90" in surviving
    assert "10.48550/arxiv.1512.03385" not in surviving
    assert Path(path).name == remaining[0]


# --- DOI precedence: a DOI-carrying record beats its DOI-less twin ----------


def test_existing_doi_beats_incoming_doiless_same_work(tmp_path: Path) -> None:
    """Existing record with a DOI is kept over a DOI-less incoming twin."""
    author_dir = _author_dir(tmp_path)
    with_doi = factories.article(title="Ocean Graph Networks", author="Spadon, Gabriel", doi=PUBLISHED_DOI, key="A")
    doiless = factories.article(title="Ocean Graph Networks", author="Spadon, Gabriel", key="B")
    factories.write_bib(author_dir, with_doi, "withdoi.bib")

    path, was_written = save_entry_to_file(str(tmp_path), AUTHOR_ID, doiless)

    assert was_written is False
    assert _bib_files(author_dir) == ["withdoi.bib"]
    assert Path(path).name == "withdoi.bib"
    assert PUBLISHED_DOI in _read(author_dir, "withdoi.bib").lower()


def test_incoming_doi_replaces_existing_doiless_same_work(tmp_path: Path) -> None:
    """Incoming record with a DOI replaces a DOI-less record of the same work."""
    author_dir = _author_dir(tmp_path)
    with_doi = factories.article(title="Ocean Graph Networks", author="Spadon, Gabriel", doi=PUBLISHED_DOI, key="A")
    doiless = factories.article(title="Ocean Graph Networks", author="Spadon, Gabriel", key="B")
    factories.write_bib(author_dir, doiless, "nodoi.bib")

    path, was_written = save_entry_to_file(str(tmp_path), AUTHOR_ID, with_doi)

    assert was_written is True
    remaining = _bib_files(author_dir)
    assert len(remaining) == 1
    assert PUBLISHED_DOI in _read(author_dir, remaining[0]).lower()
    assert Path(path).name == remaining[0]


# --- ORPHAN SAFETY: distinct works must never collapse ----------------------


def test_distinct_works_same_title_kept_as_two_files(tmp_path: Path) -> None:
    """Same title under two authors with distinct DOIs stays as two files."""
    author_dir = _author_dir(tmp_path)
    first, second = factories.duplicate_titles_two_authors()
    factories.write_bib(author_dir, first, "first.bib")

    _path, was_written = save_entry_to_file(str(tmp_path), AUTHOR_ID, second)

    assert was_written is True
    remaining = _bib_files(author_dir)
    assert len(remaining) == 2
    all_text = "".join(_read(author_dir, name).lower() for name in remaining)
    assert "10.1145/3580305" in all_text
    assert "10.1038/s41586-024-00001" in all_text


# --- DOI url-vs-bare normalization ------------------------------------------


def test_same_doi_url_and_bare_dedup_to_one_file(tmp_path: Path) -> None:
    """Two different-title entries sharing one DOI (url form vs bare) => one file.

    Dedup runs through ``_norm_doi``, which strips the ``https://doi.org/``
    wrapper, so the DOIs compare equal despite the different titles/keys.
    """
    author_dir = _author_dir(tmp_path)
    as_url = factories.article(title="Title One", author="Alpha, A", doi="https://doi.org/10.1145/3580305", key="K1")
    as_bare = factories.article(title="Completely Different Title", author="Beta, B", doi="10.1145/3580305", key="K2")
    factories.write_bib(author_dir, as_url, "one.bib")

    path, _was_written = save_entry_to_file(str(tmp_path), AUTHOR_ID, as_bare)

    remaining = _bib_files(author_dir)
    assert len(remaining) == 1
    assert Path(path).name in remaining
    assert PUBLISHED_DOI in _read(author_dir, remaining[0]).lower()


# --- OSError scan branch -----------------------------------------------------


def test_unreadable_bib_does_not_abort_dedup(tmp_path: Path) -> None:
    """An unreadable ``.bib`` on disk is skipped; dedup still hits the readable one.

    A directory named ``*.bib`` is enumerated by ``iter_author_bibs`` but raises
    ``IsADirectoryError`` (an ``OSError``) on ``open()``. The per-file ``except
    OSError`` must swallow it so the readable true-duplicate is still matched.
    """
    author_dir = _author_dir(tmp_path)
    author_dir.mkdir(parents=True, exist_ok=True)
    # Sorts before the readable file, so it is scanned first and must not abort.
    (author_dir / "aaa_unreadable.bib").mkdir()
    readable = factories.article(title="Deep Sea Mapping", author="Nara, A", doi=PUBLISHED_DOI, key="R")
    factories.write_bib(author_dir, readable, "zzz_readable.bib")
    incoming = factories.article(title="Deep Sea Mapping", author="Nara, A", doi=PUBLISHED_DOI, key="R")

    path, _was_written = save_entry_to_file(str(tmp_path), AUTHOR_ID, incoming)

    # Deduped against the readable file (no third file created), no exception.
    assert Path(path).name == "zzz_readable.bib"
    assert _bib_files(author_dir) == ["zzz_readable.bib"]
    assert (author_dir / "aaa_unreadable.bib").is_dir()
    assert PUBLISHED_DOI in _read(author_dir, "zzz_readable.bib").lower()


# --- Composite-dedup threshold boundary -------------------------------------
#
# Path exercised: existing=preprint on disk, incoming=published, identical
# title (title_sim = 1.0, so the preprint-pair composite check is entered).
# The only variable across the two cases is the author list, which moves
# compute_dedup_score(count_preprint_xor=False) from ~0.535 (below) to ~0.785
# (above) the 0.60 threshold. Both sides sit > 0.02 from the threshold.

_BELOW_AUTHORS = ("Smith, John", "Doe, Jane")  # disjoint => author_overlap 0.0
_ABOVE_AUTHORS = ("Kaiming, He and Zhang, Xiangyu", "Kaiming, He and Zhang, Xiangyu")


def _boundary_pair(existing_author: str, incoming_author: str) -> tuple[factories.Entry, factories.Entry]:
    existing_preprint = factories.article(
        title=BOUNDARY_TITLE, author=existing_author, year="2023", journal="arXiv", doi=PREPRINT_DOI, key="P"
    )
    incoming_published = factories.article(
        title=BOUNDARY_TITLE,
        author=incoming_author,
        year="2023",
        journal="Nature Communications",
        doi=PUBLISHED_DOI,
        key="Q",
    )
    return existing_preprint, incoming_published


def test_composite_below_threshold_keeps_two_files(tmp_path: Path) -> None:
    """Composite just below ``SIM_DEDUP_COMPOSITE_THRESHOLD`` => no merge, two files."""
    author_dir = _author_dir(tmp_path)
    existing, incoming = _boundary_pair(*_BELOW_AUTHORS)
    score = compute_dedup_score(existing["fields"], incoming["fields"], count_preprint_xor=False)
    assert score < SIM_DEDUP_COMPOSITE_THRESHOLD - 0.02  # robustly below, not on the fence
    factories.write_bib(author_dir, existing, "pre.bib")

    _path, was_written = save_entry_to_file(str(tmp_path), AUTHOR_ID, incoming)

    assert was_written is True
    remaining = _bib_files(author_dir)
    assert len(remaining) == 2
    assert _dois_on_disk(author_dir) == {PREPRINT_DOI, PUBLISHED_DOI}


def test_composite_above_threshold_merges_to_one_file(tmp_path: Path) -> None:
    """Composite just above ``SIM_DEDUP_COMPOSITE_THRESHOLD`` => merge, one file.

    The single flipped signal (author overlap) tips the same preprint/published
    pair over the threshold; the preprint is replaced by the published record.
    """
    author_dir = _author_dir(tmp_path)
    existing, incoming = _boundary_pair(*_ABOVE_AUTHORS)
    score = compute_dedup_score(existing["fields"], incoming["fields"], count_preprint_xor=False)
    assert score > SIM_DEDUP_COMPOSITE_THRESHOLD + 0.02  # robustly above, not on the fence
    factories.write_bib(author_dir, existing, "pre.bib")

    _path, was_written = save_entry_to_file(str(tmp_path), AUTHOR_ID, incoming)

    assert was_written is True
    remaining = _bib_files(author_dir)
    assert len(remaining) == 1
    surviving = _read(author_dir, remaining[0]).lower()
    assert PUBLISHED_DOI in surviving
    assert PREPRINT_DOI not in surviving


@pytest.mark.parametrize(
    ("authors", "expected_files"),
    [(_BELOW_AUTHORS, 2), (_ABOVE_AUTHORS, 1)],
    ids=["below_threshold_two_files", "above_threshold_one_file"],
)
def test_composite_boundary_flips_verdict(tmp_path: Path, authors: tuple[str, str], expected_files: int) -> None:
    """The merge/no-merge verdict flips across the composite threshold.

    Consolidated parametrized guard: only the author list changes between the
    two rows, yet the on-disk file count flips 2 <-> 1 as the composite crosses
    ``SIM_DEDUP_COMPOSITE_THRESHOLD``.
    """
    author_dir = _author_dir(tmp_path)
    existing, incoming = _boundary_pair(*authors)
    factories.write_bib(author_dir, existing, "pre.bib")

    save_entry_to_file(str(tmp_path), AUTHOR_ID, incoming)

    assert len(_bib_files(author_dir)) == expected_files
