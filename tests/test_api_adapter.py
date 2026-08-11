"""Offline contract tests for configured metadata-candidate adapters."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from citeforge import api_configs, api_generics
from citeforge.api_configs import (
    CROSSREF_VENUE_SEARCH_CONFIG,
    EUROPEPMC_SEARCH_CONFIG,
    OPENALEX_VENUE_SEARCH_CONFIG,
    S2_SEARCH_CONFIG,
)
from citeforge.api_generics import APISearchConfig
from citeforge.cache import ResponseCache
from citeforge.clients import search_apis
from citeforge.pipeline import article

TITLE = "Ocean Forecasting"
AUTHOR = "Ada Lovelace"


@dataclass(frozen=True)
class _AdapterCase:
    """Immutable source contract used by migrated cache scenarios."""

    source_id: str
    config: APISearchConfig
    api_key: str | None
    venue: str | None
    namespace: str
    cache_key: str
    zero_limit_param: str
    zero_limit_expected: str
    candidate: dict[str, object]


SEMANTIC_SCHOLAR = _AdapterCase(
    "semantic-scholar",
    S2_SEARCH_CONFIG,
    "s2-secret",
    None,
    "semantic_scholar",
    "multi|ocean forecasting|ada lovelace",
    "limit",
    "0",
    {"id": "s2", "title": TITLE, "authors": [{"name": AUTHOR}]},
)
EUROPEPMC = _AdapterCase(
    "europepmc",
    EUROPEPMC_SEARCH_CONFIG,
    None,
    None,
    "europepmc",
    "multi|ocean forecasting|ada lovelace",
    "pageSize",
    "0",
    {"id": "epmc", "title": TITLE, "authorString": AUTHOR},
)
CROSSREF_VENUE = _AdapterCase(
    "crossref-venue",
    CROSSREF_VENUE_SEARCH_CONFIG,
    None,
    "Nature",
    "crossref_venue",
    "venue|ocean forecasting|ada lovelace|nature",
    "rows",
    "10",
    {"id": "crossref", "title": [TITLE], "author": [{"given": "Ada", "family": "Lovelace"}]},
)
OPENALEX_VENUE = _AdapterCase(
    "openalex-venue",
    OPENALEX_VENUE_SEARCH_CONFIG,
    None,
    "Nature",
    "openalex_venue",
    "venue|ocean forecasting|ada lovelace|nature",
    "per-page",
    "10",
    {"id": "openalex", "title": TITLE, "authorships": [{"author": {"display_name": AUTHOR}}]},
)

MIGRATED_CONTRACTS = [
    pytest.param(SEMANTIC_SCHOLAR, "negative", id="semantic-scholar-negative-cache"),
    pytest.param(EUROPEPMC, "negative", id="europepmc-negative-cache"),
    pytest.param(CROSSREF_VENUE, "negative", id="crossref-venue-negative-cache"),
    pytest.param(OPENALEX_VENUE, "negative", id="openalex-venue-negative-cache"),
    pytest.param(SEMANTIC_SCHOLAR, "positive", id="semantic-scholar-positive-cache"),
    pytest.param(EUROPEPMC, "positive", id="europepmc-positive-cache"),
    pytest.param(CROSSREF_VENUE, "positive", id="crossref-venue-positive-cache"),
    pytest.param(OPENALEX_VENUE, "positive", id="openalex-venue-positive-cache"),
    pytest.param(SEMANTIC_SCHOLAR, "zero-limit", id="semantic-scholar-zero-limit"),
    pytest.param(CROSSREF_VENUE, "zero-limit", id="crossref-venue-zero-limit"),
    pytest.param(OPENALEX_VENUE, "zero-limit", id="openalex-venue-zero-limit"),
]


@pytest.fixture(autouse=True)
def adapter_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ResponseCache:
    """Give every adapter contract an isolated offline response cache."""
    cache = ResponseCache(str(tmp_path / "cache"))
    monkeypatch.setattr(api_generics, "response_cache", cache)
    return cache


def _adapter_payload(case: _AdapterCase, records: list[dict[str, object]]) -> dict[str, object]:
    """Build the visibly source-specific response envelope for *case*."""
    if case.source_id == "semantic-scholar":
        return {"data": records}
    if case.source_id == "europepmc":
        return {"resultList": {"result": records}}
    if case.source_id == "crossref-venue":
        return {"message": {"items": records}}
    if case.source_id == "openalex-venue":
        return {"results": records}
    raise AssertionError(f"unsupported adapter case: {case.source_id}")


def _install_adapter_stub(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> dict[str, object]:
    """Install one observable stub for both generic and Semantic Scholar HTTP paths."""
    observed: dict[str, object] = {"calls": 0}

    def fake_get(url: str, timeout: float) -> dict[str, object]:
        observed["calls"] = int(observed["calls"]) + 1
        observed["url"] = url
        return payload

    def fake_s2(url: str, api_key: str, timeout: float) -> dict[str, object]:
        assert api_key == "s2-secret"
        return fake_get(url, timeout)

    monkeypatch.setattr(api_generics, "http_get_json", fake_get)
    monkeypatch.setattr(api_generics, "s2_http_get_json", fake_s2)
    return observed


def _query(observed: dict[str, object]) -> dict[str, list[str]]:
    """Return parsed query parameters from a recorded request."""
    url = observed["url"]
    assert isinstance(url, str)
    return parse_qs(urlparse(url).query)


def test_s2_multiple_preserves_api_order_and_cache_identity(
    monkeypatch: pytest.MonkeyPatch, adapter_env: ResponseCache
) -> None:
    """A low-scoring first S2 record remains first, unlike scored sources."""
    first = {"paperId": "first", "title": "Unrelated Result", "authors": [{"name": AUTHOR}]}
    second = {"paperId": "second", "title": "Ocean Forecasting with Neural Networks", "authors": [{"name": AUTHOR}]}
    original_params = dict(S2_SEARCH_CONFIG.additional_params)
    observed = _install_adapter_stub(monkeypatch, {"data": [first, second]})

    result = api_generics.search_api_generic_multiple(
        "Ocean Forecasting with Neural Networks", AUTHOR, S2_SEARCH_CONFIG, "s2-secret", max_results=2
    )

    assert result == [first, second]
    params = _query(observed)
    assert params["query"] == ['"Ocean Forecasting with Neural Networks" Ada Lovelace']
    assert params["limit"] == ["4"]
    assert adapter_env.get("semantic_scholar", "multi|ocean forecasting with neural networks|ada lovelace") == {
        "results": [first, second]
    }
    assert S2_SEARCH_CONFIG.additional_params == original_params


def test_europepmc_multiple_keeps_source_order_and_fielded_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Europe PMC preserves its API ranking instead of applying generic scoring."""
    first = {"id": "first", "title": "Unrelated Result", "authorString": AUTHOR}
    second = {"id": "second", "title": "Ocean Forecasting with Neural Networks", "authorString": AUTHOR}
    observed = _install_adapter_stub(monkeypatch, {"resultList": {"result": [first, second]}})

    result = api_generics.search_api_generic_multiple(
        'Ocean "Forecasting" with Neural Networks', AUTHOR, EUROPEPMC_SEARCH_CONFIG, max_results=2
    )

    assert result == [first, second]
    params = _query(observed)
    assert params["query"] == ['TITLE:"Ocean Forecasting with Neural Networks" AND AUTH:"Ada Lovelace"']
    assert params["pageSize"] == ["2"]


