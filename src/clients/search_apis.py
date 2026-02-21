from __future__ import annotations

import copy
import json
import os
import re
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from typing import Any

from ..cache import response_cache
from ..config import (
    ARXIV_BASE,
    CACHE_TTL_DOI_DAYS,
    CACHE_TTL_SEARCH_DAYS,
    DBLP_BASE,
    DBLP_PERSON_BASE,
    HTTP_TIMEOUT_DEFAULT,
    OPENREVIEW_BASE,
    PUBMED_BASE,
    SIM_EXACT_PICK_THRESHOLD,
    SIM_MERGE_DUPLICATE_THRESHOLD,
    SIM_TITLE_SIM_MIN,
)
from ..exceptions import (
    ALL_API_ERRORS,
    FIELD_ACCESS_ERRORS,
    NETWORK_ERRORS,
    NUMERIC_ERRORS,
    PARSE_ERRORS,
    XML_PARSE_ERRORS,
)
from ..http_utils import (
    DEFAULT_JSON_HEADERS,
    handle_api_errors,
    http_fetch_bytes,
    http_get_json,
    http_get_text,
    s2_http_get_json,
)
from ..id_utils import _norm_doi, find_arxiv_in_text, find_doi_in_text
from ..text_utils import (
    author_in_text,
    author_name_matches,
    authors_overlap,
    build_url,
    extract_year_from_any,
    normalize_title,
    safe_get_nested,
    title_similarity,
    trim_title_default,
)
from .helpers import _best_item_by_score, _sanitize_dblp_author, _score_candidate_generic

# ============ Semantic Scholar ============

def s2_search_paper(title: str, author_name: str | None, api_key: str | None) -> dict[str, Any] | None:
    """Search Semantic Scholar for a paper matching the given title and optional author."""
    if not api_key or not title:
        return None
    query_parts = [f'"{title}"']
    if author_name:
        query_parts.append(author_name)
    from ..api_configs import S2_SEARCH_CONFIG
    from ..api_generics import search_api_generic
    config = copy.copy(S2_SEARCH_CONFIG)
    config.additional_params = {**config.additional_params, config.query_param_name: " ".join(query_parts)}
    return search_api_generic(title, author_name, config, api_key=api_key)


def build_bibtex_from_s2(paper: dict[str, Any], keyhint: str) -> str | None:
    """Convert a Semantic Scholar paper record into a BibTeX entry."""
    from ..api_configs import S2_FIELD_MAPPING
    from ..api_generics import build_bibtex_from_response
    return build_bibtex_from_response(paper, keyhint, S2_FIELD_MAPPING)


def s2_search_papers_multiple(
    title: str, author_name: str | None, api_key: str | None, max_results: int = 5,
) -> list[dict[str, Any]]:
    """Search Semantic Scholar for multiple paper candidates."""
    if not api_key or not title:
        return []
    query_parts = [f'"{title}"']
    if author_name:
        query_parts.append(author_name)
    from ..api_configs import S2_SEARCH_CONFIG
    config = copy.copy(S2_SEARCH_CONFIG)
    config.additional_params = {**config.additional_params, "limit": min(max_results * 2, 20)}
    params = {config.query_param_name: " ".join(query_parts), **config.additional_params}
    url = build_url(config.base_url, params)
    try:
        data = s2_http_get_json(url, api_key, timeout=config.timeout)
    except ALL_API_ERRORS:
        return []
    results = safe_get_nested(data, *config.result_path, default=[])
    return list(results[:max_results]) if results else []


# ============ Crossref ============

def crossref_search(title: str, author_name: str | None) -> dict[str, Any] | None:
    """Look up a publication in Crossref by title and optional author."""
    if not title:
        return None
    from ..api_configs import CROSSREF_SEARCH_CONFIG
    from ..api_generics import search_api_generic
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


def build_bibtex_from_crossref(item: dict[str, Any], keyhint: str) -> str | None:
    """Build a BibTeX entry from a Crossref record."""
    from ..api_configs import CROSSREF_FIELD_MAPPING
    from ..api_generics import build_bibtex_from_response
    return build_bibtex_from_response(item, keyhint, CROSSREF_FIELD_MAPPING)


