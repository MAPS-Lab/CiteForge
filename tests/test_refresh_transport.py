from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
import requests

from citeforge.refresh.census import AuthorCensus, AuthorCensusRow
from citeforge.refresh.ledger import Ledger, RequestSpec, TaskSpec
from citeforge.refresh.provider_adapters import JSON_ADAPTERS
from citeforge.refresh.transport import (
    LedgerTransport,
    OutcomeClass,
    ProviderResponse,
    SchemaChangedError,
    ScriptedTransport,
    SendOperation,
    correlate_exact_batch,
)
from citeforge.refresh.types import GenerationSpec, GenerationState, TaskDisposition

NOW = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)


def _response(status: int, body: object = None, *, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = json.dumps(body).encode() if body is not None else b""
    response.url = "https://api.crossref.org/works/example"
    return response


def _request(**changes: object) -> RequestSpec:
    values: dict[str, object] = {
        "provider": "crossref",
        "operation": "lookup",
        "method": "GET",
        "normalized_payload": {"doi": "10.1/example"},
        "requested_fields": ("title",),
        "adapter_version": "1",
        "freshness_epoch": "2026-08",
        "quota_scope": "public",
    }
    values.update(changes)
    return RequestSpec(**values)  # type: ignore[arg-type]


def _ready_ledger(path: Path, request: RequestSpec, *, consumers: int = 1) -> tuple[Ledger, list[TaskSpec]]:
    row = AuthorCensusRow(
        2,
        "author-ada",
        "Ada Lovelace",
        "ada lovelace",
        "",
        "",
        False,
        "transport test",
        TaskDisposition.NOT_APPLICABLE,
    )
    census = AuthorCensus((row,))
    generation = GenerationSpec(census, "policy-v1", {request.provider: request.adapter_version}, "abc123")
    ledger = Ledger.open(path)
    ledger.create_or_resume(generation, census)
    tasks = [
        TaskSpec("author-ada", f"pub-{index}", request.provider, request.operation, request)
        for index in range(consumers)
    ]
    for task in tasks:
        ledger.plan_task(task)
    ledger.seal_plan(tasks)
    ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
    return ledger, tasks


def _operation(
    request: RequestSpec,
    *,
    validator=lambda body: body,
    empty_validator=lambda body: body.get("items") == [],
    idempotent: bool | None = None,
    max_attempts: int = 3,
    headers: dict[str, str] | None = None,
) -> SendOperation:
    return SendOperation(
        request=request,
        url="https://api.crossref.org/works/example?api_key=send-only-secret",
        timeout=5.0,
        headers=headers or {"Authorization": "Bearer send-only-secret"},
        validator=validator,
        empty_validator=empty_validator,
        idempotent=idempotent,
        max_attempts=max_attempts,
    )


def _claim(ledger: Ledger, owner: str, at: datetime = NOW):
    claim = ledger.claim_due(owner, at, timedelta(minutes=5))
    assert claim is not None
    return claim


def test_scripted_transport_is_deterministic() -> None:
    expected = ProviderResponse(TaskDisposition.SUCCEEDED, OutcomeClass.SUCCESS, {"title": "A"}, 200)
    transport = ScriptedTransport([expected])
    assert transport.send(_operation(_request())) == expected
    assert transport.physical_calls == 1
    with pytest.raises(AssertionError, match="exhausted"):
        transport.send(_operation(_request()))


def test_exact_request_key_changes_for_every_semantic_dimension_and_excludes_secrets() -> None:
    base = _request()
    variants = [
        _request(provider="openalex"),
        _request(operation="search"),
        _request(method="POST"),
        _request(normalized_payload={"doi": "10.2/other"}),
        _request(requested_fields=("title", "year")),
        _request(adapter_version="2"),
        _request(freshness_epoch="2026-09"),
        _request(quota_scope="authenticated"),
    ]
    assert len({base.key, *(variant.key for variant in variants)}) == len(variants) + 1
    assert "secret" not in json.dumps(base.canonical_content()).casefold()
    with pytest.raises(ValueError, match="secret"):
        _request(normalized_payload={"api_key": "secret"})


def test_one_physical_send_across_concurrent_exact_consumers(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request, consumers=2)
    calls = 0

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal calls
        calls += 1
        return _response(200, {"title": "Shared"})

    claims = [_claim(ledger, f"worker-{index}") for index in range(2)]

    def execute(claim):
        with Ledger.open(ledger.path) as worker_ledger:
            transport = LedgerTransport(worker_ledger, send_once=sender, clock=lambda: NOW, jitter=lambda _delay: 0.0)
            return transport.send_claim(claim, _operation(request))

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(execute, claims))
    assert calls == 1
    assert {response.disposition for response in responses} <= {TaskDisposition.SUCCEEDED, TaskDisposition.LEASED}
    assert ledger.manifest().data["attempts"] == [
        {
            "finished_at": NOW.isoformat(timespec="microseconds"),
            "http_status": 200,
            "outcome": "success",
            "request_key": request.key,
            "response_digest": hashlib.sha256(b'{"title":"Shared"}').hexdigest(),
            "retry_delay_seconds": None,
            "safe_diagnostic": "validated response",
            "started_at": NOW.isoformat(timespec="microseconds"),
            "number": 1,
        }
    ]
    ledger.close()


