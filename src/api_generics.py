from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .cache import response_cache
from .config import CACHE_TTL_SEARCH_DAYS, GENERIC_SERIES_NAMES, SIM_EXACT_PICK_THRESHOLD, SIM_THRESHOLD_TOLERANCE
from .exceptions import ALL_API_ERRORS, FIELD_ACCESS_ERRORS
from .http_utils import http_get_json, s2_http_get_json
from .id_utils import find_arxiv_in_text, find_doi_in_text
from .log_utils import LogCategory, logger
from .text_utils import (
    author_in_text,
    author_name_matches,
    build_url,
    extract_author_names,
    extract_year_from_any,
    has_placeholder,
    normalize_title,
    safe_get_field,
    safe_get_nested,
)


def _resolve_dotted(obj: dict[str, Any], field: str) -> Any:
    """Resolve a dot-notation field path (e.g. ``externalIds.DOI``) against *obj*.

    Falls back to a literal key lookup when there is no dot or when the
    dot-traversal fails, so existing non-dotted field names keep working.
    """
    if "." not in field:
        return obj.get(field)
    parts = field.split(".")
    cur: Any = obj
    for part in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _resolve_dotted_str(obj: dict[str, Any], field: str, *, check_placeholder: bool = False) -> str | None:
    """Like :func:`_resolve_dotted` but coerces the result to ``str``.

    Returns ``None`` when the value is missing, empty, or (optionally) a
    placeholder string.
    """
    value = _resolve_dotted(obj, field)
    if value is None:
        return None
    # Handle list values (common in API responses)
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if check_placeholder and has_placeholder(s):
        return None
    return s


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
    author_param_name: str | None = None
    additional_params: dict[str, Any] = field(default_factory=dict)

    # Response structure
    result_path: list[str] = field(default_factory=lambda: ["results"])
    title_field: str = "title"
    author_field: str = "authors"

    # Customization
    timeout: float = 15.0
    requires_api_key: bool = False

    # Optional custom extractors
    title_getter: Callable[[dict[str, Any]], str] | None = None
    year_getter: Callable[[dict[str, Any]], int | None] | None = None
    authors_getter: Callable[[dict[str, Any]], Any] | None = None


@dataclass
class APIFieldMapping:
    """
    Configuration for API-specific field mappings when building BibTeX entries,
    translating diverse field names and structures to a unified BibTeX format.
    """
    api_name: str

    # Core field mappings (list of possible field names, first match wins)
    title_fields: list[str]
    author_fields: list[str]
    year_fields: list[str]
    venue_fields: list[str]

    # Identifier mappings
    doi_fields: list[str] = field(default_factory=lambda: ["doi"])
    url_fields: list[str] = field(default_factory=lambda: ["url"])
    arxiv_fields: list[str] = field(default_factory=list)
    pmid_fields: list[str] = field(default_factory=list)

    # Extra field mappings (source_field -> bibtex_field)
    extra_field_mappings: dict[str, str] = field(default_factory=dict)

    # Author extraction config
    author_name_key: str | None = "name"
    author_given_key: str | None = None
    author_family_key: str | None = None

    # Entry type config
    entry_type_field: str = "type"
    entry_type_list_field: str | None = None
    venue_hints: dict[str, str] = field(default_factory=dict)

    # Custom extractors for complex cases
    custom_author_extractor: Callable[[dict[str, Any]], list[str]] | None = None
    custom_year_extractor: Callable[[dict[str, Any]], int] | None = None


def _build_scoring_function(
    title: str,
    author_name: str | None,
    config: APISearchConfig,
) -> Callable[[Any], float]:
    """Build getter functions from an APISearchConfig and return a scoring function.

    Resolves custom getters (title_getter, authors_getter, year_getter) from the
    config, falling back to default field-based accessors, and composes them into
    a single scoring function via ``create_scoring_function``.
    """
    from .bibtex_build import create_scoring_function

    title_getter: Callable[[dict[str, Any]], str] = (
        config.title_getter
        or (lambda c: safe_get_field(c, config.title_field) or "")
    )
    authors_getter: Callable[[dict[str, Any]], Any] = (
        config.authors_getter
        or (lambda c: c.get(config.author_field) or [])
    )
    year_getter: Callable[[dict[str, Any]], int | None] = (
        config.year_getter
        or (lambda c: c.get("year"))
    )

    return create_scoring_function(
        title=title,
        author_name=author_name,
        year_hint=None,
        title_getter=title_getter,
        authors_getter=authors_getter,
        year_getter=year_getter,
    )