def test_europepmc_zero_limit_is_empty_without_mutating_shared_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero candidate limit is preserved in the request and leaves the descriptor unchanged."""
    original_params = dict(EUROPEPMC_SEARCH_CONFIG.additional_params)
    observed = _install_adapter_stub(
        monkeypatch, {"resultList": {"result": [{"id": "ignored", "title": "Any", "authorString": "A"}]}}
    )

    result = api_generics.search_api_generic_multiple("Any", "A", EUROPEPMC_SEARCH_CONFIG, max_results=0)

    assert result == []
    assert _query(observed)["pageSize"] == ["0"]
    assert EUROPEPMC_SEARCH_CONFIG.additional_params == original_params


@pytest.mark.parametrize(("max_results", "expected_ids"), [(0, []), (1, ["first"])])
def test_multiple_cache_hit_honors_limit_and_returns_defensive_copies(
    monkeypatch: pytest.MonkeyPatch, max_results: int, expected_ids: list[str]
) -> None:
    """A cached wider search cannot exceed a later limit or leak mutable state."""
    records = [{"paperId": "first", "title": "First"}, {"paperId": "second", "title": "Second"}]
    observed = _install_adapter_stub(monkeypatch, {"data": records})

    assert (
        len(api_generics.search_api_generic_multiple("Cached candidates", AUTHOR, S2_SEARCH_CONFIG, "s2-secret", 2))
        == 2
    )
    cached_result = api_generics.search_api_generic_multiple(
        "Cached candidates", AUTHOR, S2_SEARCH_CONFIG, "s2-secret", max_results
    )
    assert [record["paperId"] for record in cached_result] == expected_ids

    if cached_result:
        cached_result[0]["paperId"] = "mutated"
        repeated = api_generics.search_api_generic_multiple(
            "Cached candidates", AUTHOR, S2_SEARCH_CONFIG, "s2-secret", max_results
        )
        assert [record["paperId"] for record in repeated] == expected_ids
    assert observed["calls"] == 1


def test_client_multiple_wrappers_use_the_configured_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Client wrappers only select their immutable adapter and preserve call inputs."""
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_multiple(*args: object, **kwargs: object) -> list[dict[str, str]]:
        calls.append((args, kwargs))
        assert isinstance(args[2], APISearchConfig)
        return [{"adapter": args[2].api_name}]

    monkeypatch.setattr(api_generics, "search_api_generic_multiple", fake_multiple)

    assert search_apis.s2_search_papers_multiple("Title", "Author", "s2-key", max_results=3) == [
        {"adapter": "semantic_scholar"}
    ]
    assert search_apis.europepmc_search_papers_multiple("Title", "Author", max_results=0) == [{"adapter": "europepmc"}]
    assert search_apis.crossref_search_by_venue("Title", "Author", "Venue", max_results=4) == [
        {"adapter": "crossref_venue"}
    ]
    assert search_apis.openalex_search_by_venue("Title", "Author", "Venue", max_results=4) == [
        {"adapter": "openalex_venue"}
    ]
    assert calls == [
        (("Title", "Author", S2_SEARCH_CONFIG, "s2-key", 3), {}),
        (("Title", "Author", EUROPEPMC_SEARCH_CONFIG), {"max_results": 0}),
        (("Title", "Author", CROSSREF_VENUE_SEARCH_CONFIG), {"max_results": 4, "venue": "Venue"}),
        (("Title", "Author", OPENALEX_VENUE_SEARCH_CONFIG), {"max_results": 4, "venue": "Venue"}),
    ]


