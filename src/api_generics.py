from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .exceptions import ALL_API_ERRORS, FIELD_ACCESS_ERRORS
from .http_utils import http_get_json, s2_http_get_json
from .text_utils import (
    build_url,
    normalize_title,
    author_in_text,
    author_name_matches,
    safe_get_field,
    safe_get_nested,
    extract_author_names,
    extract_year_from_any,
    has_placeholder,
)
from .id_utils import find_doi_in_text, find_arxiv_in_text
from .config import SIM_EXACT_PICK_THRESHOLD


@dataclass
class APISearchConfig:
    """
    Configuration for API-specific search behavior including endpoint details,
    query parameters, and custom field extractors.
    """
    api_name: str
    base_url: str

    # Query parameters
    query_param_name: str = "query"
    author_param_name: Optional[str] = None
    additional_params: Dict[str, Any] = field(default_factory=dict)

    # Response structure
    result_path: List[str] = field(default_factory=lambda: ["results"])
    title_field: str = "title"
    author_field: str = "authors"

    # Customization
    timeout: float = 15.0
    requires_api_key: bool = False

    # Optional custom extractors
    title_getter: Optional[Callable[[Dict[str, Any]], str]] = None
    year_getter: Optional[Callable[[Dict[str, Any]], Optional[int]]] = None
    authors_getter: Optional[Callable[[Dict[str, Any]], Any]] = None


@dataclass
class APIFieldMapping:
    """
    Configuration for API-specific field mappings when building BibTeX entries,
    translating diverse field names and structures to a unified BibTeX format.
    """
    api_name: str

    # Core field mappings (list of possible field names, first match wins)
    title_fields: List[str]
    author_fields: List[str]
    year_fields: List[str]
    venue_fields: List[str]

    # Identifier mappings
    doi_fields: List[str] = field(default_factory=lambda: ["doi"])
    url_fields: List[str] = field(default_factory=lambda: ["url"])
    arxiv_fields: List[str] = field(default_factory=list)
    pmid_fields: List[str] = field(default_factory=list)

    # Extra field mappings (source_field -> bibtex_field)
    extra_field_mappings: Dict[str, str] = field(default_factory=dict)

    # Author extraction config
    author_name_key: Optional[str] = "name"
    author_given_key: Optional[str] = None
    author_family_key: Optional[str] = None

    # Entry type config
    entry_type_field: str = "type"
    entry_type_list_field: Optional[str] = None
    venue_hints: Dict[str, str] = field(default_factory=dict)

    # Custom extractors for complex cases
    custom_author_extractor: Optional[Callable[[Dict[str, Any]], List[str]]] = None
    custom_year_extractor: Optional[Callable[[Dict[str, Any]], int]] = None