def crossref_search_multiple(title: str, author_name: str | None, max_results: int = 5) -> list[dict[str, Any]]:
    """Search Crossref for multiple work candidates."""
    if not title:
        return []
    from ..api_configs import CROSSREF_SEARCH_CONFIG
    from ..api_generics import search_api_generic_multiple
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
    return search_api_generic_multiple(title, author_name, config, None, max_results)


# ============ DOI / CSL ============

@handle_api_errors(default_return=None)
def fetch_csl_via_doi(doi: str, timeout: float = 20.0) -> dict[str, Any] | None:
    """Resolve a DOI using content negotiation and return the associated CSL-JSON metadata."""
    doi_norm = _norm_doi(doi)
    if not doi_norm:
        return None
    cached = response_cache.get("doi_csl", doi_norm)
    if cached is not None:
        return cached
    url = f"https://doi.org/{doi_norm}"
    headers = DEFAULT_JSON_HEADERS.copy()
    headers["Accept"] = "application/vnd.citationstyles.csl+json"
    raw = http_fetch_bytes(url, headers, timeout)
    result: dict[str, Any] = json.loads(raw.decode("utf-8"))
    response_cache.put("doi_csl", doi_norm, result, ttl_days=CACHE_TTL_DOI_DAYS)
    return result


def fetch_bibtex_via_doi(doi: str, timeout: float = 20.0) -> str | None:
    """Resolve a DOI and ask the resolver for BibTeX output."""
    doi_norm = _norm_doi(doi)
    if not doi_norm:
        return None
    cached = response_cache.get("doi_bibtex", doi_norm)
    if cached is not None:
        return cached.get("bibtex")
    url = f"https://doi.org/{doi_norm}"
    headers = DEFAULT_JSON_HEADERS.copy()
    headers["Accept"] = "application/x-bibtex"
    try:
        raw = http_fetch_bytes(url, headers, timeout)
        result = raw.decode("utf-8", errors="replace")
        response_cache.put("doi_bibtex", doi_norm, {"bibtex": result}, ttl_days=CACHE_TTL_DOI_DAYS)
        return result
    except NETWORK_ERRORS:
        return None


def bibtex_from_csl(csl: dict[str, Any], keyhint: str) -> str:
    """Translate a CSL-JSON citation description into a BibTeX entry."""
    from ..bibtex_build import build_bibtex_entry, determine_entry_type
    from ..text_utils import extract_authors_from_any, extract_year_from_any, safe_get_field
    title = safe_get_field(csl, "title") or ""
    subtitle_raw = csl.get("subtitle")
    subtitle = (subtitle_raw[0] if subtitle_raw else "") if isinstance(subtitle_raw, list) else (subtitle_raw or "")
    if subtitle:
        title = f"{title}: {subtitle}" if title else subtitle
    authors = extract_authors_from_any(csl, field_names=["author"])
    year = extract_year_from_any(csl, fallback=0) or 0
    container = safe_get_field(csl, "container-title")
    entry_type = determine_entry_type(csl)
    doi = safe_get_field(csl, "DOI")
    url = safe_get_field(csl, "URL")
    volume = safe_get_field(csl, "volume")
    number = safe_get_field(csl, "issue")
    pages = safe_get_field(csl, "page")
    publisher = safe_get_field(csl, "publisher")
    if publisher and publisher.strip().lower() == "arxiv":
        publisher = None
    return build_bibtex_entry(
        entry_type=entry_type, title=title, authors=authors, year=year, keyhint=keyhint,
        venue=container or None, doi=doi or None, url=url or None,
        extra_fields={
            k: v for k, v in
            {"volume": volume, "number": number, "pages": pages, "publisher": publisher}.items()
            if v is not None
        },
    )


# ============ arXiv ============

