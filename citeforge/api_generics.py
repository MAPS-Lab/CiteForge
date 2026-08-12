"""Config-driven, source-agnostic search and BibTeX construction.

Provides the generic search-and-build engine (`APISearchConfig`,
`APIFieldMapping`, the generic search routine, and the build-from-response
converter) that `api_configs.py` parameterizes for each individual API, so every
source shares one matching and construction path.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .cache import response_cache
from .clients.helpers import title_author_cache_key
from .config import (
    CACHE_TTL_SEARCH_DAYS,
    GENERIC_SERIES_NAMES,
    REDACT_QUERY_PARAM_NAMES,
    SIM_BEST_ITEM_THRESHOLD,
    SIM_EXACT_PICK_THRESHOLD,
    SIM_THRESHOLD_TOLERANCE,
)
from .exceptions import ALL_API_ERRORS, FIELD_ACCESS_ERRORS
from .http_utils import http_get_json, s2_http_get_json
from .id_utils import find_arxiv_in_text, find_doi_in_text
from .log_utils import LogCategory, logger
from .refresh.ledger import TaskClaim
from .refresh.provider_adapters import JSON_ADAPTERS
from .refresh.transport import ProviderTransport, SendOperation, consume_response
from .text_utils import (
    build_url,
    extract_author_names,
    extract_year_from_any,
    has_placeholder,
    safe_get_field,
)
from .venue import first_non_generic_container


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
    # Handle list values (common in API responses)
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    s = str(value).strip()
    if not s or (check_placeholder and has_placeholder(s)):
        return None
    return s


@dataclass
class APISearchConfig:
    """Per-API search settings (endpoint, query parameters, response paths, extractors)."""

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


class ProviderSchemaError(ValueError):
    """A provider response no longer satisfies its configured result envelope."""


_TRANSPORT_PROVIDER = {"semantic_scholar": "s2", "crossref_venue": "crossref", "openalex_venue": "openalex"}
_TRANSPORT_ADAPTER = {
    "semantic_scholar": "semantic_scholar.search",
    "europepmc": "europepmc.search",
    "crossref": "crossref.search",
    "crossref_venue": "crossref.venue",
    "openalex": "openalex.search",
    "openalex_venue": "openalex.venue",
}
_SECRET_QUERY_NAMES = {item.casefold() for item in REDACT_QUERY_PARAM_NAMES}


def _semantic_url_identity(url: str) -> dict[str, object]:
    """Return ordered non-secret URL semantics for durable request identity."""
    parsed = urlsplit(url)
    query: dict[str, list[str]] = {}
    for name, value in sorted(parse_qsl(parsed.query, keep_blank_values=True)):
        if name.casefold() in _SECRET_QUERY_NAMES or name.casefold() == "mailto":
            continue
        query.setdefault(name, []).append(value)
    return {"host": parsed.hostname or "", "path": parsed.path, "query": query}


def _mutable_json(value: object) -> object:
    """Copy an immutable transport payload into legacy adapter-owned JSON values."""
    if isinstance(value, Mapping):
        return {str(key): _mutable_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_mutable_json(item) for item in value]
    return value


def validate_result_envelope(data: dict[str, Any], config: APISearchConfig) -> list[Any]:
    """Return the configured result list, rejecting missing or wrong-shaped envelopes."""
    current: object = data
    for component in config.result_path:
        if not isinstance(current, dict) or component not in current:
            raise ProviderSchemaError(f"{config.api_name} response is missing {'.'.join(config.result_path)}")
        current = current[component]
    if not isinstance(current, list):
        raise ProviderSchemaError(f"{config.api_name} result envelope is not a list")
    return current


def search_operation(
    url: str,
    config: APISearchConfig,
    *,
    author_scope: str,
    freshness_epoch: str,
    adapter_version: str,
    api_key: str | None = None,
) -> SendOperation:
    """Build the canonical fuzzy-search operation without persisting credentials."""
    headers = {"x-api-key": api_key} if api_key and config.requires_api_key else None
    try:
        adapter = JSON_ADAPTERS[_TRANSPORT_ADAPTER[config.api_name]]
    except KeyError as exc:
        raise ValueError(f"no durable JSON adapter for {config.api_name}") from exc
    return adapter.build_operation(
        url=url,
        normalized_payload={"author_scope": author_scope, "request": _semantic_url_identity(url)},
        freshness_epoch=freshness_epoch,
        adapter_version=adapter_version,
        quota_scope=_TRANSPORT_PROVIDER.get(config.api_name, config.api_name),
        timeout=config.timeout,
        headers=headers,
    )


@dataclass
class APIFieldMapping:
    """Per-API field mappings used when building BibTeX entries from a response."""

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
    year_hint: int | None = None,
) -> Callable[[Any], float]:
    """Build getter functions from an APISearchConfig and return a scoring function.

    Resolves custom getters (title_getter, authors_getter, year_getter) from the
    config, falling back to default field-based accessors, and composes them into
    a single scoring function via ``create_scoring_function``. When ``year_hint``
    is supplied, a candidate whose year agrees with it earns the year bonus, which
    lets a same-year, same-author, near-identical-title published record clear the
    accept threshold even when a trivial title-word difference lowers the raw title
    similarity. It never admits a title/author mismatch: the title-minimum and
    author gates run first and short-circuit to zero before year is considered.
    """
    from .bibtex_build import create_scoring_function

    title_getter: Callable[[dict[str, Any]], str] = config.title_getter or (
        lambda c: safe_get_field(c, config.title_field) or ""
    )
    authors_getter: Callable[[dict[str, Any]], Any] = config.authors_getter or (
        lambda c: c.get(config.author_field) or []
    )
    year_getter: Callable[[dict[str, Any]], int | None] = config.year_getter or (lambda c: c.get("year"))

    return create_scoring_function(
        title=title,
        author_name=author_name,
        year_hint=year_hint,
        title_getter=title_getter,
        authors_getter=authors_getter,
        year_getter=year_getter,
    )


def _fetch_results(
    title: str,
    author_name: str | None,
    config: APISearchConfig,
    api_key: str | None,
    cache_key: str,
    *,
    params: dict[str, Any] | None = None,
    transport: ProviderTransport | None = None,
    task_claim: TaskClaim | None = None,
    author_key: str | None = None,
    freshness_epoch: str = "legacy",
    adapter_version: str = "1",
) -> list[Any] | None:
    """Issue the configured search request and extract the raw result list.

    Returns ``None`` both on HTTP failure (not cached, so transient errors are
    retried on the next run) and on an empty result set (recorded as a negative
    cache entry under *cache_key* before returning).
    """
    if params is None:
        params = {config.query_param_name: title, **config.additional_params}
        if author_name and config.author_param_name:
            params[config.author_param_name] = author_name

    url = build_url(config.base_url, params)
    logger.debug(f"{config.api_name} | HTTP_REQUEST | url={url[:80]}", category=LogCategory.SCORE)

    if transport is not None:
        if task_claim is None or not author_key:
            raise ValueError("durable provider transport requires task claim and stable author key")
        normalized = consume_response(
            transport.send(
                search_operation(
                    url,
                    config,
                    author_scope=author_key,
                    freshness_epoch=freshness_epoch,
                    adapter_version=adapter_version,
                    api_key=api_key,
                ),
                task_claim=task_claim,
            )
        )
        mutable_results = _mutable_json(normalized.get("results", []))
        if not isinstance(mutable_results, Sequence) or isinstance(mutable_results, (str, bytes)):
            raise ProviderSchemaError(f"{config.api_name} normalized results are not a sequence")
        results = list(mutable_results)
    else:
        try:
            if api_key and config.requires_api_key:
                data = s2_http_get_json(url, api_key, timeout=config.timeout)
            else:
                data = http_get_json(url, timeout=config.timeout)
        except ALL_API_ERRORS:
            return None
        results = validate_result_envelope(data, config)
    if not results:
        if transport is None:
            response_cache.put_negative(config.api_name, cache_key)
        return None
    return results


def _candidate_query_params(
    title: str,
    author_name: str | None,
    config: APISearchConfig,
    max_results: int,
    venue: str | None = None,
) -> dict[str, Any]:
    """Build the four supported source query shapes without mutating *config*."""
    params = dict(config.additional_params)
    if config.api_name == "semantic_scholar":
        params["query"] = " ".join([f'"{title}"', *([author_name] if author_name else [])])
        params["limit"] = min(max_results * 2, 20)
    elif config.api_name == "europepmc":
        safe_title = title.replace('"', "")
        query = f'TITLE:"{safe_title}"'
        params["query"] = query + (f' AND AUTH:"{author_name}"' if author_name else "")
        params["pageSize"] = max_results
    elif config.api_name == "crossref_venue":
        if not venue:
            return {}
        params.update({"query.container-title": venue, "query.bibliographic": title, "rows": max(max_results, 10)})
        if author_name:
            params["query.author"] = author_name
        mailto = os.getenv("CROSSREF_MAILTO")
        if mailto:
            params["mailto"] = mailto
    elif config.api_name == "openalex_venue":
        if not venue:
            return {}
        params.update(
            {
                "search": title,
                "filter": f"primary_location.source.display_name.search:{venue}",
                "per-page": max(max_results, 10),
            }
        )
    elif config.api_name == "crossref":
        if author_name:
            params.update({"query.title": title, "query.author": author_name})
        else:
            params["query.bibliographic"] = title
        mailto = os.getenv("CROSSREF_MAILTO")
        if mailto:
            params["mailto"] = mailto
    else:
        params[config.query_param_name] = title
        if author_name and config.author_param_name:
            params[config.author_param_name] = author_name
    return params


def search_api_generic_multiple(
    title: str,
    author_name: str | None,
    config: APISearchConfig,
    api_key: str | None = None,
    max_results: int = 5,
    year_hint: int | None = None,
    *,
    venue: str | None = None,
    transport: ProviderTransport | None = None,
    task_claim: TaskClaim | None = None,
    author_key: str | None = None,
    freshness_epoch: str = "legacy",
    adapter_version: str = "1",
) -> list[dict[str, Any]]:
    """Search one configured API and return candidates in its declared order.

    Scored sources retain every candidate above their configured threshold.
    API-ordered sources preserve their source ranking. Both return at most
    *max_results* raw records for later pipeline admission.
    """
    if not title or (config.api_name == "semantic_scholar" and not api_key):
        return []

    if config.api_name.endswith("_venue") and not venue:
        return []
    cache_key = (
        f"venue|{title_author_cache_key(title, author_name)}|{(venue or '').lower().strip()}"
        if config.api_name.endswith("_venue")
        else title_author_cache_key(title, author_name, prefix="multi|")
    )
    cached = response_cache.get(config.api_name, cache_key) if transport is None else None
    if cached is not None:
        if cached.get("_negative"):
            logger.debug(f"{config.api_name}_multi | NEG_HIT | key={cache_key[:60]}", category=LogCategory.CACHE)
            return []
        cached_list: list[dict[str, Any]] = cached.get("results", [])
        logger.debug(f"{config.api_name}_multi | HIT | key={cache_key[:60]}", category=LogCategory.CACHE)
        return [dict(item) for item in cached_list[:max_results]]

    params = _candidate_query_params(title, author_name, config, max_results, venue)
    if not params:
        return []
    results = _fetch_results(
        title,
        author_name,
        config,
        api_key,
        cache_key,
        params=params,
        transport=transport,
        task_claim=task_claim,
        author_key=author_key,
        freshness_epoch=freshness_epoch,
        adapter_version=adapter_version,
    )
    if results is None:
        return []

    if config.api_name in {"semantic_scholar", "europepmc"}:
        top_results = [dict(item) for item in results[:max_results]]
        scored_count = len(results)
    else:
        score_fn = _build_scoring_function(title, author_name, config, year_hint)
        scored_results = []
        effective_threshold = (
            SIM_BEST_ITEM_THRESHOLD
            if config.api_name.endswith("_venue")
            else SIM_EXACT_PICK_THRESHOLD - SIM_THRESHOLD_TOLERANCE
        )

        for item in results:
            try:
                score = score_fn(item)
            except FIELD_ACCESS_ERRORS:
                continue
            accepted = score is not None and score >= effective_threshold
            logger.debug(
                f"{config.api_name} | ITEM_SCORE | score={score:.3f}"
                f" | threshold={effective_threshold:.3f} | accepted={accepted}",
                category=LogCategory.SCORE,
            )
            if accepted:
                scored_results.append((score, item))

        scored_results.sort(key=lambda x: x[0], reverse=True)
        top_results = [dict(item) for _, item in scored_results[:max_results]]
        scored_count = len(scored_results)
    logger.debug(
        f"{config.api_name}_multi | RESULT | scored={scored_count}/{len(results)} | top={len(top_results)}",
        category=LogCategory.SCORE,
    )
    if top_results and transport is None:
        response_cache.put(
            config.api_name,
            cache_key,
            {"results": [dict(r) for r in top_results]},
            ttl_days=CACHE_TTL_SEARCH_DAYS,
        )
    elif transport is None:
        response_cache.put_negative(config.api_name, cache_key)
    return top_results


def _first_resolved_str(
    obj: dict[str, Any],
    field_names: list[str],
    *,
    check_placeholder: bool = False,
) -> str | None:
    """Return the first non-empty string resolved from a list of dotted field paths."""
    for name in field_names:
        value = _resolve_dotted_str(obj, name, check_placeholder=check_placeholder)
        if value:
            return value
    return None


def _first_resolved_with_transform(
    obj: dict[str, Any],
    field_names: list[str],
    transform: Callable[[str], str | None],
) -> str | None:
    """Resolve fields in order, applying *transform* and returning the first truthy result."""
    for name in field_names:
        candidate = _resolve_dotted_str(obj, name)
        if candidate:
            result = transform(candidate)
            if result:
                return result
    return None


def _extract_venue(
    response: dict[str, Any],
    mapping: APIFieldMapping,
) -> str | None:
    """Extract the best venue string from an API response, filtering generic series names."""
    venue: str | None = None
    for field_name in mapping.venue_fields:
        raw_venue = _resolve_dotted(response, field_name)
        # Crossref returns container-title as array: [series_name, conference_name]
        # Prefer the non-generic element over generic series names like LNCS
        if isinstance(raw_venue, list) and len(raw_venue) > 1:
            non_generic = first_non_generic_container(raw_venue)
            venue = non_generic or _resolve_dotted_str(response, field_name)
            logger.debug(
                f"{mapping.api_name} | VENUE_ARRAY | elements={len(raw_venue)}"
                f" | generic_filtered={non_generic is not None} | selected={(venue or '')[:50]}",
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
                    f"{mapping.api_name} | EVENT_NAME | generic_series={venue[:40]} | event={event_name[:40]}",
                    category=LogCategory.SCORE,
                )
                venue = event_name

    return venue


def build_bibtex_from_response(response: dict[str, Any], keyhint: str, mapping: APIFieldMapping) -> str | None:
    """Build a BibTeX entry from an API response using the configured field mappings."""
    from .bibtex_build import build_bibtex_entry, determine_entry_type

    title = _first_resolved_str(response, mapping.title_fields, check_placeholder=True)
    if not title:
        return None

    if mapping.custom_author_extractor:
        authors = mapping.custom_author_extractor(response)
    else:
        author_data = next(
            (v for f in mapping.author_fields if (v := _resolve_dotted(response, f))),
            None,
        )
        authors = extract_author_names(
            author_data,
            name_key=mapping.author_name_key or "name",
            given_key=mapping.author_given_key,
            family_key=mapping.author_family_key,
        )

    if not authors or has_placeholder(", ".join(authors)):
        return None

    if mapping.custom_year_extractor:
        year = mapping.custom_year_extractor(response)
    else:
        year = extract_year_from_any(response, field_names=mapping.year_fields, fallback=0) or 0

    entry_type = determine_entry_type(
        response,
        type_field=mapping.entry_type_field,
        publication_types_field=mapping.entry_type_list_field,
        venue_hints=mapping.venue_hints,
    )

    venue = _extract_venue(response, mapping)
    doi = _first_resolved_with_transform(response, mapping.doi_fields, find_doi_in_text)
    url = _first_resolved_str(response, mapping.url_fields)
    arxiv_id = _first_resolved_with_transform(response, mapping.arxiv_fields, find_arxiv_in_text)

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
        extra_fields=extra_fields,
    )
