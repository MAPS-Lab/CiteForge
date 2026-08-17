"""Provider JSON envelope contracts shared by legacy and durable refresh callers."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from ..config import HTTP_MAX_RETRIES
from .capabilities import capability_by_id
from .decoders import decode_response
from .ledger import RequestSpec, TaskClaim
from .transport import (
    ProviderTransport,
    RawProviderResponse,
    SchemaChangedError,
    SendOperation,
    consume_response,
)

Normalizer = Callable[[dict[str, object]], Mapping[str, object]]
EmptyCheck = Callable[[dict[str, object]], bool]


def _exact_builder_payload(capability_id: str, payload: Mapping[str, object], url: str) -> Mapping[str, object]:
    query = {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}
    author_key = payload.get("author_key", payload.get("author_scope"))
    if capability_id.startswith("s2."):
        return {
            "author_key": author_key,
            "query": query.get("query", payload.get("query", "")),
            "limit": int(query.get("limit", 15)),
        }
    if capability_id.startswith("crossref."):
        result: dict[str, object] = {
            "author_key": author_key,
            "query": query.get("query.title", query.get("query.bibliographic", payload.get("query", ""))),
            "author": query.get("query.author"),
            "rows": int(query.get("rows", 20)),
        }
        if capability_id == "crossref.venue_search.v1":
            result["venue"] = query.get("query.container-title", payload.get("venue", ""))
        return result
    if capability_id.startswith("openalex."):
        result = {
            "author_key": author_key,
            "query": query.get("search", payload.get("query", "")),
            "per_page": int(query.get("per-page", 20)),
        }
        if capability_id == "openalex.venue_search.v1":
            prefix = "primary_location.source.display_name.search:"
            result["venue"] = query.get("filter", "").removeprefix(prefix)
        return result
    if capability_id == "europepmc.fuzzy_search.v1":
        return {
            "author_key": author_key,
            "query": query.get("query", payload.get("query", "")),
            "page_size": int(query.get("pageSize", 20)),
        }
    if capability_id == "serply.scholar_search.v1":
        return {"author_key": author_key, "query": payload["query"], "start": payload.get("start", 0)}
    if capability_id == "pubmed.title_search.v1":
        return {
            "author_key": author_key,
            "query": payload["query"],
            "retmax": payload.get("retmax", int(query.get("retmax", 5))),
        }
    if capability_id == "pubmed.summary.v1":
        requested = payload.get("requested_pmids")
        return {"requested_pmids": requested if requested is not None else (payload.get("pmid"),)}
    if capability_id.startswith("openreview."):
        return (
            {"author_key": author_key, "term": payload["term"], "limit": int(query.get("limit", 20))}
            if "term" in payload
            else {"author_key": author_key, "query": payload["query"]}
        )
    if capability_id == "gemini.short_title.v1":
        return {
            "title": payload.get("prompt_digest_input", payload.get("title")),
            "max_words": payload["max_words"],
            "prompt_version": payload["prompt_version"],
            "model_id": payload["model_id"],
            "generation_config": payload["generation_config"],
        }
    if capability_id.startswith("doi_csl."):
        return {"doi": payload["doi"]}
    return payload


class DurableJsonRouter(Protocol):
    """Engine-owned claimed routing for one durable JSON operation."""

    def send(self, adapter_name: str, operation: SendOperation) -> Mapping[str, object]: ...


def route_json(
    router: DurableJsonRouter,
    adapter_name: str,
    *,
    url: str,
    normalized_payload: Mapping[str, object],
    freshness_epoch: str,
    adapter_version: str,
    timeout: float,
    headers: Mapping[str, str] | None = None,
    json_payload: Mapping[str, object] | None = None,
    idempotent: bool | None = None,
) -> Mapping[str, object]:
    """Legacy URL-injecting compatibility route pending builder execution in Task 5C.2."""
    adapter = JSON_ADAPTERS[adapter_name]
    operation = adapter.build_operation(
        url=url,
        normalized_payload=normalized_payload,
        freshness_epoch=freshness_epoch,
        adapter_version=adapter_version,
        timeout=timeout,
        headers=headers,
        json_payload=json_payload,
        idempotent=idempotent,
    )
    return router.send(adapter_name, operation)


def _path(value: object, *components: str) -> object:
    current = value
    for component in components:
        if not isinstance(current, dict) or component not in current:
            raise SchemaChangedError(f"missing provider envelope {'.'.join(components)}")
        current = current[component]
    return current


def _list(name: str, *components: str) -> Normalizer:
    def normalize(value: dict[str, object]) -> Mapping[str, object]:
        result = _path(value, *components)
        if not isinstance(result, list):
            raise SchemaChangedError(f"provider envelope {'.'.join(components)} is not a list")
        return {name: result}

    return normalize


def _mapping(name: str, *components: str) -> Normalizer:
    def normalize(value: dict[str, object]) -> Mapping[str, object]:
        result = _path(value, *components)
        if not isinstance(result, dict):
            raise SchemaChangedError(f"provider envelope {'.'.join(components)} is not an object")
        return {name: result}

    return normalize


@dataclass(frozen=True)
class JsonProviderAdapter:
    """One JSON operation's versioned envelope and empty semantics."""

    provider: str
    operation: str
    requested_field: str
    normalize: Normalizer
    authoritative_empty: EmptyCheck
    method: str = "GET"
    quota_scope: str | None = None
    capability_id: str = ""
    decoder_context: Mapping[str, object] | None = None
    decoder_context_factory: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None

    def __post_init__(self) -> None:
        if not self.capability_id:
            return
        capability = capability_by_id(self.capability_id)
        if self.method != capability.method or self.requested_field not in capability.requested_fields:
            raise RuntimeError("legacy JSON adapter conflicts with durable capability authority")

    @property
    def decoder_id(self) -> str:
        if not self.capability_id:
            return "legacy.dblp.author_search.decoder"
        return self.capability_id + ".decoder"

    def build_operation(
        self,
        *,
        url: str,
        normalized_payload: Mapping[str, object],
        freshness_epoch: str,
        adapter_version: str,
        quota_scope: str | None = None,
        timeout: float,
        headers: Mapping[str, str] | None = None,
        json_payload: Mapping[str, object] | None = None,
        idempotent: bool | None = None,
        idempotency_key: str | None = None,
        idempotency_header: str | None = None,
    ) -> SendOperation:
        capability = capability_by_id(self.capability_id) if self.capability_id else None
        if capability is not None:
            if adapter_version != capability.adapter_version:
                raise ValueError("adapter version does not match durable capability")
            if quota_scope is not None and quota_scope != capability.quota_scope:
                raise ValueError("quota scope does not match durable capability")
            if idempotent is not None and idempotent != capability.idempotent:
                raise ValueError("idempotency policy does not match durable capability")
            provider = capability.logical_source
            operation = capability.operation
            method = capability.method
            exact_quota = capability.quota_scope
        else:
            provider = self.provider
            operation = self.operation
            method = self.method
            exact_quota = quota_scope or self.quota_scope or self.provider
        exact_payload = (
            _exact_builder_payload(capability.capability_id, normalized_payload, url)
            if capability is not None
            else normalized_payload
        )
        request = RequestSpec(
            provider,
            operation,
            method,
            exact_payload,
            (self.requested_field,),
            adapter_version,
            freshness_epoch,
            exact_quota,
        )
        return SendOperation(
            request=request,
            url=url,
            timeout=timeout,
            validator=self.normalize,
            empty_validator=self.authoritative_empty,
            headers=headers,
            json_payload=json_payload,
            idempotent=capability.idempotent if capability is not None else idempotent,
            idempotency_key=idempotency_key,
            idempotency_header=idempotency_header,
            max_attempts=capability.max_attempts if capability is not None else HTTP_MAX_RETRIES + 1,
            response_decoder=(
                (
                    lambda raw: decode_response(
                        capability.decoder_id,
                        raw,
                        self.decoder_context_factory(exact_payload)
                        if self.decoder_context_factory is not None
                        else self.decoder_context,
                    )
                )
                if capability is not None
                else None
            ),
            decoder_schema=capability.decoder_schema if capability is not None else None,
            max_body_bytes=capability.body_limit if capability is not None else 2_000_000,
            capability_id=capability.capability_id if capability is not None else None,
        )

    def send(
        self, transport: ProviderTransport, operation: SendOperation, *, task_claim: TaskClaim | None = None
    ) -> Mapping[str, object]:
        return consume_response(transport.send(operation, task_claim=task_claim))


