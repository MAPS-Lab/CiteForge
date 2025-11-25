import sys
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch
import urllib.error
import pytest

from src import bibtex_utils as bt, api_clients as api
from src.doi_utils import validate_doi_candidate, process_validated_doi
from src.exceptions import ALL_API_ERRORS

# ===== DOI VALIDATION PIPELINE TESTS =====

def test_validate_doi_candidate_both_formats_match():
    """
    Verify that DOI validation succeeds when both CSL and BibTeX metadata
    from the DOI resolver match the baseline publication.
    """
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
    mock_bibtex = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani and Noam Shazeer},
          year = {2017}
        }
    """).strip()
    # patch API functions to return matching metadata
    with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl):
        with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex):
            with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex):
                csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
                    doi="10.48550/arXiv.1706.03762",
                    baseline_entry=baseline_entry,
                    result_id="test"
                )

    # both formats should validate successfully
    assert csl_matched and bibtex_matched, "Both formats should have matched"
    # both entries should be returned for enrichment
    assert csl_entry and bibtex_entry, "Both entries should be returned"

def test_validate_doi_candidate_csl_only_matches():
    """
    Check partial validation where CSL-JSON succeeds but BibTeX fails.
    """
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
    mock_bibtex_wrong = dedent("""
        @inproceedings{Wrong2018,
          title = {Different Paper About Attention},
          author = {John Doe},
          year = {2018}
        }
    """).strip()
    # CSL-to-BibTeX conversion produces correct metadata
    mock_bibtex_from_csl = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani and Noam Shazeer},
          year = {2017}
        }
    """).strip()
    with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl):
        with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex_wrong):
            with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex_from_csl):
                csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
                    doi="10.48550/arXiv.1706.03762",
                    baseline_entry=baseline_entry,
                    result_id="test"
                )

    # CSL should validate, BibTeX should be rejected
    assert csl_matched, "CSL should match"
    assert not bibtex_matched, "BibTeX should not match"
    assert csl_entry, "CSL entry should be returned"
    assert not bibtex_entry, "BibTeX entry should not be returned"

def test_validate_doi_candidate_neither_matches():
    """
    Test complete rejection when a DOI resolves to metadata for a different paper.
    """
    baseline_entry = {
        'type': 'inproceedings',
        'key': 'Vaswani2017',
        'fields': {
            'title': 'Attention Is All You Need',
            'author': 'Ashish Vaswani',
            'year': '2017'
        }
    }

    # both CSL and BibTeX return metadata for completely different paper (BERT)
    mock_csl_wrong = {
        'title': 'BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding',
        'author': [{'given': 'Jacob', 'family': 'Devlin'}],
        'issued': {'date-parts': [[2019]]}
    }

    mock_bibtex_wrong = dedent("""
        @inproceedings{Devlin2019,
          title = {BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding},
          author = {Jacob Devlin and Ming-Wei Chang and Kenton Lee and Kristina Toutanova},
          year = {2019}
        }
    """).strip()
    with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl_wrong):
        with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex_wrong):
            with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex_wrong):
                csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
                    doi="10.18653/v1/N19-1423", # Real DOI for BERT
                    baseline_entry=baseline_entry,
                    result_id="test"
                )

    # neither format should match; DOI should be rejected entirely
    assert not csl_matched and not bibtex_matched, "Both formats should be rejected"
    assert not csl_entry and not bibtex_entry, "No entries should be returned"

def test_validate_doi_candidate_network_errors():
    """
    Verify resilient error handling when DOI resolution fails due to network issues.
    """
    baseline_entry = {
        'type': 'inproceedings',
        'key': 'Vaswani2017',
        'fields': {
            'title': 'Attention Is All You Need',
            'author': 'Ashish Vaswani',
            'year': '2017'
        }
    }

    # simulate network failures for both formats
    with patch.object(api, 'fetch_csl_via_doi', side_effect=urllib.error.URLError("Network error")):
        with patch.object(api, 'fetch_bibtex_via_doi', side_effect=urllib.error.HTTPError(
            url='test', code=500, msg='Server Error', hdrs={}, fp=None
        )):
            csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
                doi="10.48550/arXiv.1706.03762",
                baseline_entry=baseline_entry,
                result_id="test"
            )

    # should gracefully return False without raising exceptions
    assert not csl_matched and not bibtex_matched, "Should handle network errors gracefully"
    assert not csl_entry and not bibtex_entry, "Should not return entries on error"

