"""Google Scholar author profiles via the SerpAPI REST API (serpapi.com).

Uses the ``google_scholar_author`` engine to fetch an author's full
publication list by Scholar profile ID.  Supports pagination (up to 100
results per page) and ``sort=pubdate`` ordering.

This module handles **author publication retrieval** only.  Per-article
citation detail lookups remain in ``serply_scholar.py`` (cheaper API).

All calls are stateless HTTP GETs through ``http_fetch_bytes``, so no
locking is required.

SerpAPI response structure (``/search?engine=google_scholar_author``)::

    {
      "articles": [
        {
          "title": "...",
          "link": "https://scholar.google.com/citations?...",
          "citation_id": "abc123:def456",
          "authors": "A Smith, B Jones",
          "publication": "Nature, 2024",
          "cited_by": {"value": 42, "link": "..."},
          "year": "2024"
        }
      ],
      "serpapi_pagination": {"next": "https://serpapi.com/search?...&start=100"}
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from ..config import HTTP_TIMEOUT_DEFAULT, SERPAPI_BASE
from ..http_utils import http_fetch_bytes

_log = logging.getLogger("CiteForge.serpapi")

# Safety cap: max pages to paginate (100 articles/page → 1000 max)
_MAX_PAGES = 10

# SerpAPI max results per page
_PAGE_SIZE = 100


def _serpapi_get(
    api_key: str, author_id: str, start: int = 0, num: int = _PAGE_SIZE, sort: str = "pubdate"
) -> dict[str, Any]:
    """Execute a GET request against the SerpAPI Scholar Author endpoint.

    Args:
        api_key: SerpAPI key (passed as ``api_key`` query param).
        author_id: Google Scholar author profile ID.
        start: Result offset for pagination (0, 100, 200, ...).
        num: Results per page (max 100).
        sort: Sort order, ``"pubdate"`` (newest first) or ``"citedby"``.

    Returns:
        Parsed JSON response dict, or empty dict on failure.
    """
    params = urlencode(
        {
            "engine": "google_scholar_author",
            "author_id": author_id,
            "start": start,
            "num": num,
            "sort": sort,
            "api_key": api_key,
        }
    )
    url = f"{SERPAPI_BASE}?{params}"

    headers = {"Accept": "application/json"}

    try:
        raw = http_fetch_bytes(url, headers, HTTP_TIMEOUT_DEFAULT)
        data: dict[str, Any] = json.loads(raw.decode("utf-8"))
        if "error" in data:
            _log.warning("SerpAPI returned error for %s: %s", author_id, data["error"])
            return {}
        return data
    except Exception as exc:
        _log.warning("SerpAPI request failed for %s: %s", author_id, type(exc).__name__)
        return {}


def _convert_article(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a SerpAPI article to CiteForge format.

    SerpAPI provides structured fields (no description parsing needed):
    ``title``, ``authors``, ``year``, ``citation_id``, ``publication``.

    Returns:
        CiteForge-format article dict, or empty dict if title is missing.
    """
    title = item.get("title") or ""
    if not title:
        return {}

    year_raw = str(item.get("year") or "")
    citation_id = item.get("citation_id") or ""

    article: dict[str, Any] = {
        "title": title,
        "authors": item.get("authors") or "",
        "year": int(year_raw) if year_raw.isdigit() else "",
        "citation_id": citation_id,
        "result_id": citation_id,
        "source": "scholar",
    }

    publication = item.get("publication") or ""
    if publication:
        article["publication_info"] = {"summary": publication}
        article["publication"] = publication

    link = item.get("link") or ""
    if link:
        article["url"] = link

    return article


def serpapi_fetch_author_publications(
    api_key: str,
    author_id: str,
    num: int = 250,
    sort: str = "pubdate",
    min_year: int = 0,
) -> dict[str, Any]:
    """Fetch publications for an author via SerpAPI Scholar Author endpoint.

    Uses ``engine=google_scholar_author`` with the author's Scholar profile
    ID for exact profile matching.  Paginates automatically until *num*
    results are collected or no more pages are available.

    When *min_year* > 0 and *sort* is ``"pubdate"``, pagination continues
    until all articles in the year window have been fetched (i.e., the
    newest article on the last page has year < *min_year*).  The *num*
    parameter still acts as a hard safety cap.

    Args:
        api_key: SerpAPI key.
        author_id: Google Scholar profile ID (e.g., ``"dg7f4K8AAAAJ"``).
        num: Maximum number of articles to return (hard safety cap).
        sort: Sort order, ``"pubdate"`` or ``"citedby"``.
        min_year: Minimum publication year to fetch.  When > 0 and *sort*
            is ``"pubdate"``, pagination stops once a full page falls
            below this year.  0 disables year-bounded stopping.

    Returns:
        Dict matching the CiteForge ``fetch_author_publications()`` contract::

            {"articles": [...], "search_metadata": {"status": "Success", "source": "serpapi"}}
    """
    if not api_key or not author_id:
        return {"articles": [], "search_metadata": {"status": "Error", "source": "serpapi"}}

    year_bounded = min_year > 0 and sort == "pubdate"
    articles: list[dict[str, Any]] = []
    start = 0

    for _page in range(_MAX_PAGES):
        page_size = min(num - len(articles), _PAGE_SIZE)
        if page_size <= 0:
            break
        data = _serpapi_get(api_key, author_id, start=start, num=page_size, sort=sort)

        page_articles = data.get("articles") or []
        if not page_articles:
            break

        page_start = len(articles)
        for item in page_articles:
            converted = _convert_article(item)
            if converted:
                articles.append(converted)

        if len(articles) >= num:
            break

        # Year-bounded stop: if ALL articles with valid years on this page
        # are below min_year, we've passed the contribution window.
        if year_bounded and len(articles) > page_start:
            page_years = [a["year"] for a in articles[page_start:] if isinstance(a["year"], int) and a["year"] > 0]
            if page_years:
                max_year = max(page_years)
                if max_year < min_year:
                    _log.debug(
                        "Year-bounded stop for %s: page max year %d < min_year %d",
                        author_id,
                        max_year,
                        min_year,
                    )
                    break

        pagination = data.get("serpapi_pagination") or {}
        if not pagination.get("next"):
            break

        start += len(page_articles)

    articles = articles[:num]

    return {
        "articles": articles,
        "search_metadata": {"status": "Success", "source": "serpapi"},
    }
