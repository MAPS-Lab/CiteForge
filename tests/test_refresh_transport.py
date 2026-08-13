from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from citeforge import api_generics
from citeforge.api_configs import S2_SEARCH_CONFIG
from citeforge.cache import ResponseCache
from citeforge.refresh.capabilities import GEMINI_GENERATION_CONFIG, GEMINI_MODEL_ID, GEMINI_PROMPT_VERSION
from citeforge.refresh.census import AuthorCensus, AuthorCensusRow
from citeforge.refresh.ledger import Ledger, ProviderObservation, RequestClaim, RequestSpec, TaskClaim, TaskSpec
from citeforge.refresh.provider_adapters import JSON_ADAPTERS, JSON_DURABLE_CALLSITES, pubmed_summary_adapter
from citeforge.refresh.transport import (
    LedgerTransport,
    OutcomeClass,
    ProviderResponse,
    RawProviderResponse,
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


@pytest.mark.parametrize("payload", [{"bad": {1, 2}}, {"bad": object()}, {"bad": ("tuple",)}, {"bad": float("nan")}])
def test_provider_response_rejects_non_json_mutable_or_nonfinite_values(payload: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError), match="JSON"):
        ProviderResponse(TaskDisposition.SUCCEEDED, OutcomeClass.SUCCESS, payload)


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_send_operation_rejects_unsupported_wire_methods_before_claim(method: str) -> None:
    with pytest.raises(ValueError, match="wire method"):
        _operation(_request(method=method))


def test_head_is_a_supported_retryable_wire_method() -> None:
    assert _operation(_request(method="HEAD")).retryable


def test_durable_response_is_streamed_bounded_and_closed_before_decode(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    raw = _response(200)
    raw.headers["Content-Type"] = "application/json"
    chunks = iter((b'{"items":[', b'{"title":"A"}', b"]}"))
    close_count = 0
    decode_count = 0

    def iter_content(chunk_size: int):
        assert chunk_size <= 65_536
        yield from chunks

    def close() -> None:
        nonlocal close_count
        close_count += 1

    raw.iter_content = iter_content  # type: ignore[method-assign]
    raw.close = close  # type: ignore[method-assign]

    def decoder(response: RawProviderResponse) -> tuple[dict[str, object], bool]:
        nonlocal decode_count
        decode_count += 1
        assert close_count == 1
        return {"items": json.loads(response.body)["items"], "title": "A"}, False

    operation = SendOperation(
        request,
        "https://api.crossref.org/works",
        5,
        lambda _body: {},
        lambda _body: False,
        response_decoder=decoder,
        decoder_schema="test-v1",
        max_body_bytes=64,
    )
    result = LedgerTransport(ledger, send_once=lambda _operation: raw, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), operation
    )
    assert result.disposition is TaskDisposition.SUCCEEDED
    assert close_count == 1
    assert decode_count == 1
    ledger.close()


def test_success_clock_is_reacquired_after_decoder(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    raw = _response(200)
    raw.headers["Content-Type"] = "application/json"
    instants = iter((NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2), NOW + timedelta(seconds=3)))
    decoded = False

    def clock() -> datetime:
        instant = next(instants)
        if instant >= NOW + timedelta(seconds=2):
            assert decoded
        return instant

    def decoder(_raw: RawProviderResponse) -> tuple[dict[str, object], bool]:
        nonlocal decoded
        decoded = True
        return {"title": "A"}, False

    operation = SendOperation(
        request,
        "https://provider.test/resource",
        5,
        lambda _body: {},
        lambda _body: False,
        response_decoder=decoder,
        decoder_schema="test-v1",
    )
    result = LedgerTransport(ledger, send_once=lambda _operation: raw, clock=clock).send_claim(
        _claim(ledger, "worker"), operation
    )
    assert result.disposition is TaskDisposition.SUCCEEDED
    ledger.close()


