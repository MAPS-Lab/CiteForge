"""Classified, ledger-backed provider transport for durable refresh work."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from types import MappingProxyType
from typing import Protocol

import requests

from ..config import HTTP_BACKOFF_INITIAL, HTTP_BACKOFF_MAX, HTTP_MAX_RETRIES
from ..http_utils import send_http_once
from .ledger import Ledger, ProviderObservation, RequestResult, RequestSpec, TaskClaim
from .types import TaskDisposition

JsonMapping = Mapping[str, object]
EnvelopeValidator = Callable[[dict[str, object]], Mapping[str, object]]
EmptyValidator = Callable[[dict[str, object]], bool]
Clock = Callable[[], datetime]
Jitter = Callable[[float], float]

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class OutcomeClass(str, Enum):
    """Physical or reused provider outcome without false empty states."""

    SUCCESS = "success"
    AUTHORITATIVE_EMPTY = "authoritative_empty"
    MALFORMED = "malformed"
    WRONG_SHAPE = "wrong_shape"
    SCHEMA_CHANGED = "schema_changed"
    AUTHENTICATION_FAILURE = "authentication_failure"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    TRANSIENT_SERVER_ERROR = "transient_server_error"
    CONNECTION_FAILURE = "connection_failure"
    TIMEOUT = "timeout"
    CIRCUIT_OPEN = "circuit_open"
    AMBIGUOUS_PARTIAL = "ambiguous_partial"
    RETRY_EXHAUSTED = "retry_exhausted"
    IN_FLIGHT = "in_flight"
    REUSED = "reused"


class SchemaChangedError(ValueError):
    """A syntactically valid provider envelope no longer matches its contract."""


class ProviderTransportError(RuntimeError):
    """A classified provider outcome cannot be consumed as metadata."""

    def __init__(self, response: ProviderResponse) -> None:
        self.response = response
        super().__init__(response.safe_diagnostic or response.outcome.value)


@dataclass(frozen=True)
class ProviderResponse:
    """One explicitly classified provider result."""

    disposition: TaskDisposition
    outcome: OutcomeClass
    payload: Mapping[str, object] | None = None
    status: int | None = None
    response_digest: str | None = None
    retry_delay: float | None = None
    safe_diagnostic: str = ""
    from_ledger: bool = False

    def __post_init__(self) -> None:
        if self.payload is not None:
            object.__setattr__(self, "payload", _deep_freeze(self.payload))


@dataclass(frozen=True)
class SendOperation:
    """Send-time details kept outside the durable non-secret request identity."""

    request: RequestSpec
    url: str
    timeout: float
    validator: EnvelopeValidator
    empty_validator: EmptyValidator
    headers: Mapping[str, str] | None = None
    json_payload: Mapping[str, object] | None = None
    idempotent: bool | None = None
    idempotency_key: str | None = None
    idempotency_header: str | None = None
    max_attempts: int = HTTP_MAX_RETRIES + 1

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("provider timeout must be positive")
        if self.max_attempts < 1:
            raise ValueError("provider max attempts must be positive")
        if self.request.method == "POST" and self.idempotent:
            headers = {name.casefold(): value for name, value in (self.headers or {}).items()}
            if (
                not self.idempotency_header
                or not self.idempotency_key
                or headers.get(self.idempotency_header.casefold()) != self.idempotency_key
            ):
                raise ValueError("POST idempotency requires a matching transmitted idempotency header")

    @property
    def retryable(self) -> bool:
        return self.request.method in {"GET", "HEAD"} or self.idempotent is True


class ProviderTransport(Protocol):
    """Provider transport interface used by refresh adapters."""

    def send(self, operation: SendOperation, *, task_claim: TaskClaim | None = None) -> ProviderResponse:
        """Send or classify one operation."""


class ScriptedTransport:
    """Deterministic no-socket transport for adapter and engine tests."""

    def __init__(self, responses: Sequence[ProviderResponse | BaseException]) -> None:
        self._responses = list(responses)
        self.physical_calls = 0

    def send(self, _operation: SendOperation, *, task_claim: TaskClaim | None = None) -> ProviderResponse:
        del task_claim
        if not self._responses:
            raise AssertionError("scripted transport exhausted")
        self.physical_calls += 1
        result = self._responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def consume_response(response: ProviderResponse) -> Mapping[str, object]:
    """Return successful normalized payload while preserving every non-success class."""
    if response.disposition is TaskDisposition.SUCCEEDED and response.payload is not None:
        return response.payload
    if response.disposition is TaskDisposition.CONFIRMED_EMPTY:
        return MappingProxyType({})
    raise ProviderTransportError(response)


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _canonical_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _retry_after(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delay = (target - now.astimezone(timezone.utc)).total_seconds()
    return min(max(0.0, delay), HTTP_BACKOFF_MAX)


def correlate_exact_batch(
    requested: Sequence[str], members: Sequence[Mapping[str, object]], *, correlation_field: str
) -> Mapping[str, Mapping[str, object]]:
    """Correlate exact batch members, rejecting every ambiguous response shape."""
    requested_keys = tuple(requested)
    if not requested_keys or len(set(requested_keys)) != len(requested_keys):
        raise ValueError("batch request identifiers must be unique")
    correlated: dict[str, Mapping[str, object]] = {}
    for member in members:
        key = member.get(correlation_field)
        if not isinstance(key, str) or not key or key in correlated:
            raise ValueError("batch response has malformed or duplicate correlation key")
        if key not in requested_keys:
            raise ValueError("batch response has unexpected correlation key")
        correlated[key] = MappingProxyType(dict(member))
    if set(correlated) != set(requested_keys):
        raise ValueError("batch response omitted requested member")
    return MappingProxyType({key: correlated[key] for key in requested_keys})


class LedgerTransport:
    """Persist exact claims, physical attempts, retry deadlines, and observations."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        send_once: Callable[[SendOperation], requests.Response] | None = None,
        clock: Clock | None = None,
        jitter: Jitter | None = None,
    ) -> None:
        self.ledger = ledger
        self._send_once = send_once or self._default_send_once
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._jitter = jitter or (lambda delay: random.uniform(0.0, delay * 0.3))

    @staticmethod
    def _default_send_once(operation: SendOperation) -> requests.Response:
        return send_http_once(
            operation.request.method,
            operation.url,
            dict(operation.headers or {}),
            operation.timeout,
            dict(operation.json_payload) if operation.json_payload is not None else None,
        )

    def result(self, request_key: str) -> ProviderResponse | None:
        result = self.ledger.request_result(request_key)
        return self._from_result(result) if result is not None else None

    @staticmethod
    def _from_result(result: RequestResult) -> ProviderResponse:
        fallback = {
            TaskDisposition.SUCCEEDED: OutcomeClass.REUSED,
            TaskDisposition.CONFIRMED_EMPTY: OutcomeClass.AUTHORITATIVE_EMPTY,
            TaskDisposition.MALFORMED: OutcomeClass.MALFORMED,
            TaskDisposition.AUTHENTICATION_FAILED: OutcomeClass.AUTHENTICATION_FAILURE,
            TaskDisposition.SCHEMA_CHANGED: OutcomeClass.SCHEMA_CHANGED,
            TaskDisposition.PERMANENT_FAILURE: OutcomeClass.INVALID_REQUEST,
            TaskDisposition.CIRCUIT_OPEN: OutcomeClass.CIRCUIT_OPEN,
            TaskDisposition.AMBIGUOUS: OutcomeClass.AMBIGUOUS_PARTIAL,
            TaskDisposition.BLOCKED: OutcomeClass.RETRY_EXHAUSTED,
        }.get(result.disposition, OutcomeClass.REUSED)
        try:
            outcome = OutcomeClass(result.outcome) if result.outcome else fallback
        except ValueError:
            outcome = fallback
        return ProviderResponse(
            result.disposition,
            outcome,
            result.normalized_response,
            status=result.http_status,
            response_digest=result.response_digest,
            safe_diagnostic="reused durable observation",
            from_ledger=True,
        )

    def send(self, operation: SendOperation, *, task_claim: TaskClaim | None = None) -> ProviderResponse:
        """Execute a claimed request or reuse an already-terminal exact result."""
        if task_claim is not None:
            return self.send_claim(task_claim, operation)
        result = self.result(operation.request.key)
        if result is None:
            raise ValueError("ledger transport requires a claimed logical task")
        return result

    def send_claim(self, task_claim: TaskClaim, operation: SendOperation) -> ProviderResponse:
        """Execute one claimed task or observe its shared exact request."""
        now = self.clock()
        if task_claim.request_key != operation.request.key:
            raise ValueError("claimed task does not match exact request")
        result = self.result(operation.request.key)
        if result is not None:
            self.ledger.finish_task(task_claim.key, task_claim.owner, result.disposition, now)
            return result

        request_claim = self.ledger.claim_request(task_claim.key, task_claim.owner, now, timedelta(minutes=10))
        if request_claim is None:
            result = self.result(operation.request.key)
            if result is not None:
                self.ledger.finish_task(task_claim.key, task_claim.owner, result.disposition, now)
                return result
            return ProviderResponse(
                TaskDisposition.LEASED,
                OutcomeClass.IN_FLIGHT,
                safe_diagnostic="exact request leased by another worker",
                from_ledger=True,
            )

        started = self.clock()
        try:
            raw = self._send_once(operation)
        except requests.Timeout:
            return self._finish_retryable(task_claim, operation, started, OutcomeClass.TIMEOUT, "request timed out")
        except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
            return self._finish_retryable(
                task_claim, operation, started, OutcomeClass.CONNECTION_FAILURE, "provider connection failed"
            )
        except requests.RequestException:
            return self._finish_terminal(
                task_claim,
                operation,
                started,
                self.clock(),
                TaskDisposition.PERMANENT_FAILURE,
                OutcomeClass.INVALID_REQUEST,
                None,
                "provider request was invalid",
            )
        finished = self.clock()
        status = raw.status_code
        if status in {401, 403}:
            return self._finish_terminal(
                task_claim,
                operation,
                started,
                finished,
                TaskDisposition.AUTHENTICATION_FAILED,
                OutcomeClass.AUTHENTICATION_FAILURE,
                status,
                "provider authentication or policy rejected request",
            )
        if status in _RETRYABLE_STATUS:
            outcome = OutcomeClass.RATE_LIMITED if status == 429 else OutcomeClass.TRANSIENT_SERVER_ERROR
            return self._finish_retryable(task_claim, operation, started, outcome, "retryable provider response", raw)
        if 400 <= status < 500:
            outcome = OutcomeClass.NOT_FOUND if status == 404 else OutcomeClass.INVALID_REQUEST
            return self._finish_terminal(
                task_claim,
                operation,
                started,
                finished,
                TaskDisposition.PERMANENT_FAILURE,
                outcome,
                status,
                "permanent provider client response",
            )
        if status < 200 or status >= 300:
            return self._finish_terminal(
                task_claim,
                operation,
                started,
                finished,
                TaskDisposition.PERMANENT_FAILURE,
                OutcomeClass.INVALID_REQUEST,
                status,
                "unsupported provider status",
            )
        try:
            decoded = json.loads(
                raw.content.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid constant {value}")),
            )
        except (UnicodeError, json.JSONDecodeError, ValueError):
            return self._finish_terminal(
                task_claim,
                operation,
                started,
                finished,
                TaskDisposition.MALFORMED,
                OutcomeClass.MALFORMED,
                status,
                "provider returned malformed JSON",
            )
        if not isinstance(decoded, dict):
            return self._finish_terminal(
                task_claim,
                operation,
                started,
                finished,
                TaskDisposition.MALFORMED,
                OutcomeClass.WRONG_SHAPE,
                status,
                "provider returned non-object JSON",
            )
        try:
            normalized = dict(operation.validator(decoded))
            is_empty = operation.empty_validator(decoded)
            if not isinstance(is_empty, bool):
                raise SchemaChangedError("provider empty validator did not return bool")
            _canonical_digest(normalized)
        except Exception:  # provider adapters are untrusted schema boundaries
            return self._finish_terminal(
                task_claim,
                operation,
                started,
                finished,
                TaskDisposition.SCHEMA_CHANGED,
                OutcomeClass.SCHEMA_CHANGED,
                status,
                "provider envelope failed schema validation",
            )
        if is_empty:
            return self._finish_observation(
                task_claim, operation, started, finished, {}, status, authoritative_empty=True
            )
        return self._finish_observation(task_claim, operation, started, finished, normalized, status)

    def _finish_observation(
        self,
        task_claim: TaskClaim,
        operation: SendOperation,
        started: datetime,
        finished: datetime,
        normalized: Mapping[str, object],
        status: int,
        *,
        authoritative_empty: bool = False,
    ) -> ProviderResponse:
        disposition = TaskDisposition.CONFIRMED_EMPTY if authoritative_empty else TaskDisposition.SUCCEEDED
        outcome = OutcomeClass.AUTHORITATIVE_EMPTY if authoritative_empty else OutcomeClass.SUCCESS
        diagnostic = "validated authoritative empty" if authoritative_empty else "validated response"
        try:
            digest = _canonical_digest(normalized)
            observation = ProviderObservation(
                operation.request.provider,
                operation.request.adapter_version,
                normalized,
                authoritative_empty=authoritative_empty,
            )
        except Exception:
            return self._finish_terminal(
                task_claim,
                operation,
                started,
                finished,
                TaskDisposition.MALFORMED,
                OutcomeClass.MALFORMED,
                status,
                "normalized provider evidence was invalid",
            )
        self.ledger.record_attempt(
            operation.request.key,
            task_claim.owner,
            started,
            finished,
            outcome.value,
            http_status=status,
            response_digest=digest,
            safe_diagnostic=diagnostic,
        )
        self.ledger.finish_request(
            operation.request.key,
            task_claim.owner,
            disposition,
            finished,
            response_digest=digest,
            observation=observation,
            safe_diagnostic=diagnostic,
        )
        self.ledger.finish_task(task_claim.key, task_claim.owner, disposition, finished)
        return ProviderResponse(disposition, outcome, normalized, status, digest, safe_diagnostic=diagnostic)

    def _finish_retryable(
        self,
        task_claim: TaskClaim,
        operation: SendOperation,
        started: datetime,
        outcome: OutcomeClass,
        diagnostic: str,
        response: requests.Response | None = None,
    ) -> ProviderResponse:
        finished = self.clock()
        status = response.status_code if response is not None else None
        if not operation.retryable:
            return self._finish_terminal(
                task_claim,
                operation,
                started,
                finished,
                TaskDisposition.AMBIGUOUS,
                OutcomeClass.AMBIGUOUS_PARTIAL,
                status,
                "non-idempotent operation has ambiguous provider outcome",
            )
        previous_attempts = self.ledger.request_attempt_count(operation.request.key)
        attempt_number = previous_attempts + 1
        exhausted = attempt_number >= operation.max_attempts
        retry_after = _retry_after(response.headers.get("Retry-After"), finished) if response is not None else None
        base = min(HTTP_BACKOFF_INITIAL * (2 ** (attempt_number - 1)), HTTP_BACKOFF_MAX)
        delay = retry_after if retry_after is not None else min(HTTP_BACKOFF_MAX, base + self._jitter(base))
        durable_outcome = OutcomeClass.RETRY_EXHAUSTED if exhausted else outcome
        self.ledger.record_attempt(
            operation.request.key,
            task_claim.owner,
            started,
            finished,
            durable_outcome.value,
            http_status=status,
            retry_delay=None if exhausted else delay,
            safe_diagnostic=diagnostic,
        )
        disposition = TaskDisposition.BLOCKED if exhausted else TaskDisposition.RETRY_WAIT
        retry_at = None if exhausted else finished + timedelta(seconds=delay)
        self.ledger.finish_request(
            operation.request.key,
            task_claim.owner,
            disposition,
            finished,
            retry_at=retry_at,
            safe_diagnostic=diagnostic,
        )
        self.ledger.finish_task(
            task_claim.key,
            task_claim.owner,
            disposition,
            finished,
            retry_at=retry_at,
            reason=diagnostic,
        )
        return ProviderResponse(
            disposition,
            durable_outcome,
            status=status,
            retry_delay=delay if not exhausted else None,
            safe_diagnostic=diagnostic,
        )

    def _finish_terminal(
        self,
        task_claim: TaskClaim,
        operation: SendOperation,
        started: datetime,
        finished: datetime,
        disposition: TaskDisposition,
        outcome: OutcomeClass,
        status: int | None,
        diagnostic: str,
    ) -> ProviderResponse:
        self.ledger.record_attempt(
            operation.request.key,
            task_claim.owner,
            started,
            finished,
            outcome.value,
            http_status=status,
            safe_diagnostic=diagnostic,
        )
        self.ledger.finish_request(
            operation.request.key,
            task_claim.owner,
            disposition,
            finished,
            safe_diagnostic=diagnostic,
        )
        self.ledger.finish_task(task_claim.key, task_claim.owner, disposition, finished, reason=diagnostic)
        return ProviderResponse(disposition, outcome, status=status, safe_diagnostic=diagnostic)


__all__ = [
    "LedgerTransport",
    "OutcomeClass",
    "ProviderResponse",
    "ProviderTransport",
    "ProviderTransportError",
    "SchemaChangedError",
    "ScriptedTransport",
    "SendOperation",
    "consume_response",
    "correlate_exact_batch",
]
