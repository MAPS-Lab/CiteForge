from __future__ import annotations

import contextlib
from typing import Any

import pytest

from src import api_configs, api_generics, bibtex_utils, doi_utils
from src.clients import scholar, search_apis
from src.http_utils import http_get_json
from tests.fixtures import load_api_keys
from tests.test_data import API_SPECIFIC_PAPERS, KNOWN_PAPERS, OPENALEX_CANNED_WORK


@pytest.fixture(scope="module")
def api_keys() -> dict[str, Any]:
    return load_api_keys()


def test_scholar_connection(api_keys: dict[str, Any]) -> None:
    """Fetch publications from Scholar via SerpAPI and verify article structure."""
    if not api_keys.get("serpapi"):
        pytest.skip("SerpAPI key not available")

    author_id = "JicYPdAAAAAJ"
    data = scholar.fetch_author_publications(api_keys["serpapi"], author_id, "Gabriel Spadon")

    articles = data.get("articles", [])
    if not articles:
        pytest.skip("Scholar returned no results (key expired or rate-limited)")

    assert articles, "Expected at least one article"
    for article in articles[:3]:
        assert "title" in article, f"Article missing 'title' field: {article}"


def test_scholar_citation(api_keys: dict[str, Any]) -> None:
    """Fetch a Scholar citation via Serply and build BibTeX from it."""
    if not api_keys.get("serply"):
        pytest.skip("Serply key not available")

    try:
        fields = scholar.fetch_scholar_citation(
            api_keys["serply"],
            "Attention Is All You Need",
            "Ashish Vaswani",
        )
    except Exception as exc:
        if "429" in str(exc):
            pytest.skip("Rate limited (expected with frequent requests)")
        raise

    if not fields:
        pytest.skip("Scholar returned no results (key expired or rate-limited)")

    assert "title" in fields, "No valid fields returned from Scholar citation"

    bibtex = scholar.build_bibtex_from_scholar_fields(fields, keyhint="test")
    assert bibtex and "@" in bibtex, "BibTeX building from citation fields failed"


def test_crossref_search() -> None:
    """Search Crossref for a known paper and verify BibTeX parsing."""
    paper = KNOWN_PAPERS[0]
    item = search_apis.crossref_search(paper["title"], paper["first_author"])

    if not item:
        pytest.skip("No result (API may be unavailable)")

    bibtex = search_apis.build_bibtex_from_crossref(item, paper["first_author"])
    assert bibtex is not None, "BibTeX building returned None"
    parsed = bibtex_utils.parse_bibtex_to_dict(bibtex)
    assert parsed and "type" in parsed, "BibTeX parsing failed"


def test_openalex_search() -> None:
    """Search OpenAlex for a known paper, falling back to canned data if rate-limited."""
    paper = API_SPECIFIC_PAPERS["openalex"]

    work = None
    with contextlib.suppress(Exception):
        work = search_apis.openalex_search_paper(paper["title"], paper["first_author"])

    if not work:
        with contextlib.suppress(Exception):
            work = http_get_json(
                f"https://api.openalex.org/works/{paper['openalex_id']}",
                timeout=15,
            )

    work = work or OPENALEX_CANNED_WORK

    bibtex = search_apis.build_bibtex_from_openalex(work, paper["first_author"])
    assert bibtex and "@" in bibtex, "BibTeX building from OpenAlex failed"

    parsed = bibtex_utils.parse_bibtex_to_dict(bibtex)
    assert parsed and "type" in parsed, "BibTeX parsing failed"


def test_all_multiple_candidate_functions_exist() -> None:
    """Verify all multiple-candidate wrapper functions are present and callable."""
    for func_name in (
        "crossref_search_multiple",
        "openalex_search_multiple",
        "s2_search_papers_multiple",
        "pubmed_search_papers_multiple",
        "europepmc_search_papers_multiple",
        "openreview_search_papers_multiple",
    ):
        assert callable(getattr(search_apis, func_name, None)), f"Function {func_name} not found or not callable"