def arxiv_search(
    title: str, author_name: str | None, year_hint: int | None, max_results: int = 10,
) -> list[dict[str, Any]]:
    """Search arXiv for papers matching the given title and optional author."""
    if not title:
        return []
    cache_key = f"{normalize_title(title)}|{(author_name or '').strip().lower()}"
    cached = response_cache.get("arxiv", cache_key)
    if cached is not None:
        entries_cached: list[dict[str, Any]] = cached.get("entries", [])
        return entries_cached
    q_parts = [f'ti:"{title}"']
    if author_name:
        q_parts.append(f'au:"{author_name}"')
    search_query = "+AND+".join(q_parts)
    params = {
        "search_query": search_query, "start": 0, "max_results": max_results,
        "sortBy": "relevance", "sortOrder": "descending",
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
        return tag[1:].split("}")[0] if tag.startswith("{") else ""

    atom_ns = _ns_uri(root.tag)

    def qn(ns: str, local: str) -> str:
        return f"{{{ns}}}{local}" if ns else local

    def find_child(el: ElementTree.Element, local: str) -> ElementTree.Element | None:
        for child in el:
            if child.tag.split("}")[-1] == local:
                return child
        return None

    entries = []
    for entry_el in root.findall(qn(atom_ns, "entry")):
        title_el = find_child(entry_el, "title")
        title_val = (title_el.text or "").strip() if title_el is not None else ""
        authors_list: list[str] = []
        for author_el in entry_el.findall(qn(atom_ns, "author")):
            name_el = find_child(author_el, "name")
            if name_el is not None and name_el.text:
                authors_list.append(name_el.text.strip())
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
            if link_el.attrib.get("rel", "") == "alternate":
                link_abs = link_el.attrib.get("href", "")
        doi_val = ""
        for ch in entry_el.iter():
            if ch.tag.split("}")[-1] == "doi" and ch.text:
                doi_val = find_doi_in_text(ch.text.strip()) or ""
                break
        pc = ""
        for ch in entry_el.iter():
            if ch.tag.split("}")[-1] == "primary_category":
                pc = ch.attrib.get("term", "") or ""
                break
        arxiv_id = find_arxiv_in_text(link_abs or entry_id) or ""
        entries.append({
            "title": title_val, "authors": authors_list, "year": year,
            "abs_url": link_abs, "doi": doi_val, "primary_class": pc, "arxiv_id": arxiv_id,
        })
    if not entries:
        return []
    from ..bibtex_build import create_scoring_function
    score_fn = create_scoring_function(
        title=title, author_name=author_name, year_hint=year_hint,
        title_getter=lambda ent: ent.get("title", ""),
        authors_getter=lambda ent: ent.get("authors", []),
        year_getter=lambda ent: ent.get("year"),
        author_match_fn=lambda anv, al: authors_overlap(anv, al)
    )
    entries.sort(key=score_fn, reverse=True)
    response_cache.put("arxiv", cache_key, {"entries": entries}, ttl_days=CACHE_TTL_SEARCH_DAYS)
    return entries


def build_bibtex_from_arxiv(entry: dict[str, Any], keyhint: str) -> str | None:
    """Turn a parsed arXiv search result into a BibTeX entry."""
    from ..api_configs import ARXIV_FIELD_MAPPING
    from ..api_generics import build_bibtex_from_response
    return build_bibtex_from_response(entry, keyhint, ARXIV_FIELD_MAPPING)


# ============ OpenReview ============

_OPENREVIEW_SESSION: dict[str, str] | None = None
_OPENREVIEW_SESSION_LOCK = threading.Lock()


def openreview_login(creds: tuple[str, ...] | None) -> dict[str, str] | None:
    """Log into OpenReview and return headers with a session cookie."""
    global _OPENREVIEW_SESSION
    if not creds:
        return None
    # Fast path: return cached session without acquiring the lock
    if _OPENREVIEW_SESSION is not None:
        return _OPENREVIEW_SESSION
    with _OPENREVIEW_SESSION_LOCK:
        # Double-check after acquiring lock
        if _OPENREVIEW_SESSION is not None:
            return _OPENREVIEW_SESSION
        login, password = creds[0], creds[1]
        try:
            url = f"{OPENREVIEW_BASE}/login"
            payload = json.dumps({"id": login, "password": password}).encode("utf-8")
            headers = DEFAULT_JSON_HEADERS.copy()
            headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=20) as resp:
                set_cookie = resp.headers.get("Set-Cookie") if hasattr(resp, "headers") else None
                if set_cookie:
                    headers_with_cookie = DEFAULT_JSON_HEADERS.copy()
                    headers_with_cookie["Cookie"] = set_cookie
                    _OPENREVIEW_SESSION = headers_with_cookie
                    return _OPENREVIEW_SESSION
        except (*NETWORK_ERRORS, *PARSE_ERRORS):
            return None
    return None


