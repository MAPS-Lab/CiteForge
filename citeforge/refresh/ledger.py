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

from .census import AuthorCensus
from .types import GenerationSpec, GenerationState, TaskDisposition

_SCHEMA_VERSION = "2"
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


def _canonical(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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


class ApplicabilityReason(str, Enum):
    NO_APPLICABLE_IDENTIFIER = "no_applicable_identifier"
    PROVIDER_NOT_SUPPORTED = "provider_not_supported"


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
    rule: str
    covered_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.stronger_observation_keys or not self.covered_fields:
            raise ValueError("dominance evidence requires observations and covered fields")
        for key in self.stronger_observation_keys:
            _digest_text(key, "observation key")
        _identifier(self.rule, "dominance rule")
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
                    completed_manifest_digest TEXT
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
                    PRIMARY KEY (generation_id, task_key),
                    UNIQUE (generation_id, identity_digest),
                    FOREIGN KEY (generation_id, task_key) REFERENCES tasks(generation_id, task_key)
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
                    rule TEXT NOT NULL,
                    covered_fields_json TEXT NOT NULL,
                    PRIMARY KEY (generation_id, task_key),
                    FOREIGN KEY (generation_id, task_key) REFERENCES tasks(generation_id, task_key)
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
                for operation in ("UPDATE", "DELETE", "INSERT"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_sealed_{operation.lower()} BEFORE {operation} "  # noqa: S608
                        f"ON {table} WHEN (SELECT plan_sealed FROM generations WHERE generation_id = "
                        f"{'OLD' if operation != 'INSERT' else 'NEW'}.generation_id) = 1 "
                        "BEGIN SELECT RAISE(ABORT, 'sealed obligations are immutable'); END"
                    )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS attempts_no_delete BEFORE DELETE ON attempts "
                "BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END"
            )
            version = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
            if version is None:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)", (_SCHEMA_VERSION,)
                )
            elif version[0] != _SCHEMA_VERSION:
                raise ValueError(f"unsupported ledger schema version: {version[0]}")

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
            if new is GenerationState.VALIDATING and not self._all_required_satisfied(connection, generation_id):
                raise ValueError("generation cannot validate before sealed complete plan")
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
            census_rows = connection.execute(
                "SELECT row_key, scholar_id, dblp_id FROM authors WHERE generation_id = ? AND enabled = 1",
                (generation_id,),
            )
            mandatory_inventory = {
                (row["row_key"], provider)
                for row in census_rows
                for provider, provider_id in (("scholar", row["scholar_id"]), ("dblp", row["dblp_id"]))
                if provider_id
            }
            declared_inventory = {
                (task.author_key, task.provider)
                for task in expected_tasks
                if task.required and task.operation == "inventory"
            }
            if declared_inventory != mandatory_inventory:
                raise ValueError("inventory obligations do not exactly match enabled census sources")
            connection.executemany(
                "INSERT INTO plan_obligations(generation_id, task_key, identity_digest, author_key, provider, "
                "operation, required, applicability) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
            plan_content = self._plan_content(connection, generation_id)
            connection.execute(
                "UPDATE generations SET plan_sealed = 1, plan_digest = ?, updated_at = ? WHERE generation_id = ?",
                (_digest(plan_content), _timestamp(datetime.now(timezone.utc)), generation_id),
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
        return {"tasks": tasks, "validations": validations}

    @classmethod
    def _verify_plan_integrity(cls, connection: sqlite3.Connection, generation_id: str) -> None:
        generation = connection.execute(
            "SELECT plan_sealed, plan_digest FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if generation is None or not generation[0]:
            return
        if generation[1] != _digest(cls._plan_content(connection, generation_id)):
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
                "SELECT state, plan_sealed FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value or not generation[1]:
                return None
            candidate = connection.execute(
                "SELECT task_key FROM tasks WHERE generation_id = ? AND request_key IS NOT NULL AND "
                "((state IN (?, ?) AND "
                "(next_attempt_at IS NULL OR next_attempt_at <= ?)) OR (state = ? AND lease_expires_at <= ?)) "
                "ORDER BY task_key LIMIT 1",
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
                "SELECT state, plan_sealed FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value or not generation[1]:
                raise ValueError("request claims require a sealed running generation")
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
                request_identity = connection.execute(
                    "SELECT identity_json FROM requests WHERE generation_id = ? AND request_key = ?",
                    (generation_id, request_key),
                ).fetchone()
                if request_identity is None:
                    raise ValueError("request identity missing")
                identity = json.loads(request_identity[0])
                if observation is not None and observation.provider != identity["provider"]:
                    raise ValueError("observation provider does not match request")
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
            "SELECT disposition, response_json, response_digest FROM observations WHERE generation_id = ? "
            "AND request_key = ?",
            (self._generation_id(), request_key),
        ).fetchone()
        if row is None:
            return None
        response = MappingProxyType(json.loads(row[1])) if row[1] is not None else None
        return RequestResult(request_key, TaskDisposition(row[0]), response, row[2])

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
                        "SELECT request_key FROM observations WHERE generation_id = ? AND request_key = ?",
                        (generation_id, observation_key),
                    ).fetchone()
                    for observation_key in evidence.stronger_observation_keys
                ]
                terminal_observations = [item for item in terminal_observations if item is not None]
                if len(terminal_observations) != len(evidence.stronger_observation_keys):
                    raise ValueError("dominance evidence must reference persisted terminal observations")
                connection.execute(
                    "INSERT INTO dominance_evidence(generation_id, task_key, stronger_observations_json, rule, "
                    "covered_fields_json) VALUES (?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        task_key,
                        _canonical(sorted(evidence.stronger_observation_keys)),
                        evidence.rule,
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
            "SELECT plan_sealed FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if sealed is None or not sealed[0]:
            return False
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
        decision_rule: str,
    ) -> None:
        if not _FIELD_RE.fullmatch(field_name):
            raise ValueError("invalid provenance field")
        _digest_text(selected_value_digest, "selected value digest")
        _digest_text(request_key, "request key")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO field_provenance(generation_id, author_key, publication_key, field_name, "
                "selected_value_digest, provider, request_key, decision_rule) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._generation_id(),
                    _identifier(author_key, "author key"),
                    _identifier(publication_key, "publication key"),
                    field_name,
                    selected_value_digest,
                    _provider(provider),
                    request_key,
                    _identifier(decision_rule, "decision rule"),
                ),
            )

    def manifest(self) -> LedgerManifest:
        with self._transaction(immediate=True) as connection:
            generation_id = self._generation_id()
            self._verify_plan_integrity(connection, generation_id)
            generation = connection.execute(
                "SELECT generation_id, identity_json, census_digest, authors_digest, base_commit, input_digest, "
                "policy_digest, adapter_digest, state, created_at, updated_at, completed_at, published_at, "
                "checkpoint_sequence, blocking_reason, plan_sealed, plan_digest FROM generations "
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
                    "SELECT task_key, identity_digest, author_key, provider, operation, required, applicability "
                    "FROM plan_obligations WHERE generation_id = ? ORDER BY task_key",
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
    "EvidenceState",
    "FaultInjectedError",
    "Ledger",
    "LedgerManifest",
    "MaterializationEvidence",
    "ProviderObservation",
    "PublicationMetadata",
    "RequestClaim",
    "RequestResult",
    "RequestSpec",
    "TaskClaim",
    "TaskSpec",
    "ValidationSpec",
    "inventory_tasks",
]