def _normalized_empty(normalize: Normalizer, field: str) -> EmptyCheck:
    return lambda value: not normalize(value)[field]


def _registry_normalizer(
    capability_id: str, *, decoder_context: Mapping[str, object] = MappingProxyType({})
) -> Normalizer:
    def normalize(value: dict[str, object]) -> Mapping[str, object]:
        normalized, _empty = decode_response(
            capability_id + ".decoder",
            RawProviderResponse(
                json.dumps(value, allow_nan=False, separators=(",", ":")).encode(),
                "application/json",
                "https://compatibility.invalid/normalized-envelope",
                {},
            ),
            decoder_context,
        )
        return normalized

    return normalize


_S2_SEARCH = _registry_normalizer("s2.fuzzy_search.v1")
_CROSSREF_SEARCH = _registry_normalizer("crossref.fuzzy_search.v1")
_OPENALEX_SEARCH = _registry_normalizer("openalex.fuzzy_search.v1")
_EUROPEPMC_SEARCH = _registry_normalizer("europepmc.fuzzy_search.v1")
_SERPLY = _registry_normalizer("serply.scholar_search.v1")


def _serpapi(value: dict[str, object]) -> Mapping[str, object]:
    parameters = value.get("search_parameters")
    if not isinstance(parameters, dict) or not isinstance(parameters.get("author_id"), str):
        raise SchemaChangedError("Scholar compatibility response lacks exact request evidence")
    offset = parameters.get("start", parameters.get("cstart", 0))
    normalized, _empty = decode_response(
        "scholar.inventory.v1.decoder",
        RawProviderResponse(
            json.dumps(value, allow_nan=False, separators=(",", ":")).encode(),
            "application/json",
            "https://serpapi.com/search",
            {},
        ),
        {
            "profile_id": parameters["author_id"],
            "offset": offset,
            "page_size": 100,
            "min_year": 0,
        },
    )
    return normalized