def openreview_search_paper(
    title: str, author_name: str | None, creds: tuple[str, ...] | None,
) -> dict[str, Any] | None:
    """Query OpenReview for notes matching the requested paper."""
    if not title:
        return None
    cache_key = f"{normalize_title(title)}|{(author_name or '').strip().lower()}"
    cached = response_cache.get("openreview", cache_key)
    if cached is not None:
        return cached if cached else None
    headers = openreview_login(creds) or DEFAULT_JSON_HEADERS.copy()
    candidates: list[dict[str, Any]] = []

    def _extend_with_notes(req_url: str) -> None:
        raw = http_fetch_bytes(req_url, headers, timeout=30.0)
        data = json.loads(raw.decode("utf-8"))
        notes = data.get("notes") or data.get("data") or []
        if isinstance(notes, list):
            candidates.extend(notes)

    try:
        url = build_url(f"{OPENREVIEW_BASE}/notes", {"term": title, "details": "metadata"})
        _extend_with_notes(url)
    except ALL_API_ERRORS:
        pass
    if not candidates:
        try:
            url = build_url(f"{OPENREVIEW_BASE}/notes/search", {"q": title, "limit": 20})
            _extend_with_notes(url)
        except ALL_API_ERRORS:
            pass
    if not candidates:
        return None

    def note_title(note: dict[str, Any]) -> str:
        c = note.get("content") or {}
        return (c.get("title") or note.get("title") or "").strip()

    def note_authors(note: dict[str, Any]) -> Any:
        c = note.get("content") or {}
        return c.get("authors") or c.get("authorids") or note.get("authors")

    target_norm = normalize_title(title)
    for cand in candidates:
        if normalize_title(note_title(cand)) == target_norm and (
            not author_name or author_name_matches(author_name, note_authors(cand))
        ):
            response_cache.put("openreview", cache_key, dict(cand), ttl_days=CACHE_TTL_SEARCH_DAYS)
            return cand

    def note_year(note_obj: dict[str, Any]) -> int | None:
        try:
            ms = note_obj.get("cdate") or note_obj.get("tcdate")
            if isinstance(ms, (int, float)):
                return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).year
        except (*NUMERIC_ERRORS, OSError):
            return None
        return None

    from ..bibtex_build import create_scoring_function
    score_fn = create_scoring_function(
        title=title, author_name=author_name, year_hint=None,
        title_getter=note_title, authors_getter=note_authors, year_getter=note_year,
    )
    best = _best_item_by_score(candidates, score_fn, threshold=SIM_EXACT_PICK_THRESHOLD)
    if best is not None:
        response_cache.put("openreview", cache_key, dict(best), ttl_days=CACHE_TTL_SEARCH_DAYS)
    return best


def build_bibtex_from_openreview(note: dict[str, Any], keyhint: str) -> str | None:
    """Build a BibTeX entry from an OpenReview note."""
    from ..api_configs import OPENREVIEW_FIELD_MAPPING
    from ..api_generics import build_bibtex_from_response
    return build_bibtex_from_response(note, keyhint, OPENREVIEW_FIELD_MAPPING)


