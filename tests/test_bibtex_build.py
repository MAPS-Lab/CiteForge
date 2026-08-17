"""Pure-unit contracts for :mod:`citeforge.bibtex_build`.

Drives the real entry-type classifier, container-field router, and BibTeX
assembler. Field values are read back with the shared ``extract_bibtex_field``
helper and every expected value was captured from the live functions, never
hand-derived. One exact-byte assertion locks the serializer output to guard the
byte-identical determinism contract.
"""

from __future__ import annotations

import pytest

from citeforge.bibtex_build import (
    build_bibtex_entry,
    create_scoring_function,
    determine_entry_type,
    get_container_field,
)
from citeforge.config import SIM_EXACT_PICK_THRESHOLD, SIM_THRESHOLD_TOLERANCE
from tests.conftest import extract_bibtex_field

# ---------------------------------------------------------------------------
# get_container_field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry_type", "field"),
    [
        ("article", "journal"),
        ("inproceedings", "booktitle"),
        ("incollection", "booktitle"),
        ("misc", "howpublished"),
        ("phdthesis", "howpublished"),
        ("book", "howpublished"),
        ("unknown", "howpublished"),
        ("", "howpublished"),
    ],
)
def test_get_container_field(entry_type: str, field: str) -> None:
    """Every entry type routes its venue to the captured container field."""
    assert get_container_field(entry_type) == field


# ---------------------------------------------------------------------------
# build_bibtex_entry — container routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry_type", "container", "other_containers"),
    [
        ("article", "journal", ("booktitle", "howpublished")),
        ("inproceedings", "booktitle", ("journal", "howpublished")),
        ("incollection", "booktitle", ("journal", "howpublished")),
        ("misc", "howpublished", ("journal", "booktitle")),
    ],
)
def test_build_bibtex_entry_container_routing(
    entry_type: str, container: str, other_containers: tuple[str, ...]
) -> None:
    """The venue lands in the type's container field and nowhere else."""
    out = build_bibtex_entry(entry_type, "A Study of Neural Networks", ["Doe, Jane"], 2021, "kh", venue="Some Venue")
    assert out.startswith(f"@{entry_type}{{")
    assert extract_bibtex_field(out, container) == "Some Venue"
    for other in other_containers:
        assert extract_bibtex_field(out, other) is None


def test_build_bibtex_entry_omits_empty_venue_doi_url() -> None:
    """Empty venue/doi/url are dropped (falsy fields are filtered out)."""
    out = build_bibtex_entry("article", "T", ["Doe, Jane"], 2021, "kh", venue="", doi="", url="")
    assert extract_bibtex_field(out, "journal") is None
    assert extract_bibtex_field(out, "doi") is None
    assert extract_bibtex_field(out, "url") is None
    # Present, non-empty fields survive.
    assert extract_bibtex_field(out, "title") == "T"
    assert extract_bibtex_field(out, "year") == "2021"


def test_build_bibtex_entry_omits_missing_optional_fields() -> None:
    """When no venue/doi/url/arxiv are supplied, only core fields appear."""
    out = build_bibtex_entry("article", "T", ["Doe, Jane"], 2021, "kh")
    for absent in ("journal", "doi", "url", "eprint", "archiveprefix"):
        assert extract_bibtex_field(out, absent) is None


def test_build_bibtex_entry_arxiv_id_emits_eprint_and_archiveprefix() -> None:
    """arxiv_id populates a normalized eprint and a literal archiveprefix=arXiv."""
    out = build_bibtex_entry("misc", "T", ["Doe, Jane"], 2021, "kh", arxiv_id="arXiv:2401.00001v2")
    # _norm_arxiv_id strips the ``arXiv:`` prefix and the version suffix.
    assert extract_bibtex_field(out, "eprint") == "2401.00001"
    assert extract_bibtex_field(out, "archiveprefix") == "arXiv"


def test_build_bibtex_entry_extra_fields_override_container() -> None:
    """extra_fields is applied after the base map, so it overrides the venue."""
    out = build_bibtex_entry(
        "article", "T", ["Doe, Jane"], 2021, "kh", venue="Nature", extra_fields={"journal": "Science", "pages": "1-10"}
    )
    assert extract_bibtex_field(out, "journal") == "Science"
    assert extract_bibtex_field(out, "pages") == "1-10"


def test_build_bibtex_entry_exact_bytes_locks_serializer() -> None:
    """The full serialized entry is byte-locked (determinism guard).

    Field order, brace style, indentation, and trailing newline are captured
    from the live function; any drift here would break byte-identical reruns.
    """
    out = build_bibtex_entry(
        "article",
        "A Study of Neural Networks",
        ["Doe, Jane"],
        2021,
        "kh",
        venue="Nature",
        doi="10.1000/xyz",
        url="http://x",
        arxiv_id="2401.00001",
    )
    expected = (
        "@article{Jane2021A,\n"
        "  title = {A Study of Neural Networks},\n"
        "  author = {Doe, Jane},\n"
        "  year = {2021},\n"
        "  journal = {Nature},\n"
        "  doi = {10.1000/xyz},\n"
        "  url = {http://x},\n"
        "  eprint = {2401.00001},\n"
        "  archiveprefix = {arXiv}\n"
        "}\n"
    )
    assert out == expected


# ---------------------------------------------------------------------------
# create_scoring_function -- year signal (published DOI recovery)
# ---------------------------------------------------------------------------

_ACCEPT = SIM_EXACT_PICK_THRESHOLD - SIM_THRESHOLD_TOLERANCE


