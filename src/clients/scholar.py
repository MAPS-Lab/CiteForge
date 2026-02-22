from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from typing import Any

from ..browser import NODRIVER_AVAILABLE, ScholarBrowserLoop
from ..cache import response_cache
from ..config import (
    CACHE_TTL_SEARCH_DAYS,
    SCHOLAR_BROWSER_BACKOFF_BASE,
    SCHOLAR_BROWSER_BACKOFF_CAP,
    SCHOLAR_BROWSER_CIRCUIT_THRESHOLD,
    SERPAPI_BASE,
    SIM_AUTHOR_BONUS,
    SIM_MERGE_DUPLICATE_THRESHOLD,
    SIM_SCHOLAR_FUZZY_ACCEPT,
    SIM_TITLE_SIM_MIN,
    SIM_TITLE_WEIGHT,
    SIM_YEAR_BONUS,
    SIM_YEAR_MATCH_WINDOW,
)
from ..exceptions import ALL_API_ERRORS, DECODE_ERRORS, FIELD_ACCESS_ERRORS, PARSE_ERRORS, ScholarBrowserBlockedError
from ..http_utils import DEFAULT_JSON_HEADERS, handle_api_errors, http_fetch_bytes, http_get_json
from ..id_utils import find_doi_in_text
from ..text_utils import (
    author_in_text,
    author_name_matches,
    authors_overlap,
    build_url,
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
from .scholar_browser import (
    browser_fetch_author_publications,
    browser_fetch_bibtex,
    browser_fetch_citation_detail,
    browser_search_scholar,
)

_log = logging.getLogger("CiteForge.scholar")

_ESSENTIAL_RESULT_KEYS = ("organic_results", "results")

# Circuit breaker state: stop trying the browser after repeated blocks
_circuit_lock = threading.Lock()
_browser_consecutive_errors = 0
_browser_circuit_open = False


def _first_author_sortkey(authors: Any) -> str:
    """Extract a lowercase first-author string for sorting, handling list-of-dicts, list-of-str, and str."""
    if isinstance(authors, list) and authors:
        first = authors[0]
        return ((first.get("name") or "") if isinstance(first, dict) else str(first)).lower()
    if isinstance(authors, str):
        return authors.split(",")[0].split(" and ")[0].strip().lower()
    return ""


def _extract_serpapi_cite_link(result: dict[str, Any]) -> str | None:
    """Extract the SerpAPI cite link from a Scholar search result, checking nested and top-level locations."""
    link = (result.get("inline_links") or {}).get("serpapi_cite_link") or result.get("serpapi_cite_link")
    return str(link) if link else None


# ======================================================================
# SerpAPI implementations (private, used as fallback)
# ======================================================================


def _serpapi_fetch_author_publications(
    api_key: str, author_id: str, num: int = 100, start: int = 0,
) -> dict[str, Any]:
    """Fetch publications for an author from Google Scholar via SerpAPI."""
    if not api_key:
        return {}

    @handle_api_errors(default_return={})
    def _fetch() -> dict[str, Any]:
        params = {
            "engine": "google_scholar_author",
            "author_id": author_id,
            "api_key": api_key,
            "num": num,
            "start": start,
        }
        return http_get_json(build_url(SERPAPI_BASE, params))

    return _fetch()


def _serpapi_fetch_citation(
    api_key: str, author_id: str, citation_id: str
) -> dict[str, str] | None:
    """Fetch individual article citation details from Google Scholar using SerpAPI."""
    if not api_key or not author_id or not citation_id:
        return None

    params = {
        "engine": "google_scholar_author",
        "author_id": author_id,
        "view_op": "view_citation",
        "citation_id": citation_id,
        "api_key": api_key,
    }
    url = build_url(SERPAPI_BASE, params)

    try:
        data = http_get_json(url, timeout=20.0)
        citation = data.get("citation", {})
        if not citation:
            return None

        fields: dict[str, str] = {}
        if citation.get("title"):
            fields["title"] = citation["title"]
        authors = citation.get("authors")
        if authors:
            fields["authors"] = ", ".join(authors) if isinstance(authors, list) else str(authors)
        if citation.get("publication_date"):
            fields["publication date"] = citation["publication_date"]
        for key in ("journal", "conference", "book", "volume", "issue", "pages", "publisher", "description"):
            if citation.get(key):
                fields[key] = citation[key]
        return fields if fields else None

    except ALL_API_ERRORS as e:
        _log.debug("SerpAPI citation fetch failed for %s:%s: %s", author_id, citation_id, e)
        return None


@handle_api_errors(default_return=None)
def _serpapi_search_scholar_for_cite_link(api_key: str, title: str, author_name: str | None = None) -> str | None:
    """Query Google Scholar for a paper by title and return the best matching cite dialog link via SerpAPI."""
    q = f'"{title}"' if title else title
    params = {"engine": "google_scholar", "q": q, "api_key": api_key, "num": 10}
    if author_name:
        params["as_sauthors"] = author_name
    data = http_get_json(build_url(SERPAPI_BASE, params))
    results = next((data.get(key) or [] for key in _ESSENTIAL_RESULT_KEYS if key in data), [])
    if not results:
        return None
    target_norm = normalize_title(title)

    def candidate_authors(item: dict[str, Any]) -> Any:
        item_authors = item.get("authors")
        if isinstance(item_authors, (list, str)):
            return item_authors
        pubinfo = item.get("publication_info") or {}
        if isinstance(pubinfo, dict):
            return pubinfo.get("authors") or pubinfo.get("summary") or item.get("snippet")
        return item.get("snippet")

    def _author_ok(result: dict[str, Any]) -> bool:
        """Return True if author_name is absent or matches the result's authors."""
        if not author_name:
            return True
        cand = candidate_authors(result)
        return author_name_matches(author_name, cand) or author_in_text(author_name, cand)

    # Pass 1: exact normalized-title match
    for r in results:
        r_title = r.get("title") or r.get("name")
        if normalize_title(r_title) != target_norm:
            continue
        if not _author_ok(r):
            continue
        link = _extract_serpapi_cite_link(r)
        if link:
            return link

    # Pass 2: fuzzy best-match fallback
    best: dict[str, Any] | None = None
    best_tsim = 0.0
    for r in results:
        r_title = r.get("title") or r.get("name") or ""
        tsim = title_similarity(title, r_title)
        if tsim > best_tsim:
            best, best_tsim = r, tsim
    if best and best_tsim >= SIM_SCHOLAR_FUZZY_ACCEPT and _author_ok(best):
        return _extract_serpapi_cite_link(best)
    return None


def _inject_api_key(url: str, api_key: str) -> str:
    """Ensure ``api_key`` is present in the query string of *url*."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    if "api_key" not in qs:
        qs["api_key"] = [api_key]
    flat = {k: v[0] if isinstance(v, list) else v for k, v in qs.items()}
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(flat)))


def _serpapi_fetch_bibtex_from_cite(api_key: str, cite_url: str) -> str:
    """Retrieve the BibTeX text for a publication using Scholar's cite dialog through SerpAPI."""
    cite_with_key = _inject_api_key(cite_url, api_key)
    raw = http_fetch_bytes(cite_with_key, DEFAULT_JSON_HEADERS.copy(), timeout=30.0)
    try:
        cite_json = json.loads(raw.decode("utf-8"))
    except DECODE_ERRORS:
        cite_json = json.loads(raw.decode("utf-8", errors="replace"))

    def find_bibtex_link(obj: dict[str, Any]) -> str | None:
        for key in ("citations", "links", "resources"):
            container = obj.get(key)
            if not isinstance(container, list):
                continue
            for c in container:
                if not isinstance(c, dict):
                    continue
                label = (c.get("title") or c.get("name") or "").strip().lower()
                file_format = (c.get("file_format") or "").strip().lower()
                if label == "bibtex" or file_format == "bibtex":
                    link = c.get("serpapi_link") or c.get("serpapi_url") or c.get("link") or c.get("url")
                    if link:
                        return str(link)
        return None

    bib_link = find_bibtex_link(cite_json)
    if not bib_link:
        available = ",".join(cite_json)
        raise ValueError(f"BibTeX link not found in citation formats. Available keys: {available}")
    try:
        parsed = urllib.parse.urlparse(bib_link)
        if parsed.netloc.endswith("serpapi.com"):
            bib_link = _inject_api_key(bib_link, api_key)
    except FIELD_ACCESS_ERRORS:
        pass
    text_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    raw_bib = http_fetch_bytes(bib_link, text_headers, timeout=30.0)
    try:
        return raw_bib.decode("utf-8")
    except DECODE_ERRORS:
        return raw_bib.decode("latin-1", errors="replace")


# ======================================================================
# Public facade functions: browser-first, SerpAPI fallback
# ======================================================================


def _run_browser_coro(coro_factory: Any, context: str) -> Any | None:
    """Run a browser coroutine with circuit breaker and back-off.

    ``coro_factory`` receives the browser instance and returns a coroutine.
    ``context`` is used in log messages to identify the operation.

    After ``SCHOLAR_BROWSER_CIRCUIT_THRESHOLD`` consecutive errors the circuit
    opens and all subsequent calls return ``None`` immediately (SerpAPI-only
    mode for the rest of the session).  Each retry before that uses a linear
    back-off capped at ``SCHOLAR_BROWSER_BACKOFF_CAP`` seconds.
    """
    global _browser_consecutive_errors, _browser_circuit_open

    if not NODRIVER_AVAILABLE:
        return None

    with _circuit_lock:
        if _browser_circuit_open:
            return None
        errors = _browser_consecutive_errors

    # Back-off: wait before retrying after prior errors
    if errors > 0:
        delay = min(SCHOLAR_BROWSER_BACKOFF_BASE * errors, SCHOLAR_BROWSER_BACKOFF_CAP)
        _log.info("Browser back-off: %.1fs (attempt after %d error(s))", delay, errors)
        time.sleep(delay)

    try:
        loop = ScholarBrowserLoop()

        async def _wrapped() -> Any:
            browser = await loop.get_browser()
            return await coro_factory(browser)

        result = loop.run(_wrapped())

        # Success: reset error counter
        with _circuit_lock:
            if _browser_consecutive_errors > 0:
                _log.info("Browser recovered after %d error(s)", _browser_consecutive_errors)
            _browser_consecutive_errors = 0

        return result

    except ScholarBrowserBlockedError as e:
        with _circuit_lock:
            _browser_consecutive_errors += 1
            count = _browser_consecutive_errors
            if count >= SCHOLAR_BROWSER_CIRCUIT_THRESHOLD:
                _browser_circuit_open = True
                _log.warning(
                    "Circuit breaker OPEN after %d consecutive blocks -- switching to SerpAPI-only",
                    count,
                )
            else:
                _log.warning(
                    "Browser blocked (%s): %s [%d/%d before circuit opens]",
                    context, e, count, SCHOLAR_BROWSER_CIRCUIT_THRESHOLD,
                )

    except Exception as e:
        with _circuit_lock:
            _browser_consecutive_errors += 1
            count = _browser_consecutive_errors
            if count >= SCHOLAR_BROWSER_CIRCUIT_THRESHOLD:
                _browser_circuit_open = True
                _log.warning(
                    "Circuit breaker OPEN after %d consecutive errors -- switching to SerpAPI-only",
                    count,
                )
            else:
                _log.warning(
                    "Browser error (%s): %s [%d/%d before circuit opens]",
                    context, e, count, SCHOLAR_BROWSER_CIRCUIT_THRESHOLD,
                )

    return None


def fetch_author_publications(
    api_key: str, author_id: str, num: int = 100, start: int = 0,
) -> dict[str, Any]:
    """Fetch publications for an author -- tries headless browser first, falls back to SerpAPI."""
    cache_key = f"{author_id}|page_{start}"
    cached = response_cache.get("scholar_publications", cache_key)
    if cached is not None:
        return cached if cached else {}

    result: dict[str, Any] | None = _run_browser_coro(
        lambda b: browser_fetch_author_publications(b, author_id, num=num, start=start),
        f"author {author_id}",
    )
    if result and result.get("articles"):
        response_cache.put("scholar_publications", cache_key, result, ttl_days=CACHE_TTL_SEARCH_DAYS)
        _log.info("Fetched %d articles via browser for %s", len(result["articles"]), author_id)
        return result

    return _serpapi_fetch_author_publications(api_key, author_id, num, start)


def fetch_scholar_citation_via_serpapi(
    api_key: str, author_id: str, citation_id: str
) -> dict[str, str] | None:
    """Fetch citation details -- tries headless browser first, falls back to SerpAPI."""
    if not author_id or not citation_id:
        return None

    cache_key = f"{author_id}|{citation_id}"
    for namespace in ("scholar_citation", "serpapi_citation"):
        cached = response_cache.get(namespace, cache_key)
        if cached is not None:
            return cached if cached else None

    if NODRIVER_AVAILABLE:
        fields: dict[str, str] | None = _run_browser_coro(
            lambda b: browser_fetch_citation_detail(b, author_id, citation_id),
            f"citation {author_id}:{citation_id}",
        )
        if fields:
            response_cache.put("scholar_citation", cache_key, fields, ttl_days=CACHE_TTL_SEARCH_DAYS)
            _log.info("Fetched citation via browser for %s:%s", author_id, citation_id)
            return fields

    return _serpapi_fetch_citation(api_key, author_id, citation_id)


def search_scholar_for_cite_link(api_key: str, title: str, author_name: str | None = None) -> str | None:
    """Search Scholar by title -- tries headless browser first, falls back to SerpAPI."""
    cache_key = f"{normalize_title(title)}|{(author_name or '').lower()}"
    cached = response_cache.get("scholar_search_cite", cache_key)
    if cached is not None:
        return cached.get("link") or None

    link: str | None = _run_browser_coro(
        lambda b: browser_search_scholar(b, title, author_name),
        f"search '{title[:60]}'",
    )
    if link:
        _log.info("Found cite link via browser for '%s'", title[:60])
        response_cache.put("scholar_search_cite", cache_key, {"link": link}, ttl_days=CACHE_TTL_SEARCH_DAYS)
        return link

    link = _serpapi_search_scholar_for_cite_link(api_key, title, author_name)
    if link:
        response_cache.put("scholar_search_cite", cache_key, {"link": link}, ttl_days=CACHE_TTL_SEARCH_DAYS)
    return link


def fetch_bibtex_from_cite(api_key: str, cite_url: str) -> str:
    """Retrieve BibTeX -- tries headless browser first for non-SerpAPI URLs, falls back to SerpAPI."""
    cached = response_cache.get("scholar_bibtex", cite_url)
    if cached is not None:
        bib: str = cached.get("bibtex", "")
        if bib:
            return bib

    if "serpapi.com" not in cite_url:
        bibtex: str | None = _run_browser_coro(
            lambda b: browser_fetch_bibtex(b, cite_url),
            "BibTeX fetch",
        )
        if bibtex:
            _log.info("Fetched BibTeX via browser")
            response_cache.put("scholar_bibtex", cite_url, {"bibtex": bibtex}, ttl_days=CACHE_TTL_SEARCH_DAYS)
            return bibtex

    result = _serpapi_fetch_bibtex_from_cite(api_key, cite_url)
    if result:
        response_cache.put("scholar_bibtex", cite_url, {"bibtex": result}, ttl_days=CACHE_TTL_SEARCH_DAYS)
    return result


# ======================================================================
# Utility functions
# ======================================================================


def extract_cite_link(article: dict[str, Any]) -> str | None:
    """Find the URL for Scholar's cite dialog by checking multiple nested locations."""
    cite_link = _extract_serpapi_cite_link(article)
    if cite_link:
        return cite_link
    inline = article.get("inline_links") or {}
    for key in ("citations", "links", "resources"):
        cont = inline.get(key)
        if isinstance(cont, list):
            for c in cont:
                if isinstance(c, dict):
                    cand = c.get("serpapi_cite_link") or c.get("serpapi_url") or c.get("serpapi_link")
                    if isinstance(cand, str) and "google_scholar_cite" in cand:
                        return cand
    try:
        txt = json.dumps(article)
        m = re.search(r"https?://[^\"']+google_scholar_cite[^\"']+", txt)
        if m:
            return m.group(0)
    except PARSE_ERRORS:
        pass
    return None


def scholar_view_citation_url(author_id: str, result_id: str) -> str:
    """Build the Google Scholar view_citation URL for a given author and result."""
    base = "https://scholar.google.com/citations"
    citation_for_view = f"{author_id}:{result_id}" if ":" not in result_id else result_id
    params = {
        "view_op": "view_citation",
        "hl": "en",
        "user": author_id,
        "sortby": "pubdate",
        "citation_for_view": citation_for_view,
    }
    return build_url(base, params)


def build_bibtex_from_scholar_fields(fields: dict[str, str], keyhint: str) -> str | None:
    """Turn structured fields parsed from a Scholar citation page into a BibTeX entry."""
    from ..bibtex_build import build_bibtex_entry, determine_entry_type
    from ..text_utils import extract_authors_from_any, extract_year_from_any, safe_get_field

    title = safe_get_field(fields, "title") or safe_get_field(fields, "paper title")
    if not title:
        return None
    authors_val = fields.get("authors") or ""
    authors = extract_authors_from_any(authors_val)
    pub_date = fields.get("publication date") or fields.get("year") or ""
    year = extract_year_from_any(pub_date, fallback=0) or 0
    venue = safe_get_field(fields, "journal") or safe_get_field(fields, "conference") or safe_get_field(fields, "book")
    entry_type = determine_entry_type(fields, venue_hints={"journal": "article", "conference": "inproceedings"})
    pages = safe_get_field(fields, "pages")
    publisher = safe_get_field(fields, "publisher")
    volume = safe_get_field(fields, "volume")
    number = safe_get_field(fields, "issue") or safe_get_field(fields, "number")
    doi_candidate = safe_get_field(fields, "doi") or safe_get_field(fields, "url")
    doi = find_doi_in_text(doi_candidate) if doi_candidate else None
    url = safe_get_field(fields, "url")
    return build_bibtex_entry(
        entry_type=entry_type, title=title, authors=authors, year=year, keyhint=keyhint,
        venue=venue or None, doi=doi, url=url,
        extra_fields={
            k: v for k, v in
            {"volume": volume, "number": number, "pages": pages, "publisher": publisher}.items()
            if v is not None
        },
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
    pubs: list[dict[str, Any]], _target_author: str | None = None,
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
        p_year = pub.get("year") or None
        p_authors = pub.get("authors") or []

        # Fast-path: exact normalized title match skips expensive fuzzy comparison
        if p_norm and p_norm in seen_normalized:
            continue

        is_duplicate = False
        for existing in deduplicated:
            e_title = existing.get("title") or ""
            e_year = existing.get("year") or None
            e_authors = existing.get("authors") or []
            tsim = title_similarity(p_title, e_title) if p_title and e_title else 0.0
            if tsim < SIM_TITLE_SIM_MIN:
                continue
            score = 0.0
            score += SIM_TITLE_WEIGHT * tsim
            e_authors_str = ", ".join(e_authors) if isinstance(e_authors, list) else str(e_authors or "")
            p_authors_str = ", ".join(p_authors) if isinstance(p_authors, list) else str(p_authors or "")
            if authors_overlap(e_authors_str, p_authors_str):
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


def merge_publication_lists(primary: list[dict[str, Any]], secondary: list[dict[str, Any]],
                            target_author: str | None) -> list[dict[str, Any]]:
    """Merge two publication lists into one unified list with complete deduplication."""
    primary_deduped = _deduplicate_publication_list(primary, target_author) if primary else []
    secondary_deduped = _deduplicate_publication_list(secondary, target_author) if secondary else []
    merged: list[dict[str, Any]] = list(primary_deduped)
    if not secondary_deduped:
        return merged
    for sec in secondary_deduped:
        s_title_raw = sec.get("title") or ""
        s_title = trim_title_default(s_title_raw)
        s_year = sec.get("year") or None
        s_authors = sec.get("authors") or []
        best = 0.0
        for p in merged:
            tsim = title_similarity(s_title, p.get("title") or "") if s_title else 0.0
            if tsim < SIM_TITLE_SIM_MIN:
                continue
            ps_year = p.get("year") or None
            sc = _score_candidate_generic(
                target_title=p.get("title") or "", target_author=target_author, target_year=ps_year,
                cand_title=s_title, cand_authors=s_authors, cand_year=s_year,
                title_sim=title_similarity,
                author_match=lambda author_name_value, author_list: authors_overlap(author_name_value, author_list),
            )
            if sc > best:
                best = sc
            if best >= SIM_MERGE_DUPLICATE_THRESHOLD:
                break
        if best < SIM_MERGE_DUPLICATE_THRESHOLD:
            sec2 = dict(sec)
            if s_title and s_title != s_title_raw:
                sec2["title"] = s_title
            merged.append(sec2)
    return merged
