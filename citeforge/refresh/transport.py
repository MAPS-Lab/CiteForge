"""Classified, ledger-backed provider transport for durable refresh work."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

from ..config import HTTP_BACKOFF_INITIAL, HTTP_BACKOFF_MAX, HTTP_MAX_RETRIES
from ..http_utils import send_http_once
from .ledger import Ledger, ProviderObservation, RequestClaim, RequestResult, RequestSpec, StaleClaimError, TaskClaim
from .types import TaskDisposition

JsonMapping = Mapping[str, object]
EnvelopeValidator = Callable[[dict[str, object]], Mapping[str, object]]
EmptyValidator = Callable[[dict[str, object]], bool]
ResponseDecoder = Callable[["RawProviderResponse"], tuple[Mapping[str, object], bool]]
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


_SAFE_RESPONSE_HEADERS = frozenset({"content-type", "etag", "last-modified", "retry-after", "x-request-id"})


@dataclass(frozen=True)
class RawProviderResponse:
    """Bounded, secret-safe response material passed across decoder boundaries."""

    body: bytes = field(repr=False)
    content_type: str = field(repr=False)
    final_url: str = field(repr=False)
    headers: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.body, bytes):
            raise TypeError("provider response body must be bytes")
        parts = urlsplit(self.final_url)
        if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("unsafe provider final URL")
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("unsafe provider final URL port") from exc
        default_port = 443 if parts.scheme.casefold() == "https" else 80
        if port not in {None, default_port}:
            raise ValueError("unsafe provider final URL port")
        hostname = parts.hostname.casefold()
        netloc = f"[{hostname}]" if ":" in hostname else hostname
        path_digest = hashlib.sha256((parts.path or "/").encode()).hexdigest()
        safe_path = f"/path-sha256/{path_digest}"
        if hostname in {"doi.org", "dx.doi.org"}:
            from ..id_utils import find_doi_in_text

            redirect_doi = find_doi_in_text(parts.path)
            if redirect_doi:
                safe_path = f"/{redirect_doi.casefold()}"
        safe_headers: dict[str, str] = {}
        for raw_name, raw_value in self.headers.items():
            name = raw_name.casefold().strip()
            if "\r" in raw_value or "\n" in raw_value or len(raw_value) > 4096:
                raise ValueError("unsafe provider response header")
            if name in _SAFE_RESPONSE_HEADERS:
                safe_headers[name] = (
                    raw_value.split(";", 1)[0].strip().casefold()
                    if name == "content-type"
                    else hashlib.sha256(raw_value.encode()).hexdigest()
                )
        content_type = self.content_type.split(";", 1)[0].strip().casefold()
        if (
            not content_type
            or len(content_type) > 200
            or not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", content_type)
            or any(marker in content_type for marker in ("secret", "token", "key="))
        ):
            raise ValueError("unsafe provider content type")
        object.__setattr__(self, "content_type", content_type)
        object.__setattr__(
            self,
            "final_url",
            urlunsplit((parts.scheme.casefold(), netloc, safe_path, "", "")),
        )
        object.__setattr__(self, "headers", MappingProxyType(safe_headers))


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
    url: str = field(repr=False)
    timeout: float
    validator: EnvelopeValidator
    empty_validator: EmptyValidator
    headers: Mapping[str, str] | None = field(default=None, repr=False)
    json_payload: Mapping[str, object] | None = field(default=None, repr=False)
    idempotent: bool | None = None
    idempotency_key: str | None = None
    idempotency_header: str | None = None
    max_attempts: int = HTTP_MAX_RETRIES + 1
    response_decoder: ResponseDecoder | None = None
    decoder_schema: str | None = None
    max_body_bytes: int = 2_000_000
    capability_id: str | None = None

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("provider timeout must be positive")
        if self.max_attempts < 1:
            raise ValueError("provider max attempts must be positive")
        if self.max_body_bytes < 1:
            raise ValueError("provider body limit must be positive")
        if self.request.method not in {"GET", "HEAD", "POST"}:
            raise ValueError("unsupported wire method")
        if self.response_decoder is not None and not self.decoder_schema:
            raise ValueError("typed response decoder requires an exact schema identity")
        if self.request.method == "POST" and self.idempotency_key is not None:
            headers = {name.casefold(): value for name, value in (self.headers or {}).items()}
            if (
                self.idempotency_header != "Idempotency-Key"
                or headers.get(self.idempotency_header.casefold()) != self.idempotency_key
            ):
                raise ValueError("POST idempotency key requires matching standard Idempotency-Key header")
        elif self.request.method == "POST" and self.idempotency_header is not None:
            raise ValueError("POST idempotency header requires idempotency key")

    @property
    def retryable(self) -> bool:
        return self.request.method in {"GET", "HEAD"} or self.idempotent is True or self.idempotency_key is not None


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
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        return value
    raise TypeError(f"JSON value has unsupported type {type(value).__name__}")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _canonical_digest(payload: Mapping[str, object]) -> str:
    def thaw(value: object) -> object:
        if isinstance(value, Mapping):
            return {str(key): thaw(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [thaw(item) for item in value]
        if value is None or isinstance(value, str | bool | int):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        raise TypeError("canonical provider evidence must be strict JSON")

    encoded = json.dumps(
        thaw(payload), allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_bounded_body(response: requests.Response, limit: int) -> bytes:
    """Read at most ``limit + 1`` bytes from a streamed response."""
    if (
        response.raw is None and "iter_content" not in response.__dict__ and isinstance(response._content, bytes)
    ):  # deterministic requests.Response test seam
        if len(response._content) > limit:
            raise ValueError("provider response exceeds body limit")
        return response._content
    body = bytearray()
    for chunk in response.iter_content(chunk_size=min(limit + 1, 65_536)):
        body.extend(chunk)
        if len(body) > limit:
            raise ValueError("provider response exceeds body limit")
    return bytes(body)


def _close_response(response: requests.Response) -> None:
    with suppress(Exception):
        response.close()


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
            stream=True,
            allow_redirects=False,
            isolated_session=True,
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
        from .capabilities import capability_by_id, capability_for, validate_capability_wire

        try:
            registered = capability_for(
                operation.request.provider, operation.request.operation, operation.request.adapter_version
            )
        except ValueError:
            registered = None
        if registered is not None and operation.capability_id is None:
            raise ValueError("registered durable operation requires exact capability proof")
        if operation.capability_id is not None:
            capability = capability_by_id(operation.capability_id)
            if (
                operation.request.provider != capability.logical_source
                or operation.request.operation != capability.operation
                or operation.request.adapter_version != capability.adapter_version
                or operation.request.method != capability.method
                or operation.request.quota_scope != capability.quota_scope
                or operation.request.requested_fields != capability.requested_fields
                or operation.decoder_schema != capability.decoder_schema
                or operation.response_decoder is None
                or operation.max_body_bytes != capability.body_limit
                or operation.max_attempts != capability.max_attempts
                or operation.idempotent != capability.idempotent
            ):
                raise ValueError("send operation does not prove exact durable capability")
            validate_capability_wire(
                operation.capability_id,
                operation.request.normalized_payload,
                operation.url,
                operation.headers,
                operation.json_payload,
            )
        if task_claim.request_key != operation.request.key:
            raise ValueError("claimed task does not match exact request")
        try:
            now = self.clock()
        except Exception:
            if datetime.now(timezone.utc) >= task_claim.lease_expires:
                return ProviderResponse(
                    TaskDisposition.LEASED,
                    OutcomeClass.IN_FLIGHT,
                    safe_diagnostic="task claim expired before provider classification",
                    from_ledger=True,
                )
            fallback = self._claim_safe_time(task_claim)
            result = self.result(operation.request.key)
            if result is not None:
                self.ledger.finish_task(task_claim.key, task_claim.owner, result.disposition, fallback)
                return result
            request_claim = self.ledger.claim_request(task_claim.key, task_claim.owner, fallback, timedelta(minutes=10))
            if request_claim is None:
                return ProviderResponse(
                    TaskDisposition.LEASED,
                    OutcomeClass.IN_FLIGHT,
                    safe_diagnostic="exact request leased by another worker",
                    from_ledger=True,
                )
            return self._finish_before_send_failure(task_claim, request_claim, operation)
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

        started = now
        unresolved = self.ledger.unresolved_physical_send(operation.request.key)
        if unresolved is not None and not unresolved[1]:
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                unresolved[0],
                now,
                TaskDisposition.AMBIGUOUS,
                OutcomeClass.AMBIGUOUS_PARTIAL,
                None,
                "unresolved non-idempotent physical send was not repeated",
            )
        if unresolved is not None:
            self.ledger.record_intermediate_attempt(
                task_claim, request_claim, unresolved[0], now, "ambiguous_partial", None
            )
            if self.ledger.request_attempt_count(operation.request.key) >= operation.max_attempts:
                return self._finish_terminal(
                    task_claim,
                    request_claim,
                    operation,
                    unresolved[0],
                    now,
                    TaskDisposition.BLOCKED,
                    OutcomeClass.RETRY_EXHAUSTED,
                    None,
                    "idempotent crash attempts exhausted",
                    persist_attempt=False,
                )
        try:
            self.ledger.mark_physical_send(task_claim, request_claim, started, idempotent=operation.retryable)
        except ValueError:
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                started,
                started,
                TaskDisposition.AMBIGUOUS,
                OutcomeClass.AMBIGUOUS_PARTIAL,
                None,
                "unresolved non-idempotent physical send was not repeated",
            )
        sent = self._send_marked_physical(task_claim, request_claim, operation, started)
        if isinstance(sent, ProviderResponse):
            return sent
        raw, status, finished = sent
        redirect_hops = 0
        while status in {301, 302, 303, 307, 308} and operation.capability_id in {
            "doi_csl.csl_lookup.v1",
            "doi_bibtex.bibtex_lookup.v1",
        }:
            redirect_hops += 1
            if redirect_hops > 3:
                _close_response(raw)
                return self._finish_terminal(
                    task_claim,
                    request_claim,
                    operation,
                    started,
                    self._claim_safe_time(task_claim, request_claim),
                    TaskDisposition.PERMANENT_FAILURE,
                    OutcomeClass.INVALID_REQUEST,
                    status,
                    "DOI redirect limit exceeded",
                )
            try:
                location = raw.headers.get("Location", "")
                target = urlsplit(location)
                safe_target = (
                    target.scheme == "https"
                    and target.hostname
                    in {
                        "doi.org",
                        "api.crossref.org",
                        "data.crossref.org",
                        "api.datacite.org",
                        "data.crosscite.org",
                    }
                    and not target.username
                    and not target.password
                    and target.port in {None, 443}
                )
            except Exception:
                safe_target = False
            if not safe_target:
                _close_response(raw)
                return self._finish_terminal(
                    task_claim,
                    request_claim,
                    operation,
                    started,
                    self._claim_safe_time(task_claim, request_claim),
                    TaskDisposition.PERMANENT_FAILURE,
                    OutcomeClass.INVALID_REQUEST,
                    status,
                    "unsafe DOI redirect was rejected",
                )
            _close_response(raw)
            try:
                hop_finished = self.clock()
            except Exception:
                return self._finish_classification_failure(task_claim, request_claim, operation, started)
            self.ledger.record_intermediate_attempt(
                task_claim, request_claim, started, hop_finished, "redirect", status
            )
            started = hop_finished
            self.ledger.mark_physical_send(task_claim, request_claim, started, idempotent=True)
            operation = replace(
                operation, url=location, headers={"Accept": (operation.headers or {}).get("Accept", "")}
            )
            sent = self._send_marked_physical(task_claim, request_claim, operation, started)
            if isinstance(sent, ProviderResponse):
                return sent
            raw, status, finished = sent
        if status in {401, 403}:
            _close_response(raw)
            return self._finish_terminal(
                task_claim,
                request_claim,
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
            try:
                return self._finish_retryable(
                    task_claim, request_claim, operation, started, outcome, "retryable provider response", raw
                )
            finally:
                _close_response(raw)
        if 400 <= status < 500:
            outcome = OutcomeClass.NOT_FOUND if status == 404 else OutcomeClass.INVALID_REQUEST
            _close_response(raw)
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                started,
                finished,
                TaskDisposition.PERMANENT_FAILURE,
                outcome,
                status,
                "permanent provider client response",
            )
        if status < 200 or status >= 300:
            _close_response(raw)
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                started,
                finished,
                TaskDisposition.PERMANENT_FAILURE,
                OutcomeClass.INVALID_REQUEST,
                status,
                "unsupported provider status",
            )
        try:
            body = _read_bounded_body(raw, operation.max_body_bytes)
        except requests.Timeout:
            _close_response(raw)
            try:
                started = min(started, self.clock())
            except Exception:
                return self._finish_classification_failure(task_claim, request_claim, operation, started)
            return self._finish_retryable(
                task_claim, request_claim, operation, started, OutcomeClass.TIMEOUT, "response stream timed out"
            )
        except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
            _close_response(raw)
            try:
                started = min(started, self.clock())
            except Exception:
                return self._finish_classification_failure(task_claim, request_claim, operation, started)
            return self._finish_retryable(
                task_claim,
                request_claim,
                operation,
                started,
                OutcomeClass.CONNECTION_FAILURE,
                "provider response stream failed",
            )
        except Exception:
            _close_response(raw)
            try:
                finished = self.clock()
            except Exception:
                return self._finish_classification_failure(task_claim, request_claim, operation, started)
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                started,
                finished,
                TaskDisposition.MALFORMED,
                OutcomeClass.MALFORMED,
                status,
                "provider response exceeded body limit or failed while streaming",
            )
        if operation.response_decoder is not None:
            try:
                content_type = raw.headers.get("Content-Type", "")
                final_url = raw.url or operation.url
                response_headers = dict(raw.headers)
            except Exception:
                _close_response(raw)
                try:
                    finished = self.clock()
                except Exception:
                    return self._finish_classification_failure(task_claim, request_claim, operation, started)
                return self._finish_terminal(
                    task_claim,
                    request_claim,
                    operation,
                    started,
                    finished,
                    TaskDisposition.MALFORMED,
                    OutcomeClass.MALFORMED,
                    status,
                    "provider response metadata was invalid",
                )
            _close_response(raw)
            try:
                response = RawProviderResponse(
                    body,
                    content_type,
                    final_url,
                    response_headers,
                )
                normalized, is_empty = operation.response_decoder(response)
                if not isinstance(is_empty, bool):
                    raise SchemaChangedError("provider decoder did not return boolean empty evidence")
                _canonical_digest(normalized)
            except SchemaChangedError:
                try:
                    finished = self.clock()
                except Exception:
                    return self._finish_classification_failure(task_claim, request_claim, operation, started)
                return self._finish_terminal(
                    task_claim,
                    request_claim,
                    operation,
                    started,
                    finished,
                    TaskDisposition.SCHEMA_CHANGED,
                    OutcomeClass.SCHEMA_CHANGED,
                    status,
                    "provider envelope failed schema validation",
                )
            except Exception:
                try:
                    finished = self.clock()
                except Exception:
                    return self._finish_classification_failure(task_claim, request_claim, operation, started)
                return self._finish_terminal(
                    task_claim,
                    request_claim,
                    operation,
                    started,
                    finished,
                    TaskDisposition.MALFORMED,
                    OutcomeClass.MALFORMED,
                    status,
                    "provider returned malformed encoded response",
                )
            try:
                finished = self.clock()
            except Exception:
                return self._finish_classification_failure(task_claim, request_claim, operation, started)
            if is_empty:
                return self._finish_observation(
                    task_claim, request_claim, operation, started, finished, {}, status, authoritative_empty=True
                )
            return self._finish_observation(task_claim, request_claim, operation, started, finished, normalized, status)
        _close_response(raw)
        try:
            decoded = json.loads(
                body.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid constant {value}")),
                object_pairs_hook=_reject_duplicate_pairs,
            )
        except Exception:
            return self._finish_terminal(
                task_claim,
                request_claim,
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
                request_claim,
                operation,
                started,
                finished,
                TaskDisposition.SCHEMA_CHANGED,
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
                request_claim,
                operation,
                started,
                finished,
                TaskDisposition.SCHEMA_CHANGED,
                OutcomeClass.SCHEMA_CHANGED,
                status,
                "provider envelope failed schema validation",
            )
        try:
            finished = self.clock()
        except Exception:
            return self._finish_classification_failure(task_claim, request_claim, operation, started)
        if is_empty:
            return self._finish_observation(
                task_claim, request_claim, operation, started, finished, {}, status, authoritative_empty=True
            )
        return self._finish_observation(task_claim, request_claim, operation, started, finished, normalized, status)

    def _finish_observation(
        self,
        task_claim: TaskClaim,
        request_claim: RequestClaim,
        operation: SendOperation,
        started: datetime,
        finished: datetime,
        normalized: Mapping[str, object],
        status: int,
        *,
        authoritative_empty: bool = False,
    ) -> ProviderResponse:
        try:
            finished = self.clock()
        except Exception:
            return self._finish_classification_failure(task_claim, request_claim, operation, started)
        disposition = TaskDisposition.CONFIRMED_EMPTY if authoritative_empty else TaskDisposition.SUCCEEDED
        outcome = OutcomeClass.AUTHORITATIVE_EMPTY if authoritative_empty else OutcomeClass.SUCCESS
        diagnostic = "validated authoritative empty" if authoritative_empty else "validated response"
        try:
            digest = _canonical_digest(normalized)
            observation = ProviderObservation(
                operation.request.provider,
                operation.decoder_schema or operation.request.adapter_version,
                normalized,
                authoritative_empty=authoritative_empty,
            )
        except Exception:
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                started,
                finished,
                TaskDisposition.MALFORMED,
                OutcomeClass.MALFORMED,
                status,
                "normalized provider evidence was invalid",
            )
        try:
            self.ledger.complete_physical_attempt(
                task_claim,
                request_claim,
                started,
                finished,
                outcome.value,
                disposition,
                http_status=status,
                response_digest=digest,
                observation=observation,
                safe_diagnostic=diagnostic,
            )
        except StaleClaimError:
            return ProviderResponse(
                TaskDisposition.LEASED,
                OutcomeClass.IN_FLIGHT,
                safe_diagnostic="stale claim lost before provider completion",
                from_ledger=True,
            )
        except ValueError:
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                started,
                finished,
                TaskDisposition.SCHEMA_CHANGED,
                OutcomeClass.SCHEMA_CHANGED,
                status,
                "normalized provider evidence did not satisfy the request",
            )
        return ProviderResponse(disposition, outcome, normalized, status, digest, safe_diagnostic=diagnostic)

    def _finish_retryable(
        self,
        task_claim: TaskClaim,
        request_claim: RequestClaim,
        operation: SendOperation,
        started: datetime,
        outcome: OutcomeClass,
        diagnostic: str,
        response: requests.Response | None = None,
    ) -> ProviderResponse:
        try:
            finished = self.clock()
            status = response.status_code if response is not None else None
            if status is not None and not isinstance(status, int):
                raise TypeError("invalid response status")
            previous_attempts = self.ledger.request_attempt_count(operation.request.key)
            attempt_number = previous_attempts + 1
            exhausted = attempt_number >= operation.max_attempts
            retry_after = _retry_after(response.headers.get("Retry-After"), finished) if response is not None else None
            base = min(HTTP_BACKOFF_INITIAL * (2 ** (attempt_number - 1)), HTTP_BACKOFF_MAX)
            delay = retry_after if retry_after is not None else min(HTTP_BACKOFF_MAX, base + self._jitter(base))
        except Exception:
            return self._finish_classification_failure(task_claim, request_claim, operation, started)
        if not operation.retryable:
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                started,
                finished,
                TaskDisposition.AMBIGUOUS,
                OutcomeClass.AMBIGUOUS_PARTIAL,
                status,
                "non-idempotent operation has ambiguous provider outcome",
            )
        durable_outcome = OutcomeClass.RETRY_EXHAUSTED if exhausted else outcome
        disposition = TaskDisposition.BLOCKED if exhausted else TaskDisposition.RETRY_WAIT
        retry_at = None if exhausted else finished + timedelta(seconds=delay)
        try:
            self.ledger.complete_physical_attempt(
                task_claim,
                request_claim,
                started,
                finished,
                durable_outcome.value,
                disposition,
                http_status=status,
                retry_at=retry_at,
                retry_delay=None if exhausted else delay,
                safe_diagnostic=diagnostic,
                task_reason=diagnostic,
            )
        except StaleClaimError:
            return ProviderResponse(
                TaskDisposition.LEASED,
                OutcomeClass.IN_FLIGHT,
                safe_diagnostic="stale claim lost before provider completion",
                from_ledger=True,
            )
        return ProviderResponse(
            disposition,
            durable_outcome,
            status=status,
            retry_delay=delay if not exhausted else None,
            safe_diagnostic=diagnostic,
        )

    def _finish_classification_failure(
        self,
        task_claim: TaskClaim,
        request_claim: RequestClaim,
        operation: SendOperation,
        started: datetime | None = None,
    ) -> ProviderResponse:
        """Release a claimed request without consulting injected timing helpers."""
        fallback_finished = self._claim_safe_time(task_claim, request_claim)
        fallback_started = min(started or fallback_finished, fallback_finished)
        return self._finish_terminal(
            task_claim,
            request_claim,
            operation,
            fallback_started,
            fallback_finished,
            TaskDisposition.PERMANENT_FAILURE,
            OutcomeClass.INVALID_REQUEST,
            None,
            "provider response classification failed",
        )

    def _send_marked_physical(
        self,
        task_claim: TaskClaim,
        request_claim: RequestClaim,
        operation: SendOperation,
        started: datetime,
    ) -> tuple[requests.Response, int, datetime] | ProviderResponse:
        """Send one already-marked socket attempt through one classified boundary."""
        try:
            raw = self._send_once(operation)
        except requests.Timeout:
            return self._finish_retryable(
                task_claim, request_claim, operation, started, OutcomeClass.TIMEOUT, "request timed out"
            )
        except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
            return self._finish_retryable(
                task_claim,
                request_claim,
                operation,
                started,
                OutcomeClass.CONNECTION_FAILURE,
                "provider connection failed",
            )
        except requests.RequestException:
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                started,
                self._claim_safe_time(task_claim, request_claim),
                TaskDisposition.PERMANENT_FAILURE,
                OutcomeClass.INVALID_REQUEST,
                None,
                "provider request was invalid",
            )
        except Exception:
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                started,
                self._claim_safe_time(task_claim, request_claim),
                TaskDisposition.PERMANENT_FAILURE,
                OutcomeClass.INVALID_REQUEST,
                None,
                "provider send callback failed",
            )
        try:
            finished = self.clock()
        except Exception:
            _close_response(raw)
            return self._finish_classification_failure(task_claim, request_claim, operation, started)
        if not isinstance(raw, requests.Response):
            return self._finish_terminal(
                task_claim,
                request_claim,
                operation,
                started,
                finished,
                TaskDisposition.MALFORMED,
                OutcomeClass.MALFORMED,
                None,
                "provider send callback returned invalid response",
            )
        try:
            status = raw.status_code
        except Exception:
            _close_response(raw)
            return self._finish_classification_failure(task_claim, request_claim, operation, started)
        if not isinstance(status, int):
            _close_response(raw)
            return self._finish_classification_failure(task_claim, request_claim, operation, started)
        return raw, status, finished

    @staticmethod
    def _claim_safe_time(task_claim: TaskClaim, request_claim: RequestClaim | None = None) -> datetime:
        """Choose an instant inside every available durable lease."""
        lease_expires = (
            min(task_claim.lease_expires, request_claim.lease_expires) if request_claim else task_claim.lease_expires
        )
        return lease_expires - timedelta(microseconds=1)

    def _finish_before_send_failure(
        self, task_claim: TaskClaim, request_claim: RequestClaim, operation: SendOperation
    ) -> ProviderResponse:
        """Terminalize claimed work without inventing a physical attempt."""
        finished = self._claim_safe_time(task_claim, request_claim)
        diagnostic = "provider response classification failed"
        self.ledger.finish_request(
            operation.request.key,
            task_claim.owner,
            TaskDisposition.PERMANENT_FAILURE,
            finished,
            safe_diagnostic=diagnostic,
        )
        self.ledger.finish_task(
            task_claim.key,
            task_claim.owner,
            TaskDisposition.PERMANENT_FAILURE,
            finished,
            reason=diagnostic,
        )
        return ProviderResponse(
            TaskDisposition.PERMANENT_FAILURE,
            OutcomeClass.INVALID_REQUEST,
            safe_diagnostic=diagnostic,
        )

    def _finish_terminal(
        self,
        task_claim: TaskClaim,
        request_claim: RequestClaim,
        operation: SendOperation,
        started: datetime,
        finished: datetime,
        disposition: TaskDisposition,
        outcome: OutcomeClass,
        status: int | None,
        diagnostic: str,
        *,
        persist_attempt: bool = True,
    ) -> ProviderResponse:
        with suppress(Exception):
            finished = self.clock()
        try:
            self.ledger.complete_physical_attempt(
                task_claim,
                request_claim,
                started,
                finished,
                outcome.value,
                disposition,
                http_status=status,
                safe_diagnostic=diagnostic,
                task_reason=diagnostic,
                persist_attempt=persist_attempt,
            )
        except StaleClaimError:
            return ProviderResponse(
                TaskDisposition.LEASED,
                OutcomeClass.IN_FLIGHT,
                safe_diagnostic="stale claim lost before provider completion",
                from_ledger=True,
            )
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
