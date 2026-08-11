"""Tests for Scholar clients (serpapi_scholar.py, serply_scholar.py) and scholar.py facade."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from citeforge.clients import serpapi_scholar
from citeforge.clients.scholar import (
    _deduplicate_publication_list,
    fetch_author_publications,
    fetch_scholar_citation,
    merge_publication_lists,
)
from citeforge.clients.serpapi_scholar import serpapi_fetch_author_publications
from citeforge.clients.serply_scholar import serply_fetch_citation

_SERPLY_HTTP_PATCH = "citeforge.clients.serply_scholar.http_fetch_bytes"
_SERPAPI_HTTP_PATCH = "citeforge.clients.serpapi_scholar.http_fetch_bytes"


def test_serpapi_programming_error_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        serpapi_scholar,
        "http_fetch_bytes",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("bug")),
    )
    with pytest.raises(AssertionError, match="bug"):
        serpapi_scholar._serpapi_get("key", "author")


def test_exact_title_explicit_author_conflict_keeps_both() -> None:
    """Exact normalized titles with incompatible nonempty authors remain distinct."""
    pubs = [
        {"title": "Shared Benchmark Title", "authors": ["Alpha, Alice"], "year": 2024},
        {"title": "Shared Benchmark Title", "authors": ["Beta, Bob"], "year": 2024},
    ]

    assert len(_deduplicate_publication_list(pubs)) == 2


def test_exact_title_year_gap_four_keeps_both() -> None:
    """Exact normalized titles with explicit years four apart remain distinct."""
    pubs = [
        {"title": "Shared Benchmark Title", "authors": ["Alpha, Alice"], "year": 2020},
        {"title": "Shared Benchmark Title", "authors": ["Alpha, Alice"], "year": 2024},
    ]

    assert len(_deduplicate_publication_list(pubs)) == 2


def test_cross_source_sparse_title_without_target_author_keeps_both() -> None:
    """Missing target-author evidence does not activate exact-title deduplication."""
    publication = {"title": "Shared Benchmark Title", "year": 2024}

    assert len(merge_publication_lists([publication], [publication], None)) == 2


@pytest.mark.parametrize(
    ("publications", "expected_count"),
    [
        ([], 0),
        ([{"title": "Deep Learning Survey", "year": 2024, "authors": ["Jane Doe"]}], 1),
        (
            [
                {"title": "Attention Is All You Need", "year": 2017, "authors": ["Ashish Vaswani"]},
                {"title": "Attention Is All You Need", "year": 2017, "authors": ["Ashish Vaswani"]},
            ],
            1,
        ),
        (
            [
                {"title": "Attention Is All You Need", "year": 2017, "authors": ["Ashish Vaswani"]},
                {
                    "title": "Deep Residual Learning for Image Recognition",
                    "year": 2016,
                    "authors": ["Kaiming He"],
                },
            ],
            2,
        ),
    ],
)
def test_deduplicate_publication_list_basic_cardinality(
    publications: list[dict[str, object]], expected_count: int
) -> None:
    """The Scholar facade preserves basic empty, singleton, duplicate, and distinct cases."""
    assert len(_deduplicate_publication_list(publications)) == expected_count


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _make_serply_response(
    articles: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a Serply API response dict matching real API structure."""
    return {
        "articles": articles,
        "ads": [],
        "ads_count": 0,
        "answers": [],
        "results": [],
        "shopping_ads": [],
        "places": [],
        "related_searches": [],
        "image_results": [],
        "ts": 1.234,
        "device_region": "US",
        "device_type": None,
    }


def _make_serply_article(
    title: str = "Machine Learning in Healthcare",
    author_names: list[str] | None = None,
    description: str = "J Smith, J Doe - Nature, 2024 - nature.com",
    link: str = "https://scholar.google.com/example",
    article_id: str = "abc123",
) -> dict[str, Any]:
    """Build a single Serply article item matching real API structure."""
    names = author_names or ["J Smith", "J Doe"]
    authors_list = [{"name": n, "link": f"https://scholar.google.com/citations?user={n}"} for n in names]
    _prefix, _, suffix = description.partition(" - ")
    names_str = f"{', '.join(names)} - {suffix}" if suffix else description
    return {
        "title": title,
        "link": link,
        "id": article_id,
        "cite": f"https://scholar.google.com/scholar?q=info:{article_id}",
        "author": {
            "names": names_str,
            "authors": authors_list,
        },
        "description": description,
        "extras": {
            "citations": {"count": "Cited by 42", "link": "https://scholar.google.com/scholar?cites=123"},
            "related": {"link": "https://scholar.google.com/scholar?q=related:abc"},
            "versions": {"count": "All 5 versions", "link": "https://scholar.google.com/scholar?cluster=123"},
        },
    }