def openreview_search_papers_multiple(
    title: str, author_name: str | None, creds: tuple[str, ...] | None, max_results: int = 5,
) -> list[dict[str, Any]]:
    """Query OpenReview for multiple candidate notes."""
    if not title:
        return []
    cache_key = f"multi|{normalize_title(title)}|{(author_name or '').strip().lower()}"
    cached = response_cache.get("openreview", cache_key)
    if cached is not None:
        return list(cached.get("results", []))
    headers = openreview_login(creds) or DEFAULT_JSON_HEADERS.copy()
    candidates: list[dict[str, Any]] = []

    def _extend_with_notes(req_url: str) -> None:
        raw = http_fetch_bytes(req_url, headers, timeout=30.0)
        data = json.loads(raw.decode("utf-8"))
        notes = data.get("notes") or data.get("data") or []
        if isinstance(notes, list):
            candidates.extend(notes)

    try:
        url = build_url(f"{OPENREVIEW_BASE}/notes", {"term": title, "details": "metadata"})
        _extend_with_notes(url)
    except ALL_API_ERRORS:
        pass
    if not candidates:
        try:
            url = build_url(f"{OPENREVIEW_BASE}/notes/search", {"q": title, "limit": 20})
            _extend_with_notes(url)
        except ALL_API_ERRORS:
            pass
    if not candidates:
        return []

    def note_title(note: dict[str, Any]) -> str:
        c = note.get("content") or {}
        return (c.get("title") or note.get("title") or "").strip()

    def note_authors(note: dict[str, Any]) -> Any:
        c = note.get("content") or {}
        return c.get("authors") or c.get("authorids") or note.get("authors")

    def note_year(note_obj: dict[str, Any]) -> int | None:
        try:
            ms = note_obj.get("cdate") or note_obj.get("tcdate")
            if isinstance(ms, (int, float)):
                return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).year
        except (*NUMERIC_ERRORS, OSError):
            return None
        return None

    target_norm = normalize_title(title)
    exact: list[dict[str, Any]] = []
    for cand in candidates:
        if normalize_title(note_title(cand)) == target_norm and (
            not author_name or author_name_matches(author_name, note_authors(cand))
        ):
            exact.append(cand)
    if exact:
        candidates = exact
    from ..bibtex_build import create_scoring_function
    score_fn = create_scoring_function(
        title=title, author_name=author_name, year_hint=None,
        title_getter=note_title, authors_getter=note_authors, year_getter=note_year,
    )
    scored = []
    for cand in candidates:
        try:
            score = score_fn(cand)
            if score is not None:
                scored.append((score, cand))
        except FIELD_ACCESS_ERRORS:
            continue
    scored.sort(key=lambda x: x[0], reverse=True)
    top_results = [cand for _score, cand in scored[:max_results]]
    if top_results:
        response_cache.put("openreview", cache_key, {"results": top_results}, ttl_days=CACHE_TTL_SEARCH_DAYS)
    return top_results


# ============ DBLP ============

def dblp_extract_pid(val: str | None) -> str | None:
    """Extract a DBLP person identifier from a hint value."""
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
def dblp_find_author_pid(name: str) -> str | None:
    """Look up a DBLP person identifier for an author name."""
    if not name:
        return None
    params = {"q": name, "format": "json"}
    url = build_url(DBLP_BASE, params)
    data = http_get_json(url)
    res = (data.get("result") or {}).get("hits") or {}
    hits = res.get("hit") or []
    name_norm = name.strip().lower()
    exact_pid = None
    first_pid = None
    for h in hits:
        info = h.get("info") or {}
        pid = (info.get("pid") or "").strip()
        author_name_val = (info.get("author") or info.get("name") or "").strip()
        if pid and not first_pid:
            first_pid = pid
        if author_name_val and author_name_val.lower() == name_norm:
            exact_pid = pid
            break
    return exact_pid or first_pid


