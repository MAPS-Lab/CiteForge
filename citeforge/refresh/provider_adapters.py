"""Provider JSON envelope contracts shared by legacy and durable refresh callers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .ledger import RequestSpec, TaskClaim
from .transport import ProviderTransport, SchemaChangedError, SendOperation, consume_response, correlate_exact_batch

Normalizer = Callable[[dict[str, object]], Mapping[str, object]]
EmptyCheck = Callable[[dict[str, object]], bool]


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


def _csl(value: dict[str, object]) -> Mapping[str, object]:
    if not value or not isinstance(value.get("title"), (str, list)):
        raise SchemaChangedError("DOI CSL response lacks title metadata")
    return {"metadata": value}


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
        request = RequestSpec(
            self.provider,
            self.operation,
            self.method,
            normalized_payload,
            (self.requested_field,),
            adapter_version,
            freshness_epoch,
            quota_scope or self.quota_scope or self.provider,
        )
        return SendOperation(
            request,
            url,
            timeout,
            self.normalize,
            self.authoritative_empty,
            headers,
            json_payload,
            idempotent,
            idempotency_key,
            idempotency_header,
        )

    def send(
        self, transport: ProviderTransport, operation: SendOperation, *, task_claim: TaskClaim | None = None
    ) -> Mapping[str, object]:
        return consume_response(transport.send(operation, task_claim=task_claim))


def _normalized_empty(normalize: Normalizer, field: str) -> EmptyCheck:
    return lambda value: not normalize(value)[field]


_S2_SEARCH = _list("results", "data")
_CROSSREF_SEARCH = _list("results", "message", "items")
_OPENALEX_SEARCH = _list("results", "results")
_EUROPEPMC_SEARCH = _list("results", "resultList", "result")
_SERPLY = _list("articles", "articles")
_SERPAPI = _list("articles", "articles")
_DBLP = _list("hits", "result", "hits", "hit")
_PUBMED_SEARCH = _list("pmids", "esearchresult", "idlist")
_OPENREVIEW = _list("notes", "notes")
_GEMINI = _list("candidates", "candidates")

JSON_ADAPTERS: Mapping[str, JsonProviderAdapter] = {
    "semantic_scholar.search": JsonProviderAdapter(
        "s2", "fuzzy_search", "results", _S2_SEARCH, _normalized_empty(_S2_SEARCH, "results")
    ),
    "crossref.search": JsonProviderAdapter(
        "crossref", "fuzzy_search", "results", _CROSSREF_SEARCH, _normalized_empty(_CROSSREF_SEARCH, "results")
    ),
    "openalex.search": JsonProviderAdapter(
        "openalex", "fuzzy_search", "results", _OPENALEX_SEARCH, _normalized_empty(_OPENALEX_SEARCH, "results")
    ),
    "europepmc.search": JsonProviderAdapter(
        "europepmc", "fuzzy_search", "results", _EUROPEPMC_SEARCH, _normalized_empty(_EUROPEPMC_SEARCH, "results")
    ),
    "serply.scholar": JsonProviderAdapter(
        "serply", "scholar_search", "articles", _SERPLY, _normalized_empty(_SERPLY, "articles")
    ),
    "serpapi.author": JsonProviderAdapter(
        "serpapi", "author_inventory", "articles", _SERPAPI, _normalized_empty(_SERPAPI, "articles")
    ),
    "dblp.author_search": JsonProviderAdapter("dblp", "author_search", "hits", _DBLP, _normalized_empty(_DBLP, "hits")),
    "pubmed.search": JsonProviderAdapter(
        "pubmed", "search", "pmids", _PUBMED_SEARCH, _normalized_empty(_PUBMED_SEARCH, "pmids")
    ),
    "openreview.notes": JsonProviderAdapter(
        "openreview", "notes_search", "notes", _OPENREVIEW, _normalized_empty(_OPENREVIEW, "notes")
    ),
    "gemini.short_title": JsonProviderAdapter(
        "gemini", "short_title", "candidates", _GEMINI, _normalized_empty(_GEMINI, "candidates"), "POST"
    ),
    "doi.csl": JsonProviderAdapter("doi_csl", "csl_lookup", "metadata", _csl, lambda _value: False, quota_scope="doi"),
}


def pubmed_summary_adapter(requested_pmids: tuple[str, ...]) -> JsonProviderAdapter:
    """Build the fail-closed ESummary adapter for one exact PMID set."""

    def normalize(value: dict[str, object]) -> Mapping[str, object]:
        result = _path(value, "result")
        if not isinstance(result, dict):
            raise SchemaChangedError("PubMed ESummary result is not an object")
        uids = result.get("uids")
        if not isinstance(uids, list) or not all(isinstance(uid, str) for uid in uids):
            raise SchemaChangedError("PubMed ESummary lacks string uids")
        raw_members = [result.get(uid) for uid in uids]
        if not all(isinstance(member, Mapping) for member in raw_members):
            raise SchemaChangedError("PubMed ESummary lacks a requested record")
        members = [member for member in raw_members if isinstance(member, Mapping)]
        try:
            correlated = correlate_exact_batch(requested_pmids, members, correlation_field="uid")
        except ValueError as exc:
            raise SchemaChangedError(f"PubMed ESummary correlation failed: {exc}") from exc
        return {"records": correlated}

    return JsonProviderAdapter("pubmed", "summary", "records", normalize, lambda _value: False)


__all__ = ["JSON_ADAPTERS", "JsonProviderAdapter", "pubmed_summary_adapter"]
