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

import pytest

from citeforge.bibtex_utils import bibtex_from_dict, parse_bibtex_to_dict, parse_strict_bibtex_document
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


def test_serializer_preserves_tildes_in_urls() -> None:
    """LaTeX whitespace handling must not corrupt a literal URL path segment."""
    entry = {
        "type": "misc",
        "key": "UrlTilde",
        "fields": {"url": "https://example.org/~researcher/article"},
    }
    assert bibtex_from_dict(entry) == "@misc{UrlTilde,\n  url = {https://example.org/~researcher/article}\n}\n"


@pytest.mark.parametrize(
    ("source", "expected_bibtex"),
    [
        ("The {API} Thing", "@article{CaseProtection,\n  title = {The {API} Thing}\n}\n"),
        (r"\textbf{The {API} Thing}", "@article{CaseProtection,\n  title = {The {API} Thing}\n}\n"),
        (r"\LaTeX{} API", "@article{CaseProtection,\n  title = {LaTeX API}\n}\n"),
    ],
)
def test_serializer_preserves_bibtex_case_protection(source: str, expected_bibtex: str) -> None:
    """Case groups survive serialization, except formatting macro wrappers."""
    entry = {"type": "article", "key": "CaseProtection", "fields": {"title": source}}
    assert bibtex_from_dict(entry) == expected_bibtex


@pytest.mark.parametrize(
    "value",
    [
        r"Schloss Dagstuhl - Leibniz-Zentrum f{\"u}r Informatik",
        r"Jos{\'e} Antonio",
        r"Mu{\~n}oz",
        r"Fran{\c c}aise",
        r"30 {\circ } C",
    ],
    ids=["umlaut", "acute", "tilde", "cedilla", "degree"],
)
def test_serializer_preserves_latex_accent_macros(value: str) -> None:
    """A braced accent macro survives serialization unchanged.

    `latex_to_ascii` folds `f{\\"u}r` to "fur" and deletes `{\\circ }` outright,
    so re-serializing silently degraded 20 committed files: they held the
    correct escape on disk and any rewrite spelled the German publisher name
    "fur". It also made `_rule_fix_zentrum_umlaut` unreachable, because the
    escape that rule restores was stripped again on the way out.
    """
    entry = {"type": "article", "key": "Accent", "fields": {"title": "T", "publisher": value}}

    assert f"publisher = {{{value}}}" in bibtex_from_dict(entry)


def test_serializer_encodes_raw_unicode_accents_as_latex_macros() -> None:
    """Raw Unicode accents are encoded to LaTeX macros, not folded to ASCII,
    the same as an accent macro already present in the input."""
    entry = {"type": "article", "key": "Raw", "fields": {"title": "T", "publisher": "Zentrum für Informatik"}}

    assert 'publisher = {Zentrum f{\\"u}r Informatik}' in bibtex_from_dict(entry)


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


def test_nonascii_author_encodes_to_stable_latex_macros() -> None:
    """A non-ASCII author is deterministically encoded to LaTeX macros (the
    serializer preserves accents rather than stripping them), the encoding
    is still pure ASCII on disk, and it is a fixpoint, so a re-save never
    drifts and never emits mixed encodings."""
    e = factories.nonascii_author()
    once = bibtex_from_dict(e)
    # Accents are preserved as macros, not folded, and the result is still ASCII.
    assert "M{\\\"u}ller, Andr{\\'e} and S{\\o}rensen, Bj{\\o}rn" in once
    once.encode("ascii")  # raises if any non-ASCII byte survived
    reparsed = parse_bibtex_to_dict(once)
    assert reparsed is not None
    assert bibtex_from_dict(reparsed) == once


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CD16a\nhigh NK cell infiltration", "CD16a high NK cell infiltration"),
        ("Detecting Opioid Misuse (\nNER\n) With Deep Learning", "Detecting Opioid Misuse ( NER ) With Deep Learning"),
        ("tabs\tand   runs", "tabs and runs"),
    ],
    ids=["newline", "wrapped-parenthetical", "tabs-and-runs"],
)
def test_field_values_are_written_on_one_line(raw: str, expected: str) -> None:
    """A provider title containing a newline must not reach the file verbatim.

    The strict parser collapses whitespace on read, so a multi-line value makes
    the entry unequal to its own re-serialization. The post-run repair then
    rewrites it every run, the corpus digest never repeats, and the monthly
    refresh loop cannot converge.
    """
    entry = {"type": "article", "key": "K", "fields": {"title": raw, "author": "Doe, J", "year": "2021"}}

    out = bibtex_from_dict(entry)

    assert f"title = {{{expected}}}" in out
    assert out.count("\n") == len([line for line in out.splitlines() if line])


def test_a_written_entry_round_trips_byte_identically() -> None:
    """Serialize, parse, serialize again: the bytes must not move."""
    entry = {
        "type": "article",
        "key": "K",
        "fields": {"title": "CD16a\nhigh NK cells", "author": "Doe, J", "year": "2021", "journal": "J"},
    }

    once = bibtex_from_dict(entry)

    assert bibtex_from_dict(parse_strict_bibtex_document(once.encode())) == once


def test_portuguese_diacritics_round_trip_through_reload() -> None:
    """A Portuguese title with cedilla and tilde survives serialization, and
    a second write after reloading the file does not degrade it further.

    Regression for belizario2026aprendizado: the title lost its cedilla and
    tilde entirely (Reforco/Alocacao, not even a LaTeX macro), because the
    pre-fix serializer unidecode-stripped any accent that was not already a
    braced macro in the input.
    """
    title = "Aprendizado por Reforço para Alocação de Rins"
    entry = {"type": "misc", "key": "Belizario2026", "fields": {"title": title, "author": "A and B", "year": "2026"}}

    once = bibtex_from_dict(entry)
    assert "Refor{\\c{c}}o" in once
    assert "Aloca{\\c{c}}{\\~a}o" in once

    reparsed = parse_bibtex_to_dict(once)
    assert reparsed is not None
    twice = bibtex_from_dict(reparsed)
    assert twice == once