def search_api_generic(
    title: str,
    author_name: str | None,
    config: APISearchConfig,
    api_key: str | None = None
) -> dict[str, Any] | None:
    """
    Search for academic publications across different API providers using a unified
    interface with a two-pass matching strategy that attempts exact title matches first
    and falls back to fuzzy matching when needed.
    """
    if not title:
        return None

    cache_key = f"{normalize_title(title)}|{(author_name or '').strip().lower()}"
    cached = response_cache.get(config.api_name, cache_key)
    if cached is not None:
        if cached.get("_negative"):
            return None
        logger.debug(f"{config.api_name} | HIT | key={cache_key[:60]}", category=LogCategory.CACHE)
        return cached if cached else None

    params = {config.query_param_name: title, **config.additional_params}
    if author_name and config.author_param_name:
        params[config.author_param_name] = author_name

    url = build_url(config.base_url, params)
    logger.debug(f"{config.api_name} | HTTP_REQUEST | url={url[:80]}", category=LogCategory.SCORE)

    try:
        if api_key and config.api_name == "semantic_scholar":
            data = s2_http_get_json(url, api_key, timeout=config.timeout)
        else:
            data = http_get_json(url, timeout=config.timeout)
    except ALL_API_ERRORS:
        return None

    results = safe_get_nested(data, *config.result_path, default=[])
    if not results:
        response_cache.put(config.api_name, cache_key, {"_negative": True}, ttl_days=CACHE_TTL_SEARCH_DAYS)
        return None

    logger.debug(
        f"{config.api_name} | RESULTS | count={len(results)} | path={config.result_path}",
        category=LogCategory.SCORE,
    )

    # Try exact title match first
    target_norm = normalize_title(title)
    get_title = config.title_getter or (lambda it: safe_get_field(it, config.title_field) or "")
    get_authors = config.authors_getter or (lambda it: it.get(config.author_field))

    for item in results:
        item_title = get_title(item)
        norm_match = normalize_title(item_title) == target_norm
        author_ok = True
        if norm_match and author_name:
            item_authors = get_authors(item)
            author_ok = author_name_matches(author_name, item_authors) or author_in_text(
                author_name, item_authors
            )
        logger.debug(
            f"{config.api_name} | EXACT_CHECK | item_title={item_title[:50]}"
            f" | normalized_match={norm_match} | author_check={bool(author_name)}"
            f" | author_match={author_ok}",
            category=LogCategory.SCORE,
        )
        if not norm_match or not author_ok:
            continue
        logger.debug(
            f"{config.api_name} | EXACT_MATCH | title={title[:50]}",
            category=LogCategory.SCORE,
        )
        result = dict(item)
        response_cache.put(config.api_name, cache_key, result, ttl_days=CACHE_TTL_SEARCH_DAYS)
        return result

    # Fuzzy match using scoring function
    from .clients.helpers import _best_item_by_score

    score_fn = _build_scoring_function(title, author_name, config)
    best = _best_item_by_score(results, score_fn, threshold=SIM_EXACT_PICK_THRESHOLD)

    # _best_item_by_score logs BEST_ITEM internally; here we add API-specific context.
    best_score = max((score_fn(it) for it in results), default=0.0) if results else 0.0
    logger.debug(
        f"{config.api_name} | FUZZY_RESULT | best_score={best_score:.3f}"
        f" | threshold={SIM_EXACT_PICK_THRESHOLD} | accepted={best is not None}",
        category=LogCategory.SCORE,
    )
    if best is not None:
        response_cache.put(config.api_name, cache_key, dict(best), ttl_days=CACHE_TTL_SEARCH_DAYS)
    else:
        response_cache.put(config.api_name, cache_key, {"_negative": True}, ttl_days=CACHE_TTL_SEARCH_DAYS)
    return best


