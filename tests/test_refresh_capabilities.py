from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest

from citeforge.refresh.capabilities import (
    BUILDERS,
    CAPABILITIES,
    GEMINI_GENERATION_CONFIG,
    GEMINI_MODEL_ID,
    GEMINI_PROMPT_VERSION,
    REGISTRY_DIGEST,
    AdapterCapability,
    CredentialKind,
    ResponseMediaType,
    build_request,
    capability_for,
    registry_digest,
    validate_builder_bindings,
    validate_capability_wire,
)
from citeforge.refresh.decoders import DECODERS, decode_response
from citeforge.refresh.provider_adapters import JSON_ADAPTERS
from citeforge.refresh.transport import RawProviderResponse

PLANNER_CAPABILITY_IDS = {
    "scholar.inventory.v1",
    "dblp.inventory.v1",
    "doi_csl.csl_lookup.v1",
    "doi_bibtex.bibtex_lookup.v1",
    "serply.scholar_search.v1",
    "s2.fuzzy_search.v2",
    "crossref.fuzzy_search.v1",
    "openreview.term_search.v1",
    "openreview.fallback_search.v1",
    "arxiv.fuzzy_search.v1",
    "openalex.fuzzy_search.v1",
    "pubmed.title_search.v1",
    "pubmed.summary.v1",
    "europepmc.fuzzy_search.v1",
    "crossref.venue_search.v1",
    "openalex.venue_search.v1",
    "web.doi_probe.v1",
    "gemini.short_title.v1",
}


def _builder_payload(capability_id: str) -> dict[str, object]:
    schemas: dict[str, dict[str, object]] = {
        "scholar.inventory.v1": {
            "author_key": "author",
            "profile_id": "p",
            "start": 0,
            "num": 100,
            "sort": "pubdate",
            "min_year": 2020,
        },
        "dblp.inventory.v1": {"author_key": "author", "pid": "p"},
        "s2.fuzzy_search.v2": {"author_key": "author", "author": "Ada", "title": "safe", "year": 2026},
        "s2.fuzzy_search.v1": {"author_key": "author", "query": "safe", "limit": 15},
        "serply.scholar_search.v1": {"author_key": "author", "query": "safe", "start": 0},
        "crossref.fuzzy_search.v1": {"author_key": "author", "query": "safe", "author": None, "rows": 20},
        "arxiv.fuzzy_search.v1": {
            "author_key": "author",
            "query": "safe",
            "start": 0,
            "max_results": 10,
            "sort_by": "relevance",
            "sort_order": "descending",
        },
        "openalex.fuzzy_search.v1": {"author_key": "author", "query": "safe", "per_page": 20},
        "pubmed.title_search.v1": {"author_key": "author", "query": "safe", "retmax": 5},
        "europepmc.fuzzy_search.v1": {"author_key": "author", "query": "safe", "page_size": 20},
        "doi_csl.csl_lookup.v1": {"doi": "10.1/x"},
        "doi_bibtex.bibtex_lookup.v1": {"doi": "10.1/x"},
        "openreview.term_search.v1": {"author_key": "author", "term": "safe", "limit": 20},
        "openreview.fallback_search.v1": {"author_key": "author", "query": "safe"},
        "pubmed.summary.v1": {"requested_pmids": ("1",)},
        "crossref.venue_search.v1": {"author_key": "author", "query": "safe", "venue": "v", "author": None, "rows": 20},
        "openalex.venue_search.v1": {"author_key": "author", "query": "safe", "venue": "v", "per_page": 20},
        "web.doi_probe.v1": {"url": "https://example.test"},
        "gemini.short_title.v1": {
            "title": "safe",
            "max_words": 4,
            "prompt_version": GEMINI_PROMPT_VERSION,
            "model_id": GEMINI_MODEL_ID,
            "generation_config": dict(GEMINI_GENERATION_CONFIG),
        },
    }
    return schemas.get(capability_id, {"query": "safe"})


