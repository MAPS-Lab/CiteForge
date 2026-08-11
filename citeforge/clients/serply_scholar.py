"""Google Scholar access via the Serply REST API (api.serply.io).

Exposes ``serply_fetch_citation`` for title and author citation details. It is
used in production by ``scholar.py``.

All calls are stateless HTTP GETs through ``http_fetch_bytes``, so no
locking is required.

Serply response structure (``/v1/scholar/{query}``)::

    {
      "articles": [
        {
          "title": "...",
          "link": "https://...",
          "id": "GxXV_UHzwE8J",
          "description": "Author1, Author2 - Journal, Year - publisher.com",
          "author": {
            "names": "Author1, Author2 - Journal, Year - publisher.com",
            "authors": [{"name": "Author1", "link": "..."}, ...]
          },
          "extras": {"citations": {"count": "Cited by 42", "link": "..."}}
        }
      ],
      "results": []
    }

Year and journal must be parsed from the ``description`` string which follows
the Google Scholar format: ``"Authors - Journal/Venue, Year - domain.com"``.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from ..config import HTTP_TIMEOUT_DEFAULT, SERPLY_BASE
from ..http_utils import _decode_json_bytes, http_fetch_bytes
from ._http_errors import SCHOLAR_HTTP_ERRORS

_log = logging.getLogger("CiteForge.serply")

# Matches a 4-digit year (1900-2099) in the description string
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _serply_get(api_key: str, query: str, start: int = 0) -> dict[str, Any]:
    """Execute a GET request against the Serply Scholar endpoint.

    The Serply API uses path-based query encoding: ``/v1/scholar/{encoded_query}``.
    Pagination uses ``?start=N`` as a query parameter (offset-based, 10 per page).

    Args:
        api_key: Serply API key for authentication.
        query: Raw search query (URL-encoded into the path segment).
        start: Result offset for pagination (0, 10, 20, ...).

    Returns:
        Parsed JSON response dict, or empty dict on failure.
    """
    encoded_query = quote(query, safe="")
    url = f"{SERPLY_BASE}/{encoded_query}"
    if start > 0:
        url += f"?start={start}"

    headers = {
        "X-Api-Key": api_key,
        "X-Proxy-Location": "US",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }

    try:
        raw = http_fetch_bytes(url, headers, HTTP_TIMEOUT_DEFAULT)
        return _decode_json_bytes(raw, url)
    except SCHOLAR_HTTP_ERRORS as exc:
        _log.debug("Serply request failed: %s", type(exc).__name__)
        return {}


def _parse_description(description: str) -> tuple[str, str]:
    """Extract year and journal/venue from the Serply description string.

    The description follows Google Scholar format::

        "Author1, Author2 - Journal/Venue, Year - domain.com"

    Returns:
        Tuple of (year_str, journal_str). Either may be empty.
    """
    if not description:
        return "", ""

    # Normalize non-breaking spaces before splitting (Serply uses \xa0 before dashes)
    description = description.replace("\xa0", " ")

    # Split on " - " to separate authors, venue+year, and domain
    parts = description.split(" - ")
    if len(parts) < 2:
        return "", ""

    # The middle part(s) contain venue and year: "Scientific reports, 2019"
    # The last part is typically the domain: "nature.com"
    middle = " - ".join(parts[1:-1]) if len(parts) > 2 else parts[1]

    year_str = ""
    year_match = _YEAR_RE.search(middle)
    if year_match:
        year_str = year_match.group(0)

    # Extract journal/venue: everything before the year in the middle part
    journal = ""
    if year_match:
        journal = middle[: year_match.start()].rstrip(", ")
    elif middle:
        journal = middle.strip()

    journal = journal.strip()

    return year_str, journal


def _extract_authors(item: dict[str, Any]) -> str:
    """Extract author names from a Serply article item.

    The Serply API provides authors in ``item["author"]["authors"]`` as a list
    of dicts with a ``"name"`` key.
    """
    author_data = item.get("author")
    if not isinstance(author_data, dict):
        return ""

    authors_list = author_data.get("authors")
    if isinstance(authors_list, list):
        names = [str(a["name"]) for a in authors_list if isinstance(a, dict) and a.get("name")]
        if names:
            return " and ".join(names)

    # Fallback: parse from author.names (comma-separated before the " - ")
    names_str = author_data.get("names") or ""
    if names_str and " - " in names_str:
        author_part = names_str.split(" - ")[0]
        # Remove non-breaking spaces
        author_part = author_part.replace("\xa0", " ").strip()
        if author_part:
            return author_part

    return ""


def serply_fetch_citation(
    api_key: str,
    title: str,
    author_name: str,
) -> dict[str, str] | None:
    """Fetch detailed citation metadata via Serply title+author search.

    Searches for ``"{title}" {author_name}`` and returns the first result
    as a field dict matching the CiteForge Scholar citation format, or
    ``None`` if no result is found.
    """
    if not api_key or not title:
        return None

    query = f'"{title}" {author_name}'
    data = _serply_get(api_key, query)
    results = data.get("articles") or []

    if not results:
        return None

    item = results[0]
    fields: dict[str, str] = {}

    item_title = item.get("title") or ""
    if item_title:
        fields["title"] = item_title

    authors_str = _extract_authors(item)
    if authors_str:
        fields["authors"] = authors_str

    description = item.get("description") or ""
    year_str, journal = _parse_description(description)

    if year_str:
        fields["publication date"] = year_str

    if journal:
        fields["journal"] = journal

    if description:
        fields["description"] = description

    link = item.get("link") or ""
    if link:
        fields["url"] = link

    return fields or None