def test_restart_reuses_terminal_exact_result_without_send(tmp_path: Path) -> None:
    request = _request()
    path = tmp_path / "ledger.db"
    ledger, _ = _ready_ledger(path, request)
    transport = LedgerTransport(ledger, send_once=lambda _op: _response(200, {"title": "Cached"}), clock=lambda: NOW)
    assert transport.send_claim(_claim(ledger, "first"), _operation(request)).from_ledger is False
    ledger.close()

    reopened = Ledger.open(path)
    reused = LedgerTransport(reopened, send_once=lambda _op: pytest.fail("network called"), clock=lambda: NOW)
    result = reused.result(request.key)
    assert result is not None and result.from_ledger and result.payload == {"title": "Cached"}
    reopened.close()


def test_ledger_transport_implements_claimed_provider_transport_protocol(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    transport = LedgerTransport(ledger, send_once=lambda _op: _response(200, {"title": "Protocol"}), clock=lambda: NOW)
    result = transport.send(_operation(request), task_claim=_claim(ledger, "worker"))
    assert result.disposition is TaskDisposition.SUCCEEDED
    assert result.payload == {"title": "Protocol"}
    ledger.close()


@pytest.mark.parametrize(
    ("scripted", "outcome"),
    [
        (requests.Timeout("token=secret"), OutcomeClass.TIMEOUT),
        (requests.ConnectionError("api_key=secret"), OutcomeClass.CONNECTION_FAILURE),
        (_response(500, {"error": "down"}), OutcomeClass.TRANSIENT_SERVER_ERROR),
        (_response(502, {"error": "down"}), OutcomeClass.TRANSIENT_SERVER_ERROR),
        (_response(503, {"error": "down"}), OutcomeClass.TRANSIENT_SERVER_ERROR),
        (_response(504, {"error": "down"}), OutcomeClass.TRANSIENT_SERVER_ERROR),
    ],
)
def test_transient_outcomes_persist_retry_without_secret(
    tmp_path: Path, scripted: object, outcome: OutcomeClass
) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)

    def sender(_operation: SendOperation) -> requests.Response:
        if isinstance(scripted, BaseException):
            raise scripted
        assert isinstance(scripted, requests.Response)
        return scripted

    response = LedgerTransport(ledger, send_once=sender, clock=lambda: NOW, jitter=lambda _d: 0).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert response.disposition is TaskDisposition.RETRY_WAIT
    assert response.outcome is outcome
    manifest = json.dumps(ledger.manifest().data)
    assert "secret" not in manifest
    assert ledger.manifest().data["attempts"][0]["retry_delay_seconds"] == 1.0
    ledger.close()


