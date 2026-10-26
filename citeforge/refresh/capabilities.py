"""Immutable authority for durable publication provider capabilities."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from ..config import (
    ARXIV_BASE,
    CROSSREF_BASE,
    DBLP_PERSON_BASE,
    DOI_BASE,
    EUROPEPMC_BASE,
    GEMINI_BASE,
    HTTP_MAX_RETRIES,
    OPENALEX_BASE,
    OPENREVIEW_BASE,
    PUBMED_BASE,
    S2_BASE,
    S2_SEARCH_FIELDS,
    SERPAPI_BASE,
    SERPLY_BASE,
)


class ResponseMediaType(str, Enum):
    JSON = "json"
    XML = "xml"
    BIBTEX = "bibtex"
    HTML = "html"


class CredentialKind(str, Enum):
    NONE = "none"
    SERPAPI_KEY = "serpapi_key"
    SERPLY_KEY = "serply_key"
    S2_API_KEY = "s2_api_key"
    OPENREVIEW_RUNTIME = "openreview_runtime"
    GEMINI_KEY = "gemini_key"


_CAPABILITY_TOKEN = object()


@dataclass(frozen=True)
class BuilderDefinition:
    callback_id: str
    version: str
    callback: Callable[[Mapping[str, object]], BuiltRequest] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class BuiltRequest:
    capability_id: str
    method: str
    endpoint: str = field(repr=False)
    identity_payload: Mapping[str, object]
    query: Mapping[str, object]
    body: Mapping[str, object] | None = None
    credential_injection: str = "none"
    required_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identity = _safe_payload(self.identity_payload)
        query = _safe_payload(self.query)
        if not isinstance(identity, Mapping) or not isinstance(query, Mapping):
            raise TypeError("built request artifacts must be JSON objects")
        object.__setattr__(self, "identity_payload", identity)
        object.__setattr__(self, "query", query)
        headers = _safe_payload(self.required_headers)
        if not isinstance(headers, Mapping) or not all(isinstance(value, str) for value in headers.values()):
            raise TypeError("built request headers must be a string mapping")
        object.__setattr__(self, "required_headers", headers)
        if self.body is not None:
            body = _safe_payload(self.body)
            if not isinstance(body, Mapping):
                raise TypeError("built request body must be a JSON object")
            object.__setattr__(self, "body", body)


_SECRET_KEY = re.compile(r"(?:^|[_-])(?:authorization|api[_-]?key|cookie|token|secret|password|credential)(?:$|[_-])")
_SECRET_VALUE = re.compile(
    r"(?:authorization|api[_-]?key|cookie|token|secret|password|credential)\s*[:=]",
    re.IGNORECASE,
)


def _safe_payload(value: object, *, key: str = "") -> object:
    if key and _SECRET_KEY.search(key.casefold()):
        raise ValueError("wire secrets cannot enter durable builder identity")
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            raise ValueError("wire secrets cannot enter durable builder identity")
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("durable builder identity requires finite JSON")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(item_key, str) for item_key in value):
            raise TypeError("durable builder identity requires string JSON keys")
        return MappingProxyType({item_key: _safe_payload(item, key=item_key) for item_key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_safe_payload(item) for item in value)
    raise TypeError("durable builder identity requires strict JSON")


_ENDPOINTS = {
    "scholar.inventory.v1": SERPAPI_BASE,
    "dblp.inventory.v1": f"{DBLP_PERSON_BASE}/{{pid}}.xml",
    "doi_csl.csl_lookup.v1": f"{DOI_BASE}/{{doi}}",
    "doi_bibtex.bibtex_lookup.v1": f"{DOI_BASE}/{{doi}}",
    "serply.scholar_search.v1": f"{SERPLY_BASE}/{{encoded_query}}",
    "s2.fuzzy_search.v1": f"{S2_BASE}/paper/search",
    "s2.fuzzy_search.v2": f"{S2_BASE}/paper/search",
    "crossref.fuzzy_search.v1": CROSSREF_BASE,
    "openreview.term_search.v1": f"{OPENREVIEW_BASE}/notes",
    "openreview.fallback_search.v1": f"{OPENREVIEW_BASE}/notes/search",
    "arxiv.fuzzy_search.v1": ARXIV_BASE,
    "openalex.fuzzy_search.v1": OPENALEX_BASE,
    "pubmed.title_search.v1": f"{PUBMED_BASE}/esearch.fcgi",
    "pubmed.summary.v1": f"{PUBMED_BASE}/esummary.fcgi",
    "europepmc.fuzzy_search.v1": f"{EUROPEPMC_BASE}/search",
    "crossref.venue_search.v1": CROSSREF_BASE,
    "openalex.venue_search.v1": OPENALEX_BASE,
    "web.doi_probe.v1": "validated_https_url",
    "gemini.short_title.v1": GEMINI_BASE,
}


_BUILDER_REQUIRED = {
    "scholar.inventory.v1": frozenset({"author_key", "profile_id", "start", "num", "sort", "min_year"}),
    "dblp.inventory.v1": frozenset({"author_key", "pid"}),
    "doi_csl.csl_lookup.v1": frozenset({"doi"}),
    "doi_bibtex.bibtex_lookup.v1": frozenset({"doi"}),
    "serply.scholar_search.v1": frozenset({"author_key", "query", "start"}),
    "s2.fuzzy_search.v1": frozenset({"author_key", "query", "limit"}),
    "s2.fuzzy_search.v2": frozenset({"author_key", "author", "title", "year"}),
    "crossref.fuzzy_search.v1": frozenset({"author_key", "query", "author", "rows"}),
    "openreview.term_search.v1": frozenset({"author_key", "term", "limit"}),
    "openreview.fallback_search.v1": frozenset({"author_key", "query"}),
    "arxiv.fuzzy_search.v1": frozenset({"author_key", "query", "start", "max_results", "sort_by", "sort_order"}),
    "openalex.fuzzy_search.v1": frozenset({"author_key", "query", "per_page"}),
    "pubmed.title_search.v1": frozenset({"author_key", "query", "retmax"}),
    "pubmed.summary.v1": frozenset({"requested_pmids"}),
    "europepmc.fuzzy_search.v1": frozenset({"author_key", "query", "page_size"}),
    "crossref.venue_search.v1": frozenset({"author_key", "query", "venue", "author", "rows"}),
    "openalex.venue_search.v1": frozenset({"author_key", "query", "venue", "per_page"}),
    "web.doi_probe.v1": frozenset({"url"}),
    "gemini.short_title.v1": frozenset({"title", "max_words", "prompt_version", "model_id", "generation_config"}),
}

GEMINI_PROMPT_VERSION = "camelcase-short-title-v1"
GEMINI_MODEL_ID = "gemini-2.5-flash-lite"
GEMINI_GENERATION_CONFIG: Mapping[str, object] = MappingProxyType(
    {"maxOutputTokens": 50, "temperature": 0.3, "topP": 0.8, "topK": 20}
)


def gemini_prompt(title: str, max_words: int) -> str:
    return (
        f"Create a smart, concise CamelCase title (1 to {max_words} words) "
        f'for this publication: "{title}". '
        "Extract the most important keywords. "
        "Skip stop words (a, an, the, for, of, and, to, in, with, from, by, at). "
        f"Use exactly {max_words} words or fewer if shorter captures the essence better. "
        "IMPORTANT: Write as ONE word in CamelCase format with NO spaces between words "
        "(e.g., 'AttentionMechanism' not 'Attention Mechanism'). "
        "Return ONLY the CamelCase title with no quotes, explanation, spaces, or punctuation."
    )


def _validate_builder_values(capability_id: str, payload: Mapping[str, object]) -> None:
    for key, value in payload.items():
        if key == "generation_config":
            if not isinstance(value, Mapping):
                raise ValueError("generation_config must be an exact mapping")
            continue
        if key == "year" and value is None:
            continue
        if key == "author" and value is None:
            continue
        if key in {
            "start",
            "num",
            "min_year",
            "year",
            "max_words",
            "limit",
            "rows",
            "max_results",
            "per_page",
            "retmax",
            "page_size",
        }:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be an exact integer")
        elif key == "requested_pmids":
            if (
                not isinstance(value, (tuple, list))
                or len(value) != 1
                or not isinstance(value[0], str)
                or not value[0].isdigit()
            ):
                raise ValueError("PubMed summary requires one exact PMID")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a nonblank string")
    if capability_id == "scholar.inventory.v1" and (
        payload["sort"] != "pubdate"
        or not 0 <= payload["start"] <= 1_000_000  # type: ignore[operator]
        or not 1 <= payload["num"] <= 100  # type: ignore[operator]
        or not 0 <= payload["min_year"] <= 9999  # type: ignore[operator]
    ):
        raise ValueError("Scholar identity is outside canonical bounds")
    if "max_words" in payload and not 1 <= payload["max_words"] <= 20:  # type: ignore[operator]
        raise ValueError("Gemini max words is outside canonical bounds")
    bounded = {
        "limit": 100,
        "rows": 1000,
        "max_results": 2000,
        "per_page": 200,
        "retmax": 10_000,
        "page_size": 1000,
    }
    for key, maximum in bounded.items():
        if key in payload and not 1 <= payload[key] <= maximum:  # type: ignore[operator]
            raise ValueError(f"{key} is outside provider bounds")
    if "start" in payload and payload["start"] < 0:  # type: ignore[operator]
        raise ValueError("start is outside provider bounds")
    if capability_id == "arxiv.fuzzy_search.v1" and (
        payload["sort_by"] not in {"relevance", "lastUpdatedDate", "submittedDate"}
        or payload["sort_order"] not in {"ascending", "descending"}
    ):
        raise ValueError("arXiv sort policy is invalid")
    if capability_id == "s2.fuzzy_search.v2" and payload["year"] is not None and not 1800 <= payload["year"] <= 2100:  # type: ignore[operator]
        raise ValueError("S2 year is outside project bounds")
    if capability_id == "gemini.short_title.v1" and (
        payload["prompt_version"] != GEMINI_PROMPT_VERSION
        or payload["model_id"] != GEMINI_MODEL_ID
        or payload["generation_config"] != GEMINI_GENERATION_CONFIG
    ):
        raise ValueError("Gemini wire policy identity is not canonical")


def _builder_callback(capability_id: str, method: str, endpoint: str) -> Callable[[Mapping[str, object]], BuiltRequest]:
    def build(payload: Mapping[str, object]) -> BuiltRequest:
        built_endpoint = endpoint
        frozen = _safe_payload(payload)
        required = _BUILDER_REQUIRED[capability_id]
        if set(payload) != required:
            raise ValueError("durable builder payload does not match exact operation schema")
        _validate_builder_values(capability_id, payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("durable builder identity must be a mapping")
        query: Mapping[str, object] = frozen
        body: Mapping[str, object] | None = None
        credential = "none"
        required_headers: Mapping[str, str] = MappingProxyType({})
        if capability_id == "scholar.inventory.v1":
            query = MappingProxyType(
                {
                    "engine": "google_scholar_author",
                    "author_id": frozen["profile_id"],
                    "start": frozen["start"],
                    "num": frozen["num"],
                    "sort": frozen["sort"],
                }
            )
            credential = "query:api_key"
        elif capability_id == "dblp.inventory.v1":
            built_endpoint = f"{DBLP_PERSON_BASE}/{quote(str(frozen['pid']), safe='/')}.xml"
            query = MappingProxyType({})
        elif capability_id.startswith("doi_"):
            built_endpoint = f"{DOI_BASE}/{quote(str(frozen['doi']), safe='/')}"
            query = MappingProxyType({})
            required_headers = MappingProxyType(
                {
                    "Accept": (
                        "application/vnd.citationstyles.csl+json"
                        if capability_id == "doi_csl.csl_lookup.v1"
                        else "application/x-bibtex"
                    )
                }
            )
        elif capability_id == "serply.scholar_search.v1":
            built_endpoint = f"{SERPLY_BASE}/{quote(str(frozen['query']), safe='')}"
            query = MappingProxyType({"start": frozen["start"]} if frozen["start"] else {})
            credential = "header:X-API-KEY"
            required_headers = MappingProxyType(
                {
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "X-Proxy-Location": "US",
                }
            )
        elif capability_id.startswith("s2."):
            search_text = frozen["query"] if "query" in frozen else f'"{frozen["title"]}" {frozen["author"]}'
            query = MappingProxyType(
                {
                    "query": search_text,
                    "limit": frozen.get("limit", 15),
                    "fields": S2_SEARCH_FIELDS,
                }
            )
            credential = "header:x-api-key"
        elif capability_id.startswith("crossref."):
            bibliographic_key: str = (
                "query.title"
                if capability_id == "crossref.fuzzy_search.v1" and frozen.get("author")
                else "query.bibliographic"
            )
            # Built as one annotated dict rather than a chain of `|` unions.
            # Merging dict literals whose keys are inferred separately widens
            # the key type, and the result stops satisfying Mapping[str, object].
            crossref_query: dict[str, object] = {
                bibliographic_key: frozen["query"],
                "rows": frozen["rows"],
                "select": (
                    "title,author,issued,container-title,type,URL,DOI,published-print,published-online,"
                    "publisher,volume,issue,page"
                ),
            }
            if frozen.get("author"):
                crossref_query["query.author"] = frozen["author"]
            if "venue" in frozen:
                crossref_query["query.container-title"] = frozen["venue"]
            query = MappingProxyType(crossref_query)
            credential = "query:mailto_if_configured"
        elif capability_id.startswith("openreview."):
            query = MappingProxyType(
                {"term": frozen["term"], "details": "metadata", "limit": frozen["limit"]}
                if "term" in frozen
                else {"query": frozen["query"], "limit": 20}
            )
            credential = "cookie:runtime_session_if_selected"
        elif capability_id == "arxiv.fuzzy_search.v1":
            query = MappingProxyType(
                {
                    "search_query": frozen["query"],
                    "start": frozen["start"],
                    "max_results": frozen["max_results"],
                    "sortBy": frozen["sort_by"],
                    "sortOrder": frozen["sort_order"],
                }
            )
        elif capability_id.startswith("openalex."):
            query = MappingProxyType(
                {"search": frozen["query"], "per-page": frozen["per_page"]}
                | (
                    {"filter": f"primary_location.source.display_name.search:{frozen['venue']}"}
                    if "venue" in frozen
                    else {}
                )
            )
            credential = "query:mailto_if_configured"
        elif capability_id == "pubmed.title_search.v1":
            query = MappingProxyType(
                {"db": "pubmed", "term": frozen["query"], "retmax": frozen["retmax"], "retmode": "json"}
            )
        elif capability_id == "pubmed.summary.v1":
            query = MappingProxyType({"db": "pubmed", "id": ",".join(frozen["requested_pmids"]), "retmode": "json"})
        elif capability_id == "europepmc.fuzzy_search.v1":
            query = MappingProxyType({"query": frozen["query"], "format": "json", "pageSize": frozen["page_size"]})
        elif capability_id == "web.doi_probe.v1":
            parsed = urlsplit(str(frozen["url"]))
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query:
                raise ValueError("web probe identity requires credential-free HTTPS URL")
            built_endpoint = str(frozen["url"])
            query = MappingProxyType({})
            frozen = MappingProxyType(
                {"url_digest": hashlib.sha256(str(frozen["url"]).encode()).hexdigest(), "scheme": "https"}
            )
            required_headers = MappingProxyType(
                {
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "identity",
                    "Accept-Language": "en-US,en;q=0.9",
                    "User-Agent": "CiteForge-Durable-Probe/1",
                }
            )
        elif capability_id == "gemini.short_title.v1":
            query = MappingProxyType({})
            prompt = gemini_prompt(str(frozen["title"]), int(frozen["max_words"]))
            body = MappingProxyType(
                {
                    "contents": ({"parts": ({"text": prompt},)},),
                    "generationConfig": frozen["generation_config"],
                }
            )
            credential = "header:x-goog-api-key"
        return BuiltRequest(
            capability_id,
            method,
            built_endpoint,
            frozen,
            query,
            body,
            credential,
            required_headers,
        )

    return build


@dataclass(frozen=True)
class AdapterCapability:
    logical_source: str
    operation: str
    adapter_version: str
    capability_id: str
    wire_provider: str
    quota_scope: str
    media_type: ResponseMediaType
    credential_kind: CredentialKind
    decoder_schema: str
    requested_fields: tuple[str, ...]
    method: str = "GET"
    builder_id: str = ""
    builder_version: str = "1"
    decoder_id: str = ""
    decoder_version: str = "1"
    body_limit: int = 2_000_000
    idempotent: bool = True
    max_attempts: int = HTTP_MAX_RETRIES + 1
    planner_emittable: bool = True
    plan_expansion: str = "none"
    auth_mode: str = "none"
    url_policy: str = "fixed_https_origin"
    _authority_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._authority_token is not _CAPABILITY_TOKEN:
            raise TypeError("authoritative capabilities cannot be constructed publicly")

    @property
    def exact_key(self) -> tuple[str, str, str]:
        return self.logical_source, self.operation, self.adapter_version

    def canonical_content(self) -> Mapping[str, object]:
        """Return the frozen Task 5B identity projection without expansion."""
        return MappingProxyType(
            {
                "adapter_version": self.adapter_version,
                "capability_id": self.capability_id,
                "credential_kind": (
                    "none" if self.capability_id == "s2.fuzzy_search.v1" else self.credential_kind.value
                ),
                "decoder_schema": self.decoder_schema,
                "logical_source": self.logical_source,
                "media_type": self.media_type.value,
                "operation": self.operation,
                "quota_scope": self.quota_scope,
                "requested_fields": self.requested_fields,
                "wire_provider": self.wire_provider,
            }
        )

    def registry_content(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "adapter_version": self.adapter_version,
                "auth_mode": self.auth_mode,
                "body_limit": self.body_limit,
                "builder_id": self.builder_id,
                "builder_version": self.builder_version,
                "capability_id": self.capability_id,
                "credential_kind": self.credential_kind.value,
                "decoder_id": self.decoder_id,
                "decoder_schema": self.decoder_schema,
                "decoder_version": self.decoder_version,
                "idempotent": self.idempotent,
                "logical_source": self.logical_source,
                "max_attempts": self.max_attempts,
                "media_type": self.media_type.value,
                "method": self.method,
                "operation": self.operation,
                "planner_emittable": self.planner_emittable,
                "plan_expansion": self.plan_expansion,
                "quota_scope": self.quota_scope,
                "requested_fields": self.requested_fields,
                "url_policy": self.url_policy,
                "wire_provider": self.wire_provider,
            }
        )


def _cap(
    capability_id: str,
    *,
    wire: str | None = None,
    media: ResponseMediaType = ResponseMediaType.JSON,
    credential: CredentialKind = CredentialKind.NONE,
    schema: str,
    fields: tuple[str, ...],
    method: str = "GET",
    planner: bool = True,
    idempotent: bool = True,
    max_attempts: int = HTTP_MAX_RETRIES + 1,
    auth_mode: str = "none",
    url_policy: str = "fixed_https_origin",
    body_limit: int = 2_000_000,
    plan_expansion: str = "none",
    builder_version: str = "1",
) -> AdapterCapability:
    logical_source, operation, version_token = capability_id.rsplit(".", 2)
    adapter_version = version_token.removeprefix("v")
    wire_provider = wire or logical_source
    return AdapterCapability(
        logical_source,
        operation,
        adapter_version,
        capability_id,
        wire_provider,
        wire_provider,
        media,
        credential,
        schema,
        fields,
        method,
        f"{capability_id}.builder",
        builder_version,
        f"{capability_id}.decoder",
        "1",
        body_limit,
        idempotent,
        max_attempts,
        planner,
        plan_expansion,
        auth_mode,
        url_policy,
        _CAPABILITY_TOKEN,
    )


_VALUES = (
    _cap(
        "scholar.inventory.v1",
        wire="serpapi",
        credential=CredentialKind.SERPAPI_KEY,
        schema="serpapi-scholar-author-v1",
        fields=("articles",),
        plan_expansion="scholar_next_page",
    ),
    _cap("dblp.inventory.v1", media=ResponseMediaType.XML, schema="dblpperson-v1", fields=("articles",)),
    _cap("doi_csl.csl_lookup.v1", wire="doi", schema="doi-csl-v1", fields=("metadata",)),
    _cap(
        "doi_bibtex.bibtex_lookup.v1",
        wire="doi",
        media=ResponseMediaType.BIBTEX,
        schema="doi-bibtex-v1",
        fields=("metadata",),
    ),
    _cap(
        "serply.scholar_search.v1",
        credential=CredentialKind.SERPLY_KEY,
        schema="serply-scholar-v1",
        fields=("articles",),
    ),
    _cap(
        "s2.fuzzy_search.v1",
        credential=CredentialKind.S2_API_KEY,
        schema="s2-search-v1",
        fields=("results",),
        planner=False,
    ),
    _cap(
        "s2.fuzzy_search.v2",
        credential=CredentialKind.S2_API_KEY,
        schema="s2-search-v2",
        fields=("results",),
        builder_version="2",
    ),
    _cap("crossref.fuzzy_search.v1", schema="crossref-search-v1", fields=("results",)),
    _cap(
        "openreview.term_search.v1",
        credential=CredentialKind.OPENREVIEW_RUNTIME,
        schema="openreview-notes-v1",
        fields=("notes",),
        auth_mode="runtime_selected_session_or_anonymous_no_downgrade",
        plan_expansion="openreview_fallback_if_empty",
    ),
    _cap(
        "openreview.fallback_search.v1",
        credential=CredentialKind.OPENREVIEW_RUNTIME,
        schema="openreview-search-v1",
        fields=("notes",),
        auth_mode="runtime_selected_session_or_anonymous_no_downgrade",
        builder_version="2",
    ),
    _cap("arxiv.fuzzy_search.v1", media=ResponseMediaType.XML, schema="arxiv-atom-v1", fields=("entries",)),
    _cap("openalex.fuzzy_search.v1", schema="openalex-search-v1", fields=("results",)),
    _cap(
        "pubmed.title_search.v1",
        schema="pubmed-esearch-v1",
        fields=("pmids",),
        plan_expansion="pubmed_singleton_summaries",
    ),
    _cap("pubmed.summary.v1", schema="pubmed-esummary-v1", fields=("records",)),
    _cap("europepmc.fuzzy_search.v1", schema="europepmc-search-v1", fields=("results",)),
    _cap("crossref.venue_search.v1", schema="crossref-venue-v1", fields=("results",)),
    _cap("openalex.venue_search.v1", schema="openalex-venue-v1", fields=("results",)),
    _cap(
        "web.doi_probe.v1",
        media=ResponseMediaType.HTML,
        schema="html-doi-v1",
        fields=("doi",),
        url_policy="ssrf_safe_https_redirects_no_private_networks",
        plan_expansion="bounded_html_probe_wave",
    ),
    _cap(
        "gemini.short_title.v1",
        credential=CredentialKind.GEMINI_KEY,
        schema="gemini-short-title-v1",
        fields=("candidates",),
        method="POST",
        idempotent=False,
        max_attempts=1,
    ),
)


def _validated(
    values: tuple[AdapterCapability, ...],
) -> tuple[Mapping[str, AdapterCapability], Mapping[str, BuilderDefinition]]:
    if len({item.capability_id for item in values}) != len(values) or len({item.exact_key for item in values}) != len(
        values
    ):
        raise RuntimeError("duplicate durable capability identity")
    capabilities = {item.capability_id: item for item in values}
    builders = {
        item.builder_id: BuilderDefinition(
            item.builder_id,
            item.builder_version,
            _builder_callback(item.capability_id, item.method, _ENDPOINTS[item.capability_id]),
        )
        for item in values
    }
    if any(not item.builder_id or not item.decoder_id or not item.registry_content() for item in values):
        raise RuntimeError("incomplete durable capability")
    return MappingProxyType(capabilities), MappingProxyType(builders)


def validate_builder_bindings(
    capabilities: Mapping[str, AdapterCapability], builders: Mapping[str, BuilderDefinition]
) -> None:
    if {item.builder_id for item in capabilities.values()} != set(builders):
        raise RuntimeError("durable capability builder registry is incomplete")
    for capability in capabilities.values():
        builder = builders[capability.builder_id]
        probe: dict[str, object] = {
            key: (
                1
                if key
                in {
                    "start",
                    "num",
                    "min_year",
                    "max_words",
                    "year",
                    "limit",
                    "rows",
                    "max_results",
                    "per_page",
                    "retmax",
                    "page_size",
                }
                else "probe"
            )
            for key in _BUILDER_REQUIRED[capability.capability_id]
        }
        if capability.capability_id == "pubmed.summary.v1":
            probe["requested_pmids"] = ("1",)
        elif capability.capability_id == "web.doi_probe.v1":
            probe["url"] = "https://example.test/publication"
        elif capability.capability_id == "scholar.inventory.v1":
            probe.update({"start": 0, "num": 100, "sort": "pubdate", "min_year": 2020})
        elif capability.capability_id == "arxiv.fuzzy_search.v1":
            probe.update({"start": 0, "sort_by": "relevance", "sort_order": "descending"})
        elif capability.capability_id == "s2.fuzzy_search.v2":
            probe["year"] = 2020
        elif capability.capability_id == "gemini.short_title.v1":
            probe.update(
                {
                    "prompt_version": GEMINI_PROMPT_VERSION,
                    "model_id": GEMINI_MODEL_ID,
                    "generation_config": dict(GEMINI_GENERATION_CONFIG),
                }
            )
        if "author" in probe:
            probe["author"] = None
        try:
            if builder.callback is None:
                raise RuntimeError("durable capability builder callback is missing")
            artifact = builder.callback(probe)
            expected = _builder_callback(
                capability.capability_id, capability.method, _ENDPOINTS[capability.capability_id]
            )(probe)
        except Exception as exc:
            raise RuntimeError("durable capability builder binding mismatch") from exc
        if (
            builder.callback_id != capability.builder_id
            or builder.version != capability.builder_version
            or artifact.capability_id != capability.capability_id
            or artifact.method != capability.method
            or artifact.endpoint != expected.endpoint
            or dict(artifact.query) != dict(expected.query)
            or artifact.body != expected.body
            or artifact.credential_injection != expected.credential_injection
            or artifact.required_headers != expected.required_headers
        ):
            raise RuntimeError("durable capability builder binding mismatch")


_AUTHORITATIVE_VALUES = _VALUES
_AUTHORITATIVE_CAPABILITIES, _AUTHORITATIVE_BUILDERS = _validated(_AUTHORITATIVE_VALUES)
CAPABILITIES = MappingProxyType({item.capability_id: replace(item) for item in _AUTHORITATIVE_VALUES})
BUILDERS = MappingProxyType(
    {
        builder_id: BuilderDefinition(builder.callback_id, builder.version)
        for builder_id, builder in _AUTHORITATIVE_BUILDERS.items()
    }
)
validate_builder_bindings(_AUTHORITATIVE_CAPABILITIES, _AUTHORITATIVE_BUILDERS)


def capability_for(logical_source: str, operation: str, adapter_version: str) -> AdapterCapability:
    matches = [item for item in _AUTHORITATIVE_VALUES if item.exact_key == (logical_source, operation, adapter_version)]
    if len(matches) != 1:
        raise ValueError("exactly one adapter capability is required")
    return replace(matches[0])


def capability_by_id(capability_id: str) -> AdapterCapability:
    """Return a detached immutable capability value from private authority."""
    try:
        return replace(_AUTHORITATIVE_CAPABILITIES[capability_id])
    except KeyError as exc:
        raise ValueError("unknown durable capability") from exc


def build_request(capability_id: str, identity_payload: Mapping[str, object]) -> BuiltRequest:
    """Build a detached exact request artifact through private authority."""
    capability = capability_by_id(capability_id)
    callback = _AUTHORITATIVE_BUILDERS[capability.builder_id].callback
    if callback is None:
        raise RuntimeError("authoritative builder callback is missing")
    return callback(identity_payload)


def validate_capability_wire(
    capability_id: str,
    identity_payload: Mapping[str, object],
    url: str,
    headers: Mapping[str, str] | None,
    body: Mapping[str, object] | None,
) -> None:
    """Verify concrete wire metadata against the exact private builder."""
    capability_by_id(capability_id)
    if capability_id == "web.doi_probe.v1":
        built = build_request(capability_id, {"url": url})
        if set(identity_payload) != {"url_digest", "scheme"} or identity_payload.get("scheme") != "https":
            raise ValueError("web wire identity lacks exact digest evidence")
        parsed_web = urlsplit(url)
        if (
            parsed_web.scheme != "https"
            or not parsed_web.hostname
            or parsed_web.username
            or parsed_web.password
            or parsed_web.query
            or parsed_web.port not in {None, 443}
            or identity_payload.get("url_digest") != hashlib.sha256(url.encode()).hexdigest()
        ):
            raise ValueError("web wire URL does not match exact identity digest")
        expected_headers = {name.casefold(): value for name, value in built.required_headers.items()}
        actual_headers = {name.casefold(): value for name, value in (headers or {}).items()}
        if actual_headers != expected_headers or body:
            raise ValueError("web wire headers or body do not match the fixed probe policy")
        return
    built = build_request(capability_id, identity_payload)
    parsed = urlsplit(url)
    actual_endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    if actual_endpoint != built.endpoint:
        raise ValueError("wire endpoint does not match exact capability builder")
    actual_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    allows_mailto = built.credential_injection == "query:mailto_if_configured"
    if not allows_mailto and any(key.casefold() == "mailto" for key, _ in actual_pairs):
        raise ValueError("wire query contains undeclared contact material")
    actual_query = sorted(
        (key, value)
        for key, value in actual_pairs
        if key.casefold() != "api_key" and not (allows_mailto and key.casefold() == "mailto")
    )
    expected_query = sorted((key, str(value)) for key, value in built.query.items())
    if actual_query != expected_query:
        raise ValueError("wire query does not match exact capability builder")
    header_names = {name.casefold() for name in (headers or {})}
    actual_headers = {name.casefold(): value for name, value in (headers or {}).items()}
    expected_headers = {name.casefold(): value for name, value in built.required_headers.items()}
    if any(actual_headers.get(name) != value for name, value in expected_headers.items()):
        raise ValueError("wire headers do not match exact capability builder")
    declared_secret_headers = (
        {built.credential_injection.split(":", 1)[1].casefold()}
        if built.credential_injection.startswith("header:")
        else ({"cookie"} if built.credential_injection.startswith("cookie:") else set())
    )
    benign_headers = {"accept", "accept-encoding", "content-type", "user-agent", "x-proxy-location"}
    if header_names - benign_headers - declared_secret_headers:
        raise ValueError("wire headers contain undeclared material")
    if (
        built.credential_injection.startswith("header:")
        and built.credential_injection.split(":", 1)[1].casefold() not in header_names
    ):
        raise ValueError("wire credential injection does not match capability")
    if built.credential_injection == "query:api_key" and (
        sum(key == "api_key" for key, _ in actual_pairs) != 1
        or any(key.casefold() == "api_key" and key != "api_key" for key, _ in actual_pairs)
    ):
        raise ValueError("wire credential injection does not match capability")
    if built.credential_injection != "query:api_key" and any(key.casefold() == "api_key" for key, _ in actual_pairs):
        raise ValueError("wire query contains undeclared credential material")
    if sum(key.casefold() == "mailto" for key, _ in actual_pairs) > 1:
        raise ValueError("wire contact injection is ambiguous")
    if _safe_payload(body) != built.body:
        raise ValueError("wire body does not match exact capability builder")


def registry_digest(values: Iterable[AdapterCapability]) -> str:
    content = [dict(item.registry_content()) for item in sorted(values, key=lambda item: item.capability_id)]
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


REGISTRY_DIGEST = registry_digest(CAPABILITIES.values())

__all__ = [
    "BUILDERS",
    "CAPABILITIES",
    "REGISTRY_DIGEST",
    "AdapterCapability",
    "BuiltRequest",
    "CredentialKind",
    "ResponseMediaType",
    "build_request",
    "capability_by_id",
    "capability_for",
    "registry_digest",
    "validate_builder_bindings",
    "validate_capability_wire",
]
