"""State-isolation contracts for the BibTeX parser adapter."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from citeforge.bibtex_utils import parse_bibtex_to_dict


def test_sequential_parses_do_not_share_entries_or_string_macros() -> None:
    """A reused parser must reject undefined macros without poisoning later input."""
    first = parse_bibtex_to_dict('@string{venue = "First Venue"}\n@article{One, title = venue}')
    second = parse_bibtex_to_dict('@string{venue = "Second Venue"}\n@article{Two, title = venue}')

    assert first == {"type": "article", "key": "One", "fields": {"title": "First Venue"}}
    assert second == {"type": "article", "key": "Two", "fields": {"title": "Second Venue"}}
    assert parse_bibtex_to_dict("@article{Three, title = venue}") is None
    assert parse_bibtex_to_dict("@article{Four, title={Fourth Venue}}") == {
        "type": "article",
        "key": "Four",
        "fields": {"title": "Fourth Venue"},
    }


def test_concurrent_parses_remain_isolated() -> None:
    """Parallel author workers must not mix parser state across threads."""
    inputs = [f"@article{{Entry{index}, title={{Title {index}}}}}" for index in range(32)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(parse_bibtex_to_dict, inputs))

    assert [result["key"] for result in results if result is not None] == [f"Entry{index}" for index in range(32)]
    assert [result["fields"]["title"] for result in results if result is not None] == [
        f"Title {index}" for index in range(32)
    ]


def test_malformed_input_does_not_poison_the_next_parse() -> None:
    """A failed parse must leave the next independent input usable."""
    assert parse_bibtex_to_dict("@article{broken") is None
    assert parse_bibtex_to_dict("@article{Good, title={Good}}") == {
        "type": "article",
        "key": "Good",
        "fields": {"title": "Good"},
    }


def test_repeated_parse_results_are_defensive_copies() -> None:
    """Mutating one result must not alter a later result for the same text."""
    text = "@article{Independent, title={Original}}"
    first = parse_bibtex_to_dict(text)
    assert first is not None
    first["fields"]["title"] = "Mutated"

    assert parse_bibtex_to_dict(text) == {
        "type": "article",
        "key": "Independent",
        "fields": {"title": "Original"},
    }
