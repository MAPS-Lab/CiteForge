import urllib.error
from email.message import Message
from textwrap import dedent
from unittest.mock import patch

from src.clients import search_apis
from src.doi_utils import process_validated_doi, validate_doi_candidate


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
    with (
        patch.object(search_apis, 'fetch_csl_via_doi', return_value=mock_csl),
        patch.object(search_apis, 'fetch_bibtex_via_doi', return_value=mock_bibtex),
        patch.object(search_apis, 'bibtex_from_csl', return_value=mock_bibtex),
    ):
        csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
            doi="10.48550/arXiv.1706.03762",
            baseline_entry=baseline_entry,
            result_id="test"
        )

    # CSL matched, so BibTeX fetch is skipped (Phase 3a optimization)
    assert csl_matched, "CSL format should have matched"
    assert not bibtex_matched, "BibTeX should be skipped when CSL matches"
    assert csl_entry is not None, "CSL entry should be returned"
    assert bibtex_entry is None, "BibTeX entry should be None (skipped)"

def test_validate_doi_candidate_csl_only_matches():
    """
    Check that when CSL-JSON matches the baseline, BibTeX is skipped
    (Phase 3a optimization) even if BibTeX would resolve to a wrong paper.
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
    mock_bibtex_from_csl = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani and Noam Shazeer},
          year = {2017}
        }
    """).strip()
    with (
        patch.object(search_apis, 'fetch_csl_via_doi', return_value=mock_csl),
        patch.object(search_apis, 'fetch_bibtex_via_doi', return_value=mock_bibtex_wrong),
        patch.object(search_apis, 'bibtex_from_csl', return_value=mock_bibtex_from_csl),
    ):
        csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
            doi="10.48550/arXiv.1706.03762",
            baseline_entry=baseline_entry,
            result_id="test"
        )

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
    with (
        patch.object(search_apis, 'fetch_csl_via_doi', return_value=mock_csl_wrong),
        patch.object(search_apis, 'fetch_bibtex_via_doi', return_value=mock_bibtex_wrong),
        patch.object(search_apis, 'bibtex_from_csl', return_value=mock_bibtex_wrong),
    ):
        csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
            doi="10.18653/v1/N19-1423",
            baseline_entry=baseline_entry,
            result_id="test"
        )

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

    with (
        patch.object(search_apis, 'fetch_csl_via_doi', side_effect=urllib.error.URLError("Network error")),
        patch.object(search_apis, 'fetch_bibtex_via_doi', side_effect=urllib.error.HTTPError(
            url='test', code=500, msg='Server Error', hdrs=Message(), fp=None
        )),
    ):
        csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
            doi="10.48550/arXiv.1706.03762",
            baseline_entry=baseline_entry,
            result_id="test"
        )

    assert not csl_matched and not bibtex_matched, "Should handle network errors gracefully"
    assert not csl_entry and not bibtex_entry, "Should not return entries on error"

def test_validate_doi_candidate_bibtex_fallback_when_csl_fails():
    """
    Confirm that BibTeX fallback triggers when CSL validation fails,
    exercising the Phase 3a short-circuit logic in reverse.
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

    # CSL returns wrong paper (won't match baseline)
    mock_csl_wrong = {
        'title': 'A Completely Different Paper Title',
        'author': [{'given': 'John', 'family': 'Doe'}],
        'issued': {'date-parts': [[2020]]}
    }

    # BibTeX returns correct paper (matches baseline)
    mock_bibtex_correct = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani and Noam Shazeer},
          year = {2017}
        }
    """).strip()

    mock_bibtex_from_csl_wrong = dedent("""
        @article{Doe2020,
          title = {A Completely Different Paper Title},
          author = {John Doe},
          year = {2020}
        }
    """).strip()

    with (
        patch.object(search_apis, 'fetch_csl_via_doi', return_value=mock_csl_wrong),
        patch.object(search_apis, 'fetch_bibtex_via_doi', return_value=mock_bibtex_correct),
        patch.object(search_apis, 'bibtex_from_csl', return_value=mock_bibtex_from_csl_wrong),
    ):
        csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
            doi="10.48550/arXiv.1706.03762", baseline_entry=baseline_entry,
            result_id="test"
        )

    # CSL should have failed (wrong paper metadata)
    assert not csl_matched, "CSL should not match (wrong paper)"
    assert csl_entry is None, "CSL entry should be None"
    # BibTeX fallback should have succeeded
    assert bibtex_matched, "BibTeX fallback should match when CSL fails"
    assert bibtex_entry is not None, "BibTeX entry should be returned"

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
    enr_list = []
    flags = {"doi_csl": False, "doi_bibtex": False}

    with (
        patch.object(search_apis, 'fetch_csl_via_doi', return_value=mock_csl),
        patch.object(search_apis, 'fetch_bibtex_via_doi', return_value=mock_bibtex),
        patch.object(search_apis, 'bibtex_from_csl', return_value=mock_bibtex),
    ):
        doi_matched = process_validated_doi(
            doi="10.48550/arXiv.1706.03762", baseline_entry=baseline_entry,
            result_id="test", enr_list=enr_list, flags=flags
        )

    assert doi_matched, "Should return True"
    assert len(enr_list) > 0, "Should populate enr_list"

    # CSL flag should be set; BibTeX skipped (Phase 3a optimization)
    assert flags.get("doi_csl"), "CSL flag should be set"
    assert not flags.get("doi_bibtex"), "BibTeX flag should not be set (skipped when CSL matches)"

    # only CSL entry should appear in enrichment list
    source_names = [source for source, _ in enr_list]
    assert "csl" in source_names, "CSL source should be in enr_list"

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
    enr_list = []
    flags = {"doi_csl": False, "doi_bibtex": False}

    with (
        patch.object(search_apis, 'fetch_csl_via_doi', return_value=mock_csl_wrong),
        patch.object(search_apis, 'fetch_bibtex_via_doi', return_value=mock_bibtex_wrong),
        patch.object(search_apis, 'bibtex_from_csl', return_value=mock_bibtex_wrong),
    ):
        doi_matched = process_validated_doi(
            doi="10.18653/v1/N19-1423", baseline_entry=baseline_entry,
            result_id="test", enr_list=enr_list, flags=flags
        )

    assert not doi_matched, "Should return False"
    assert len(enr_list) == 0, "Should leave enr_list empty"

    # flags should remain False to indicate no enrichment occurred
    assert not flags.get("doi_csl") and not flags.get("doi_bibtex"), "Flags should remain False"