@pytest.mark.parametrize("retry_after", ["7", format_datetime(NOW + timedelta(seconds=9), usegmt=True)])
def test_rate_limit_honors_numeric_and_http_date_retry_after(tmp_path: Path, retry_after: str) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    response = LedgerTransport(
        ledger,
        send_once=lambda _op: _response(429, {"error": "rate"}, headers={"Retry-After": retry_after}),
        clock=lambda: NOW,
        jitter=lambda _d: 0,
    ).send_claim(_claim(ledger, "worker"), _operation(request))
    assert response.outcome is OutcomeClass.RATE_LIMITED
    expected = 7.0 if retry_after == "7" else 9.0
    assert response.retry_delay == expected
    assert ledger.manifest().data["attempts"][0]["retry_delay_seconds"] == expected
    ledger.close()


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failures_fail_fast(tmp_path: Path, status: int) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    response = LedgerTransport(ledger, send_once=lambda _op: _response(status, {}), clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert response.disposition is TaskDisposition.AUTHENTICATION_FAILED
    assert response.outcome is OutcomeClass.AUTHENTICATION_FAILURE
    assert len(ledger.manifest().data["attempts"]) == 1
    ledger.close()


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(400, OutcomeClass.INVALID_REQUEST), (404, OutcomeClass.NOT_FOUND), (422, OutcomeClass.INVALID_REQUEST)],
)
def test_permanent_client_errors_fail_fast(tmp_path: Path, status: int, outcome: OutcomeClass) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    response = LedgerTransport(ledger, send_once=lambda _op: _response(status, {}), clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert response.disposition is TaskDisposition.PERMANENT_FAILURE
    assert response.outcome is outcome
    ledger.close()


@pytest.mark.parametrize(
    ("body", "outcome"),
    [(b"not json", OutcomeClass.MALFORMED), (b"[]", OutcomeClass.WRONG_SHAPE)],
)
def test_malformed_and_wrong_shape_json_block(tmp_path: Path, body: bytes, outcome: OutcomeClass) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    raw = _response(200)
    raw._content = body
    response = LedgerTransport(ledger, send_once=lambda _op: raw, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert response.disposition is TaskDisposition.MALFORMED
    assert response.outcome is outcome
    ledger.close()


def test_schema_change_is_distinct_from_authoritative_empty(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "schema.db", request)

    def schema_validator(_body: dict[str, object]) -> dict[str, object]:
        raise SchemaChangedError("missing message.items")

    changed = LedgerTransport(ledger, send_once=lambda _op: _response(200, {"unexpected": []}), clock=lambda: NOW)
    response = changed.send_claim(_claim(ledger, "worker"), _operation(request, validator=schema_validator))
    assert response.disposition is TaskDisposition.SCHEMA_CHANGED
    assert response.outcome is OutcomeClass.SCHEMA_CHANGED
    ledger.close()

    ledger, _ = _ready_ledger(tmp_path / "empty.db", request)
    empty = LedgerTransport(ledger, send_once=lambda _op: _response(200, {"items": []}), clock=lambda: NOW)
    response = empty.send_claim(_claim(ledger, "worker"), _operation(request))
    assert response.disposition is TaskDisposition.CONFIRMED_EMPTY
    assert response.outcome is OutcomeClass.AUTHORITATIVE_EMPTY
    assert response.payload == {}
    ledger.close()


def test_retry_exhaustion_becomes_blocking(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    transport = LedgerTransport(
        ledger, send_once=lambda _op: _response(500, {}), clock=lambda: NOW, jitter=lambda _d: 0
    )
    operation = _operation(request, max_attempts=2)
    first = transport.send_claim(_claim(ledger, "one"), operation)
    assert first.disposition is TaskDisposition.RETRY_WAIT
    second_at = NOW + timedelta(seconds=1)
    transport.clock = lambda: second_at
    second = transport.send_claim(_claim(ledger, "two", second_at), operation)
    assert second.disposition is TaskDisposition.BLOCKED
    assert second.outcome is OutcomeClass.RETRY_EXHAUSTED
    assert len(ledger.manifest().data["attempts"]) == 2
    ledger.close()


@pytest.mark.parametrize("idempotent", [False, None])
def test_post_without_proven_idempotency_never_retries(tmp_path: Path, idempotent: bool | None) -> None:
    request = _request(method="POST")
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    response = LedgerTransport(ledger, send_once=lambda _op: _response(503, {}), clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request, idempotent=idempotent)
    )
    assert response.disposition is TaskDisposition.AMBIGUOUS
    assert response.outcome is OutcomeClass.AMBIGUOUS_PARTIAL
    ledger.close()


def test_idempotent_post_may_retry(tmp_path: Path) -> None:
    request = _request(method="POST")
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    response = LedgerTransport(ledger, send_once=lambda _op: _response(503, {}), clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request, idempotent=True)
    )
    assert response.disposition is TaskDisposition.RETRY_WAIT
    ledger.close()


def test_response_digest_is_of_normalized_mapping(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    response = LedgerTransport(
        ledger, send_once=lambda _op: _response(200, {"title": "A", "ignored": 1}), clock=lambda: NOW
    ).send_claim(_claim(ledger, "worker"), _operation(request, validator=lambda body: {"title": body["title"]}))
    assert response.response_digest == hashlib.sha256(b'{"title":"A"}').hexdigest()
    assert ledger.manifest().data["attempts"][0]["response_digest"] == response.response_digest
    ledger.close()


def test_stale_lease_never_sends(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    claim = ledger.claim_due("old", NOW, timedelta(seconds=1))
    assert claim
    transport = LedgerTransport(
        ledger, send_once=lambda _op: pytest.fail("network called"), clock=lambda: NOW + timedelta(seconds=2)
    )
    with pytest.raises(ValueError, match="stale"):
        transport.send_claim(claim, _operation(request))
    ledger.close()


@pytest.mark.parametrize(
    "members",
    [
        [{"id": "a", "title": "A"}],
        [{"id": "a"}, {"id": "a"}],
        [{"id": "a"}, {"id": "b"}, {"id": "extra"}],
        [{"title": "A"}, {"id": "b"}],
    ],
)
def test_batch_correlation_fails_closed_for_missing_duplicate_unexpected_or_malformed(
    members: list[dict[str, str]],
) -> None:
    with pytest.raises(ValueError, match="batch"):
        correlate_exact_batch(("a", "b"), members, correlation_field="id")


def test_batch_correlation_accepts_reordered_exact_members() -> None:
    correlated = correlate_exact_batch(
        ("a", "b"), [{"id": "b", "title": "B"}, {"id": "a", "title": "A"}], correlation_field="id"
    )
    assert list(correlated) == ["a", "b"]


@pytest.mark.parametrize(
    ("adapter_name", "envelope", "field"),
    [
        ("semantic_scholar.search", {"data": [{"paperId": "s2"}]}, "results"),
        ("crossref.search", {"message": {"items": [{"DOI": "10.1/x"}]}}, "results"),
        ("openalex.search", {"results": [{"id": "W1"}]}, "results"),
        ("europepmc.search", {"resultList": {"result": [{"id": "1"}]}}, "results"),
        ("serply.scholar", {"articles": [{"id": "1"}]}, "articles"),
        ("serpapi.author", {"articles": [{"citation_id": "1"}]}, "articles"),
        ("dblp.author_search", {"result": {"hits": {"hit": [{"info": {"pid": "1"}}]}}}, "hits"),
        ("pubmed.search", {"esearchresult": {"idlist": ["1"]}}, "pmids"),
        ("pubmed.summary", {"result": {"1": {"uid": "1"}}}, "records"),
        ("openreview.notes", {"notes": [{"id": "1"}]}, "notes"),
        ("gemini.short_title", {"candidates": [{"content": {}}]}, "candidates"),
        ("doi.csl", {"title": "A", "DOI": "10.1/x"}, "metadata"),
    ],
)
def test_every_json_provider_has_one_transport_adapter(
    adapter_name: str, envelope: dict[str, object], field: str
) -> None:
    adapter = JSON_ADAPTERS[adapter_name]
    normalized = adapter.normalize(envelope)
    transport = ScriptedTransport([ProviderResponse(TaskDisposition.SUCCEEDED, OutcomeClass.SUCCESS, normalized, 200)])
    operation = adapter.build_operation(
        url="https://provider.invalid/resource",
        normalized_payload={"author_scope": "author-ada", "query": "safe"},
        freshness_epoch="2026-08",
        adapter_version="1",
        quota_scope="public",
        timeout=5,
        headers={"Authorization": "secret-at-send-only"},
        idempotent=adapter.method != "POST",
    )
    assert adapter.send(transport, operation)[field]
    assert "secret" not in json.dumps(operation.request.canonical_content()).casefold()


@pytest.mark.parametrize("adapter_name", sorted(JSON_ADAPTERS))
def test_every_json_provider_adapter_rejects_wrong_envelope(adapter_name: str) -> None:
    with pytest.raises(SchemaChangedError):
        JSON_ADAPTERS[adapter_name].normalize({"unexpected": []})


def test_adapter_provider_names_match_existing_merge_and_quota_namespaces() -> None:
    assert JSON_ADAPTERS["semantic_scholar.search"].provider == "s2"
    assert JSON_ADAPTERS["doi.csl"].provider == "doi_csl"
    assert JSON_ADAPTERS["crossref.search"].provider == "crossref"
    assert JSON_ADAPTERS["openalex.search"].provider == "openalex"
    operation = JSON_ADAPTERS["doi.csl"].build_operation(
        url="https://doi.org/10.1/x",
        normalized_payload={"doi": "10.1/x"},
        freshness_epoch="2026-08",
        adapter_version="1",
        timeout=5,
    )
    assert operation.request.quota_scope == "doi"