_SERPAPI = _serpapi
_DBLP = _list("hits", "result", "hits", "hit")
_PUBMED_SEARCH = _registry_normalizer("pubmed.title_search.v1", decoder_context=MappingProxyType({"retmax": 5}))
_OPENREVIEW = _registry_normalizer("openreview.term_search.v1")
_GEMINI = _registry_normalizer("gemini.short_title.v1")

JSON_ADAPTERS: Mapping[str, JsonProviderAdapter] = MappingProxyType(
    {
        "semantic_scholar.search": JsonProviderAdapter(
            "s2",
            "fuzzy_search",
            "results",
            _S2_SEARCH,
            _normalized_empty(_S2_SEARCH, "results"),
            capability_id="s2.fuzzy_search.v1",
        ),
        "crossref.search": JsonProviderAdapter(
            "crossref",
            "fuzzy_search",
            "results",
            _CROSSREF_SEARCH,
            _normalized_empty(_CROSSREF_SEARCH, "results"),
            capability_id="crossref.fuzzy_search.v1",
        ),
        "openalex.search": JsonProviderAdapter(
            "openalex",
            "fuzzy_search",
            "results",
            _OPENALEX_SEARCH,
            _normalized_empty(_OPENALEX_SEARCH, "results"),
            capability_id="openalex.fuzzy_search.v1",
        ),
        "europepmc.search": JsonProviderAdapter(
            "europepmc",
            "fuzzy_search",
            "results",
            _EUROPEPMC_SEARCH,
            _normalized_empty(_EUROPEPMC_SEARCH, "results"),
            capability_id="europepmc.fuzzy_search.v1",
        ),
        "serply.scholar": JsonProviderAdapter(
            "serply",
            "scholar_search",
            "articles",
            _SERPLY,
            _normalized_empty(_SERPLY, "articles"),
            capability_id="serply.scholar_search.v1",
        ),
        "serpapi.author": JsonProviderAdapter(
            "serpapi",
            "author_inventory",
            "articles",
            _SERPAPI,
            _normalized_empty(_SERPAPI, "articles"),
            capability_id="scholar.inventory.v1",
            decoder_context_factory=lambda payload: MappingProxyType(
                {
                    "profile_id": payload["profile_id"],
                    "offset": payload["start"],
                    "page_size": payload["num"],
                    "min_year": payload["min_year"],
                }
            ),
        ),
        "dblp.author_search": JsonProviderAdapter(
            "dblp", "author_search", "hits", _DBLP, _normalized_empty(_DBLP, "hits")
        ),
        "pubmed.search": JsonProviderAdapter(
            "pubmed",
            "search",
            "pmids",
            _PUBMED_SEARCH,
            _normalized_empty(_PUBMED_SEARCH, "pmids"),
            capability_id="pubmed.title_search.v1",
        ),
        "openreview.notes": JsonProviderAdapter(
            "openreview",
            "notes_search",
            "notes",
            _OPENREVIEW,
            _normalized_empty(_OPENREVIEW, "notes"),
            capability_id="openreview.term_search.v1",
        ),
        "openreview.term": JsonProviderAdapter(
            "openreview",
            "term_search",
            "notes",
            _OPENREVIEW,
            _normalized_empty(_OPENREVIEW, "notes"),
            capability_id="openreview.term_search.v1",
        ),
        "openreview.fallback": JsonProviderAdapter(
            "openreview",
            "fallback_search",
            "notes",
            _registry_normalizer("openreview.fallback_search.v1"),
            _normalized_empty(_registry_normalizer("openreview.fallback_search.v1"), "notes"),
            capability_id="openreview.fallback_search.v1",
        ),
        "crossref.venue": JsonProviderAdapter(
            "crossref",
            "venue_search",
            "results",
            _registry_normalizer("crossref.venue_search.v1"),
            _normalized_empty(_registry_normalizer("crossref.venue_search.v1"), "results"),
            capability_id="crossref.venue_search.v1",
        ),
        "openalex.venue": JsonProviderAdapter(
            "openalex",
            "venue_search",
            "results",
            _registry_normalizer("openalex.venue_search.v1"),
            _normalized_empty(_registry_normalizer("openalex.venue_search.v1"), "results"),
            capability_id="openalex.venue_search.v1",
        ),
        "gemini.short_title": JsonProviderAdapter(
            "gemini",
            "short_title",
            "candidates",
            _GEMINI,
            _normalized_empty(_GEMINI, "candidates"),
            "POST",
            capability_id="gemini.short_title.v1",
        ),
        "doi.csl": JsonProviderAdapter(
            "doi_csl",
            "csl_lookup",
            "metadata",
            _registry_normalizer("doi_csl.csl_lookup.v1"),
            lambda _value: False,
            quota_scope="doi",
            capability_id="doi_csl.csl_lookup.v1",
            decoder_context_factory=lambda payload: MappingProxyType({"doi": payload["doi"]}),
        ),
    }
)

