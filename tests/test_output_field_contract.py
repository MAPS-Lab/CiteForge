"""Contract for what the pipeline is allowed to write into a BibTeX field.

Every field the pipeline sets is published verbatim: the entry is committed to
`output/`, aggregated by the website importer, and rendered as the citation a
reader copies. The website's importer additionally lists `note` among the
fields it preserves, so a diagnostic written there can never be corrected
downstream. Pipeline provenance therefore belongs in the audit log, which is
where every one of these branches already writes it, and never in the entry.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ARTICLE = Path(__file__).resolve().parents[1] / "citeforge/pipeline/article.py"

# Words that describe the pipeline's own confidence or coverage rather than the
# work being cited. A field value containing one is a leaked diagnostic.
_DIAGNOSTIC_WORDS = (
    "unenriched",
    "unverified",
    "enrichment",
    "fallback",
    "heuristic",
    "stub",
    "serpapi",
    "scholar",
    "no match",
    "not found",
)


def _field_string_literals() -> list[tuple[int, str]]:
    """Every string literal assigned to a merged BibTeX field, with its line."""
    tree = ast.parse(_ARTICLE.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)):
                continue
            if not target.value.id.startswith("merged_fields"):
                continue
            for part in ast.walk(node.value):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    found.append((part.lineno, part.value))
    return found


def test_the_parser_is_finding_the_assignments_it_claims_to_check() -> None:
    """A walk that silently matches nothing would pass the contract below
    forever, so pin that real assignments are in scope."""
    assert len(_field_string_literals()) >= 1


def test_no_bibtex_field_carries_a_pipeline_diagnostic() -> None:
    """`note = {Unenriched: no enrichment sources matched}` shipped to a public
    citation page before this contract existed."""
    leaked = [
        (line, value, word)
        for line, value in _field_string_literals()
        for word in _DIAGNOSTIC_WORDS
        if word in value.lower()
    ]

    assert not leaked, f"pipeline diagnostics assigned to BibTeX fields: {leaked}"
