"""Tests for citeforge.venue.first_non_generic_container.

Includes differential tests whose oracles are verbatim replicas of the two
inline selection expressions the helper replaced (the ``next(...)`` form in
``api_generics._extract_venue`` and the loop form in
``clients.search_apis.bibtex_from_csl``), plus behavior pins on
``bibtex_from_csl`` container selection over representative CSL payloads.
"""

from __future__ import annotations

from typing import Any

import pytest

from citeforge.clients.search_apis import bibtex_from_csl
from citeforge.config import GENERIC_SERIES_NAMES
from citeforge.venue import first_non_generic_container

_GENERIC = "Lecture Notes in Computer Science"
_CONFERENCE = "International Conference on Machine Learning"

_CASES: list[list[Any]] = [
    [_GENERIC, _CONFERENCE],  # Crossref [series, conference] shape
    [_CONFERENCE, _GENERIC],  # non-generic first
    [_GENERIC, _GENERIC.upper()],  # generic-only (case-insensitive)
    ["", "   "],  # empty and whitespace-only
    ["", _CONFERENCE],  # empty then usable
    [None, 123, _CONFERENCE],  # non-string elements coerced via str()
    [" Padded Venue ", _GENERIC],  # stripping applied to the winner
    [],  # empty list
    [_GENERIC.title()],  # single generic element
]


def _legacy_extract_venue_selection(raw_venue: list[Any]) -> str | None:
    """Replica of the pre-consolidation next(...) in api_generics._extract_venue."""
    return next(
        (str(c).strip() for c in raw_venue if str(c).strip() and str(c).strip().lower() not in GENERIC_SERIES_NAMES),
        None,
    )


def _legacy_bibtex_from_csl_selection(container_raw: list[Any]) -> str | None:
    """Replica of the pre-consolidation loop in clients.search_apis.bibtex_from_csl."""
    container = None
    for candidate in container_raw:
        candidate_str = str(candidate).strip()
        if candidate_str and candidate_str.lower() not in GENERIC_SERIES_NAMES:
            container = candidate_str
            break
    return container


@pytest.mark.parametrize("values", _CASES)
def test_first_non_generic_container_matches_both_legacy_selections(values: list[Any]) -> None:
    result = first_non_generic_container(values)

    assert result == _legacy_extract_venue_selection(values)
    assert result == _legacy_bibtex_from_csl_selection(values)


def test_first_non_generic_container_never_returns_empty_or_generic() -> None:
    for values in _CASES:
        result = first_non_generic_container(values)
        if result is not None:
            assert result == result.strip()
            assert result
            assert result.lower() not in GENERIC_SERIES_NAMES


def _csl(container_title: Any, event: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": "A Representative Paper",
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "issued": {"date-parts": [[2024]]},
        "type": "paper-conference",
        "DOI": "10.1000/xyz",
    }
    if container_title is not None:
        payload["container-title"] = container_title
    if event is not None:
        payload["event"] = event
    return payload


def test_bibtex_from_csl_prefers_non_generic_container_element() -> None:
    bib = bibtex_from_csl(_csl([_GENERIC, _CONFERENCE]), keyhint="k")

    assert _CONFERENCE in bib
    assert _GENERIC not in bib


def test_bibtex_from_csl_generic_container_falls_back_to_event_name() -> None:
    bib = bibtex_from_csl(_csl(_GENERIC, event={"name": "MICCAI 2024"}), keyhint="k")

    assert "MICCAI 2024" in bib


def test_bibtex_from_csl_generic_only_array_keeps_first_element_without_event() -> None:
    # All-generic array: selection yields None, safe_get_field falls back to
    # the first element, and without an event name the generic value stays.
    bib = bibtex_from_csl(_csl([_GENERIC, _GENERIC]), keyhint="k")

    assert _GENERIC in bib


def test_bibtex_from_csl_missing_container_omits_venue() -> None:
    bib = bibtex_from_csl(_csl(None), keyhint="k")

    assert "booktitle" not in bib and "journal" not in bib
