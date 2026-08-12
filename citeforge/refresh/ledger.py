"""Transactional SQLite authority for resumable refresh generations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import cast
from urllib.parse import parse_qsl, urlsplit

from .census import AuthorCensus
from .types import GenerationSpec, GenerationState, TaskDisposition

_SCHEMA_VERSION = "1"
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


class FaultInjectedError(RuntimeError):
    """Test-only interruption raised after a named durable boundary."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return True
        parsed = urlsplit(value)
        if parsed.query and any(_SECRET_KEY.search(query_key) for query_key, _ in parse_qsl(parsed.query)):
            return True
    return False


def _safe_text(value: str) -> str:
    if _contains_secret(value):
        raise ValueError("secret material cannot be persisted")
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


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
        canonical = {
            "adapter_version": self.adapter_version,
            "freshness_epoch": self.freshness_epoch,
            "method": self.method.upper(),
            "normalized_payload": payload,
            "operation": self.operation,
            "provider": self.provider,
            "quota_scope": self.quota_scope,
            "requested_fields": fields,
        }
        object.__setattr__(self, "method", self.method.upper())
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

    key: str
    author_key: str
    publication_key: str | None
    provider: str
    operation: str
    request: RequestSpec
    required: bool = True

    def __post_init__(self) -> None:
        if not self.key or not self.author_key:
            raise ValueError("task key and author key must be non-empty")
        if self.provider != self.request.provider or self.operation != self.request.operation:
            raise ValueError("task and request provider operation must match")


