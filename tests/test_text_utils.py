"""Pure-unit contracts for the similarity scorers in :mod:`citeforge.text_utils`.

Covers the four signals dedup relies on: venue similarity (pure fuzz, no hidden
preprint-vs-published bonus), title similarity, author-overlap Jaccard, and the
XOR signal accounting inside ``compute_dedup_score``. Golden values were read
from the live functions; fuzz-derived checks use bands robustly away from every
decision threshold rather than brittle exact scores.
"""

from __future__ import annotations

import pytest
from rapidfuzz.fuzz import ratio as fuzz_ratio

from citeforge.text_utils import (
    author_overlap_ratio,
    compute_dedup_score,
    normalize_title,
    title_similarity,
    venue_similarity,
)
from tests.corpus import PREPRINT_SERVER_JOURNALS

# A real journal name distinct from every preprint server, so a preprint-vs-this
# pair scores far below the 0.5 duplicate band (max observed ~0.24).
_JOURNAL = "IEEE Transactions on Pattern Analysis and Machine Intelligence"


def _pure_fuzz(a: str, b: str) -> float:
    """Reference venue score: rapidfuzz over normalized strings, 1.0 when equal.

    This is the whole contract of ``venue_similarity`` -- no preprint/published
    (XOR) term is added, so equality with this value proves no double-counting.
    """
    na, nb = normalize_title(a), normalize_title(b)
    if na == nb:
        return 1.0
    return fuzz_ratio(na, nb) / 100.0


def _venue_rows() -> list[tuple[str, str, bool]]:
    """(venue_a, venue_b, expect_below_half) rows covering the full contract."""
    rows: list[tuple[str, str, bool]] = []
    for srv in PREPRINT_SERVER_JOURNALS:
        rows.append((srv, srv, False))  # identical venue -> 1.0
        rows.append((srv, _JOURNAL, True))  # preprint server vs journal -> pure fuzz, < 0.5
    return rows


@pytest.mark.parametrize(("venue_a", "venue_b", "below_half"), _venue_rows())
def test_venue_similarity_is_pure_fuzz(venue_a: str, venue_b: str, below_half: bool) -> None:
    """``venue_similarity`` equals rapidfuzz-over-normalized-strings with no
    hidden XOR bonus: identical venues score 1.0, and a preprint-server-vs-journal
    pair returns exactly the fuzz ratio and stays below the 0.5 duplicate band."""
    fields_a = {"journal": venue_a}
    fields_b = {"journal": venue_b}
    got = venue_similarity(fields_a, fields_b)
    assert got == _pure_fuzz(venue_a, venue_b)
    if venue_a == venue_b:
        assert got == 1.0
    if below_half:
        assert got < 0.5


def test_venue_similarity_empty_side_is_zero() -> None:
    """A missing venue on either side yields 0.0 (no container to compare)."""
    assert venue_similarity({"journal": ""}, {"journal": "Nature"}) == 0.0
    assert venue_similarity({}, {"journal": "Nature"}) == 0.0


# (title_a, title_b, lo, hi) -- inclusive band for the returned score. Identical
# and empty/None cases pin an exact value (lo == hi); fuzz cases use a band that
# is at least 0.13 from any threshold, per the no-close-to-threshold rule.
_TITLE_CASES: list[tuple[str | None, str | None, float, float]] = [
    ("Deep Residual Learning", "Deep Residual Learning", 1.0, 1.0),
    ("Attention Is All You Need", "attention is all you need", 1.0, 1.0),
    (
        "Deep Residual Learning for Image Recognition",
        "Deep Residual Learning for Image Recognation",
        0.90,
        0.999,
    ),
    ("Quantum Computing Foundations", "A Survey of Marine Biology", 0.0, 0.45),
    ("", "", 1.0, 1.0),
    (None, None, 1.0, 1.0),
    (None, "Something", 0.0, 0.0),
    ("", "Something", 0.0, 0.0),
]


@pytest.mark.parametrize(("a", "b", "lo", "hi"), _TITLE_CASES)
def test_title_similarity_bands(a: str | None, b: str | None, lo: float, hi: float) -> None:
    """Identical titles score 1.0, near-duplicates score high (0.90-0.999),
    distinct titles score low (< 0.5), and empty/None inputs are safe -- two
    empties/Nones normalize equal (1.0) while an empty-vs-text pair is 0.0."""
    score = title_similarity(a, b)
    assert lo <= score <= hi


# (authors_a, authors_b, expected) -- Jaccard on last-name+initials signatures,
# exact rationals (no fuzz), so equality is asserted directly.
_AUTHOR_CASES: list[tuple[str, str, float]] = [
    ("Smith, John and Doe, Jane", "Smith, John and Doe, Jane", 1.0),
    ("Doe, Jane and Smith, John", "Smith, John and Doe, Jane", 1.0),
    ("Smith, John A", "Smith, Robert B", 0.0),
    ("Smith, John", "Zhang, Wei", 0.0),
    ("Smith, John and Doe, Jane", "Smith, John", 0.5),
    ("", "Smith, John", 0.0),
]


@pytest.mark.parametrize(("authors_a", "authors_b", "expected"), _AUTHOR_CASES)
def test_author_overlap_ratio(authors_a: str, authors_b: str, expected: float) -> None:
    """Jaccard over normalized author signatures: full overlap (any order) is
    1.0, partial overlap is a proper fraction, and disjoint/empty lists are
    0.0."""
    assert author_overlap_ratio(authors_a, authors_b) == expected


def test_author_overlap_distinguishes_same_surname_different_initials() -> None:
    """Same surname with different initials is NOT treated as overlap: the shared
    surname alone scores 0.0, while identical initials score 1.0."""
    identical = author_overlap_ratio("Smith, John A", "Smith, John A")
    different = author_overlap_ratio("Smith, John A", "Smith, Robert B")
    assert identical == 1.0
    assert different == 0.0
    assert different < identical


def _preprint_side() -> dict[str, str]:
    return {"title": "T", "author": "Smith, John", "year": "2021", "journal": "arXiv"}


def _published_side(journal: str) -> dict[str, str]:
    return {"title": "T", "author": "Smith, John", "year": "2021", "journal": journal}


# (fields_a, fields_b, expected_delta) -- difference between counting the
# preprint-vs-published XOR (Signal 6) and not counting it.
_XOR_DELTA_CASES: list[tuple[dict[str, str], dict[str, str], float]] = [
    (_preprint_side(), _published_side("Nature"), 0.10),  # XOR true -> the 0.10 signal fires
    (_published_side("Nature"), _published_side("Science"), 0.0),  # both published -> no split
    (_preprint_side(), {"title": "T", "author": "Smith, John", "year": "2021", "journal": "bioRxiv"}, 0.0),
]


@pytest.mark.parametrize(("fields_a", "fields_b", "expected_delta"), _XOR_DELTA_CASES)
def test_compute_dedup_score_xor_signal_accounting(
    fields_a: dict[str, str], fields_b: dict[str, str], expected_delta: float
) -> None:
    """Toggling ``count_preprint_xor`` moves the composite by exactly the 0.10
    XOR signal for a preprint/journal pair and by 0.0 when both sides are the
    same class -- proving the split is counted exactly once."""
    with_xor = compute_dedup_score(fields_a, fields_b, count_preprint_xor=True)
    without_xor = compute_dedup_score(fields_a, fields_b, count_preprint_xor=False)
    assert with_xor - without_xor == pytest.approx(expected_delta, abs=1e-9)