def _make_serpapi_response(
    articles: list[dict[str, Any]],
    has_next: bool = False,
) -> dict[str, Any]:
    """Build a SerpAPI Scholar Author response dict."""
    resp: dict[str, Any] = {
        "articles": articles,
        "search_metadata": {"status": "Success"},
        "author": {"name": "Test Author"},
    }
    if has_next:
        resp["serpapi_pagination"] = {"next": "https://serpapi.com/search?start=100"}
    return resp


def _make_serpapi_article(
    title: str = "Machine Learning in Healthcare",
    authors: str = "J Smith, J Doe",
    year: str = "2024",
    publication: str = "Nature",
    citation_id: str = "abc123:def456",
    link: str = "https://scholar.google.com/citations?view_op=view_citation&hl=en",
) -> dict[str, Any]:
    """Build a single SerpAPI article item matching real API structure."""
    return {
        "title": title,
        "link": link,
        "citation_id": citation_id,
        "authors": authors,
        "publication": publication,
        "cited_by": {"value": 42, "link": "https://scholar.google.com/scholar?cites=123"},
        "year": year,
    }


class TestSerpapiFetchAuthorPublications:
    """Test serpapi_fetch_author_publications conversion."""

    @patch(_SERPAPI_HTTP_PATCH)
    def test_basic_conversion(self, mock_http: MagicMock) -> None:
        """SerpAPI article -> CiteForge article dict."""
        response = _make_serpapi_response([_make_serpapi_article()])
        mock_http.return_value = _json_bytes(response)

        result = serpapi_fetch_author_publications("test_key", "dg7f4K8AAAAJ", num=10)

        assert result["search_metadata"]["status"] == "Success"
        assert result["search_metadata"]["source"] == "serpapi"
        assert len(result["articles"]) == 1

        art = result["articles"][0]
        assert art["title"] == "Machine Learning in Healthcare"
        assert art["authors"] == "J Smith, J Doe"
        assert art["year"] == 2024
        assert art["source"] == "scholar"
        assert art["citation_id"] == "abc123:def456"
        assert art["result_id"] == art["citation_id"]
        assert art["publication_info"] == {"summary": "Nature"}
        assert art["publication"] == "Nature"

    @patch(_SERPAPI_HTTP_PATCH)
    def test_pagination_multi_page(self, mock_http: MagicMock) -> None:
        """Multiple pages should be fetched when serpapi_pagination.next is present."""
        page1_articles = [_make_serpapi_article(title=f"Paper {i}") for i in range(3)]
        page2_articles = [_make_serpapi_article(title=f"Paper {i + 3}") for i in range(2)]
        page1 = _make_serpapi_response(page1_articles, has_next=True)
        page2 = _make_serpapi_response(page2_articles, has_next=False)

        mock_http.side_effect = [
            _json_bytes(page1),
            _json_bytes(page2),
        ]

        result = serpapi_fetch_author_publications("key", "author_id", num=10)
        assert len(result["articles"]) == 5
        assert mock_http.call_count == 2

    def test_empty_api_key(self) -> None:
        """Empty API key should return error struct without making HTTP calls."""
        result = serpapi_fetch_author_publications("", "author_id")
        assert result["articles"] == []
        assert result["search_metadata"]["status"] == "Error"

    def test_empty_author_id(self) -> None:
        """Empty author ID should return error struct without making HTTP calls."""
        result = serpapi_fetch_author_publications("key", "")
        assert result["articles"] == []
        assert result["search_metadata"]["status"] == "Error"

    @patch(_SERPAPI_HTTP_PATCH)
    def test_num_limit(self, mock_http: MagicMock) -> None:
        """Results should be trimmed to num parameter."""
        articles = [_make_serpapi_article(title=f"Paper {i}") for i in range(5)]
        response = _make_serpapi_response(articles)
        mock_http.return_value = _json_bytes(response)

        result = serpapi_fetch_author_publications("key", "author_id", num=3)
        assert len(result["articles"]) == 3

    @patch(_SERPAPI_HTTP_PATCH)
    def test_stop_on_no_next_page(self, mock_http: MagicMock) -> None:
        """Pagination should stop when serpapi_pagination.next is absent."""
        articles = [_make_serpapi_article(title="Only Page")]
        response = _make_serpapi_response(articles, has_next=False)
        mock_http.return_value = _json_bytes(response)

        result = serpapi_fetch_author_publications("key", "author_id", num=200)
        assert len(result["articles"]) == 1
        assert mock_http.call_count == 1

    @patch(_SERPAPI_HTTP_PATCH)
    def test_empty_results(self, mock_http: MagicMock) -> None:
        """Empty articles array should return no articles."""
        response = _make_serpapi_response([])
        mock_http.return_value = _json_bytes(response)

        result = serpapi_fetch_author_publications("key", "author_id")
        assert result["articles"] == []
        assert result["search_metadata"]["status"] == "Success"

    @patch(_SERPAPI_HTTP_PATCH)
    def test_http_exception(self, mock_http: MagicMock) -> None:
        """Exception during HTTP call should return response with no articles."""
        mock_http.side_effect = requests.exceptions.Timeout("Network error")
        result = serpapi_fetch_author_publications("key", "author_id")
        assert result["articles"] == []

    @patch(_SERPAPI_HTTP_PATCH)
    def test_missing_year(self, mock_http: MagicMock) -> None:
        """Result without year should have empty year."""
        article = _make_serpapi_article(year="")
        response = _make_serpapi_response([article])
        mock_http.return_value = _json_bytes(response)

        result = serpapi_fetch_author_publications("key", "author_id")
        assert result["articles"][0]["year"] == ""

    @patch(_SERPAPI_HTTP_PATCH)
    def test_no_publication(self, mock_http: MagicMock) -> None:
        """Result without publication should not have publication_info."""
        article = _make_serpapi_article(publication="")
        response = _make_serpapi_response([article])
        mock_http.return_value = _json_bytes(response)

        result = serpapi_fetch_author_publications("key", "author_id")
        art = result["articles"][0]
        assert "publication_info" not in art
        assert "publication" not in art

    @patch(_SERPAPI_HTTP_PATCH)
    def test_url_contains_author_id(self, mock_http: MagicMock) -> None:
        """SerpAPI request URL should contain the author_id parameter."""
        response = _make_serpapi_response([_make_serpapi_article()])
        mock_http.return_value = _json_bytes(response)

        serpapi_fetch_author_publications("key", "dg7f4K8AAAAJ", num=5)

        call_url = mock_http.call_args[0][0]
        assert "author_id=dg7f4K8AAAAJ" in call_url
        assert "engine=google_scholar_author" in call_url

    @patch(_SERPAPI_HTTP_PATCH)
    def test_titleless_article_dropped(self, mock_http: MagicMock) -> None:
        """Article without a title should be silently dropped."""
        response = _make_serpapi_response([_make_serpapi_article(title="")])
        mock_http.return_value = _json_bytes(response)

        result = serpapi_fetch_author_publications("key", "author_id")
        assert result["articles"] == []

    @patch(_SERPAPI_HTTP_PATCH)
    def test_json_decode_error(self, mock_http: MagicMock) -> None:
        """Invalid JSON (e.g., HTML error page) should return no articles."""
        mock_http.return_value = b"<html>502 Bad Gateway</html>"
        result = serpapi_fetch_author_publications("key", "author_id")
        assert result["articles"] == []

    @patch(_SERPAPI_HTTP_PATCH)
    def test_year_non_digit(self, mock_http: MagicMock) -> None:
        """Non-digit year (e.g., 'N/A') should produce empty string year."""
        article = _make_serpapi_article(year="N/A")
        response = _make_serpapi_response([article])
        mock_http.return_value = _json_bytes(response)

        result = serpapi_fetch_author_publications("key", "author_id")
        assert result["articles"][0]["year"] == ""

    @patch(_SERPAPI_HTTP_PATCH)
    def test_missing_authors_key(self, mock_http: MagicMock) -> None:
        """Article without authors key should produce empty string authors."""
        article = _make_serpapi_article()
        del article["authors"]
        response = _make_serpapi_response([article])
        mock_http.return_value = _json_bytes(response)

        result = serpapi_fetch_author_publications("key", "author_id")
        assert result["articles"][0]["authors"] == ""

    @patch(_SERPAPI_HTTP_PATCH)
    def test_year_bounded_stop(self, mock_http: MagicMock) -> None:
        """Pagination stops when all page articles are below min_year."""
        page1 = _make_serpapi_response(
            [_make_serpapi_article(title=f"P{i}", year="2024") for i in range(3)],
            has_next=True,
        )
        page2 = _make_serpapi_response(
            [_make_serpapi_article(title=f"Old{i}", year="2018") for i in range(3)],
            has_next=True,
        )
        mock_http.side_effect = [
            _json_bytes(page1),
            _json_bytes(page2),
        ]
        result = serpapi_fetch_author_publications("key", "aid", num=1000, min_year=2019)
        assert len(result["articles"]) == 6
        assert mock_http.call_count == 2

    @patch(_SERPAPI_HTTP_PATCH)
    def test_year_bounded_continues_mixed_page(self, mock_http: MagicMock) -> None:
        """Pagination continues when page has mix of in/out-of-window articles."""
        page1 = _make_serpapi_response(
            [
                _make_serpapi_article(title="New", year="2024"),
                _make_serpapi_article(title="Old", year="2017"),
                _make_serpapi_article(title="Recent", year="2020"),
            ],
            has_next=True,
        )
        page2 = _make_serpapi_response(
            [_make_serpapi_article(title="Ancient", year="2015")],
            has_next=False,
        )
        mock_http.side_effect = [
            _json_bytes(page1),
            _json_bytes(page2),
        ]
        result = serpapi_fetch_author_publications("key", "aid", num=1000, min_year=2019)
        assert len(result["articles"]) == 4
        assert mock_http.call_count == 2

    @patch(_SERPAPI_HTTP_PATCH)
    def test_year_bounded_ignores_missing_years(self, mock_http: MagicMock) -> None:
        """Articles without years should not trigger year-bounded stop."""
        page1 = _make_serpapi_response(
            [
                _make_serpapi_article(title="No Year", year=""),
                _make_serpapi_article(title="Also No Year", year="N/A"),
            ],
            has_next=True,
        )
        page2 = _make_serpapi_response(
            [_make_serpapi_article(title="Has Year", year="2020")],
            has_next=False,
        )
        mock_http.side_effect = [
            _json_bytes(page1),
            _json_bytes(page2),
        ]
        result = serpapi_fetch_author_publications("key", "aid", num=1000, min_year=2019)
        assert len(result["articles"]) == 3
        assert mock_http.call_count == 2

    @patch(_SERPAPI_HTTP_PATCH)
    def test_num_cap_still_applies_with_min_year(self, mock_http: MagicMock) -> None:
        """num parameter should still cap results even with year-bounded fetching."""
        articles = [_make_serpapi_article(title=f"P{i}", year="2024") for i in range(5)]
        mock_http.return_value = _json_bytes(_make_serpapi_response(articles))
        result = serpapi_fetch_author_publications("key", "aid", num=3, min_year=2019)
        assert len(result["articles"]) == 3

    @patch(_SERPAPI_HTTP_PATCH)
    def test_year_bounded_disabled_when_not_pubdate(self, mock_http: MagicMock) -> None:
        """Year-bounded stop should not apply when sort != pubdate."""
        page1 = _make_serpapi_response(
            [_make_serpapi_article(title="Old", year="2010")],
            has_next=False,
        )
        mock_http.return_value = _json_bytes(page1)
        result = serpapi_fetch_author_publications(
            "key",
            "aid",
            num=1000,
            min_year=2019,
            sort="citedby",
        )
        assert len(result["articles"]) == 1


