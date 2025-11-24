import sys
from pathlib import Path
import pytest
from src import api_clients, api_generics, bibtex_utils, api_configs, doi_utils
from tests.fixtures import load_api_keys
from tests.test_data import KNOWN_PAPERS, API_SPECIFIC_PAPERS

@pytest.fixture(scope="module")
def api_keys():
    return load_api_keys()

# ===== SERPAPI (GOOGLE SCHOLAR) =====

def test_serpapi_connection(api_keys):
    """
    Test SerpAPI connection and publication fetching.
    """
    if not api_keys.get('serpapi'):
        pytest.skip("SerpAPI key not available")

    # Use Geoffrey Hinton's Scholar ID
    author_id = "JicYPdAAAAAJ"
    data = api_clients.fetch_author_publications(api_keys['serpapi'], author_id)

    articles = data.get('articles', [])
    assert articles and len(articles) > 0, "No publications returned"

def test_serpapi_scholar_citation(api_keys):
    """
    Test SerpAPI Scholar citation fetch via API.
    """
    if not api_keys.get('serpapi'):
        pytest.skip("SerpAPI key not available")

    try:
        # Fetch publications to get a real citation_id
        author_id = "JicYPdAAAAAJ"
        data = api_clients.fetch_author_publications(api_keys['serpapi'], author_id)

        articles = data.get('articles', [])
        assert articles, "No articles to test citation fetch"

        citation_id = articles[0].get('citation_id')
        assert citation_id, "No citation_id found"

        # Test SerpAPI citation function
        fields = api_clients.fetch_scholar_citation_via_serpapi(
            api_keys['serpapi'],
            author_id,
            citation_id
        )

        assert fields and 'title' in fields, "No valid fields returned from SerpAPI citation"

        # Build BibTeX from fields
        bibtex = api_clients.build_bibtex_from_scholar_fields(fields, keyhint="test")
        assert bibtex and '@' in bibtex, "BibTeX building from citation fields failed"

    except Exception as e:
        error_msg = str(e)
        if '429' in error_msg:
            pytest.skip("Rate limited (expected with frequent requests)")
        else:
            raise e

# ===== SINGLE-RESULT SEARCHES =====

def test_crossref_search():
    """
    Test Crossref API search and BibTeX building.
    """
    paper = KNOWN_PAPERS[0]
    item = api_clients.crossref_search(paper['title'], paper['first_author'])

    if item:
        bibtex = api_clients.build_bibtex_from_crossref(item, paper['first_author'])
        parsed = bibtex_utils.parse_bibtex_to_dict(bibtex)

        assert parsed and 'type' in parsed, "BibTeX building failed"
    else:
        pytest.skip("No result (API may be unavailable)")

def test_openalex_search():
    """
    Test OpenAlex API search and BibTeX building.
    """
    paper = API_SPECIFIC_PAPERS['openalex']
    work = api_clients.openalex_search_paper(paper['title'], paper['first_author'])

    if work:
        bibtex = api_clients.build_bibtex_from_openalex(work, paper['first_author'])
        parsed = bibtex_utils.parse_bibtex_to_dict(bibtex)

        assert parsed and 'type' in parsed, "BibTeX building failed"
    else:
        pytest.skip("No result (API may be unavailable)")

# ===== MULTIPLE-CANDIDATE SEARCHES =====

def test_all_multiple_candidate_functions_exist():
    """
    Test that all multiple-candidate wrapper functions exist.
    """
    required_functions = [
        'crossref_search_multiple',
        'openalex_search_multiple',
        's2_search_papers_multiple',
        'pubmed_search_papers_multiple',
        'europepmc_search_papers_multiple',
        'openreview_search_papers_multiple',
    ]

    for func_name in required_functions:
        assert hasattr(api_clients, func_name), f"Function {func_name} not found"
        assert callable(getattr(api_clients, func_name)), f"Function {func_name} is not callable"

def test_crossref_multiple_candidates():
    """
    Test Crossref multiple-candidate search.
    """
    paper = KNOWN_PAPERS[0]
    candidates = api_clients.crossref_search_multiple(
        paper['title'],
        paper['first_author'],
        max_results=5
    )

    assert isinstance(candidates, list), f"Expected list, got {type(candidates).__name__}"

def test_s2_multiple_candidates(api_keys):
    """
    Test Semantic Scholar multiple-candidate search.
    """
    if not api_keys.get('semantic'):
        pytest.skip("Semantic Scholar key not available")

    paper = API_SPECIFIC_PAPERS['semantic_scholar']
    candidates = api_clients.s2_search_papers_multiple(
        paper['title'],
        paper['first_author'],
        api_keys['semantic'],
        max_results=5
    )

    assert isinstance(candidates, list), f"Expected list, got {type(candidates).__name__}"

# ===== EDGE CASES =====

def test_multiple_candidate_empty_inputs():
    """
    Test multiple-candidate searches handle empty inputs.
    """
    # Test empty title
    candidates = api_clients.crossref_search_multiple("", "Ashish Vaswani", max_results=5)
    assert isinstance(candidates, list), "Empty title: did not return list"

    # Test None author
    candidates = api_clients.crossref_search_multiple("Attention Is All You Need", None, max_results=5)
    assert isinstance(candidates, list), "None author: did not return list"

    # Test max_results=0
    candidates = api_clients.crossref_search_multiple("Attention Is All You Need", "Ashish Vaswani", max_results=0)
    assert len(candidates) == 0, f"max_results=0: expected empty list, got {len(candidates)} items"

# ===== API INFRASTRUCTURE =====

def test_api_configs():
    """
    Test API configuration objects.
    """
    configs = ['S2_SEARCH_CONFIG', 'CROSSREF_SEARCH_CONFIG', 'OPENALEX_SEARCH_CONFIG']
    for name in configs:
        assert hasattr(api_configs, name), f"Missing: {name}"
        cfg = getattr(api_configs, name)
        assert isinstance(cfg, api_generics.APISearchConfig), f"{name} wrong type"
        assert cfg.api_name and cfg.base_url, f"{name} incomplete"

def test_api_field_mappings():
    """
    Test API field mapping objects.
    """
    mappings = ['S2_FIELD_MAPPING', 'CROSSREF_FIELD_MAPPING', 'OPENALEX_FIELD_MAPPING']
    for name in mappings:
        assert hasattr(api_configs, name), f"Missing: {name}"
        mapping = getattr(api_configs, name)
        assert isinstance(mapping, api_generics.APIFieldMapping), f"{name} wrong type"
        assert mapping.title_fields and mapping.author_fields, f"{name} incomplete"

def test_doi_validation_functions():
    """
    Test DOI validation utilities.
    """
    assert hasattr(doi_utils, 'validate_doi_candidate'), "Missing validate_doi_candidate"
    assert callable(doi_utils.validate_doi_candidate), "validate_doi_candidate not callable"

    assert hasattr(doi_utils, 'process_validated_doi'), "Missing process_validated_doi"
    assert callable(doi_utils.process_validated_doi), "process_validated_doi not callable"

# ===== INTEGRATION =====

def test_bibtex_building_from_api_responses():
    """
    Test BibTeX building from all API response types.
    """
    paper = KNOWN_PAPERS[0]

    # Test Crossref
    cr_item = api_clients.crossref_search(paper['title'], paper['first_author'])
    if cr_item:
        bibtex = api_clients.build_bibtex_from_crossref(cr_item, paper['first_author'])
        assert bibtex and '@' in bibtex, "Crossref BibTeX building failed"
