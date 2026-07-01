from __future__ import annotations

import contextlib
import urllib.error
from email.message import Message
from textwrap import dedent
from typing import Any
from unittest.mock import patch

from src.clients import search_apis
from src.doi_utils import process_validated_doi, validate_doi_candidate

# Shared baseline entries reused across DOI validation tests
_BASELINE_FULL: dict[str, Any] = {
    "type": "inproceedings",
    "key": "Vaswani2017",
    "fields": {
        "title": "Attention Is All You Need",
        "author": "Ashish Vaswani and Noam Shazeer and Niki Parmar",
        "year": "2017",
    },
}

_BASELINE_MINIMAL: dict[str, Any] = {
    "type": "inproceedings",
    "key": "Vaswani2017",
    "fields": {
        "title": "Attention Is All You Need",
        "author": "Ashish Vaswani",
        "year": "2017",
    },
}

_MATCHING_CSL: dict[str, Any] = {
    "title": "Attention Is All You Need",
    "author": [{"given": "Ashish", "family": "Vaswani"}],
    "issued": {"date-parts": [[2017]]},
}

_MATCHING_BIBTEX = dedent("""\
    @inproceedings{Vaswani2017,
      title = {Attention Is All You Need},
      author = {Ashish Vaswani and Noam Shazeer},
      year = {2017}
    }""")

_WRONG_CSL_BERT: dict[str, Any] = {
    "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
    "author": [{"given": "Jacob", "family": "Devlin"}],
    "issued": {"date-parts": [[2019]]},
}

_WRONG_BIBTEX_BERT = dedent("""\
    @inproceedings{Devlin2019,
      title = {BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding},
      author = {Jacob Devlin and Ming-Wei Chang and Kenton Lee and Kristina Toutanova},
      year = {2019}
    }""")

_ARXIV_DOI = "10.48550/arXiv.1706.03762"
_BERT_DOI = "10.18653/v1/N19-1423"


def _patch_doi_resolvers(
    *,
    csl: Any = None,
    bibtex: Any = None,
    bibtex_from_csl: Any = None,
    csl_side_effect: Any = None,
    bibtex_side_effect: Any = None,
) -> contextlib.ExitStack:
    """Patch DOI resolver functions with the given return values or side effects."""
    csl_kwargs: dict[str, Any] = {"side_effect": csl_side_effect} if csl_side_effect else {"return_value": csl}
    bib_kwargs: dict[str, Any] = {"side_effect": bibtex_side_effect} if bibtex_side_effect else {"return_value": bibtex}
    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(search_apis, "fetch_csl_via_doi", **csl_kwargs))
    stack.enter_context(patch.object(search_apis, "fetch_bibtex_via_doi", **bib_kwargs))
    stack.enter_context(patch.object(search_apis, "bibtex_from_csl", return_value=bibtex_from_csl))
    return stack


def test_validate_doi_candidate_both_formats_match() -> None:
    """
    Verify that DOI validation succeeds when both CSL and BibTeX metadata
    from the DOI resolver match the baseline publication.
    """
    mock_csl = {
        "type": "paper-conference",
        "title": "Attention Is All You Need",
        "author": [
            {"given": "Ashish", "family": "Vaswani"},
            {"given": "Noam", "family": "Shazeer"},
        ],
        "issued": {"date-parts": [[2017]]},
    }

    with _patch_doi_resolvers(
        csl=mock_csl,
        bibtex=_MATCHING_BIBTEX,
        bibtex_from_csl=_MATCHING_BIBTEX,
    ):
        csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
            doi=_ARXIV_DOI,
            baseline_entry=_BASELINE_FULL,
            result_id="test",
        )

    # CSL matched, so BibTeX fetch is skipped (Phase 3a optimization)
    assert csl_matched, "CSL format should have matched"
    assert not bibtex_matched, "BibTeX should be skipped when CSL matches"
    assert csl_entry is not None, "CSL entry should be returned"
    assert bibtex_entry is None, "BibTeX entry should be None (skipped)"


def test_validate_doi_candidate_csl_only_matches() -> None:
    """
    Check that when CSL-JSON matches the baseline, BibTeX is skipped
    (Phase 3a optimization) even if BibTeX would resolve to a wrong paper.
    """
    baseline_entry: dict[str, Any] = {
        "type": "inproceedings",
        "key": "Vaswani2017",
        "fields": {
            "title": "Attention Is All You Need",
            "author": "Ashish Vaswani and Noam Shazeer",
            "year": "2017",
        },
    }

    mock_bibtex_wrong = dedent("""\
        @inproceedings{Wrong2018,
          title = {Different Paper About Attention},
          author = {John Doe},
          year = {2018}
        }""")

    with _patch_doi_resolvers(
        csl=_MATCHING_CSL,
        bibtex=mock_bibtex_wrong,
        bibtex_from_csl=_MATCHING_BIBTEX,
    ):
        csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
            doi=_ARXIV_DOI,
            baseline_entry=baseline_entry,
            result_id="test",
        )

    assert csl_matched, "CSL should match"
    assert not bibtex_matched, "BibTeX should not match"
    assert csl_entry is not None, "CSL entry should be returned"
    assert bibtex_entry is None, "BibTeX entry should not be returned"