class TestSerplyFetchCitation:
    """Test serply_fetch_citation conversion."""

    @patch(_SERPLY_HTTP_PATCH)
    def test_citation_found(self, mock_http: MagicMock) -> None:
        """First article should be returned as field dict."""
        response = _make_serply_response([_make_serply_article()])
        mock_http.return_value = _json_bytes(response)

        result = serply_fetch_citation("key", "Machine Learning in Healthcare", "John Smith")
        assert result is not None
        assert result["title"] == "Machine Learning in Healthcare"
        assert result["authors"] == "J Smith and J Doe"
        assert result["publication date"] == "2024"
        assert result["journal"] == "Nature"
        assert "description" in result
        assert result["url"] == "https://scholar.google.com/example"

    @patch(_SERPLY_HTTP_PATCH)
    def test_citation_no_results(self, mock_http: MagicMock) -> None:
        """Empty articles should return None."""
        response = _make_serply_response([])
        mock_http.return_value = _json_bytes(response)

        result = serply_fetch_citation("key", "Nonexistent Paper", "Nobody")
        assert result is None

    def test_empty_params(self) -> None:
        """Any empty required parameter should return None."""
        assert serply_fetch_citation("", "Title", "Author") is None
        assert serply_fetch_citation("key", "", "Author") is None

    @patch(_SERPLY_HTTP_PATCH)
    def test_citation_http_exception(self, mock_http: MagicMock) -> None:
        """Exception during HTTP call should return None."""
        mock_http.side_effect = requests.exceptions.Timeout("Timeout")
        result = serply_fetch_citation("key", "Some Title", "Some Author")
        assert result is None

    @patch(_SERPLY_HTTP_PATCH)
    def test_citation_minimal_fields(self, mock_http: MagicMock) -> None:
        """Result with only title should return dict with just title."""
        item: dict[str, Any] = {"title": "Minimal Paper"}
        response = _make_serply_response([item])
        mock_http.return_value = _json_bytes(response)

        result = serply_fetch_citation("key", "Minimal Paper", "Author")
        assert result is not None
        assert result["title"] == "Minimal Paper"
        assert "authors" not in result
        assert "publication date" not in result