@pytest.mark.parametrize("broken_attribute", ["headers", "url"])
def test_typed_response_metadata_failure_closes_and_terminalizes_one_attempt(
    tmp_path: Path, broken_attribute: str
) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)

    class BrokenMetadataResponse(requests.Response):
        @property
        def headers(self):  # type: ignore[override]
            if broken_attribute == "headers":
                raise ValueError("secret metadata")
            return self.__dict__.setdefault("_safe_headers", requests.structures.CaseInsensitiveDict())

        @headers.setter
        def headers(self, value):  # type: ignore[override]
            self.__dict__["_safe_headers"] = value

        @property
        def url(self):  # type: ignore[override]
            if broken_attribute == "url":
                raise ValueError("secret metadata")
            return "https://provider.test/resource"

        @url.setter
        def url(self, value):  # type: ignore[override]
            self.__dict__["_safe_url"] = value

    raw = BrokenMetadataResponse()
    raw.status_code = 200
    raw._content = b"{}"
    close_count = 0

    def close() -> None:
        nonlocal close_count
        close_count += 1

    raw.close = close  # type: ignore[method-assign]
    operation = SendOperation(
        request,
        "https://provider.test/resource",
        5,
        lambda _body: {},
        lambda _body: False,
        response_decoder=lambda _raw: ({"title": "never"}, False),
        decoder_schema="test-v1",
    )
    result = LedgerTransport(ledger, send_once=lambda _operation: raw, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), operation
    )
    assert result.disposition is TaskDisposition.MALFORMED
    assert close_count == 1
    assert len(ledger.manifest().data["attempts"]) == 1
    ledger.close()


def test_invalid_status_access_closes_and_terminalizes_claim(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)

    class BrokenStatusResponse(requests.Response):
        @property
        def status_code(self):  # type: ignore[override]
            raise ValueError("invalid status")

        @status_code.setter
        def status_code(self, value):  # type: ignore[override]
            self.__dict__["_status"] = value

    raw = BrokenStatusResponse()
    close_count = 0

    def close() -> None:
        nonlocal close_count
        close_count += 1

    raw.close = close  # type: ignore[method-assign]
    result = LedgerTransport(ledger, send_once=lambda _operation: raw, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert result.disposition is TaskDisposition.PERMANENT_FAILURE
    assert close_count == 1
    assert ledger.request_result(request.key) is not None
    ledger.close()


@pytest.mark.parametrize("content_type", ["application/json", "application/xml", "application/x-bibtex", "text/html"])
def test_oversized_typed_response_is_one_malformed_attempt_before_decoder(tmp_path: Path, content_type: str) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    raw = _response(200)
    raw.headers["Content-Type"] = content_type
    raw._content = b"x" * 33
    raw.iter_content = lambda chunk_size: iter((raw._content,))  # type: ignore[method-assign]
    raw.close = lambda: None  # type: ignore[method-assign]
    decoded = False

    def decoder(_response: RawProviderResponse) -> tuple[dict[str, object], bool]:
        nonlocal decoded
        decoded = True
        return {}, False

    operation = SendOperation(
        request,
        "https://provider.test/resource",
        5,
        lambda _body: {},
        lambda _body: False,
        response_decoder=decoder,
        decoder_schema="test-v1",
        max_body_bytes=32,
    )
    result = LedgerTransport(ledger, send_once=lambda _operation: raw, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), operation
    )
    assert result.disposition is TaskDisposition.MALFORMED
    assert result.outcome is OutcomeClass.MALFORMED
    assert not decoded
    assert len(ledger.manifest().data["attempts"]) == 1
    ledger.close()


