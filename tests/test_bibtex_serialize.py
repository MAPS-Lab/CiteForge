"""Golden byte-identity tests for the BibTeX serializer.

Byte-identical .bib output on cache-hit runs is the CiteForge PRIME DIRECTIVE.
``bibtex_from_dict`` is the single choke point that turns an entry dict into
those bytes, so these tests pin its exact output: the preferred field order,
the sorted tail for non-preferred fields, the two-space indent, the
``@type{key,`` header, the absence of a trailing comma on the last field, and
the terminating newline. A field reorder or whitespace drift here would change
every emitted file, and one of these assertions must fail before that ships.
"""

from __future__ import annotations

from citeforge.bibtex_utils import bibtex_from_dict, parse_bibtex_to_dict
from tests import factories

# Captured from the live serializer. Regenerate only through a reviewed step if
# the output contract deliberately changes; a hand-edited value here would
# become a rival source of truth.
GOLDEN = (
    "@article{Smith2024-Widgets,\n"
    "  title = {A Study of Widgets},\n"
    "  author = {Smith, John and Doe, Jane},\n"
    "  year = {2024},\n"
    "  journal = {Nature},\n"
    "  volume = {12},\n"
    "  pages = {1-10},\n"
    "  doi = {10.1145/3580305},\n"
    "  abstract = {We study widgets.},\n"
    "  keywords = {widgets, study},\n"
    "  note = {preprint note}\n"
    "}\n"
)

_RICH_ENTRY = {
    "type": "article",
    "key": "Smith2024-Widgets",
    "fields": {
        "year": "2024",
        "title": "A Study of Widgets",
        "doi": "10.1145/3580305",
        "author": "Smith, John and Doe, Jane",
        "journal": "Nature",
        "keywords": "widgets, study",
        "abstract": "We study widgets.",
        "note": "preprint note",
        "pages": "1-10",
        "volume": "12",
    },
}


def test_serializer_emits_exact_golden_bytes() -> None:
    """The serializer output equals the captured golden string byte for byte."""
    assert bibtex_from_dict(_RICH_ENTRY) == GOLDEN


def test_preferred_fields_precede_sorted_tail() -> None:
    """Preferred citation fields come first in canonical order; every remaining
    field follows in sorted() order."""
    lines = [ln.strip() for ln in bibtex_from_dict(_RICH_ENTRY).splitlines() if " = {" in ln]
    keys = [ln.split(" = {", 1)[0] for ln in lines]
    assert keys == ["title", "author", "year", "journal", "volume", "pages", "doi", "abstract", "keywords", "note"]


def test_last_field_has_no_trailing_comma() -> None:
    """The final field line ends with ``}`` and no trailing comma; the entry
    closes on its own line."""
    out = bibtex_from_dict(_RICH_ENTRY)
    body = out.rstrip("\n").splitlines()
    assert body[-1] == "}"
    assert body[-2].strip() == "note = {preprint note}"
    assert not body[-2].rstrip().endswith(",")


def test_input_field_order_does_not_change_output() -> None:
    """Two entries with identical fields inserted in different dict order
    serialize to identical bytes (the serializer imposes its own order)."""
    reordered = {
        "type": "article",
        "key": "Smith2024-Widgets",
        "fields": dict(reversed(list(_RICH_ENTRY["fields"].items()))),  # type: ignore[attr-defined]
    }
    assert bibtex_from_dict(reordered) == bibtex_from_dict(_RICH_ENTRY)


def test_serialize_is_idempotent_through_parse() -> None:
    """Serializing, parsing, and re-serializing yields the same bytes (the
    round trip is a fixpoint, so a cache-hit re-save cannot drift)."""
    once = bibtex_from_dict(_RICH_ENTRY)
    reparsed = parse_bibtex_to_dict(once)
    assert reparsed is not None
    assert bibtex_from_dict(reparsed) == once


def test_nonascii_author_folds_to_stable_ascii() -> None:
    """A non-ASCII author is deterministically folded to ASCII (the serializer
    strips accents), and the fold is a fixpoint, so a re-save never drifts and
    never emits mixed encodings."""
    e = factories.nonascii_author()
    once = bibtex_from_dict(e)
    # Accents are folded, not preserved, and the result is pure ASCII.
    assert "Muller, Andre" in once
    once.encode("ascii")  # raises if any non-ASCII byte survived
    reparsed = parse_bibtex_to_dict(once)
    assert reparsed is not None
    assert bibtex_from_dict(reparsed) == once