class TestFacadeCacheBehavior:
    """Test that facade functions use cache and skip API calls on cache hit."""

    @patch("citeforge.clients.scholar.response_cache")
    @patch("citeforge.clients.scholar.serpapi_fetch_author_publications")
    def test_publications_cache_hit(
        self,
        mock_serpapi: MagicMock,
        mock_cache: MagicMock,
    ) -> None:
        """Cached publications should be returned without calling SerpAPI."""
        cached_data = {"articles": [{"title": "Cached"}], "search_metadata": {"status": "Success"}}
        mock_cache.get.return_value = cached_data

        result = fetch_author_publications("key", "author_a", "Author A")
        assert result["articles"][0]["title"] == "Cached"
        mock_cache.get.assert_called_once_with("serpapi_publications", "author_a|page_0")
        mock_serpapi.assert_not_called()

    @patch("citeforge.clients.scholar.response_cache")
    @patch("citeforge.clients.scholar.serpapi_fetch_author_publications")
    def test_publications_cache_miss(
        self,
        mock_serpapi: MagicMock,
        mock_cache: MagicMock,
    ) -> None:
        """Cache miss should call SerpAPI with author_id and store the result."""
        mock_cache.get.return_value = None
        mock_serpapi.return_value = {
            "articles": [{"title": "Fresh"}],
            "search_metadata": {"status": "Success", "source": "serpapi"},
        }

        result = fetch_author_publications("key", "author_b", "Author B")
        assert result["articles"][0]["title"] == "Fresh"
        mock_serpapi.assert_called_once_with("key", "author_b", num=100, min_year=0)
        mock_cache.put.assert_called_once()
        put_args = mock_cache.put.call_args[0]
        assert put_args[0] == "serpapi_publications"
        assert put_args[1] == "author_b|page_0"

    @patch("citeforge.clients.scholar.response_cache")
    @patch("citeforge.clients.scholar.serpapi_fetch_author_publications")
    def test_publications_cache_miss_empty_result(
        self,
        mock_serpapi: MagicMock,
        mock_cache: MagicMock,
    ) -> None:
        """When SerpAPI returns no articles, result must not be cached."""
        mock_cache.get.return_value = None
        mock_serpapi.return_value = {
            "articles": [],
            "search_metadata": {"status": "Success", "source": "serpapi"},
        }

        result = fetch_author_publications("key", "author_c", "Author C")
        assert result["articles"] == []
        mock_serpapi.assert_called_once()
        mock_cache.put.assert_not_called()

    @patch("citeforge.clients.scholar.response_cache")
    @patch("citeforge.clients.scholar.serply_fetch_citation")
    def test_citation_cache_hit(
        self,
        mock_serply: MagicMock,
        mock_cache: MagicMock,
    ) -> None:
        """Cached citation should be returned without calling Serply."""
        mock_cache.get.return_value = {"title": "Cached Paper"}
        result = fetch_scholar_citation("key", "Some Title", "Author")
        assert result is not None
        assert result["title"] == "Cached Paper"
        mock_serply.assert_not_called()

    @patch("citeforge.clients.scholar.response_cache")
    @patch("citeforge.clients.scholar.serply_fetch_citation")
    def test_citation_cache_miss(self, mock_serply: MagicMock, mock_cache: MagicMock) -> None:
        """Cache miss should call Serply and store the result."""
        mock_cache.get.return_value = None
        mock_serply.return_value = {"title": "Fresh Citation"}

        result = fetch_scholar_citation("key", "Fresh Title", "Author")
        assert result is not None
        assert result["title"] == "Fresh Citation"
        mock_serply.assert_called_once()
        mock_cache.put.assert_called_once()
        put_args = mock_cache.put.call_args[0]
        assert put_args[0] == "serply_citation"
        assert put_args[2] == {"title": "Fresh Citation"}

    @patch("citeforge.clients.scholar.response_cache")
    @patch("citeforge.clients.scholar.serply_fetch_citation")
    def test_citation_negative_not_cached(
        self,
        mock_serply: MagicMock,
        mock_cache: MagicMock,
    ) -> None:
        """When Serply returns None, no negative is cached (Serply conflates errors and empty)."""
        mock_cache.get.return_value = None
        mock_serply.return_value = None

        result = fetch_scholar_citation("key", "Unknown Title", "Author")
        assert result is None
        mock_cache.put_negative.assert_not_called()

    def test_citation_empty_title(self) -> None:
        """Empty title should return None without calling Serply."""
        result = fetch_scholar_citation("key", "", "Author")
        assert result is None

    @patch("citeforge.clients.scholar.response_cache")
    @patch("citeforge.clients.scholar.serpapi_fetch_author_publications")
    def test_stale_cache_invalidated(
        self,
        mock_serpapi: MagicMock,
        mock_cache: MagicMock,
    ) -> None:
        """Cache with count-truncated results should be invalidated and re-fetched."""
        stale_data = {
            "articles": [{"title": f"P{i}", "year": 2021 + (i % 5)} for i in range(200)],
            "search_metadata": {"status": "Success"},
        }
        mock_cache.get.return_value = stale_data
        mock_serpapi.return_value = {
            "articles": [{"title": "Fresh"}],
            "search_metadata": {"status": "Success", "source": "serpapi"},
        }
        fetch_author_publications("key", "auth_x", "Author X", num=400, min_year=2019)
        mock_cache.invalidate.assert_called_once()
        mock_serpapi.assert_called_once()

    @patch("citeforge.clients.scholar.response_cache")
    @patch("citeforge.clients.scholar.serpapi_fetch_author_publications")
    def test_complete_cache_not_invalidated(
        self,
        mock_serpapi: MagicMock,
        mock_cache: MagicMock,
    ) -> None:
        """Cache covering the full window should not be invalidated."""
        complete_data = {
            "articles": [{"title": f"P{i}", "year": 2015 + (i % 10)} for i in range(155)],
            "search_metadata": {"status": "Success"},
        }
        mock_cache.get.return_value = complete_data
        fetch_author_publications("key", "auth_y", "Author Y", num=400, min_year=2019)
        mock_cache.invalidate.assert_not_called()
        mock_serpapi.assert_not_called()


class TestThreadSafety:
    """Verify that Scholar clients require no locking (no module-level state)."""

    @pytest.mark.parametrize(
        ("module_path", "forbidden_attr"),
        [
            ("citeforge.clients.serply_scholar", "_lock"),
            ("citeforge.clients.serply_scholar", "_author_pubs_cache"),
            ("citeforge.clients.serpapi_scholar", "_lock"),
            ("citeforge.clients.serpapi_scholar", "_author_pubs_cache"),
        ],
    )
    def test_no_module_level_state(self, module_path: str, forbidden_attr: str) -> None:
        """Scholar client modules must not hold module-level locks or caches."""
        import importlib

        mod = importlib.import_module(module_path)
        assert not hasattr(mod, forbidden_attr), f"{module_path} should not have '{forbidden_attr}'"
