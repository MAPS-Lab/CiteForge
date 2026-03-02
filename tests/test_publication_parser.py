"""Tests for src.publication_parser — SerpAPI publication string parsing."""

from __future__ import annotations

import pytest

from src.publication_parser import parse_publication_string


class TestJournalPatterns:
    """Journal strings with volume, issue, and/or pages."""

    def test_vol_issue_pages(self) -> None:
        r = parse_publication_string("ACM Computing Surveys 56 (1), 1-34, 2023")
        assert r is not None
        assert r.venue_name == "ACM Computing Surveys"
        assert r.venue_type == "journal"
        assert r.volume == "56"
        assert r.issue == "1"
        assert r.pages == "1-34"
        assert r.year == 2023
        assert r.confidence >= 0.9

    def test_vol_pages_no_issue(self) -> None:
        r = parse_publication_string("IEEE Access 14, 9506-9531, 2026")
        assert r is not None
        assert r.venue_name == "IEEE Access"
        assert r.volume == "14"
        assert r.pages == "9506-9531"
        assert r.year == 2026
        assert r.confidence >= 0.85

    def test_vol_article_id(self) -> None:
        r = parse_publication_string("JMIR Infodemiology 6, e77783, 2026")
        assert r is not None
        assert r.venue_name == "JMIR Infodemiology"
        assert r.volume == "6"
        assert r.pages == "e77783"
        assert r.year == 2026

    def test_vol_issue_pages_with_spaces(self) -> None:
        r = parse_publication_string("Journal of Ocean Technology 16 (3), 57-63, 2021")
        assert r is not None
        assert r.venue_name == "Journal of Ocean Technology"
        assert r.volume == "16"
        assert r.issue == "3"
        assert r.pages == "57-63"
        assert r.year == 2021
        assert r.confidence >= 0.9

    def test_issue_range(self) -> None:
        """Issue number given as a range like (1-2)."""
        r = parse_publication_string("Some Journal 10 (1-2), 100-200, 2020")
        assert r is not None
        assert r.issue == "1-2"


class TestConferencePatterns:
    """Conference / proceedings strings."""

    def test_conference_with_pages(self) -> None:
        r = parse_publication_string(
            "Proceedings of the 2020 genetic and evolutionary computation conference, 1-8, 2020"
        )
        assert r is not None
        assert r.venue_type == "conference"
        assert r.pages == "1-8"
        assert r.year == 2020
        assert r.confidence >= 0.7

    def test_workshop_no_pages(self) -> None:
        r = parse_publication_string(
            "NeurIPS 2022 Workshop: Tackling Climate Change with ML, 2022"
        )
        assert r is not None
        assert r.venue_type == "conference"
        assert r.year == 2022
        assert r.confidence >= 0.6

    def test_symposium(self) -> None:
        r = parse_publication_string(
            "2007 IEEE International Parallel and Distributed Processing Symposium, 1-8, 2007"
        )
        assert r is not None
        assert r.venue_type == "conference"
        assert r.pages == "1-8"

    def test_truncated_conference(self) -> None:
        r = parse_publication_string(
            "10th Conference of the European Chapter of the Association for Computational \u2026, 2003"
        )
        assert r is not None
        assert r.venue_type == "conference"
        assert r.year == 2003


class TestPreprintPatterns:
    """arXiv, bioRxiv, and other preprint patterns."""

    def test_arxiv_with_id(self) -> None:
        r = parse_publication_string("arXiv preprint arXiv:2407.18753, 2024")
        assert r is not None
        assert r.venue_type == "preprint"
        assert r.arxiv_id == "2407.18753"
        assert r.year == 2024
        assert r.confidence >= 0.9

    def test_arxiv_short(self) -> None:
        r = parse_publication_string("arXiv preprint arXiv:2301.12345, 2023")
        assert r is not None
        assert r.arxiv_id == "2301.12345"

    def test_biorxiv_with_doi(self) -> None:
        r = parse_publication_string("BioRxiv, 10.1101/2025.05.22.655348, 2025")
        assert r is not None
        assert r.venue_type == "preprint"
        assert r.doi_fragment == "10.1101/2025.05.22.655348"
        assert r.year == 2025

    def test_medrxiv_with_doi(self) -> None:
        r = parse_publication_string("medRxiv, 10.1101/2024.01.01.000001, 2024")
        assert r is not None
        assert r.venue_type == "preprint"
        assert r.doi_fragment == "10.1101/2024.01.01.000001"


class TestPatentPattern:
    """US Patent strings."""

    def test_patent(self) -> None:
        r = parse_publication_string("US Patent 10,901,713, 2021")
        assert r is not None
        assert r.venue_type == "patent"
        assert r.patent_number == "10,901,713"
        assert r.year == 2021
        assert r.confidence >= 0.85

    def test_patent_app(self) -> None:
        r = parse_publication_string("US Patent App. 16/234,567, 2019")
        assert r is not None
        assert r.venue_type == "patent"


class TestLowConfidenceAndEdgeCases:
    """Publisher-only, ambiguous, and edge-case strings."""

    def test_publisher_only_low_confidence(self) -> None:
        r = parse_publication_string("Sage, 2021")
        assert r is not None
        assert r.venue_type == "publisher"
        assert r.confidence < 0.5

    def test_empty_string(self) -> None:
        assert parse_publication_string("") is None

    def test_none_input(self) -> None:
        assert parse_publication_string(None) is None

    def test_whitespace_only(self) -> None:
        assert parse_publication_string("   ") is None

    def test_frozen_dataclass(self) -> None:
        """ParsedPublication is immutable."""
        r = parse_publication_string("IEEE Access 14, 1-10, 2020")
        assert r is not None
        with pytest.raises(AttributeError):
            r.venue_name = "changed"  # type: ignore[misc]

    def test_book_chapter_pages(self) -> None:
        """Book chapter with page range is parsed with moderate confidence."""
        r = parse_publication_string(
            "Handbook of Evolutionary Machine Learning, 205-243, 2023"
        )
        assert r is not None
        assert r.pages == "205-243"
        assert r.year == 2023
        # Not a conference, so lower confidence
        assert r.confidence < 0.85

    def test_three_word_venue_no_volume(self) -> None:
        """Longer venue name without volume gets moderate confidence."""
        r = parse_publication_string("International Journal of Something, 2022")
        assert r is not None
        assert r.confidence >= 0.4


class TestConfidenceThresholds:
    """Verify confidence levels align with Tier 1/2 thresholds."""

    def test_tier1_eligible_journal(self) -> None:
        """Journal with vol/issue/pages should exceed Tier 1 threshold (0.5)."""
        r = parse_publication_string("Nature 600 (1), 100-110, 2021")
        assert r is not None
        assert r.confidence >= 0.5
        assert r.venue_type in ("journal", "conference")

    def test_tier2_eligible_journal(self) -> None:
        """Journal with vol/pages should exceed Tier 2 threshold (0.7)."""
        r = parse_publication_string("Science 370, 200-210, 2020")
        assert r is not None
        assert r.confidence >= 0.7

    def test_publisher_below_tier1(self) -> None:
        """Publisher-only should be below Tier 1 threshold."""
        r = parse_publication_string("Springer, 2019")
        assert r is not None
        assert r.confidence < 0.5