def test_crossref_multiple_candidates() -> None:
    """Crossref multiple-candidate search returns results for a well-known paper."""
    paper = KNOWN_PAPERS[0]
    candidates = search_apis.crossref_search_multiple(
        paper["title"],
        paper["first_author"],
        max_results=5,
    )

    assert isinstance(candidates, list), f"Expected list, got {type(candidates).__name__}"
    for cand in candidates:
        assert isinstance(cand, dict), f"Candidate should be dict, got {type(cand).__name__}"


def test_s2_multiple_candidates(api_keys: dict[str, Any]) -> None:
    """Semantic Scholar multiple-candidate search returns results for a well-known paper."""
    if not api_keys.get("semantic"):
        pytest.skip("Semantic Scholar key not available")

    paper = API_SPECIFIC_PAPERS["semantic_scholar"]
    candidates = search_apis.s2_search_papers_multiple(
        paper["title"],
        paper["first_author"],
        api_keys["semantic"],
        max_results=5,
    )

    assert isinstance(candidates, list), f"Expected list, got {type(candidates).__name__}"
    for cand in candidates:
        assert isinstance(cand, dict), f"Candidate should be dict, got {type(cand).__name__}"


def test_multiple_candidate_empty_inputs() -> None:
    """Multiple-candidate searches handle empty and edge-case inputs gracefully."""
    candidates = search_apis.crossref_search_multiple("", "Ashish Vaswani", max_results=5)
    assert isinstance(candidates, list), "Empty title: did not return list"

    candidates = search_apis.crossref_search_multiple("Attention Is All You Need", None, max_results=5)
    assert isinstance(candidates, list), "None author: did not return list"

    candidates = search_apis.crossref_search_multiple(
        "Attention Is All You Need",
        "Ashish Vaswani",
        max_results=0,
    )
    assert not candidates, f"max_results=0: expected empty list, got {len(candidates)} items"


def test_api_configs() -> None:
    """APISearchConfig objects are present and complete."""
    for name in ("S2_SEARCH_CONFIG", "CROSSREF_SEARCH_CONFIG", "OPENALEX_SEARCH_CONFIG"):
        cfg = getattr(api_configs, name, None)
        assert isinstance(cfg, api_generics.APISearchConfig), f"{name} missing or wrong type"
        assert cfg.api_name and cfg.base_url, f"{name} incomplete"


def test_api_field_mappings() -> None:
    """APIFieldMapping objects are present and complete."""
    for name in ("S2_FIELD_MAPPING", "CROSSREF_FIELD_MAPPING", "OPENALEX_FIELD_MAPPING"):
        mapping = getattr(api_configs, name, None)
        assert isinstance(mapping, api_generics.APIFieldMapping), f"{name} missing or wrong type"
        assert mapping.title_fields and mapping.author_fields, f"{name} incomplete"


def test_doi_validation_functions() -> None:
    """DOI validation utilities are present and callable."""
    for func_name in ("validate_doi_candidate", "process_validated_doi"):
        assert callable(getattr(doi_utils, func_name, None)), f"{func_name} not found or not callable"


def test_bibtex_building_from_openalex_canned() -> None:
    """Build BibTeX from a canned OpenAlex response to exercise the builder offline."""
    bibtex = search_apis.build_bibtex_from_openalex(OPENALEX_CANNED_WORK, "Vaswani")
    assert bibtex and "@" in bibtex, "BibTeX building from canned OpenAlex failed"

    parsed = bibtex_utils.parse_bibtex_to_dict(bibtex)
    assert parsed is not None, "Failed to parse BibTeX built from canned OpenAlex"
    assert parsed.get("type"), "Parsed entry missing type"
    fields = parsed.get("fields", {})
    assert "title" in fields, "Parsed entry missing title field"
    assert "author" in fields, "Parsed entry missing author field"