def _xml_text(el: ElementTree.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""




def dblp_fetch_publications(pid: str) -> list[dict[str, Any]]:
    """Download a DBLP author XML record and convert entries into publication dicts."""
    if not pid:
        return []
    cache_key = f"dblp_pubs|{pid}"
    cached = response_cache.get("dblp", cache_key)
    if cached is not None:
        return list(cached.get("articles", []))
    url = f"{DBLP_PERSON_BASE}/{pid}.xml"
    try:
        xml = http_get_text(url, timeout=HTTP_TIMEOUT_DEFAULT)
    except NETWORK_ERRORS:
        return []
    try:
        root = ElementTree.fromstring(xml)
    except XML_PARSE_ERRORS:
        return []
    articles: list[dict[str, Any]] = []
    for r in root.findall("r"):
        child = None
        for ch in r:
            if isinstance(ch.tag, str):
                child = ch
                break
        if child is None:
            continue
        title_el = child.find("title")
        title_val = "".join(title_el.itertext()) if title_el is not None else ""
        title = trim_title_default(title_val or "")
        if not title:
            continue
        year = 0
        year_el = child.find("year")
        if year_el is not None and year_el.text and re.match(r"^(19|20)\d{2}$", year_el.text.strip()):
            try:
                year = int(year_el.text.strip())
            except PARSE_ERRORS:
                year = 0
        authors_list: list[str] = []
        for ael in child.findall("author"):
            nm = _xml_text(ael)
            if nm:
                nm = _sanitize_dblp_author(nm)
                if nm:
                    authors_list.append(nm)
        if not authors_list:
            for eel in child.findall("editor"):
                nm = _xml_text(eel)
                if nm:
                    nm = _sanitize_dblp_author(nm)
                    if nm:
                        authors_list.append(nm)
        ee = _xml_text(child.find("ee"))
        dburl = _xml_text(child.find("url"))
        doi = _norm_doi(find_doi_in_text(ee) or find_doi_in_text(dburl))
        abs_or_url = ee or dburl
        venue = _xml_text(child.find("journal")) or _xml_text(child.find("booktitle"))
        art: dict[str, Any] = {
            "title": title, "authors": authors_list, "year": year,
            "publication": venue, "link": abs_or_url,
            "snippet": ", ".join([v for v in [venue, str(year) if year else "", doi or ""] if v]),
            "source": "dblp",
        }
        if doi:
            art["result_id"] = f"dblp:doi:{doi}"
        else:
            _san = re.sub(r"\W+", "_", normalize_title(title))
            art["result_id"] = f"dblp:{_san[:64]}"
        articles.append(art)
    if articles:
        response_cache.put("dblp", cache_key, {"articles": articles}, ttl_days=CACHE_TTL_SEARCH_DAYS)
    return articles


def build_synthetic_article_from_dblp(item: dict[str, Any]) -> dict[str, Any]:
    return dict(item)


def enhance_scholar_article_with_dblp(
    scholar_art: dict[str, Any], dblp_items: list[dict[str, Any]], target_author: str | None = None,
) -> bool:
    """Enhance a Scholar article with complete data from DBLP if a match is found."""
    from ..text_utils import is_truncated
    if not dblp_items:
        return False
    scholar_title = scholar_art.get("title", "")
    if not scholar_title:
        return False
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
            target_title=scholar_title, target_author=target_author, target_year=scholar_art.get("year"),
            cand_title=dblp_title, cand_authors=dblp_item.get("authors", []), cand_year=dblp_item.get("year"),
            title_sim=title_similarity,
            author_match=lambda anv, al: authors_overlap(anv, al),
        )
        if score > best_score:
            best_score = score
            best_match = dblp_item
    if best_score >= SIM_MERGE_DUPLICATE_THRESHOLD and best_match:
        enhanced = False
        if is_truncated(scholar_title) and best_match.get("title") and not is_truncated(best_match["title"]):
            scholar_art["title"] = best_match["title"]
            enhanced = True
        scholar_authors = scholar_art.get("author_info", [])
        if is_truncated(str(scholar_authors)) and best_match.get("authors"):
            dblp_authors = best_match["authors"]
            if not is_truncated(str(dblp_authors)):
                if isinstance(dblp_authors, list):
                    scholar_art["author_info"] = [{"name": a} for a in dblp_authors]
                else:
                    scholar_art["author_info"] = dblp_authors
                enhanced = True
        scholar_pub = scholar_art.get("publication_info", "")
        if best_match.get("publication") and (not scholar_pub or is_truncated(scholar_pub)):
            scholar_art["publication_info"] = best_match["publication"]
            enhanced = True
        if not scholar_art.get("year") and best_match.get("year"):
            scholar_art["year"] = best_match["year"]
            enhanced = True
        if enhanced:
            scholar_art["_dblp_enhanced"] = True
            return True
    return False


def dblp_fetch_for_author(name: str, dblp_hint: str | None, min_year: int | None) -> list[dict[str, Any]]:
    """Fetch DBLP publications for an author."""
    pid = dblp_extract_pid(dblp_hint) if dblp_hint else None
    if not pid:
        pid = dblp_find_author_pid(name)
    items = dblp_fetch_publications(pid) if pid else []
    if min_year:
        items = [it for it in items if (it.get("year") or 0) >= int(min_year)]
    return items


# ============ OpenAlex ============

def openalex_search_paper(title: str, author_name: str | None) -> dict[str, Any] | None:
    """Search OpenAlex for a publication by title and optional author."""
    from ..api_configs import OPENALEX_SEARCH_CONFIG
    from ..api_generics import search_api_generic
    return search_api_generic(title, author_name, OPENALEX_SEARCH_CONFIG)


def build_bibtex_from_openalex(work: dict[str, Any], keyhint: str) -> str | None:
    """Build a BibTeX entry from an OpenAlex work record."""
    from ..api_configs import OPENALEX_FIELD_MAPPING
    from ..api_generics import build_bibtex_from_response
    return build_bibtex_from_response(work, keyhint, OPENALEX_FIELD_MAPPING)


