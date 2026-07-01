from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..cache import response_cache
from ..config import (
    CACHE_TTL_SEARCH_DAYS,
    SIM_AUTHOR_BONUS,
    SIM_MERGE_DUPLICATE_THRESHOLD,
    SIM_TITLE_SIM_MIN,
    SIM_TITLE_WEIGHT,
    SIM_YEAR_BONUS,
    SIM_YEAR_MATCH_WINDOW,
)
from ..id_utils import find_doi_in_text
from ..text_utils import (
    authors_overlap,
    extract_year_from_any,
    normalize_title,
    title_similarity,
    trim_title_default,
)
from .helpers import (
    _score_candidate_generic,
    get_article_year,
    get_current_year,
)
from .serpapi_scholar import serpapi_fetch_author_publications
from .serply_scholar import serply_fetch_citation

_log = logging.getLogger("CiteForge.scholar")


def _authors_as_str(authors: Any) -> str:
    """Coerce an authors value (list or str) to a comma-separated string."""
    if isinstance(authors, list):
        return ", ".join(str(a) for a in authors)
    return str(authors or "")


def _first_author_sortkey(authors: Any) -> str:
    """Extract a lowercase first-author string for sorting.

    Handles list-of-dicts, list-of-str, and plain str formats.
    """
    if isinstance(authors, list) and authors:
        first = authors[0]
        if isinstance(first, dict):
            return (first.get("name") or "").lower()
        return str(first).lower()
    if isinstance(authors, str):
        return authors.split(",")[0].split(" and ")[0].strip().lower()
    return ""


def _cache_covers_window(cached: dict[str, Any], min_year: int) -> bool:
    """Check whether a cached SerpAPI result covers the full contribution window.

    Returns *False* when the cache was likely truncated by the old count-based
    regime (article count is a multiple of 100 AND the oldest article is still
    above *min_year*).
    """
    articles = cached.get("articles") or []
    if not articles:
        return False
    years = [a.get("year") for a in articles if isinstance(a.get("year"), int) and a["year"] > 0]
    if not years or min(years) <= min_year:
        return True
    return len(articles) % 100 != 0


def fetch_author_publications(
    api_key: str,
    author_id: str,
    _author_name: str,
    num: int = 100,
    min_year: int = 0,
) -> dict[str, Any]:
    """Fetch publications for an author from Google Scholar via SerpAPI.

    Uses the ``google_scholar_author`` engine for exact profile matching
    by Scholar ID, with pagination support (up to 100 results per page).

    Args:
        api_key: SerpAPI key.
        author_id: Google Scholar profile ID.
        _author_name: Author name (unused by SerpAPI, kept for interface compat).
        num: Maximum number of articles to return (hard safety cap).
        min_year: Minimum publication year.  When > 0, stale caches that
            do not cover the window are invalidated and re-fetched.
    """
    cache_key = f"{author_id}|page_0"
    cached = response_cache.get("serpapi_publications", cache_key)
    if cached is not None:
        if min_year > 0 and not _cache_covers_window(cached, min_year):
            _log.info(
                "Cache for %s does not cover min_year=%d; re-fetching",
                author_id,
                min_year,
            )
            response_cache.invalidate("serpapi_publications", cache_key)
        else:
            return cached or {}

    result = serpapi_fetch_author_publications(api_key, author_id, num=num, min_year=min_year)
    if result and result.get("articles"):
        response_cache.put("serpapi_publications", cache_key, result, ttl_days=CACHE_TTL_SEARCH_DAYS)
        _log.info("Fetched %d articles via SerpAPI for %s", len(result["articles"]), author_id)
    return result


def fetch_scholar_citation(
    api_key: str,
    title: str,
    author_name: str,
) -> dict[str, str] | None:
    """Fetch citation details for an article from Google Scholar via Serply."""
    if not title:
        return None

    title_hash = hashlib.sha256(title.lower().encode("utf-8")).hexdigest()[:16]
    cache_key = f"{title_hash}|{author_name}"

    cached = response_cache.get("serply_citation", cache_key)
    if cached is not None:
        if cached.get("_negative"):
            _log.debug("serply_citation | NEG_HIT | key=%s", cache_key[:60])
            return None
        return cached or None

    result = serply_fetch_citation(api_key, title, author_name)
    if result:
        response_cache.put("serply_citation", cache_key, result, ttl_days=CACHE_TTL_SEARCH_DAYS)
        _log.info("Fetched citation via Serply for '%s'", title[:50])
    return result


