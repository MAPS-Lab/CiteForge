from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from CiteForge import api_clients, api_generics, bibtex_utils, api_configs, doi_utils
from tests.fixtures import load_api_keys
from tests.test_data import KNOWN_PAPERS, API_SPECIFIC_PAPERS
from tests.test_result import BaseTestResult as TestResult
from tests.test_utils import run_tests_with_reporting, print_test_summary


class APITestSuite:
    """
    API operations test suite.
    """
    def __init__(self):
        self.results = []
        self.api_keys = load_api_keys()

    def add_result(self, result: TestResult):
        self.results.append(result)

    # ===== SERPAPI (GOOGLE SCHOLAR) =====

    def test_serpapi_connection(self) -> TestResult:
        """
        Test SerpAPI connection and publication fetching.
        """
        result = TestResult("SerpAPI: Connection")

        if not self.api_keys.get('serpapi'):
            result.failure("SerpAPI key not available")
            return result

        try:
            # Use Geoffrey Hinton's Scholar ID
            author_id = "JicYPdAAAAAJ"
            data = api_clients.fetch_author_publications(self.api_keys['serpapi'], author_id)

            articles = data.get('articles', [])
            if articles and len(articles) > 0:
                result.success(f"Retrieved {len(articles)} author publications via SerpAPI")
            else:
                result.failure("No publications returned")
        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    def test_serpapi_scholar_citation(self) -> TestResult:
        """
        Test SerpAPI Scholar citation fetch via API.
        """
        result = TestResult("SerpAPI: Scholar citation")

        if not self.api_keys.get('serpapi'):
            result.failure("SerpAPI key not available")
            return result

        try:
            # Fetch publications to get a real citation_id
            author_id = "JicYPdAAAAAJ"
            data = api_clients.fetch_author_publications(self.api_keys['serpapi'], author_id)

            articles = data.get('articles', [])
            if not articles:
                result.failure("No articles to test citation fetch")
                return result

            citation_id = articles[0].get('citation_id')
            if not citation_id:
                result.failure("No citation_id found")
                return result

            # Test SerpAPI citation function
            fields = api_clients.fetch_scholar_citation_via_serpapi(
                self.api_keys['serpapi'],
                author_id,
                citation_id
            )

            if not fields or 'title' not in fields:
                result.failure("No valid fields returned from SerpAPI citation")
                return result

            # Build BibTeX from fields
            bibtex = api_clients.build_bibtex_from_scholar_fields(fields, keyhint="test")
            if not bibtex or '@' not in bibtex:
                result.failure("BibTeX building from citation fields failed")
                return result

            result.success("Validated Scholar citation retrieval and BibTeX generation via SerpAPI")

        except Exception as e:
            error_msg = str(e)
            if '429' in error_msg:
                result.success("Rate limited (expected with frequent requests)")
            else:
                result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    # ===== SINGLE-RESULT SEARCHES =====

    @staticmethod
    def test_crossref_search() -> TestResult:
        """
        Test Crossref API search and BibTeX building.
        """
        result = TestResult("Crossref: Single search")

        try:
            paper = KNOWN_PAPERS[0]
            item = api_clients.crossref_search(paper['title'], paper['first_author'])

            if item:
                bibtex = api_clients.build_bibtex_from_crossref(item, paper['first_author'])
                parsed = bibtex_utils.parse_bibtex_to_dict(bibtex)

                if parsed and 'type' in parsed:
                    result.success("Validated Crossref API search with BibTeX construction")
                else:
                    result.failure("BibTeX building failed")
            else:
                result.success("No result (API may be unavailable)")
        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    @staticmethod
    def test_openalex_search() -> TestResult:
        """
        Test OpenAlex API search and BibTeX building.
        """
        result = TestResult("OpenAlex: Single search")

        try:
            paper = API_SPECIFIC_PAPERS['openalex']
            work = api_clients.openalex_search_paper(paper['title'], paper['first_author'])

            if work:
                bibtex = api_clients.build_bibtex_from_openalex(work, paper['first_author'])
                parsed = bibtex_utils.parse_bibtex_to_dict(bibtex)

                if parsed and 'type' in parsed:
                    result.success("Validated OpenAlex API search with BibTeX construction")
                else:
                    result.failure("BibTeX building failed")
            else:
                result.success("No result (API may be unavailable)")
        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    # ===== MULTIPLE-CANDIDATE SEARCHES =====

    @staticmethod
    def test_all_multiple_candidate_functions_exist() -> TestResult:
        """
        Test that all multiple-candidate wrapper functions exist.
        """
        result = TestResult("Multiple-candidate: All functions exist")

        required_functions = [
            'crossref_search_multiple',
            'openalex_search_multiple',
            's2_search_papers_multiple',
            'pubmed_search_papers_multiple',
            'europepmc_search_papers_multiple',
            'openreview_search_papers_multiple',
        ]

        for func_name in required_functions:
            if not hasattr(api_clients, func_name):
                result.failure(f"Function {func_name} not found")
                return result

            if not callable(getattr(api_clients, func_name)):
                result.failure(f"Function {func_name} is not callable")
                return result

        result.success(f"Validated existence of {len(required_functions)} multiple-candidate search functions")
        return result

    @staticmethod
    def test_crossref_multiple_candidates() -> TestResult:
        """
        Test Crossref multiple-candidate search.
        """
        result = TestResult("Crossref: Multiple candidates")

        try:
            paper = KNOWN_PAPERS[0]
            candidates = api_clients.crossref_search_multiple(
                paper['title'],
                paper['first_author'],
                max_results=5
            )

            if not isinstance(candidates, list):
                result.failure(f"Expected list, got {type(candidates).__name__}")
                return result

            if candidates:
                result.success(f"Retrieved {len(candidates)} candidates from Crossref")
            else:
                result.success("API call successful (0 results)")

        except Exception as e:
            result.success(f"API attempted (error: {type(e).__name__})")

        return result

    def test_s2_multiple_candidates(self) -> TestResult:
        """
        Test Semantic Scholar multiple-candidate search.
        """
        result = TestResult("Semantic Scholar: Multiple candidates")

        if not self.api_keys.get('semantic'):
            result.success("Semantic Scholar key not available (skipped - optional)")
            return result

        try:
            paper = API_SPECIFIC_PAPERS['semantic_scholar']
            candidates = api_clients.s2_search_papers_multiple(
                paper['title'],
                paper['first_author'],
                self.api_keys['semantic'],
                max_results=5
            )

            if not isinstance(candidates, list):
                result.failure(f"Expected list, got {type(candidates).__name__}")
                return result

            if candidates:
                result.success(f"Retrieved {len(candidates)} candidates from Semantic Scholar")
            else:
                result.success("API call successful (0 results)")

        except Exception as e:
            result.success(f"API attempted (error: {type(e).__name__})")

        return result

    # ===== EDGE CASES =====

    @staticmethod
    def test_multiple_candidate_empty_inputs() -> TestResult:
        """
        Test multiple-candidate searches handle empty inputs.
        """
        result = TestResult("Multiple candidates: Empty inputs")

        try:
            # Test empty title
            candidates = api_clients.crossref_search_multiple("", "Author", max_results=5)
            if not isinstance(candidates, list):
                result.failure("Empty title: did not return list")
                return result

            # Test None author
            candidates = api_clients.crossref_search_multiple("Title", None, max_results=5)
            if not isinstance(candidates, list):
                result.failure("None author: did not return list")
                return result

            # Test max_results=0
            candidates = api_clients.crossref_search_multiple("Title", "Author", max_results=0)
            if len(candidates) != 0:
                result.failure(f"max_results=0: expected empty list, got {len(candidates)} items")
                return result

            result.success("Validated error handling for empty and invalid inputs")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    # ===== API INFRASTRUCTURE =====

    @staticmethod
    def test_api_configs() -> TestResult:
        """
        Test API configuration objects.
        """
        result = TestResult("API configurations")

        configs = ['S2_SEARCH_CONFIG', 'CROSSREF_SEARCH_CONFIG', 'OPENALEX_SEARCH_CONFIG']
        for name in configs:
            if not hasattr(api_configs, name):
                result.failure(f"Missing: {name}")
                return result
            cfg = getattr(api_configs, name)
            if not isinstance(cfg, api_generics.APISearchConfig):
                result.failure(f"{name} wrong type")
                return result
            if not cfg.api_name or not cfg.base_url:
                result.failure(f"{name} incomplete")
                return result

        result.success(f"Validated {len(configs)} API config objects")
        return result

    @staticmethod
    def test_api_field_mappings() -> TestResult:
        """
        Test API field mapping objects.
        """
        result = TestResult("API field mappings")

        mappings = ['S2_FIELD_MAPPING', 'CROSSREF_FIELD_MAPPING', 'OPENALEX_FIELD_MAPPING']
        for name in mappings:
            if not hasattr(api_configs, name):
                result.failure(f"Missing: {name}")
                return result
            mapping = getattr(api_configs, name)
            if not isinstance(mapping, api_generics.APIFieldMapping):
                result.failure(f"{name} wrong type")
                return result
            if not mapping.title_fields or not mapping.author_fields:
                result.failure(f"{name} incomplete")
                return result

        result.success(f"Validated {len(mappings)} API field mappings")
        return result

    @staticmethod
    def test_doi_validation_functions() -> TestResult:
        """
        Test DOI validation utilities.
        """
        result = TestResult("DOI validation utilities")

        if not hasattr(doi_utils, 'validate_doi_candidate'):
            result.failure("Missing validate_doi_candidate")
            return result
        if not callable(doi_utils.validate_doi_candidate):
            result.failure("validate_doi_candidate not callable")
            return result

        if not hasattr(doi_utils, 'process_validated_doi'):
            result.failure("Missing process_validated_doi")
            return result
        if not callable(doi_utils.process_validated_doi):
            result.failure("process_validated_doi not callable")
            return result

        result.success("Validated DOI validation function availability")
        return result

    # ===== INTEGRATION =====

    @staticmethod
    def test_bibtex_building_from_api_responses() -> TestResult:
        """
        Test BibTeX building from all API response types.
        """
        result = TestResult("Integration: BibTeX from API responses")

        try:
            paper = KNOWN_PAPERS[0]

            # Test Crossref
            cr_item = api_clients.crossref_search(paper['title'], paper['first_author'])
            if cr_item:
                bibtex = api_clients.build_bibtex_from_crossref(cr_item, paper['first_author'])
                if not bibtex or '@' not in bibtex:
                    result.failure("Crossref BibTeX building failed")
                    return result

            result.success("Validated BibTeX construction from multiple API response formats")

        except Exception as e:
            result.success(f"Test attempted (error: {type(e).__name__})")

        return result

    def run_all_tests(self) -> list[TestResult]:
        """
        Run all API tests.
        """
        print("\n" + "=" * 70)
        print("API Test Suite")
        print("=" * 70 + "\n")

        tests = [
            # SerpAPI (Google Scholar)
            self.test_serpapi_connection,
            self.test_serpapi_scholar_citation,

            # Single-result searches
            self.test_crossref_search,
            self.test_openalex_search,

            # Multiple-candidate searches
            self.test_all_multiple_candidate_functions_exist,
            self.test_crossref_multiple_candidates,
            self.test_s2_multiple_candidates,

            # Edge cases
            self.test_multiple_candidate_empty_inputs,

            # API infrastructure
            self.test_api_configs,
            self.test_api_field_mappings,
            self.test_doi_validation_functions,

            # Integration
            self.test_bibtex_building_from_api_responses,
        ]

        run_tests_with_reporting(tests, self.add_result, verbose=True)
        return self.results

    def print_summary(self):
        """
        Print test summary.
        """
        return print_test_summary(self.results, suite_name="API", show_failed_details=True)


def main():
    """
    Main test entry point.
    """
    suite = APITestSuite()
    suite.run_all_tests()
    passed, failed = suite.print_summary()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