@pytest.mark.parametrize(
    ("builder_name", "mapping"),
    [
        ("S2", api_configs.S2_FIELD_MAPPING),
        ("CROSSREF", api_configs.CROSSREF_FIELD_MAPPING),
        ("ARXIV", api_configs.ARXIV_FIELD_MAPPING),
        ("OPENREVIEW", api_configs.OPENREVIEW_FIELD_MAPPING),
        ("OPENALEX", api_configs.OPENALEX_FIELD_MAPPING),
    ],
)
def test_phase2_uses_mapping_bound_generic_builders(builder_name: str, mapping: api_generics.APIFieldMapping) -> None:
    """Phase 2 binds each source directly to its existing field mapping."""
    builder = getattr(article, f"_{builder_name}_BUILDER")

    assert isinstance(builder, partial)
    assert builder.func is api_generics.build_bibtex_from_response
    assert builder.keywords == {"mapping": mapping}


@pytest.mark.parametrize(("case", "scenario"), MIGRATED_CONTRACTS)
def test_migrated_candidate_contracts(
    monkeypatch: pytest.MonkeyPatch, adapter_env: ResponseCache, case: _AdapterCase, scenario: str
) -> None:
    """Migrated adapters retain cache, copy, and zero-limit contracts per source."""
    config_before = deepcopy(case.config)
    records = [] if scenario == "negative" else [case.candidate]
    observed = _install_adapter_stub(monkeypatch, _adapter_payload(case, records))

    if scenario == "negative":
        for _ in range(3):
            assert (
                api_generics.search_api_generic_multiple(TITLE, AUTHOR, case.config, case.api_key, venue=case.venue)
                == []
            )
        assert observed["calls"] == 3
        assert adapter_env.get(case.namespace, case.cache_key) == {
            "_negative": True,
            "_confirmations": 3,
            "_safe": True,
        }
        assert (
            api_generics.search_api_generic_multiple(TITLE, AUTHOR, case.config, case.api_key, venue=case.venue) == []
        )
        assert observed["calls"] == 3
    elif scenario == "positive":
        result = api_generics.search_api_generic_multiple(
            TITLE, AUTHOR, case.config, case.api_key, max_results=1, venue=case.venue
        )
        assert result == [case.candidate]
        assert adapter_env.get(case.namespace, case.cache_key) == {"results": [case.candidate]}
        result[0]["id"] = "mutated"
        assert api_generics.search_api_generic_multiple(
            TITLE, AUTHOR, case.config, case.api_key, max_results=1, venue=case.venue
        ) == [case.candidate]
        assert observed["calls"] == 1
    else:
        assert (
            api_generics.search_api_generic_multiple(
                TITLE, AUTHOR, case.config, case.api_key, max_results=0, venue=case.venue
            )
            == []
        )
        assert _query(observed)[case.zero_limit_param] == [case.zero_limit_expected]
    assert case.config == config_before