def test_unexpected_send_callback_exception_terminalizes_claim_and_attempt(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    response = LedgerTransport(
        ledger,
        send_once=lambda _operation: (_ for _ in ()).throw(ValueError("secret=https://x.test?k=credential")),
        clock=lambda: NOW,
    ).send_claim(_claim(ledger, "worker"), _operation(request))
    assert response.disposition is TaskDisposition.PERMANENT_FAILURE
    assert response.outcome is OutcomeClass.INVALID_REQUEST
    manifest = ledger.manifest().data
    assert len(manifest["attempts"]) == 1
    assert "credential" not in json.dumps(manifest).casefold()
    assert ledger.request_result(request.key).disposition is TaskDisposition.PERMANENT_FAILURE
    ledger.close()


def test_invalid_send_callback_response_terminalizes_claim_and_attempt(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    response = LedgerTransport(ledger, send_once=lambda _operation: object(), clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert response.disposition is TaskDisposition.MALFORMED
    assert response.outcome is OutcomeClass.MALFORMED
    assert len(ledger.manifest().data["attempts"]) == 1
    ledger.close()


def test_unexpected_response_decode_exception_terminalizes_attempt(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    raw = _response(200)
    raw._content = False
    raw._content_consumed = True
    response = LedgerTransport(ledger, send_once=lambda _operation: raw, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert response.disposition is TaskDisposition.MALFORMED
    assert response.outcome is OutcomeClass.MALFORMED
    assert len(ledger.manifest().data["attempts"]) == 1
    ledger.close()


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
            200,
            {
                "total": 1,
                "data": [{"paperId": "s2", "title": "Ocean Forecasting", "authors": [{"name": "Ada Lovelace"}]}],
            },
            headers={"Content-Type": "application/json"},
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
    assert operation.request.canonical_content()["normalized_payload"]["query"] == '"Ocean Forecasting" Ada Lovelace'
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


class _RaisingHeaders(dict[str, str]):
    def get(self, _key: str, _default: object = None) -> str | None:
        raise RuntimeError("token=secret")


@pytest.mark.parametrize("failure", ["jitter", "retry_headers", "status_none"])
def test_post_claim_retry_helper_failure_terminalizes_exactly_once(tmp_path: Path, failure: str) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    raw = _response(500, {"error": "down"})
    jitter_called = False

    def jitter(_delay: float) -> float:
        nonlocal jitter_called
        jitter_called = True
        if failure == "jitter":
            raise RuntimeError("token=secret")
        return 0.0

    if failure == "retry_headers":
        raw.status_code = 429
        raw.headers = _RaisingHeaders()
    elif failure == "status_none":
        raw.status_code = None
    response = LedgerTransport(ledger, send_once=lambda _operation: raw, clock=lambda: NOW, jitter=jitter).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert response.disposition is TaskDisposition.PERMANENT_FAILURE
    assert response.safe_diagnostic == "provider response classification failed"
    assert len(ledger.manifest().data["attempts"]) == 1
    assert "secret" not in json.dumps(ledger.manifest().data).casefold()
    assert ledger.request_result(request.key).disposition is TaskDisposition.PERMANENT_FAILURE
    assert jitter_called is (failure == "jitter")
    ledger.close()


def test_initial_clock_failure_terminalizes_claim_with_claim_safe_time(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    sent = False

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal sent
        sent = True
        return _response(200, {"title": "unexpected"})

    response = LedgerTransport(
        ledger,
        send_once=sender,
        clock=lambda: (_ for _ in ()).throw(RuntimeError("api_key=secret")),
    ).send_claim(_claim(ledger, "worker", datetime.now(timezone.utc)), _operation(request))
    assert not sent
    assert response.disposition is TaskDisposition.PERMANENT_FAILURE
    assert response.safe_diagnostic == "provider response classification failed"
    assert len(ledger.manifest().data["attempts"]) == 0
    assert "secret" not in json.dumps(ledger.manifest().data).casefold()
    assert ledger.request_result(request.key).disposition is TaskDisposition.PERMANENT_FAILURE
    manifest = ledger.manifest().data
    assert manifest["tasks"][0]["state"] == TaskDisposition.PERMANENT_FAILURE.value
    assert manifest["requests"][0]["state"] == TaskDisposition.PERMANENT_FAILURE.value
    ledger.close()


def test_stale_initial_clock_claim_cannot_rewind_or_mutate_durable_state(tmp_path: Path) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    physical_calls = 0

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal physical_calls
        physical_calls += 1
        return _response(200, {"title": "unexpected"})

    stale_claim = _claim(ledger, "stale-worker", datetime.now(timezone.utc) - timedelta(minutes=10))
    response = LedgerTransport(
        ledger,
        send_once=sender,
        clock=lambda: (_ for _ in ()).throw(RuntimeError("api_key=secret")),
    ).send_claim(stale_claim, _operation(request))
    assert physical_calls == 0
    assert response.disposition is TaskDisposition.LEASED
    assert response.outcome is OutcomeClass.IN_FLIGHT
    assert response.safe_diagnostic == "task claim expired before provider classification"
    manifest = ledger.manifest().data
    assert manifest["tasks"][0]["state"] == TaskDisposition.LEASED.value
    assert manifest["requests"][0]["state"] == TaskDisposition.PENDING.value
    assert manifest["attempts"] == []
    reclaimed = ledger.claim_due("recovery-worker", datetime.now(timezone.utc), timedelta(minutes=5))
    assert reclaimed is not None and reclaimed.owner == "recovery-worker"
    ledger.close()


def test_claim_safe_fallback_uses_earlier_request_lease_bound() -> None:
    task_claim = TaskClaim("a" * 64, "b" * 64, "worker", NOW + timedelta(minutes=10))
    request_claim = RequestClaim("b" * 64, "worker", NOW + timedelta(minutes=2))
    assert LedgerTransport._claim_safe_time(task_claim, request_claim) == (
        request_claim.lease_expires - timedelta(microseconds=1)
    )


@pytest.mark.parametrize("fail_on_call", [2, 3])
def test_post_claim_clock_failure_terminalizes_exactly_once(tmp_path: Path, fail_on_call: int) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    calls = 0
    physical_calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        if calls == fail_on_call:
            raise RuntimeError("api_key=secret")
        return NOW

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal physical_calls
        physical_calls += 1
        return _response(500, {"error": "down"})

    response = LedgerTransport(ledger, send_once=sender, clock=clock, jitter=lambda _d: 0).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert physical_calls == 1
    assert response.disposition is TaskDisposition.PERMANENT_FAILURE
    assert response.safe_diagnostic == "provider response classification failed"
    assert len(ledger.manifest().data["attempts"]) == 1
    assert "secret" not in json.dumps(ledger.manifest().data).casefold()
    assert ledger.request_result(request.key).disposition is TaskDisposition.PERMANENT_FAILURE
    manifest = ledger.manifest().data
    assert manifest["tasks"][0]["state"] == TaskDisposition.PERMANENT_FAILURE.value
    assert manifest["requests"][0]["state"] == TaskDisposition.PERMANENT_FAILURE.value
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


def test_doi_redirect_invalid_url_is_terminalized_as_one_attempt_per_hop(tmp_path: Path) -> None:
    adapter = JSON_ADAPTERS["doi.csl"]
    operation = adapter.build_operation(
        url="https://doi.org/10.1/x",
        normalized_payload={"doi": "10.1/x"},
        freshness_epoch="2026-08",
        adapter_version="1",
        timeout=5,
        headers={"Accept": "application/vnd.citationstyles.csl+json"},
    )
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", operation.request)
    calls = 0

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response(302, headers={"Location": "https://api.crossref.org/works/10.1/x"})
        raise requests.exceptions.InvalidURL("invalid redirect target")

    result = LedgerTransport(ledger, send_once=sender, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), operation
    )
    assert result.outcome is OutcomeClass.INVALID_REQUEST
    assert result.disposition is TaskDisposition.PERMANENT_FAILURE
    assert calls == 2
    assert len(ledger.manifest().data["attempts"]) == 2
    ledger.close()


def test_redirect_chain_never_exceeds_capability_physical_attempt_bound(tmp_path: Path) -> None:
    adapter = JSON_ADAPTERS["doi.csl"]
    operation = adapter.build_operation(
        url="https://doi.org/10.1/x",
        normalized_payload={"doi": "10.1/x"},
        freshness_epoch="2026-08",
        adapter_version="1",
        timeout=5,
        headers={"Accept": "application/vnd.citationstyles.csl+json"},
    )
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", operation.request)
    calls = 0

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal calls
        calls += 1
        return _response(302, headers={"Location": "https://api.crossref.org/works/10.1/x"})

    result = LedgerTransport(ledger, send_once=sender, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), operation
    )
    assert result.disposition is TaskDisposition.BLOCKED
    assert result.outcome is OutcomeClass.RETRY_EXHAUSTED
    assert calls == 3
    assert len(ledger.manifest().data["attempts"]) == 3
    ledger.close()


@pytest.mark.parametrize(
    ("second_result", "expected_outcome", "expected_disposition"),
    [
        (object(), OutcomeClass.MALFORMED, TaskDisposition.MALFORMED),
        (RuntimeError("api_key=secret"), OutcomeClass.INVALID_REQUEST, TaskDisposition.PERMANENT_FAILURE),
        (requests.Timeout("late timeout"), OutcomeClass.TIMEOUT, TaskDisposition.RETRY_WAIT),
        (
            requests.ConnectionError("late connection"),
            OutcomeClass.CONNECTION_FAILURE,
            TaskDisposition.RETRY_WAIT,
        ),
    ],
)
def test_every_later_doi_redirect_hop_uses_the_physical_send_classifier(
    tmp_path: Path,
    second_result: object,
    expected_outcome: OutcomeClass,
    expected_disposition: TaskDisposition,
) -> None:
    adapter = JSON_ADAPTERS["doi.csl"]
    operation = adapter.build_operation(
        url="https://doi.org/10.1/x",
        normalized_payload={"doi": "10.1/x"},
        freshness_epoch="2026-08",
        adapter_version="1",
        timeout=5,
        headers={"Accept": "application/vnd.citationstyles.csl+json"},
    )
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", operation.request)
    calls = 0

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response(302, headers={"Location": "https://api.crossref.org/works/10.1/x"})
        if isinstance(second_result, BaseException):
            raise second_result
        return second_result  # type: ignore[return-value]

    result = LedgerTransport(ledger, send_once=sender, clock=lambda: NOW, jitter=lambda _delay: 0).send_claim(
        _claim(ledger, "worker"), operation
    )
    assert result.outcome is expected_outcome
    assert result.disposition is expected_disposition
    manifest = ledger.manifest().data
    assert calls == 2
    assert len(manifest["attempts"]) == 2
    assert manifest["physical_send_markers"][0]["resolved_at"] is not None
    assert "secret" not in json.dumps(manifest).casefold()
    ledger.close()


@pytest.mark.parametrize("location", ["https://[", "https://api.crossref.org:bad/works/10.1/x"])
def test_malformed_doi_redirect_location_terminalizes_current_hop(tmp_path: Path, location: str) -> None:
    adapter = JSON_ADAPTERS["doi.csl"]
    operation = adapter.build_operation(
        url="https://doi.org/10.1/x",
        normalized_payload={"doi": "10.1/x"},
        freshness_epoch="2026-08",
        adapter_version="1",
        timeout=5,
        headers={"Accept": "application/vnd.citationstyles.csl+json"},
    )
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", operation.request)
    result = LedgerTransport(
        ledger, send_once=lambda _operation: _response(302, headers={"Location": location}), clock=lambda: NOW
    ).send_claim(_claim(ledger, "worker"), operation)
    assert result.outcome is OutcomeClass.INVALID_REQUEST
    assert result.disposition is TaskDisposition.PERMANENT_FAILURE
    manifest = ledger.manifest().data
    assert len(manifest["attempts"]) == 1
    assert manifest["physical_send_markers"][0]["resolved_at"] is not None
    ledger.close()


def test_later_doi_redirect_system_exit_preserves_exact_crash_marker(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    adapter = JSON_ADAPTERS["doi.csl"]
    operation = adapter.build_operation(
        url="https://doi.org/10.1/x",
        normalized_payload={"doi": "10.1/x"},
        freshness_epoch="2026-08",
        adapter_version="1",
        timeout=5,
        headers={"Accept": "application/vnd.citationstyles.csl+json"},
    )
    ledger, _ = _ready_ledger(path, operation.request)
    calls = 0

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response(302, headers={"Location": "https://api.crossref.org/works/10.1/x"})
        raise SystemExit("crash on later hop")

    with pytest.raises(SystemExit):
        LedgerTransport(ledger, send_once=sender, clock=lambda: NOW).send_claim(_claim(ledger, "worker"), operation)
    manifest = ledger.manifest().data
    assert calls == 2
    assert len(manifest["attempts"]) == 1
    assert manifest["physical_send_markers"][0]["resolved_at"] is None
    ledger.close()


def test_redirect_crash_resume_starts_at_durable_target_without_repeating_origin(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    target = "https://api.crossref.org/works/10.1/x"
    adapter = JSON_ADAPTERS["doi.csl"]
    operation = adapter.build_operation(
        url="https://doi.org/10.1/x",
        normalized_payload={"doi": "10.1/x"},
        freshness_epoch="2026-08",
        adapter_version="1",
        timeout=5,
        headers={"Accept": "application/vnd.citationstyles.csl+json"},
    )
    ledger, _ = _ready_ledger(path, operation.request)
    sent_urls: list[str] = []

    def crash_on_target(sent: SendOperation) -> requests.Response:
        sent_urls.append(sent.url)
        if sent.url == operation.url:
            return _response(302, headers={"Location": target})
        raise SystemExit("crash on redirect target")

    with pytest.raises(SystemExit):
        LedgerTransport(ledger, send_once=crash_on_target, clock=lambda: NOW).send_claim(
            _claim(ledger, "old"), operation
        )
    ledger.close()

    reopened = Ledger.open(path)
    resumed_at = NOW + timedelta(minutes=11)

    def finish_target(sent: SendOperation) -> requests.Response:
        sent_urls.append(sent.url)
        assert sent.url == target
        return _response(
            200,
            {"DOI": "10.1/x", "title": "Engine"},
            headers={"Content-Type": "application/json"},
        )

    result = LedgerTransport(reopened, send_once=finish_target, clock=lambda: resumed_at).send_claim(
        _claim(reopened, "new", resumed_at), operation
    )
    assert result.disposition is TaskDisposition.SUCCEEDED
    assert sent_urls == [operation.url, target, target]
    assert operation.url not in sent_urls[1:]
    reopened.close()


@pytest.mark.parametrize(("fail_on_call", "expected_physical_calls"), [(2, 1), (3, 1), (4, 2)])
def test_doi_redirect_clock_failure_resolves_every_started_hop(
    tmp_path: Path, fail_on_call: int, expected_physical_calls: int
) -> None:
    adapter = JSON_ADAPTERS["doi.csl"]
    operation = adapter.build_operation(
        url="https://doi.org/10.1/x",
        normalized_payload={"doi": "10.1/x"},
        freshness_epoch="2026-08",
        adapter_version="1",
        timeout=5,
        headers={"Accept": "application/vnd.citationstyles.csl+json"},
    )
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", operation.request)
    clock_calls = 0
    physical_calls = 0

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == fail_on_call:
            raise RuntimeError("clock unavailable")
        return NOW

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal physical_calls
        physical_calls += 1
        if physical_calls == 1:
            return _response(302, headers={"Location": "https://api.crossref.org/works/10.1/x"})
        return _response(
            200,
            {"title": "Safe", "DOI": "10.1/x"},
            headers={"Content-Type": "application/vnd.citationstyles.csl+json"},
        )

    result = LedgerTransport(ledger, send_once=sender, clock=clock).send_claim(_claim(ledger, "worker"), operation)
    assert result.disposition is TaskDisposition.PERMANENT_FAILURE
    manifest = ledger.manifest().data
    assert physical_calls == expected_physical_calls
    assert len(manifest["attempts"]) == expected_physical_calls
    assert manifest["physical_send_markers"][0]["resolved_at"] is not None
    assert manifest["tasks"][0]["lease_owner"] is None
    assert manifest["requests"][0]["state"] == TaskDisposition.PERMANENT_FAILURE.value
    ledger.close()


def test_doi_wrong_or_missing_accept_fails_preflight_without_send(tmp_path: Path) -> None:
    adapter = JSON_ADAPTERS["doi.csl"]
    operation = adapter.build_operation(
        url="https://doi.org/10.1/x",
        normalized_payload={"doi": "10.1/x"},
        freshness_epoch="2026-08",
        adapter_version="1",
        timeout=5,
        headers={"Accept": "application/x-bibtex"},
    )
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", operation.request)
    calls = 0

    def sender(_operation: SendOperation) -> requests.Response:
        nonlocal calls
        calls += 1
        return _response(200, {})

    with pytest.raises(ValueError, match="headers"):
        LedgerTransport(ledger, send_once=sender, clock=lambda: NOW).send_claim(_claim(ledger, "worker"), operation)
    assert calls == 0
    assert ledger.manifest().data["attempts"] == []
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
    ("body", "disposition", "outcome"),
    [
        (b"not json", TaskDisposition.MALFORMED, OutcomeClass.MALFORMED),
        (b"[]", TaskDisposition.SCHEMA_CHANGED, OutcomeClass.WRONG_SHAPE),
    ],
)
def test_malformed_and_wrong_shape_json_block(
    tmp_path: Path, body: bytes, disposition: TaskDisposition, outcome: OutcomeClass
) -> None:
    request = _request()
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    raw = _response(200)
    raw._content = body
    response = LedgerTransport(ledger, send_once=lambda _op: raw, clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"), _operation(request)
    )
    assert response.disposition is disposition
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


@pytest.mark.parametrize("body", [b'{"title":"A","title":"B"}', b'{"outer":{"id":"1","id":"2"}}'])
def test_duplicate_json_object_keys_are_malformed_at_every_depth(tmp_path: Path, body: bytes) -> None:
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

    kwargs: dict[str, Any] = {"validator": broken} if phase == "validator" else {"empty_validator": broken}
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


def test_nonidempotent_crash_marker_blocks_repeat_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    request = _request(method="POST")
    ledger, _ = _ready_ledger(path, request)
    calls = 0

    def crash(_operation: SendOperation) -> requests.Response:
        nonlocal calls
        calls += 1
        raise SystemExit("crash after physical send began")

    with pytest.raises(SystemExit):
        LedgerTransport(ledger, send_once=crash, clock=lambda: NOW).send_claim(
            _claim(ledger, "old"), _operation(request, idempotent=False)
        )
    original_started = ledger.manifest().data["physical_send_markers"][0]["started_at"]
    ledger.close()

    reopened = Ledger.open(path)
    resume_at = NOW + timedelta(minutes=11)
    claim = _claim(reopened, "new", resume_at)
    response = LedgerTransport(
        reopened,
        send_once=lambda _operation: pytest.fail("non-idempotent request was repeated"),
        clock=lambda: resume_at,
    ).send_claim(claim, _operation(request, idempotent=False))
    assert response.disposition is TaskDisposition.AMBIGUOUS
    manifest = reopened.manifest().data
    assert calls == 1
    assert manifest["attempts"][0]["started_at"] == original_started
    assert manifest["physical_send_markers"][0]["resolved_at"] is not None
    reopened.close()


def test_idempotent_crashes_record_exactly_one_attempt_per_physical_send_until_max(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    request = _request()
    ledger, _ = _ready_ledger(path, request)
    physical_calls = 0
    operation = _operation(request, idempotent=True, max_attempts=3)

    def crash(_operation: SendOperation) -> requests.Response:
        nonlocal physical_calls
        physical_calls += 1
        raise SystemExit("crash after physical send began")

    for index in range(3):
        at = NOW + timedelta(minutes=11 * index)
        with pytest.raises(SystemExit):
            LedgerTransport(ledger, send_once=crash, clock=lambda at=at: at).send_claim(
                _claim(ledger, f"worker-{index}", at), operation
            )
        ledger.close()
        ledger = Ledger.open(path)

    resume_at = NOW + timedelta(minutes=33)
    result = LedgerTransport(
        ledger,
        send_once=lambda _operation: pytest.fail("exhausted crash marker was resent"),
        clock=lambda: resume_at,
    ).send_claim(_claim(ledger, "terminal", resume_at), operation)
    assert result.disposition is TaskDisposition.BLOCKED
    manifest = ledger.manifest().data
    assert physical_calls == 3
    assert len(manifest["attempts"]) == 3
    assert all(attempt["http_status"] is None for attempt in manifest["attempts"])
    ledger.close()


def test_idempotent_post_may_retry(tmp_path: Path) -> None:
    request = _request(method="POST")
    ledger, _ = _ready_ledger(tmp_path / "ledger.db", request)
    response = LedgerTransport(ledger, send_once=lambda _op: _response(503, {}), clock=lambda: NOW).send_claim(
        _claim(ledger, "worker"),
        _operation(request, idempotent=True),
    )
    assert response.disposition is TaskDisposition.RETRY_WAIT
    ledger.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"idempotency_header": "Idempotency-Key", "idempotency_key": "operation-123"},
        {
            "headers": {"Idempotency-Key": "different"},
            "idempotency_header": "Idempotency-Key",
            "idempotency_key": "operation-123",
        },
    ],
)
def test_post_retry_requires_matching_transmitted_idempotency_header(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="idempotency"):
        _operation(_request(method="POST"), **kwargs)


def test_post_retry_accepts_only_matching_standard_idempotency_key_header() -> None:
    operation = _operation(
        _request(method="POST"),
        headers={"Idempotency-Key": "operation-123"},
        idempotency_header="Idempotency-Key",
        idempotency_key="operation-123",
    )
    assert operation.retryable
    with pytest.raises(ValueError, match="standard Idempotency-Key"):
        _operation(
            _request(method="POST"),
            headers={"X-Foo": "operation-123"},
            idempotency_header="X-Foo",
            idempotency_key="operation-123",
        )


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
            "123": {"uid": "123", "title": "A", "authors": [], "pubdate": "2026"},
            "456": {"uid": "456", "title": "B", "authors": [], "pubdate": "2026"},
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


def test_pubmed_esummary_rejects_uid_shaped_record_not_listed_in_uids() -> None:
    adapter = pubmed_summary_adapter(("123",))
    envelope = {"result": {"uids": ["123"], "123": {"uid": "123"}, "999": {"uid": "999"}}}
    with pytest.raises(SchemaChangedError, match="PubMed"):
        adapter.normalize(envelope)


def test_pubmed_esummary_rejects_uid_shaped_nonrecord_not_listed_in_uids() -> None:
    adapter = pubmed_summary_adapter(("123",))
    envelope = {"result": {"uids": ["123"], "123": {"uid": "123"}, "999": None}}
    with pytest.raises(SchemaChangedError, match="PubMed"):
        adapter.normalize(envelope)


def test_pubmed_esummary_rejects_record_key_uid_mismatch() -> None:
    adapter = pubmed_summary_adapter(("123",))
    envelope = {"result": {"uids": ["123"], "123": {"uid": "999"}}}
    with pytest.raises(SchemaChangedError, match="PubMed"):
        adapter.normalize(envelope)


@pytest.mark.parametrize(
    ("adapter_name", "envelope", "field"),
    [
        ("semantic_scholar.search", {"total": 1, "data": [{"paperId": "s2", "title": "A"}]}, "results"),
        (
            "crossref.search",
            {"status": "ok", "message": {"total-results": 1, "items": [{"DOI": "10.1/x", "title": ["A"]}]}},
            "results",
        ),
        ("openalex.search", {"meta": {"count": 1}, "results": [{"id": "W1", "title": "A"}]}, "results"),
        ("europepmc.search", {"hitCount": 1, "resultList": {"result": [{"id": "1", "title": "A"}]}}, "results"),
        ("serply.scholar", {"articles": [{"title": "A"}]}, "articles"),
        (
            "serpapi.author",
            {
                "search_metadata": {
                    "status": "Success",
                    "google_scholar_author_url": "https://scholar.google.com/citations?user=p",
                },
                "search_parameters": {"engine": "google_scholar_author", "author_id": "p", "start": 0},
                "author": {"name": "Ada"},
                "articles": [{"citation_id": "1", "title": "A", "authors": "Ada"}],
            },
            "articles",
        ),
        ("dblp.author_search", {"result": {"hits": {"hit": [{"info": {"pid": "1"}}]}}}, "hits"),
        ("pubmed.search", {"esearchresult": {"count": "1", "idlist": ["1"]}}, "pmids"),
        ("openreview.notes", {"notes": [{"id": "1", "content": {"title": "A"}}]}, "notes"),
        ("gemini.short_title", {"candidates": [{"content": {"parts": [{"text": "Ocean"}]}}]}, "candidates"),
        ("doi.csl", {"title": "A", "DOI": "10.1/x"}, "metadata"),
    ],
)
def test_every_json_provider_has_one_transport_adapter(
    adapter_name: str, envelope: dict[str, object], field: str
) -> None:
    adapter = JSON_ADAPTERS[adapter_name]
    normalized = adapter.normalize(envelope)
    transport = ScriptedTransport([ProviderResponse(TaskDisposition.SUCCEEDED, OutcomeClass.SUCCESS, normalized, 200)])
    payload = (
        {
            "prompt_digest_input": "Safe title",
            "max_words": 4,
            "prompt_version": GEMINI_PROMPT_VERSION,
            "model_id": GEMINI_MODEL_ID,
            "generation_config": dict(GEMINI_GENERATION_CONFIG),
        }
        if adapter_name == "gemini.short_title"
        else ({"doi": "10.1/x"} if adapter_name == "doi.csl" else {"author_scope": "author-ada", "query": "safe"})
    )
    operation = adapter.build_operation(
        url="https://provider.invalid/resource",
        normalized_payload=payload,
        freshness_epoch="2026-08",
        adapter_version="1",
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


def test_registry_covers_every_production_json_durable_callsite_once() -> None:
    assert JSON_DURABLE_CALLSITES == {
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
    static_names = set(JSON_ADAPTERS)
    assert set(JSON_DURABLE_CALLSITES.values()) - {"pubmed.summary.singleton"} <= static_names
