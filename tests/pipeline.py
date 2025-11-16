from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).parent.parent))

from CiteForge import bibtex_utils as bt, api_clients as api
from CiteForge.doi_utils import validate_doi_candidate, process_validated_doi
from CiteForge.exceptions import ALL_API_ERRORS
from tests.test_result import BaseTestResult as TestResult
from tests.test_utils import run_tests_with_reporting, print_test_summary


class PipelineTestSuite:
    """
    Test suite for critical pipeline components including DOI validation,
    multi-candidate selection, and smart caching mechanisms that ensure data
    integrity throughout the enrichment process.
    """
    def __init__(self):
        self.results = []

    def add_result(self, result: TestResult):
        self.results.append(result)

    # ===== DOI VALIDATION PIPELINE TESTS =====

    @staticmethod
    def test_validate_doi_candidate_both_formats_match() -> TestResult:
        """
        Verify that DOI validation succeeds when both CSL and BibTeX metadata
        from the DOI resolver match the baseline publication, ensuring we accept
        DOIs only when they unambiguously identify the correct paper.
        """
        result = TestResult("DOI validation: both formats match")

        # baseline entry representing the known paper we're validating against
        baseline_entry = {
            'type': 'inproceedings',
            'key': 'Vaswani2017',
            'fields': {
                'title': 'Attention Is All You Need',
                'author': 'Ashish Vaswani and Noam Shazeer and Niki Parmar',
                'year': '2017'
            }
        }

        # mock CSL-JSON response that matches baseline metadata
        mock_csl = {
            'type': 'paper-conference',
            'title': 'Attention Is All You Need',
            'author': [
                {'given': 'Ashish', 'family': 'Vaswani'},
                {'given': 'Noam', 'family': 'Shazeer'}
            ],
            'issued': {'date-parts': [[2017]]}
        }

        # mock BibTeX response that also matches baseline
        mock_bibtex = """@inproceedings{Vaswani2017,
  title = {Attention Is All You Need},
  author = {Ashish Vaswani and Noam Shazeer},
  year = {2017}
}"""
        # patch API functions to return matching metadata
        with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl):
            with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex):
                with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex):
                    csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
                        doi="10.48550/arXiv.1706.03762",
                        baseline_entry=baseline_entry,
                        result_id="test",
                        is_early=False
                    )

        # both formats should validate successfully
        if not csl_matched or not bibtex_matched:
            result.failure("validate_doi_candidate", "Both formats should have matched")
            return result
        # both entries should be returned for enrichment
        if not csl_entry or not bibtex_entry:
            result.failure("validate_doi_candidate", "Both entries should be returned")
            return result

        result.success("Both CSL and BibTeX formats validated successfully")
        return result

    @staticmethod
    def test_validate_doi_candidate_csl_only_matches() -> TestResult:
        """
        Check partial validation where CSL-JSON succeeds but BibTeX fails,
        ensuring the pipeline accepts the DOI when at least one format validates
        while properly rejecting the mismatched format.
        """
        result = TestResult("DOI validation: CSL only")

        baseline_entry = {
            'type': 'inproceedings',
            'key': 'Vaswani2017',
            'fields': {
                'title': 'Attention Is All You Need',
                'author': 'Ashish Vaswani and Noam Shazeer',
                'year': '2017'
            }
        }

        # CSL matches baseline
        mock_csl = {
            'title': 'Attention Is All You Need',
            'author': [{'given': 'Ashish', 'family': 'Vaswani'}],
            'issued': {'date-parts': [[2017]]}
        }

        # BibTeX returns wrong paper metadata
        mock_bibtex_wrong = """@inproceedings{Wrong2018,
  title = {Different Paper About Attention},
  author = {John Doe},
  year = {2018}
}"""
        # CSL-to-BibTeX conversion produces correct metadata
        mock_bibtex_from_csl = """@inproceedings{Vaswani2017,
  title = {Attention Is All You Need},
  author = {Ashish Vaswani and Noam Shazeer},
  year = {2017}
}"""
        with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl):
            with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex_wrong):
                with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex_from_csl):
                    csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
                        doi="10.48550/arXiv.1706.03762",
                        baseline_entry=baseline_entry,
                        result_id="test",
                        is_early=False
                    )

        # CSL should validate, BibTeX should be rejected
        if not csl_matched or bibtex_matched or not csl_entry or bibtex_entry:
            result.failure("validate_doi_candidate", "CSL should match, BibTeX should not")
            return result

        result.success("CSL validated, BibTeX rejected as expected")
        return result

    @staticmethod
    def test_validate_doi_candidate_neither_matches() -> TestResult:
        """
        Test complete rejection when a DOI resolves to metadata for a different
        paper, preventing misattribution by ensuring strict title/author/year
        validation before accepting any DOI.
        """
        result = TestResult("DOI validation: neither matches")

        baseline_entry = {
            'type': 'inproceedings',
            'key': 'Vaswani2017',
            'fields': {
                'title': 'Attention Is All You Need',
                'author': 'Ashish Vaswani',
                'year': '2017'
            }
        }

        # both CSL and BibTeX return metadata for completely different paper
        mock_csl_wrong = {
            'title': 'Different Paper',
            'author': [{'given': 'Jane', 'family': 'Doe'}],
            'issued': {'date-parts': [[2020]]}
        }

        mock_bibtex_wrong = """@article{Doe2020,
  title = {Different Paper},
  author = {Jane Doe},
  year = {2020}
}"""
        with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl_wrong):
            with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex_wrong):
                with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex_wrong):
                    csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
                        doi="10.1234/wrong-paper",
                        baseline_entry=baseline_entry,
                        result_id="test",
                        is_early=False
                    )

        # neither format should match; DOI should be rejected entirely
        if csl_matched or bibtex_matched or csl_entry or bibtex_entry:
            result.failure("validate_doi_candidate", "Both formats should be rejected")
            return result

        result.success("Both formats correctly rejected (wrong paper)")
        return result

    @staticmethod
    def test_validate_doi_candidate_network_errors() -> TestResult:
        """
        Verify resilient error handling when DOI resolution fails due to network
        issues, ensuring the pipeline continues processing without crashing and
        properly logs the failure without corrupting enrichment data.
        """
        result = TestResult("DOI validation: network errors")

        baseline_entry = {
            'type': 'inproceedings',
            'key': 'Test2020',
            'fields': {
                'title': 'Test Paper',
                'author': 'Test Author',
                'year': '2020'
            }
        }

        # simulate network failures for both formats
        with patch.object(api, 'fetch_csl_via_doi', side_effect=urllib.error.URLError("Network error")):
            with patch.object(api, 'fetch_bibtex_via_doi', side_effect=urllib.error.HTTPError(
                url='test', code=500, msg='Server Error', hdrs={}, fp=None
            )):
                csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
                    doi="10.1234/test",
                    baseline_entry=baseline_entry,
                    result_id="test",
                    is_early=False
                )

        # should gracefully return False without raising exceptions
        if csl_matched or bibtex_matched or csl_entry or bibtex_entry:
            result.failure("validate_doi_candidate", "Should handle network errors gracefully")
            return result

        result.success("Network errors handled gracefully")
        return result

    @staticmethod
    def test_validate_doi_candidate_early_vs_late() -> TestResult:
        """
        Confirm that validation logic remains consistent between early validation
        (baseline already has DOI) and late validation (DOI discovered during
        enrichment), with only logging messages differing between the two stages.
        """
        result = TestResult("DOI validation: early vs late")

        baseline_entry = {
            'type': 'article',
            'key': 'Test2020',
            'fields': {
                'title': 'Test Paper',
                'author': 'Test Author',
                'year': '2020'
            }
        }

        mock_csl = {
            'title': 'Test Paper',
            'author': [{'given': 'Test', 'family': 'Author'}],
            'issued': {'date-parts': [[2020]]}
        }

        mock_bibtex = """@article{Test2020,
  title = {Test Paper},
  author = {Test Author},
  year = {2020}
}"""
        # test early validation (baseline has DOI already)
        with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl):
            with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex):
                with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex):
                    csl_matched_early, _, _, _ = validate_doi_candidate(
                        doi="10.1234/test", baseline_entry=baseline_entry,
                        result_id="test", is_early=True
                    )

        # test late validation (DOI found during enrichment)
        with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl):
            with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex):
                with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex):
                    csl_matched_late, _, _, _ = validate_doi_candidate(
                        doi="10.1234/test", baseline_entry=baseline_entry,
                        result_id="test", is_early=False
                    )

        # both stages should produce identical validation results
        if not csl_matched_early or not csl_matched_late:
            result.failure("validate_doi_candidate", "Both early and late should succeed")
            return result

        result.success("Early and late validation both work correctly")
        return result

    @staticmethod
    def test_process_validated_doi_success() -> TestResult:
        """
        Verify that successful DOI validation properly updates the enrichment
        tracking structures, adding validated entries to the merge list and
        setting appropriate flags for summary CSV reporting.
        """
        result = TestResult("DOI processing: enrichment update")

        baseline_entry = {
            'type': 'article',
            'key': 'Test2020',
            'fields': {
                'title': 'Test Paper',
                'author': 'Test Author',
                'year': '2020'
            }
        }

        mock_csl = {
            'title': 'Test Paper',
            'author': [{'given': 'Test', 'family': 'Author'}],
            'issued': {'date-parts': [[2020]]}
        }

        mock_bibtex = """@article{Test2020,
  title = {Test Paper},
  author = {Test Author},
  year = {2020}
}"""
        # track enrichment state before validation
        enr_list = []
        flags = {"doi_csl": False, "doi_bibtex": False}

        with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl):
            with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex):
                with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex):
                    doi_matched = process_validated_doi(
                        doi="10.1234/test", baseline_entry=baseline_entry,
                        result_id="test", enr_list=enr_list, flags=flags, is_early=False
                    )

        # validation should succeed and populate structures
        if not doi_matched or len(enr_list) == 0:
            result.failure("process_validated_doi", "Should return True and populate enr_list")
            return result

        # both flags should be set for summary tracking
        if not flags.get("doi_csl") or not flags.get("doi_bibtex"):
            result.failure("process_validated_doi", "Both flags should be set")
            return result

        # both entries should appear in enrichment list for merging
        source_names = [source for source, _ in enr_list]
        if "csl" not in source_names or "doi_bibtex" not in source_names:
            result.failure("process_validated_doi", "Both sources should be in enr_list")
            return result

        result.success("Enrichment structures updated correctly (2 entries, 2 flags)")
        return result

    @staticmethod
    def test_process_validated_doi_failure() -> TestResult:
        """
        Confirm that failed DOI validation leaves enrichment structures untouched,
        preventing misattribution by ensuring rejected DOIs do not pollute the
        enrichment list or trigger false success flags in the summary CSV.
        """
        result = TestResult("DOI processing: validation failure")

        baseline_entry = {
            'type': 'article',
            'key': 'Test2020',
            'fields': {
                'title': 'Test Paper',
                'author': 'Test Author',
                'year': '2020'
            }
        }

        # DOI resolves to wrong paper metadata
        mock_csl_wrong = {
            'title': 'Wrong Paper',
            'author': [{'given': 'Wrong', 'family': 'Author'}],
            'issued': {'date-parts': [[2019]]}
        }

        mock_bibtex_wrong = """@article{Wrong2019,
  title = {Wrong Paper},
  author = {Wrong Author},
  year = {2019}
}"""
        # track state before failed validation
        enr_list = []
        flags = {"doi_csl": False, "doi_bibtex": False}

        with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl_wrong):
            with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex_wrong):
                with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex_wrong):
                    doi_matched = process_validated_doi(
                        doi="10.1234/wrong", baseline_entry=baseline_entry,
                        result_id="test", enr_list=enr_list, flags=flags, is_early=False
                    )

        # validation should fail and leave structures unchanged
        if doi_matched or len(enr_list) != 0:
            result.failure("process_validated_doi", "Should return False and leave enr_list empty")
            return result

        # flags should remain False to indicate no enrichment occurred
        if flags.get("doi_csl") or flags.get("doi_bibtex"):
            result.failure("process_validated_doi", "Flags should remain False")
            return result

        result.success("Failed validation correctly leaves structures unchanged")
        return result

    def run_all_tests(self):
        """
        Run all pipeline tests.
        """
        tests = [
            # DOI Validation Pipeline Tests
            self.test_validate_doi_candidate_both_formats_match,
            self.test_validate_doi_candidate_csl_only_matches,
            self.test_validate_doi_candidate_neither_matches,
            self.test_validate_doi_candidate_network_errors,
            self.test_validate_doi_candidate_early_vs_late,
            self.test_process_validated_doi_success,
            self.test_process_validated_doi_failure,
        ]

        run_tests_with_reporting(tests, self.add_result, verbose=True)
        return self.results

    def print_summary(self):
        """
        Print test summary.
        """
        return print_test_summary(self.results, suite_name="Pipeline", show_failed_details=True)


def main():
    """
    Run all pipeline tests.
    """
    suite = PipelineTestSuite()
    suite.run_all_tests()
    return suite.print_summary()


if __name__ == "__main__":
    import sys
    sys.exit(main())
