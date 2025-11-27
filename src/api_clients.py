from __future__ import annotations

import copy
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable

from .bibtex_utils import make_bibkey
from .log_utils import logger, LogSource, LogCategory
from .config import (
    SERPAPI_BASE,
    HTTP_TIMEOUT_SHORT,
    ARXIV_BASE,
    OPENREVIEW_BASE,
    DBLP_BASE,
    DBLP_PERSON_BASE,
    GEMINI_BASE,
    PUBMED_BASE,
    DATACITE_BASE,
    ORCID_BASE,
    SIM_TITLE_WEIGHT,
    SIM_AUTHOR_BONUS,
    SIM_YEAR_BONUS,
    SIM_YEAR_MATCH_WINDOW,
    SIM_TITLE_SIM_MIN,
    SIM_EXACT_PICK_THRESHOLD,
    SIM_BEST_ITEM_THRESHOLD,
    SIM_SCHOLAR_FUZZY_ACCEPT,
    SIM_MERGE_DUPLICATE_THRESHOLD,
)
from .exceptions import (
    NETWORK_ERRORS, PARSE_ERRORS, DECODE_ERRORS, ALL_API_ERRORS, NUMERIC_ERRORS,
    XML_PARSE_ERRORS, FIELD_ACCESS_ERRORS
)
from .http_utils import (
    http_get_json, http_get_text, http_fetch_bytes, s2_http_get_json,
    DEFAULT_JSON_HEADERS, handle_api_errors
)
from .id_utils import _norm_doi, find_doi_in_text, find_arxiv_in_text
from .io_utils import safe_read_file
from .text_utils import (
    build_url,
    normalize_title,
    trim_title_default,
    author_name_matches,
    author_in_text,
    title_similarity,
    authors_overlap,
    format_author_dirname,
    extract_year_from_any,
    safe_get_nested,
)


def _safe_make_key(title: str, authors: List[str], year: int, keyhint: str) -> str:
    # build citation key from title/authors/year, fall back to keyhint if needed
    return make_bibkey(title, authors, year, fallback=re.sub(r"\W+", "", keyhint) or "entry")


def _score_candidate_generic(
        target_title: str,
        target_author: Optional[str],
        target_year: Optional[int],
        cand_title: str,
        cand_authors: Any,
        cand_year: Optional[int],
        title_sim: Callable[[str, str], float],
        author_match: Callable[[str, Any], bool],
) -> float:
    # score how well candidate matches target - title similarity is key, then author/year
    s = 0.0
    s += SIM_TITLE_WEIGHT * title_sim(target_title, cand_title)
    if target_author and author_match(target_author, cand_authors):
        s += SIM_AUTHOR_BONUS

    # year bonus only if both are valid numbers
    ty = extract_year_from_any(target_year) if target_year else None
    cy = extract_year_from_any(cand_year) if cand_year else None
    if ty is not None and cy is not None:
        s += SIM_YEAR_BONUS * (1.0 if abs(ty - cy) <= SIM_YEAR_MATCH_WINDOW else 0.0)
    return s


def _best_item_by_score(
        items: List[Any],
        score_fn: Callable[[Any], float],
        threshold: float = SIM_BEST_ITEM_THRESHOLD,
) -> Optional[Any]:
    """
    Pick the highest-scoring item that meets the threshold.
    """
    best = None
    best_s = 0.0
    for it in items:
        s = score_fn(it)
        if s > best_s:
            best, best_s = it, s
    return best if best_s >= threshold else None


def fetch_author_publications(api_key: str, author_id: str, num: int = 100, start: int = 0, sort_by: str = "pubdate") -> Dict[str, Any]:
    """
    Fetch publications for an author from Google Scholar via SerpAPI.

    By default, sorts by publication date (newest first) to ensure recent publications
    are included in the results. This is important because the contribution window
    typically focuses on recent years, and Google Scholar's default sort (by citations)
    would return older, highly-cited papers first.
    """
    from .http_utils import handle_api_errors

    @handle_api_errors(default_return={})
    def _fetch():
        params = {
            "engine": "google_scholar_author",
            "author_id": author_id,
            "api_key": api_key,
            "num": num,
            "start": start,
            "sort": sort_by,
        }
        url = build_url(SERPAPI_BASE, params)
        return http_get_json(url)

    return _fetch()


def extract_cite_link(article: Dict[str, Any]) -> Optional[str]:
    """
    Find the URL for Scholar's cite dialog by checking multiple nested locations.
    """
    inline = article.get("inline_links") or {}
    cite_link = inline.get("serpapi_cite_link") or article.get("serpapi_cite_link")
    if cite_link:
        return cite_link
    # try a few additional common shapes
    for key in ("citations", "links", "resources"):
        cont = inline.get(key)
        if isinstance(cont, list):
            for c in cont:
                if isinstance(c, dict):
                    cand = c.get("serpapi_cite_link") or c.get("serpapi_url") or c.get("serpapi_link")
                    if isinstance(cand, str) and "google_scholar_cite" in cand:
                        return cand
    # last resort: regex scan
    try:
        txt = json.dumps(article)
        m = re.search(r"https?://[^\"']+google_scholar_cite[^\"']+", txt)
        if m:
            return m.group(0)
    except PARSE_ERRORS:
        pass
    # only for cite dialog, not item URLs
    return None


def extract_authors_from_article(art: Dict[str, Any]) -> Optional[List[str]]:
    """
    Extract author names from a Scholar article. When the list is truncated with
    ellipses or contains an 'et al.' token, return the partial list (excluding
    the truncation markers) instead of None so downstream code can still build a
    reasonable baseline entry.
    """
    from .text_utils import extract_author_names

    authors = art.get("authors")
    if not authors:
        return None

    # Use centralized extraction logic
    names = extract_author_names(authors, name_key="name")

    # Filter out Scholar-specific truncation markers
    def _is_truncation_marker(name_str: str) -> bool:
        low = name_str.strip().lower()
        return low in ("...", "…") or "et al" in low

    # Keep only valid names (filter out truncation markers)
    filtered_names = [n for n in names if n and not _is_truncation_marker(n)]

    return filtered_names if filtered_names else None


def get_current_year() -> int:
    """
    Get the current year.
    """
    return datetime.now(timezone.utc).year


def get_article_year(art: Dict[str, Any]) -> int:
    """
    Extract the publication year from an article by checking multiple fields, returning 0 if not found.
    """
    # Try year fields first
    y = art.get("year") or art.get("publication_year")
    year = extract_year_from_any(y, fallback=None)
    if year is not None:
        return year

    # Try publication info fields
    pub = art.get("publication") or art.get("snippet") or art.get("publication_info")
    year = extract_year_from_any(pub, fallback=None)
    if year is not None:
        return year

    return 0


