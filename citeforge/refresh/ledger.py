"""Transactional SQLite authority for resumable refresh generations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import cast
from urllib.parse import parse_qsl, urlsplit

from ..config import PREPRINT_ONLY_PUBLISHERS, PREPRINT_SERVERS
from ..id_utils import is_secondary_doi
from ..merge_utils import merge_with_policy
from ..text_utils import has_placeholder
from .census import AuthorCensus
from .types import GenerationSpec, GenerationState, PlanPhase, TaskDisposition

_SCHEMA_VERSION = "3"
_MAX_PLAN_ROUNDS = 64
_SATISFIED = frozenset(
    {
        TaskDisposition.SUCCEEDED,
        TaskDisposition.CONFIRMED_EMPTY,
        TaskDisposition.NOT_APPLICABLE,
        TaskDisposition.DOMINATED,
    }
)
_TERMINAL = _SATISFIED | frozenset(
    {
        TaskDisposition.MALFORMED,
        TaskDisposition.AUTHENTICATION_FAILED,
        TaskDisposition.SCHEMA_CHANGED,
        TaskDisposition.PERMANENT_FAILURE,
        TaskDisposition.CIRCUIT_OPEN,
        TaskDisposition.AMBIGUOUS,
        TaskDisposition.BLOCKED,
        TaskDisposition.UNKNOWN,
    }
)
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:authorization|api[_-]?key|token|secret|password|credential(?:_path)?)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:authorization|api[_-]?key|token|secret|password|credential)\s*[:=])",
    re.IGNORECASE,
)
_FAULT_POINTS = frozenset(
    {
        "after_claim_commit",
        "after_attempt_commit",
        "after_response_commit",
        "after_task_terminalization",
        "after_manifest_commit",
        "after_initial_round_publications",
        "after_initial_round_tasks",
        "after_initial_round_obligations",
        "after_initial_round_round",
        "after_initial_round_commit",
        "after_reduction_publications",
        "after_reduction_tasks",
        "after_reduction_obligations",
        "after_reduction_round",
        "after_reduction_receipt",
        "after_reduction_commit",
        "after_plan_close_validations",
        "after_plan_close_commit",
    }
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}")
_PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_FIELD_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,127}")
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_LEGAL_GENERATION_TRANSITIONS = {
    GenerationState.PLANNING: frozenset({GenerationState.RUNNING, GenerationState.SUPERSEDED}),
    GenerationState.RUNNING: frozenset(
        {GenerationState.WAITING, GenerationState.BLOCKED, GenerationState.VALIDATING, GenerationState.SUPERSEDED}
    ),
    GenerationState.WAITING: frozenset({GenerationState.RUNNING, GenerationState.BLOCKED, GenerationState.SUPERSEDED}),
    GenerationState.BLOCKED: frozenset({GenerationState.RUNNING, GenerationState.SUPERSEDED}),
    GenerationState.VALIDATING: frozenset(
        {GenerationState.COMPLETE, GenerationState.BLOCKED, GenerationState.SUPERSEDED}
    ),
    GenerationState.COMPLETE: frozenset({GenerationState.PUBLISHED, GenerationState.SUPERSEDED}),
    GenerationState.PUBLISHED: frozenset(),
    GenerationState.SUPERSEDED: frozenset(),
}


class FaultInjectedError(RuntimeError):
    """Test-only interruption raised after a named durable boundary."""


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain_json(item) for item in value]
    return value


def _canonical(value: object) -> str:
    return json.dumps(_plain_json(value), allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _contains_secret(value: object, *, key: str = "") -> bool:
    if key and _SECRET_KEY.search(key):
        return True
    if isinstance(value, Mapping):
        return any(_contains_secret(item, key=str(item_key)) for item_key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_secret(item) for item in value)
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return True
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return True
        if parsed.query and any(_SECRET_KEY.search(query_key) for query_key, _ in parse_qsl(parsed.query)):
            return True
        normalized = value.casefold().replace("\\", "/")
        if re.search(
            r"(?:^|/)(?:\.aws/credentials|\.ssh(?:/|$)|[^/]*key\.pem$|[^/]*(?:credential|secret)[^/]*\.(?:json|pem|key)$)",
            normalized,
        ):
            return True
    return False


def _identifier(value: str, purpose: str) -> str:
    if _SECRET_KEY.search(value) or _contains_secret(value):
        raise ValueError(f"secret material cannot be persisted as {purpose}")
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"invalid {purpose}")
    return value


def _provider(value: str) -> str:
    if _SECRET_KEY.search(value) or _contains_secret(value):
        raise ValueError("secret material cannot be persisted as provider")
    if not _PROVIDER_RE.fullmatch(value):
        raise ValueError("invalid provider")
    return value


def _free_text(value: str, purpose: str, *, required: bool = False) -> str:
    if required and not value.strip():
        raise ValueError(f"{purpose} is required")
    if len(value) > 2000 or "\x00" in value:
        raise ValueError(f"invalid {purpose}")
    if _contains_secret(value):
        raise ValueError(f"secret material cannot be persisted as {purpose}")
    return value


def _digest_text(value: str, purpose: str) -> str:
    if not _HEX_DIGEST_RE.fullmatch(value):
        raise ValueError(f"invalid {purpose}")
    return value


def _freeze_json(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        _canonical(value)
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _has_observation_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and not has_placeholder(value)
    if isinstance(value, Mapping):
        return bool(value) and any(_has_observation_value(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return bool(value) and any(_has_observation_value(item) for item in value)
    return True


def _is_preprint_observation(response: Mapping[str, object]) -> bool:
    doi = response.get("doi")
    journal = str(response.get("journal") or "").casefold()
    publisher = str(response.get("publisher") or "").casefold().strip()
    return bool(
        (doi and is_secondary_doi(str(doi)))
        or any(server in journal for server in PREPRINT_SERVERS)
        or publisher in PREPRINT_ONLY_PUBLISHERS
    )


def _is_published_observation(response: Mapping[str, object]) -> bool:
    if _is_preprint_observation(response):
        return False
    doi = response.get("doi")
    journal = str(response.get("journal") or "").casefold()
    return bool(
        (doi and not is_secondary_doi(str(doi))) or (journal and not any(s in journal for s in PREPRINT_SERVERS))
    )


def _merge_proves_dominance(
    lower_provider: str,
    lower_response: Mapping[str, object],
    stronger: Sequence[tuple[str, Mapping[str, object]]],
    covered_fields: Sequence[str],
    rule: DominanceRule,
) -> bool:
    # merge_with_policy assigns primary fields to scholar_min. Until the reducer
    # exposes provenance-aware incumbents, other lower providers are unprovable.
    if lower_provider != "scholar_min":
        return False
    enrichers = [(provider, {"type": "misc", "fields": dict(response)}) for provider, response in stronger]
    primary = {"type": "misc", "fields": dict(lower_response)}
    merged = merge_with_policy(primary, enrichers)["fields"]
    lower_normalized = merge_with_policy(primary, [])["fields"]
    stronger_normalized = merge_with_policy({"type": "misc", "fields": {}}, enrichers)["fields"]
    if rule is DominanceRule.PUBLISHED_OVER_PREPRINT and (
        not _is_preprint_observation(lower_response) or not _is_published_observation(stronger_normalized)
    ):
        return False
    return all(
        field_name in stronger_normalized
        and merged.get(field_name) == stronger_normalized[field_name]
        and merged.get(field_name) != lower_normalized.get(field_name)
        for field_name in covered_fields
    )


class ApplicabilityReason(str, Enum):
    NO_APPLICABLE_IDENTIFIER = "no_applicable_identifier"
    PROVIDER_NOT_SUPPORTED = "provider_not_supported"


class DominanceRule(str, Enum):
    PUBLISHED_OVER_PREPRINT = "published_over_preprint"
    AUTHORITATIVE_METADATA = "authoritative_metadata"


class ProvenanceRule(str, Enum):
    TRUST_POLICY = "trust_policy"
    PUBLISHED_OVER_PREPRINT = "published_over_preprint"


class EvidenceState(str, Enum):
    PENDING = "pending"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    VALIDATED = "validated"


@dataclass(frozen=True)
class ProviderObservation:
    provider: str
    schema_version: str
    response: Mapping[str, object]
    authoritative_empty: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _provider(self.provider))
        object.__setattr__(self, "schema_version", _identifier(self.schema_version, "schema version"))
        if _contains_secret(self.response):
            raise ValueError("secret material cannot be persisted in provider observation")
        frozen = _freeze_json(self.response)
        if not isinstance(frozen, Mapping):
            raise TypeError("provider response must be a JSON object")
        object.__setattr__(self, "response", frozen)

    @property
    def digest(self) -> str:
        return _digest(dict(self.response))


@dataclass(frozen=True)
class DominanceEvidence:
    stronger_observation_keys: tuple[str, ...]
    rule: DominanceRule
    covered_fields: tuple[str, ...]
    dominated_observation_key: str

    def __post_init__(self) -> None:
        if not self.stronger_observation_keys or not self.covered_fields:
            raise ValueError("dominance evidence requires observations and covered fields")
        for key in self.stronger_observation_keys:
            _digest_text(key, "observation key")
        _digest_text(self.dominated_observation_key, "dominated observation key")
        if not isinstance(self.rule, DominanceRule):
            raise ValueError("invalid dominance rule")
        for field_name in self.covered_fields:
            if not _FIELD_RE.fullmatch(field_name):
                raise ValueError("invalid dominated field")


@dataclass(frozen=True)
class ValidationSpec:
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, "validation name"))


@dataclass(frozen=True)
class MaterializationEvidence:
    staged_path: str
    manifest_digest: str
    corpus_counts: Mapping[str, int]
    validation_state: EvidenceState


@dataclass(frozen=True)
class PublicationMetadata:
    author_key: str
    publication_key: str
    discovery_source: str
    normalized_title: str
    year: int | None
    exact_identifiers: Mapping[str, str]
    baseline_output_path: str
    freshness_policy: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "author_key", _identifier(self.author_key, "author key"))
        object.__setattr__(self, "publication_key", _identifier(self.publication_key, "publication key"))
        object.__setattr__(self, "discovery_source", _provider(self.discovery_source))
        object.__setattr__(
            self, "normalized_title", _free_text(self.normalized_title, "normalized title", required=True)
        )
        if self.year is not None and (
            isinstance(self.year, bool) or not isinstance(self.year, int) or not 1000 <= self.year <= 9999
        ):
            raise ValueError("invalid publication year")
        identifiers = dict(self.exact_identifiers)
        if _contains_secret(identifiers):
            raise ValueError("secret material cannot be persisted in publication identifiers")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in identifiers.items()):
            raise TypeError("publication identifiers must be strings")
        object.__setattr__(self, "exact_identifiers", _freeze_json(identifiers))
        object.__setattr__(self, "baseline_output_path", _free_text(self.baseline_output_path, "baseline output path"))
        object.__setattr__(self, "freshness_policy", _identifier(self.freshness_policy, "freshness policy"))


@dataclass(frozen=True)
class PlannedTask:
    task: TaskSpec
    expands_plan: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.task, TaskSpec) or not isinstance(self.expands_plan, bool):
            raise TypeError("planned task requires a task and boolean expansion flag")


@dataclass(frozen=True)
class PlanRound:
    key: str
    sequence: int
    phase: PlanPhase
    planner_id: str
    planner_version: str
    source_task_keys: tuple[str, ...]
    source_evidence_digest: str
    publications: tuple[PublicationMetadata, ...]
    tasks: tuple[PlannedTask, ...]
    task_set_digest: str
    content_digest: str


@dataclass(frozen=True)
class ReductionReceipt:
    source_task_keys: tuple[str, ...]
    round_key: str
    source_dispositions: tuple[TaskDisposition, ...]
    source_evidence_digests: tuple[str, ...]
    reduction_digest: str


@dataclass(frozen=True)
class PlanStatus:
    revision: int
    closed: bool
    authority_mode: str
    plan_digest: str | None
    closure_digest: str | None
    open_expanders: int
    unbound_tasks: int


@dataclass(frozen=True)
class RequestSpec:
    """Canonical, non-secret identity of one exact provider request."""

    provider: str
    operation: str
    method: str
    normalized_payload: Mapping[str, object]
    requested_fields: tuple[str, ...]
    adapter_version: str
    freshness_epoch: str
    quota_scope: str
    key: str = field(init=False)
    _payload_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        payload = dict(self.normalized_payload)
        if _contains_secret(payload):
            raise ValueError("secret material cannot be persisted in a request")
        fields = tuple(sorted(set(self.requested_fields)))
        provider = _provider(self.provider)
        operation = _identifier(self.operation, "operation")
        if self.method.upper() not in {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"}:
            raise ValueError("invalid HTTP method")
        for requested_field in fields:
            if _SECRET_KEY.search(requested_field) or _contains_secret(requested_field):
                raise ValueError("secret material cannot be persisted as requested field")
            if not _FIELD_RE.fullmatch(requested_field):
                raise ValueError("invalid requested field")
        adapter_version = _identifier(self.adapter_version, "adapter version")
        freshness_epoch = _identifier(self.freshness_epoch, "freshness epoch")
        quota_scope = _identifier(self.quota_scope, "quota scope")
        canonical = {
            "adapter_version": adapter_version,
            "freshness_epoch": freshness_epoch,
            "method": self.method.upper(),
            "normalized_payload": payload,
            "operation": operation,
            "provider": provider,
            "quota_scope": quota_scope,
            "requested_fields": fields,
        }
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "adapter_version", adapter_version)
        object.__setattr__(self, "freshness_epoch", freshness_epoch)
        object.__setattr__(self, "quota_scope", quota_scope)
        payload_json = _canonical(payload)
        object.__setattr__(self, "normalized_payload", _freeze_json(payload))
        object.__setattr__(self, "requested_fields", fields)
        object.__setattr__(self, "key", _digest(canonical))
        object.__setattr__(self, "_payload_json", payload_json)

    def canonical_content(self) -> dict[str, object]:
        return {
            "adapter_version": self.adapter_version,
            "freshness_epoch": self.freshness_epoch,
            "method": self.method,
            "normalized_payload": json.loads(self._payload_json),
            "operation": self.operation,
            "provider": self.provider,
            "quota_scope": self.quota_scope,
            "requested_fields": list(self.requested_fields),
        }


@dataclass(frozen=True)
class TaskSpec:
    """One author-scoped logical consumer of an exact request."""

    author_key: str
    publication_key: str | None
    provider: str
    operation: str
    request: RequestSpec | None
    required: bool = True
    applicability: str = "applicable"
    key: str = field(init=False)
    identity_digest: str = field(init=False)

    def __post_init__(self) -> None:
        author_key = _identifier(self.author_key, "author key")
        publication_key = (
            _identifier(self.publication_key, "publication key") if self.publication_key is not None else None
        )
        provider = _provider(self.provider)
        operation = _identifier(self.operation, "operation")
        applicability = _identifier(self.applicability, "applicability")
        if self.request is not None and (
            self.provider != self.request.provider or self.operation != self.request.operation
        ):
            raise ValueError("task and request provider operation must match")
        if applicability == "not_applicable" and self.request is not None:
            raise ValueError("not applicable task cannot have an applicable request")
        if applicability != "not_applicable" and self.request is None:
            raise ValueError("applicable task requires an exact request")
        identity = {
            "applicability": applicability,
            "author_key": author_key,
            "operation": operation,
            "provider": provider,
            "publication_key": publication_key,
            "request_key": self.request.key if self.request is not None else None,
            "required": self.required,
        }
        identity_digest = _digest(identity)
        object.__setattr__(self, "author_key", author_key)
        object.__setattr__(self, "publication_key", publication_key)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "applicability", applicability)
        object.__setattr__(self, "identity_digest", identity_digest)
        object.__setattr__(self, "key", identity_digest)


def inventory_tasks(
    census: AuthorCensus, adapter_versions: Mapping[str, str], freshness_epoch: str
) -> tuple[TaskSpec, ...]:
    """Derive mandatory exact inventory obligations from configured enabled census sources."""
    tasks: list[TaskSpec] = []
    for row in census.enabled_rows:
        for provider, provider_id in (("dblp", row.dblp_id), ("scholar", row.scholar_id)):
            if not provider_id:
                continue
            if provider not in adapter_versions:
                raise ValueError(f"missing adapter version for inventory provider {provider}")
            request = RequestSpec(
                provider,
                "inventory",
                "GET",
                {"profile_id": provider_id},
                ("publications",),
                adapter_versions[provider],
                freshness_epoch,
                provider,
            )
            tasks.append(TaskSpec(row.row_key, None, provider, "inventory", request))
    return tuple(sorted(tasks, key=lambda task: (task.author_key, task.provider)))


@dataclass(frozen=True)
class TaskClaim:
    key: str
    request_key: str | None
    owner: str
    lease_expires: datetime


@dataclass(frozen=True)
class RequestClaim:
    key: str
    owner: str
    lease_expires: datetime


@dataclass(frozen=True)
class RequestResult:
    key: str
    disposition: TaskDisposition
    normalized_response: Mapping[str, object] | None
    response_digest: str | None
    outcome: str | None
    http_status: int | None


@dataclass(frozen=True)
class LedgerManifest:
    data: Mapping[str, object]
    canonical_json: str
    digest: str


class Ledger:
    """Single-generation SQLite ledger with explicit durable transitions."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection
        self._fault: str | None = None
        self._manifest_probe: Callable[[], None] | None = None

    @classmethod
    def open(cls, path: Path) -> Ledger:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        journal_mode = str(connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]).lower()
        if journal_mode != "delete":
            connection.close()
            raise ValueError(f"unsupported SQLite journal mode: {journal_mode}")
        ledger = cls(path, connection)
        try:
            ledger._initialize_schema()
        except Exception:
            connection.close()
            raise
        return ledger

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _initialize_schema(self) -> None:
        with self._transaction(immediate=True) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            existing_version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing_version is not None and existing_version[0] != _SCHEMA_VERSION:
                raise ValueError(f"unsupported ledger schema version: {existing_version[0]}")
            if existing_version is not None:
                self._validate_schema_v3(connection)
                return
            schema = """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generations (
                    generation_id TEXT PRIMARY KEY,
                    identity_json TEXT NOT NULL,
                    census_digest TEXT NOT NULL,
                    authors_digest TEXT NOT NULL,
                    base_commit TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    adapter_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    published_at TEXT,
                    checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
                    blocking_reason TEXT NOT NULL DEFAULT '',
                    plan_sealed INTEGER NOT NULL DEFAULT 0 CHECK(plan_sealed IN (0, 1)),
                    plan_digest TEXT,
                    completed_manifest_digest TEXT,
                    inventory_freshness_epoch TEXT
                    ,plan_closed INTEGER NOT NULL DEFAULT 0 CHECK(plan_closed IN (0, 1))
                    ,plan_revision INTEGER NOT NULL DEFAULT 0
                    ,closure_digest TEXT
                    ,plan_authority_mode TEXT NOT NULL DEFAULT 'phased'
                );
                CREATE TABLE IF NOT EXISTS authors (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    row_key TEXT NOT NULL,
                    physical_row INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    scholar_id TEXT NOT NULL,
                    dblp_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    exclusion_reason TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    PRIMARY KEY (generation_id, row_key)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    request_key TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    next_attempt_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    response_digest TEXT,
                    safe_diagnostic TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    task_key TEXT NOT NULL,
                    author_key TEXT NOT NULL,
                    publication_key TEXT,
                    provider TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_key TEXT,
                    identity_digest TEXT NOT NULL,
                    required INTEGER NOT NULL CHECK(required IN (0, 1)),
                    applicability TEXT NOT NULL,
                    applicability_reason TEXT NOT NULL DEFAULT '',
                    dominance_reason TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error_class TEXT,
                    safe_diagnostic TEXT NOT NULL DEFAULT '',
                    next_attempt_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    PRIMARY KEY (generation_id, task_key),
                    UNIQUE (generation_id, identity_digest),
                    FOREIGN KEY (generation_id, author_key) REFERENCES authors(generation_id, row_key),
                    FOREIGN KEY (generation_id, author_key, publication_key)
                        REFERENCES publications(generation_id, author_key, publication_key),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS request_consumers (
                    generation_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    PRIMARY KEY (generation_id, request_key, task_key),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key),
                    FOREIGN KEY (generation_id, task_key) REFERENCES tasks(generation_id, task_key)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    generation_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    http_status INTEGER,
                    retry_delay_seconds REAL,
                    response_digest TEXT,
                    safe_diagnostic TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (generation_id, request_key, attempt_number),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS observations (
                    generation_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    response_json TEXT,
                    response_digest TEXT,
                    provider TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    authoritative_empty INTEGER NOT NULL CHECK(authoritative_empty IN (0, 1)),
                    observed_at TEXT NOT NULL,
                    safe_diagnostic TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (generation_id, request_key),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    sequence INTEGER NOT NULL,
                    ciphertext_digest TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS publications (
                    generation_id TEXT NOT NULL,
                    author_key TEXT NOT NULL,
                    publication_key TEXT NOT NULL,
                    discovery_source TEXT NOT NULL DEFAULT '',
                    normalized_title TEXT NOT NULL DEFAULT '',
                    year INTEGER,
                    exact_identifiers_json TEXT NOT NULL DEFAULT '{}',
                    baseline_output_path TEXT NOT NULL DEFAULT '',
                    freshness_policy TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (generation_id, author_key, publication_key),
                    FOREIGN KEY (generation_id, author_key) REFERENCES authors(generation_id, row_key)
                );
                CREATE TABLE IF NOT EXISTS publication_evidence (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    kind TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, kind, commit_sha)
                );
                CREATE TABLE IF NOT EXISTS manifests (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    digest TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    PRIMARY KEY (generation_id, digest)
                );
                CREATE TABLE IF NOT EXISTS plan_obligations (
                    generation_id TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    identity_digest TEXT NOT NULL,
                    author_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    required INTEGER NOT NULL CHECK(required IN (0, 1)),
                    applicability TEXT NOT NULL,
                    round_sequence INTEGER,
                    expands_plan INTEGER NOT NULL DEFAULT 0 CHECK(expands_plan IN (0, 1)),
                    PRIMARY KEY (generation_id, task_key),
                    UNIQUE (generation_id, identity_digest),
                    FOREIGN KEY (generation_id, task_key) REFERENCES tasks(generation_id, task_key)
                );
                CREATE TABLE IF NOT EXISTS plan_rounds (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    sequence INTEGER NOT NULL CHECK(sequence > 0),
                    round_key TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    planner_id TEXT NOT NULL,
                    planner_version TEXT NOT NULL,
                    source_task_keys_json TEXT NOT NULL,
                    source_evidence_digest TEXT NOT NULL,
                    task_set_digest TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, sequence),
                    UNIQUE (generation_id, round_key)
                );
                CREATE TABLE IF NOT EXISTS reduction_receipts (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    reduction_digest TEXT NOT NULL,
                    round_key TEXT NOT NULL,
                    source_task_keys_json TEXT NOT NULL,
                    source_dispositions_json TEXT NOT NULL,
                    source_evidence_digests_json TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, reduction_digest),
                    UNIQUE (generation_id, round_key),
                    FOREIGN KEY (generation_id, round_key) REFERENCES plan_rounds(generation_id, round_key)
                );
                CREATE TABLE IF NOT EXISTS round_publications (
                    generation_id TEXT NOT NULL,
                    round_sequence INTEGER NOT NULL,
                    author_key TEXT NOT NULL,
                    publication_key TEXT NOT NULL,
                    PRIMARY KEY (generation_id, round_sequence, author_key, publication_key),
                    FOREIGN KEY (generation_id, round_sequence)
                        REFERENCES plan_rounds(generation_id, sequence),
                    FOREIGN KEY (generation_id, author_key, publication_key)
                        REFERENCES publications(generation_id, author_key, publication_key)
                );
                CREATE TABLE IF NOT EXISTS reduction_sources (
                    generation_id TEXT NOT NULL,
                    source_task_key TEXT NOT NULL,
                    reduction_digest TEXT NOT NULL,
                    PRIMARY KEY (generation_id, source_task_key),
                    FOREIGN KEY (generation_id, source_task_key) REFERENCES tasks(generation_id, task_key),
                    FOREIGN KEY (generation_id, reduction_digest)
                        REFERENCES reduction_receipts(generation_id, reduction_digest)
                );
                CREATE TABLE IF NOT EXISTS validation_obligations (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    check_name TEXT NOT NULL,
                    required INTEGER NOT NULL CHECK(required IN (0, 1)),
                    PRIMARY KEY (generation_id, check_name)
                );
                CREATE TABLE IF NOT EXISTS field_provenance (
                    generation_id TEXT NOT NULL,
                    author_key TEXT NOT NULL,
                    publication_key TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    selected_value_digest TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    decision_rule TEXT NOT NULL,
                    PRIMARY KEY (generation_id, author_key, publication_key, field_name),
                    FOREIGN KEY (generation_id, author_key, publication_key)
                        REFERENCES publications(generation_id, author_key, publication_key),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS dominance_evidence (
                    generation_id TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    stronger_observations_json TEXT NOT NULL,
                    dominated_observation_key TEXT NOT NULL,
                    rule TEXT NOT NULL,
                    covered_fields_json TEXT NOT NULL,
                    PRIMARY KEY (generation_id, task_key),
                    FOREIGN KEY (generation_id, task_key) REFERENCES tasks(generation_id, task_key),
                    FOREIGN KEY (generation_id, dominated_observation_key)
                        REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS provider_state (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    provider TEXT NOT NULL,
                    quota_pool TEXT NOT NULL,
                    current_concurrency INTEGER NOT NULL,
                    rate_limit_deadline TEXT,
                    circuit_state TEXT NOT NULL,
                    success_count INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL,
                    async_job_id TEXT,
                    request_digest TEXT,
                    PRIMARY KEY (generation_id, provider, quota_pool)
                );
                CREATE TABLE IF NOT EXISTS materializations (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    staged_path TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    corpus_counts_json TEXT NOT NULL,
                    validation_state TEXT NOT NULL,
                    PRIMARY KEY (generation_id, staged_path)
                );
                CREATE TABLE IF NOT EXISTS validations (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    check_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    safe_detail TEXT NOT NULL,
                    PRIMARY KEY (generation_id, check_name)
                );
                """
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS attempts_no_update BEFORE UPDATE ON attempts "
                "BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END"
            )
            for table in ("plan_obligations", "validation_obligations"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{operation.lower()} BEFORE {operation} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT, 'sealed planning obligations are append-only'); END"
                    )
                for operation in ("INSERT",):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_sealed_{operation.lower()} BEFORE {operation} "  # noqa: S608
                        f"ON {table} WHEN (SELECT plan_sealed FROM generations WHERE generation_id = "
                        f"{'OLD' if operation != 'INSERT' else 'NEW'}.generation_id) = 1 "
                        "BEGIN SELECT RAISE(ABORT, 'sealed obligations are immutable'); END"
                    )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS tasks_append_only_identity_update BEFORE UPDATE OF task_key, author_key, "
                "publication_key, provider, operation, request_key, identity_digest, required, applicability ON tasks "
                "BEGIN SELECT RAISE(ABORT, 'sealed task identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS tasks_append_only_delete BEFORE DELETE ON tasks "
                "BEGIN SELECT RAISE(ABORT, 'sealed task identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS tasks_sealed_insert BEFORE INSERT ON tasks WHEN "
                "(SELECT plan_sealed FROM generations WHERE generation_id = NEW.generation_id) = 1 "
                "BEGIN SELECT RAISE(ABORT, 'sealed task identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS requests_append_only_identity_update BEFORE UPDATE OF request_key, "
                "identity_json ON requests BEGIN SELECT RAISE(ABORT, 'sealed request identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS requests_append_only_delete BEFORE DELETE ON requests "
                "BEGIN SELECT RAISE(ABORT, 'sealed request identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS requests_sealed_insert BEFORE INSERT ON requests WHEN "
                "(SELECT plan_sealed FROM generations WHERE generation_id = NEW.generation_id) = 1 "
                "BEGIN SELECT RAISE(ABORT, 'sealed request identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS attempts_no_delete BEFORE DELETE ON attempts "
                "BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END"
            )
            for table in ("observations", "dominance_evidence"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{operation.lower()} BEFORE {operation} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT, 'terminal evidence is append-only'); END"
                    )
            for table in ("plan_rounds", "reduction_receipts", "reduction_sources", "round_publications"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{operation.lower()} BEFORE {operation} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT, 'phased planning evidence is append-only'); END"
                    )
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_closed_insert BEFORE INSERT ON {table} "  # noqa: S608
                    "WHEN (SELECT plan_closed FROM generations WHERE generation_id = NEW.generation_id) = 1 "
                    "BEGIN SELECT RAISE(ABORT, 'closed plan rejects planning insert'); END"
                )
            for table in ("publications", "request_consumers"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{operation.lower()} BEFORE {operation} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT, 'planning identity is append-only'); END"
                    )
            if existing_version is None:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)", (_SCHEMA_VERSION,)
                )
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_fingerprint', ?)",
                    (self._schema_fingerprint(connection),),
                )
            self._validate_schema_v3(connection)

    @staticmethod
    def _schema_fingerprint(connection: sqlite3.Connection) -> str:
        objects = [
            {"name": row[1], "sql": row[3], "table": row[2], "type": row[0]}
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
        return _digest(objects)

    @staticmethod
    def _validate_schema_v3(connection: sqlite3.Connection) -> None:
        required_columns = {
            "generations": {"plan_closed", "plan_revision", "closure_digest", "plan_authority_mode"},
            "plan_obligations": {"round_sequence", "expands_plan"},
            "plan_rounds": {
                "sequence",
                "round_key",
                "phase",
                "planner_id",
                "planner_version",
                "source_task_keys_json",
                "source_evidence_digest",
                "task_set_digest",
                "content_digest",
            },
            "reduction_receipts": {
                "reduction_digest",
                "round_key",
                "source_task_keys_json",
                "source_dispositions_json",
                "source_evidence_digests_json",
            },
            "reduction_sources": {"source_task_key", "reduction_digest"},
            "round_publications": {"round_sequence", "author_key", "publication_key"},
        }
        for table, required in required_columns.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not required <= columns:
                raise ValueError(f"structurally inconsistent schema version 3 table: {table}")
        fingerprint = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_fingerprint'").fetchone()
        if fingerprint is None or fingerprint[0] != Ledger._schema_fingerprint(connection):
            raise ValueError("structurally inconsistent schema version 3 fingerprint")

    def _generation_id(self) -> str:
        rows = self._connection.execute("SELECT generation_id FROM generations").fetchall()
        if len(rows) != 1:
            raise ValueError("ledger must contain exactly one generation")
        return str(rows[0][0])

    def create_or_resume(self, spec: GenerationSpec, census: AuthorCensus) -> None:
        if spec.census.canonical_content() != census.canonical_content():
            raise ValueError("generation census does not match supplied census")
        identity = {
            "adapter_versions": dict(spec.adapter_versions),
            "base_commit": spec.base_commit.strip(),
            "census": census.canonical_content(),
            "refresh_policy_version": spec.refresh_policy_version.strip(),
        }
        identity_json = _canonical(identity)
        census_digest = _digest(census.canonical_content())
        durable_census = self._census_rows(census)
        for row in durable_census:
            _identifier(str(row["row_key"]), "census row key")
            _free_text(str(row["name"]), "author name", required=True)
            _free_text(str(row["normalized_name"]), "normalized author name", required=True)
            if row["scholar_id"]:
                _identifier(str(row["scholar_id"]), "Scholar identifier")
            if row["dblp_id"]:
                _identifier(str(row["dblp_id"]), "DBLP identifier")
            _free_text(str(row["exclusion_reason"]), "exclusion reason")
        authors_digest = _digest(durable_census)
        base_commit = _identifier(spec.base_commit.strip(), "base commit")
        policy_version = _identifier(spec.refresh_policy_version.strip(), "refresh policy version")
        adapters = dict(spec.adapter_versions)
        for provider_name, adapter_version in adapters.items():
            _provider(provider_name)
            _identifier(adapter_version, "adapter version")
        policy_digest = _digest(policy_version)
        adapter_digest = _digest(dict(sorted(adapters.items())))
        created_at = _timestamp(datetime.now(timezone.utc))
        with self._transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT generation_id, identity_json, census_digest, authors_digest FROM generations"
            ).fetchall()
            if existing:
                row = existing[0]
                if len(existing) != 1 or row[0] != spec.id or row[1] != identity_json:
                    raise ValueError("generation identity mismatch")
                if row[2] != census_digest:
                    raise ValueError("census mismatch")
                stored_rows = [
                    dict(item)
                    for item in connection.execute(
                        "SELECT row_key, physical_row, name, normalized_name, scholar_id, dblp_id, enabled, "
                        "exclusion_reason, disposition FROM authors WHERE generation_id = ? ORDER BY row_key",
                        (spec.id,),
                    )
                ]
                if row[3] != authors_digest or _digest(stored_rows) != authors_digest or stored_rows != durable_census:
                    raise ValueError("durable census mismatch")
                self._verify_plan_integrity(connection, spec.id)
                return
            connection.execute(
                "INSERT INTO generations(generation_id, identity_json, census_digest, authors_digest, base_commit, "
                "input_digest, policy_digest, adapter_digest, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    spec.id,
                    identity_json,
                    census_digest,
                    authors_digest,
                    base_commit,
                    census_digest,
                    policy_digest,
                    adapter_digest,
                    GenerationState.PLANNING.value,
                    created_at,
                    created_at,
                ),
            )
            connection.executemany(
                "INSERT INTO authors(generation_id, row_key, physical_row, name, normalized_name, scholar_id, "
                "dblp_id, enabled, exclusion_reason, disposition) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        spec.id,
                        row.row_key,
                        row.physical_row,
                        row.name,
                        row.normalized_name,
                        row.scholar_id,
                        row.dblp_id,
                        int(row.enabled),
                        row.exclusion_reason,
                        row.disposition.value,
                    )
                    for row in census.rows
                ],
            )

    def transition_generation(
        self,
        expected: GenerationState,
        new: GenerationState,
        now: datetime,
        *,
        blocking_reason: str = "",
    ) -> None:
        if new not in _LEGAL_GENERATION_TRANSITIONS[expected]:
            raise ValueError(f"illegal generation transition from {expected.value} to {new.value}")
        safe_reason = _free_text(blocking_reason, "blocking reason", required=new is GenerationState.BLOCKED)
        if new is not GenerationState.BLOCKED and safe_reason:
            raise ValueError("blocking reason only applies to blocked generation state")
        now_text = _timestamp(now)
        with self._transaction(immediate=True) as connection:
            generation_id = self._generation_id()
            self._verify_plan_integrity(connection, generation_id)
            if expected is GenerationState.PLANNING and new is GenerationState.RUNNING:
                initial_round = connection.execute(
                    "SELECT COUNT(*) FROM plan_rounds WHERE generation_id = ? AND sequence = 1", (generation_id,)
                ).fetchone()[0]
                if not initial_round:
                    raise ValueError("generation cannot run before committed initial round")
            if new is GenerationState.VALIDATING and not self._all_required_satisfied(connection, generation_id):
                raise ValueError("generation cannot validate before structurally closed complete plan")
            if new is GenerationState.COMPLETE:
                incomplete = connection.execute(
                    "SELECT COUNT(*) FROM validation_obligations AS obligation LEFT JOIN validations AS validation "
                    "ON validation.generation_id = obligation.generation_id AND validation.check_name = "
                    "obligation.check_name WHERE obligation.generation_id = ? AND obligation.required = 1 "
                    "AND (validation.check_name IS NULL OR validation.state != 'succeeded')",
                    (generation_id,),
                ).fetchone()[0]
                if incomplete:
                    raise ValueError("generation cannot complete without successful validation evidence")
                unexpected_failures = connection.execute(
                    "SELECT COUNT(*) FROM validations WHERE generation_id = ? AND state != 'succeeded'",
                    (generation_id,),
                ).fetchone()[0]
                if unexpected_failures:
                    raise ValueError("generation has failed or pending validation evidence")
                current_binding = connection.execute(
                    "SELECT digest FROM manifests WHERE generation_id = ? ORDER BY rowid DESC LIMIT 1",
                    (generation_id,),
                ).fetchone()
                materialization = connection.execute(
                    "SELECT COUNT(*) FROM materializations WHERE generation_id = ? AND validation_state = ? "
                    "AND manifest_digest = ?",
                    (generation_id, EvidenceState.VALIDATED.value, current_binding[0] if current_binding else ""),
                ).fetchone()[0]
                if not materialization:
                    raise ValueError("generation cannot complete without bound validated materialization")
                completed_manifest_digest = current_binding[0]
            else:
                completed_manifest_digest = None
            if new is GenerationState.PUBLISHED:
                published = connection.execute(
                    "SELECT COUNT(*) FROM publication_evidence AS publication JOIN materializations AS materialization "
                    "ON materialization.generation_id = publication.generation_id AND materialization.manifest_digest "
                    "= publication.manifest_digest JOIN generations AS generation ON generation.generation_id = "
                    "publication.generation_id WHERE publication.generation_id = ? AND publication.kind = "
                    "'verified_merge' AND materialization.validation_state = ? AND publication.manifest_digest = "
                    "generation.completed_manifest_digest AND publication.candidate_digest = "
                    "generation.completed_manifest_digest",
                    (generation_id, EvidenceState.VALIDATED.value),
                ).fetchone()[0]
                if not published:
                    raise ValueError("generation cannot publish without exact verified merge evidence")
            completed_at = now_text if new is GenerationState.COMPLETE else None
            published_at = now_text if new is GenerationState.PUBLISHED else None
            cursor = connection.execute(
                "UPDATE generations SET state = ?, updated_at = ?, completed_at = COALESCE(?, completed_at), "
                "published_at = COALESCE(?, published_at), blocking_reason = ?, "
                "completed_manifest_digest = COALESCE(?, completed_manifest_digest) "
                "WHERE generation_id = ? AND state = ?",
                (
                    new.value,
                    now_text,
                    completed_at,
                    published_at,
                    safe_reason,
                    completed_manifest_digest,
                    generation_id,
                    expected.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("generation state transition rejected")

    @staticmethod
    def _census_rows(census: AuthorCensus) -> list[dict[str, object]]:
        return [
            {
                "row_key": row.row_key,
                "physical_row": row.physical_row,
                "name": row.name,
                "normalized_name": row.normalized_name,
                "scholar_id": row.scholar_id,
                "dblp_id": row.dblp_id,
                "enabled": int(row.enabled),
                "exclusion_reason": row.exclusion_reason,
                "disposition": row.disposition.value,
            }
            for row in sorted(census.rows, key=lambda item: item.row_key)
        ]

    @staticmethod
    def _publication_content(publication: PublicationMetadata) -> dict[str, object]:
        return {
            "author_key": publication.author_key,
            "publication_key": publication.publication_key,
            "discovery_source": publication.discovery_source,
            "normalized_title": publication.normalized_title,
            "year": publication.year,
            "exact_identifiers": dict(sorted(publication.exact_identifiers.items())),
            "baseline_output_path": publication.baseline_output_path,
            "freshness_policy": publication.freshness_policy,
        }

    @classmethod
    def _round_content(
        cls,
        sequence: int,
        phase: PlanPhase,
        planner_id: str,
        planner_version: str,
        source_task_keys: Sequence[str],
        source_evidence_digest: str,
        publications: Sequence[PublicationMetadata],
        tasks: Sequence[PlannedTask],
    ) -> dict[str, object]:
        task_content = [
            {"expands_plan": item.expands_plan, "identity": item.task.identity_digest, "task_key": item.task.key}
            for item in sorted(tasks, key=lambda item: item.task.key)
        ]
        publication_content = [
            cls._publication_content(item)
            for item in sorted(publications, key=lambda item: (item.author_key, item.publication_key))
        ]
        return {
            "phase": phase.value,
            "planner_id": _identifier(planner_id, "planner identifier"),
            "planner_version": _identifier(planner_version, "planner version"),
            "publications": publication_content,
            "sequence": sequence,
            "source_evidence_digest": _digest_text(source_evidence_digest, "source evidence digest"),
            "source_task_keys": sorted(_digest_text(key, "source task key") for key in source_task_keys),
            "task_set_digest": _digest(task_content),
            "tasks": task_content,
        }

    @staticmethod
    def _insert_task(connection: sqlite3.Connection, generation_id: str, task: TaskSpec) -> None:
        request_key = task.request.key if task.request is not None else None
        if task.request is not None:
            identity = _canonical(task.request.canonical_content())
            connection.execute(
                "INSERT OR IGNORE INTO requests(generation_id, request_key, identity_json, state) VALUES (?, ?, ?, ?)",
                (generation_id, request_key, identity, TaskDisposition.PENDING.value),
            )
            stored = connection.execute(
                "SELECT identity_json FROM requests WHERE generation_id = ? AND request_key = ?",
                (generation_id, request_key),
            ).fetchone()
            if stored is None or stored[0] != identity:
                raise ValueError("exact request identity collision")
        if task.publication_key is not None:
            publication = connection.execute(
                "SELECT 1 FROM publications WHERE generation_id = ? AND author_key = ? AND publication_key = ?",
                (generation_id, task.author_key, task.publication_key),
            ).fetchone()
            if publication is None:
                raise ValueError("phased publication task requires committed publication metadata")
        connection.execute(
            "INSERT INTO tasks(generation_id, task_key, author_key, publication_key, provider, operation, "
            "request_key, required, applicability, identity_digest, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                generation_id,
                task.key,
                task.author_key,
                task.publication_key,
                task.provider,
                task.operation,
                request_key,
                int(task.required),
                task.applicability,
                task.identity_digest,
                TaskDisposition.PENDING.value,
            ),
        )
        if request_key is not None:
            connection.execute(
                "INSERT INTO request_consumers(generation_id, request_key, task_key) VALUES (?, ?, ?)",
                (generation_id, request_key, task.key),
            )

    def _validate_mandatory_inventory(
        self, connection: sqlite3.Connection, generation_id: str, tasks: Sequence[TaskSpec]
    ) -> str | None:
        generation_identity = json.loads(
            connection.execute(
                "SELECT identity_json FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()[0]
        )
        mandatory = []
        for row in connection.execute(
            "SELECT row_key, scholar_id, dblp_id FROM authors WHERE generation_id = ? AND enabled = 1",
            (generation_id,),
        ):
            for provider, profile_id in (("scholar", row["scholar_id"]), ("dblp", row["dblp_id"])):
                if profile_id:
                    mandatory.append((row["row_key"], provider, profile_id))
        declared_inventory = [task for task in tasks if task.operation == "inventory"]
        if not mandatory:
            if declared_inventory:
                raise ValueError("declared tasks do not match full canonical inventory obligations")
            return None
        epochs = {task.request.freshness_epoch for task in declared_inventory if task.request is not None}
        if len(epochs) != 1:
            raise ValueError("canonical inventory obligations require one explicit freshness epoch")
        epoch = next(iter(epochs))
        canonical = []
        for author_key, provider, profile_id in mandatory:
            version = generation_identity["adapter_versions"].get(provider)
            if version is None:
                raise ValueError(f"generation lacks adapter version for inventory provider {provider}")
            request = RequestSpec(
                provider,
                "inventory",
                "GET",
                {"profile_id": profile_id},
                ("publications",),
                version,
                epoch,
                provider,
            )
            canonical.append(TaskSpec(author_key, None, provider, "inventory", request))
        if sorted(task.key for task in declared_inventory) != sorted(task.key for task in canonical):
            raise ValueError("declared tasks do not match full canonical inventory obligations")
        return epoch

    def commit_initial_round(
        self,
        tasks: Sequence[PlannedTask],
        *,
        source_evidence_digest: str,
        publications: Sequence[PublicationMetadata] = (),
        now: datetime,
    ) -> PlanRound:
        generation_id = self._generation_id()
        if len({item.task.key for item in tasks}) != len(tasks):
            raise ValueError("duplicate initial round task")
        content = self._round_content(
            1,
            PlanPhase.INVENTORIES,
            "inventory_planner",
            "1",
            (),
            source_evidence_digest,
            publications,
            tasks,
        )
        content_digest = _digest(content)
        round_key = _digest({"generation_id": generation_id, "content_digest": content_digest})
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state, plan_closed FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or generation[0] != GenerationState.PLANNING.value or generation[1]:
                raise ValueError("initial round requires open planning generation")
            existing = connection.execute(
                "SELECT round_key, content_digest FROM plan_rounds WHERE generation_id = ? AND sequence = 1",
                (generation_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != round_key or existing[1] != content_digest:
                    raise ValueError("conflicting initial round replay")
                return self._load_round(connection, generation_id, 1)
            epoch = self._validate_mandatory_inventory(connection, generation_id, [item.task for item in tasks])
            for publication in publications:
                self._insert_publication(connection, generation_id, publication)
            self._inject("after_initial_round_publications")
            for item in sorted(tasks, key=lambda value: value.task.key):
                self._insert_task(connection, generation_id, item.task)
            self._inject("after_initial_round_tasks")
            connection.executemany(
                "INSERT INTO plan_obligations(generation_id, task_key, identity_digest, author_key, provider, "
                "operation, required, applicability, round_sequence, expands_plan) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                [
                    (
                        generation_id,
                        item.task.key,
                        item.task.identity_digest,
                        item.task.author_key,
                        item.task.provider,
                        item.task.operation,
                        int(item.task.required),
                        item.task.applicability,
                        int(item.expands_plan),
                    )
                    for item in sorted(tasks, key=lambda value: value.task.key)
                ],
            )
            self._inject("after_initial_round_obligations")
            connection.execute(
                "INSERT INTO plan_rounds(generation_id, sequence, round_key, phase, planner_id, planner_version, "
                "source_task_keys_json, source_evidence_digest, task_set_digest, content_digest, committed_at) "
                "VALUES (?, 1, ?, ?, ?, ?, '[]', ?, ?, ?, ?)",
                (
                    generation_id,
                    round_key,
                    PlanPhase.INVENTORIES.value,
                    "inventory_planner",
                    "1",
                    source_evidence_digest,
                    cast(str, content["task_set_digest"]),
                    content_digest,
                    _timestamp(now),
                ),
            )
            connection.executemany(
                "INSERT INTO round_publications(generation_id, round_sequence, author_key, publication_key) "
                "VALUES (?, 1, ?, ?)",
                [(generation_id, publication.author_key, publication.publication_key) for publication in publications],
            )
            self._inject("after_initial_round_round")
            connection.execute(
                "UPDATE generations SET plan_revision = 1, plan_digest = ?, inventory_freshness_epoch = ?, "
                "updated_at = ? WHERE generation_id = ?",
                (_digest([content_digest]), epoch, _timestamp(now), generation_id),
            )
        self._inject("after_initial_round_commit")
        return self._load_round(self._connection, generation_id, 1)

    @staticmethod
    def _insert_publication(
        connection: sqlite3.Connection, generation_id: str, publication: PublicationMetadata
    ) -> None:
        connection.execute(
            "INSERT INTO publications(generation_id, author_key, publication_key, discovery_source, normalized_title, "
            "year, exact_identifiers_json, baseline_output_path, freshness_policy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                generation_id,
                publication.author_key,
                publication.publication_key,
                publication.discovery_source,
                publication.normalized_title,
                publication.year,
                _canonical(dict(publication.exact_identifiers)),
                publication.baseline_output_path,
                publication.freshness_policy,
            ),
        )

    def _load_round(self, connection: sqlite3.Connection, generation_id: str, sequence: int) -> PlanRound:
        row = connection.execute(
            "SELECT * FROM plan_rounds WHERE generation_id = ? AND sequence = ?", (generation_id, sequence)
        ).fetchone()
        if row is None:
            raise ValueError("missing plan round")
        planned = []
        for task_row in connection.execute(
            "SELECT task_key, expands_plan FROM plan_obligations WHERE generation_id = ? AND round_sequence = ? "
            "ORDER BY task_key",
            (generation_id, sequence),
        ):
            planned.append(PlannedTask(self._load_task(connection, generation_id, task_row[0]), bool(task_row[1])))
        publications = tuple(
            PublicationMetadata(
                publication["author_key"],
                publication["publication_key"],
                publication["discovery_source"],
                publication["normalized_title"],
                publication["year"],
                MappingProxyType(json.loads(publication["exact_identifiers_json"])),
                publication["baseline_output_path"],
                publication["freshness_policy"],
            )
            for publication in connection.execute(
                "SELECT publication.* FROM round_publications AS binding JOIN publications AS publication ON "
                "publication.generation_id = binding.generation_id AND publication.author_key = binding.author_key "
                "AND publication.publication_key = binding.publication_key WHERE binding.generation_id = ? "
                "AND binding.round_sequence = ? ORDER BY publication.author_key, publication.publication_key",
                (generation_id, sequence),
            )
        )
        return PlanRound(
            row["round_key"],
            row["sequence"],
            PlanPhase(row["phase"]),
            row["planner_id"],
            row["planner_version"],
            tuple(json.loads(row["source_task_keys_json"])),
            row["source_evidence_digest"],
            publications,
            tuple(planned),
            row["task_set_digest"],
            row["content_digest"],
        )

    @staticmethod
    def _load_task(connection: sqlite3.Connection, generation_id: str, task_key: str) -> TaskSpec:
        row = connection.execute(
            "SELECT task.*, request.identity_json FROM tasks AS task LEFT JOIN requests AS request ON "
            "request.generation_id = task.generation_id AND request.request_key = task.request_key "
            "WHERE task.generation_id = ? AND task.task_key = ?",
            (generation_id, task_key),
        ).fetchone()
        if row is None:
            raise ValueError("missing task")
        request = RequestSpec(**json.loads(row["identity_json"])) if row["identity_json"] else None
        return TaskSpec(
            row["author_key"],
            row["publication_key"],
            row["provider"],
            row["operation"],
            request,
            bool(row["required"]),
            row["applicability"],
        )

    def plan_status(self) -> PlanStatus:
        generation_id = self._generation_id()
        row = self._connection.execute(
            "SELECT plan_revision, plan_closed, plan_authority_mode, plan_digest, closure_digest FROM generations "
            "WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        open_expanders = self._connection.execute(
            "SELECT COUNT(*) FROM plan_obligations AS obligation LEFT JOIN reduction_sources AS source ON "
            "source.generation_id = obligation.generation_id AND source.source_task_key = obligation.task_key "
            "WHERE obligation.generation_id = ? AND obligation.expands_plan = 1 AND source.source_task_key IS NULL",
            (generation_id,),
        ).fetchone()[0]
        unbound = self._connection.execute(
            "SELECT COUNT(*) FROM tasks AS task LEFT JOIN plan_obligations AS obligation ON obligation.generation_id = "
            "task.generation_id AND obligation.task_key = task.task_key WHERE task.generation_id = ? "
            "AND obligation.task_key IS NULL",
            (generation_id,),
        ).fetchone()[0]
        return PlanStatus(row[0], bool(row[1]), row[2], row[3], row[4], int(open_expanders), int(unbound))

    @staticmethod
    def _source_evidence(
        connection: sqlite3.Connection, generation_id: str, task_key: str
    ) -> tuple[TaskDisposition, str]:
        row = connection.execute(
            "SELECT task.state, task.applicability_reason, observation.response_digest, observation.response_json, "
            "dominance.rule, "
            "dominance.stronger_observations_json, dominance.dominated_observation_key, dominance.covered_fields_json "
            "FROM tasks AS task LEFT JOIN observations AS observation ON "
            "observation.generation_id = task.generation_id "
            "AND observation.request_key = task.request_key LEFT JOIN dominance_evidence AS dominance ON "
            "dominance.generation_id = task.generation_id AND dominance.task_key = task.task_key "
            "WHERE task.generation_id = ? AND task.task_key = ?",
            (generation_id, task_key),
        ).fetchone()
        if row is None or TaskDisposition(row[0]) not in _SATISFIED:
            raise ValueError("reduction source is not satisfied")
        disposition = TaskDisposition(row[0])
        if disposition in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY}:
            if not row[2]:
                raise ValueError("reduction source lacks durable observation evidence")
            if row[3] is None or _digest(json.loads(row[3])) != row[2]:
                raise ValueError("reduction source observation digest mismatch")
            evidence_digest = row[2]
        elif disposition is TaskDisposition.NOT_APPLICABLE:
            evidence_digest = _digest({"applicability_reason": row[1], "task_key": task_key})
        else:
            if not row[4]:
                raise ValueError("dominated reduction source lacks durable evidence")
            evidence_digest = _digest(
                {
                    "covered_fields": json.loads(row[7]),
                    "dominated_observation_key": row[6],
                    "rule": row[4],
                    "stronger_observations": json.loads(row[5]),
                    "task_key": task_key,
                }
            )
        return disposition, evidence_digest

    def commit_reduction(
        self,
        source_task_key: str | Sequence[str],
        *,
        source_evidence_digest: str,
        publications: Sequence[PublicationMetadata],
        tasks: Sequence[PlannedTask],
        now: datetime,
        phase: PlanPhase = PlanPhase.DISCOVERY,
        reducer_id: str = "discovery_reducer",
        reducer_version: str = "1",
    ) -> ReductionReceipt:
        generation_id = self._generation_id()
        source_keys = (
            (source_task_key,)
            if isinstance(source_task_key, str)
            else tuple(sorted(_digest_text(key, "source task key") for key in source_task_key))
        )
        if not source_keys or len(set(source_keys)) != len(source_keys):
            raise ValueError("reduction requires unique source tasks")
        if len({item.task.key for item in tasks}) != len(tasks):
            raise ValueError("duplicate reduction task")
        supplied_digest = _digest_text(source_evidence_digest, "source evidence digest")
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state, plan_closed, plan_revision FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value or generation[1]:
                raise ValueError("reduction requires open running plan")
            if int(generation[2]) >= _MAX_PLAN_ROUNDS:
                raise ValueError("plan round policy limit exceeded")
            durable = []
            source_round_phases = set()
            for key in source_keys:
                source = connection.execute(
                    "SELECT obligation.expands_plan, obligation.round_sequence, round.phase FROM plan_obligations AS "
                    "obligation JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id AND "
                    "round.sequence = obligation.round_sequence WHERE obligation.generation_id = ? AND "
                    "obligation.task_key = ?",
                    (generation_id, key),
                ).fetchone()
                if source is None or not source[0]:
                    raise ValueError("reduction requires committed expanding source")
                source_round_phases.add(PlanPhase(source[2]))
                durable.append(self._source_evidence(connection, generation_id, key))
            durable_digests = tuple(item[1] for item in durable)
            aggregate_digest = durable_digests[0] if len(durable_digests) == 1 else _digest(list(durable_digests))
            if supplied_digest != aggregate_digest:
                raise ValueError("reduction evidence digest mismatch")
            allowed_edges = {
                PlanPhase.INVENTORIES: {PlanPhase.DISCOVERY, PlanPhase.AUTHORITATIVE},
                PlanPhase.DISCOVERY: {
                    PlanPhase.DISCOVERY,
                    PlanPhase.AUTHORITATIVE,
                    PlanPhase.LATE_IDENTIFIERS,
                    PlanPhase.REDUCERS,
                },
                PlanPhase.LATE_IDENTIFIERS: {
                    PlanPhase.LATE_IDENTIFIERS,
                    PlanPhase.AUTHORITATIVE,
                    PlanPhase.REDUCERS,
                },
                PlanPhase.AUTHORITATIVE: {
                    PlanPhase.AUTHORITATIVE,
                    PlanPhase.LATE_IDENTIFIERS,
                    PlanPhase.REDUCERS,
                },
                PlanPhase.REDUCERS: {PlanPhase.REDUCERS},
                PlanPhase.CLOSED: set(),
            }
            if any(phase not in allowed_edges[source_phase] for source_phase in source_round_phases):
                raise ValueError("invalid plan phase edge")
            prior = [
                row
                for source_key in source_keys
                if (
                    row := connection.execute(
                        "SELECT source.source_task_key, source.reduction_digest, round.sequence "
                        "FROM reduction_sources AS source JOIN reduction_receipts AS receipt ON "
                        "receipt.generation_id = source.generation_id AND "
                        "receipt.reduction_digest = source.reduction_digest JOIN plan_rounds AS round ON "
                        "round.generation_id = receipt.generation_id AND round.round_key = receipt.round_key "
                        "WHERE source.generation_id = ? AND source.source_task_key = ?",
                        (generation_id, source_key),
                    ).fetchone()
                )
                is not None
            ]
            if prior:
                if len(prior) != len(source_keys) or len({row[1] for row in prior}) != 1:
                    raise ValueError("conflicting reduction replay")
                replay_sequence = int(prior[0][2])
                replay_content = self._round_content(
                    replay_sequence,
                    phase,
                    reducer_id,
                    reducer_version,
                    source_keys,
                    supplied_digest,
                    publications,
                    tasks,
                )
                replay_reduction = _digest(
                    {
                        "content_digest": _digest(replay_content),
                        "source_dispositions": [item[0].value for item in durable],
                        "source_evidence_digests": list(durable_digests),
                        "source_task_keys": list(source_keys),
                    }
                )
                if replay_reduction != prior[0][1]:
                    raise ValueError("conflicting reduction replay")
                return self._load_receipt(connection, generation_id, replay_reduction)
            sequence = int(generation[2]) + 1
            content = self._round_content(
                sequence,
                phase,
                reducer_id,
                reducer_version,
                source_keys,
                supplied_digest,
                publications,
                tasks,
            )
            content_digest = _digest(content)
            round_key = _digest({"generation_id": generation_id, "content_digest": content_digest})
            reduction_content = {
                "content_digest": content_digest,
                "source_dispositions": [item[0].value for item in durable],
                "source_evidence_digests": list(durable_digests),
                "source_task_keys": list(source_keys),
            }
            reduction_digest = _digest(reduction_content)
            existing_task = any(
                connection.execute(
                    "SELECT 1 FROM tasks WHERE generation_id = ? AND task_key = ?",
                    (generation_id, item.task.key),
                ).fetchone()
                is not None
                for item in tasks
            )
            if existing_task:
                raise ValueError("reduction must append unseen task keys")
            for publication in publications:
                self._insert_publication(connection, generation_id, publication)
            self._inject("after_reduction_publications")
            for item in sorted(tasks, key=lambda value: value.task.key):
                self._insert_task(connection, generation_id, item.task)
            self._inject("after_reduction_tasks")
            connection.executemany(
                "INSERT INTO plan_obligations(generation_id, task_key, identity_digest, author_key, provider, "
                "operation, required, applicability, round_sequence, expands_plan) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        generation_id,
                        item.task.key,
                        item.task.identity_digest,
                        item.task.author_key,
                        item.task.provider,
                        item.task.operation,
                        int(item.task.required),
                        item.task.applicability,
                        sequence,
                        int(item.expands_plan),
                    )
                    for item in sorted(tasks, key=lambda value: value.task.key)
                ],
            )
            self._inject("after_reduction_obligations")
            connection.execute(
                "INSERT INTO plan_rounds(generation_id, sequence, round_key, phase, planner_id, planner_version, "
                "source_task_keys_json, source_evidence_digest, task_set_digest, content_digest, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    sequence,
                    round_key,
                    phase.value,
                    reducer_id,
                    reducer_version,
                    _canonical(source_keys),
                    supplied_digest,
                    content["task_set_digest"],
                    content_digest,
                    _timestamp(now),
                ),
            )
            connection.executemany(
                "INSERT INTO round_publications(generation_id, round_sequence, author_key, publication_key) "
                "VALUES (?, ?, ?, ?)",
                [(generation_id, sequence, item.author_key, item.publication_key) for item in publications],
            )
            self._inject("after_reduction_round")
            connection.execute(
                "INSERT INTO reduction_receipts(generation_id, reduction_digest, round_key, source_task_keys_json, "
                "source_dispositions_json, source_evidence_digests_json, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    reduction_digest,
                    round_key,
                    _canonical(source_keys),
                    _canonical([item[0].value for item in durable]),
                    _canonical(durable_digests),
                    _timestamp(now),
                ),
            )
            connection.executemany(
                "INSERT INTO reduction_sources(generation_id, source_task_key, reduction_digest) VALUES (?, ?, ?)",
                [(generation_id, key, reduction_digest) for key in source_keys],
            )
            self._inject("after_reduction_receipt")
            cumulative = _digest(
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT content_digest FROM plan_rounds WHERE generation_id = ? ORDER BY sequence",
                        (generation_id,),
                    )
                ]
            )
            connection.execute(
                "UPDATE generations SET plan_revision = ?, plan_digest = ?, updated_at = ? WHERE generation_id = ?",
                (sequence, cumulative, _timestamp(now), generation_id),
            )
        self._inject("after_reduction_commit")
        return self._load_receipt(self._connection, generation_id, reduction_digest)

    @staticmethod
    def _load_receipt(connection: sqlite3.Connection, generation_id: str, reduction_digest: str) -> ReductionReceipt:
        row = connection.execute(
            "SELECT * FROM reduction_receipts WHERE generation_id = ? AND reduction_digest = ?",
            (generation_id, reduction_digest),
        ).fetchone()
        if row is None:
            raise ValueError("missing reduction receipt")
        return ReductionReceipt(
            tuple(json.loads(row["source_task_keys_json"])),
            row["round_key"],
            tuple(TaskDisposition(value) for value in json.loads(row["source_dispositions_json"])),
            tuple(json.loads(row["source_evidence_digests_json"])),
            row["reduction_digest"],
        )

    def closure_content(self, required_validations: Sequence[ValidationSpec] = ()) -> Mapping[str, object]:
        generation_id = self._generation_id()
        connection = self._connection
        rounds = [
            {
                "content_digest": row["content_digest"],
                "phase": row["phase"],
                "planner_id": row["planner_id"],
                "planner_version": row["planner_version"],
                "round_key": row["round_key"],
                "sequence": row["sequence"],
                "source_evidence_digest": row["source_evidence_digest"],
                "source_task_keys": json.loads(row["source_task_keys_json"]),
                "task_set_digest": row["task_set_digest"],
            }
            for row in connection.execute(
                "SELECT * FROM plan_rounds WHERE generation_id = ? ORDER BY sequence", (generation_id,)
            )
        ]
        obligations = [
            dict(row)
            for row in connection.execute(
                "SELECT task_key, identity_digest, author_key, provider, operation, required, applicability, "
                "round_sequence, expands_plan FROM plan_obligations WHERE generation_id = ? ORDER BY task_key",
                (generation_id,),
            )
        ]
        publications = [
            {
                **{key: value for key, value in dict(row).items() if key != "exact_identifiers_json"},
                "exact_identifiers": json.loads(row["exact_identifiers_json"]),
            }
            for row in connection.execute(
                "SELECT author_key, publication_key, discovery_source, normalized_title, year, exact_identifiers_json, "
                "baseline_output_path, freshness_policy FROM publications WHERE generation_id = ? "
                "ORDER BY author_key, publication_key",
                (generation_id,),
            )
        ]
        receipts = [
            {
                "reduction_digest": row["reduction_digest"],
                "round_key": row["round_key"],
                "source_dispositions": json.loads(row["source_dispositions_json"]),
                "source_evidence_digests": json.loads(row["source_evidence_digests_json"]),
                "source_task_keys": json.loads(row["source_task_keys_json"]),
            }
            for row in connection.execute(
                "SELECT * FROM reduction_receipts WHERE generation_id = ? ORDER BY round_key", (generation_id,)
            )
        ]
        task_outcomes = [
            dict(row)
            for row in connection.execute(
                "SELECT task_key, state, applicability_reason, dominance_reason FROM tasks "
                "WHERE generation_id = ? ORDER BY task_key",
                (generation_id,),
            )
        ]
        observations = [
            dict(row)
            for row in connection.execute(
                "SELECT request_key, disposition, response_digest, provider, schema_version, authoritative_empty "
                "FROM observations WHERE generation_id = ? ORDER BY request_key",
                (generation_id,),
            )
        ]
        dominance = [
            {
                "covered_fields": json.loads(row[4]),
                "dominated_observation_key": row[3],
                "rule": row[2],
                "stronger_observations": json.loads(row[1]),
                "task_key": row[0],
            }
            for row in connection.execute(
                "SELECT task_key, stronger_observations_json, rule, dominated_observation_key, covered_fields_json "
                "FROM dominance_evidence WHERE generation_id = ? ORDER BY task_key",
                (generation_id,),
            )
        ]
        existing_validations = [
            row[0]
            for row in connection.execute(
                "SELECT check_name FROM validation_obligations WHERE generation_id = ? ORDER BY check_name",
                (generation_id,),
            )
        ]
        validation_names = (
            sorted(validation.name for validation in required_validations)
            if required_validations
            else existing_validations
        )
        freshness = connection.execute(
            "SELECT inventory_freshness_epoch FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()[0]
        return MappingProxyType(
            {
                "generation_id": generation_id,
                "inventory_freshness_epoch": freshness,
                "obligations": obligations,
                "observations": observations,
                "publications": publications,
                "receipts": receipts,
                "required_validations": validation_names,
                "rounds": rounds,
                "structural_authority_version": "1",
                "task_outcomes": task_outcomes,
                "typed_dominance": dominance,
            }
        )

    def _verify_structural_closure(self, connection: sqlite3.Connection, generation_id: str) -> None:
        sequences = [
            row[0]
            for row in connection.execute(
                "SELECT sequence FROM plan_rounds WHERE generation_id = ? ORDER BY sequence", (generation_id,)
            )
        ]
        if not sequences or sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("plan rounds are missing or noncontiguous")
        for sequence in sequences:
            round_value = self._load_round(connection, generation_id, sequence)
            content = self._round_content(
                sequence,
                round_value.phase,
                round_value.planner_id,
                round_value.planner_version,
                round_value.source_task_keys,
                round_value.source_evidence_digest,
                round_value.publications,
                round_value.tasks,
            )
            content_digest = _digest(content)
            expected_key = _digest({"generation_id": generation_id, "content_digest": content_digest})
            if (
                content_digest != round_value.content_digest
                or content["task_set_digest"] != round_value.task_set_digest
                or expected_key != round_value.key
            ):
                raise ValueError("plan round content integrity mismatch")
        unbound = connection.execute(
            "SELECT COUNT(*) FROM tasks AS task LEFT JOIN plan_obligations AS obligation ON obligation.generation_id = "
            "task.generation_id AND obligation.task_key = task.task_key WHERE task.generation_id = ? AND "
            "(obligation.task_key IS NULL OR obligation.round_sequence IS NULL)",
            (generation_id,),
        ).fetchone()[0]
        if unbound:
            raise ValueError("plan contains unbound task")
        placeholders = ",".join("?" for _ in _SATISFIED)
        unsatisfied = connection.execute(
            f"SELECT COUNT(*) FROM plan_obligations AS obligation JOIN tasks AS task ON "  # noqa: S608
            f"task.generation_id = obligation.generation_id AND task.task_key = obligation.task_key WHERE "
            f"obligation.generation_id = ? AND task.state NOT IN ({placeholders})",
            (generation_id, *(state.value for state in sorted(_SATISFIED, key=lambda state: state.value))),
        ).fetchone()[0]
        if unsatisfied:
            raise ValueError("plan contains open or blocking work")
        for observation in connection.execute(
            "SELECT disposition, response_json, response_digest FROM observations WHERE generation_id = ?",
            (generation_id,),
        ):
            if observation[0] in {
                TaskDisposition.SUCCEEDED.value,
                TaskDisposition.CONFIRMED_EMPTY.value,
            } and (
                observation[1] is None
                or observation[2] is None
                or _digest(json.loads(observation[1])) != observation[2]
            ):
                raise ValueError("terminal observation content digest mismatch")
        expanding = {
            row[0]
            for row in connection.execute(
                "SELECT task_key FROM plan_obligations WHERE generation_id = ? AND expands_plan = 1",
                (generation_id,),
            )
        }
        consumed = [
            row[0]
            for row in connection.execute(
                "SELECT source_task_key FROM reduction_sources WHERE generation_id = ?", (generation_id,)
            )
        ]
        if len(consumed) != len(set(consumed)) or set(consumed) != expanding:
            raise ValueError("expanding task is unconsumed or multiply consumed")
        for key in consumed:
            disposition, evidence_digest = self._source_evidence(connection, generation_id, key)
            row = connection.execute(
                "SELECT receipt.source_dispositions_json, receipt.source_evidence_digests_json, "
                "receipt.source_task_keys_json FROM reduction_sources AS source JOIN reduction_receipts AS receipt ON "
                "receipt.generation_id = source.generation_id AND receipt.reduction_digest = source.reduction_digest "
                "WHERE source.generation_id = ? AND source.source_task_key = ?",
                (generation_id, key),
            ).fetchone()
            keys = json.loads(row[2])
            index = keys.index(key)
            if json.loads(row[0])[index] != disposition.value or json.loads(row[1])[index] != evidence_digest:
                raise ValueError("reduction receipt evidence mismatch")
        later_rounds = connection.execute(
            "SELECT COUNT(*) FROM plan_rounds WHERE generation_id = ? AND sequence > 1", (generation_id,)
        ).fetchone()[0]
        receipts = connection.execute(
            "SELECT COUNT(*) FROM reduction_receipts WHERE generation_id = ?", (generation_id,)
        ).fetchone()[0]
        if later_rounds != receipts:
            raise ValueError("noninitial round lacks exact reduction receipt")
        generation = connection.execute(
            "SELECT plan_closed, closure_digest FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if generation[0]:
            actual_closure = _digest(dict(self.closure_content()))
            if generation[1] != actual_closure:
                raise ValueError("structural closure digest mismatch")

    def close_plan(
        self,
        *,
        expected_closure_digest: str,
        required_validations: Sequence[ValidationSpec] = (),
        inventory_freshness_epoch: str | None = None,
        now: datetime,
    ) -> PlanStatus:
        generation_id = self._generation_id()
        expected = _digest_text(expected_closure_digest, "closure digest")
        if len({item.name for item in required_validations}) != len(required_validations):
            raise ValueError("duplicate required validation")
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state, plan_closed, closure_digest, plan_authority_mode FROM generations "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value:
                raise ValueError("structural plan closure requires running generation")
            if generation[1]:
                if generation[2] != expected or generation[3] != "phased_structural":
                    raise ValueError("conflicting plan closure replay")
                return self.plan_status()
            self._verify_structural_closure(connection, generation_id)
            if inventory_freshness_epoch is not None:
                current = connection.execute(
                    "SELECT inventory_freshness_epoch FROM generations WHERE generation_id = ?", (generation_id,)
                ).fetchone()[0]
                if current != inventory_freshness_epoch:
                    raise ValueError("inventory freshness epoch mismatch")
            candidate = _digest(dict(self.closure_content(required_validations)))
            if candidate != expected:
                raise ValueError("closure digest mismatch")
            connection.executemany(
                "INSERT INTO validation_obligations(generation_id, check_name, required) VALUES (?, ?, 1)",
                [(generation_id, item.name) for item in sorted(required_validations, key=lambda item: item.name)],
            )
            self._inject("after_plan_close_validations")
            connection.execute(
                "UPDATE generations SET plan_closed = 1, plan_sealed = 1, closure_digest = ?, plan_digest = ?, "
                "plan_authority_mode = 'phased_structural', updated_at = ? WHERE generation_id = ?",
                (expected, expected, _timestamp(now), generation_id),
            )
        self._inject("after_plan_close_commit")
        return self.plan_status()

    def plan_task(self, task: TaskSpec) -> TaskClaim:
        generation_id = self._generation_id()
        request_key = task.request.key if task.request is not None else None
        request_identity = _canonical(task.request.canonical_content()) if task.request is not None else None
        with self._transaction(immediate=True) as connection:
            sealed = connection.execute(
                "SELECT plan_sealed FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if sealed is None or sealed[0]:
                raise ValueError("generation plan is sealed")
            if task.request is not None:
                connection.execute(
                    "INSERT OR IGNORE INTO requests(generation_id, request_key, identity_json, state) "
                    "VALUES (?, ?, ?, ?)",
                    (generation_id, request_key, request_identity, TaskDisposition.PENDING.value),
                )
                stored_request = connection.execute(
                    "SELECT identity_json FROM requests WHERE generation_id = ? AND request_key = ?",
                    (generation_id, request_key),
                ).fetchone()
                if stored_request is None or stored_request[0] != request_identity:
                    raise ValueError("exact request identity collision")
            existing = connection.execute(
                "SELECT author_key, publication_key, provider, operation, request_key, required, applicability, "
                "identity_digest FROM tasks "
                "WHERE generation_id = ? AND task_key = ?",
                (generation_id, task.key),
            ).fetchone()
            expected = (
                task.author_key,
                task.publication_key,
                task.provider,
                task.operation,
                request_key,
                int(task.required),
                task.applicability,
                task.identity_digest,
            )
            if existing is not None and tuple(existing) != expected:
                raise ValueError("task identity mismatch")
            if existing is None:
                if task.publication_key is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO publications(generation_id, author_key, publication_key) "
                        "VALUES (?, ?, ?)",
                        (generation_id, task.author_key, task.publication_key),
                    )
                connection.execute(
                    "INSERT INTO tasks(generation_id, task_key, author_key, publication_key, provider, operation, "
                    "request_key, required, applicability, identity_digest, state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (generation_id, task.key, *expected, TaskDisposition.PENDING.value),
                )
                if request_key is not None:
                    connection.execute(
                        "INSERT INTO request_consumers(generation_id, request_key, task_key) VALUES (?, ?, ?)",
                        (generation_id, request_key, task.key),
                    )
        return TaskClaim(task.key, request_key, "", datetime.min.replace(tzinfo=timezone.utc))

    def seal_plan(
        self,
        expected_tasks: list[TaskSpec],
        *,
        required_validations: tuple[ValidationSpec, ...] = (),
        inventory_freshness_epoch: str | None = None,
    ) -> None:
        generation_id = self._generation_id()
        declared = {task.key: task for task in expected_tasks}
        if len(declared) != len(expected_tasks):
            raise ValueError("duplicate expected obligations")
        with self._transaction(immediate=True) as connection:
            sealed = connection.execute(
                "SELECT plan_sealed FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if sealed is None or sealed[0]:
                raise ValueError("generation plan is sealed")
            actual = {
                row[0]: tuple(row[1:])
                for row in connection.execute(
                    "SELECT task_key, identity_digest, author_key, provider, operation, required, applicability "
                    "FROM tasks WHERE generation_id = ? ORDER BY task_key",
                    (generation_id,),
                )
            }
            expected = {
                task.key: (
                    task.identity_digest,
                    task.author_key,
                    task.provider,
                    task.operation,
                    int(task.required),
                    task.applicability,
                )
                for task in expected_tasks
            }
            if actual != expected:
                raise ValueError("expected obligations do not exactly match planned tasks")
            census_rows = list(
                connection.execute(
                    "SELECT row_key, scholar_id, dblp_id FROM authors WHERE generation_id = ? AND enabled = 1",
                    (generation_id,),
                )
            )
            generation_identity = json.loads(
                connection.execute(
                    "SELECT identity_json FROM generations WHERE generation_id = ?", (generation_id,)
                ).fetchone()[0]
            )
            mandatory_sources = [
                (row["row_key"], provider, provider_id)
                for row in census_rows
                for provider, provider_id in (("scholar", row["scholar_id"]), ("dblp", row["dblp_id"]))
                if provider_id
            ]
            if mandatory_sources and inventory_freshness_epoch is None:
                raise ValueError("canonical inventory obligations require an explicit freshness epoch")
            if inventory_freshness_epoch is not None:
                inventory_freshness_epoch = _identifier(inventory_freshness_epoch, "inventory freshness epoch")
            canonical_inventory: list[TaskSpec] = []
            for author_key, provider, profile_id in mandatory_sources:
                adapter_version = generation_identity["adapter_versions"].get(provider)
                if adapter_version is None:
                    raise ValueError(f"generation lacks adapter version for inventory provider {provider}")
                request = RequestSpec(
                    provider,
                    "inventory",
                    "GET",
                    {"profile_id": profile_id},
                    ("publications",),
                    adapter_version,
                    cast(str, inventory_freshness_epoch),
                    provider,
                )
                canonical_inventory.append(TaskSpec(author_key, None, provider, "inventory", request))
            declared_inventory = [task for task in expected_tasks if task.operation == "inventory"]
            if [task.key for task in sorted(declared_inventory, key=lambda item: item.key)] != [
                task.key for task in sorted(canonical_inventory, key=lambda item: item.key)
            ]:
                raise ValueError("declared tasks do not match full canonical inventory obligations")
            connection.executemany(
                "INSERT INTO plan_obligations(generation_id, task_key, identity_digest, author_key, provider, "
                "operation, required, applicability, round_sequence, expands_plan) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0)",
                [
                    (
                        generation_id,
                        task.key,
                        task.identity_digest,
                        task.author_key,
                        task.provider,
                        task.operation,
                        int(task.required),
                        task.applicability,
                    )
                    for task in sorted(expected_tasks, key=lambda item: item.key)
                ],
            )
            connection.executemany(
                "INSERT INTO validation_obligations(generation_id, check_name, required) VALUES (?, ?, 1)",
                [
                    (generation_id, validation.name)
                    for validation in sorted(required_validations, key=lambda item: item.name)
                ],
            )
            connection.execute(
                "UPDATE generations SET inventory_freshness_epoch = ? WHERE generation_id = ?",
                (inventory_freshness_epoch, generation_id),
            )
            plan_content = self._plan_content(connection, generation_id)
            content_digest = _digest(plan_content)
            round_key = _digest({"generation_id": generation_id, "legacy_plan": content_digest})
            connection.execute(
                "INSERT INTO plan_rounds(generation_id, sequence, round_key, phase, planner_id, planner_version, "
                "source_task_keys_json, source_evidence_digest, task_set_digest, content_digest, committed_at) "
                "VALUES (?, 1, ?, ?, 'legacy_adapter', '1', '[]', ?, ?, ?, ?)",
                (
                    generation_id,
                    round_key,
                    PlanPhase.INVENTORIES.value,
                    "0" * 64,
                    _digest(sorted(task.key for task in expected_tasks)),
                    content_digest,
                    _timestamp(datetime.now(timezone.utc)),
                ),
            )
            connection.execute(
                "UPDATE generations SET plan_sealed = 1, plan_closed = 1, plan_revision = 1, "
                "plan_authority_mode = 'legacy_compatibility', plan_digest = ?, closure_digest = ?, updated_at = ? "
                "WHERE generation_id = ?",
                (content_digest, content_digest, _timestamp(datetime.now(timezone.utc)), generation_id),
            )

    @staticmethod
    def _plan_content(connection: sqlite3.Connection, generation_id: str) -> dict[str, object]:
        tasks = []
        for row in connection.execute(
            "SELECT obligation.task_key, obligation.identity_digest, obligation.author_key, obligation.provider, "
            "obligation.operation, obligation.required, obligation.applicability, task.publication_key, "
            "request.identity_json FROM plan_obligations AS obligation JOIN tasks AS task ON task.generation_id = "
            "obligation.generation_id AND task.task_key = obligation.task_key LEFT JOIN requests AS request ON "
            "request.generation_id = task.generation_id AND request.request_key = task.request_key "
            "WHERE obligation.generation_id = ? ORDER BY obligation.task_key",
            (generation_id,),
        ):
            item = dict(row)
            identity_json = item.pop("identity_json")
            item["request"] = json.loads(identity_json) if identity_json is not None else None
            tasks.append(item)
        validations = [
            {"name": row[0], "required": bool(row[1])}
            for row in connection.execute(
                "SELECT check_name, required FROM validation_obligations WHERE generation_id = ? ORDER BY check_name",
                (generation_id,),
            )
        ]
        freshness = connection.execute(
            "SELECT inventory_freshness_epoch FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()[0]
        return {"inventory_freshness_epoch": freshness, "tasks": tasks, "validations": validations}

    @classmethod
    def _verify_plan_integrity(cls, connection: sqlite3.Connection, generation_id: str) -> None:
        generation = connection.execute(
            "SELECT plan_sealed, plan_digest, plan_authority_mode FROM generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        if generation is None or not generation[0]:
            return
        live_tasks = connection.execute(
            "SELECT task.task_key, task.identity_digest, task.author_key, task.publication_key, task.provider, "
            "task.operation, task.request_key, task.required, task.applicability, obligation.identity_digest AS "
            "obligation_digest, obligation.author_key AS obligation_author, obligation.provider AS "
            "obligation_provider, obligation.operation AS obligation_operation, obligation.required AS "
            "obligation_required, obligation.applicability AS obligation_applicability, request.identity_json "
            "FROM tasks AS task JOIN plan_obligations AS obligation ON obligation.generation_id = task.generation_id "
            "AND obligation.task_key = task.task_key LEFT JOIN requests AS request ON request.generation_id = "
            "task.generation_id AND request.request_key = task.request_key WHERE task.generation_id = ?",
            (generation_id,),
        ).fetchall()
        for task in live_tasks:
            request_key = task["request_key"]
            if request_key is not None and (
                task["identity_json"] is None or _digest(json.loads(task["identity_json"])) != request_key
            ):
                raise ValueError("sealed request identity mismatch")
            canonical_task = {
                "applicability": task["applicability"],
                "author_key": task["author_key"],
                "operation": task["operation"],
                "provider": task["provider"],
                "publication_key": task["publication_key"],
                "request_key": request_key,
                "required": bool(task["required"]),
            }
            if (
                _digest(canonical_task) != task["task_key"]
                or task["identity_digest"] != task["task_key"]
                or task["identity_digest"] != task["obligation_digest"]
                or task["author_key"] != task["obligation_author"]
                or task["provider"] != task["obligation_provider"]
                or task["operation"] != task["obligation_operation"]
                or task["required"] != task["obligation_required"]
                or task["applicability"] != task["obligation_applicability"]
            ):
                raise ValueError("sealed live task identity mismatch")
        if generation[2] == "legacy_compatibility" and generation[1] != _digest(
            cls._plan_content(connection, generation_id)
        ):
            raise ValueError("sealed plan obligation digest mismatch")

    def claim_due(self, owner: str, now: datetime, lease_for: timedelta) -> TaskClaim | None:
        owner = _identifier(owner, "lease owner")
        if lease_for <= timedelta(0):
            raise ValueError("claim owner and positive lease are required")
        generation_id = self._generation_id()
        now_text = _timestamp(now)
        expires = now + lease_for
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value:
                return None
            candidate = connection.execute(
                "SELECT task.task_key FROM tasks AS task JOIN plan_obligations AS obligation ON "
                "obligation.generation_id = task.generation_id AND obligation.task_key = task.task_key "
                "WHERE task.generation_id = ? AND obligation.round_sequence IS NOT NULL AND "
                "task.request_key IS NOT NULL AND "
                "((task.state IN (?, ?) AND "
                "(task.next_attempt_at IS NULL OR task.next_attempt_at <= ?)) OR "
                "(task.state = ? AND task.lease_expires_at <= ?)) "
                "ORDER BY task.task_key LIMIT 1",
                (
                    generation_id,
                    TaskDisposition.PENDING.value,
                    TaskDisposition.RETRY_WAIT.value,
                    now_text,
                    TaskDisposition.LEASED.value,
                    now_text,
                ),
            ).fetchone()
            if candidate is None:
                return None
            cursor = connection.execute(
                "UPDATE tasks SET state = ?, lease_owner = ?, lease_expires_at = ? WHERE generation_id = ? "
                "AND task_key = ? AND ((state IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) "
                "OR (state = ? AND lease_expires_at <= ?))",
                (
                    TaskDisposition.LEASED.value,
                    owner,
                    _timestamp(expires),
                    generation_id,
                    candidate[0],
                    TaskDisposition.PENDING.value,
                    TaskDisposition.RETRY_WAIT.value,
                    now_text,
                    TaskDisposition.LEASED.value,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT request_key FROM tasks WHERE generation_id = ? AND task_key = ?",
                (generation_id, candidate[0]),
            ).fetchone()
        self._inject("after_claim_commit")
        return TaskClaim(str(candidate[0]), str(row[0]), owner, expires)

    def claim_request(self, task_key: str, owner: str, now: datetime, lease_for: timedelta) -> RequestClaim | None:
        task_key = _digest_text(task_key, "task key")
        owner = _identifier(owner, "lease owner")
        if lease_for <= timedelta(0):
            raise ValueError("positive request lease is required")
        generation_id = self._generation_id()
        now_text = _timestamp(now)
        expires = now + lease_for
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value:
                raise ValueError("request claims require a running generation")
            task = connection.execute(
                "SELECT request_key, lease_owner, lease_expires_at, state FROM tasks WHERE generation_id = ? "
                "AND task_key = ?",
                (generation_id, task_key),
            ).fetchone()
            if task is None or task[1] != owner or task[3] != TaskDisposition.LEASED.value or task[2] < now_text:
                raise ValueError("stale owner cannot claim request")
            cursor = connection.execute(
                "UPDATE requests SET state = ?, lease_owner = ?, lease_expires_at = ? WHERE generation_id = ? "
                "AND request_key = ? AND ((state IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) "
                "OR (state = ? AND lease_expires_at <= ?))",
                (
                    TaskDisposition.LEASED.value,
                    owner,
                    _timestamp(expires),
                    generation_id,
                    task[0],
                    TaskDisposition.PENDING.value,
                    TaskDisposition.RETRY_WAIT.value,
                    now_text,
                    TaskDisposition.LEASED.value,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return RequestClaim(str(task[0]), owner, expires)

    def _assert_owner(self, table: str, key_column: str, key: str, owner: str, at: datetime) -> sqlite3.Row:
        generation_id = self._generation_id()
        row = self._connection.execute(
            f"SELECT state, lease_owner, lease_expires_at FROM {table} "  # noqa: S608 - identifiers are internal constants
            f"WHERE generation_id = ? AND {key_column} = ?",
            (generation_id, key),
        ).fetchone()
        if row is None or row[0] != TaskDisposition.LEASED.value or row[1] != owner or row[2] < _timestamp(at):
            raise ValueError("stale owner cannot mutate leased work")
        return cast(sqlite3.Row, row)

    def record_attempt(
        self,
        request_key: str,
        owner: str,
        started_at: datetime,
        finished_at: datetime,
        outcome: str,
        *,
        http_status: int | None = None,
        retry_delay: float | None = None,
        response_digest: str | None = None,
        safe_diagnostic: str = "",
    ) -> int:
        request_key = _digest_text(request_key, "request key")
        owner = _identifier(owner, "lease owner")
        diagnostic = _free_text(safe_diagnostic, "attempt diagnostic")
        outcome = _identifier(outcome, "attempt outcome")
        if http_status is not None and not 100 <= http_status <= 599:
            raise ValueError("invalid HTTP status")
        if retry_delay is not None and retry_delay < 0:
            raise ValueError("invalid retry delay")
        if response_digest is not None:
            _digest_text(response_digest, "response digest")
        generation_id = self._generation_id()
        with self._transaction(immediate=True) as connection:
            self._assert_owner("requests", "request_key", request_key, owner, finished_at)
            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM attempts WHERE generation_id = ? "
                    "AND request_key = ?",
                    (generation_id, request_key),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO attempts(generation_id, request_key, attempt_number, started_at, finished_at, outcome, "
                "http_status, retry_delay_seconds, response_digest, safe_diagnostic) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    request_key,
                    number,
                    _timestamp(started_at),
                    _timestamp(finished_at),
                    outcome,
                    http_status,
                    retry_delay,
                    response_digest,
                    diagnostic,
                ),
            )
            connection.execute(
                "UPDATE tasks SET attempt_count = attempt_count + 1 WHERE generation_id = ? AND request_key = ?",
                (generation_id, request_key),
            )
        self._inject("after_attempt_commit")
        return number

    def finish_request(
        self,
        request_key: str,
        owner: str,
        disposition: TaskDisposition,
        now: datetime,
        *,
        retry_at: datetime | None = None,
        response_digest: str | None = None,
        observation: ProviderObservation | None = None,
        safe_diagnostic: str = "",
    ) -> None:
        request_key = _digest_text(request_key, "request key")
        owner = _identifier(owner, "lease owner")
        if disposition not in _TERMINAL | {TaskDisposition.RETRY_WAIT}:
            raise ValueError("invalid request finish disposition")
        if disposition is TaskDisposition.RETRY_WAIT and retry_at is None:
            raise ValueError("retry wait requires a durable retry deadline")
        diagnostic = _free_text(safe_diagnostic, "request diagnostic")
        if disposition in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY} and observation is None:
            raise ValueError("terminal request requires a validated provider observation")
        if (
            disposition is TaskDisposition.CONFIRMED_EMPTY
            and observation is not None
            and not observation.authoritative_empty
        ):
            raise ValueError("confirmed empty requires authoritative empty observation")
        if disposition is TaskDisposition.SUCCEEDED and observation is not None and observation.authoritative_empty:
            raise ValueError("successful request cannot use authoritative empty observation")
        response_json: str | None = None
        if observation is not None:
            response_json = _canonical(dict(observation.response))
            calculated_digest = observation.digest
            if response_digest is not None and response_digest != calculated_digest:
                raise ValueError("response digest does not match normalized response")
            response_digest = calculated_digest
        generation_id = self._generation_id()
        with self._transaction(immediate=True) as connection:
            self._assert_owner("requests", "request_key", request_key, owner, now)
            request_identity = connection.execute(
                "SELECT identity_json FROM requests WHERE generation_id = ? AND request_key = ?",
                (generation_id, request_key),
            ).fetchone()
            if request_identity is None:
                raise ValueError("request identity missing")
            identity = json.loads(request_identity[0])
            if observation is not None and observation.provider != identity["provider"]:
                raise ValueError("observation provider does not match request")
            if disposition is TaskDisposition.SUCCEEDED and observation is not None:
                missing = [
                    field_name
                    for field_name in identity["requested_fields"]
                    if field_name not in observation.response
                    or not _has_observation_value(observation.response[field_name])
                ]
                if missing:
                    raise ValueError(f"successful observation lacks requested field evidence: {', '.join(missing)}")
            if disposition is TaskDisposition.CONFIRMED_EMPTY and observation is not None and observation.response:
                raise ValueError("confirmed empty requires an empty response")
            connection.execute(
                "UPDATE requests SET state = ?, next_attempt_at = ?, lease_owner = NULL, lease_expires_at = NULL, "
                "response_digest = ?, safe_diagnostic = ? WHERE generation_id = ? AND request_key = ?",
                (
                    disposition.value,
                    _timestamp(retry_at) if retry_at else None,
                    response_digest,
                    diagnostic,
                    generation_id,
                    request_key,
                ),
            )
            if disposition in _TERMINAL:
                connection.execute(
                    "INSERT INTO observations(generation_id, request_key, disposition, response_json, response_digest, "
                    "provider, schema_version, authoritative_empty, observed_at, safe_diagnostic) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        request_key,
                        disposition.value,
                        response_json,
                        response_digest,
                        observation.provider if observation is not None else identity["provider"],
                        observation.schema_version if observation is not None else identity["adapter_version"],
                        int(observation.authoritative_empty) if observation is not None else 0,
                        _timestamp(now),
                        diagnostic,
                    ),
                )
        self._inject("after_response_commit")

    def request_result(self, request_key: str) -> RequestResult | None:
        row = self._connection.execute(
            "SELECT observation.disposition, observation.response_json, observation.response_digest, "
            "attempt.outcome, attempt.http_status FROM observations AS observation LEFT JOIN attempts AS attempt "
            "ON attempt.generation_id = observation.generation_id AND attempt.request_key = observation.request_key "
            "AND attempt.attempt_number = (SELECT MAX(latest.attempt_number) FROM attempts AS latest WHERE "
            "latest.generation_id = observation.generation_id AND latest.request_key = observation.request_key) "
            "WHERE observation.generation_id = ? AND observation.request_key = ?",
            (self._generation_id(), request_key),
        ).fetchone()
        if row is None:
            return None
        response = MappingProxyType(json.loads(row[1])) if row[1] is not None else None
        return RequestResult(request_key, TaskDisposition(row[0]), response, row[2], row[3], row[4])

    def request_attempt_count(self, request_key: str) -> int:
        """Return the durable physical-attempt count for one exact request."""
        request_key = _digest_text(request_key, "request key")
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE generation_id = ? AND request_key = ?",
                (self._generation_id(), request_key),
            ).fetchone()[0]
        )

    def finish_task(
        self,
        task_key: str,
        owner: str,
        disposition: TaskDisposition,
        now: datetime,
        *,
        retry_at: datetime | None = None,
        evidence: ApplicabilityReason | DominanceEvidence | None = None,
        reason: str = "",
    ) -> None:
        task_key = _digest_text(task_key, "task key")
        owner = _identifier(owner, "lease owner")
        generation_id = self._generation_id()
        current = self._connection.execute(
            "SELECT task.state, request.state FROM tasks AS task LEFT JOIN requests AS request "
            "ON request.generation_id = task.generation_id AND request.request_key = task.request_key "
            "WHERE task.generation_id = ? AND task.task_key = ?",
            (generation_id, task_key),
        ).fetchone()
        if current is not None and TaskDisposition(current[0]) in _TERMINAL:
            raise ValueError("terminal task state is immutable")
        if disposition not in _TERMINAL | {TaskDisposition.RETRY_WAIT}:
            raise ValueError("invalid task finish disposition")
        if disposition is TaskDisposition.RETRY_WAIT and retry_at is None:
            raise ValueError("retry wait requires a durable retry deadline")
        safe_reason = _free_text(reason, "task reason")
        with self._transaction(immediate=True) as connection:
            task_evidence = connection.execute(
                "SELECT applicability, request_key, state FROM tasks WHERE generation_id = ? AND task_key = ?",
                (generation_id, task_key),
            ).fetchone()
            if disposition is TaskDisposition.NOT_APPLICABLE:
                if (
                    task_evidence is None
                    or task_evidence[0] != "not_applicable"
                    or not isinstance(evidence, ApplicabilityReason)
                ):
                    raise ValueError("not applicable terminalization requires matching typed applicability evidence")
                if task_evidence[1] is not None or task_evidence[2] != TaskDisposition.PENDING.value:
                    raise ValueError("not applicable task must be pending without a request")
                applicability_reason = evidence.value
            else:
                self._assert_owner("tasks", "task_key", task_key, owner, now)
                applicability_reason = ""
            if disposition is TaskDisposition.DOMINATED:
                if not isinstance(evidence, DominanceEvidence):
                    raise ValueError("dominated terminalization requires typed dominance evidence")
                terminal_observations = [
                    connection.execute(
                        "SELECT request_key, provider, disposition, response_json FROM observations "
                        "WHERE generation_id = ? AND request_key = ?",
                        (generation_id, observation_key),
                    ).fetchone()
                    for observation_key in evidence.stronger_observation_keys
                ]
                terminal_observations = [item for item in terminal_observations if item is not None]
                if len(terminal_observations) != len(evidence.stronger_observation_keys) or any(
                    item["disposition"] != TaskDisposition.SUCCEEDED.value for item in terminal_observations
                ):
                    raise ValueError("dominance evidence must reference succeeded observations")
                lower_observation = connection.execute(
                    "SELECT observation.request_key, observation.provider, observation.disposition, "
                    "observation.response_json, request.identity_json FROM observations AS observation "
                    "JOIN requests AS request ON request.generation_id = observation.generation_id "
                    "AND request.request_key = observation.request_key "
                    "WHERE observation.generation_id = ? AND observation.request_key = ?",
                    (generation_id, evidence.dominated_observation_key),
                ).fetchone()
                if lower_observation is None or lower_observation["disposition"] != TaskDisposition.SUCCEEDED.value:
                    raise ValueError("dominance evidence requires a succeeded dominated observation")
                request_identity = connection.execute(
                    "SELECT request.identity_json, task.request_key, task.provider, task.author_key, "
                    "task.publication_key, task.operation FROM tasks AS task JOIN requests AS request ON "
                    "request.generation_id = task.generation_id AND request.request_key = task.request_key "
                    "WHERE task.generation_id = ? AND task.task_key = ?",
                    (generation_id, task_key),
                ).fetchone()
                if request_identity is None:
                    raise ValueError("dominated task requires a persisted exact request")
                lower_request_identity = json.loads(lower_observation["identity_json"])
                if (
                    evidence.dominated_observation_key != request_identity["request_key"]
                    or lower_observation["provider"] != request_identity["provider"]
                    or lower_request_identity["provider"] != request_identity["provider"]
                    or lower_request_identity["operation"] != request_identity["operation"]
                    or connection.execute(
                        "SELECT 1 FROM request_consumers WHERE generation_id = ? AND request_key = ? AND task_key = ?",
                        (generation_id, evidence.dominated_observation_key, task_key),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("dominated observation must be the terminalized logical task request")
                requested_fields = tuple(json.loads(request_identity["identity_json"])["requested_fields"])
                if tuple(sorted(evidence.covered_fields)) != tuple(sorted(requested_fields)):
                    raise ValueError("dominance covered fields must exactly match dominated requested fields")
                if not _merge_proves_dominance(
                    lower_observation["provider"],
                    json.loads(lower_observation["response_json"]),
                    [(item["provider"], json.loads(item["response_json"])) for item in terminal_observations],
                    evidence.covered_fields,
                    evidence.rule,
                ):
                    raise ValueError("live merge policy does not prove dominance for every covered field")
                connection.execute(
                    "INSERT INTO dominance_evidence(generation_id, task_key, stronger_observations_json, "
                    "dominated_observation_key, rule, covered_fields_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        task_key,
                        _canonical(sorted(evidence.stronger_observation_keys)),
                        evidence.dominated_observation_key,
                        evidence.rule.value,
                        _canonical(sorted(evidence.covered_fields)),
                    ),
                )
            if (
                current is not None
                and disposition not in {TaskDisposition.NOT_APPLICABLE, TaskDisposition.DOMINATED}
                and current[1] != disposition.value
            ):
                raise ValueError("task disposition does not match durable request disposition")
            connection.execute(
                "UPDATE tasks SET state = ?, reason = ?, applicability_reason = ?, next_attempt_at = ?, "
                "lease_owner = NULL, "
                "lease_expires_at = NULL, last_error_class = ?, safe_diagnostic = ? "
                "WHERE generation_id = ? AND task_key = ?",
                (
                    disposition.value,
                    safe_reason,
                    applicability_reason,
                    _timestamp(retry_at) if retry_at else None,
                    disposition.value if disposition not in _SATISFIED else None,
                    safe_reason,
                    generation_id,
                    task_key,
                ),
            )
        if disposition in _TERMINAL:
            self._inject("after_task_terminalization")

    def all_required_satisfied(self) -> bool:
        generation_id = self._generation_id()
        return self._all_required_satisfied(self._connection, generation_id)

    @staticmethod
    def _all_required_satisfied(connection: sqlite3.Connection, generation_id: str) -> bool:
        Ledger._verify_plan_integrity(connection, generation_id)
        sealed = connection.execute(
            "SELECT plan_closed, plan_authority_mode FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if sealed is None or not sealed[0]:
            return False
        if sealed[1] == "phased_structural":
            verifier = Ledger.__new__(Ledger)
            verifier._connection = connection
            Ledger._verify_structural_closure(verifier, connection, generation_id)
        missing_obligations = connection.execute(
            "SELECT COUNT(*) FROM plan_obligations AS obligation LEFT JOIN tasks AS task "
            "ON task.generation_id = obligation.generation_id AND task.task_key = obligation.task_key "
            "WHERE obligation.generation_id = ? AND (task.task_key IS NULL OR task.identity_digest != "
            "obligation.identity_digest)",
            (generation_id,),
        ).fetchone()[0]
        if int(missing_obligations) != 0:
            return False
        placeholders = ",".join("?" for _ in _SATISFIED)
        count = connection.execute(
            f"SELECT COUNT(*) FROM plan_obligations AS obligation JOIN tasks AS task "  # noqa: S608
            f"ON task.generation_id = obligation.generation_id AND task.task_key = obligation.task_key "
            f"WHERE obligation.generation_id = ? AND obligation.required = 1 AND task.state NOT IN ({placeholders})",
            (generation_id, *(state.value for state in sorted(_SATISFIED, key=lambda item: item.value))),
        ).fetchone()[0]
        return int(count) == 0

    def record_checkpoint(self, sequence: int, ciphertext_digest: str, key_id: str, created_at: datetime) -> None:
        if sequence < 1:
            raise ValueError("checkpoint sequence must be positive")
        _digest_text(ciphertext_digest, "ciphertext digest")
        key_id = _identifier(key_id, "checkpoint key identifier")
        with self._transaction(immediate=True) as connection:
            generation_id = self._generation_id()
            self._verify_plan_integrity(connection, generation_id)
            connection.execute(
                "INSERT INTO checkpoints(generation_id, sequence, ciphertext_digest, key_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (generation_id, sequence, ciphertext_digest, key_id, _timestamp(created_at)),
            )
            connection.execute(
                "UPDATE generations SET checkpoint_sequence = ?, updated_at = ? WHERE generation_id = ? "
                "AND checkpoint_sequence < ?",
                (sequence, _timestamp(created_at), generation_id, sequence),
            )

    def record_publication(
        self,
        kind: str,
        commit_sha: str,
        created_at: datetime,
        *,
        candidate_digest: str,
        manifest_digest: str,
    ) -> None:
        kind = _identifier(kind, "publication evidence kind")
        if not re.fullmatch(r"[0-9a-f]{7,64}", commit_sha):
            raise ValueError("invalid publication commit SHA")
        _digest_text(candidate_digest, "candidate digest")
        _digest_text(manifest_digest, "publication manifest digest")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO publication_evidence(generation_id, kind, commit_sha, candidate_digest, "
                "manifest_digest, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._generation_id(),
                    kind,
                    commit_sha,
                    candidate_digest,
                    manifest_digest,
                    _timestamp(created_at),
                ),
            )

    def record_validation(
        self, check_name: str, state: EvidenceState, evidence_digest: str, safe_detail: str = ""
    ) -> None:
        check_name = _identifier(check_name, "validation check name")
        if not isinstance(state, EvidenceState) or state not in {
            EvidenceState.PENDING,
            EvidenceState.FAILED,
            EvidenceState.SUCCEEDED,
        }:
            raise ValueError("invalid validation state")
        _digest_text(evidence_digest, "validation evidence digest")
        detail = _free_text(safe_detail, "validation detail")
        with self._transaction(immediate=True) as connection:
            generation_id = self._generation_id()
            declared = connection.execute(
                "SELECT required FROM validation_obligations WHERE generation_id = ? AND check_name = ?",
                (generation_id, check_name),
            ).fetchone()
            if declared is None:
                raise ValueError("validation check is not a sealed obligation")
            connection.execute(
                "INSERT INTO validations(generation_id, check_name, state, evidence_digest, safe_detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (generation_id, check_name, state.value, evidence_digest, detail),
            )

    def current_manifest_binding(self) -> str:
        return self.manifest().digest

    def record_materialization(self, evidence: MaterializationEvidence) -> None:
        staged_path = _free_text(evidence.staged_path, "staged path", required=True)
        _digest_text(evidence.manifest_digest, "materialization manifest digest")
        if evidence.validation_state is not EvidenceState.VALIDATED:
            raise ValueError("invalid materialization validation state")
        validation_state = evidence.validation_state.value
        counts = dict(evidence.corpus_counts)
        if any(not isinstance(value, int) or value < 0 for value in counts.values()):
            raise ValueError("invalid corpus counts")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO materializations(generation_id, staged_path, manifest_digest, corpus_counts_json, "
                "validation_state) VALUES (?, ?, ?, ?, ?)",
                (self._generation_id(), staged_path, evidence.manifest_digest, _canonical(counts), validation_state),
            )

    def record_publication_metadata(self, metadata: PublicationMetadata) -> None:
        identifiers = dict(metadata.exact_identifiers)
        if _contains_secret(identifiers):
            raise ValueError("secret material cannot be persisted in publication identifiers")
        _freeze_json(identifiers)
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO publications(generation_id, author_key, publication_key, discovery_source, "
                "normalized_title, year, exact_identifiers_json, baseline_output_path, freshness_policy) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(generation_id, author_key, publication_key) "
                "DO UPDATE SET discovery_source = excluded.discovery_source, "
                "normalized_title = excluded.normalized_title, "
                "year = excluded.year, exact_identifiers_json = excluded.exact_identifiers_json, "
                "baseline_output_path = excluded.baseline_output_path, freshness_policy = excluded.freshness_policy",
                (
                    self._generation_id(),
                    _identifier(metadata.author_key, "author key"),
                    _identifier(metadata.publication_key, "publication key"),
                    _provider(metadata.discovery_source),
                    _free_text(metadata.normalized_title, "normalized title", required=True),
                    metadata.year,
                    _canonical(identifiers),
                    _free_text(metadata.baseline_output_path, "baseline output path"),
                    _identifier(metadata.freshness_policy, "freshness policy"),
                ),
            )

    def record_provider_state(
        self,
        provider: str,
        quota_pool: str,
        current_concurrency: int,
        circuit_state: str,
        success_count: int,
        failure_count: int,
    ) -> None:
        if min(current_concurrency, success_count, failure_count) < 0:
            raise ValueError("provider state counts cannot be negative")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO provider_state(generation_id, provider, quota_pool, current_concurrency, "
                "circuit_state, success_count, failure_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._generation_id(),
                    _provider(provider),
                    _identifier(quota_pool, "quota pool"),
                    current_concurrency,
                    _identifier(circuit_state, "circuit state"),
                    success_count,
                    failure_count,
                ),
            )

    def record_field_provenance(
        self,
        author_key: str,
        publication_key: str,
        field_name: str,
        selected_value_digest: str,
        provider: str,
        request_key: str,
        decision_rule: ProvenanceRule,
    ) -> None:
        if not _FIELD_RE.fullmatch(field_name):
            raise ValueError("invalid provenance field")
        _digest_text(selected_value_digest, "selected value digest")
        _digest_text(request_key, "request key")
        if not isinstance(decision_rule, ProvenanceRule):
            raise ValueError("invalid field provenance decision rule")
        with self._transaction(immediate=True) as connection:
            generation_id = self._generation_id()
            grounded = connection.execute(
                "SELECT observation.provider FROM observations AS observation JOIN tasks AS task ON "
                "task.generation_id = observation.generation_id AND task.request_key = observation.request_key "
                "JOIN publications AS publication ON publication.generation_id = task.generation_id AND "
                "publication.author_key = task.author_key AND publication.publication_key = task.publication_key "
                "WHERE observation.generation_id = ? AND observation.request_key = ? AND observation.disposition = ? "
                "AND publication.author_key = ? AND publication.publication_key = ?",
                (
                    generation_id,
                    request_key,
                    TaskDisposition.SUCCEEDED.value,
                    author_key,
                    publication_key,
                ),
            ).fetchone()
            if grounded is None:
                raise ValueError("field provenance requires a persisted succeeded observation and matching task")
            validated_provider = _provider(provider)
            if grounded[0] != validated_provider:
                raise ValueError("field provenance provider does not match succeeded observation")
            connection.execute(
                "INSERT INTO field_provenance(generation_id, author_key, publication_key, field_name, "
                "selected_value_digest, provider, request_key, decision_rule) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    _identifier(author_key, "author key"),
                    _identifier(publication_key, "publication key"),
                    field_name,
                    selected_value_digest,
                    validated_provider,
                    request_key,
                    decision_rule.value,
                ),
            )

    def manifest(self) -> LedgerManifest:
        with self._transaction(immediate=True) as connection:
            generation_id = self._generation_id()
            self._verify_plan_integrity(connection, generation_id)
            closure = connection.execute(
                "SELECT plan_closed, plan_authority_mode FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if closure[0] and closure[1] == "phased_structural":
                self._verify_structural_closure(connection, generation_id)
            generation = connection.execute(
                "SELECT generation_id, identity_json, census_digest, authors_digest, base_commit, input_digest, "
                "policy_digest, adapter_digest, state, created_at, updated_at, completed_at, published_at, "
                "checkpoint_sequence, blocking_reason, plan_sealed, plan_digest, completed_manifest_digest, "
                "inventory_freshness_epoch, plan_closed, plan_revision, closure_digest, plan_authority_mode "
                "FROM generations "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if self._manifest_probe is not None:
                probe = self._manifest_probe
                probe()
                self._manifest_probe = None
            data = self._manifest_data(connection, generation_id, generation)
            canonical_json = _canonical(data)
            digest = hashlib.sha256(canonical_json.encode()).hexdigest()
            connection.execute(
                "INSERT OR IGNORE INTO manifests(generation_id, digest, canonical_json) VALUES (?, ?, ?)",
                (generation_id, digest, canonical_json),
            )
        self._inject("after_manifest_commit")
        return LedgerManifest(data, canonical_json, digest)

    @staticmethod
    def _manifest_data(
        connection: sqlite3.Connection, generation_id: str, generation: sqlite3.Row
    ) -> dict[str, object]:
        census = [
            dict(row)
            for row in connection.execute(
                "SELECT row_key, physical_row, name, normalized_name, scholar_id, dblp_id, enabled, exclusion_reason, "
                "disposition FROM authors WHERE generation_id = ? ORDER BY row_key",
                (generation_id,),
            )
        ]
        requests = []
        for row in connection.execute(
            "SELECT request_key, identity_json, state, next_attempt_at, response_digest, safe_diagnostic "
            "FROM requests WHERE generation_id = ? ORDER BY request_key",
            (generation_id,),
        ):
            item = dict(row)
            item["identity"] = json.loads(item.pop("identity_json"))
            item["consumers"] = [
                consumer[0]
                for consumer in connection.execute(
                    "SELECT task_key FROM request_consumers WHERE generation_id = ? AND request_key = ? "
                    "ORDER BY task_key",
                    (generation_id, row["request_key"]),
                )
            ]
            requests.append(item)
        data: dict[str, object] = {
            "attempts": [
                {
                    **{key: value for key, value in dict(row).items() if key != "attempt_number"},
                    "number": row["attempt_number"],
                }
                for row in connection.execute(
                    "SELECT request_key, attempt_number, started_at, finished_at, outcome, http_status, "
                    "retry_delay_seconds, response_digest, safe_diagnostic FROM attempts WHERE generation_id = ? "
                    "ORDER BY request_key, attempt_number",
                    (generation_id,),
                )
            ],
            "census": census,
            "checkpoints": [
                dict(row)
                for row in connection.execute(
                    "SELECT sequence, ciphertext_digest, key_id, created_at FROM checkpoints WHERE generation_id = ? "
                    "ORDER BY sequence",
                    (generation_id,),
                )
            ],
            "field_provenance": [
                dict(row)
                for row in connection.execute(
                    "SELECT author_key, publication_key, field_name, selected_value_digest, provider, request_key, "
                    "decision_rule FROM field_provenance WHERE generation_id = ? "
                    "ORDER BY author_key, publication_key, field_name",
                    (generation_id,),
                )
            ],
            "dominance_evidence": [
                {
                    "task_key": row["task_key"],
                    "stronger_observation_keys": json.loads(row["stronger_observations_json"]),
                    "dominated_observation_key": row["dominated_observation_key"],
                    "rule": row["rule"],
                    "covered_fields": json.loads(row["covered_fields_json"]),
                }
                for row in connection.execute(
                    "SELECT task_key, stronger_observations_json, dominated_observation_key, rule, "
                    "covered_fields_json FROM dominance_evidence WHERE generation_id = ? ORDER BY task_key",
                    (generation_id,),
                )
            ],
            "generation": {
                **{key: value for key, value in dict(generation).items() if key != "identity_json"},
                "identity": json.loads(generation["identity_json"]),
            },
            "publications": [
                dict(row)
                for row in connection.execute(
                    "SELECT author_key, publication_key, discovery_source, normalized_title, year, "
                    "exact_identifiers_json, baseline_output_path, freshness_policy FROM publications "
                    "WHERE generation_id = ? "
                    "ORDER BY author_key, publication_key",
                    (generation_id,),
                )
            ],
            "publication_evidence": [
                dict(row)
                for row in connection.execute(
                    "SELECT kind, commit_sha, candidate_digest, manifest_digest, created_at "
                    "FROM publication_evidence WHERE generation_id = ? "
                    "ORDER BY kind, commit_sha",
                    (generation_id,),
                )
            ],
            "materializations": [
                dict(row)
                for row in connection.execute(
                    "SELECT staged_path, manifest_digest, corpus_counts_json, validation_state FROM materializations "
                    "WHERE generation_id = ? ORDER BY staged_path",
                    (generation_id,),
                )
            ],
            "observations": [
                dict(row)
                for row in connection.execute(
                    "SELECT request_key, disposition, response_digest, provider, schema_version, observed_at, "
                    "authoritative_empty, safe_diagnostic FROM observations WHERE generation_id = ? "
                    "ORDER BY request_key",
                    (generation_id,),
                )
            ],
            "plan_obligations": [
                dict(row)
                for row in connection.execute(
                    "SELECT task_key, identity_digest, author_key, provider, operation, required, applicability, "
                    "round_sequence, expands_plan "
                    "FROM plan_obligations WHERE generation_id = ? ORDER BY task_key",
                    (generation_id,),
                )
            ],
            "plan_rounds": [
                {
                    **{key: value for key, value in dict(row).items() if not key.endswith("_json")},
                    "source_task_keys": json.loads(row["source_task_keys_json"]),
                }
                for row in connection.execute(
                    "SELECT sequence, round_key, phase, planner_id, planner_version, source_task_keys_json, "
                    "source_evidence_digest, task_set_digest, content_digest, committed_at FROM plan_rounds "
                    "WHERE generation_id = ? ORDER BY sequence",
                    (generation_id,),
                )
            ],
            "reduction_receipts": [
                {
                    "reduction_digest": row["reduction_digest"],
                    "round_key": row["round_key"],
                    "source_task_keys": json.loads(row["source_task_keys_json"]),
                    "source_dispositions": json.loads(row["source_dispositions_json"]),
                    "source_evidence_digests": json.loads(row["source_evidence_digests_json"]),
                    "committed_at": row["committed_at"],
                }
                for row in connection.execute(
                    "SELECT * FROM reduction_receipts WHERE generation_id = ? ORDER BY round_key",
                    (generation_id,),
                )
            ],
            "provider_state": [
                dict(row)
                for row in connection.execute(
                    "SELECT provider, quota_pool, current_concurrency, rate_limit_deadline, circuit_state, "
                    "success_count, failure_count, async_job_id, request_digest FROM provider_state "
                    "WHERE generation_id = ? ORDER BY provider, quota_pool",
                    (generation_id,),
                )
            ],
            "requests": requests,
            "tasks": [
                dict(row)
                for row in connection.execute(
                    "SELECT task_key, identity_digest, author_key, publication_key, provider, operation, request_key, "
                    "required, applicability, applicability_reason, dominance_reason, state, reason, attempt_count, "
                    "last_error_class, safe_diagnostic, next_attempt_at, lease_owner, lease_expires_at "
                    "FROM tasks WHERE generation_id = ? "
                    "ORDER BY task_key",
                    (generation_id,),
                )
            ],
            "validations": [
                dict(row)
                for row in connection.execute(
                    "SELECT check_name, state, evidence_digest, safe_detail FROM validations WHERE generation_id = ? "
                    "ORDER BY check_name",
                    (generation_id,),
                )
            ],
            "validation_obligations": [
                dict(row)
                for row in connection.execute(
                    "SELECT check_name, required FROM validation_obligations WHERE generation_id = ? "
                    "ORDER BY check_name",
                    (generation_id,),
                )
            ],
        }
        return data

    def _set_manifest_probe_for_test(self, probe: Callable[[], None]) -> None:
        if not callable(probe):
            raise TypeError("manifest probe must be callable")
        self._manifest_probe = probe

    def set_fault(self, name: str) -> None:
        if name not in _FAULT_POINTS:
            raise ValueError(f"unknown fault point: {name}")
        self._fault = name

    def _inject(self, name: str) -> None:
        if self._fault == name:
            self._fault = None
            raise FaultInjectedError(name)

    def pragma(self, name: str) -> object:
        if name not in {"journal_mode", "synchronous", "foreign_keys", "integrity_check"}:
            raise ValueError("unsupported pragma")
        return self._connection.execute(f"PRAGMA {name}").fetchone()[0]


__all__ = [
    "ApplicabilityReason",
    "DominanceEvidence",
    "DominanceRule",
    "EvidenceState",
    "FaultInjectedError",
    "Ledger",
    "LedgerManifest",
    "MaterializationEvidence",
    "PlanRound",
    "PlanStatus",
    "PlannedTask",
    "ProvenanceRule",
    "ProviderObservation",
    "PublicationMetadata",
    "ReductionReceipt",
    "RequestClaim",
    "RequestResult",
    "RequestSpec",
    "TaskClaim",
    "TaskSpec",
    "ValidationSpec",
    "inventory_tasks",
]