@dataclass(frozen=True)
class TaskClaim:
    key: str
    request_key: str
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
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL
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
                    request_key TEXT NOT NULL,
                    required INTEGER NOT NULL CHECK(required IN (0, 1)),
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    next_attempt_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    PRIMARY KEY (generation_id, task_key),
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
                    PRIMARY KEY (generation_id, author_key, publication_key),
                    FOREIGN KEY (generation_id, author_key) REFERENCES authors(generation_id, row_key)
                );
                CREATE TABLE IF NOT EXISTS publication_evidence (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    kind TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, kind, commit_sha)
                );
                CREATE TABLE IF NOT EXISTS manifests (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    digest TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    PRIMARY KEY (generation_id, digest)
                );
                """
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS attempts_no_update BEFORE UPDATE ON attempts "
                "BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END"
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
        with self._transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT generation_id, identity_json, census_digest FROM generations"
            ).fetchall()
            if existing:
                row = existing[0]
                if len(existing) != 1 or row[0] != spec.id or row[1] != identity_json:
                    raise ValueError("generation identity mismatch")
                if row[2] != census_digest:
                    raise ValueError("census mismatch")
                return
            connection.execute(
                "INSERT INTO generations(generation_id, identity_json, census_digest, state, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    spec.id,
                    identity_json,
                    census_digest,
                    GenerationState.PLANNING.value,
                    _timestamp(datetime.now(timezone.utc)),
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

    def transition_generation(self, expected: GenerationState, new: GenerationState) -> None:
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE generations SET state = ? WHERE generation_id = ? AND state = ?",
                (new.value, self._generation_id(), expected.value),
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
        request_identity = _canonical(task.request.canonical_content())
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO requests(generation_id, request_key, identity_json, state) VALUES (?, ?, ?, ?)",
                (generation_id, task.request.key, request_identity, TaskDisposition.PENDING.value),
            )
            stored_request = connection.execute(
                "SELECT identity_json FROM requests WHERE generation_id = ? AND request_key = ?",
                (generation_id, task.request.key),
            ).fetchone()
            if stored_request is None or stored_request[0] != request_identity:
                raise ValueError("exact request identity collision")
            existing = connection.execute(
                "SELECT author_key, publication_key, provider, operation, request_key, required FROM tasks "
                "WHERE generation_id = ? AND task_key = ?",
                (generation_id, task.key),
            ).fetchone()
            expected = (
                task.author_key,
                task.publication_key,
                task.provider,
                task.operation,
                task.request.key,
                int(task.required),
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
                    "request_key, required, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (generation_id, task.key, *expected, TaskDisposition.PENDING.value),
                )
                connection.execute(
                    "INSERT INTO request_consumers(generation_id, request_key, task_key) VALUES (?, ?, ?)",
                    (generation_id, task.request.key, task.key),
                )
        return TaskClaim(task.key, task.request.key, "", datetime.min.replace(tzinfo=timezone.utc))

    def claim_due(self, owner: str, now: datetime, lease_for: timedelta) -> TaskClaim | None:
        if not owner or lease_for <= timedelta(0):
            raise ValueError("claim owner and positive lease are required")
        generation_id = self._generation_id()
        now_text = _timestamp(now)
        expires = now + lease_for
        with self._transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT task_key FROM tasks WHERE generation_id = ? AND ((state IN (?, ?) AND "
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
        generation_id = self._generation_id()
        now_text = _timestamp(now)
        expires = now + lease_for
        with self._transaction(immediate=True) as connection:
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
        diagnostic = _safe_text(safe_diagnostic)
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
        normalized_response: Mapping[str, object] | None = None,
        safe_diagnostic: str = "",
    ) -> None:
        if disposition not in _TERMINAL | {TaskDisposition.RETRY_WAIT}:
            raise ValueError("invalid request finish disposition")
        if disposition is TaskDisposition.RETRY_WAIT and retry_at is None:
            raise ValueError("retry wait requires a durable retry deadline")
        diagnostic = _safe_text(safe_diagnostic)
        if (
            disposition
            in {
                TaskDisposition.CONFIRMED_EMPTY,
                TaskDisposition.NOT_APPLICABLE,
                TaskDisposition.DOMINATED,
            }
            and not diagnostic
        ):
            raise ValueError("request disposition requires validation evidence")
        if disposition is TaskDisposition.SUCCEEDED and normalized_response is None:
            raise ValueError("successful request requires a normalized response")
        response_json: str | None = None
        if normalized_response is not None:
            if _contains_secret(normalized_response):
                raise ValueError("secret material cannot be persisted in a response")
            response_json = _canonical(dict(normalized_response))
            calculated_digest = hashlib.sha256(response_json.encode()).hexdigest()
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
                connection.execute(
                    "INSERT INTO observations(generation_id, request_key, disposition, response_json, response_digest, "
                    "observed_at, safe_diagnostic) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        request_key,
                        disposition.value,
                        response_json,
                        response_digest,
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
        reason: str = "",
    ) -> None:
        generation_id = self._generation_id()
        current = self._connection.execute(
            "SELECT task.state, request.state FROM tasks AS task JOIN requests AS request "
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
        if (
            disposition
            in {
                TaskDisposition.CONFIRMED_EMPTY,
                TaskDisposition.NOT_APPLICABLE,
                TaskDisposition.DOMINATED,
            }
            and not reason
        ):
            raise ValueError("terminal disposition requires proof reason")
        safe_reason = _safe_text(reason)
        with self._transaction(immediate=True) as connection:
            self._assert_owner("tasks", "task_key", task_key, owner, now)
            if (
                current is not None
                and disposition not in {TaskDisposition.NOT_APPLICABLE, TaskDisposition.DOMINATED}
                and current[1] != disposition.value
            ):
                raise ValueError("task disposition does not match durable request disposition")
            connection.execute(
                "UPDATE tasks SET state = ?, reason = ?, next_attempt_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL WHERE generation_id = ? AND task_key = ?",
                (
                    disposition.value,
                    safe_reason,
                    _timestamp(retry_at) if retry_at else None,
                    generation_id,
                    task_key,
                ),
            )
        if disposition in _TERMINAL:
            self._inject("after_task_terminalization")

    def all_required_satisfied(self) -> bool:
        generation_id = self._generation_id()
        missing_author_work = self._connection.execute(
            "SELECT COUNT(*) FROM authors AS author WHERE author.generation_id = ? AND author.enabled = 1 "
            "AND NOT EXISTS (SELECT 1 FROM tasks AS task WHERE task.generation_id = author.generation_id "
            "AND task.author_key = author.row_key AND task.required = 1)",
            (generation_id,),
        ).fetchone()[0]
        if int(missing_author_work) != 0:
            return False
        placeholders = ",".join("?" for _ in _SATISFIED)
        count = self._connection.execute(
            f"SELECT COUNT(*) FROM tasks WHERE generation_id = ? AND required = 1 AND state NOT IN ({placeholders})",  # noqa: S608
            (generation_id, *(state.value for state in sorted(_SATISFIED, key=lambda item: item.value))),
        ).fetchone()[0]
        return int(count) == 0

    def record_checkpoint(self, sequence: int, ciphertext_digest: str, key_id: str, created_at: datetime) -> None:
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO checkpoints(generation_id, sequence, ciphertext_digest, key_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._generation_id(), sequence, ciphertext_digest, _safe_text(key_id), _timestamp(created_at)),
            )

    def record_publication(self, kind: str, commit_sha: str, created_at: datetime) -> None:
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO publication_evidence(generation_id, kind, commit_sha, created_at) VALUES (?, ?, ?, ?)",
                (self._generation_id(), kind, commit_sha, _timestamp(created_at)),
            )

    def manifest(self) -> LedgerManifest:
        generation_id = self._generation_id()
        generation = self._connection.execute(
            "SELECT generation_id, identity_json, census_digest, state FROM generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        census = [
            dict(row)
            for row in self._connection.execute(
                "SELECT row_key, physical_row, name, normalized_name, scholar_id, dblp_id, enabled, exclusion_reason, "
                "disposition FROM authors WHERE generation_id = ? ORDER BY row_key",
                (generation_id,),
            )
        ]
        requests = []
        for row in self._connection.execute(
            "SELECT request_key, identity_json, state, next_attempt_at, response_digest, safe_diagnostic "
            "FROM requests WHERE generation_id = ? ORDER BY request_key",
            (generation_id,),
        ):
            item = dict(row)
            item["identity"] = json.loads(item.pop("identity_json"))
            item["consumers"] = [
                consumer[0]
                for consumer in self._connection.execute(
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
                for row in self._connection.execute(
                    "SELECT request_key, attempt_number, started_at, finished_at, outcome, http_status, "
                    "retry_delay_seconds, response_digest, safe_diagnostic FROM attempts WHERE generation_id = ? "
                    "ORDER BY request_key, attempt_number",
                    (generation_id,),
                )
            ],
            "census": census,
            "checkpoints": [
                dict(row)
                for row in self._connection.execute(
                    "SELECT sequence, ciphertext_digest, key_id, created_at FROM checkpoints WHERE generation_id = ? "
                    "ORDER BY sequence",
                    (generation_id,),
                )
            ],
            "generation": {
                "census_digest": generation["census_digest"],
                "generation_id": generation["generation_id"],
                "identity": json.loads(generation["identity_json"]),
                "state": generation["state"],
            },
            "publications": [
                dict(row)
                for row in self._connection.execute(
                    "SELECT author_key, publication_key FROM publications WHERE generation_id = ? "
                    "ORDER BY author_key, publication_key",
                    (generation_id,),
                )
            ],
            "publication_evidence": [
                dict(row)
                for row in self._connection.execute(
                    "SELECT kind, commit_sha, created_at FROM publication_evidence WHERE generation_id = ? "
                    "ORDER BY kind, commit_sha",
                    (generation_id,),
                )
            ],
            "requests": requests,
            "tasks": [
                dict(row)
                for row in self._connection.execute(
                    "SELECT task_key, author_key, publication_key, provider, operation, request_key, required, state, "
                    "reason, next_attempt_at FROM tasks WHERE generation_id = ? ORDER BY task_key",
                    (generation_id,),
                )
            ],
        }
        canonical_json = _canonical(data)
        digest = hashlib.sha256(canonical_json.encode()).hexdigest()
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO manifests(generation_id, digest, canonical_json) VALUES (?, ?, ?)",
                (generation_id, digest, canonical_json),
            )
        self._inject("after_manifest_commit")
        return LedgerManifest(data, canonical_json, digest)

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
    "FaultInjectedError",
    "Ledger",
    "LedgerManifest",
    "RequestClaim",
    "RequestResult",
    "RequestSpec",
    "TaskClaim",
    "TaskSpec",
]