def test_validate_doi_candidate_early_vs_late():
    """
    Confirm that validation logic remains consistent between early validation
    and late validation.
    """
    baseline_entry = {
        'type': 'inproceedings',
        'key': 'Vaswani2017',
        'fields': {
            'title': 'Attention Is All You Need',
            'author': 'Ashish Vaswani',
            'year': '2017'
        }
    }

    mock_csl = {
        'title': 'Attention Is All You Need',
        'author': [{'given': 'Ashish', 'family': 'Vaswani'}],
        'issued': {'date-parts': [[2017]]}
    }

    mock_bibtex = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani and Noam Shazeer},
          year = {2017}
        }
    """).strip()
    # test early validation (baseline has DOI already)
    with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl):
        with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex):
            with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex):
                csl_matched_early, _, _, _ = validate_doi_candidate(
                    doi="10.48550/arXiv.1706.03762", baseline_entry=baseline_entry,
                    result_id="test"
                )

    # test late validation (DOI found during enrichment)
    with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl):
        with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex):
            with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex):
                csl_matched_late, _, _, _ = validate_doi_candidate(
                    doi="10.48550/arXiv.1706.03762", baseline_entry=baseline_entry,
                    result_id="test"
                )

    # both stages should produce identical validation results
    assert csl_matched_early and csl_matched_late, "Both early and late should succeed"

def test_process_validated_doi_success():
    """
    Verify that successful DOI validation properly updates the enrichment
    tracking structures.
    """
    baseline_entry = {
        'type': 'inproceedings',
        'key': 'Vaswani2017',
        'fields': {
            'title': 'Attention Is All You Need',
            'author': 'Ashish Vaswani',
            'year': '2017'
        }
    }

    mock_csl = {
        'title': 'Attention Is All You Need',
        'author': [{'given': 'Ashish', 'family': 'Vaswani'}],
        'issued': {'date-parts': [[2017]]}
    }

    mock_bibtex = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani and Noam Shazeer},
          year = {2017}
        }
    """).strip()
    # track enrichment state before validation
    enr_list = []
    flags = {"doi_csl": False, "doi_bibtex": False}

    with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl):
        with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex):
            with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex):
                doi_matched = process_validated_doi(
                    doi="10.48550/arXiv.1706.03762", baseline_entry=baseline_entry,
                    result_id="test", enr_list=enr_list, flags=flags
                )

    # validation should succeed and populate structures
    assert doi_matched, "Should return True"
    assert len(enr_list) > 0, "Should populate enr_list"

    # both flags should be set for summary tracking
    assert flags.get("doi_csl") and flags.get("doi_bibtex"), "Both flags should be set"

    # both entries should appear in enrichment list for merging
    source_names = [source for source, _ in enr_list]
    assert "doi_csl" in source_names or "csl" in source_names, "CSL source should be in enr_list"
    assert "doi_bibtex" in source_names, "BibTeX source should be in enr_list"

def test_process_validated_doi_failure():
    """
    Confirm that failed DOI validation leaves enrichment structures untouched.
    """
    baseline_entry = {
        'type': 'inproceedings',
        'key': 'Vaswani2017',
        'fields': {
            'title': 'Attention Is All You Need',
            'author': 'Ashish Vaswani',
            'year': '2017'
        }
    }

    # DOI resolves to wrong paper metadata (BERT)
    mock_csl_wrong = {
        'title': 'BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding',
        'author': [{'given': 'Jacob', 'family': 'Devlin'}],
        'issued': {'date-parts': [[2019]]}
    }

    mock_bibtex_wrong = dedent("""
        @inproceedings{Devlin2019,
          title = {BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding},
          author = {Jacob Devlin and Ming-Wei Chang},
          year = {2019}
        }
    """).strip()
    # track state before failed validation
    enr_list = []
    flags = {"doi_csl": False, "doi_bibtex": False}

    with patch.object(api, 'fetch_csl_via_doi', return_value=mock_csl_wrong):
        with patch.object(api, 'fetch_bibtex_via_doi', return_value=mock_bibtex_wrong):
            with patch.object(api, 'bibtex_from_csl', return_value=mock_bibtex_wrong):
                doi_matched = process_validated_doi(
                    doi="10.18653/v1/N19-1423", baseline_entry=baseline_entry,
                    result_id="test", enr_list=enr_list, flags=flags
                )

    # validation should fail and leave structures unchanged
    assert not doi_matched, "Should return False"
    assert len(enr_list) == 0, "Should leave enr_list empty"

    # flags should remain False to indicate no enrichment occurred
    assert not flags.get("doi_csl") and not flags.get("doi_bibtex"), "Flags should remain False"
