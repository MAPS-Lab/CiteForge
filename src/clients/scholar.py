from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any

from ..cache import response_cache
from ..config import (
    CACHE_TTL_SEARCH_DAYS,
    SERPAPI_BASE,
    SIM_AUTHOR_BONUS,
    SIM_MERGE_DUPLICATE_THRESHOLD,
    SIM_SCHOLAR_FUZZY_ACCEPT,
    SIM_TITLE_SIM_MIN,
    SIM_TITLE_WEIGHT,
    SIM_YEAR_BONUS,
    SIM_YEAR_MATCH_WINDOW,
)
from ..exceptions import ALL_API_ERRORS, DECODE_ERRORS, FIELD_ACCESS_ERRORS, PARSE_ERRORS
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


def fetch_author_publications(
    api_key: str, author_id: str, num: int = 100, start: int = 0,
) -> dict[str, Any]:
    """Fetch publications for an author from Google Scholar via SerpAPI."""
    @handle_api_errors(default_return={})
    def _fetch() -> dict[str, Any]:
        params = {
            "engine": "google_scholar_author",
            "author_id": author_id,
            "api_key": api_key,
            "num": num,
            "start": start,
        }
        url = build_url(SERPAPI_BASE, params)
        return http_get_json(url)

    result: dict[str, Any] = _fetch()
    return result


def extract_cite_link(article: dict[str, Any]) -> str | None:
    """Find the URL for Scholar's cite dialog by checking multiple nested locations."""
    from .helpers import strip_html_tags as _strip  # noqa: F401 (unused but keeps import consistency)

    inline = article.get("inline_links") or {}
    cite_link = inline.get("serpapi_cite_link") or article.get("serpapi_cite_link")
    if cite_link:
        return str(cite_link)
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


def fetch_scholar_citation_via_serpapi(
    api_key: str, author_id: str, citation_id: str
) -> dict[str, str] | None:
    """Fetch individual article citation details from Google Scholar using SerpAPI."""
    if not api_key or not author_id or not citation_id:
        return None

    cache_key = f"{author_id}|{citation_id}"
    cached = response_cache.get("serpapi_citation", cache_key)
    if cached is not None:
        return cached if cached else None

    params = {
        "engine": "google_scholar_author",
        "author_id": author_id,
        "view_op": "view_citation",
        "citation_id": citation_id,
        "api_key": api_key,
    }
    url = build_url("https://serpapi.com/search", params)

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
        if fields:
            response_cache.put("serpapi_citation", cache_key, fields, ttl_days=CACHE_TTL_SEARCH_DAYS)
        return fields if fields else None

    except ALL_API_ERRORS:
        return None


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


essential_result_keys = ("organic_results", "results")


def fetch_bibtex_from_cite(api_key: str, cite_url: str) -> str:
    """Retrieve the BibTeX text for a publication using Google Scholar's cite dialog through SerpAPI."""
    parsed = urllib.parse.urlparse(cite_url)
    q = urllib.parse.parse_qs(parsed.query)
    q["api_key"] = [api_key]
    new_query = urllib.parse.urlencode({k: v[0] if isinstance(v, list) else v for k, v in q.items()})
    cite_with_key = urllib.parse.urlunparse(parsed._replace(query=new_query))
    json_headers = DEFAULT_JSON_HEADERS.copy()
    raw = http_fetch_bytes(cite_with_key, json_headers, timeout=30.0)
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
                title = (c.get("title") or c.get("name") or "").strip().lower()
                file_format = (c.get("file_format") or "").strip().lower()
                if title == "bibtex" or file_format == "bibtex":
                    link = c.get("serpapi_link") or c.get("serpapi_url") or c.get("link") or c.get("url")
                    if link:
                        return str(link)
        return None

    bib_link = find_bibtex_link(cite_json)
    if not bib_link:
        available = ",".join(cite_json)
        raise ValueError(f"BibTeX link not found in citation formats. Available keys: {available}")
    try:
        p = urllib.parse.urlparse(bib_link)
        if p.netloc.endswith("serpapi.com"):
            q2 = urllib.parse.parse_qs(p.query)
            if "api_key" not in q2:
                q2["api_key"] = [api_key]
                bib_link = urllib.parse.urlunparse(p._replace(
                    query=urllib.parse.urlencode({k: v[0] if isinstance(v, list) else v for k, v in q2.items()})))
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