def test_registry_has_exact_planner_capabilities_and_complete_callbacks() -> None:
    planner = {item.capability_id for item in CAPABILITIES.values() if item.planner_emittable}
    assert planner == PLANNER_CAPABILITY_IDS
    assert len({item.exact_key for item in CAPABILITIES.values()}) == len(CAPABILITIES)
    for capability in CAPABILITIES.values():
        assert BUILDERS[capability.builder_id].callback_id == capability.builder_id
        payload = _builder_payload(capability.capability_id)
        assert BUILDERS[capability.builder_id].callback is None
        built = build_request(capability.capability_id, payload)
        assert built.capability_id == capability.capability_id
        assert built.method == capability.method
        assert built.endpoint.startswith("https://") or built.endpoint == "validated_https_url"
        if capability.capability_id == "web.doi_probe.v1":
            assert set(built.identity_payload) == {"url_digest", "scheme"}
            assert "example.test" not in repr(built)
        else:
            assert built.identity_payload == payload
        assert DECODERS[capability.decoder_id].callback_id == capability.decoder_id
        assert DECODERS[capability.decoder_id].schema == capability.decoder_schema
        assert DECODERS[capability.decoder_id].media_type is capability.media_type
        assert set(capability.registry_content()) == {
            "adapter_version",
            "auth_mode",
            "body_limit",
            "builder_id",
            "builder_version",
            "capability_id",
            "credential_kind",
            "decoder_id",
            "decoder_schema",
            "decoder_version",
            "idempotent",
            "logical_source",
            "max_attempts",
            "media_type",
            "method",
            "operation",
            "planner_emittable",
            "plan_expansion",
            "quota_scope",
            "requested_fields",
            "url_policy",
            "wire_provider",
        }


def test_registry_digest_is_order_stable_and_materially_sensitive() -> None:
    assert registry_digest(tuple(reversed(tuple(CAPABILITIES.values())))) == REGISTRY_DIGEST
    original = next(iter(CAPABILITIES.values()))
    fields: dict[str, Any] = {
        "method": "HEAD" if original.method == "GET" else "GET",
        "builder_version": "changed",
        "decoder_version": "changed",
        "body_limit": original.body_limit + 1,
        "quota_scope": f"{original.quota_scope}-changed",
        "credential_kind": CredentialKind.NONE
        if original.credential_kind is not CredentialKind.NONE
        else CredentialKind.S2_API_KEY,
        "media_type": ResponseMediaType.HTML,
    }
    for name, value in fields.items():
        assert registry_digest((*CAPABILITIES.values(), replace(original, **{name: value}))) != REGISTRY_DIGEST


def test_public_registry_is_immutable_and_substitution_does_not_change_lookup() -> None:
    scholar = capability_for("scholar", "inventory", "1")
    with pytest.raises(TypeError):
        CAPABILITIES[scholar.capability_id] = scholar  # type: ignore[index]
    with pytest.raises(TypeError):
        BUILDERS[scholar.builder_id] = BUILDERS[scholar.builder_id]  # type: ignore[index]
    with pytest.raises(TypeError):
        DECODERS[scholar.decoder_id] = DECODERS[scholar.decoder_id]  # type: ignore[index]
    forged = replace(scholar, wire_provider="attacker")
    assert forged != capability_for("scholar", "inventory", "1")
    assert registry_digest(CAPABILITIES.values()) == REGISTRY_DIGEST


def test_builder_callback_substitution_fails_registry_validation() -> None:
    scholar = capability_for("scholar", "inventory", "1")
    s2 = capability_for("s2", "fuzzy_search", "2")
    forged = dict(BUILDERS)
    forged[scholar.builder_id] = replace(forged[scholar.builder_id], callback=BUILDERS[s2.builder_id].callback)
    with pytest.raises(RuntimeError, match="binding mismatch"):
        validate_builder_bindings(CAPABILITIES, forged)


def test_public_builder_object_mutation_cannot_change_authoritative_wire_proof() -> None:
    capability_id = "doi_csl.csl_lookup.v1"
    public_builder = BUILDERS[CAPABILITIES[capability_id].builder_id]
    try:
        object.__setattr__(
            public_builder, "callback", lambda _payload: build_request("doi_bibtex.bibtex_lookup.v1", {"doi": "10.1/x"})
        )
        with pytest.raises(ValueError, match="headers"):
            validate_capability_wire(capability_id, {"doi": "10.1/x"}, "https://doi.org/10.1/x", {}, None)
        assert capability_for("doi_csl", "csl_lookup", "1").capability_id == capability_id
        assert registry_digest(CAPABILITIES.values()) == REGISTRY_DIGEST
    finally:
        object.__setattr__(public_builder, "callback", None)


def test_public_builder_closure_mutation_cannot_change_authoritative_wire_proof() -> None:
    capability_id = "doi_csl.csl_lookup.v1"
    assert BUILDERS[CAPABILITIES[capability_id].builder_id].callback is None
    with pytest.raises(ValueError, match="headers"):
        validate_capability_wire(
            capability_id,
            {"doi": "10.1/x"},
            "https://doi.org/10.1/x",
            {"Accept": "application/x-bibtex"},
            None,
        )
    validate_capability_wire(
        capability_id,
        {"doi": "10.1/x"},
        "https://doi.org/10.1/x",
        {"Accept": "application/vnd.citationstyles.csl+json"},
        None,
    )
    assert capability_for("doi_csl", "csl_lookup", "1").capability_id == capability_id
    assert registry_digest(CAPABILITIES.values()) == REGISTRY_DIGEST


