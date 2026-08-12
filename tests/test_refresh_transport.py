from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
import requests

from citeforge import api_generics
from citeforge.api_configs import S2_SEARCH_CONFIG
from citeforge.cache import ResponseCache
from citeforge.refresh.census import AuthorCensus, AuthorCensusRow
from citeforge.refresh.ledger import Ledger, ProviderObservation, RequestSpec, TaskSpec
from citeforge.refresh.provider_adapters import JSON_ADAPTERS, pubmed_summary_adapter
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
from citeforge.text_utils import build_url

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
    idempotency_header: str | None = None,
    idempotency_key: str | None = None,
) -> SendOperation:
    return SendOperation(
        request=request,
        url="https://api.crossref.org/works/example?api_key=send-only-secret",
        timeout=5.0,
        headers=headers or {"Authorization": "Bearer send-only-secret"},
        validator=validator,
        empty_validator=empty_validator,
        idempotent=idempotent,
        idempotency_header=idempotency_header,
        idempotency_key=idempotency_key,
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


def test_provider_response_is_deeply_immutable() -> None:
    response = ProviderResponse(
        TaskDisposition.SUCCEEDED,
        OutcomeClass.SUCCESS,
        {"nested": {"items": [{"title": "A"}]}},
    )
    with pytest.raises(TypeError):
        response.payload["nested"]["items"][0]["title"] = "mutated"  # type: ignore[index, union-attr]


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


def test_in_flight_consumer_eventually_terminalizes_without_second_send(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request, consumers=2)
    first_claim = _claim(ledger, "first")
    second_claim = _claim(ledger, "second")
    request_claim = ledger.claim_request(first_claim.key, "first", NOW, timedelta(minutes=5))
    assert request_claim is not None
    second_transport = LedgerTransport(ledger, send_once=lambda _op: pytest.fail("network called"), clock=lambda: NOW)
    in_flight = second_transport.send(_operation(request), task_claim=second_claim)
    assert in_flight.outcome is OutcomeClass.IN_FLIGHT
    ledger.record_attempt(request.key, "first", NOW, NOW, "success", http_status=200)
    ledger.finish_request(
        request.key,
        "first",
        TaskDisposition.SUCCEEDED,
        NOW,
        observation=ProviderObservation("crossref", "1", {"title": "Shared"}),
    )
    ledger.finish_task(first_claim.key, "first", TaskDisposition.SUCCEEDED, NOW)
    completed = second_transport.send(_operation(request), task_claim=second_claim)
    assert completed.disposition is TaskDisposition.SUCCEEDED
    assert completed.payload == {"title": "Shared"}
    assert len(ledger.manifest().data["attempts"]) == 1
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


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(401, OutcomeClass.AUTHENTICATION_FAILURE), (404, OutcomeClass.NOT_FOUND), (422, OutcomeClass.INVALID_REQUEST)],
)
def test_restart_preserves_exact_terminal_outcome_and_status(
    tmp_path: Path, status: int, outcome: OutcomeClass
) -> None:
    request = _request()
    path = tmp_path / "ledger.db"
    ledger, _ = _ready_ledger(path, request)
    first = LedgerTransport(ledger, send_once=lambda _op: _response(status, {}), clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert first.outcome is outcome and first.status == status
    ledger.close()
    with Ledger.open(path) as reopened:
        reused = LedgerTransport(
            reopened, send_once=lambda _op: pytest.fail("network called"), clock=lambda: NOW
        ).result(request.key)
        assert reused is not None and reused.outcome is outcome and reused.status == status


def test_ledger_transport_implements_claimed_provider_transport_protocol(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    transport = LedgerTransport(ledger, send_once=lambda _op: _response(200, {"title": "Protocol"}), clock=lambda: NOW)
    result = transport.send(_operation(request), task_claim=_claim(ledger, "worker"))
    assert result.disposition is TaskDisposition.SUCCEEDED
    assert result.payload == {"title": "Protocol"}
    ledger.close()


def test_generic_search_uses_stable_author_key_and_real_ledger_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = {"query": '"Ocean Forecasting" Ada Lovelace', **S2_SEARCH_CONFIG.additional_params, "limit": 2}
    operation = api_generics.search_operation(
        build_url(S2_SEARCH_CONFIG.base_url, params),
        S2_SEARCH_CONFIG,
        author_scope="author-ada",
        freshness_epoch="2026-08",
        adapter_version="1",
        api_key="send-secret",
    )
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", operation.request)
    calls = 0

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal calls
        calls += 1
        return _response(
            200, {"data": [{"paperId": "s2", "title": "Ocean Forecasting", "authors": [{"name": "Ada Lovelace"}]}]}
        )

    transport = LedgerTransport(
        ledger,
        send_once=sender,
        clock=lambda: NOW,
    )
    cache = ResponseCache(str(tmp_path / "cache"))
    cache.put(
        "semantic_scholar",
        "multi|ocean forecasting|ada lovelace",
        {"results": [{"paperId": "stale", "title": "Stale"}]},
    )
    monkeypatch.setattr(api_generics, "response_cache", cache)
    result = api_generics.search_api_generic_multiple(
        "Ocean Forecasting",
        "Ada Lovelace",
        S2_SEARCH_CONFIG,
        "send-secret",
        max_results=1,
        transport=transport,
        task_claim=_claim(ledger, "worker"),
        author_key="author-ada",
        freshness_epoch="2026-08",
    )
    assert result[0]["paperId"] == "s2"
    assert calls == 1
    assert len(ledger.manifest().data["attempts"]) == 1
    assert operation.request.canonical_content()["normalized_payload"]["author_scope"] == "author-ada"
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


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (requests.exceptions.InvalidURL("https://example.test/?api_key=secret"), OutcomeClass.INVALID_REQUEST),
        (requests.RequestException("token=secret"), OutcomeClass.INVALID_REQUEST),
    ],
)
def test_every_requests_exception_is_durable_and_secret_safe(
    tmp_path: Path, raised: requests.RequestException, expected: OutcomeClass
) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)

    def sender(_operation: SendOperation) -> requests.Response:
        raise raised

    result = LedgerTransport(ledger, send_once=sender, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert result.outcome is expected
    manifest = json.dumps(ledger.manifest().data)
    assert "secret" not in manifest
    assert len(ledger.manifest().data["attempts"]) == 1
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


@pytest.mark.parametrize("body", [b'{"title": NaN}', b'{"title": Infinity}', b'{"title": -Infinity}'])
def test_nonfinite_json_is_malformed_and_durable(tmp_path: Path, body: bytes) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    raw = _response(200)
    raw._content = body
    result = LedgerTransport(ledger, send_once=lambda _op: raw, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert result.outcome is OutcomeClass.MALFORMED
    assert len(ledger.manifest().data["attempts"]) == 1
    ledger.close()


@pytest.mark.parametrize("phase", ["validator", "empty_validator"])
def test_unexpected_envelope_callback_failure_is_durable_schema_change(tmp_path: Path, phase: str) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)

    def broken(_body: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("api_key=secret")

    kwargs = {"validator": broken} if phase == "validator" else {"empty_validator": broken}
    result = LedgerTransport(
        ledger, send_once=lambda _op: _response(200, {"title": "A"}), clock=lambda: NOW
    ).send_claim(_claim(ledger, "worker"), _operation(request, **kwargs))
    assert result.outcome is OutcomeClass.SCHEMA_CHANGED
    assert len(ledger.manifest().data["attempts"]) == 1
    assert "secret" not in json.dumps(ledger.manifest().data)
    ledger.close()


def test_invalid_normalized_evidence_is_durable_malformed(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    result = LedgerTransport(
        ledger, send_once=lambda _op: _response(200, {"title": "A"}), clock=lambda: NOW
    ).send_claim(_claim(ledger, "worker"), _operation(request, validator=lambda _body: {"api_key": "secret"}))
    assert result.outcome is OutcomeClass.MALFORMED
    assert len(ledger.manifest().data["attempts"]) == 1
    assert "secret" not in json.dumps(ledger.manifest().data)
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
        _claim(ledger, "worker"),
        _operation(
            request,
            idempotent=True,
            headers={"Idempotency-Key": "operation-123"},
            idempotency_header="Idempotency-Key",
            idempotency_key="operation-123",
        ),
    )
    assert response.disposition is TaskDisposition.RETRY_WAIT
    ledger.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"idempotent": True},
        {"idempotent": True, "idempotency_header": "Idempotency-Key", "idempotency_key": "operation-123"},
        {
            "idempotent": True,
            "headers": {"Idempotency-Key": "different"},
            "idempotency_header": "Idempotency-Key",
            "idempotency_key": "operation-123",
        },
    ],
)
def test_post_retry_requires_matching_transmitted_idempotency_header(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="idempotency"):
        _operation(_request(method="POST"), **kwargs)


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


def test_pubmed_esummary_real_envelope_has_exact_member_correlation() -> None:
    adapter = pubmed_summary_adapter(("123", "456"))
    envelope = {
        "result": {
            "uids": ["456", "123"],
            "123": {"uid": "123", "title": "A"},
            "456": {"uid": "456", "title": "B"},
        }
    }
    assert list(adapter.normalize(envelope)["records"]) == ["123", "456"]


@pytest.mark.parametrize("uids", [["123"], ["123", "123"], ["123", "456", "789"]])
def test_pubmed_esummary_missing_duplicate_or_unexpected_members_fail_closed(uids: list[str]) -> None:
    adapter = pubmed_summary_adapter(("123", "456"))
    envelope = {
        "result": {
            "uids": uids,
            "123": {"uid": "123"},
            "456": {"uid": "456"},
            "789": {"uid": "789"},
        }
    }
    with pytest.raises(SchemaChangedError, match="PubMed"):
        adapter.normalize(envelope)


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