JSON_DURABLE_CALLSITES: Mapping[str, str] = {
    "api_generics.crossref": "crossref.search",
    "api_generics.crossref_venue": "crossref.venue",
    "api_generics.europepmc": "europepmc.search",
    "api_generics.openalex": "openalex.search",
    "api_generics.openalex_venue": "openalex.venue",
    "api_generics.semantic_scholar": "semantic_scholar.search",
    "search_apis.dblp_find_author_pid": "dblp.author_search",
    "search_apis.fetch_csl_via_doi": "doi.csl",
    "search_apis.openreview_term": "openreview.term",
    "search_apis.openreview_fallback": "openreview.fallback",
    "search_apis.pubmed_search": "pubmed.search",
    "search_apis.pubmed_summary": "pubmed.summary.singleton",
    "serpapi_scholar._serpapi_get": "serpapi.author",
    "serply_scholar._serply_get": "serply.scholar",
    "utility_apis.gemini_generate_short_title": "gemini.short_title",
}


def pubmed_summary_adapter(requested_pmids: tuple[str, ...]) -> JsonProviderAdapter:
    """Build the fail-closed ESummary adapter for one exact PMID set."""

    def normalize(value: dict[str, object]) -> Mapping[str, object]:
        normalized, _empty = decode_response(
            "pubmed.summary.v1.decoder",
            RawProviderResponse(
                json.dumps(value, allow_nan=False, separators=(",", ":")).encode(),
                "application/json",
                "https://compatibility.invalid/normalized-envelope",
                {},
            ),
            {"requested_pmids": requested_pmids},
        )
        return normalized

    return JsonProviderAdapter(
        "pubmed",
        "summary",
        "records",
        normalize,
        lambda _value: False,
        capability_id="pubmed.summary.v1",
        decoder_context=MappingProxyType({"requested_pmids": requested_pmids}),
    )


__all__ = [
    "JSON_ADAPTERS",
    "JSON_DURABLE_CALLSITES",
    "DurableJsonRouter",
    "JsonProviderAdapter",
    "pubmed_summary_adapter",
    "route_json",
]