def _candidate(title: str, author: str, year: int) -> dict[str, object]:
    """A Crossref-shaped candidate with a single author and an ``issued`` year."""
    given, _, family = author.partition(" ")
    return {
        "title": [title],
        "author": [{"given": given, "family": family}],
        "issued": {"date-parts": [[year]]},
    }


def _score(target_title: str, target_author: str, year_hint: int | None, cand: dict[str, object]) -> float:
    """Score *cand* against the target using the real scoring function."""
    fn = create_scoring_function(
        title=target_title,
        author_name=target_author,
        year_hint=year_hint,
        title_getter=lambda c: (c.get("title") or [""])[0],
        authors_getter=lambda c: c.get("author") or [],
        year_getter=lambda c: c.get("issued", {}).get("date-parts", [[None]])[0][0],
    )
    return fn(cand)


def test_year_hint_recovers_published_record_with_trivial_title_diff() -> None:
    """A same-year, same-author candidate whose title differs only by a leading
    article word ("A Web-based" vs "Web-based") clears the accept threshold WHEN a
    matching year hint is supplied, but falls just below it without one. This is
    the exact NxPlain/EACL case that left a preprint DOI in place; the year signal
    is what recovers the authoritative published DOI.
    """
    target_title = "NxPlain: Web-based Tool for Discovery of Latent Concepts"
    cand = _candidate("NxPlain: A Web-based Tool for Discovery of Latent Concepts", "Hassan Sajjad", 2023)

    without_year = _score(target_title, "Hassan Sajjad", None, cand)
    with_year = _score(target_title, "Hassan Sajjad", 2023, cand)

    assert without_year < _ACCEPT, "regression guard: without a year hint the record is (wrongly) rejected"
    assert with_year >= _ACCEPT, "with a matching year hint the published record must be accepted"


def test_year_hint_does_not_admit_title_mismatch() -> None:
    """The year signal never rescues a title mismatch: the title-minimum gate
    short-circuits to zero before year is considered, so a wrong-title candidate
    scores 0 even with a matching year and author.
    """
    target_title = "Detecting Ongoing Events Using Contextual Word Embeddings"
    cand = _candidate("A Completely Unrelated Paper About Marine Biology", "Hassan Sajjad", 2023)
    assert _score(target_title, "Hassan Sajjad", 2023, cand) == 0.0


def test_year_hint_wrong_year_gives_no_bonus() -> None:
    """A candidate whose year disagrees with the hint earns no year bonus, so the
    bonus cannot inflate an off-year near-title match past the threshold.
    """
    target_title = "NxPlain: Web-based Tool for Discovery of Latent Concepts"
    cand = _candidate("NxPlain: A Web-based Tool for Discovery of Latent Concepts", "Hassan Sajjad", 2019)

    off_year = _score(target_title, "Hassan Sajjad", 2023, cand)
    no_hint = _score(target_title, "Hassan Sajjad", None, cand)
    assert off_year == no_hint, "a year mismatch must contribute exactly nothing (same as no hint)"
    assert off_year < _ACCEPT, "an off-year near-title match must not be auto-accepted"


# ---------------------------------------------------------------------------
# determine_entry_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pub_types", "expected"),
    [
        (["JournalArticle"], "article"),
        (["Review"], "article"),
        (["Conference"], "inproceedings"),
        (["JournalArticle", "Conference"], "article"),  # journal wins first
    ],
)
def test_determine_entry_type_publication_types(pub_types: list[str], expected: str) -> None:
    """A Semantic-Scholar-style publicationTypes list classifies as captured."""
    obj = {"publicationTypes": pub_types}
    assert determine_entry_type(obj, publication_types_field="publicationTypes") == expected


@pytest.mark.parametrize(
    ("obj", "expected"),
    [
        ("journal-article", "article"),
        ("proceedings-article", "inproceedings"),
        ("book-chapter", "incollection"),
        ("book", "book"),
        ("something-weird", "misc"),
        (None, "misc"),
        ({"foo": "bar"}, "misc"),
        ({"type": "journal-article"}, "article"),
    ],
)
def test_determine_entry_type_string_and_dict(obj: object, expected: str) -> None:
    """String and dict type inputs classify to their captured entry types."""
    assert determine_entry_type(obj) == expected


def test_determine_entry_type_book_chapter_heuristic() -> None:
    """howpublished + publisher + pages (no journal/booktitle) yields incollection.

    The heuristic fires only when a book-series keyword or a book-publisher
    keyword is present.
    """
    obj = {
        "howpublished": "Lecture Notes in Computer Science",
        "publisher": "Springer",
        "pages": "1-10",
    }
    assert determine_entry_type(obj) == "incollection"


def test_determine_entry_type_no_journal_heuristic_misses_without_keyword() -> None:
    """The same shape without a series/publisher keyword falls through to misc."""
    obj = {"howpublished": "Some Blog", "publisher": "Self", "pages": "1-10"}
    assert determine_entry_type(obj) == "misc"


def test_determine_entry_type_conference_venue_keyword() -> None:
    """A conference keyword in a venue field classifies as inproceedings."""
    obj = {"journal": "Proceedings of the ACM Conference"}
    assert determine_entry_type(obj) == "inproceedings"


def test_determine_entry_type_venue_hints_fallback() -> None:
    """venue_hints is the last-resort router when nothing else classifies."""
    obj = {"eprint": "2401.00001"}
    assert determine_entry_type(obj, venue_hints={"eprint": "misc"}) == "misc"
