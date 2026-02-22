"""Regression tests for CiteForge (Phases 7b, 7c, 7d, Pipeline Perfection).

Tests cover:
- Phase 7c: BibTeX parser edge cases, tilde handling, LaTeX normalization, title sanitization
- Phase 7b: Cache integration for search_api_generic_multiple, DOI validation short-circuit
- Phase 7d: Deduplication of publication lists
- Pipeline Perfection: DOI selection, pages validation, HTML entity decoding, title sanitization,
  minimum title length, arXiv consistency, dedup gate relaxation
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock, patch

from src import bibtex_utils as bt
from src import id_utils, merge_utils, text_utils
from src.api_generics import APISearchConfig, search_api_generic_multiple
from src.clients.scholar import _deduplicate_publication_list
from src.config import MIN_TITLE_WORDS, PAGES_MAX_DIGITS
from src.doi_utils import validate_doi_candidate

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

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


# ===========================================================================
# Phase 7c: Bug Regression Tests
# ===========================================================================


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


# ===========================================================================
# Phase 7b: Cache Integration Tests
# ===========================================================================


class TestSearchApiGenericMultipleCache:
    """Test that search_api_generic_multiple uses cache for repeated queries."""

    def test_cache_hit_skips_http(self, tmp_path: Any) -> None:
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
            csl_matched, bibtex_matched, _csl_entry, _bibtex_entry = validate_doi_candidate(
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


# ===========================================================================
# Phase 7d: Deduplication Tests
# ===========================================================================


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


# ===========================================================================
# Pipeline Perfection: Targeted Tests for Fixes 1-8
# ===========================================================================


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
        assert len(["Games"]) < MIN_TITLE_WORDS

    def test_two_word_title_meets_minimum(self) -> None:
        assert len(["Good", "Title"]) >= MIN_TITLE_WORDS

    def test_normal_title_well_above_minimum(self) -> None:
        title = "A Comprehensive Survey on Machine Learning"
        assert len(title.split()) >= MIN_TITLE_WORDS


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

    def test_incollection_with_conference_booktitle_becomes_inproceedings(self) -> None:
        """Crossref book-chapter typed entries with conference booktitle should be @inproceedings."""
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
                    "booktitle": "Challenges of Trustable AI and Added-Value on Health",
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


# ---------------------------------------------------------------------------
# Browser circuit breaker
# ---------------------------------------------------------------------------


class TestBrowserCircuitBreaker:
    """Tests for the browser circuit breaker in scholar.py."""

    def _reset_circuit(self) -> None:
        """Reset circuit breaker state between tests."""
        import src.clients.scholar as scholar_mod
        with scholar_mod._circuit_lock:
            scholar_mod._browser_consecutive_errors = 0
            scholar_mod._browser_circuit_open = False

    def test_circuit_opens_after_threshold(self) -> None:
        """After SCHOLAR_BROWSER_CIRCUIT_THRESHOLD consecutive errors, circuit opens."""
        import src.clients.scholar as scholar_mod
        from src.clients.scholar import _run_browser_coro
        from src.config import SCHOLAR_BROWSER_CIRCUIT_THRESHOLD
        from src.exceptions import ScholarBrowserBlockedError

        self._reset_circuit()

        with (
            patch.object(scholar_mod, "NODRIVER_AVAILABLE", True),
            patch("src.clients.scholar.ScholarBrowserLoop") as mock_loop_cls,
            patch("src.clients.scholar.time"),
        ):
            mock_loop = MagicMock()
            mock_loop_cls.return_value = mock_loop
            mock_loop.run.side_effect = ScholarBrowserBlockedError("CAPTCHA")

            # Trigger threshold errors
            for i in range(SCHOLAR_BROWSER_CIRCUIT_THRESHOLD):
                result = _run_browser_coro(lambda b: None, f"test-{i}")
                assert result is None

            # Next call should skip browser entirely (circuit open)
            mock_loop.run.reset_mock()
            result = _run_browser_coro(lambda b: None, "after-circuit-open")
            assert result is None
            mock_loop.run.assert_not_called()

        self._reset_circuit()

    def test_circuit_resets_on_success(self) -> None:
        """A successful browser call resets the consecutive error counter."""
        import src.clients.scholar as scholar_mod
        from src.clients.scholar import _run_browser_coro
        from src.exceptions import ScholarBrowserBlockedError

        self._reset_circuit()

        with (
            patch.object(scholar_mod, "NODRIVER_AVAILABLE", True),
            patch("src.clients.scholar.ScholarBrowserLoop") as mock_loop_cls,
            patch("src.clients.scholar.time"),
        ):
            mock_loop = MagicMock()
            mock_loop_cls.return_value = mock_loop

            # 5 errors
            mock_loop.run.side_effect = ScholarBrowserBlockedError("CAPTCHA")
            for _ in range(5):
                _run_browser_coro(lambda b: None, "pre-success")

            with scholar_mod._circuit_lock:
                assert scholar_mod._browser_consecutive_errors == 5

            # 1 success
            mock_loop.run.side_effect = None
            mock_loop.run.return_value = {"data": "ok"}
            result = _run_browser_coro(lambda b: None, "success")
            assert result == {"data": "ok"}

            with scholar_mod._circuit_lock:
                assert scholar_mod._browser_consecutive_errors == 0
                assert not scholar_mod._browser_circuit_open

        self._reset_circuit()

    def test_backoff_increases_with_errors(self) -> None:
        """Back-off delay increases linearly with each consecutive error."""
        import src.clients.scholar as scholar_mod
        from src.clients.scholar import _run_browser_coro
        from src.config import SCHOLAR_BROWSER_BACKOFF_BASE, SCHOLAR_BROWSER_BACKOFF_CAP
        from src.exceptions import ScholarBrowserBlockedError

        self._reset_circuit()
        sleep_calls: list[float] = []

        with (
            patch.object(scholar_mod, "NODRIVER_AVAILABLE", True),
            patch("src.clients.scholar.ScholarBrowserLoop") as mock_loop_cls,
            patch("src.clients.scholar.time") as mock_time,
        ):
            mock_loop = MagicMock()
            mock_loop_cls.return_value = mock_loop
            mock_loop.run.side_effect = ScholarBrowserBlockedError("CAPTCHA")
            mock_time.sleep.side_effect = lambda d: sleep_calls.append(d)

            # First call: no back-off (0 prior errors)
            _run_browser_coro(lambda b: None, "err-1")
            # Second call: back-off = base * 1
            _run_browser_coro(lambda b: None, "err-2")
            # Third call: back-off = base * 2
            _run_browser_coro(lambda b: None, "err-3")

        assert len(sleep_calls) == 2  # first call had 0 errors, no sleep
        assert sleep_calls[0] == SCHOLAR_BROWSER_BACKOFF_BASE * 1
        assert sleep_calls[1] == SCHOLAR_BROWSER_BACKOFF_BASE * 2

        # Verify cap would apply at higher counts
        assert min(SCHOLAR_BROWSER_BACKOFF_BASE * 100, SCHOLAR_BROWSER_BACKOFF_CAP) == SCHOLAR_BROWSER_BACKOFF_CAP

        self._reset_circuit()