def test_crossref_venue_uses_compatible_cache_and_08_score_threshold(
    monkeypatch: pytest.MonkeyPatch, adapter_env: ResponseCache
) -> None:
    """A venue candidate between 0.8 and 0.89 is retained under its venue cache key."""
    config = getattr(api_configs, "CROSSREF_VENUE_SEARCH_CONFIG", None)
    assert isinstance(config, APISearchConfig)
    title = "A Study of Neural Networks for Ocean Forecasting"
    candidate = {
        "id": "near-match",
        "title": ["A Study of Neural Networks for Weather Forecasting"],
        "author": [{"given": "Ada", "family": "Lovelace"}],
    }
    monkeypatch.setenv("CROSSREF_MAILTO", "citeforge@example.test")
    observed = _install_adapter_stub(monkeypatch, {"message": {"items": [candidate]}})

    result = api_generics.search_api_generic_multiple(title, AUTHOR, config, max_results=1, venue="Nature")

    assert result == [candidate]
    params = _query(observed)
    assert params["query.container-title"] == ["Nature"]
    assert params["query.bibliographic"] == [title]
    assert params["query.author"] == [AUTHOR]
    assert params["mailto"] == ["citeforge@example.test"]
    assert params["rows"] == ["10"]
    assert adapter_env.get(
        "crossref_venue", "venue|a study of neural networks for ocean forecasting|ada lovelace|nature"
    ) == {"results": [candidate]}

    result[0]["id"] = "mutated"
    assert api_generics.search_api_generic_multiple(title, AUTHOR, config, max_results=1, venue="Nature") == [candidate]
    assert observed["calls"] == 1


def test_openalex_venue_uses_filter_and_scored_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAlex venue search keeps only score-qualified records in score order."""
    config = getattr(api_configs, "OPENALEX_VENUE_SEARCH_CONFIG", None)
    assert isinstance(config, APISearchConfig)
    title = "A Study of Neural Networks for Ocean Forecasting"
    near = {
        "id": "near-match",
        "title": "A Study of Neural Networks for Weather Forecasting",
        "authorships": [{"author": {"display_name": AUTHOR}}],
    }
    exact = {"id": "exact-match", "title": title, "authorships": [{"author": {"display_name": AUTHOR}}]}
    observed = _install_adapter_stub(monkeypatch, {"results": [near, exact]})

    result = api_generics.search_api_generic_multiple(title, AUTHOR, config, max_results=1, venue="Nature")

    assert result == [exact]
    params = _query(observed)
    assert params["search"] == [title]
    assert params["filter"] == ["primary_location.source.display_name.search:Nature"]
    assert params["per-page"] == ["10"]


@pytest.mark.parametrize(
    ("author_name", "expected_title_param", "unexpected_title_param"),
    [(AUTHOR, "query.title", "query.bibliographic"), (None, "query.bibliographic", "query.title")],
)
def test_crossref_candidate_query_uses_its_author_variant(
    monkeypatch: pytest.MonkeyPatch, author_name: str | None, expected_title_param: str, unexpected_title_param: str
) -> None:
    """Crossref chooses its distinct title-plus-author or bibliographic vector."""
    title = "Ocean Forecasting with Neural Networks"
    response = {
        "title": [title],
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "issued": {"date-parts": [[2024]]},
    }
    observed = _install_adapter_stub(monkeypatch, {"message": {"items": [response]}})

    assert api_generics.search_api_generic_multiple(
        title, author_name, api_configs.CROSSREF_SEARCH_CONFIG, year_hint=2024
    ) == [response]
    params = _query(observed)
    assert params[expected_title_param] == [title]
    assert unexpected_title_param not in params
    if author_name:
        assert params["query.author"] == [author_name]