def test_public_builder_metadata_exposes_no_globals_or_private_authority() -> None:
    for builder in BUILDERS.values():
        assert builder.callback is None
    built = build_request("doi_csl.csl_lookup.v1", {"doi": "10.1/x"})
    assert built.required_headers == {"Accept": "application/vnd.citationstyles.csl+json"}


def test_public_decoder_metadata_has_no_reachable_authoritative_callback() -> None:
    decoder_id = "doi_csl.csl_lookup.v1.decoder"
    public_decoder = DECODERS[decoder_id]
    assert public_decoder.callback is None
    object.__setattr__(public_decoder, "callback", lambda *_args: ({"metadata": {"title": "forged"}}, False))
    try:
        normalized, empty = decode_response(
            decoder_id,
            RawProviderResponse(
                b'{"title":"authoritative"}',
                "application/vnd.citationstyles.csl+json",
                "https://doi.org/10.1/x",
                {},
            ),
            {"doi": "10.1/x"},
        )
        assert cast(dict[str, Any], normalized)["metadata"]["title"] == "authoritative"
        assert not empty
    finally:
        object.__setattr__(public_decoder, "callback", None)


def test_distinct_operations_bind_exact_endpoint_templates() -> None:
    endpoints = {
        capability_id: build_request(capability_id, _builder_payload(capability_id)).endpoint
        for capability_id in (
            "openreview.term_search.v1",
            "openreview.fallback_search.v1",
            "pubmed.title_search.v1",
            "pubmed.summary.v1",
        )
    }
    assert endpoints["openreview.term_search.v1"].endswith("/notes")
    assert endpoints["openreview.fallback_search.v1"].endswith("/notes/search")
    assert endpoints["pubmed.title_search.v1"].endswith("/esearch.fcgi")
    assert endpoints["pubmed.summary.v1"].endswith("/esummary.fcgi")
    assert len(set(endpoints.values())) == 4
    fallback = build_request("openreview.fallback_search.v1", _builder_payload("openreview.fallback_search.v1"))
    assert fallback.query == {"query": "safe", "limit": 20}
    serply = build_request("serply.scholar_search.v1", _builder_payload("serply.scholar_search.v1")).endpoint
    assert serply.endswith("/safe")


def test_serply_builder_binds_output_affecting_current_client_headers() -> None:
    request = build_request("serply.scholar_search.v1", _builder_payload("serply.scholar_search.v1"))
    assert request.required_headers == {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "X-Proxy-Location": "US",
    }


def test_doi_media_operations_require_distinct_exact_accept_headers() -> None:
    csl = build_request("doi_csl.csl_lookup.v1", {"doi": "10.1/x"})
    bibtex = build_request("doi_bibtex.bibtex_lookup.v1", {"doi": "10.1/x"})
    assert csl.required_headers == {"Accept": "application/vnd.citationstyles.csl+json"}
    assert bibtex.required_headers == {"Accept": "application/x-bibtex"}
    assert csl.required_headers != bibtex.required_headers
    for built in (csl, bibtex):
        with pytest.raises(ValueError, match="headers"):
            validate_capability_wire(built.capability_id, built.identity_payload, built.endpoint, {}, built.body)
        validate_capability_wire(
            built.capability_id, built.identity_payload, built.endpoint, built.required_headers, built.body
        )


def test_web_digest_identity_reconstructs_exact_private_wire_without_repr_leak() -> None:
    raw_url = "https://private.example.test/private/paper"
    built = build_request("web.doi_probe.v1", {"url": raw_url})
    assert raw_url not in repr(built)
    assert set(built.identity_payload) == {"url_digest", "scheme"}
    validate_capability_wire(
        built.capability_id, built.identity_payload, built.endpoint, built.required_headers, built.body
    )
    with pytest.raises(ValueError, match="digest"):
        validate_capability_wire(
            built.capability_id,
            built.identity_payload,
            "https://private.example.test/other",
            built.required_headers,
            built.body,
        )


def test_builder_rejects_wire_secret_material() -> None:
    with pytest.raises(ValueError, match="secrets"):
        build_request("scholar.inventory.v1", {"auth": {"api_key": "secret"}})
    with pytest.raises(ValueError, match="secrets"):
        build_request("scholar.inventory.v1", {"url": "https://x.test?q=api_key=secret"})