def sort_articles_by_year_current_first(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort articles with current year first, then descending by year.

    Uses a stable, deterministic sort key: (year_group, -year, normalized_title, first_author)
    This ensures the same articles always appear in the same order regardless of
    API response ordering, making the process reproducible across runs.
    """
    cur = get_current_year()

    def key_func(a: Dict[str, Any]):
        y = get_article_year(a)
        group = 0 if y == cur else 1
        # Add stable secondary keys for deterministic ordering
        title = normalize_title(a.get("title") or "")
        # Get first author for tertiary sort
        authors = a.get("authors") or []
        if isinstance(authors, list) and authors:
            first_author = (authors[0].get("name") if isinstance(authors[0], dict) else str(authors[0])).lower()
        elif isinstance(authors, str):
            first_author = authors.split(",")[0].split(" and ")[0].strip().lower()
        else:
            first_author = ""
        return (group, -y, title, first_author)

    return sorted(articles, key=key_func)


def strip_html_tags(s: str) -> str:
    """
    Remove HTML tags, convert <br> to newlines, and collapse multiple
    whitespace characters into single spaces.
    """
    s2 = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.IGNORECASE)
    s2 = re.sub(r"<[^>]+>", " ", s2)
    s2 = re.sub(r"\s+", " ", s2)
    return s2.strip()


def scholar_view_citation_url(author_id: str, result_id: str) -> str:
    """
    Build the Google Scholar view_citation URL for a given author and result so
    the citation page can be fetched reliably.
    """
    base = "https://scholar.google.com/citations"
    if ":" not in result_id:
        citation_for_view = f"{author_id}:{result_id}"
    else:
        citation_for_view = result_id
    params = {
        "view_op": "view_citation",
        "hl": "en",
        "user": author_id,
        "sortby": "pubdate",
        "citation_for_view": citation_for_view,
    }
    return build_url(base, params)


def output_cached_page_path(out_dir: str, author_id: str, result_id: str, author_name: Optional[str] = None) -> str:
    """
    Compute the cache file path for a Scholar citation page.
    Creates the internal _pages folder and produces a safe filename.
    """
    author_dirname = format_author_dirname(author_name, author_id)
    pages_dir = os.path.join(out_dir, author_dirname, "_pages")
    os.makedirs(pages_dir, exist_ok=True)
    keep = "-_.() "
    s2 = "".join(ch if ch.isalnum() or ch in keep else "_" for ch in result_id)
    s2 = s2[:200] if len(s2) > 200 else s2
    return os.path.join(pages_dir, f"{s2}.html")


def fetch_scholar_citation_via_serpapi(
    api_key: str, author_id: str, citation_id: str
) -> Optional[Dict[str, str]]:
    """
    Fetch individual article citation details from Google Scholar using SerpAPI,
    bypassing direct HTTP requests that get blocked.
    """
    if not api_key or not author_id or not citation_id:
        return None

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

        # Convert SerpAPI response to the format expected by build_bibtex_from_scholar_fields
        fields = {}

        # Extract title
        if citation.get("title"):
            fields["title"] = citation.get("title")

        # Extract authors - SerpAPI returns as list
        if citation.get("authors"):
            authors = citation.get("authors")
            if isinstance(authors, list):
                fields["authors"] = ", ".join(authors)
            else:
                fields["authors"] = str(authors)

        # Extract publication date/year
        if citation.get("publication_date"):
            fields["publication date"] = citation.get("publication_date")

        # Extract venue information
        if citation.get("journal"):
            fields["journal"] = citation.get("journal")
        if citation.get("conference"):
            fields["conference"] = citation.get("conference")
        if citation.get("book"):
            fields["book"] = citation.get("book")

        # Extract additional metadata
        if citation.get("volume"):
            fields["volume"] = citation.get("volume")
        if citation.get("issue"):
            fields["issue"] = citation.get("issue")
        if citation.get("pages"):
            fields["pages"] = citation.get("pages")
        if citation.get("publisher"):
            fields["publisher"] = citation.get("publisher")
        if citation.get("description"):
            fields["description"] = citation.get("description")

        return fields if fields else None

    except ALL_API_ERRORS:
        return None


def fetch_scholar_view_html(out_dir: str, author_id: str, result_id: str, author_name: Optional[str] = None) -> \
        Optional[str]:
    """
    DEPRECATED - Use fetch_scholar_citation_via_serpapi() instead. Direct HTTP requests to Google Scholar get blocked.
    """
    cache_path = output_cached_page_path(out_dir, author_id, result_id, author_name=author_name)

    # Try to read from cache only
    cached_html = safe_read_file(cache_path)
    if cached_html is not None:
        return cached_html

    # Direct fetching from Google Scholar is disabled as it gets blocked
    # Use fetch_scholar_citation_via_serpapi() instead
    return None


def parse_scholar_view_fields(html: str) -> Dict[str, str]:
    """
    Parse a Scholar citation HTML page and extract the title and key label–value fields into a simple dictionary.
    """
    fields: Dict[str, str] = {}
    if not html:
        return fields
    m = re.search(r"<div[^>]*id=\"gsc_oci_title\"[^>]*>(.*?)</div>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        m = re.search(r"<div[^>]*class=\"gsc_oci_title\"[^>]*>(.*?)</div>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title_html = m.group(1)
        fields["title"] = strip_html_tags(title_html)
    for m in re.finditer(r"<div[^>]*class=\"gsc_oci_field\"[^>]*>(.*?)</div>\s*"
                         r"<div[^>]*class=\"gsc_oci_value\"[^>]*>(.*?)</div>", html, re.IGNORECASE | re.DOTALL):
        label = strip_html_tags(m.group(1)).lower()
        val = strip_html_tags(m.group(2))
        fields[label] = val
    return fields


# Note: get_container_field() from bibtex_build.py should be used instead


def build_bibtex_from_scholar_fields(fields: Dict[str, str], keyhint: str) -> Optional[str]:
    """
    Turn structured fields parsed from a Scholar citation page into a BibTeX
    entry, inferring the entry type and filling common metadata.
    """
    from .bibtex_build import build_bibtex_entry, determine_entry_type
    from .text_utils import extract_year_from_any, extract_authors_from_any, safe_get_field

    title = safe_get_field(fields, "title") or safe_get_field(fields, "paper title")
    if not title:
        return None

    # extract authors
    authors_val = fields.get("authors") or ""
    authors = extract_authors_from_any(authors_val)

    # extract year
    pub_date = fields.get("publication date") or fields.get("year") or ""
    year = extract_year_from_any(pub_date, fallback=0) or 0

    # extract venue
    venue = safe_get_field(fields, "journal") or safe_get_field(fields, "conference") or safe_get_field(fields, "book")

    # determine entry type
    entry_type = determine_entry_type(
        fields,
        venue_hints={"journal": "article", "conference": "inproceedings"}
    )

    # extract additional fields
    pages = safe_get_field(fields, "pages")
    publisher = safe_get_field(fields, "publisher")
    volume = safe_get_field(fields, "volume")
    number = safe_get_field(fields, "issue") or safe_get_field(fields, "number")
    doi_candidate = safe_get_field(fields, "doi") or safe_get_field(fields, "url")
    doi = find_doi_in_text(doi_candidate) if doi_candidate else None
    url = safe_get_field(fields, "url")

    # build entry
    return build_bibtex_entry(
        entry_type=entry_type,
        title=title,
        authors=authors,
        year=year,
        keyhint=keyhint,
        venue=venue or None,
        doi=doi,
        url=url,
        extra_fields={
            "volume": volume,
            "number": number,
            "pages": pages,
            "publisher": publisher
        }
    )


essential_result_keys = ("organic_results", "results")


def fetch_bibtex_from_cite(api_key: str, cite_url: str) -> str:
    """
    Retrieve the BibTeX text for a publication using Google Scholar’s cite
    dialog through SerpAPI, following the BibTeX download link when found.
    """
    parsed = urllib.parse.urlparse(cite_url)
    q = urllib.parse.parse_qs(parsed.query)
    q["api_key"] = [api_key]
    new_query = urllib.parse.urlencode({k: v[0] if isinstance(v, list) else v for k, v in q.items()})
    cite_with_key = urllib.parse.urlunparse(parsed._replace(query=new_query))

    # Get the cite dialog JSON (retries are handled by session-level retry strategy)
    json_headers = DEFAULT_JSON_HEADERS.copy()
    raw = http_fetch_bytes(cite_with_key, json_headers, timeout=30.0)
    try:
        cite_json = json.loads(raw.decode("utf-8"))
    except DECODE_ERRORS:
        # Sometimes the encoding is weird, just replace bad characters
        cite_json = json.loads(raw.decode("utf-8", errors="replace"))

    def find_bibtex_link(obj: Dict[str, Any]) -> Optional[str]:
        # Look through the response for a BibTeX download link
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
                        return link
        return None

    bib_link = find_bibtex_link(cite_json)
    if not bib_link:
        available = ",".join(k for k in cite_json.keys())
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

    # Now actually fetch the BibTeX file (pretending to be a browser helps with rate limits)
    text_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/119.0 Safari/537.36"
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
def search_scholar_for_cite_link(api_key: str, title: str, author_name: Optional[str] = None) -> Optional[str]:
    """
    Query Google Scholar for a paper by title, optionally filtered by author,
    and return the best matching cite dialog link when available.
    """
    query_parts = []
    if title:
        query_parts.append(f'"{title}"')
    q = " ".join(query_parts) if query_parts else title

    params = {
        "engine": "google_scholar",
        "q": q,
        "api_key": api_key,
        "num": 10,
    }
    if author_name:
        # Use Scholar's author filter to narrow results
        params["as_sauthors"] = author_name
    url = build_url(SERPAPI_BASE, params)
    data = http_get_json(url)

    results = None
    for key in essential_result_keys:
        if key in data:
            results = data.get(key) or []
            break
    if results is None:
        results = []
    if not results:
        return None

    target_norm = normalize_title(title)

    def candidate_authors(item: Dict[str, Any]) -> Any:
        if isinstance(item.get("authors"), list) or isinstance(item.get("authors"), str):
            return item.get("authors")
        pubinfo = item.get("publication_info") or {}
        if isinstance(pubinfo, dict):
            if pubinfo.get("authors"):
                return pubinfo.get("authors")
            if pubinfo.get("summary"):
                return pubinfo.get("summary")
        return item.get("snippet")

    # Look for exact title matches first
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
            return link

    # If no exact match, try to find something close enough
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
                    return link
        else:
            link = (best.get("inline_links") or {}).get("serpapi_cite_link") or best.get("serpapi_cite_link")
            if link:
                return link

    return None


def s2_search_paper(title: str, author_name: Optional[str], api_key: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Search Semantic Scholar for a paper that matches the given title and
    optional author, preferring exact matches and otherwise using a scoring
    function.
    """
    if not api_key or not title:
        return None

    # Build query with author if provided
    query_parts = [f'"{title}"']
    if author_name:
        query_parts.append(author_name)

    from .api_generics import search_api_generic
    from .api_configs import S2_SEARCH_CONFIG

    # Create a copy of config with the formatted query
    config = S2_SEARCH_CONFIG
    # Override additional_params to include the formatted query
    config_copy = copy.copy(config)
    config_copy.additional_params = {
        **config.additional_params,
        config.query_param_name: " ".join(query_parts)
    }

    return search_api_generic(title, author_name, config_copy, api_key=api_key)


def build_bibtex_from_s2(paper: Dict[str, Any], keyhint: str) -> Optional[str]:
    """
    Convert a Semantic Scholar paper record into a BibTeX entry, choosing an
    entry type from the publication metadata and copying identifiers when possible.
    """
    from .api_generics import build_bibtex_from_response
    from .api_configs import S2_FIELD_MAPPING

    return build_bibtex_from_response(paper, keyhint, S2_FIELD_MAPPING)


def crossref_search(title: str, author_name: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Look up a publication in Crossref by title and optional author and return
    the best matching record based on normalized title and scoring.
    """
    if not title:
        return None

    from .api_generics import search_api_generic
    from .api_configs import CROSSREF_SEARCH_CONFIG

    # Adjust config based on whether we have an author
    config = copy.copy(CROSSREF_SEARCH_CONFIG)
    additional_params = dict(config.additional_params)

    if author_name:
        additional_params["query.title"] = title
        additional_params["query.author"] = author_name
    else:
        additional_params["query.bibliographic"] = title

    mailto = os.getenv("CROSSREF_MAILTO")
    if mailto:
        additional_params["mailto"] = mailto

    config.additional_params = additional_params

    return search_api_generic(title, author_name, config)


def build_bibtex_from_crossref(item: Dict[str, Any], keyhint: str) -> Optional[str]:
    """
    Build a BibTeX entry from a Crossref record, mapping Crossref's fields into
    a standard BibTeX structure with venue and basic publication details.
    """
    from .api_generics import build_bibtex_from_response
    from .api_configs import CROSSREF_FIELD_MAPPING

    return build_bibtex_from_response(item, keyhint, CROSSREF_FIELD_MAPPING)


@handle_api_errors(default_return=None)
def fetch_csl_via_doi(doi: str, timeout: float = 20.0) -> Optional[Dict[str, Any]]:
    """
    Resolve a DOI using content negotiation and return the associated CSL-JSON
    metadata when the resolver responds successfully.
    """
    doi_norm = _norm_doi(doi)
    if not doi_norm:
        return None
    url = f"https://doi.org/{doi_norm}"
    headers = DEFAULT_JSON_HEADERS.copy()
    headers["Accept"] = "application/vnd.citationstyles.csl+json"
    raw = http_fetch_bytes(url, headers, timeout)
    return json.loads(raw.decode("utf-8"))


def fetch_bibtex_via_doi(doi: str, timeout: float = 20.0) -> Optional[str]:
    """
    Resolve a DOI and ask the resolver for BibTeX output, returning the raw BibTeX text or None when the lookup fails.
    """
    doi_norm = _norm_doi(doi)
    if not doi_norm:
        return None
    url = f"https://doi.org/{doi_norm}"
    headers = DEFAULT_JSON_HEADERS.copy()
    headers["Accept"] = "application/x-bibtex"
    try:
        raw = http_fetch_bytes(url, headers, timeout)
        return raw.decode("utf-8", errors="replace")
    except NETWORK_ERRORS:
        return None


def bibtex_from_csl(csl: Dict[str, Any], keyhint: str) -> str:
    """
    Translate a CSL-JSON citation description into a BibTeX entry, reusing
    common publication fields and identifiers where available.
    """
    from .bibtex_build import build_bibtex_entry, determine_entry_type
    from .text_utils import extract_year_from_any, extract_authors_from_any, safe_get_field

    # CSL-JSON often separates main title and subtitle into different fields
    # Combine them to preserve the full title (e.g., "Raptor: GPU-based Analytics")
    title = safe_get_field(csl, "title") or ""
    subtitle_raw = csl.get("subtitle")
    # Subtitle can be a string or a list of strings
    if isinstance(subtitle_raw, list):
        subtitle = subtitle_raw[0] if subtitle_raw else ""
    else:
        subtitle = subtitle_raw or ""
    if subtitle:
        # Combine title and subtitle with colon separator
        title = f"{title}: {subtitle}" if title else subtitle

    # Get authors from CSL's format
    authors = extract_authors_from_any(csl, field_names=["author"])

    # Extract year from CSL date-parts
    year = extract_year_from_any(csl, fallback=0) or 0

    # Extract venue container
    container = safe_get_field(csl, "container-title")

    # Determine entry type
    entry_type = determine_entry_type(csl)

    # Extract identifiers 
    doi = safe_get_field(csl, "DOI")
    url = safe_get_field(csl, "URL")

    # Extract publication details
    volume = safe_get_field(csl, "volume")
    number = safe_get_field(csl, "issue")
    pages = safe_get_field(csl, "page")
    publisher = safe_get_field(csl, "publisher")

    # Filter out arXiv as publisher (arXiv is a preprint repository, not a publisher)
    if publisher and publisher.strip().lower() == "arxiv":
        publisher = None

    # Build entry
    return build_bibtex_entry(
        entry_type=entry_type,
        title=title,
        authors=authors,
        year=year,
        keyhint=keyhint,
        venue=container or None,
        doi=doi or None,
        url=url or None,
        extra_fields={
            "volume": volume or None,
            "number": number or None,
            "pages": pages or None,
            "publisher": publisher or None
        }
    )


def arxiv_search(
        title: str,
        author_name: Optional[str],
        year_hint: Optional[int],
        max_results: int = 10,
) -> List[Dict[str, Any]]:
    """
    Search arXiv for papers that match the given title and optional author and
    return a list of candidate records sorted by match quality.
    """
    if not title:
        return []
    q_parts = [f'ti:"{title}"']
    if author_name:
        q_parts.append(f'au:"{author_name}"')
    search_query = "+AND+".join(q_parts)
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    url = build_url(ARXIV_BASE, params)
    try:
        xml = http_get_text(url)
    except NETWORK_ERRORS:
        return []
    try:
        root = ElementTree.fromstring(xml)
    except XML_PARSE_ERRORS:
        return []

    def _ns_uri(tag: str) -> str:
        if tag.startswith("{"):
            return tag[1:].split("}")[0]
        return ""

    atom_ns = _ns_uri(root.tag)

    def qn(ns: str, local: str) -> str:
        return f"{{{ns}}}{local}" if ns else local

    def find_child(el, local: str):
        for child in el:
            if child.tag.split("}")[-1] == local:
                return child
        return None

    entries = []
    for entry_el in root.findall(qn(atom_ns, "entry")):
        title_el = find_child(entry_el, "title")
        title_val = (title_el.text or "").strip() if title_el is not None else ""
        authors = []
        for author_el in entry_el.findall(qn(atom_ns, "author")):
            name_el = find_child(author_el, "name")
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())
        pub_el = find_child(entry_el, "published")
        year = 0
        if pub_el is not None and pub_el.text:
            m = re.match(r"(\d{4})-", pub_el.text.strip())
            if m:
                year = int(m.group(1))
        id_el = find_child(entry_el, "id")
        entry_id = (id_el.text or "") if id_el is not None else ""
        link_abs = ""
        for link_el in entry_el.findall(qn(atom_ns, "link")):
            rel = link_el.attrib.get("rel", "")
            href = link_el.attrib.get("href", "")
            if rel == "alternate":
                link_abs = href
        doi = ""
        doi_el = None
        for ch in entry_el.iter():
            if ch.tag.split("}")[-1] == "doi":
                doi_el = ch
                break
        if doi_el is not None and doi_el.text:
            doi = find_doi_in_text(doi_el.text.strip()) or ""
        pc = ""
        pcel = None
        for ch in entry_el.iter():
            if ch.tag.split("}")[-1] == "primary_category":
                pcel = ch
                break
        if pcel is not None:
            pc = pcel.attrib.get("term", "") or ""
        arxiv_id = find_arxiv_in_text(link_abs or entry_id) or ""
        entries.append({
            "title": title_val,
            "authors": authors,
            "year": year,
            "abs_url": link_abs,
            "doi": doi,
            "primary_class": pc,
            "arxiv_id": arxiv_id,
        })
    if not entries:
        return []

    # Use scoring factory with authors_overlap for arXiv
    from .bibtex_build import create_scoring_function
    score_fn = create_scoring_function(
        title=title,
        author_name=author_name,
        year_hint=year_hint,
        title_getter=lambda ent: ent.get("title", ""),
        authors_getter=lambda ent: ent.get("authors", []),
        year_getter=lambda ent: ent.get("year"),
        author_match_fn=lambda author_name_value, author_list: authors_overlap(author_name_value, author_list)
    )

    entries.sort(key=score_fn, reverse=True)
    return entries


def build_bibtex_from_arxiv(entry: Dict[str, Any], keyhint: str) -> Optional[str]:
    """
    Turn a parsed arXiv search result into a BibTeX entry, including the arXiv
    identifier and basic publication information when present.
    """
    from .api_generics import build_bibtex_from_response
    from .api_configs import ARXIV_FIELD_MAPPING

    return build_bibtex_from_response(entry, keyhint, ARXIV_FIELD_MAPPING)


def openreview_login(creds: Optional[tuple]) -> Optional[Dict[str, str]]:
    """
    Log into OpenReview using the given credentials and return a header
    dictionary with a session cookie that can be reused for later requests.
    """
    if not creds:
        return None
    login, password = creds[0], creds[1]
    try:
        url = f"{OPENREVIEW_BASE}/login"
        payload = json.dumps({"id": login, "password": password}).encode("utf-8")
        headers = DEFAULT_JSON_HEADERS.copy()
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                set_cookie = resp.headers.get("Set-Cookie") if hasattr(resp, "headers") else None
                if set_cookie:
                    headers_with_cookie = DEFAULT_JSON_HEADERS.copy()
                    headers_with_cookie["Cookie"] = set_cookie
                    return headers_with_cookie
        except NETWORK_ERRORS:
            return None
    except PARSE_ERRORS:
        return None
    return None


def openreview_search_paper(title: str, author_name: Optional[str], creds: Optional[tuple]) -> Optional[Dict[str, Any]]:
    """
    Query OpenReview for notes whose titles resemble the requested paper and
    return the best candidate using simple matching and scoring.
    """
    if not title:
        return None
    headers = openreview_login(creds) or DEFAULT_JSON_HEADERS.copy()
    candidates: List[Dict[str, Any]] = []

    def _extend_with_notes(req_url: str) -> None:
        raw = http_fetch_bytes(req_url, headers, timeout=30.0)
        data = json.loads(raw.decode("utf-8"))
        notes = data.get("notes") or data.get("data") or []
        if isinstance(notes, list):
            candidates.extend(notes)

    # Try search endpoint 1: /notes with term query
    try:
        params = {"term": title, "details": "metadata"}
        url = build_url(f"{OPENREVIEW_BASE}/notes", params)
        _extend_with_notes(url)
    except ALL_API_ERRORS:
        pass
    # Try endpoint 2: /notes/search?q=
    if not candidates:
        try:
            url = build_url(f"{OPENREVIEW_BASE}/notes/search", {"q": title, "limit": 20})
            _extend_with_notes(url)
        except ALL_API_ERRORS:
            pass
    if not candidates:
        return None

    def note_title(note: Dict[str, Any]) -> str:
        c = note.get("content") or {}
        return (c.get("title") or note.get("title") or "").strip()

    def note_authors(note: Dict[str, Any]) -> Any:
        c = note.get("content") or {}
        return c.get("authors") or c.get("authorids") or note.get("authors")

    target_norm = normalize_title(title)
    exact: List[Dict[str, Any]] = []
    for cand in candidates:
        if normalize_title(note_title(cand)) == target_norm:
            if not author_name or author_name_matches(author_name, note_authors(cand)):
                exact.append(cand)
    if exact:
        return exact[0]

    # Helper to extract year from Unix timestamp (milliseconds)
    def note_year(note_obj: Dict[str, Any]) -> Optional[int]:
        try:
            ms = note_obj.get("cdate") or note_obj.get("tcdate")
            if isinstance(ms, (int, float)):
                return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).year
        except (*NUMERIC_ERRORS, OSError):
            return None
        return None

    # Use scoring factory
    from .bibtex_build import create_scoring_function
    score_fn = create_scoring_function(
        title=title,
        author_name=author_name,
        year_hint=None,
        title_getter=note_title,
        authors_getter=note_authors,
        year_getter=note_year
    )

    return _best_item_by_score(candidates, score_fn, threshold=SIM_EXACT_PICK_THRESHOLD)


def build_bibtex_from_openreview(note: Dict[str, Any], keyhint: str) -> Optional[str]:
    """
    Build a compact BibTeX inproceedings entry from an OpenReview note, using
    its title, authors, venue, year, and links when available.
    """
    from .api_generics import build_bibtex_from_response
    from .api_configs import OPENREVIEW_FIELD_MAPPING

    return build_bibtex_from_response(note, keyhint, OPENREVIEW_FIELD_MAPPING)


# ---------------- DBLP utilities ----------------

def dblp_extract_pid(val: Optional[str]) -> Optional[str]:
    """
    Extract a DBLP person identifier from a hint value, handling plain IDs,
    prefixed forms, and full URLs that contain a /pid/ segment.
    """
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None
    m = re.search(r"/pid/([^/#?]+)", s)
    if m:
        return m.group(1)
    m = re.match(r"^(pid:)?([0-9a-zA-Z/._-]+)$", s)
    if m:
        return m.group(2)
    return None


@handle_api_errors(default_return=None)
def dblp_find_author_pid(name: str) -> Optional[str]:
    """
    Look up a DBLP person identifier for an author name, preferring exact name
    matches and falling back to the first hit when needed.
    """
    if not name:
        return None
    params = {"q": name, "format": "json"}
    url = build_url(DBLP_BASE, params)
    data = http_get_json(url)
    res = (data.get("result") or {}).get("hits") or {}
    hits = res.get("hit") or []
    # normalize both sides for match
    name_norm = (name or "").strip().lower()
    exact_pid = None
    first_pid = None
    for h in hits:
        info = h.get("info") or {}
        pid = (info.get("pid") or "").strip()
        author_name = (info.get("author") or info.get("name") or "").strip()
        if pid and not first_pid:
            first_pid = pid
        if author_name and author_name.strip().lower() == name_norm:
            exact_pid = pid
            break
    return exact_pid or first_pid


def _xml_text(el: Optional[ElementTree.Element]) -> str:
    """
    Read and strip the text content of an XML element, returning an empty
    string when the element or its text is missing.
    """
    return (el.text or "").strip() if el is not None else ""


def _sanitize_dblp_author(name: str) -> str:
    """
    Clean a DBLP author name by removing trailing numeric disambiguators,
    keeping only the human-readable part of the name.
    """
    if not name:
        return name
    s = name.strip()
    # Remove parenthesized suffix like "(0001)" at end
    s = re.sub(r"\s*\((0\d{3})\)\s*$", "", s)
    # Remove plain suffix like " 0001" at end
    s = re.sub(r"\s+(0\d{3})\s*$", "", s)
    return s


def dblp_fetch_publications(pid: str) -> List[Dict[str, Any]]:
    """
    Download a DBLP author XML record for the given person identifier and
    convert each listed entry into a simplified publication dictionary.
    """
    if not pid:
        return []
    url = f"{DBLP_PERSON_BASE}/{pid}.xml"
    try:
        xml = http_get_text(url, timeout=HTTP_TIMEOUT_SHORT)
    except NETWORK_ERRORS:
        return []
    try:
        # Parse XML with default settings; ElementTree does not expand external entities by default
        # For production-grade hardening against XXE, consider defusedxml, but we keep stdlib only here
        root = ElementTree.fromstring(xml)
    except XML_PARSE_ERRORS:
        return []

    articles: List[Dict[str, Any]] = []
    for r in root.findall("r"):
        child = None
        # pick first child element inside <r> (article, inproceedings, etc.)
        for ch in r:
            if isinstance(ch.tag, str):
                child = ch
                break
        if child is None:
            continue
        title_el = child.find("title")
        # title may contain nested tags (e.g., <sub>), join texts
        title_val = "".join(title_el.itertext()) if title_el is not None else ""
        title = trim_title_default((title_val or ""))
        if not title:
            continue
        year = 0
        year_el = child.find("year")
        if year_el is not None and year_el.text and re.match(r"^(19|20)\d{2}$", year_el.text.strip()):
            try:
                year = int(year_el.text.strip())
            except PARSE_ERRORS:
                year = 0
        authors = []
        # DBLP uses <author> for regular publications and <editor> for proceedings/books
        for ael in child.findall("author"):
            nm = _xml_text(ael)
            if nm:
                nm = _sanitize_dblp_author(nm)
                if nm:
                    authors.append(nm)
        # If no authors found, check for editors (proceedings/books)
        if not authors:
            for eel in child.findall("editor"):
                nm = _xml_text(eel)
                if nm:
                    nm = _sanitize_dblp_author(nm)
                    if nm:
                        authors.append(nm)
        # URLs: ee (electronic edition), url (DBLP page)
        ee = _xml_text(child.find("ee"))
        dburl = _xml_text(child.find("url"))
        doi = _norm_doi(find_doi_in_text(ee) or find_doi_in_text(dburl))
        abs_or_url = ee or dburl
        venue = _xml_text(child.find("journal")) or _xml_text(child.find("booktitle"))
        # Build synthetic article dict
        art: Dict[str, Any] = {
            "title": title,
            "authors": authors,
            "year": year,
            "publication": venue,
            "link": abs_or_url,
            "snippet": ", ".join([v for v in [venue, str(year) if year else "", doi or ""] if v]),
            # Mark as DBLP-derived to avoid misinterpretation elsewhere
            "source": "dblp",
        }
        # provide a stable synthetic id based on doi or title
        if doi:
            art["result_id"] = f"dblp:doi:{doi}"
        else:
            _san = re.sub(r"\W+", "_", normalize_title(title))
            art["result_id"] = f"dblp:{_san[:64]}"
        articles.append(art)
    return articles


def build_synthetic_article_from_dblp(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Provide a hook for future DBLP-specific transformations while currently
    returning a shallow copy of the original publication dictionary.
    """
    return dict(item)


def _deduplicate_publication_list(pubs: List[Dict[str, Any]], _target_author: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Remove internal duplicates from a single publication list.

    For each publication, check if it's similar to any previously added publication.
    Only keep publications that don't match any existing entries above the
    deduplication threshold.

    Publications are sorted first by (year desc, normalized title, first author) for
    deterministic ordering, ensuring the same duplicates are removed regardless of
    input ordering from APIs.
    """
    if not pubs:
        return []

    # Sort publications for deterministic deduplication order
    # This ensures the same publications are kept/removed regardless of API response order
    def sort_key(pub: Dict[str, Any]) -> tuple:
        year = extract_year_from_any(pub.get("year"), fallback=0) or 0
        title = normalize_title(pub.get("title") or "")
        authors = pub.get("authors") or []
        if isinstance(authors, list) and authors:
            first_author = (authors[0].get("name") if isinstance(authors[0], dict) else str(authors[0])).lower()
        elif isinstance(authors, str):
            first_author = authors.split(",")[0].split(" and ")[0].strip().lower()
        else:
            first_author = ""
        return (-year, title, first_author)

    sorted_pubs = sorted(pubs, key=sort_key)
    deduplicated: List[Dict[str, Any]] = []

    for pub in sorted_pubs:
        # Trim title for consistent matching
        p_title_raw = pub.get("title") or ""
        p_title = trim_title_default(p_title_raw)
        p_year = pub.get("year") or None
        p_authors = pub.get("authors") or []

        # Check against all already-added publications
        is_duplicate = False
        for existing in deduplicated:
            e_title = existing.get("title") or ""
            e_year = existing.get("year") or None
            e_authors = existing.get("authors") or []

            # Quick title similarity check
            tsim = title_similarity(p_title, e_title) if p_title and e_title else 0.0
            if tsim < SIM_TITLE_SIM_MIN:
                continue

            # Full scoring - for internal dedup, compare author lists directly
            score = 0.0
            score += SIM_TITLE_WEIGHT * tsim

            # Check if author lists overlap (for internal dedup, we don't need target_author)
            if authors_overlap(e_authors, p_authors):
                score += SIM_AUTHOR_BONUS

            # Year bonus
            e_year_int = extract_year_from_any(e_year) if e_year else None
            p_year_int = extract_year_from_any(p_year) if p_year else None
            if e_year_int is not None and p_year_int is not None:
                score += SIM_YEAR_BONUS * (1.0 if abs(e_year_int - p_year_int) <= SIM_YEAR_MATCH_WINDOW else 0.0)

            if score >= SIM_MERGE_DUPLICATE_THRESHOLD:
                is_duplicate = True
                break

        if not is_duplicate:
            # Add to deduplicated list with trimmed title
            pub_copy = dict(pub)
            if p_title and p_title != p_title_raw:
                pub_copy["title"] = p_title
            deduplicated.append(pub_copy)

    return deduplicated


def merge_publication_lists(primary: List[Dict[str, Any]], secondary: List[Dict[str, Any]],
                            target_author: Optional[str]) -> List[Dict[str, Any]]:
    """
    Merge two publication lists into one unified list with complete deduplication.

    First deduplicates each source internally, then combines them by keeping all
    primary entries and adding secondary entries that are not duplicates.

    This ensures: primary ∪ secondary with no duplicates (within or across sources).
    """
    # Deduplicate each source internally first
    primary_deduped = _deduplicate_publication_list(primary, target_author) if primary else []
    secondary_deduped = _deduplicate_publication_list(secondary, target_author) if secondary else []

    # Start with all deduplicated primary entries
    merged: List[Dict[str, Any]] = list(primary_deduped)
    if not secondary_deduped:
        return merged

    # Precompute normalized titles for primary (already deduplicated)
    prim_norm = [(normalize_title(p.get("title") or ""), p) for p in merged]

    # Add deduplicated secondary entries that don't match primary
    for sec in secondary_deduped:
        s_title_raw = sec.get("title") or ""
        s_title = trim_title_default(s_title_raw)
        s_year = sec.get("year") or None
        s_authors = sec.get("authors") or []
        s_norm = normalize_title(s_title)
        best = 0.0
        for tnorm, p in prim_norm:
            tsim = title_similarity(s_title, p.get("title") or "") if s_title else 0.0
            if tsim < SIM_TITLE_SIM_MIN:
                continue
            ps_year = p.get("year") or None
            sc = _score_candidate_generic(
                target_title=p.get("title") or "",
                target_author=target_author,
                target_year=ps_year,
                cand_title=s_title,
                cand_authors=s_authors,
                cand_year=s_year,
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
            prim_norm.append((s_norm, sec2))
    return merged


def enhance_scholar_article_with_dblp(
    scholar_art: Dict[str, Any],
    dblp_items: List[Dict[str, Any]],
    target_author: Optional[str] = None
) -> bool:
    """
    Enhance a Scholar article with complete data from DBLP if a match is found,
    modifying the article in-place and returning True if enhancement was performed.
    """
    from .text_utils import is_truncated

    if not dblp_items:
        return False

    scholar_title = scholar_art.get("title", "")
    if not scholar_title:
        return False

    # Find best matching DBLP item
    best_score = 0.0
    best_match = None

    for dblp_item in dblp_items:
        dblp_title = dblp_item.get("title", "")
        if not dblp_title:
            continue

        tsim = title_similarity(scholar_title, dblp_title)
        if tsim < SIM_TITLE_SIM_MIN:
            continue

        score = _score_candidate_generic(
            target_title=scholar_title,
            target_author=target_author,
            target_year=scholar_art.get("year"),
            cand_title=dblp_title,
            cand_authors=dblp_item.get("authors", []),
            cand_year=dblp_item.get("year"),
            title_sim=title_similarity,
            author_match=lambda author_name_value, author_list: authors_overlap(author_name_value, author_list),
        )

        if score > best_score:
            best_score = score
            best_match = dblp_item

    # If good match found, enhance Scholar data with DBLP fields
    if best_score >= SIM_MERGE_DUPLICATE_THRESHOLD and best_match:
        enhanced = False

        # Replace truncated title
        if is_truncated(scholar_title) and best_match.get("title"):
            if not is_truncated(best_match["title"]):
                scholar_art["title"] = best_match["title"]
                enhanced = True

        # Replace truncated authors
        scholar_authors = scholar_art.get("author_info", [])
        if is_truncated(str(scholar_authors)) and best_match.get("authors"):
            dblp_authors = best_match["authors"]
            if not is_truncated(str(dblp_authors)):
                if isinstance(dblp_authors, list):
                    scholar_art["author_info"] = [{"name": a} for a in dblp_authors]
                else:
                    scholar_art["author_info"] = dblp_authors
                enhanced = True

        # Add venue if missing or truncated
        scholar_pub = scholar_art.get("publication_info", "")
        if best_match.get("publication"):
            if not scholar_pub or is_truncated(scholar_pub):
                scholar_art["publication_info"] = best_match["publication"]
                enhanced = True

        # Add year if missing
        if not scholar_art.get("year") and best_match.get("year"):
            scholar_art["year"] = best_match["year"]
            enhanced = True

        if enhanced:
            scholar_art["_dblp_enhanced"] = True
            return True

    return False


def dblp_fetch_for_author(name: str, dblp_hint: Optional[str], min_year: Optional[int]) -> List[Dict[str, Any]]:
    """
    Fetch DBLP publications for an author by resolving their person identifier
    using an optional hint and filtering the results by a minimum year.
    """
    pid = dblp_extract_pid(dblp_hint) if dblp_hint else None
    if not pid:
        pid = dblp_find_author_pid(name)
    items = dblp_fetch_publications(pid) if pid else []
    if min_year:
        items = [it for it in items if (it.get("year") or 0) >= int(min_year)]
    return items


def gemini_generate_short_title(
    full_title: str, api_key: str, max_words: int = None
) -> Optional[str]:
    """
    Call the Gemini API to generate a short CamelCase title for a publication,
    suitable for BibTeX keys and filenames.
    """
    from .config import BIBTEX_KEY_MAX_WORDS
    if max_words is None:
        max_words = BIBTEX_KEY_MAX_WORDS

    if not api_key or not full_title:
        return None

    # Construct the prompt with explicit requirements for optimal results
    # Key aspects:
    # - Specify range (1 to max_words) to allow Gemini to choose optimal length
    # - Request "smart, concise" to prioritize quality over filling word limit
    # - Ask for "most important keywords" to focus on core concepts
    # - Emphasize CamelCase format with NO SPACES for BibTeX key compatibility
    prompt = (
        f"Create a smart, concise CamelCase title (1 to {max_words} words) "
        f"for this publication: \"{full_title}\". "
        f"Extract the most important keywords. "
        f"Skip stop words (a, an, the, for, of, and, to, in, with, from, by, at). "
        f"Use exactly {max_words} words or fewer if shorter captures the essence better. "
        f"IMPORTANT: Write as ONE word in CamelCase format with NO spaces between words "
        f"(e.g., 'AttentionMechanism' not 'Attention Mechanism'). "
        f"Return ONLY the CamelCase title with no quotes, explanation, spaces, or punctuation."
    )

    # build the request URL with API key
    url = f"{GEMINI_BASE}?key={api_key}"

    # prepare the request payload
    payload = {
        "contents": [{
            "parts": [{
                "text": prompt
            }]
        }],
        "generationConfig": {
            "maxOutputTokens": 50,  # Short titles only need ~10-20 tokens
            "temperature": 0.3,  # Lower temperature for more focused, deterministic output
            "topP": 0.8,
            "topK": 20,
        }
    }

    try:
        # make the API request
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
            }
        )

        with urllib.request.urlopen(req, timeout=15.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # extract the generated text from the response
        if "candidates" in data and len(data["candidates"]) > 0:
            candidate = data["candidates"][0]
            if "content" in candidate and "parts" in candidate["content"]:
                parts = candidate["content"]["parts"]
                if len(parts) > 0 and "text" in parts[0]:
                    short_title = parts[0]["text"].strip()
                    # clean up the response - remove quotes, extra whitespace
                    short_title = short_title.strip('"\'').strip()
                    # remove all spaces and newlines to ensure CamelCase without spaces
                    short_title = short_title.replace(" ", "").replace("\n", "").replace("\r", "").replace("\t", "")

                    # validate word count by counting capital letters (CamelCase convention)
                    if short_title:
                        word_count = sum(1 for c in short_title if c.isupper())
                        if word_count > max_words:
                            # Gemini exceeded max_words - fall back to default algorithm
                            logger.warn(
                                f"Gemini returned {word_count} words (expected max {max_words}): '{short_title}'. "
                                f"Falling back to default algorithm.",
                                category=LogCategory.DEBUG,
                                source=LogSource.SYSTEM
                            )
                            return None  # Caller will use fallback algorithm

                    # validate it's reasonable (not empty, not too long)
                    if short_title and len(short_title) <= 100:
                        logger.info(f"Generated title: {short_title}", category=LogCategory.DEBUG, source=LogSource.SYSTEM)
                        return short_title

        logger.warn("Returned no valid candidates in response", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        return None

    except urllib.error.HTTPError as e:
        # Log specific HTTP errors for debugging
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            error_msg = error_body.get("error", {}).get("message", str(e.reason))
            if e.code == 503:
                logger.warn(f"API overloaded (503), falling back to default algorithm", category=LogCategory.ERROR, source=LogSource.SYSTEM)
            elif e.code == 429:
                logger.warn(f"API quota exceeded (429), falling back to default algorithm", category=LogCategory.ERROR, source=LogSource.SYSTEM)
            else:
                logger.warn(f"API error {e.code}: {error_msg}", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        except FIELD_ACCESS_ERRORS:
            # Failed to parse error response, use basic HTTP error info
            logger.warn(f"API HTTP {e.code}: {e.reason}", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        return None
    except Exception as e:
        logger.warn(f"API call failed: {type(e).__name__}: {e}", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        return None


# ============================================================================================
# OpenAlex API Integration
# ============================================================================================

def openalex_search_paper(title: str, author_name: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Search OpenAlex for a publication by title and optional author, returning the
    best matching work based on normalized title and scoring.
    """
    from .api_generics import search_api_generic
    from .api_configs import OPENALEX_SEARCH_CONFIG

    return search_api_generic(title, author_name, OPENALEX_SEARCH_CONFIG)


def build_bibtex_from_openalex(work: Dict[str, Any], keyhint: str) -> Optional[str]:
    """
    Build a BibTeX entry from an OpenAlex work record.
    """
    from .api_generics import build_bibtex_from_response
    from .api_configs import OPENALEX_FIELD_MAPPING

    return build_bibtex_from_response(work, keyhint, OPENALEX_FIELD_MAPPING)


# ============================================================================================
# PubMed API Integration
# ============================================================================================

@handle_api_errors(default_return=None)
def pubmed_search_paper(title: str, author_name: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Search PubMed for a publication by title and optional author using ESearch
    to find PMIDs and ESummary to get article metadata.
    """
    if not title:
        return None

    # Step 1: Search for PMIDs using ESearch
    search_query = f"{title}[Title]"
    if author_name:
        search_query += f" AND {author_name}[Author]"

    search_url = build_url(f"{PUBMED_BASE}/esearch.fcgi", {
        "db": "pubmed",
        "term": search_query,
        "retmax": 10,
        "retmode": "json",
    })

    search_data = http_get_json(search_url, timeout=15.0)

    pmids = (search_data.get("esearchresult") or {}).get("idlist") or []
    if not pmids:
        return None

    # Step 2: Fetch article metadata for the PMIDs using ESummary
    fetch_url = build_url(f"{PUBMED_BASE}/esummary.fcgi", {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "json",
    })

    fetch_data = http_get_json(fetch_url, timeout=15.0)

    result = (fetch_data.get("result") or {})
    articles = []
    for pmid in pmids:
        if pmid in result and isinstance(result[pmid], dict):
            articles.append(result[pmid])

    if not articles:
        return None

    # Try exact title match first
    target_norm = normalize_title(title)
    for article in articles:
        article_title = article.get("title") or ""
        if normalize_title(article_title) == target_norm:
            if not author_name or author_in_text(author_name, str(article.get("authors") or [])):
                return article

    # Helper functions for scoring
    def get_pubmed_title(a: Dict[str, Any]) -> str:
        return a.get("title") or ""

    def get_pubmed_year(a: Dict[str, Any]) -> Optional[int]:
        return extract_year_from_any(a.get("pubdate"), fallback=None)

    def get_pubmed_authors(a: Dict[str, Any]) -> List[str]:
        authors = []
        for auth in a.get("authors") or []:
            name = auth.get("name") or ""
            if name:
                authors.append(name)
        return authors

    # Use scoring factory
    from .bibtex_build import create_scoring_function
    score_fn = create_scoring_function(
        title=title,
        author_name=author_name,
        year_hint=None,
        title_getter=get_pubmed_title,
        authors_getter=get_pubmed_authors,
        year_getter=get_pubmed_year,
        author_match_fn=author_name_matches
    )

    return _best_item_by_score(articles, score_fn)


def build_bibtex_from_pubmed(article: Dict[str, Any], keyhint: str) -> Optional[str]:
    """
    Build a BibTeX entry from a PubMed article record.
    """
    from .text_utils import safe_get_field, extract_author_names
    from .bibtex_build import build_bibtex_entry, determine_entry_type

    title = safe_get_field(article, "title")
    if not title:
        return None

    # Parse authors 
    authors = extract_author_names(article.get("authors"), name_key="name")

    # Extract year from pubdate
    year = extract_year_from_any(article.get("pubdate"), fallback=0) or 0

    # Get journal name 
    venue = safe_get_field(article, "fulljournalname") or safe_get_field(article, "source")

    # Determine entry type (PubMed is typically journal articles)
    entry_type = determine_entry_type(article, venue_hints={"fulljournalname": "article", "source": "article"})

    # Get DOI from articleids
    doi = ""
    for aid in article.get("articleids") or []:
        if aid.get("idtype") == "doi":
            doi = aid.get("value") or ""
            break

    # Build PubMed URL
    pmid = article.get("uid") or article.get("pmid") or ""
    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""

    # Extra fields
    extra_fields = {}
    if article.get("volume"):
        extra_fields["volume"] = str(article["volume"])
    if article.get("issue"):
        extra_fields["number"] = str(article["issue"])
    if article.get("pages"):
        extra_fields["pages"] = str(article["pages"])
    if pmid:
        extra_fields["note"] = f"PMID: {pmid}"

    return build_bibtex_entry(
        entry_type=entry_type,
        title=title,
        authors=authors,
        year=year,
        keyhint=keyhint,
        venue=venue,
        doi=doi,
        url=url,
        arxiv_id=None,
        extra_fields=extra_fields
    )


# ============================================================================================
# Europe PMC API Integration
# ============================================================================================

def europepmc_search_paper(title: str, author_name: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Search Europe PMC for a publication by title and optional author.
    """
    if not title:
        return None

    from .api_generics import search_api_generic
    from .api_configs import EUROPEPMC_SEARCH_CONFIG

    # Build the custom query format for Europe PMC
    query = f'TITLE:"{title}"'
    if author_name:
        query += f' AND AUTH:"{author_name}"'

    config = copy.copy(EUROPEPMC_SEARCH_CONFIG)
    config.additional_params = {
        **config.additional_params,
        config.query_param_name: query
    }

    return search_api_generic(title, author_name, config)


def build_bibtex_from_europepmc(article: Dict[str, Any], keyhint: str) -> Optional[str]:
    """
    Build a BibTeX entry from a Europe PMC article record.
    """
    from .text_utils import safe_get_field, extract_author_names
    from .bibtex_build import build_bibtex_entry, determine_entry_type

    title = safe_get_field(article, "title")
    if not title:
        return None

    # Parse authors from authorString (comma-separated)
    authors = extract_author_names(article.get("authorString"))

    # Extract year
    year = 0
    year_str = article.get("pubYear") or ""
    if year_str:
        try:
            year = int(year_str)
        except NUMERIC_ERRORS:
            year = 0

    # Get journal name 
    venue = safe_get_field(article, "journalTitle") or safe_get_field(article, "bookTitle")

    # Determine entry type
    entry_type = determine_entry_type(
        article,
        type_field="pubType",
        venue_hints={"journalTitle": "article", "bookTitle": "inproceedings"}
    )

    # Get DOI 
    doi = safe_get_field(article, "doi")

    # Build URL
    pmid = article.get("pmid") or ""
    pmcid = article.get("pmcid") or ""
    if pmcid:
        url = f"https://europepmc.org/article/MED/{pmcid}"
    elif pmid:
        url = f"https://europepmc.org/article/MED/{pmid}"
    else:
        url = ""

    # Extra fields
    extra_fields = {}
    if article.get("journalVolume"):
        extra_fields["volume"] = str(article["journalVolume"])
    if article.get("issue"):
        extra_fields["number"] = str(article["issue"])
    if article.get("pageInfo"):
        extra_fields["pages"] = str(article["pageInfo"])
    if pmid:
        note_parts = [f"PMID: {pmid}"]
        if pmcid:
            note_parts.append(f"PMCID: {pmcid}")
        extra_fields["note"] = ", ".join(note_parts)

    return build_bibtex_entry(
        entry_type=entry_type,
        title=title,
        authors=authors,
        year=year,
        keyhint=keyhint,
        venue=venue,
        doi=doi,
        url=url,
        arxiv_id=None,
        extra_fields=extra_fields
    )


# ============================================================================================
# DataCite API Integration
# ============================================================================================

@handle_api_errors(default_return=None)
def datacite_search_doi(doi: str) -> Optional[Dict[str, Any]]:
    """
    Look up a DOI in DataCite to get dataset or software metadata.
    """
    if not doi:
        return None

    # Normalize DOI
    doi = _norm_doi(doi)
    if not doi:
        return None

    # DataCite uses URL-encoded DOI in path
    encoded_doi = urllib.parse.quote(doi, safe="")
    url = f"{DATACITE_BASE}/{encoded_doi}"

    data = http_get_json(url, timeout=15.0)

    return data.get("data") or None


def build_bibtex_from_datacite(record: Dict[str, Any], keyhint: str) -> Optional[str]:
    """
    Build a BibTeX entry from a DataCite record (typically for datasets/software).
    """
    from .bibtex_build import build_bibtex_entry, determine_entry_type
    from .text_utils import safe_get_field

    attributes = record.get("attributes") or {}

    # Get title - can be a list
    titles = attributes.get("titles") or []
    if titles and len(titles) > 0:
        title = safe_get_field(titles[0], "title")
    else:
        return None

    if not title:
        return None

    # Parse authors (creators in DataCite)
    authors: List[str] = []
    for creator in attributes.get("creators") or []:
        name = safe_get_field(creator, "name")
        if name:
            authors.append(name)

    # Extract year
    year = 0
    pub_year = attributes.get("publicationYear")
    if pub_year:
        try:
            year = int(pub_year)
        except NUMERIC_ERRORS:
            year = 0

    # Get publisher as "venue"
    venue = safe_get_field(attributes, "publisher")

    # DataCite records are typically datasets or software - determine entry type
    resource_type = attributes.get("types") or {}
    resource_type_general = safe_get_field(resource_type, "resourceTypeGeneral") or ""
    resource_type_general = resource_type_general.lower() if resource_type_general else ""
    # For DataCite, most records are misc since BibTeX doesn't have @dataset or @software
    entry_type = determine_entry_type(resource_type_general if resource_type_general else attributes)

    # Get DOI and URL 
    doi = safe_get_field(attributes, "doi")
    url = safe_get_field(attributes, "url")

    # Extra fields for datasets/software
    extra_fields = {}
    if resource_type_general:
        extra_fields["note"] = f"Type: {resource_type_general}"
    if attributes.get("version"):
        version_note = f"Version: {attributes['version']}"
        if "note" in extra_fields:
            extra_fields["note"] += f", {version_note}"
        else:
            extra_fields["note"] = version_note

    # Add howpublished for datasets/software
    if venue:
        extra_fields["howpublished"] = venue

    return build_bibtex_entry(
        entry_type=entry_type,
        title=title,
        authors=authors,
        year=year,
        keyhint=keyhint,
        venue="",  # Don't use venue field for datasets
        doi=doi,
        url=url,
        arxiv_id=None,
        extra_fields=extra_fields
    )


# ============================================================================================
# ORCID API Integration
# ============================================================================================

@handle_api_errors(default_return=[])
def orcid_fetch_works(orcid_id: str) -> List[Dict[str, Any]]:
    """
    Fetch a list of works for an ORCID author.
    """
    if not orcid_id:
        return []

    # Clean up ORCID ID (remove any URL prefix)
    orcid_id = orcid_id.replace("https://orcid.org/", "").replace("https://orcid.org/", "")

    # Build URL for works
    url = f"{ORCID_BASE}/{orcid_id}/works"

    # ORCID requires Accept header for content negotiation
    headers = DEFAULT_JSON_HEADERS.copy()
    headers["User-Agent"] = "CiteForge/1.0"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15.0) as response:
        data = json.loads(response.read().decode("utf-8"))

    works = []
    for work_group in (data.get("group") or []):
        work_summary = work_group.get("work-summary") or []
        if work_summary and len(work_summary) > 0:
            # Take the first work summary from each group
            work = work_summary[0]

            # Extract basic info into a dict
            title_obj = work.get("title") or {}
            title = (title_obj.get("title") or {}).get("value") or ""

            pub_date = work.get("publication-date") or {}
            year = pub_date.get("year") or {}
            year_val = year.get("value") if isinstance(year, dict) else None

            # Build simplified work record
            work_record = {
                "title": title,
                "year": year_val,
                "type": work.get("type"),
                "external-ids": work.get("external-ids") or {},
                "url": work.get("url") or {},
            }

            if title:  # Only add if it has a title
                works.append(work_record)

    return works


def orcid_search_work_by_title(orcid_id: str, title: str, _author_name: Optional[str] = None) -> Optional[
        Dict[str, Any]]:
    """
    Search ORCID works for a specific paper by title to validate a paper is in an author's ORCID profile.
    """
    works = orcid_fetch_works(orcid_id)
    if not works:
        return None

    target_norm = normalize_title(title)

    # Try exact match first
    for work in works:
        work_title = work.get("title") or ""
        if normalize_title(work_title) == target_norm:
            return work

    # Fall back to fuzzy matching
    def get_orcid_title(w: Dict[str, Any]) -> str:
        return w.get("title") or ""

    def get_orcid_year(w: Dict[str, Any]) -> Optional[int]:
        year = w.get("year")
        if year:
            try:
                return int(year)
            except NUMERIC_ERRORS:
                return None
        return None

    def match_fn(_name: str, _work_item: Dict[str, Any]) -> bool:
        # ORCID works don't have author lists in the summary
        # Just return True since we're already filtering by ORCID author
        # Note: Parameters unused but required by scoring function signature
        return True

    # Use scoring
    from .bibtex_build import create_scoring_function
    score_fn = create_scoring_function(
        title=title,
        author_name=None,  # Not used for ORCID since it's already author-specific
        year_hint=None,
        title_getter=get_orcid_title,
        authors_getter=lambda w: [],  # No authors in summary
        year_getter=get_orcid_year,
        author_match_fn=match_fn
    )

    return _best_item_by_score(works, score_fn)


def s2_search_papers_multiple(
    title: str,
    author_name: Optional[str],
    api_key: Optional[str],
    max_results: int = 5
) -> List[Dict[str, Any]]:
    """
    Search Semantic Scholar for multiple paper candidates matching the given title
    and author, returning top N results sorted by relevance.
    """
    if not api_key or not title:
        return []

    query_parts = [f'"{title}"']
    if author_name:
        query_parts.append(author_name)

    from .api_configs import S2_SEARCH_CONFIG

    config = copy.copy(S2_SEARCH_CONFIG)
    
    # Increase limit to get more results
    config.additional_params = {
        **config.additional_params,
        "limit": min(max_results * 2, 20)  # Get more than needed to filter
    }

    # Make the API call directly to get all results
    params = {config.query_param_name: " ".join(query_parts), **config.additional_params}
    url = build_url(config.base_url, params)

    try:
        data = s2_http_get_json(url, api_key, timeout=config.timeout)
    except ALL_API_ERRORS:
        return []

    results = safe_get_nested(data, *config.result_path, default=[])
    if not results:
        return []

    # Return top N results
    return results[:max_results]


def pubmed_search_papers_multiple(title: str, author_name: Optional[str], max_results: int = 5) -> List[Dict[str, Any]]:
    """
    Search PubMed for multiple paper candidates, returning top N results sorted by relevance.
    """
    if not title:
        return []

    # Step 1: Search for PMIDs
    search_query = f"{title}[Title]"
    if author_name:
        search_query += f" AND {author_name}[Author]"

    search_url = build_url(f"{PUBMED_BASE}/esearch.fcgi", {
        "db": "pubmed",
        "term": search_query,
        "retmax": max_results,
        "retmode": "json",
    })

    try:
        search_data = http_get_json(search_url, timeout=20.0)
    except NETWORK_ERRORS:
        return []

    id_list = safe_get_nested(search_data, "esearchresult", "idlist", default=[])
    if not id_list:
        return []

    # Step 2: Fetch summaries for all PMIDs
    summary_url = build_url(f"{PUBMED_BASE}/esummary.fcgi", {
        "db": "pubmed",
        "id": ",".join(id_list[:max_results]),
        "retmode": "json",
    })

    try:
        summary_data = http_get_json(summary_url, timeout=20.0)
    except NETWORK_ERRORS:
        return []

    result = safe_get_nested(summary_data, "result", default={})
    if not result:
        return []

    # Extract article data for each UID
    articles = []
    for uid in id_list[:max_results]:
        article = result.get(uid)
        if article and isinstance(article, dict):
            articles.append(article)

    return articles


def europepmc_search_papers_multiple(
    title: str, author_name: Optional[str], max_results: int = 5
) -> List[Dict[str, Any]]:
    """
    Search Europe PMC for multiple paper candidates, returning top N results sorted by relevance.
    """
    if not title:
        return []

    from .api_configs import EUROPEPMC_SEARCH_CONFIG

    query = f'TITLE:"{title}"'
    if author_name:
        query += f' AND AUTH:"{author_name}"'

    config = copy.copy(EUROPEPMC_SEARCH_CONFIG)
    config.additional_params = {
        **config.additional_params,
        "query": query,
        "pageSize": max_results,
    }

    params = {**config.additional_params}
    url = build_url(config.base_url, params)

    try:
        data = http_get_json(url, timeout=config.timeout)
    except ALL_API_ERRORS:
        return []

    results = safe_get_nested(data, *config.result_path, default=[])
    return results[:max_results]


def crossref_search_multiple(title: str, author_name: Optional[str], max_results: int = 5) -> List[Dict[str, Any]]:
    """
    Search Crossref for multiple work candidates, returning top N results sorted by relevance.
    """
    if not title:
        return []

    from .api_generics import search_api_generic_multiple
    from .api_configs import CROSSREF_SEARCH_CONFIG

    config = copy.copy(CROSSREF_SEARCH_CONFIG)
    # Adjust config based on whether we have an author
    additional_params = dict(config.additional_params)

    if author_name:
        additional_params["query.title"] = title
        additional_params["query.author"] = author_name
    else:
        additional_params["query.bibliographic"] = title

    mailto = os.getenv("CROSSREF_MAILTO")
    if mailto:
        additional_params["mailto"] = mailto

    config.additional_params = additional_params

    return search_api_generic_multiple(title, author_name, config, None, max_results)


def openalex_search_multiple(title: str, author_name: Optional[str], max_results: int = 5) -> List[Dict[str, Any]]:
    """
    Search OpenAlex for multiple work candidates, returning top N results sorted by relevance.
    """
    if not title:
        return []

    from .api_generics import search_api_generic_multiple
    from .api_configs import OPENALEX_SEARCH_CONFIG

    return search_api_generic_multiple(
        title, author_name, OPENALEX_SEARCH_CONFIG, None, max_results
    )


def openreview_search_papers_multiple(
    title: str,
    author_name: Optional[str],
    creds: Optional[tuple],
    max_results: int = 5
) -> List[Dict[str, Any]]:
    """
    Query OpenReview for notes whose titles resemble the requested paper,
    returning the top N candidates sorted by relevance score.
    """
    if not title:
        return []

    from .text_utils import normalize_title, author_name_matches

    headers = openreview_login(creds) or DEFAULT_JSON_HEADERS.copy()
    candidates: List[Dict[str, Any]] = []

    def _extend_with_notes(req_url: str) -> None:
        raw = http_fetch_bytes(req_url, headers, timeout=30.0)
        data = json.loads(raw.decode("utf-8"))
        notes = data.get("notes") or data.get("data") or []
        if isinstance(notes, list):
            candidates.extend(notes)

    # Try search endpoint 1: /notes with term query
    try:
        params = {"term": title, "details": "metadata"}
        url = build_url(f"{OPENREVIEW_BASE}/notes", params)
        _extend_with_notes(url)
    except ALL_API_ERRORS:
        pass
    # Try endpoint 2: /notes/search?q=
    if not candidates:
        try:
            url = build_url(f"{OPENREVIEW_BASE}/notes/search", {"q": title, "limit": 20})
            _extend_with_notes(url)
        except ALL_API_ERRORS:
            pass
    if not candidates:
        return []

    def note_title(note: Dict[str, Any]) -> str:
        c = note.get("content") or {}
        return (c.get("title") or note.get("title") or "").strip()

    def note_authors(note: Dict[str, Any]) -> Any:
        c = note.get("content") or {}
        return c.get("authors") or c.get("authorids") or note.get("authors")

    # Helper to extract year from Unix timestamp (milliseconds)
    def note_year(note_obj: Dict[str, Any]) -> Optional[int]:
        try:
            ms = note_obj.get("cdate") or note_obj.get("tcdate")
            if isinstance(ms, (int, float)):
                from datetime import datetime, timezone
                return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).year
        except (*NUMERIC_ERRORS, OSError):
            return None
        return None

    # Filter for exact title matches first
    target_norm = normalize_title(title)
    exact: List[Dict[str, Any]] = []
    for cand in candidates:
        if normalize_title(note_title(cand)) == target_norm:
            if not author_name or author_name_matches(author_name, note_authors(cand)):
                exact.append(cand)

    # If we have exact matches, prioritize those
    if exact:
        candidates = exact

    # Use scoring factory to rank all candidates
    from .bibtex_build import create_scoring_function
    score_fn = create_scoring_function(
        title=title,
        author_name=author_name,
        year_hint=None,
        title_getter=note_title,
        authors_getter=note_authors,
        year_getter=note_year
    )

    # Score and sort candidates
    scored = []
    for cand in candidates:
        try:
            score = score_fn(cand)
            if score is not None:
                scored.append((score, cand))
        except FIELD_ACCESS_ERRORS:
            # Skip candidates that cause scoring errors (missing fields, wrong types, etc.)
            continue

    # Sort by score (descending) and return top N
    scored.sort(key=lambda x: x[0], reverse=True)
    return [cand for score, cand in scored[:max_results]]
