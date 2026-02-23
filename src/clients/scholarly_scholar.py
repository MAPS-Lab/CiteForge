"""Google Scholar access via the ``scholarly`` library with ScraperAPI proxy.

All public functions accept an ``api_key`` argument (ScraperAPI key) and return
data structures compatible with the rest of the CiteForge pipeline.

Thread safety
-------------
``scholarly`` is **not** thread-safe.  A module-level ``_scholarly_lock``
serializes every call.  This is acceptable because Scholar is rate-limited
anyway and parallel Scholar calls increase CAPTCHA risk.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from scholarly import scholarly

_log = logging.getLogger("CiteForge.scholarly")

_scholarly_lock = threading.Lock()
_initialized = False

# Cache the raw publications list returned by ``scholarly.fill(author,
# sections=["publications"])``.  Keyed by ``author_id``, this avoids
# re-fetching the full author profile on every ``scholarly_fetch_citation``
# call — turning O(N) profile fetches into O(1) per author.
_author_pubs_cache: dict[str, list[dict[str, Any]]] = {}


def _ensure_initialized(api_key: str) -> None:
    """Lazily configure ``scholarly`` with a ScraperAPI proxy on first use.

    Must be called while holding ``_scholarly_lock``.  If initialization
    fails, ``_initialized`` remains ``False`` so the next call retries.
    The exception propagates to the caller.
    """
    global _initialized
    if _initialized:
        return
    from scholarly import ProxyGenerator

    pg = ProxyGenerator()
    pg.ScraperAPI(api_key)
    scholarly.use_proxy(pg)
    scholarly.set_retries(3)
    _initialized = True
    _log.info("scholarly initialized with ScraperAPI proxy")


def _fetch_author_pubs(
    api_key: str,
    author_id: str,
    num: int = 100,
) -> list[dict[str, Any]]:
    """Fetch and cache the publications list for *author_id*.

    Must be called while holding ``_scholarly_lock``.  Returns the cached
    list on subsequent calls for the same author.
    """
    cached = _author_pubs_cache.get(author_id)
    if cached is not None:
        return cached

    _ensure_initialized(api_key)
    try:
        author = scholarly.search_author_id(
            author_id, sortby="year", publication_limit=num,
        )
        author = scholarly.fill(author, sections=["publications"])
    except Exception as exc:
        _log.warning("scholarly author fetch failed for %s: %s", author_id, exc)
        return []

    pubs: list[dict[str, Any]] = author.get("publications") or []
    _author_pubs_cache[author_id] = pubs
    return pubs


def scholarly_fetch_author_publications(
    api_key: str,
    author_id: str,
    num: int = 100,
    start: int = 0,
) -> dict[str, Any]:
    """Fetch publications for *author_id* via ``scholarly``.

    Returns a dict matching the CiteForge ``fetch_author_publications()``
    format::

        {"articles": [...], "search_metadata": {"status": "Success", "source": "scholarly"}}
    """
    if not api_key:
        return {}

    with _scholarly_lock:
        publications = _fetch_author_pubs(api_key, author_id, num=num)

    articles: list[dict[str, Any]] = []

    for pub in publications[start:]:
        bib: dict[str, Any] = pub.get("bib") or {}
        title = bib.get("title", "")
        if not title:
            continue

        author_pub_id: str = pub.get("author_pub_id") or ""
        citation_id = author_pub_id.split(":")[-1]

        year_raw = str(bib.get("pub_year", ""))
        year_int: int | str = int(year_raw) if year_raw.isdigit() else ""

        article: dict[str, Any] = {
            "title": title,
            "authors": bib.get("author", ""),
            "year": year_int,
            "citation_id": citation_id,
            "result_id": citation_id,
            "source": "scholar",
        }

        venue = bib.get("venue") or bib.get("citation", "")
        if venue:
            article["publication_info"] = {"summary": venue}
            article["publication"] = venue

        articles.append(article)

    return {
        "articles": articles,
        "search_metadata": {"status": "Success", "source": "scholarly"},
    }


def scholarly_fetch_citation(
    api_key: str,
    author_id: str,
    citation_id: str,
) -> dict[str, str] | None:
    """Fetch detailed citation metadata for a single publication.

    Uses the cached author publications list to locate the target entry,
    then calls ``scholarly.fill(pub)`` to fetch full details.  Returns a
    dict with field names matching the CiteForge Scholar citation format,
    or ``None`` on failure.
    """
    if not api_key or not author_id or not citation_id:
        return None

    target_pub_id = f"{author_id}:{citation_id}"

    with _scholarly_lock:
        publications = _fetch_author_pubs(api_key, author_id)

        pub: dict[str, Any] | None = next(
            (p for p in publications
             if p.get("author_pub_id") == target_pub_id),
            None,
        )

        if pub is None:
            _log.debug("Publication %s not found in author profile", target_pub_id)
            return None

        try:
            pub = scholarly.fill(pub)
        except Exception as exc:
            _log.warning("scholarly fill failed for %s: %s", target_pub_id, exc)
            return None

    bib: dict[str, Any] = pub.get("bib") or {}
    fields: dict[str, str] = {}

    if bib.get("title"):
        fields["title"] = str(bib["title"])

    author_val = bib.get("author")
    if author_val:
        if isinstance(author_val, list):
            fields["authors"] = " and ".join(str(a) for a in author_val)
        else:
            fields["authors"] = str(author_val)

    if bib.get("pub_year"):
        fields["publication date"] = str(bib["pub_year"])

    for key in ("journal", "volume", "pages", "publisher"):
        if bib.get(key):
            fields[key] = str(bib[key])

    if bib.get("abstract"):
        fields["description"] = str(bib["abstract"])

    if bib.get("venue"):
        fields.setdefault("journal", str(bib["venue"]))

    return fields if fields else None