def openalex_search_multiple(title: str, author_name: str | None, max_results: int = 5) -> list[dict[str, Any]]:
    """Search OpenAlex for multiple work candidates."""
    if not title:
        return []
    from ..api_configs import OPENALEX_SEARCH_CONFIG
    from ..api_generics import search_api_generic_multiple
    return search_api_generic_multiple(title, author_name, OPENALEX_SEARCH_CONFIG, None, max_results)


# ============ PubMed ============

@handle_api_errors(default_return=None)
def pubmed_search_paper(title: str, author_name: str | None) -> dict[str, Any] | None:
    """Search PubMed for a publication by title and optional author."""
    if not title:
        return None
    cache_key = f"{normalize_title(title)}|{(author_name or '').strip().lower()}"
    cached = response_cache.get("pubmed", cache_key)
    if cached is not None:
        return cached if cached else None
    search_query = f"{title}[Title]"
    if author_name:
        search_query += f" AND {author_name}[Author]"
    search_url = build_url(
        f"{PUBMED_BASE}/esearch.fcgi",
        {"db": "pubmed", "term": search_query, "retmax": 10, "retmode": "json"},
    )
    search_data = http_get_json(search_url, timeout=15.0)
    pmids = (search_data.get("esearchresult") or {}).get("idlist") or []
    if not pmids:
        return None
    fetch_url = build_url(
        f"{PUBMED_BASE}/esummary.fcgi",
        {"db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
    )
    fetch_data = http_get_json(fetch_url, timeout=15.0)
    result = fetch_data.get("result") or {}
    articles = [result[pmid] for pmid in pmids if pmid in result and isinstance(result[pmid], dict)]
    if not articles:
        return None
    target_norm = normalize_title(title)
    for article in articles:
        article_title = article.get("title") or ""
        if normalize_title(article_title) == target_norm and (
            not author_name or author_in_text(author_name, str(article.get("authors") or []))
        ):
            result = dict(article)
            response_cache.put("pubmed", cache_key, result, ttl_days=CACHE_TTL_SEARCH_DAYS)
            return result
    from ..bibtex_build import create_scoring_function
    score_fn = create_scoring_function(
        title=title, author_name=author_name, year_hint=None,
        title_getter=lambda a: a.get("title") or "",
        authors_getter=lambda a: [auth.get("name") or "" for auth in (a.get("authors") or []) if auth.get("name")],
        year_getter=lambda a: extract_year_from_any(a.get("pubdate"), fallback=None),
        author_match_fn=author_name_matches,
    )
    best = _best_item_by_score(articles, score_fn)
    if best is not None:
        response_cache.put("pubmed", cache_key, dict(best), ttl_days=CACHE_TTL_SEARCH_DAYS)
    return best


def build_bibtex_from_pubmed(article: dict[str, Any], keyhint: str) -> str | None:
    """Build a BibTeX entry from a PubMed article record."""
    from ..bibtex_build import build_bibtex_entry, determine_entry_type
    from ..text_utils import extract_author_names, safe_get_field
    title = safe_get_field(article, "title")
    if not title:
        return None
    authors = extract_author_names(article.get("authors"), name_key="name")
    year = extract_year_from_any(article.get("pubdate"), fallback=0) or 0
    venue = safe_get_field(article, "fulljournalname") or safe_get_field(article, "source")
    entry_type = determine_entry_type(article, venue_hints={"fulljournalname": "article", "source": "article"})
    doi = ""
    for aid in article.get("articleids") or []:
        if aid.get("idtype") == "doi":
            doi = aid.get("value") or ""
            break
    pmid = article.get("uid") or article.get("pmid") or ""
    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
    extra_fields: dict[str, str] = {}
    if article.get("volume"):
        extra_fields["volume"] = str(article["volume"])
    if article.get("issue"):
        extra_fields["number"] = str(article["issue"])
    if article.get("pages"):
        extra_fields["pages"] = str(article["pages"])
    if pmid:
        extra_fields["note"] = f"PMID: {pmid}"
    return build_bibtex_entry(
        entry_type=entry_type, title=title, authors=authors, year=year,
        keyhint=keyhint, venue=venue, doi=doi, url=url, extra_fields=extra_fields,
    )


def pubmed_search_papers_multiple(title: str, author_name: str | None, max_results: int = 5) -> list[dict[str, Any]]:
    """Search PubMed for multiple paper candidates."""
    if not title:
        return []
    cache_key = f"multi|{normalize_title(title)}|{(author_name or '').strip().lower()}"
    cached = response_cache.get("pubmed", cache_key)
    if cached is not None:
        return list(cached.get("results", []))
    search_query = f"{title}[Title]"
    if author_name:
        search_query += f" AND {author_name}[Author]"
    search_url = build_url(
        f"{PUBMED_BASE}/esearch.fcgi",
        {"db": "pubmed", "term": search_query, "retmax": max_results, "retmode": "json"},
    )
    try:
        search_data = http_get_json(search_url, timeout=20.0)
    except NETWORK_ERRORS:
        return []
    id_list = safe_get_nested(search_data, "esearchresult", "idlist", default=[])
    if not id_list:
        return []
    summary_url = build_url(
        f"{PUBMED_BASE}/esummary.fcgi",
        {"db": "pubmed", "id": ",".join(id_list[:max_results]), "retmode": "json"},
    )
    try:
        summary_data = http_get_json(summary_url, timeout=20.0)
    except NETWORK_ERRORS:
        return []
    result = safe_get_nested(summary_data, "result", default={})
    results_list = [result[uid] for uid in id_list[:max_results] if uid in result and isinstance(result.get(uid), dict)]
    if results_list:
        response_cache.put("pubmed", cache_key, {"results": results_list}, ttl_days=CACHE_TTL_SEARCH_DAYS)
    return results_list


# ============ Europe PMC ============

def europepmc_search_paper(title: str, author_name: str | None) -> dict[str, Any] | None:
    """Search Europe PMC for a publication by title and optional author."""
    if not title:
        return None
    from ..api_configs import EUROPEPMC_SEARCH_CONFIG
    from ..api_generics import search_api_generic
    query = f'TITLE:"{title}"'
    if author_name:
        query += f' AND AUTH:"{author_name}"'
    config = copy.copy(EUROPEPMC_SEARCH_CONFIG)
    config.additional_params = {**config.additional_params, config.query_param_name: query}
    return search_api_generic(title, author_name, config)


def build_bibtex_from_europepmc(article: dict[str, Any], keyhint: str) -> str | None:
    """Build a BibTeX entry from a Europe PMC article record."""
    from ..bibtex_build import build_bibtex_entry, determine_entry_type
    from ..text_utils import extract_author_names, safe_get_field
    title = safe_get_field(article, "title")
    if not title:
        return None
    authors = extract_author_names(article.get("authorString"))
    year = 0
    year_str = article.get("pubYear") or ""
    if year_str:
        try:
            year = int(year_str)
        except NUMERIC_ERRORS:
            year = 0
    venue = safe_get_field(article, "journalTitle") or safe_get_field(article, "bookTitle")
    entry_type = determine_entry_type(
        article, type_field="pubType",
        venue_hints={"journalTitle": "article", "bookTitle": "inproceedings"},
    )
    doi = safe_get_field(article, "doi")
    pmid = article.get("pmid") or ""
    pmcid = article.get("pmcid") or ""
    if pmcid:
        url = f"https://europepmc.org/article/MED/{pmcid}"
    elif pmid:
        url = f"https://europepmc.org/article/MED/{pmid}"
    else:
        url = ""
    extra_fields: dict[str, str] = {}
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
        entry_type=entry_type, title=title, authors=authors, year=year,
        keyhint=keyhint, venue=venue, doi=doi, url=url, extra_fields=extra_fields,
    )


def europepmc_search_papers_multiple(title: str, author_name: str | None, max_results: int = 5) -> list[dict[str, Any]]:
    """Search Europe PMC for multiple paper candidates."""
    if not title:
        return []
    from ..api_configs import EUROPEPMC_SEARCH_CONFIG
    query = f'TITLE:"{title}"'
    if author_name:
        query += f' AND AUTH:"{author_name}"'
    config = copy.copy(EUROPEPMC_SEARCH_CONFIG)
    config.additional_params = {**config.additional_params, "query": query, "pageSize": max_results}
    url = build_url(config.base_url, config.additional_params)
    try:
        data = http_get_json(url, timeout=config.timeout)
    except ALL_API_ERRORS:
        return []
    results = safe_get_nested(data, *config.result_path, default=[])
    return list(results[:max_results])