def test_builder_payload_is_deeply_immutable_strict_json() -> None:
    built = build_request(
        "crossref.fuzzy_search.v1", {"author_key": "author", "query": "safe", "author": None, "rows": 20}
    )
    with pytest.raises(TypeError):
        built.query["query.bibliographic"] = "changed"  # type: ignore[index]
    gemini = build_request("gemini.short_title.v1", _builder_payload("gemini.short_title.v1"))
    with pytest.raises(TypeError):
        gemini.body["contents"][0]["parts"][0]["text"] = "changed"  # type: ignore[index, union-attr]
    with pytest.raises(ValueError, match="finite"):
        build_request(
            "crossref.fuzzy_search.v1",
            {"author_key": "author", "query": float("nan"), "author": None, "rows": 20},
        )
    with pytest.raises(TypeError, match="strict JSON"):
        build_request(
            "crossref.fuzzy_search.v1",
            {"author_key": "author", "query": object(), "author": None, "rows": 20},
        )


def test_task5b_inventory_canonical_projection_is_byte_identical() -> None:
    expected = {
        "adapter_version": "1",
        "capability_id": "scholar.inventory.v1",
        "credential_kind": "serpapi_key",
        "decoder_schema": "serpapi-scholar-author-v1",
        "logical_source": "scholar",
        "media_type": "json",
        "operation": "inventory",
        "quota_scope": "serpapi",
        "requested_fields": ("articles",),
        "wire_provider": "serpapi",
    }
    capability = capability_for("scholar", "inventory", "1")
    assert dict(capability.canonical_content()) == expected
    assert json.dumps(dict(capability.canonical_content()), sort_keys=True, default=list)


def test_openreview_and_gemini_policies_are_non_substitutable() -> None:
    term = capability_for("openreview", "term_search", "1")
    fallback = capability_for("openreview", "fallback_search", "1")
    assert term.capability_id != fallback.capability_id
    assert term.builder_id != fallback.builder_id
    assert term.auth_mode == fallback.auth_mode == "runtime_selected_session_or_anonymous_no_downgrade"
    gemini = capability_for("gemini", "short_title", "1")
    assert gemini.method == "POST"
    assert not gemini.idempotent
    assert gemini.max_attempts == 1


def test_web_probe_wire_requires_the_exact_fixed_header_policy() -> None:
    raw_url = "https://papers.example.test/item"
    built = build_request("web.doi_probe.v1", {"url": raw_url})
    validate_capability_wire("web.doi_probe.v1", built.identity_payload, raw_url, built.required_headers, None)
    for headers in ({}, {**dict(built.required_headers), "User-Agent": "changed"}):
        with pytest.raises(ValueError, match="fixed probe policy"):
            validate_capability_wire("web.doi_probe.v1", built.identity_payload, raw_url, headers, None)


def test_authoritative_capability_constructor_is_not_public() -> None:
    with pytest.raises(TypeError, match="authoritative"):
        AdapterCapability(  # type: ignore[call-arg]
            "x",
            "x",
            "1",
            "x.v1",
            "x",
            "x",
            ResponseMediaType.JSON,
            CredentialKind.NONE,
            "x-v1",
            ("results",),
        )


def test_legacy_json_view_is_immutable_and_capability_bound() -> None:
    with pytest.raises(TypeError):
        JSON_ADAPTERS["crossref.search"] = JSON_ADAPTERS["crossref.search"]  # type: ignore[index]
    for name, adapter in JSON_ADAPTERS.items():
        if name == "dblp.author_search":
            assert not adapter.capability_id
            continue
        capability = CAPABILITIES[adapter.capability_id]
        assert adapter.decoder_id == capability.decoder_id
        assert adapter.method == capability.method
        assert adapter.requested_field in capability.requested_fields
        operation = adapter.build_operation(
            url="https://provider.test/resource",
            normalized_payload=_builder_payload(capability.capability_id),
            freshness_epoch="current",
            adapter_version="1",
            timeout=5,
        )
        assert operation.response_decoder is not None


def test_distinct_legacy_routes_bind_distinct_capabilities() -> None:
    assert JSON_ADAPTERS["crossref.search"].capability_id == "crossref.fuzzy_search.v1"
    assert JSON_ADAPTERS["crossref.venue"].capability_id == "crossref.venue_search.v1"
    assert JSON_ADAPTERS["openalex.search"].capability_id == "openalex.fuzzy_search.v1"
    assert JSON_ADAPTERS["openalex.venue"].capability_id == "openalex.venue_search.v1"
    assert JSON_ADAPTERS["openreview.term"].capability_id == "openreview.term_search.v1"
    assert JSON_ADAPTERS["openreview.fallback"].capability_id == "openreview.fallback_search.v1"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"adapter_version": "9"}, "version"),
        ({"quota_scope": "forged"}, "quota"),
        ({"idempotent": False}, "idempotency"),
    ],
)
def test_legacy_json_view_rejects_forged_capability_policy_before_send(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "url": "https://api.crossref.org/works",
        "normalized_payload": {"query": "safe"},
        "freshness_epoch": "current",
        "adapter_version": "1",
        "timeout": 5,
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        JSON_ADAPTERS["crossref.search"].build_operation(**values)  # type: ignore[arg-type]