def search_api_generic(
    title: str,
    author_name: Optional[str],
    config: APISearchConfig,
    api_key: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Search for academic publications across different API providers using a unified
    interface with a two-pass matching strategy that attempts exact title matches first
    and falls back to fuzzy matching when needed.
    """
    if not title:
        return None

    # Build query parameters
    params = {config.query_param_name: title, **config.additional_params}
    if author_name and config.author_param_name:
        params[config.author_param_name] = author_name

    url = build_url(config.base_url, params)

    # Make HTTP request
    try:
        if api_key and config.api_name == "semantic_scholar":
            data = s2_http_get_json(url, api_key, timeout=config.timeout)
        else:
            data = http_get_json(url, timeout=config.timeout)
    except ALL_API_ERRORS:
        return None

    # Extract results using configured path
    results = safe_get_nested(data, *config.result_path, default=[])
    if not results:
        return None

    # Try exact title match first
    target_norm = normalize_title(title)
    for item in results:
        # Use custom getter if provided, otherwise use field name
        if config.title_getter:
            item_title = config.title_getter(item)
        else:
            item_title = safe_get_field(item, config.title_field) or ""

        if normalize_title(item_title) == target_norm:
            # Check author if provided
            if not author_name:
                return item

            # Use custom getter if provided
            if config.authors_getter:
                item_authors = config.authors_getter(item)
            else:
                item_authors = item.get(config.author_field)

            if author_name_matches(author_name, item_authors) or author_in_text(author_name, item_authors):
                return item

    # Fuzzy match using scoring function
    from .bibtex_build import create_scoring_function
    from .api_clients import _best_item_by_score

    # Build getters for scoring
    if config.title_getter:
        title_getter = config.title_getter
    else:
        def title_getter(c):
            return safe_get_field(c, config.title_field) or ""

    if config.authors_getter:
        authors_getter = config.authors_getter
    else:
        def authors_getter(c):
            return c.get(config.author_field) or []

    if config.year_getter:
        year_getter = config.year_getter
    else:
        def year_getter(c):
            return c.get("year")

    score_fn = create_scoring_function(
        title=title,
        author_name=author_name,
        year_hint=None,
        title_getter=title_getter,
        authors_getter=authors_getter,
        year_getter=year_getter
    )

    return _best_item_by_score(results, score_fn, threshold=SIM_EXACT_PICK_THRESHOLD)


def search_api_generic_multiple(
    title: str,
    author_name: Optional[str],
    config: APISearchConfig,
    api_key: Optional[str] = None,
    max_results: int = 5
) -> List[Dict[str, Any]]:
    """
    Search for academic publications and return multiple candidates sorted by relevance.

    Similar to search_api_generic but returns a list of top candidates instead of just
    the best match, enabling multiple candidates for validation.
    """
    if not title:
        return []

    # Build query parameters
    params = {config.query_param_name: title, **config.additional_params}
    if author_name and config.author_param_name:
        params[config.author_param_name] = author_name

    url = build_url(config.base_url, params)

    # Make HTTP request
    try:
        if api_key and config.api_name == "semantic_scholar":
            data = s2_http_get_json(url, api_key, timeout=config.timeout)
        else:
            data = http_get_json(url, timeout=config.timeout)
    except ALL_API_ERRORS:
        return []

    # Extract results using configured path
    results = safe_get_nested(data, *config.result_path, default=[])
    if not results:
        return []

    # Build getters for scoring
    if config.title_getter:
        title_getter = config.title_getter
    else:
        def title_getter(c):
            return safe_get_field(c, config.title_field) or ""

    if config.authors_getter:
        authors_getter = config.authors_getter
    else:
        def authors_getter(c):
            return c.get(config.author_field) or []

    if config.year_getter:
        year_getter = config.year_getter
    else:
        def year_getter(c):
            return c.get("year")

    # Score all results
    from .bibtex_build import create_scoring_function

    score_fn = create_scoring_function(
        title=title,
        author_name=author_name,
        year_hint=None,
        title_getter=title_getter,
        authors_getter=authors_getter,
        year_getter=year_getter
    )

    scored_results = []
    # Use a slightly lower threshold to account for floating point precision
    # If score is within 0.01 of threshold, we accept it
    effective_threshold = SIM_EXACT_PICK_THRESHOLD - 0.01

    for item in results:
        try:
            score = score_fn(item)
            if score is not None and score >= effective_threshold:
                scored_results.append((score, item))
        except FIELD_ACCESS_ERRORS:
            # Skip items that cause scoring errors (missing fields, wrong types, etc.)
            continue

    # Sort by score (descending) and return top N
    scored_results.sort(key=lambda x: x[0], reverse=True)
    return [item for score, item in scored_results[:max_results]]


def build_bibtex_from_response(
    response: Dict[str, Any],
    keyhint: str,
    mapping: APIFieldMapping
) -> Optional[str]:
    """
    Build a BibTeX entry from an API response using configured field mappings to handle
    diverse field naming conventions and data structures across different academic APIs.
    """
    from .bibtex_build import build_bibtex_entry, determine_entry_type

    # Extract title (try all configured fields)
    title = None
    for field_name in mapping.title_fields:
        title = safe_get_field(response, field_name, check_placeholder=True)
        if title:
            break
    if not title:
        return None

    # Extract authors using custom extractor or helper
    if mapping.custom_author_extractor:
        authors = mapping.custom_author_extractor(response)
    else:
        author_data = None
        for field_name in mapping.author_fields:
            author_data = response.get(field_name)
            if author_data:
                break

        authors = extract_author_names(
            author_data,
            name_key=mapping.author_name_key,
            given_key=mapping.author_given_key,
            family_key=mapping.author_family_key
        )

    if not authors or has_placeholder(", ".join(authors)):
        return None

    # Extract year using custom extractor or helper
    if mapping.custom_year_extractor:
        year = mapping.custom_year_extractor(response)
    else:
        year = extract_year_from_any(response, field_names=mapping.year_fields, fallback=0) or 0

    # Determine entry type
    entry_type = determine_entry_type(
        response,
        type_field=mapping.entry_type_field,
        publication_types_field=mapping.entry_type_list_field,
        venue_hints=mapping.venue_hints
    )

    # Extract venue
    venue = None
    for field_name in mapping.venue_fields:
        venue = safe_get_field(response, field_name)
        if venue:
            break

    # Extract identifiers
    doi = None
    for field_name in mapping.doi_fields:
        doi_candidate = safe_get_field(response, field_name)
        if doi_candidate:
            doi = find_doi_in_text(doi_candidate)
            if doi:
                break

    url = None
    for field_name in mapping.url_fields:
        url = safe_get_field(response, field_name)
        if url:
            break

    arxiv_id = None
    for field_name in mapping.arxiv_fields:
        arxiv_candidate = safe_get_field(response, field_name)
        if arxiv_candidate:
            arxiv_id = find_arxiv_in_text(arxiv_candidate)
            if arxiv_id:
                break

    # Build extra fields
    extra_fields = {}
    for source_field, bibtex_field in mapping.extra_field_mappings.items():
        value = safe_get_field(response, source_field)
        if value:
            extra_fields[bibtex_field] = value

    # Build entry
    return build_bibtex_entry(
        entry_type=entry_type,
        title=title,
        authors=authors,
        year=year,
        keyhint=keyhint,
        venue=venue,
        doi=doi,
        url=url,
        arxiv_id=arxiv_id,
        extra_fields=extra_fields
    )