@handle_api_errors(default_return=None)
def search_scholar_for_cite_link(api_key: str, title: str, author_name: str | None = None) -> str | None:
    """Query Google Scholar for a paper by title and return the best matching cite dialog link."""
    q = f'"{title}"' if title else title
    params = {"engine": "google_scholar", "q": q, "api_key": api_key, "num": 10}
    if author_name:
        params["as_sauthors"] = author_name
    url = build_url(SERPAPI_BASE, params)
    data = http_get_json(url)
    results = next((data.get(key) or [] for key in essential_result_keys if key in data), [])
    if not results:
        return None
    target_norm = normalize_title(title)

    def candidate_authors(item: dict[str, Any]) -> Any:
        authors = item.get("authors")
        if isinstance(authors, (list, str)):
            return authors
        pubinfo = item.get("publication_info") or {}
        if isinstance(pubinfo, dict):
            return pubinfo.get("authors") or pubinfo.get("summary") or item.get("snippet")
        return item.get("snippet")

    for r in results:
        r_title = r.get("title") or r.get("name")
        if normalize_title(r_title) != target_norm:
            continue
        if author_name:
            cand = candidate_authors(r)
            if not author_name_matches(author_name, cand) and not author_in_text(author_name, cand):
                continue
        link = (r.get("inline_links") or {}).get("serpapi_cite_link") or r.get("serpapi_cite_link")
        if link:
            return str(link)
    best = None
    best_tsim = 0.0
    for r in results:
        r_title = r.get("title") or r.get("name") or ""
        tsim = title_similarity(title, r_title)
        if tsim > best_tsim:
            best = r
            best_tsim = tsim
    if best and best_tsim >= SIM_SCHOLAR_FUZZY_ACCEPT:
        if author_name:
            cand = candidate_authors(best)
            if author_name_matches(author_name, cand) or author_in_text(author_name, cand):
                link = (best.get("inline_links") or {}).get("serpapi_cite_link") or best.get("serpapi_cite_link")
                if link:
                    return str(link)
        else:
            link = (best.get("inline_links") or {}).get("serpapi_cite_link") or best.get("serpapi_cite_link")
            if link:
                return str(link)
    return None


def sort_articles_by_year_current_first(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort articles with current year first, then descending by year."""
    cur = get_current_year()

    def key_func(a: dict[str, Any]) -> tuple[int, int, str, str]:
        y = get_article_year(a)
        group = 0 if y == cur else 1
        title = normalize_title(a.get("title") or "")
        authors = a.get("authors") or []
        if isinstance(authors, list) and authors:
            first_author = ((authors[0].get("name") or "") if isinstance(authors[0], dict) else str(authors[0])).lower()
        elif isinstance(authors, str):
            first_author = authors.split(",")[0].split(" and ")[0].strip().lower()
        else:
            first_author = ""
        return (group, -y, title, first_author)

    return sorted(articles, key=key_func)


def _deduplicate_publication_list(
    pubs: list[dict[str, Any]], _target_author: str | None = None,
) -> list[dict[str, Any]]:
    """Remove internal duplicates from a single publication list."""
    if not pubs:
        return []

    def sort_key(pub: dict[str, Any]) -> tuple[int, str, str]:
        year = extract_year_from_any(pub.get("year"), fallback=0) or 0
        title = normalize_title(pub.get("title") or "")
        authors = pub.get("authors") or []
        if isinstance(authors, list) and authors:
            first_author = ((authors[0].get("name") or "") if isinstance(authors[0], dict) else str(authors[0])).lower()
        elif isinstance(authors, str):
            first_author = authors.split(",")[0].split(" and ")[0].strip().lower()
        else:
            first_author = ""
        return (-year, title, first_author)

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
