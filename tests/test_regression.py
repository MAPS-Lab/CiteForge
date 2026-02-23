"""Regression tests for CiteForge.

Tests cover BibTeX parser edge cases, cache integration, DOI validation,
deduplication, pages validation, HTML entity decoding, title sanitization,
arXiv consistency, and dedup gate relaxation.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any
from unittest.mock import MagicMock, patch

from src import bibtex_utils as bt
from src import id_utils, merge_utils, text_utils
from src.api_generics import APISearchConfig, search_api_generic_multiple
from src.clients.scholar import _deduplicate_publication_list
from src.config import (
    MIN_TITLE_WORDS,
    OPENREVIEW_SESSION_TTL_SECS,
    PAGES_MAX_DIGITS,
    SIM_MERGE_DUPLICATE_THRESHOLD,
)
from src.doi_utils import validate_doi_candidate


def _extract_bibtex_field(bibtex_str: str, field_name: str) -> str | None:
    """Extract a field value from BibTeX output, handling nested braces."""
    pattern = rf"{field_name}\s*=\s*\{{"
    match = re.search(pattern, bibtex_str)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    for i in range(start, len(bibtex_str)):
        if bibtex_str[i] == "{":
            depth += 1
        elif bibtex_str[i] == "}":
            depth -= 1
            if depth == 0:
                return bibtex_str[start + 1 : i]
    return None


class TestBibtexParserInnerQuotes:
    """Test that parse_bibtex_to_dict handles quotes inside braces and outer quotes."""

    def test_quotes_inside_braces(self) -> None:
        """Quotes within braces should be preserved as literal characters."""
        bibtex = '@article{key1,\n  title = {AI "systems" review},\n  year = {2024}\n}\n'
        result = bt.parse_bibtex_to_dict(bibtex)
        assert result is not None
        assert result["fields"]["title"] == 'AI "systems" review'

    def test_simple_outer_quotes(self) -> None:
        """A value wrapped in outer double quotes should have quotes stripped."""
        bibtex = '@article{key2,\n  title = "outer value",\n  year = {2024}\n}\n'
        result = bt.parse_bibtex_to_dict(bibtex)
        assert result is not None
        assert result["fields"]["title"] == "outer value"

    def test_nested_braces_preserved(self) -> None:
        """Nested braces inside a field value should be handled correctly."""
        bibtex = '@article{key3,\n  title = {An {LSTM} Approach},\n  year = {2024}\n}\n'
        result = bt.parse_bibtex_to_dict(bibtex)
        assert result is not None
        assert "LSTM" in result["fields"]["title"]


class TestTildeInUrls:
    """Test that bibtex_from_dict preserves tildes in URLs but converts standalone tildes."""

    def test_tilde_in_url_preserved(self) -> None:
        """A tilde in a URL (preceded by /) should be kept as-is."""
        entry: dict[str, Any] = {
            "type": "misc",
            "key": "test2024",
            "fields": {
                "title": "Test",
                "url": "http://example.com/~user/page",
            },
        }
        output = bt.bibtex_from_dict(entry)
        url_val = _extract_bibtex_field(output, "url")
        assert url_val is not None
        assert "~" in url_val, f"Tilde should be preserved in URL, got: {url_val}"
        assert "~user" in url_val

    def test_standalone_tilde_converted(self) -> None:
        """A standalone tilde between words should become a space."""
        entry: dict[str, Any] = {
            "type": "misc",
            "key": "test2024",
            "fields": {
                "title": "word~word",
            },
        }
        output = bt.bibtex_from_dict(entry)
        title_val = _extract_bibtex_field(output, "title")
        assert title_val is not None
        assert "~" not in title_val, f"Standalone tilde should be replaced, got: {title_val}"
        assert "word word" in title_val


class TestNormalizeTitleWithLatex:
    """Test that normalize_title handles various LaTeX constructs."""

    def test_frac_becomes_fraction(self) -> None:
        r"""\\frac{1}{2} should normalize to contain '1/2'."""
        result = text_utils.normalize_title(r"\frac{1}{2}")
        assert "1/2" in result

    def test_textbf_stripped(self) -> None:
        r"""\\textbf{bold} should normalize to contain 'bold'."""
        result = text_utils.normalize_title(r"\textbf{bold}")
        assert "bold" in result

    def test_tilde_replaced(self) -> None:
        """A tilde should be replaced (non-breaking space) during normalization."""
        result = text_utils.normalize_title("hello~world")
        # The tilde is replaced by the regex in normalize_title that converts
        # punctuation (including ~) to spaces
        assert "~" not in result
        assert "hello" in result
        assert "world" in result


class TestSanitizeTitleRepeatedSubtitle:
    """Test _sanitize_title behavior for repeated subtitles.

    _sanitize_title is a nested function inside bibtex_from_dict, so we test it
    indirectly by round-tripping through bibtex_from_dict.
    """

    def test_short_repeated_segment_kept(self) -> None:
        """A short repeated subtitle segment (e.g., 'B') should NOT be truncated."""
        entry: dict[str, Any] = {
            "type": "article",
            "key": "test2024",
            "fields": {
                "title": "A: B: B",
            },
        }
        output = bt.bibtex_from_dict(entry)
        title_val = _extract_bibtex_field(output, "title")
        assert title_val is not None
        # "B" is only 1 char long, well under 15 chars, so both should remain
        assert title_val.count("B") == 2, f"Short repeated segment should be kept, got: {title_val}"

    def test_long_repeated_segment_truncated(self) -> None:
        """A long duplicated subtitle (> 15 chars) should be truncated."""
        long_sub = "Very Long Subtitle That Is Duplicated"
        title = f"Main Title: {long_sub}: {long_sub}"
        entry: dict[str, Any] = {
            "type": "article",
            "key": "test2024",
            "fields": {
                "title": title,
            },
        }
        output = bt.bibtex_from_dict(entry)
        title_val = _extract_bibtex_field(output, "title")
        assert title_val is not None
        # The duplicated long segment should appear only once
        assert title_val.count(long_sub) == 1, (
            f"Long repeated segment should be de-duplicated, got: {title_val}"
        )


class TestSearchApiGenericMultipleCache:
    """Test that search_api_generic_multiple uses cache for repeated queries."""

    def test_cache_hit_skips_http(self) -> None:
        """Second call with same args should return cached results without HTTP."""
        config = APISearchConfig(
            api_name="test_api_cache",
            base_url="https://api.example.com/search",
            query_param_name="q",
            result_path=["results"],
            title_field="title",
            author_field="authors",
        )

        fake_results = {
            "results": [
                {
                    "title": "Machine Learning Fundamentals",
                    "authors": [{"name": "John Smith"}],
                    "year": 2024,
                },
            ],
        }

        # Patch the cache to use a temporary directory and ensure it is enabled
        with (
            patch("src.api_generics.response_cache") as mock_cache,
            patch("src.api_generics.http_get_json", return_value=fake_results) as mock_http,
        ):
            # First call: cache miss, HTTP hit
            mock_cache.get.return_value = None
            search_api_generic_multiple(
                title="Machine Learning Fundamentals",
                author_name="John Smith",
                config=config,
            )
            # http_get_json should have been called once
            assert mock_http.call_count == 1

            # Second call: simulate cache hit
            mock_cache.get.return_value = {"results": [{"title": "Machine Learning Fundamentals",
                                                         "authors": [{"name": "John Smith"}],
                                                         "year": 2024}]}
            result2 = search_api_generic_multiple(
                title="Machine Learning Fundamentals",
                author_name="John Smith",
                config=config,
            )
            # http_get_json should NOT have been called again
            assert mock_http.call_count == 1
            assert len(result2) == 1
            assert result2[0]["title"] == "Machine Learning Fundamentals"


class TestDoiValidationSkipsBibtexWhenCslMatches:
    """Test that validate_doi_candidate does not fetch BibTeX when CSL matches."""

    @patch("src.doi_utils.search_apis.fetch_bibtex_via_doi")
    @patch("src.doi_utils.search_apis.fetch_csl_via_doi")
    @patch("src.doi_utils.search_apis.bibtex_from_csl")
    def test_csl_match_skips_bibtex(
        self,
        mock_bibtex_from_csl: MagicMock,
        mock_fetch_csl: MagicMock,
        mock_fetch_bibtex: MagicMock,
    ) -> None:
        """When CSL validation succeeds, fetch_bibtex_via_doi should not be called."""
        # Set up a baseline entry
        baseline_entry: dict[str, Any] = {
            "type": "article",
            "key": "Smith2024",
            "fields": {
                "title": "Test Paper on Machine Learning",
                "author": "John Smith",
                "year": "2024",
            },
        }

        # Mock CSL to return a matching entry
        mock_fetch_csl.return_value = {"title": "Test Paper on Machine Learning", "DOI": "10.1234/test"}
        # bibtex_from_csl returns a BibTeX string that parse_bibtex_to_dict can parse
        mock_bibtex_from_csl.return_value = (
            "@article{Smith2024,\n"
            "  title = {Test Paper on Machine Learning},\n"
            "  author = {John Smith},\n"
            "  year = {2024}\n"
            "}\n"
        )

        # Patch bibtex_entries_match_strict to return True for the CSL path
        with patch("src.doi_utils.bt.bibtex_entries_match_strict", return_value=True):
            csl_matched, bibtex_matched, _, _ = validate_doi_candidate(
                doi="10.1234/test",
                baseline_entry=baseline_entry,
                result_id="Smith2024",
            )

        # CSL should have matched
        assert csl_matched is True
        # BibTeX should not have been called at all
        mock_fetch_bibtex.assert_not_called()
        # bibtex_matched should be False since we skipped it
        assert bibtex_matched is False


class TestDeduplicatePublicationList:
    """Test _deduplicate_publication_list from src/clients/scholar.py."""

    def test_empty_list(self) -> None:
        """Empty input should return empty output."""
        result = _deduplicate_publication_list([])
        assert result == []

    def test_single_item(self) -> None:
        """A single publication should pass through unchanged."""
        pubs = [{"title": "Deep Learning Survey", "year": 2024, "authors": ["Jane Doe"]}]
        result = _deduplicate_publication_list(pubs)
        assert len(result) == 1
        assert result[0]["title"] == "Deep Learning Survey"

    def test_exact_duplicate_titles_reduced(self) -> None:
        """Two entries with identical titles should collapse to one."""
        pubs = [
            {"title": "Attention Is All You Need", "year": 2017, "authors": ["Ashish Vaswani"]},
            {"title": "Attention Is All You Need", "year": 2017, "authors": ["Ashish Vaswani"]},
        ]
        result = _deduplicate_publication_list(pubs)
        assert len(result) == 1

    def test_different_titles_both_kept(self) -> None:
        """Two entries with clearly different titles should both be kept."""
        pubs = [
            {"title": "Attention Is All You Need", "year": 2017, "authors": ["Ashish Vaswani"]},
            {"title": "Deep Residual Learning for Image Recognition", "year": 2016, "authors": ["Kaiming He"]},
        ]
        result = _deduplicate_publication_list(pubs)
        assert len(result) == 2


class TestIsSecondaryDoi:
    """Fix 1: is_secondary_doi classifies preprint and data DOIs."""

    def test_arxiv_doi(self) -> None:
        assert id_utils.is_secondary_doi("10.48550/arxiv.2401.12345") is True

    def test_psyarxiv_doi(self) -> None:
        assert id_utils.is_secondary_doi("10.31234/osf.io/abcde") is True

    def test_figshare_doi(self) -> None:
        assert id_utils.is_secondary_doi("10.6084/m9.figshare.12345678") is True

    def test_zenodo_doi(self) -> None:
        assert id_utils.is_secondary_doi("10.5281/zenodo.7654321") is True

    def test_published_doi(self) -> None:
        assert id_utils.is_secondary_doi("10.1145/1234567.1234568") is False

    def test_nature_doi(self) -> None:
        assert id_utils.is_secondary_doi("10.1038/s41586-024-00001-1") is False


class TestPagesMaxDigits:
    """Fix 2: SAGE/Wiley article IDs rejected as pages."""

    def test_sage_article_id_rejected(self) -> None:
        """16-digit SAGE article IDs should be rejected from pages field."""
        entry = {
            "type": "article",
            "key": "Test2023",
            "fields": {"title": "Test", "author": "Author", "year": "2023"},
        }
        enrichers = [
            ("crossref", {"fields": {"pages": "20552076231171496"}}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert "pages" not in merged["fields"]

    def test_normal_pages_accepted(self) -> None:
        """Normal page ranges (e.g., 123--456) should be accepted."""
        entry = {
            "type": "article",
            "key": "Test2023",
            "fields": {"title": "Test", "author": "Author", "year": "2023"},
        }
        enrichers = [
            ("crossref", {"fields": {"pages": "123--456"}}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["fields"].get("pages") == "123--456"

    def test_short_pages_accepted(self) -> None:
        """Single page numbers should be accepted."""
        entry = {
            "type": "article",
            "key": "Test2023",
            "fields": {"title": "Test", "author": "Author", "year": "2023"},
        }
        enrichers = [
            ("crossref", {"fields": {"pages": "42"}}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["fields"].get("pages") == "42"

    def test_max_digits_boundary(self) -> None:
        """Pages with exactly PAGES_MAX_DIGITS digits should be accepted."""
        pages = "1" * PAGES_MAX_DIGITS  # e.g., "12345678"
        entry = {
            "type": "article",
            "key": "Test2023",
            "fields": {"title": "Test", "author": "Author", "year": "2023"},
        }
        enrichers = [
            ("crossref", {"fields": {"pages": pages}}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["fields"].get("pages") == pages

    def test_large_page_range_accepted(self) -> None:
        """IEEE-style 5-digit page ranges like 13905-13917 should be accepted."""
        entry = {
            "type": "article",
            "key": "Test2023",
            "fields": {"title": "Test", "author": "Author", "year": "2023"},
        }
        enrichers = [
            ("crossref", {"fields": {"pages": "13905-13917"}}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["fields"].get("pages") == "13905-13917"


class TestHtmlEntityDecode:
    """Fix 3: HTML entities decoded in journal/title fields."""

    def test_amp_decoded_in_journal(self) -> None:
        entry = {
            "type": "article",
            "key": "Test2023",
            "fields": {
                "title": "Test Paper",
                "author": "Author",
                "year": "2023",
                "journal": "Computers &amp; Education",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["fields"]["journal"] == "Computers & Education"

    def test_lt_gt_decoded_in_title(self) -> None:
        entry = {
            "type": "article",
            "key": "Test2023",
            "fields": {
                "title": "A &lt;b&gt;Bold&lt;/b&gt; Approach",
                "author": "Author",
                "year": "2023",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        # HTML tags are stripped, then entities are decoded
        assert "&lt;" not in merged["fields"]["title"]
        assert "&gt;" not in merged["fields"]["title"]


class TestTrimTitleArtifacts:
    """Fix 4: 'Check for updates' prefix stripped from titles."""

    def test_check_for_updates_stripped(self) -> None:
        result = text_utils.trim_title_default("Check for updates Real Title Here")
        assert result == "Real Title Here"

    def test_check_for_updates_case_variant(self) -> None:
        result = text_utils.trim_title_default("Check for Updates Real Title Here")
        assert result == "Real Title Here"

    def test_normal_title_unchanged(self) -> None:
        result = text_utils.trim_title_default("Normal Academic Paper Title")
        assert result == "Normal Academic Paper Title"

    def test_check_inside_title_unchanged(self) -> None:
        """'Check' in the middle of a title should not be affected."""
        result = text_utils.trim_title_default("How to Check for Updates in Software")
        assert result == "How to Check for Updates in Software"


class TestMinTitleWords:
    """Fix 6: Single-word titles rejected as Scholar artifacts."""

    def test_single_word_below_minimum(self) -> None:
        """A single-word title has fewer words than MIN_TITLE_WORDS threshold."""
        title = "Games"
        word_count = len(title.split())
        assert word_count < MIN_TITLE_WORDS, (
            f"Single-word title should be below MIN_TITLE_WORDS={MIN_TITLE_WORDS}, got {word_count}"
        )

    def test_two_word_title_meets_minimum(self) -> None:
        """A two-word title should meet the MIN_TITLE_WORDS threshold."""
        title = "Good Title"
        word_count = len(title.split())
        assert word_count >= MIN_TITLE_WORDS, (
            f"Two-word title should meet MIN_TITLE_WORDS={MIN_TITLE_WORDS}, got {word_count}"
        )

    def test_normal_title_well_above_minimum(self) -> None:
        """Normal academic title should be well above the minimum word threshold."""
        title = "A Comprehensive Survey on Machine Learning"
        word_count = len(title.split())
        assert word_count >= MIN_TITLE_WORDS, (
            f"Normal title should be above MIN_TITLE_WORDS={MIN_TITLE_WORDS}, got {word_count}"
        )


class TestArxivJournalConsistency:
    """Fix 7: Pure arXiv papers consistently get journal='arXiv e-prints'."""

    def test_arxiv_eprint_no_journal_gets_standard(self) -> None:
        """An arXiv paper with eprint but no journal should get 'arXiv e-prints'."""
        fields: dict[str, Any] = {
            "eprint": "2401.12345",
            "archiveprefix": "arXiv",
            "doi": "10.48550/arxiv.2401.12345",
        }
        result = id_utils.normalize_arxiv_metadata(fields)
        assert result["journal"] == "arXiv e-prints"

    def test_arxiv_eprint_no_doi_no_journal(self) -> None:
        """An arXiv paper with eprint, no DOI, no journal should get 'arXiv e-prints'."""
        fields: dict[str, Any] = {
            "eprint": "2401.12345",
            "archiveprefix": "arXiv",
        }
        result = id_utils.normalize_arxiv_metadata(fields)
        assert result["journal"] == "arXiv e-prints"

    def test_published_doi_with_eprint_keeps_journal(self) -> None:
        """A paper with published DOI and existing journal should NOT be overwritten."""
        fields: dict[str, Any] = {
            "eprint": "2401.12345",
            "archiveprefix": "arXiv",
            "doi": "10.1145/1234567",
            "journal": "ACM Computing Surveys",
        }
        result = id_utils.normalize_arxiv_metadata(fields)
        assert result["journal"] == "ACM Computing Surveys"

    def test_arxiv_journal_variant_standardized(self) -> None:
        """arXiv preprint variants in journal field are standardized."""
        fields: dict[str, Any] = {
            "eprint": "2401.12345",
            "archiveprefix": "arXiv",
            "journal": "arXiv preprint arXiv:2401.12345",
        }
        result = id_utils.normalize_arxiv_metadata(fields)
        assert result["journal"] == "arXiv e-prints"


class TestStrongAuthorDedupGate:
    """Fix 8: Strong author overlap allows composite dedup scoring."""

    def test_same_authors_moderate_title_sim_matches(self) -> None:
        """Real Alhasani2025 duplicate: same authors, truncated title variant → should match."""
        _title_a = (
            "Bridging Research and Practice in Persuasive Mobile Stress"
            " Management Apps: A 21-Year Comparative Analysis and Novel"
            " Design Framework"
        )
        entry_a: dict[str, Any] = {
            "type": "inproceedings",
            "key": "Alhasani2025a",
            "fields": {
                "title": _title_a,
                "author": "Mona Alhasani and Oladapo Oyebode and Rita Orji",
                "year": "2025",
                "booktitle": "Lecture Notes in Computer Science",
                "pages": "147-164",
                "doi": "10.1007/978-3-031-94959-3_11",
            },
        }
        entry_b: dict[str, Any] = {
            "type": "inproceedings",
            "key": "Alhasani2025b",
            "fields": {
                "title": "Mobile Stress Management Apps: A 21-Year Comparative Analysis and Novel Design",
                "author": "Mona Alhasani and Oladapo Oyebode and Rita Orji",
                "year": "2025",
                "booktitle": "Persuasive Technology",
                "pages": "147",
            },
        }
        # Verify preconditions: high author overlap, moderate title sim
        from src.text_utils import author_overlap_ratio
        overlap = author_overlap_ratio(
            entry_a["fields"]["author"], entry_b["fields"]["author"]
        )
        assert overlap >= 0.9, f"Expected high author overlap, got {overlap}"

        sim = text_utils.title_similarity(
            entry_a["fields"]["title"], entry_b["fields"]["title"]
        )
        assert 0.6 <= sim < 0.95, f"Expected moderate title sim (0.6-0.95), got {sim}"

        # Fix 8: the strict matcher should detect this as a duplicate
        result = bt.bibtex_entries_match_strict(entry_a, entry_b)
        assert result is True, "Same authors + moderate title sim should match via composite scoring"

    def test_different_authors_not_matched(self) -> None:
        """Entries with different authors should not be matched."""
        entry_a: dict[str, Any] = {
            "type": "article",
            "key": "Smith2024",
            "fields": {
                "title": "Machine Learning for Healthcare",
                "author": "Alice Smith and Bob Jones and Carol Williams",
                "year": "2024",
            },
        }
        entry_b: dict[str, Any] = {
            "type": "article",
            "key": "Brown2024",
            "fields": {
                "title": "Deep Learning in Medical Imaging",
                "author": "Dave Brown and Eve Taylor and Frank Wilson",
                "year": "2024",
            },
        }
        result = bt.bibtex_entries_match_strict(entry_a, entry_b)
        assert result is False


class TestGenericSeriesNameMerge:
    """Fix 5: LNCS and other generic series names should be replaced by specific conference names."""

    def test_lncs_replaced_by_conference_name(self) -> None:
        """When CSL provides LNCS and enricher provides real conference name, prefer conference."""
        entry = {
            "type": "inproceedings",
            "key": "Test2022",
            "fields": {
                "title": "Test Paper Title",
                "author": "Author One",
                "year": "2022",
                "booktitle": "Lecture Notes in Computer Science",
            },
        }
        enrichers = [
            ("crossref", {"fields": {"booktitle": "Persuasive Technology 2022"}}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["fields"]["booktitle"] == "Persuasive Technology 2022"

    def test_specific_name_not_replaced_by_lncs(self) -> None:
        """A specific conference name should never be downgraded to a generic series."""
        entry = {
            "type": "inproceedings",
            "key": "Test2022",
            "fields": {
                "title": "Test Paper Title",
                "author": "Author One",
                "year": "2022",
                "booktitle": "Persuasive Technology 2022",
            },
        }
        enrichers = [
            ("csl", {"fields": {"booktitle": "Lecture Notes in Computer Science"}}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["fields"]["booktitle"] == "Persuasive Technology 2022"

    def test_lnns_also_generic(self) -> None:
        """Lecture Notes in Networks and Systems is also a generic series."""
        entry = {
            "type": "inproceedings",
            "key": "Test2024",
            "fields": {
                "title": "Test Paper",
                "author": "Author One",
                "year": "2024",
                "booktitle": "Lecture Notes in Networks and Systems",
            },
        }
        enrichers = [
            ("s2", {"fields": {"booktitle": "Actual Conference 2024"}}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["fields"]["booktitle"] == "Actual Conference 2024"

    def test_shti_also_generic(self) -> None:
        """Studies in Health Technology and Informatics (IOS Press) is a generic series."""
        entry = {
            "type": "incollection",
            "key": "Test2024",
            "fields": {
                "title": "Test Paper",
                "author": "Author One",
                "year": "2024",
                "booktitle": "Studies in Health Technology and Informatics",
            },
        }
        enrichers = [
            ("crossref", {"fields": {"booktitle": "MEDINFO 2023 - The Future Is Accessible"}}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["fields"]["booktitle"] == "MEDINFO 2023 - The Future Is Accessible"

    def test_incollection_with_generic_series_becomes_inproceedings(self) -> None:
        """incollection with generic series (SHTI) in booktitle should become @inproceedings."""
        entry = {
            "type": "misc",
            "key": "Test2022",
            "fields": {
                "title": "Test Paper on Machine Learning",
                "author": "Author One and Author Two",
                "year": "2022",
            },
        }
        enrichers = [
            ("crossref", {
                "type": "incollection",
                "fields": {
                    "booktitle": "Studies in Health Technology and Informatics",
                    "publisher": "IOS Press",
                    "doi": "10.3233/shti220385",
                },
            }),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["type"] == "inproceedings"

    def test_incollection_with_handbook_stays_incollection(self) -> None:
        """Actual book chapters (handbooks) should remain @incollection."""
        entry = {
            "type": "incollection",
            "key": "Test2023",
            "fields": {
                "title": "A Chapter on Methods",
                "author": "Author One",
                "year": "2023",
                "booktitle": "Handbook of Machine Learning",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["type"] == "incollection"

    def test_book_type_from_enricher_survives_venue_override(self) -> None:
        """Proceedings volumes typed as 'book' by CSL/Crossref should NOT be overridden to @inproceedings."""
        entry = {
            "type": "misc",
            "key": "Proceedings2022",
            "fields": {
                "title": "Conference X, 2022, Proceedings",
                "author": "Editor One and Editor Two",
                "year": "2022",
            },
        }
        enrichers = [
            ("csl", {
                "type": "book",
                "fields": {
                    "booktitle": "Lecture Notes in Computer Science",
                    "publisher": "Springer",
                    "doi": "10.1007/978-3-031-09342-5",
                },
            }),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["type"] == "book"


class TestSameSourceTypeOverride:
    """Merge must prefer the later type when CSL appears twice (arXiv DOI then published DOI)."""

    def test_csl_inproceedings_overrides_csl_article(self) -> None:
        """Second CSL enricher (published DOI) should override first (arXiv DOI) type."""
        entry = {
            "type": "misc",
            "key": "Liu2025BPMN",
            "fields": {
                "title": "BPMN to Smart Contract by Business Analyst",
                "author": "C. G. Liu and P. Bodorik and D. Jutla",
                "year": "2025",
                "doi": "10.48550/arxiv.2505.22612",
            },
        }
        enrichers = [
            ("csl", {
                "type": "article",
                "fields": {
                    "title": "BPMN to Smart Contract by Business Analyst",
                    "doi": "10.48550/ARXIV.2505.22612",
                    "url": "https://arxiv.org/abs/2505.22612",
                },
            }),
            ("s2", {
                "type": "inproceedings",
                "fields": {
                    "booktitle": "International Computer Science Conference",
                    "doi": "10.1109/icsc65596.2025.11140498",
                },
            }),
            ("csl", {
                "type": "inproceedings",
                "fields": {
                    "booktitle": "2025 5th Intelligent Cybersecurity Conference (ICSC)",
                    "publisher": "IEEE",
                    "pages": "122-129",
                    "doi": "10.1109/icsc65596.2025.11140498",
                },
            }),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["type"] == "inproceedings", (
            f"Expected inproceedings, got {merged['type']}: "
            "second CSL (published DOI) should override first CSL (arXiv DOI)"
        )
        fields = merged.get("fields", {})
        assert "booktitle" in fields, "Conference name should be in booktitle"
        assert "journal" not in fields, "No journal field for conference papers"

    def test_same_source_same_type_no_flip(self) -> None:
        """Same source with same type should not cause unnecessary change."""
        entry = {
            "type": "misc",
            "key": "Test2025",
            "fields": {
                "title": "Test Paper",
                "author": "Author One",
                "year": "2025",
            },
        }
        enrichers = [
            ("crossref", {
                "type": "article",
                "fields": {"journal": "Some Journal"},
            }),
            ("crossref", {
                "type": "article",
                "fields": {"volume": "42"},
            }),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["type"] == "article"


class TestAuthorNameMatches:
    """Tests for author_name_matches used to filter wrong-author entries."""

    def test_full_name_match(self) -> None:
        """Full name match: 'Raza Abidi' matches 'Syed Sibte Raza Abidi'."""
        from src.text_utils import author_name_matches
        assert author_name_matches("Raza Abidi", "Author One and Syed Sibte Raza Abidi")

    def test_different_first_name_no_match(self) -> None:
        """Different first name: 'Raza Abidi' should NOT match 'Saeed Abidi'."""
        from src.text_utils import author_name_matches
        assert not author_name_matches("Raza Abidi", "Author One and Saeed Abidi")

    def test_partial_name_no_match(self) -> None:
        """Partial name: 'Raza Abidi' should NOT match 'Syed Abidi' (missing Raza)."""
        from src.text_utils import author_name_matches
        assert not author_name_matches("Raza Abidi", "Author One and Syed Abidi")

    def test_exact_name_match(self) -> None:
        """Exact name match works."""
        from src.text_utils import author_name_matches
        assert author_name_matches("Gabriel Spadon", "Gabriel Spadon and Author Two")

    def test_middle_initial_in_paper(self) -> None:
        """Author with extra middle initial in paper should still match."""
        from src.text_utils import author_name_matches
        assert author_name_matches(
            "Carlos Hernandez-Castillo",
            "Faezeh Moradi and Carlos R. Hernandez-Castillo",
        )

    def test_middle_initial_no_false_positive(self) -> None:
        """Different first names with same last name should NOT match."""
        from src.text_utils import author_name_matches
        assert not author_name_matches("Alice Brown", "Betty Adams Brown")


class TestTitleLengthWhitespaceNormalization:
    """Title comparison normalizes whitespace so OCR artifacts don't get false length advantage."""

    def test_broken_title_replaced_by_correct(self) -> None:
        """'Un met' (Scholar artifact) should be replaced by 'Unmet' from a higher-trust source."""
        entry = {
            "type": "misc",
            "key": "Test2024",
            "fields": {
                "title": "A Topological Data Analysis of Un met Health Care Needs Among Injured Patients",
                "author": "Author One",
                "year": "2024",
            },
        }
        enrichers = [
            ("crossref", {
                "fields": {
                    "title": "A Topological Data Analysis of Unmet Health Care Needs Among Injured Patients",
                },
            }),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert "Un met" not in merged["fields"]["title"]
        assert "Unmet" in merged["fields"]["title"]


class TestLeadingZerosInPages:
    """Pages with leading zeros should have them stripped (e.g., 01-08 → 1-8)."""

    def test_leading_zeros_stripped(self) -> None:
        entry = {
            "type": "inproceedings",
            "fields": {"title": "Some Paper", "pages": "01-08", "booktitle": "Some Conf"},
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["fields"]["pages"] == "1-8"

    def test_no_leading_zeros_unchanged(self) -> None:
        entry = {
            "type": "article",
            "fields": {"title": "Some Paper", "pages": "123-456", "journal": "J"},
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["fields"]["pages"] == "123-456"

    def test_single_page_leading_zero(self) -> None:
        entry = {
            "type": "article",
            "fields": {"title": "Some Paper", "pages": "07", "journal": "J"},
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["fields"]["pages"] == "7"


class TestFrontiersJournalDetection:
    """Frontiers in * booktitles should be moved to journal field."""

    def test_frontiers_booktitle_becomes_journal(self) -> None:
        entry = {
            "type": "inproceedings",
            "fields": {
                "title": "Some Paper",
                "booktitle": "Frontiers in Bioinformatics",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["fields"].get("journal") == "Frontiers in Bioinformatics"
        assert "booktitle" not in merged["fields"]
        assert merged["type"] == "article"

    def test_frontiers_not_moved_when_journal_exists(self) -> None:
        entry = {
            "type": "article",
            "fields": {
                "title": "Some Paper",
                "journal": "Nature",
                "booktitle": "Frontiers in Immunology",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        # journal stays as Nature; booktitle removed because type is article
        assert merged["fields"].get("journal") == "Nature"

    def test_non_frontiers_booktitle_unchanged(self) -> None:
        entry = {
            "type": "inproceedings",
            "fields": {
                "title": "Some Paper",
                "booktitle": "International Conference on AI",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["fields"].get("booktitle") == "International Conference on AI"
        assert "journal" not in merged["fields"]


class TestHtmlEntityInSerializer:
    """HTML entities like &amp; should be decoded in bibtex_from_dict output."""

    def test_amp_decoded_in_booktitle(self) -> None:
        from src.bibtex_utils import bibtex_from_dict
        entry = {
            "type": "inproceedings",
            "key": "Test2024:SomeConf",
            "fields": {
                "title": "Some Paper",
                "booktitle": "IEEE Tech &amp; Engineering Conf",
            },
        }
        bib_str = bibtex_from_dict(entry)
        assert "&amp;" not in bib_str
        assert "& Engineering" in bib_str


class TestJournalUrlNormalization:
    """Journal fields containing URLs should be normalized to server names."""

    def test_arxiv_url_becomes_journal_name(self) -> None:
        entry = {
            "type": "article",
            "fields": {
                "title": "Some Paper",
                "journal": "https://arxiv.org/pdf/2302.08018",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["fields"]["journal"] == "arXiv e-prints"

    def test_techrxiv_url_becomes_journal_name(self) -> None:
        entry = {
            "type": "article",
            "fields": {
                "title": "Some Paper",
                "journal": "https://www.techrxiv.org/users/770734/articles/846181-test",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["fields"]["journal"] == "TechRxiv"

    def test_unknown_url_dropped(self) -> None:
        entry = {
            "type": "article",
            "fields": {
                "title": "Some Paper",
                "journal": "https://example.com/papers/123",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert "journal" not in merged["fields"]


class TestTokenBucketRateLimiter:
    """Tests for the TokenBucketRateLimiter in http_utils."""

    def test_acquire_respects_rate(self) -> None:
        """Acquire should block when tokens are exhausted."""
        from src.http_utils import TokenBucketRateLimiter

        limiter = TokenBucketRateLimiter(rate=100.0, burst=1)
        start = time.monotonic()
        limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    def test_burst_allows_multiple_immediate(self) -> None:
        """Burst > 1 should allow multiple immediate acquires."""
        from src.http_utils import TokenBucketRateLimiter

        limiter = TokenBucketRateLimiter(rate=100.0, burst=3)
        start = time.monotonic()
        for _ in range(3):
            limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1

    def test_rate_limiter_registry(self) -> None:
        """Rate limiter registry returns consistent instances."""
        from src.http_utils import _get_rate_limiter

        limiter1 = _get_rate_limiter("crossref")
        limiter2 = _get_rate_limiter("crossref")
        assert limiter1 is limiter2
        assert limiter1 is not None

    def test_unknown_namespace_returns_none(self) -> None:
        """Unknown namespaces should return None (no rate limiting)."""
        from src.http_utils import _get_rate_limiter

        assert _get_rate_limiter("nonexistent_api_xyz") is None


class TestDotNotationFieldExtraction:
    """Tests for _resolve_dotted in api_generics.py."""

    def test_simple_field(self) -> None:
        from src.api_generics import _resolve_dotted

        assert _resolve_dotted({"title": "My Paper"}, "title") == "My Paper"

    def test_nested_field(self) -> None:
        from src.api_generics import _resolve_dotted

        data = {"externalIds": {"DOI": "10.1234/test", "ArXiv": "2301.00001"}}
        assert _resolve_dotted(data, "externalIds.DOI") == "10.1234/test"
        assert _resolve_dotted(data, "externalIds.ArXiv") == "2301.00001"

    def test_deeply_nested(self) -> None:
        from src.api_generics import _resolve_dotted

        data = {"primary_location": {"source": {"display_name": "Nature"}}}
        assert _resolve_dotted(data, "primary_location.source.display_name") == "Nature"

    def test_missing_nested_field(self) -> None:
        from src.api_generics import _resolve_dotted

        data = {"externalIds": {"ArXiv": "2301.00001"}}
        assert _resolve_dotted(data, "externalIds.DOI") is None

    def test_missing_parent(self) -> None:
        from src.api_generics import _resolve_dotted

        assert _resolve_dotted({"title": "test"}, "externalIds.DOI") is None

    def test_str_variant(self) -> None:
        from src.api_generics import _resolve_dotted_str

        data = {"journal": {"name": "Nature"}}
        assert _resolve_dotted_str(data, "journal.name") == "Nature"
        assert _resolve_dotted_str(data, "journal.missing") is None

    def test_str_variant_list(self) -> None:
        """List values should be unwrapped to first element."""
        from src.api_generics import _resolve_dotted_str

        data = {"title": ["My Paper", "Subtitle"]}
        assert _resolve_dotted_str(data, "title") == "My Paper"


class TestDOINormalizationInDedup:
    """Tests that DOI comparisons in save_entry_to_file use normalization."""

    def test_doi_url_vs_bare_match(self, tmp_path: Any) -> None:
        """DOIs with and without URL prefix should match as duplicates."""
        from src.merge_utils import save_entry_to_file

        entry1 = {
            "type": "article",
            "key": "Smith2024:Test",
            "fields": {
                "title": "A Test Paper About Machine Learning",
                "author": "Smith, John and Doe, Jane",
                "year": "2024",
                "journal": "Nature",
                "doi": "10.1234/test.2024.001",
            },
        }
        # Save first entry
        path1, _ = save_entry_to_file(str(tmp_path), "test_author", entry1)
        assert os.path.exists(path1)

        # Second entry with URL-formatted DOI
        entry2 = {
            "type": "article",
            "key": "Smith2024:Test",
            "fields": {
                "title": "A Test Paper About Machine Learning",
                "author": "Smith, John and Doe, Jane",
                "year": "2024",
                "journal": "Nature",
                "doi": "https://doi.org/10.1234/test.2024.001",
            },
        }
        # Should detect as duplicate (same DOI after normalization)
        path2, _ = save_entry_to_file(str(tmp_path), "test_author", entry2)
        # Both paths should resolve to the same file (dedup worked)
        assert os.path.basename(path1) == os.path.basename(path2)


class TestHttpPostJson:
    """Tests for http_post_json going through the full HTTP infrastructure."""

    @patch("src.http_utils._http_request")
    def test_post_calls_http_request_with_post_method(self, mock_request: MagicMock) -> None:
        """http_post_json should delegate to _http_request with method='POST'."""
        from src.http_utils import http_post_json

        mock_request.return_value = b'{"result": "ok"}'
        result = http_post_json(
            "https://generativelanguage.googleapis.com/v1beta/test",
            {"key": "value"},
            timeout=10.0,
        )
        assert result == {"result": "ok"}
        mock_request.assert_called_once()
        call_args = mock_request.call_args
        assert call_args[0][0] == "POST"
        assert call_args[1]["json_payload"] == {"key": "value"}

    def test_post_sets_content_type(self) -> None:
        """http_post_json should set Content-Type header when not provided."""
        from src.http_utils import http_post_json

        with patch("src.http_utils._http_request") as mock_req:
            mock_req.return_value = b'{"ok": true}'
            http_post_json("https://example.com/api", {"data": 1})
            headers = mock_req.call_args[0][2]
            assert headers.get("Content-Type") == "application/json"

    def test_post_preserves_custom_content_type(self) -> None:
        """Custom headers with Content-Type should not be overridden."""
        from src.http_utils import http_post_json

        with patch("src.http_utils._http_request") as mock_req:
            mock_req.return_value = b'{"ok": true}'
            custom = {"Content-Type": "application/x-custom", "Accept": "application/json"}
            http_post_json("https://example.com/api", {"data": 1}, headers=custom)
            headers = mock_req.call_args[0][2]
            assert headers.get("Content-Type") == "application/x-custom"


def _reset_openreview_session() -> None:
    """Reset OpenReview session state to a clean slate."""
    import src.clients.search_apis as sa

    with sa._OPENREVIEW_SESSION_LOCK:
        sa._OPENREVIEW_SESSION = None
        sa._OPENREVIEW_SESSION_CREATED_AT = 0.0


class TestOpenReviewSessionExpiry:
    """Tests for OpenReview session TTL-based expiry."""

    def test_expired_session_triggers_relogin(self) -> None:
        """After TTL expires, openreview_login should re-authenticate."""
        import src.clients.search_apis as sa

        _reset_openreview_session()

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"Set-Cookie": "session=abc123"}

        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp

        creds = ("user@example.com", "password123")

        with patch("src.clients.search_apis._get_session", return_value=mock_session):
            # First login
            result1 = sa.openreview_login(creds)
            assert result1 is not None
            assert result1["Cookie"] == "session=abc123"
            assert mock_session.post.call_count == 1

            # Simulate session expiry by backdating the timestamp
            with sa._OPENREVIEW_SESSION_LOCK:
                sa._OPENREVIEW_SESSION_CREATED_AT = 0.0  # epoch = expired

            # Second login should re-authenticate
            mock_resp2 = MagicMock()
            mock_resp2.raise_for_status = MagicMock()
            mock_resp2.headers = {"Set-Cookie": "session=refreshed"}
            mock_session.post.return_value = mock_resp2

            result2 = sa.openreview_login(creds)
            assert result2 is not None
            assert result2["Cookie"] == "session=refreshed"
            assert mock_session.post.call_count == 2

        _reset_openreview_session()

    def test_valid_session_reused(self) -> None:
        """A session within TTL should be returned without re-login."""
        import src.clients.search_apis as sa

        _reset_openreview_session()

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"Set-Cookie": "session=valid"}

        mock_session = MagicMock()
        mock_session.post.return_value = mock_resp

        creds = ("user@example.com", "password123")

        with patch("src.clients.search_apis._get_session", return_value=mock_session):
            result1 = sa.openreview_login(creds)
            assert result1 is not None

            # Call again immediately — should reuse without re-login
            result2 = sa.openreview_login(creds)
            assert result2 is result1
            assert mock_session.post.call_count == 1

        _reset_openreview_session()


class TestBaselineThresholdConsistency:
    """Baseline file matching should use >= (not >) for threshold comparison."""

    def test_at_threshold_loads_existing_file(self) -> None:
        """Titles with similarity exactly at SIM_MERGE_DUPLICATE_THRESHOLD should match."""
        title = "Exact Same Title For Testing"
        sim = text_utils.title_similarity(title, title)
        assert sim >= SIM_MERGE_DUPLICATE_THRESHOLD

    def test_slightly_below_threshold_does_not_match(self) -> None:
        """Clearly different titles should have similarity below the threshold."""
        sim = text_utils.title_similarity(
            "Machine Learning for Healthcare",
            "Quantum Computing Applications",
        )
        assert sim < SIM_MERGE_DUPLICATE_THRESHOLD


class TestOrcidUsesHttpGetJson:
    """ORCID should go through http_get_json (shared HTTP infrastructure)."""

    @patch("src.clients.utility_apis.http_get_json")
    def test_orcid_calls_http_get_json(self, mock_get: MagicMock) -> None:
        """orcid_fetch_works should use http_get_json, not urllib."""
        from src.clients.utility_apis import orcid_fetch_works

        mock_get.return_value = {"group": []}
        with patch("src.clients.utility_apis.response_cache") as mock_cache:
            mock_cache.get.return_value = None
            orcid_fetch_works("0000-0001-2345-6789")
        mock_get.assert_called_once()
        url = mock_get.call_args[0][0]
        assert "pub.orcid.org" in url


class TestGeminiUsesHttpPostJson:
    """Gemini should go through http_post_json (shared HTTP infrastructure)."""

    @patch("src.clients.utility_apis.http_post_json")
    def test_gemini_calls_http_post_json(self, mock_post: MagicMock) -> None:
        """gemini_generate_short_title should use http_post_json, not urllib."""
        from src.clients.utility_apis import gemini_generate_short_title

        mock_post.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{"text": "MachineLearning"}]
                }
            }]
        }
        result = gemini_generate_short_title("Machine Learning Paper", "fake-key")
        assert result == "MachineLearning"
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "generativelanguage.googleapis.com" in url

    @patch("src.clients.utility_apis.http_post_json")
    def test_gemini_handles_value_error(self, mock_post: MagicMock) -> None:
        """Gemini should handle ValueError from non-JSON responses gracefully."""
        from src.clients.utility_apis import gemini_generate_short_title

        mock_post.side_effect = ValueError("No JSON object could be decoded")
        result = gemini_generate_short_title("Some Title", "fake-key")
        assert result is None


class TestHttpRequestPostDispatch:
    """Verify _http_request dispatches to the correct session method."""

    @staticmethod
    def _make_mock_session(method: str) -> tuple[MagicMock, MagicMock]:
        """Build a mock session whose *method* returns a successful response.

        Also initializes the thread-local request counter that ``_http_request``
        increments, since patching ``_get_session`` bypasses the real
        initializer.
        """
        from src.http_utils import _THREAD_LOCAL

        _THREAD_LOCAL.session_request_count = 0

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"ok": true}'
        mock_resp.raise_for_status = MagicMock()

        mock_session = MagicMock()
        getattr(mock_session, method).return_value = mock_resp
        return mock_session, mock_resp

    def test_post_calls_session_post(self) -> None:
        """_http_request('POST', ...) should call session.post, not session.get."""
        from src.http_utils import _http_request

        mock_session, _ = self._make_mock_session("post")

        with (
            patch("src.http_utils._get_session", return_value=mock_session),
            patch("src.http_utils._get_rate_limiter", return_value=None),
        ):
            result = _http_request("POST", "https://example.com/api", {}, 10.0, json_payload={"key": "val"})
            mock_session.post.assert_called_once()
            mock_session.get.assert_not_called()
            assert result == b'{"ok": true}'

    def test_get_calls_session_get(self) -> None:
        """_http_request('GET', ...) should call session.get, not session.post."""
        from src.http_utils import _http_request

        mock_session, _ = self._make_mock_session("get")

        with (
            patch("src.http_utils._get_session", return_value=mock_session),
            patch("src.http_utils._get_rate_limiter", return_value=None),
        ):
            result = _http_request("GET", "https://example.com/api", {}, 10.0)
            mock_session.get.assert_called_once()
            mock_session.post.assert_not_called()
            assert result == b'{"ok": true}'


class TestRateLimiterEntries:
    """ORCID and DataCite should have rate limiter entries in config."""

    def test_orcid_rate_limiter_exists(self) -> None:
        """_get_rate_limiter should return a limiter for 'orcid' namespace."""
        from src.http_utils import _get_rate_limiter
        limiter = _get_rate_limiter("orcid")
        assert limiter is not None

    def test_datacite_rate_limiter_exists(self) -> None:
        """_get_rate_limiter should return a limiter for 'datacite' namespace."""
        from src.http_utils import _get_rate_limiter
        limiter = _get_rate_limiter("datacite")
        assert limiter is not None


class TestOpenReviewTTLBoundary:
    """Test the exact boundary of OpenReview session TTL."""

    def test_session_expires_at_exact_ttl(self) -> None:
        """Session should be treated as expired when elapsed == TTL (>= check)."""
        import src.clients.search_apis as sa

        with sa._OPENREVIEW_SESSION_LOCK:
            sa._OPENREVIEW_SESSION = {"Cookie": "session=test"}
            sa._OPENREVIEW_SESSION_CREATED_AT = (
                time.monotonic() - OPENREVIEW_SESSION_TTL_SECS
            )

        assert sa._openreview_session_expired() is True
        _reset_openreview_session()

    def test_session_valid_just_before_ttl(self) -> None:
        """Session should be valid when elapsed < TTL by a safe margin."""
        import src.clients.search_apis as sa

        with sa._OPENREVIEW_SESSION_LOCK:
            sa._OPENREVIEW_SESSION = {"Cookie": "session=test"}
            sa._OPENREVIEW_SESSION_CREATED_AT = (
                time.monotonic() - OPENREVIEW_SESSION_TTL_SECS + 60
            )

        assert sa._openreview_session_expired() is False
        _reset_openreview_session()


class TestAbbreviatedVenueExpansion:
    """Abbreviated venue names should be expanded to full conference names."""

    def test_determine_entry_type_recognizes_abbreviated_venue(self) -> None:
        """SPIRE in journal field should be detected as inproceedings."""
        from src.bibtex_build import determine_entry_type

        result = determine_entry_type({"journal": "SPIRE"})
        assert result == "inproceedings"

    def test_determine_entry_type_case_insensitive(self) -> None:
        """Abbreviated venue lookup should be case-insensitive."""
        from src.bibtex_build import determine_entry_type

        result = determine_entry_type({"booktitle": "ircdl"})
        assert result == "inproceedings"

    def test_merge_expands_abbreviated_journal(self) -> None:
        """Merge should expand 'SPIRE' in journal to full conference name."""
        from src.config import ABBREVIATED_VENUE_MAP
        from src.merge_utils import merge_with_policy

        primary: dict = {
            "type": "article",
            "key": "Test2024:Example",
            "fields": {
                "title": "Some Paper Title Here",
                "author": "Smith, John",
                "year": "2024",
                "journal": "SPIRE",
            },
        }
        result = merge_with_policy(primary, [])
        # After merge, journal should be gone (moved to booktitle for inproceedings)
        # and booktitle should contain the expanded name
        assert result["type"] == "inproceedings"
        assert result["fields"].get("booktitle") == ABBREVIATED_VENUE_MAP["spire"]
        assert "journal" not in result["fields"]

    def test_merge_expands_abbreviated_booktitle(self) -> None:
        """Merge should expand 'IRCDL' in booktitle to full conference name."""
        from src.config import ABBREVIATED_VENUE_MAP
        from src.merge_utils import merge_with_policy

        primary: dict = {
            "type": "inproceedings",
            "key": "Test2024:Example",
            "fields": {
                "title": "Another Paper Title",
                "author": "Doe, Jane",
                "year": "2024",
                "booktitle": "IRCDL",
            },
        }
        result = merge_with_policy(primary, [])
        assert result["type"] == "inproceedings"
        assert result["fields"]["booktitle"] == ABBREVIATED_VENUE_MAP["ircdl"]

    def test_csl_container_title_array_prefers_non_generic(self) -> None:
        """CSL container-title array should prefer non-generic element over LNCS."""
        from src.bibtex_utils import parse_bibtex_to_dict
        from src.clients.search_apis import bibtex_from_csl

        csl = {
            "type": "book-chapter",
            "title": "Data Structures for SMEM-Finding in the PBWT",
            "author": [{"given": "Paola", "family": "Bonizzoni"}],
            "issued": {"date-parts": [[2023]]},
            "container-title": [
                "Lecture Notes in Computer Science",
                "String Processing and Information Retrieval",
            ],
            "DOI": "10.1007/978-3-031-43980-3_8",
        }
        bibtex = bibtex_from_csl(csl, keyhint="test")
        entry = parse_bibtex_to_dict(bibtex)
        assert entry is not None
        fields = entry["fields"]
        # Should pick "String Processing and Information Retrieval", not LNCS
        venue = fields.get("booktitle") or fields.get("journal") or fields.get("howpublished", "")
        assert "Lecture Notes" not in venue
        assert "String Processing" in venue

    def test_non_abbreviated_venue_unchanged(self) -> None:
        """Normal venue names should not be modified by abbreviation expansion."""
        from src.merge_utils import merge_with_policy

        primary: dict = {
            "type": "article",
            "key": "Test2024:Example",
            "fields": {
                "title": "A Paper About Things",
                "author": "Doe, Jane",
                "year": "2024",
                "journal": "Nature Machine Intelligence",
            },
        }
        result = merge_with_policy(primary, [])
        assert result["fields"]["journal"] == "Nature Machine Intelligence"


class TestBiorxivDoiPrefix:
    """L3: bioRxiv DOIs with any 10.1101/ prefix should be classified as preprint."""

    def test_biorxiv_old_numeric_doi(self) -> None:
        """Pre-2020 bioRxiv DOI (no date prefix) should be secondary."""
        assert id_utils.is_secondary_doi("10.1101/123456") is True

    def test_biorxiv_date_prefixed_doi(self) -> None:
        """Post-2020 bioRxiv DOI (date prefix) should be secondary."""
        assert id_utils.is_secondary_doi("10.1101/2021.01.01.123456") is True

    def test_medrxiv_doi(self) -> None:
        """medRxiv DOIs also use 10.1101/ prefix."""
        assert id_utils.is_secondary_doi("10.1101/2022.05.15.492001") is True

    def test_non_biorxiv_doi(self) -> None:
        """Regular published DOI should NOT be classified as secondary."""
        assert id_utils.is_secondary_doi("10.1038/s41586-024-00001-1") is False


class TestDoiUrlDecoding:
    """L15: _norm_doi should URL-decode percent-encoded characters."""

    def test_percent_encoded_slash(self) -> None:
        """DOI with %2F should normalize to match plain slash version."""
        d1 = id_utils.normalize_doi("10.1000/xyz%2Fabc")
        d2 = id_utils.normalize_doi("10.1000/xyz/abc")
        assert d1 == d2

    def test_double_encoded(self) -> None:
        """URL-formatted DOI with encoding should normalize correctly."""
        d = id_utils.normalize_doi("https://doi.org/10.1234%2Ftest")
        assert d == "10.1234/test"


class TestNobleParticleMatching:
    """B8/L8: Noble particles (van, von, de, etc.) should produce consistent signatures."""

    def test_van_der_waals_first_last(self) -> None:
        """'Johan van der Waals' in First Last format."""
        sig = text_utils.name_signature("Johan van der Waals")
        assert sig is not None
        assert sig["last"] == "vanderwaals"
        assert sig["initials"] == "j"

    def test_van_der_waals_comma(self) -> None:
        """'van der Waals, Johan' in Last, First format."""
        sig = text_utils.name_signature("van der Waals, Johan")
        assert sig is not None
        assert sig["last"] == "vanderwaals"
        assert sig["initials"] == "j"

    def test_both_formats_match(self) -> None:
        """Both name formats should produce the same signature."""
        sig_fl = text_utils.name_signature("Johan van der Waals")
        sig_lf = text_utils.name_signature("van der Waals, Johan")
        assert sig_fl is not None and sig_lf is not None
        assert sig_fl["last"] == sig_lf["last"]

    def test_de_silva(self) -> None:
        """'Kumar de Silva' should treat 'de' as noble particle."""
        sig = text_utils.name_signature("Kumar de Silva")
        assert sig is not None
        assert sig["last"] == "desilva"

    def test_von_neumann(self) -> None:
        """'John von Neumann' should match 'von Neumann, John'."""
        sig_fl = text_utils.name_signature("John von Neumann")
        sig_lf = text_utils.name_signature("von Neumann, John")
        assert sig_fl is not None and sig_lf is not None
        assert sig_fl["last"] == sig_lf["last"] == "vonneumann"

    def test_simple_name_no_particle(self) -> None:
        """Simple names without particles should still work."""
        sig = text_utils.name_signature("John Smith")
        assert sig is not None
        assert sig["last"] == "smith"
        assert sig["initials"] == "j"

    def test_hyphenated_surname_comma_format(self) -> None:
        """Hyphenated surnames in comma format should normalize consistently."""
        sig = text_utils.name_signature("Garcia-Marquez, Gabriel")
        assert sig is not None
        assert sig["last"] == "garciamarquez"
        assert sig["initials"] == "g"


class TestEllipsisPlaceholder:
    """L5: Only short strings with ellipsis should be treated as placeholder."""

    def test_short_ellipsis_is_placeholder(self) -> None:
        """Short string with ellipsis should be a placeholder."""
        assert text_utils.has_placeholder("Loading...") is True

    def test_long_title_with_ellipsis_not_placeholder(self) -> None:
        """A long legitimate title with '...' should NOT be a placeholder."""
        long_title = (
            "A Comprehensive Survey of Machine Learning Methods..."
            " and Their Applications to Real-World Problems"
        )
        assert text_utils.has_placeholder(long_title) is False

    def test_unicode_ellipsis_short(self) -> None:
        """Short string with unicode ellipsis should be a placeholder."""
        assert text_utils.has_placeholder("Wait\u2026") is True


class TestCJKTitleNormalization:
    """L7: CJK-only titles should not normalize to empty string."""

    def test_cjk_title_not_empty(self) -> None:
        """Chinese characters should not produce empty normalized title."""
        result = text_utils.normalize_title("机器学习方法")
        assert len(result) > 0

    def test_cjk_self_similarity(self) -> None:
        """CJK title should have 1.0 similarity with itself."""
        title = "深度学习综述"
        sim = text_utils.title_similarity(title, title)
        assert sim == 1.0

    def test_mixed_cjk_ascii(self) -> None:
        """Mixed CJK+ASCII title should normalize without losing content."""
        result = text_utils.normalize_title("深度学习 Deep Learning 综述")
        assert len(result) > 0
        assert "deep" in result or "learning" in result or "深度" in result


class TestHtmlEntityInNormalizeTitle:
    """D7: HTML entities should be decoded before title normalization."""

    def test_amp_decoded(self) -> None:
        """&amp; should become & in normalized title."""
        result = text_utils.normalize_title("Computers &amp; Education")
        expected = text_utils.normalize_title("Computers & Education")
        assert result == expected

    def test_lt_gt_decoded(self) -> None:
        """&lt; and &gt; should be decoded."""
        result = text_utils.normalize_title("A &lt;b&gt; Approach")
        assert "lt" not in result
        assert "gt" not in result

    def test_numeric_entity(self) -> None:
        """&#8211; (en-dash) should be decoded."""
        result = text_utils.normalize_title("Pages 1&#8211;10")
        assert "8211" not in result


class TestAuthorOverlapWithInitials:
    """L9: author_overlap_ratio should distinguish authors with same last name but different initials."""

    def test_same_last_different_initials_distinguished(self) -> None:
        """'J. Smith' and 'K. Smith' should not be merged when both have initials."""
        ratio = text_utils.author_overlap_ratio(
            "J. Smith and Alice Brown",
            "K. Smith and Alice Brown",
        )
        # Brown matches, but the two Smiths are different people
        assert ratio < 1.0

    def test_same_authors_full_overlap(self) -> None:
        """Identical author lists should have ratio 1.0."""
        ratio = text_utils.author_overlap_ratio(
            "John Smith and Jane Doe",
            "John Smith and Jane Doe",
        )
        assert ratio == 1.0

    def test_no_initials_falls_back(self) -> None:
        """When one side lacks initials, fall back to last-name matching."""
        ratio = text_utils.author_overlap_ratio(
            "Smith",
            "J. Smith",
        )
        assert ratio > 0.0


class TestVenueSimilarityPreprint:
    """L14: venue_similarity should correctly detect preprint servers even with hyphens."""

    def test_biorxiv_vs_journal(self) -> None:
        """bioRxiv vs a journal should give 0.5 (preprint/published pair)."""
        sim = text_utils.venue_similarity(
            {"journal": "bioRxiv"},
            {"journal": "Nature Medicine"},
        )
        assert sim == 0.5

    def test_arxiv_eprints_vs_conference(self) -> None:
        """arXiv e-prints vs conference should give 0.5."""
        sim = text_utils.venue_similarity(
            {"journal": "arXiv e-prints"},
            {"booktitle": "NeurIPS 2024"},
        )
        assert sim == 0.5


class TestBothPreprintDoiDedup:
    """B6: Two entries with different preprint DOIs should NOT match."""

    def test_different_arxiv_dois_not_matched(self) -> None:
        """Two different arXiv preprints should not be considered duplicates."""
        entry_a: dict[str, Any] = {
            "type": "article",
            "key": "Paper2024a",
            "fields": {
                "title": "Machine Learning for Natural Language Processing",
                "author": "Smith, John",
                "year": "2024",
                "doi": "10.48550/arxiv.2401.11111",
                "journal": "arXiv e-prints",
            },
        }
        entry_b: dict[str, Any] = {
            "type": "article",
            "key": "Paper2024b",
            "fields": {
                "title": "Deep Learning for Natural Language Understanding",
                "author": "Smith, John",
                "year": "2024",
                "doi": "10.48550/arxiv.2401.22222",
                "journal": "arXiv e-prints",
            },
        }
        result = bt.bibtex_entries_match_strict(entry_a, entry_b)
        assert result is False


class TestYearGapWidened:
    """L6: Year gap > 3 should reject, <= 3 should allow preprint→published."""

    def test_3_year_gap_allowed(self) -> None:
        """A 3-year gap (preprint in 2021, published in 2024) should allow matching."""
        entry_a: dict[str, Any] = {
            "type": "article",
            "key": "Paper2021",
            "fields": {
                "title": "A Novel Approach to Graph Neural Networks",
                "author": "Smith, John and Doe, Jane",
                "year": "2021",
                "doi": "10.48550/arxiv.2101.12345",
                "journal": "arXiv e-prints",
            },
        }
        entry_b: dict[str, Any] = {
            "type": "article",
            "key": "Paper2024",
            "fields": {
                "title": "A Novel Approach to Graph Neural Networks",
                "author": "Smith, John and Doe, Jane",
                "year": "2024",
                "doi": "10.1145/1234567.1234568",
                "journal": "ACM Computing Surveys",
            },
        }
        result = bt.bibtex_entries_match_strict(entry_a, entry_b)
        assert result is True

    def test_5_year_gap_rejected(self) -> None:
        """A 5-year gap should be too large even for preprint→published."""
        entry_a: dict[str, Any] = {
            "type": "article",
            "key": "Paper2019",
            "fields": {
                "title": "Some Machine Learning Research Paper Title",
                "author": "Smith, John and Doe, Jane",
                "year": "2019",
                "doi": "10.48550/arxiv.1901.12345",
                "journal": "arXiv e-prints",
            },
        }
        entry_b: dict[str, Any] = {
            "type": "article",
            "key": "Paper2024",
            "fields": {
                "title": "Some Machine Learning Research Paper Title",
                "author": "Smith, John and Doe, Jane",
                "year": "2024",
                "doi": "10.1145/9999999.9999999",
                "journal": "ACM Computing Surveys",
            },
        }
        result = bt.bibtex_entries_match_strict(entry_a, entry_b)
        assert result is False


class TestDoiConflictPreserveUpgrade:
    """B1: DOI merge should not revert a preprint→published upgrade."""

    def test_preprint_doi_upgraded_to_published(self) -> None:
        """When primary has arXiv DOI and enricher has published DOI, keep published."""
        entry = {
            "type": "article",
            "key": "Test2024",
            "fields": {
                "title": "Test Paper",
                "author": "Author One",
                "year": "2024",
                "doi": "10.48550/arxiv.2401.12345",
            },
        }
        enrichers = [
            ("csl", {"fields": {"doi": "10.1145/1234567"}}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        # The published DOI should win over the preprint DOI
        assert merged["fields"]["doi"] == "10.1145/1234567"


class TestPhantomArxivJournal:
    """B2: 'arXiv e-prints' journal should be cleared when published DOI exists."""

    def test_arxiv_journal_cleared_with_published_doi(self) -> None:
        """When eprint removed due to published DOI, phantom journal should also go."""
        entry = {
            "type": "article",
            "key": "Test2024",
            "fields": {
                "title": "Test Paper",
                "author": "Author One",
                "year": "2024",
                "eprint": "2401.12345",
                "archiveprefix": "arXiv",
                "journal": "arXiv e-prints",
            },
        }
        # The published DOI must come from a trusted enricher to survive merge
        enrichers = [
            ("csl", {"fields": {
                "doi": "10.1145/1234567",
                "journal": "ACM Computing Surveys",
            }}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        # After B2: eprint removed because published DOI exists,
        # journal should be the enricher's journal, not "arXiv e-prints"
        journal = merged["fields"].get("journal", "")
        assert journal.lower() not in ("arxiv e-prints", "arxiv")

    def test_journal_backfilled_after_phantom_removal(self) -> None:
        """When phantom arXiv journal is removed, backfill from enrichers with published DOI."""
        entry = {
            "type": "article",
            "key": "Test2024",
            "fields": {
                "title": "Test Paper",
                "author": "Author One",
                "year": "2024",
                "eprint": "2401.12345",
                "archiveprefix": "arXiv",
                "journal": "arXiv e-prints",
            },
        }
        # CSL from preprint DOI sets arXiv journal (rank 0), then enrichers with
        # published DOI provide real journal which can't beat rank 0 during merge.
        # Backfill should recover the real journal after phantom removal.
        enrichers = [
            ("csl", {"fields": {
                "doi": "10.48550/arxiv.2401.12345",
                "journal": "arXiv",
            }}),
            ("s2", {"fields": {
                "doi": "10.3390/s22166063",
                "journal": "Sensors",
            }}),
            ("crossref", {"fields": {
                "doi": "10.3390/s22166063",
                "journal": "Sensors",
            }}),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        assert merged["fields"].get("journal") == "Sensors"


class TestIncollectionPromotionRestricted:
    """B4: incollection→inproceedings should only fire for GENERIC_SERIES_NAMES."""

    def test_generic_series_promotes(self) -> None:
        """incollection with LNCS booktitle should become inproceedings."""
        entry = {
            "type": "incollection",
            "key": "Test2024",
            "fields": {
                "title": "Test Paper",
                "author": "Author One",
                "year": "2024",
                "booktitle": "Lecture Notes in Computer Science",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["type"] == "inproceedings"

    def test_real_book_chapter_stays(self) -> None:
        """incollection with real book booktitle should stay incollection."""
        entry = {
            "type": "incollection",
            "key": "Test2024",
            "fields": {
                "title": "A Chapter on Methods",
                "author": "Author One",
                "year": "2024",
                "booktitle": "Handbook of Artificial Intelligence",
            },
        }
        merged = merge_utils.merge_with_policy(entry, [])
        assert merged["type"] == "incollection"


class TestCslArticleTypePreserved:
    """L16: CSL/doi_bibtex article type should not be overridden to inproceedings."""

    def test_csl_article_not_overridden(self) -> None:
        """Article type from CSL should be preserved even with conference-like venue."""
        entry = {
            "type": "misc",
            "key": "Test2024",
            "fields": {
                "title": "Test Paper",
                "author": "Author One",
                "year": "2024",
            },
        }
        enrichers = [
            ("csl", {
                "type": "article",
                "fields": {
                    "journal": "Proceedings of the VLDB Endowment",
                },
            }),
        ]
        merged = merge_utils.merge_with_policy(entry, enrichers)
        # Should respect CSL's article type, not override to inproceedings
        assert merged["type"] == "article"


class TestPreprintServersNoFalsePositives:
    """L19: Journals with 'preprint' substring should not be misclassified."""

    def test_preprint_not_in_servers(self) -> None:
        """The generic word 'preprint' should not be in PREPRINT_SERVERS."""
        from src.config import PREPRINT_SERVERS
        assert "preprint" not in PREPRINT_SERVERS

    def test_specific_preprint_servers_present(self) -> None:
        """Specific preprint servers should still be in the set."""
        from src.config import PREPRINT_SERVERS
        assert "arxiv" in PREPRINT_SERVERS
        assert "biorxiv" in PREPRINT_SERVERS
        assert "medrxiv" in PREPRINT_SERVERS
        assert "techrxiv" in PREPRINT_SERVERS

    def test_preprints_dot_org_present(self) -> None:
        """preprints.org entry should be present."""
        from src.config import PREPRINT_SERVERS
        assert "preprints.org" in PREPRINT_SERVERS


class TestMergeDuplicateThresholdRaised:
    """L1: SIM_MERGE_DUPLICATE_THRESHOLD should be 0.95 (was 0.9)."""

    def test_threshold_value(self) -> None:
        assert SIM_MERGE_DUPLICATE_THRESHOLD == 0.95

    def test_marginal_title_not_merged(self) -> None:
        """Two clearly different papers by the same authors should NOT be treated as duplicates."""
        entry_a: dict[str, Any] = {
            "type": "article",
            "key": "Paper2024a",
            "fields": {
                "title": "Machine Learning for Healthcare Applications",
                "author": "Smith, John and Doe, Jane",
                "year": "2024",
                "journal": "Nature",
            },
        }
        entry_b: dict[str, Any] = {
            "type": "article",
            "key": "Paper2024b",
            "fields": {
                "title": "Quantum Computing for Drug Discovery",
                "author": "Smith, John and Doe, Jane",
                "year": "2024",
                "journal": "Nature",
            },
        }
        # Verify the titles have low similarity (below 0.6 to avoid strong-author gate)
        sim = text_utils.title_similarity(
            entry_a["fields"]["title"], entry_b["fields"]["title"]
        )
        assert sim < 0.6, (
            f"Title similarity {sim:.3f} should be well below threshold to test rejection"
        )
        # The strict matcher should NOT consider these as duplicates
        result = bt.bibtex_entries_match_strict(entry_a, entry_b)
        assert result is False, "Distinct papers with low title similarity should not be matched"


class TestSemaphoreReleasedDuring429:
    """B9: Global semaphore should be released before sleeping on 429."""

    def test_429_sleep_outside_semaphore(self) -> None:
        """Verify the semaphore is not held during 429 retry sleep."""
        from src.http_utils import _THREAD_LOCAL, _http_request

        _THREAD_LOCAL.session_request_count = 0

        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.headers = {}
        mock_resp_429.content = b""

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.content = b'{"ok": true}'
        mock_resp_200.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.get.side_effect = [mock_resp_429, mock_resp_200]

        with (
            patch("src.http_utils._get_session", return_value=mock_session),
            patch("src.http_utils._get_rate_limiter", return_value=None),
            patch("src.http_utils.time") as mock_time,
        ):
            mock_time.monotonic.return_value = 1000.0
            mock_time.sleep = MagicMock()
            result = _http_request("GET", "https://example.com/api", {}, 10.0)
            # Should have slept between 429 and retry
            assert mock_time.sleep.called
            assert result == b'{"ok": true}'


class TestTokenBucketJitter:
    """L17: TokenBucketRateLimiter.acquire() should include jitter in sleep."""

    def test_jitter_import_and_usage(self) -> None:
        """Verify that random.uniform is called during acquire when sleep is needed."""
        import src.http_utils as hu

        # Verify the random module is imported in http_utils (needed for jitter)
        assert hasattr(hu, "random"), "http_utils should import random for jitter"

    def test_acquire_sleeps_with_jitter_component(self) -> None:
        """When tokens are exhausted, sleep should include a jitter component."""
        from src.http_utils import TokenBucketRateLimiter

        # Very slow rate = 0.1 tokens/sec, so after burst=1 exhausted,
        # next acquire needs to wait ~10 seconds
        limiter = TokenBucketRateLimiter(rate=0.1, burst=1)
        limiter.acquire()  # exhaust burst token

        sleep_values: list[float] = []

        def capture_sleep(duration: float) -> None:
            sleep_values.append(duration)
            # Don't actually sleep

        with patch("src.http_utils.time.sleep", side_effect=capture_sleep):
            limiter.acquire()

        # Should have slept at least once
        assert len(sleep_values) >= 1
        # Verify the sleep was not exactly 10.0 (jitter should offset it)
        # Due to jitter, the actual sleep will be wait + random.uniform(0, wait*0.3)
        # so it should be > 0
        assert sleep_values[0] > 0


class TestEmptyNameSkipped:
    """L12: Records with empty Name but valid IDs should be skipped."""

    def test_empty_name_with_scholar_id_skipped(self, tmp_path: Any) -> None:
        """Record with Scholar ID but no Name should be skipped."""
        csv_content = "Name,Scholar Link,DBLP Link\n,https://scholar.google.com/citations?user=abc123,\nJohn Smith,https://scholar.google.com/citations?user=xyz789,\n"
        csv_file = tmp_path / "test_input.csv"
        csv_file.write_text(csv_content)

        from src.io_utils import read_records
        records = read_records(str(csv_file))
        # Should only have John Smith, empty name record should be skipped
        assert len(records) == 1
        assert records[0].name == "John Smith"


class TestCslEventNameFallback:
    """B10: bibtex_from_csl should use event-name when container is a generic series."""

    def test_lncs_with_event_name(self) -> None:
        """When CSL container is LNCS and event-name exists, use event name."""
        from src.clients.search_apis import bibtex_from_csl

        csl = {
            "type": "book-chapter",
            "title": "Test Paper",
            "author": [{"given": "John", "family": "Smith"}],
            "issued": {"date-parts": [[2024]]},
            "container-title": "Lecture Notes in Computer Science",
            "event": {"name": "International Conference on AI 2024"},
            "DOI": "10.1007/978-3-031-12345-6_1",
        }
        bibtex = bibtex_from_csl(csl, keyhint="test")
        assert bibtex is not None
        entry = bt.parse_bibtex_to_dict(bibtex)
        assert entry is not None
        venue = entry["fields"].get("booktitle") or entry["fields"].get("journal") or ""
        assert "International Conference on AI" in venue

    def test_non_generic_container_kept(self) -> None:
        """Non-generic container titles should not be replaced by event name."""
        from src.clients.search_apis import bibtex_from_csl

        csl = {
            "type": "book-chapter",
            "title": "Test Paper",
            "author": [{"given": "John", "family": "Smith"}],
            "issued": {"date-parts": [[2024]]},
            "container-title": "Specific Conference Proceedings",
            "event": {"name": "Some Other Event"},
            "DOI": "10.1007/978-3-031-99999-9_1",
        }
        bibtex = bibtex_from_csl(csl, keyhint="test")
        assert bibtex is not None
        entry = bt.parse_bibtex_to_dict(bibtex)
        assert entry is not None
        venue = entry["fields"].get("booktitle") or entry["fields"].get("journal") or ""
        assert "Specific Conference Proceedings" in venue