def test_validate_doi_candidate_neither_matches() -> None:
    """
    Test complete rejection when a DOI resolves to metadata for a different paper.
    """
    with _patch_doi_resolvers(
        csl=_WRONG_CSL_BERT,
        bibtex=_WRONG_BIBTEX_BERT,
        bibtex_from_csl=_WRONG_BIBTEX_BERT,
    ):
        csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
            doi=_BERT_DOI,
            baseline_entry=_BASELINE_MINIMAL,
            result_id="test",
        )

    assert not csl_matched, "CSL should be rejected"
    assert not bibtex_matched, "BibTeX should be rejected"
    assert csl_entry is None, "CSL entry should not be returned"
    assert bibtex_entry is None, "BibTeX entry should not be returned"


def test_validate_doi_candidate_network_errors() -> None:
    """
    Verify resilient error handling when DOI resolution fails due to network issues.
    """
    with _patch_doi_resolvers(
        csl_side_effect=urllib.error.URLError("Network error"),
        bibtex_side_effect=urllib.error.HTTPError(
            url="test",
            code=500,
            msg="Server Error",
            hdrs=Message(),
            fp=None,
        ),
        bibtex_from_csl=None,
    ):
        csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
            doi=_ARXIV_DOI,
            baseline_entry=_BASELINE_MINIMAL,
            result_id="test",
        )

    assert not csl_matched, "CSL should not match on network error"
    assert not bibtex_matched, "BibTeX should not match on network error"
    assert csl_entry is None, "CSL entry should not be returned on error"
    assert bibtex_entry is None, "BibTeX entry should not be returned on error"


def test_validate_doi_candidate_bibtex_fallback_when_csl_fails() -> None:
    """
    Confirm that BibTeX fallback triggers when CSL validation fails,
    exercising the Phase 3a short-circuit logic in reverse.
    """
    mock_csl_wrong: dict[str, Any] = {
        "title": "A Completely Different Paper Title",
        "author": [{"given": "John", "family": "Doe"}],
        "issued": {"date-parts": [[2020]]},
    }

    mock_bibtex_from_csl_wrong = dedent("""\
        @article{Doe2020,
          title = {A Completely Different Paper Title},
          author = {John Doe},
          year = {2020}
        }""")

    with _patch_doi_resolvers(
        csl=mock_csl_wrong,
        bibtex=_MATCHING_BIBTEX,
        bibtex_from_csl=mock_bibtex_from_csl_wrong,
    ):
        csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
            doi=_ARXIV_DOI,
            baseline_entry=_BASELINE_MINIMAL,
            result_id="test",
        )

    assert not csl_matched, "CSL should not match (wrong paper)"
    assert csl_entry is None, "CSL entry should be None"
    assert bibtex_matched, "BibTeX fallback should match when CSL fails"
    assert bibtex_entry is not None, "BibTeX entry should be returned"


def test_process_validated_doi_success() -> None:
    """
    Verify that successful DOI validation properly updates the enrichment
    tracking structures.
    """
    enr_list: list[tuple[str, dict[str, Any]]] = []
    flags = {"doi_csl": False, "doi_bibtex": False}

    with _patch_doi_resolvers(
        csl=_MATCHING_CSL,
        bibtex=_MATCHING_BIBTEX,
        bibtex_from_csl=_MATCHING_BIBTEX,
    ):
        doi_matched = process_validated_doi(
            doi=_ARXIV_DOI,
            baseline_entry=_BASELINE_MINIMAL,
            result_id="test",
            enr_list=enr_list,
            flags=flags,
        )

    assert doi_matched, "Should return True"
    assert enr_list, "Should populate enr_list"

    # CSL flag should be set; BibTeX skipped (Phase 3a optimization)
    assert flags.get("doi_csl"), "CSL flag should be set"
    assert not flags.get("doi_bibtex"), "BibTeX flag should not be set (skipped when CSL matches)"

    source_names = [source for source, _ in enr_list]
    assert "csl" in source_names, "CSL source should be in enr_list"


def test_process_validated_doi_failure() -> None:
    """
    Confirm that failed DOI validation leaves enrichment structures untouched.
    """
    mock_bibtex_wrong = dedent("""\
        @inproceedings{Devlin2019,
          title = {BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding},
          author = {Jacob Devlin and Ming-Wei Chang},
          year = {2019}
        }""")

    enr_list: list[tuple[str, dict[str, Any]]] = []
    flags = {"doi_csl": False, "doi_bibtex": False}

    with _patch_doi_resolvers(
        csl=_WRONG_CSL_BERT,
        bibtex=mock_bibtex_wrong,
        bibtex_from_csl=mock_bibtex_wrong,
    ):
        doi_matched = process_validated_doi(
            doi=_BERT_DOI,
            baseline_entry=_BASELINE_MINIMAL,
            result_id="test",
            enr_list=enr_list,
            flags=flags,
        )

    assert not doi_matched, "Should return False"
    assert not enr_list, "Should leave enr_list empty"
    assert not flags.get("doi_csl"), "doi_csl flag should remain False"
    assert not flags.get("doi_bibtex"), "doi_bibtex flag should remain False"