def search_api_generic_multiple(
    title: str,
    author_name: str | None,
    config: APISearchConfig,
    api_key: str | None = None,
    max_results: int = 5
) -> list[dict[str, Any]]:
    """
    Search for academic publications and return multiple candidates sorted by relevance.

    Similar to search_api_generic but returns a list of top candidates instead of just
    the best match, enabling multiple candidates for validation.
    """
    if not title:
        return []

    cache_key = f"multi|{normalize_title(title)}|{(author_name or '').strip().lower()}"
    cached = response_cache.get(config.api_name, cache_key)
    if cached is not None:
        if cached.get("_negative"):
            return []
        cached_list: list[dict[str, Any]] = cached.get("results", [])
        logger.debug(f"{config.api_name}_multi | HIT | key={cache_key[:60]}", category=LogCategory.CACHE)
        return cached_list

    params = {config.query_param_name: title, **config.additional_params}
    if author_name and config.author_param_name:
        params[config.author_param_name] = author_name

    url = build_url(config.base_url, params)
    logger.debug(f"{config.api_name} | HTTP_REQUEST | url={url[:80]}", category=LogCategory.SCORE)

    try:
        if api_key and config.api_name == "semantic_scholar":
            data = s2_http_get_json(url, api_key, timeout=config.timeout)
        else:
            data = http_get_json(url, timeout=config.timeout)
    except ALL_API_ERRORS:
        return []

    results = safe_get_nested(data, *config.result_path, default=[])
    if not results:
        response_cache.put(config.api_name, cache_key, {"_negative": True}, ttl_days=CACHE_TTL_SEARCH_DAYS)
        return []

    score_fn = _build_scoring_function(title, author_name, config)

    scored_results = []
    effective_threshold = SIM_EXACT_PICK_THRESHOLD - SIM_THRESHOLD_TOLERANCE

    for item in results:
        try:
            score = score_fn(item)
            accepted = score is not None and score >= effective_threshold
            logger.debug(
                f"{config.api_name} | ITEM_SCORE | score={score:.3f}"
                f" | threshold={effective_threshold:.3f} | accepted={accepted}",
                category=LogCategory.SCORE,
            )
            if accepted:
                scored_results.append((score, item))
        except FIELD_ACCESS_ERRORS:
            # Skip items that cause scoring errors (missing fields, wrong types, etc.)
            continue

    scored_results.sort(key=lambda x: x[0], reverse=True)
    top_results = [item for _, item in scored_results[:max_results]]
    logger.debug(
        f"{config.api_name}_multi | RESULT | scored={len(scored_results)}/{len(results)} | top={len(top_results)}",
        category=LogCategory.SCORE,
    )
    if top_results:
        cached_results = {"results": [dict(r) for r in top_results]}
        response_cache.put(config.api_name, cache_key, cached_results, ttl_days=CACHE_TTL_SEARCH_DAYS)
    else:
        response_cache.put(config.api_name, cache_key, {"_negative": True}, ttl_days=CACHE_TTL_SEARCH_DAYS)
    return top_results


def build_bibtex_from_response(
    response: dict[str, Any],
    keyhint: str,
    mapping: APIFieldMapping
) -> str | None:
    """
    Build a BibTeX entry from an API response using configured field mappings to handle
    diverse field naming conventions and data structures across different academic APIs.
    """
    from .bibtex_build import build_bibtex_entry, determine_entry_type

    # Extract title (try all configured fields)
    title = None
    for field_name in mapping.title_fields:
        title = _resolve_dotted_str(response, field_name, check_placeholder=True)
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
            author_data = _resolve_dotted(response, field_name)
            if author_data:
                break

        authors = extract_author_names(
            author_data,
            name_key=mapping.author_name_key or "name",
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
        raw_venue = _resolve_dotted(response, field_name)
        # Crossref returns container-title as array: [series_name, conference_name]
        # Prefer the non-generic element (e.g., "Artificial Intelligence in Medicine"
        # over "Lecture Notes in Computer Science")
        if isinstance(raw_venue, list) and len(raw_venue) > 1:
            generic_filtered = False
            for candidate in raw_venue:
                candidate_str = str(candidate).strip()
                if candidate_str and candidate_str.lower() not in GENERIC_SERIES_NAMES:
                    venue = candidate_str
                    generic_filtered = True
                    break
            if not venue:
                venue = _resolve_dotted_str(response, field_name)
            logger.debug(
                f"{mapping.api_name} | VENUE_ARRAY | elements={len(raw_venue)}"
                f" | generic_filtered={generic_filtered} | selected={(venue or '')[:50]}",
                category=LogCategory.SCORE,
            )
        else:
            venue = _resolve_dotted_str(response, field_name)
        if venue:
            break

    # For Crossref: fall back to event name if venue is still generic
    if mapping.api_name == "crossref" and venue and venue.lower().strip() in GENERIC_SERIES_NAMES:
        event = response.get("event")
        if isinstance(event, dict):
            event_name = (event.get("name") or "").strip()
            if event_name:
                logger.debug(
                    f"{mapping.api_name} | EVENT_NAME | generic_series={venue[:40]}"
                    f" | event={event_name[:40]}",
                    category=LogCategory.SCORE,
                )
                venue = event_name

    doi = None
    for field_name in mapping.doi_fields:
        doi_candidate = _resolve_dotted_str(response, field_name)
        if doi_candidate:
            doi = find_doi_in_text(doi_candidate)
            if doi:
                break

    url = None
    for field_name in mapping.url_fields:
        url = _resolve_dotted_str(response, field_name)
        if url:
            break

    arxiv_id = None
    for field_name in mapping.arxiv_fields:
        arxiv_candidate = _resolve_dotted_str(response, field_name)
        if arxiv_candidate:
            arxiv_id = find_arxiv_in_text(arxiv_candidate)
            if arxiv_id:
                break

    extra_fields = {}
    for source_field, bibtex_field in mapping.extra_field_mappings.items():
        value = _resolve_dotted_str(response, source_field)
        if value:
            extra_fields[bibtex_field] = value

    logger.debug(
        f"{mapping.api_name} | BUILD | title={title[:50]} | authors={len(authors)}"
        f" | year={year} | type={entry_type} | venue={(venue or '')[:40]}"
        f" | doi={doi or 'none'} | arxiv={arxiv_id or 'none'}",
        category=LogCategory.SCORE,
    )

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