def build_bibtex_from_scholar_fields(fields: dict[str, str], keyhint: str) -> str | None:
    """Turn structured fields parsed from a Scholar citation page into a BibTeX entry."""
    from ..bibtex_build import build_bibtex_entry, determine_entry_type
    from ..text_utils import extract_authors_from_any, safe_get_field

    title = safe_get_field(fields, "title") or safe_get_field(fields, "paper title")
    if not title:
        return None

    authors = extract_authors_from_any(fields.get("authors") or "")
    pub_date = fields.get("publication date") or fields.get("year") or ""
    year = extract_year_from_any(pub_date, fallback=0) or 0
    venue = safe_get_field(fields, "journal") or safe_get_field(fields, "conference") or safe_get_field(fields, "book")
    entry_type = determine_entry_type(fields, venue_hints={"journal": "article", "conference": "inproceedings"})

    volume = safe_get_field(fields, "volume")
    number = safe_get_field(fields, "issue") or safe_get_field(fields, "number")
    pages = safe_get_field(fields, "pages")
    publisher = safe_get_field(fields, "publisher")
    doi_candidate = safe_get_field(fields, "doi") or safe_get_field(fields, "url")
    doi = find_doi_in_text(doi_candidate) if doi_candidate else None
    url = safe_get_field(fields, "url")

    extra_fields = {
        k: v
        for k, v in {"volume": volume, "number": number, "pages": pages, "publisher": publisher}.items()
        if v is not None
    }
    return build_bibtex_entry(
        entry_type=entry_type,
        title=title,
        authors=authors,
        year=year,
        keyhint=keyhint,
        venue=venue,
        doi=doi,
        url=url,
        extra_fields=extra_fields,
    )


def sort_articles_by_year_current_first(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort articles with current year first, then descending by year."""
    cur = get_current_year()

    def key_func(a: dict[str, Any]) -> tuple[int, int, str, str]:
        y = get_article_year(a)
        group = 0 if y == cur else 1
        return (group, -y, normalize_title(a.get("title") or ""), _first_author_sortkey(a.get("authors") or []))

    return sorted(articles, key=key_func)


def _deduplicate_publication_list(
    pubs: list[dict[str, Any]],
    _target_author: str | None = None,
) -> list[dict[str, Any]]:
    """Remove internal duplicates from a single publication list."""
    if not pubs:
        return []

    def sort_key(pub: dict[str, Any]) -> tuple[int, str, str]:
        year = extract_year_from_any(pub.get("year"), fallback=0) or 0
        return (-year, normalize_title(pub.get("title") or ""), _first_author_sortkey(pub.get("authors") or []))

    sorted_pubs = sorted(pubs, key=sort_key)
    deduplicated: list[dict[str, Any]] = []
    seen_normalized: set[str] = set()

    for pub in sorted_pubs:
        p_title_raw = pub.get("title") or ""
        p_title = trim_title_default(p_title_raw)
        p_norm = normalize_title(p_title)
        p_year = pub.get("year")
        p_authors = pub.get("authors") or []

        if p_norm and p_norm in seen_normalized:
            continue

        is_duplicate = False
        for existing in deduplicated:
            e_title = existing.get("title") or ""
            e_year = existing.get("year")
            e_authors = existing.get("authors") or []
            tsim = title_similarity(p_title, e_title) if p_title and e_title else 0.0
            if tsim < SIM_TITLE_SIM_MIN:
                continue
            score = SIM_TITLE_WEIGHT * tsim
            if authors_overlap(_authors_as_str(e_authors), _authors_as_str(p_authors)):
                score += SIM_AUTHOR_BONUS
            e_year_int = extract_year_from_any(e_year) if e_year else None
            p_year_int = extract_year_from_any(p_year) if p_year else None
            if e_year_int is not None and p_year_int is not None:
                score += SIM_YEAR_BONUS * (1.0 if abs(e_year_int - p_year_int) <= SIM_YEAR_MATCH_WINDOW else 0.0)
            if score >= SIM_MERGE_DUPLICATE_THRESHOLD:
                is_duplicate = True
                break
        if not is_duplicate:
            pub_copy = dict(pub)
            if p_title and p_title != p_title_raw:
                pub_copy["title"] = p_title
            deduplicated.append(pub_copy)
            if p_norm:
                seen_normalized.add(p_norm)
    return deduplicated


def merge_publication_lists(
    primary: list[dict[str, Any]], secondary: list[dict[str, Any]], target_author: str | None
) -> list[dict[str, Any]]:
    """Merge two publication lists into one unified list with complete deduplication."""
    primary_deduped = _deduplicate_publication_list(primary, target_author)
    secondary_deduped = _deduplicate_publication_list(secondary, target_author)
    merged: list[dict[str, Any]] = list(primary_deduped)
    if not secondary_deduped:
        return merged
    for sec in secondary_deduped:
        s_title_raw = sec.get("title") or ""
        s_title = trim_title_default(s_title_raw)
        s_year = sec.get("year")
        s_authors = sec.get("authors") or []
        is_duplicate = False
        for p in merged:
            tsim = title_similarity(s_title, p.get("title") or "") if s_title else 0.0
            if tsim < SIM_TITLE_SIM_MIN:
                continue
            score = _score_candidate_generic(
                target_title=p.get("title") or "",
                target_author=target_author,
                target_year=p.get("year"),
                cand_title=s_title,
                cand_authors=s_authors,
                cand_year=s_year,
                title_sim=title_similarity,
                author_match=authors_overlap,
            )
            if score >= SIM_MERGE_DUPLICATE_THRESHOLD:
                is_duplicate = True
                break
        if not is_duplicate:
            sec2 = dict(sec)
            if s_title and s_title != s_title_raw:
                sec2["title"] = s_title
            merged.append(sec2)
    return merged
