"""Pure-unit contracts for :mod:`citeforge.id_utils`.

Drives the real DOI and arXiv identifier helpers over external truth tables
(``corpus.SECONDARY_DOI_CASES``) and hand-picked adversarial inputs. Every
expected value was captured from the live function, never hand-derived, so a
green test documents the current production contract exactly.
"""

from __future__ import annotations

import pytest

from citeforge.id_utils import (
    _norm_doi,
    extract_arxiv_eprint,
    find_arxiv_in_text,
    is_secondary_doi,
    normalize_doi,
)
from tests.corpus import SECONDARY_DOI_CASES


@pytest.mark.parametrize(("doi", "expected"), SECONDARY_DOI_CASES)
def test_is_secondary_doi_over_corpus(doi: str, expected: bool) -> None:
    """Preprint/grey-literature/data DOIs are secondary; published journal DOIs
    are primary even under a registrant that also mints preprints
    (``10.5194/egusphere`` secondary, ``10.5194/acp`` primary)."""
    assert is_secondary_doi(doi) is expected


@pytest.mark.parametrize(
    "doi",
    [
        "10.48550/ARXIV.2401.00001",
        "10.5281/ZENODO.9000001",
        "10.1101/2021.01.01.400001".upper(),
    ],
)
def test_is_secondary_doi_is_case_insensitive(doi: str) -> None:
    """The prefix check lowercases first, so an uppercase preprint DOI is still
    recognised as secondary."""
    assert is_secondary_doi(doi) is True


# (raw_doi, normalized) -- captured from the live _norm_doi.
_NORM_DOI_CASES: list[tuple[str | None, str | None]] = [
    ("10.1145/3580305", "10.1145/3580305"),
    ("https://doi.org/10.1145/3580305", "10.1145/3580305"),
    ("http://dx.doi.org/10.1145/3580305", "10.1145/3580305"),
    ("HTTPS://DOI.ORG/10.1145/3580305", "10.1145/3580305"),
    ("doi: 10.1145/3580305", "10.1145/3580305"),
    ("  10.1145/3580305  ", "10.1145/3580305"),
    ("10.1145/ABCdef", "10.1145/abcdef"),
    ("10.1000/abc%2Fdef", "10.1000/abc/def"),
    (None, None),
    ("", None),
    ("https://doi.org/", None),
]


@pytest.mark.parametrize(("raw", "expected"), _NORM_DOI_CASES)
def test_norm_doi_normalization(raw: str | None, expected: str | None) -> None:
    """``_norm_doi`` strips resolver/prefix wrappers, URL-decodes percent
    escapes, lowercases, and collapses empties to ``None``."""
    assert _norm_doi(raw) == expected


@pytest.mark.parametrize(
    "wrapper",
    [
        "https://doi.org/10.1145/3580305",
        "http://dx.doi.org/10.1145/3580305",
        "doi:10.1145/3580305",
        "10.1145/3580305",
    ],
)
def test_norm_doi_bare_url_equivalence(wrapper: str) -> None:
    """A bare DOI and its URL/``doi:``-wrapped forms normalize to one canonical
    string, which is what lets dedup compare records from different APIs."""
    assert _norm_doi(wrapper) == "10.1145/3580305"


def test_norm_doi_percent_encoding_matches_decoded_form() -> None:
    """A percent-encoded slash normalizes to the same value as the decoded DOI."""
    assert _norm_doi("10.1000/abc%2Fdef") == _norm_doi("10.1000/abc/def")


def test_normalize_doi_delegates_to_norm_doi() -> None:
    """The public ``normalize_doi`` is a thin alias over ``_norm_doi``."""
    for raw in ("https://doi.org/10.1/X", "doi: 10.2/Y", None, ""):
        assert normalize_doi(raw) == _norm_doi(raw)


# (entry_fields, expected_arxiv_id) for extract_arxiv_eprint -- version suffix
# is stripped so all versions collapse to one base id.
_EXTRACT_EPRINT_CASES: list[tuple[dict[str, str], str | None]] = [
    ({"doi": "10.48550/arXiv.2401.12345"}, "2401.12345"),
    ({"doi": "10.48550/arxiv.1512.03385"}, "1512.03385"),
    ({"archiveprefix": "arXiv", "eprint": "2401.12345v2"}, "2401.12345"),
    ({"journal": "arXiv:2401.12345"}, "2401.12345"),
    ({"doi": "10.1145/3580305"}, None),
    ({}, None),
]


@pytest.mark.parametrize(("fields", "expected"), _EXTRACT_EPRINT_CASES)
def test_extract_arxiv_eprint(fields: dict[str, str], expected: str | None) -> None:
    """arXiv id is recovered from the ``10.48550/arxiv.*`` DOI, the
    ``archiveprefix``/``eprint`` pair (version stripped), or an ``arXiv:...``
    journal string, and is ``None`` when no arXiv marker is present."""
    assert extract_arxiv_eprint({"fields": fields}) == expected


# (text_or_html, expected_arxiv_id) for find_arxiv_in_text.
_FIND_ARXIV_CASES: list[tuple[str, str | None]] = [
    ("see arXiv:2401.12345v3 for details", "2401.12345"),
    ("https://arxiv.org/abs/2401.12345", "2401.12345"),
    ("https://arxiv.org/pdf/1512.03385", "1512.03385"),
    ("10.48550/arxiv.2401.12345", "2401.12345"),
    ('<a href="https://arxiv.org/abs/2401.12345">preprint</a>', "2401.12345"),
    ("no identifier here", None),
    ("", None),
]


@pytest.mark.parametrize(("text", "expected"), _FIND_ARXIV_CASES)
def test_find_arxiv_in_text(text: str, expected: str | None) -> None:
    """arXiv ids are extracted from ``arxiv:`` prefixes, abs/pdf URLs (including
    inside HTML), and arXiv DOIs, returning ``None`` when nothing matches."""
    assert find_arxiv_in_text(text) == expected
