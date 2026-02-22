"""Tests for browser-based Scholar scraping helpers and facade fallback logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.clients.scholar_browser import (
    _extract_bibtex_from_text,
    _extract_citation_id_from_href,
    _parse_year_text,
)
from src.exceptions import ScholarBrowserBlockedError


class TestExtractBibtexFromText:
    def test_simple_article(self) -> None:
        text = '@article{key, author={Smith}, title={A Paper}}'
        assert _extract_bibtex_from_text(text) == text

    def test_nested_braces(self) -> None:
        bib = '@inproceedings{key, title={An {LSTM} Approach to {NLP}}}'
        result = _extract_bibtex_from_text(bib)
        assert result == bib

    def test_surrounded_by_html(self) -> None:
        html = '<html><body><pre>@article{key, author={A}}</pre></body></html>'
        result = _extract_bibtex_from_text(html)
        assert result == "@article{key, author={A}}"

    def test_no_bibtex(self) -> None:
        assert _extract_bibtex_from_text("no bibtex here") is None

    def test_empty_string(self) -> None:
        assert _extract_bibtex_from_text("") is None

    def test_unclosed_braces(self) -> None:
        assert _extract_bibtex_from_text("@article{key, author={Smith}") is None

    def test_css_at_media_skipped(self) -> None:
        """Bare @media should not be treated as BibTeX; only @word{ patterns match."""
        text = '@media screen { body { color: red; } } <pre>@article{k, t={X}}</pre>'
        assert _extract_bibtex_from_text(text) is not None

    def test_multiline_bibtex(self) -> None:
        bib = "@article{key,\n  author = {Smith, John},\n  title = {A Paper},\n  year = {2024}\n}"
        result = _extract_bibtex_from_text(bib)
        assert result == bib

    def test_at_sign_in_email_no_match(self) -> None:
        """Bare @ without word+{ should return None."""
        assert _extract_bibtex_from_text("user@example.com") is None


class TestParseYearText:
    def test_clean_year(self) -> None:
        assert _parse_year_text("2024") == 2024

    def test_whitespace(self) -> None:
        assert _parse_year_text("  2024  ") == 2024

    def test_empty(self) -> None:
        assert _parse_year_text("") == 0

    def test_embedded_year(self) -> None:
        assert _parse_year_text("Published 2024") == 2024

    def test_no_year(self) -> None:
        assert _parse_year_text("N/A") == 0

    def test_multiple_years(self) -> None:
        assert _parse_year_text("2023/2024") == 2023

    def test_short_number_not_year(self) -> None:
        """Three-digit numbers should not be treated as years."""
        assert _parse_year_text("123") == 123

    def test_year_in_text(self) -> None:
        assert _parse_year_text("vol. 2021, pp. 1-5") == 2021


class TestExtractCitationIdFromHref:
    def test_standard_href(self) -> None:
        href = "/citations?user=ABC123&citation_for_view=ABC123:YYYYYYY"
        assert _extract_citation_id_from_href(href) == "YYYYYYY"

    def test_full_url(self) -> None:
        href = "https://scholar.google.com/citations?user=ABC&citation_for_view=ABC:XYZ789"
        assert _extract_citation_id_from_href(href) == "XYZ789"

    def test_no_colon(self) -> None:
        href = "/citations?citation_for_view=ABCDEF"
        assert _extract_citation_id_from_href(href) == "ABCDEF"

    def test_missing_param(self) -> None:
        href = "/citations?user=ABC"
        assert _extract_citation_id_from_href(href) == ""

    def test_empty_href(self) -> None:
        assert _extract_citation_id_from_href("") == ""

    def test_multiple_colons(self) -> None:
        href = "/citations?citation_for_view=ABC:DEF:GHI"
        assert _extract_citation_id_from_href(href) == "DEF:GHI"


class TestScholarBrowserBlockedError:
    def test_is_runtime_error(self) -> None:
        assert isinstance(ScholarBrowserBlockedError("msg"), RuntimeError)

    def test_message_preserved(self) -> None:
        err = ScholarBrowserBlockedError("CAPTCHA detected")
        assert str(err) == "CAPTCHA detected"


class TestFacadeFallback:
    """Test browser-first, SerpAPI-fallback behavior in scholar.py facades."""

    @patch("src.clients.scholar.response_cache")
    @patch("src.clients.scholar.NODRIVER_AVAILABLE", False)
    @patch("src.clients.scholar._serpapi_fetch_author_publications")
    def test_no_nodriver_uses_serpapi(self, mock_serpapi: MagicMock, mock_cache: MagicMock) -> None:
        from src.clients.scholar import fetch_author_publications

        mock_cache.get.return_value = None
        mock_serpapi.return_value = {"articles": [{"title": "Test"}], "search_metadata": {"status": "Success"}}
        result = fetch_author_publications("key", "author_a")
        mock_serpapi.assert_called_once()
        assert result["articles"][0]["title"] == "Test"

    @patch("src.clients.scholar.response_cache")
    @patch("src.clients.scholar._run_browser_coro")
    @patch("src.clients.scholar._serpapi_fetch_author_publications")
    def test_browser_success_skips_serpapi(
        self, mock_serpapi: MagicMock, mock_browser: MagicMock, mock_cache: MagicMock,
    ) -> None:
        from src.clients.scholar import fetch_author_publications

        mock_cache.get.return_value = None
        mock_browser.return_value = {
            "articles": [{"title": "Browser"}],
            "search_metadata": {"status": "Success", "source": "browser"},
        }
        result = fetch_author_publications("key", "author_b")
        mock_serpapi.assert_not_called()
        assert result["articles"][0]["title"] == "Browser"

    @patch("src.clients.scholar.response_cache")
    @patch("src.clients.scholar._run_browser_coro")
    @patch("src.clients.scholar._serpapi_fetch_author_publications")
    def test_browser_failure_falls_back(
        self, mock_serpapi: MagicMock, mock_browser: MagicMock, mock_cache: MagicMock,
    ) -> None:
        from src.clients.scholar import fetch_author_publications

        mock_cache.get.return_value = None
        mock_browser.return_value = None  # browser failed
        mock_serpapi.return_value = {"articles": [{"title": "SerpAPI"}], "search_metadata": {"status": "Success"}}
        result = fetch_author_publications("key", "author_c")
        mock_serpapi.assert_called_once()
        assert result["articles"][0]["title"] == "SerpAPI"


class TestRunBrowserCoro:
    """Test _run_browser_coro error handling."""

    @patch("src.clients.scholar.NODRIVER_AVAILABLE", False)
    def test_returns_none_when_unavailable(self) -> None:
        from src.clients.scholar import _run_browser_coro

        result = _run_browser_coro(lambda b: b, "test")
        assert result is None

    @patch("src.clients.scholar.NODRIVER_AVAILABLE", True)
    @patch("src.clients.scholar.ScholarBrowserLoop")
    def test_returns_none_on_blocked(self, mock_loop_cls: MagicMock) -> None:
        from src.clients.scholar import _run_browser_coro

        mock_loop = mock_loop_cls.return_value
        mock_loop.run.side_effect = ScholarBrowserBlockedError("CAPTCHA")
        result = _run_browser_coro(lambda b: b, "test")
        assert result is None

    @patch("src.clients.scholar.NODRIVER_AVAILABLE", True)
    @patch("src.clients.scholar.ScholarBrowserLoop")
    def test_returns_none_on_generic_error(self, mock_loop_cls: MagicMock) -> None:
        from src.clients.scholar import _run_browser_coro

        mock_loop = mock_loop_cls.return_value
        mock_loop.run.side_effect = RuntimeError("browser crash")
        result = _run_browser_coro(lambda b: b, "test")
        assert result is None


class TestCacheKeyWithAuthor:
    """Test that cache keys include author_name to prevent cross-author collisions."""

    @patch("src.clients.scholar.response_cache")
    @patch("src.clients.scholar._run_browser_coro", return_value=None)
    @patch("src.clients.scholar._serpapi_search_scholar_for_cite_link", return_value=None)
    def test_different_authors_different_keys(
        self, _mock_serpapi: MagicMock, _mock_browser: MagicMock, mock_cache: MagicMock,
    ) -> None:
        from src.clients.scholar import search_scholar_for_cite_link

        mock_cache.get.return_value = None

        search_scholar_for_cite_link("key", "Machine Learning", author_name="Smith")
        search_scholar_for_cite_link("key", "Machine Learning", author_name="Jones")

        calls = mock_cache.get.call_args_list
        key1 = calls[0][0][1]  # second positional arg is the cache key
        key2 = calls[1][0][1]
        assert key1 != key2
        assert "smith" in key1
        assert "jones" in key2

    @patch("src.clients.scholar.response_cache")
    @patch("src.clients.scholar._run_browser_coro", return_value=None)
    @patch("src.clients.scholar._serpapi_search_scholar_for_cite_link", return_value=None)
    def test_none_author_handled(
        self, _mock_serpapi: MagicMock, _mock_browser: MagicMock, mock_cache: MagicMock,
    ) -> None:
        from src.clients.scholar import search_scholar_for_cite_link

        mock_cache.get.return_value = None
        search_scholar_for_cite_link("key", "Test Title", author_name=None)
        key = mock_cache.get.call_args[0][1]
        assert key.endswith("|")
