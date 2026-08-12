from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from citeforge.refresh.census import AuthorCensus, AuthorCensusRow
from citeforge.refresh.ledger import (
    FaultInjectedError,
    Ledger,
    RequestSpec,
    TaskSpec,
)
from citeforge.refresh.types import GenerationSpec, GenerationState, TaskDisposition

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def _census(*, enabled: bool = True, name: str = "Ada Lovelace") -> AuthorCensus:
    disposition = TaskDisposition.PENDING if enabled else TaskDisposition.NOT_APPLICABLE
    row = AuthorCensusRow(
        physical_row=2,
        row_key="author-ada",
        name=name,
        normalized_name=name.casefold(),
        scholar_id="Scholar123",
        dblp_id="",
        enabled=enabled,
        exclusion_reason="" if enabled else "Excluded for test",
        disposition=disposition,
    )
    return AuthorCensus((row,))


def _generation(census: AuthorCensus | None = None, *, commit: str = "abc123") -> GenerationSpec:
    census = census or _census()
    return GenerationSpec(census, "policy-v1", {"scholar": "1"}, commit)


def _request(*, query: str = "doi:10.1/example", freshness: str = "2026-08") -> RequestSpec:
    return RequestSpec(
        provider="crossref",
        operation="lookup",
        method="GET",
        normalized_payload={"query": query},
        requested_fields=("title", "year"),
        adapter_version="1",
        freshness_epoch=freshness,
        quota_scope="public",
    )


def _task(
    label: str = "1",
    *,
    request: RequestSpec | None = None,
    required: bool = True,
    author_key: str = "author-ada",
    publication_key: str | None = None,
    applicability: str = "applicable",
) -> TaskSpec:
    return TaskSpec(
        author_key=author_key,
        publication_key=publication_key or f"pub-{label}",
        provider="crossref",
        operation="lookup",
        request=request or _request(),
        required=required,
        applicability=applicability,
    )


def _open_ready(path: Path) -> Ledger:
    ledger = Ledger.open(path)
    census = _census()
    ledger.create_or_resume(_generation(census), census)
    return ledger


def _finish_request_and_task(ledger: Ledger, disposition: TaskDisposition) -> None:
    claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
    assert claim
    request_claim = ledger.claim_request(claim.key, "worker", NOW, timedelta(minutes=1))
    assert request_claim
    retry_at = NOW + timedelta(minutes=1) if disposition is TaskDisposition.RETRY_WAIT else None
    response = {"result": "ok"} if disposition is TaskDisposition.SUCCEEDED else None
    ledger.finish_request(
        request_claim.key,
        "worker",
        disposition,
        NOW,
        retry_at=retry_at,
        normalized_response=response,
        safe_diagnostic="classified outcome",
    )
    ledger.finish_task(
        claim.key,
        "worker",
        disposition,
        NOW,
        retry_at=retry_at,
        reason="proof",
    )


def test_schema_pragmas_and_generation_resume_are_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "refresh.sqlite3"
    census = _census()
    with Ledger.open(path) as ledger:
        ledger.create_or_resume(_generation(census), census)
        assert ledger.manifest().data["generation"]["state"] == "planning"
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        with pytest.raises(ValueError, match="state transition"):
            ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        assert ledger.pragma("journal_mode") == "delete"
        assert ledger.pragma("synchronous") == 2
        assert ledger.pragma("foreign_keys") == 1
        assert ledger.pragma("integrity_check") == "ok"
        ledger.create_or_resume(_generation(census), census)
        with pytest.raises(ValueError, match="generation"):
            ledger.create_or_resume(_generation(census, commit="different"), census)
        with pytest.raises(ValueError, match="census"):
            ledger.create_or_resume(_generation(census), _census(name="Grace Hopper"))

    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="schema version"):
        Ledger.open(path)


def test_retains_every_census_disposition_and_enforces_foreign_keys(tmp_path: Path) -> None:
    enabled = _census().rows[0]
    excluded = AuthorCensusRow(
        physical_row=3,
        row_key="author-excluded",
        name="Excluded",
        normalized_name="excluded",
        scholar_id="",
        dblp_id="",
        enabled=False,
        exclusion_reason="No profile",
        disposition=TaskDisposition.NOT_APPLICABLE,
    )
    census = AuthorCensus((enabled, excluded))
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        ledger.create_or_resume(_generation(census), census)
        assert [row["row_key"] for row in ledger.manifest().data["census"]] == ["author-ada", "author-excluded"]
        connection = sqlite3.connect(ledger.path)
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks(generation_id, task_key, author_key, provider, operation, request_key, required, "
                "state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_generation(census).id, "bad", "missing", "x", "x", "missing", 1, "pending"),
            )
        connection.close()


def test_task_and_exact_request_uniqueness_with_multiple_consumers(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        first = ledger.plan_task(_task("task-a"))
        second = ledger.plan_task(_task("task-b"))
        assert first.request_key == second.request_key
        assert ledger.plan_task(_task("task-a")) == first
        manifest = ledger.manifest().data
        assert len(manifest["requests"]) == 1
        assert manifest["requests"][0]["consumers"] == sorted([first.key, second.key])
        assert len({item["publication_key"] for item in manifest["tasks"]}) == 2


def test_exact_request_scope_changes_identity(tmp_path: Path) -> None:
    assert _request().key != _request(freshness="2026-09").key


def test_enabled_author_without_required_work_cannot_be_complete(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        assert not ledger.all_required_satisfied()


def test_one_claimant_across_independent_connections_and_expired_reclaim(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with _open_ready(path) as ledger:
        ledger.plan_task(_task())

    def claim(owner: str) -> str | None:
        with Ledger.open(path) as connection:
            result = connection.claim_due(owner, NOW, timedelta(minutes=5))
            return result.owner if result else None

    with ThreadPoolExecutor(max_workers=12) as executor:
        claimed = list(executor.map(claim, [f"worker-{index}" for index in range(12)]))
    winners = [owner for owner in claimed if owner]
    assert len(winners) == 1

    with Ledger.open(path) as ledger:
        assert ledger.claim_due("early", NOW + timedelta(minutes=4), timedelta(minutes=5)) is None
        reclaimed = ledger.claim_due("replacement", NOW + timedelta(minutes=6), timedelta(minutes=5))
        assert reclaimed is not None
        assert reclaimed.owner == "replacement"


def test_retry_deadline_and_stale_owner_are_enforced(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        ledger.plan_task(_task())
        claim = ledger.claim_due("first", NOW, timedelta(seconds=10))
        assert claim
        request_claim = ledger.claim_request(claim.key, "first", NOW, timedelta(seconds=10))
        assert request_claim
        ledger.finish_request(
            request_claim.key,
            "first",
            TaskDisposition.RETRY_WAIT,
            NOW,
            retry_at=NOW + timedelta(minutes=2),
            safe_diagnostic="timeout",
        )
        ledger.finish_task(
            claim.key,
            "first",
            TaskDisposition.RETRY_WAIT,
            NOW,
            retry_at=NOW + timedelta(minutes=2),
            reason="provider timeout",
        )
        assert ledger.claim_due("early", NOW + timedelta(minutes=1), timedelta(minutes=1)) is None
        new_claim = ledger.claim_due("second", NOW + timedelta(minutes=3), timedelta(minutes=1))
        assert new_claim
        with pytest.raises(ValueError, match="stale owner"):
            ledger.finish_task(new_claim.key, "first", TaskDisposition.SUCCEEDED, NOW + timedelta(minutes=3))


def test_attempts_are_append_only_monotonic_and_require_request_owner(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        ledger.plan_task(_task())
        task_claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
        assert task_claim
        request_claim = ledger.claim_request(task_claim.key, "worker", NOW, timedelta(minutes=1))
        assert request_claim
        assert ledger.record_attempt(request_claim.key, "worker", NOW, NOW, "timeout", safe_diagnostic="slow") == 1
        assert ledger.record_attempt(request_claim.key, "worker", NOW, NOW, "success", http_status=200) == 2
        with pytest.raises(ValueError, match="stale owner"):
            ledger.record_attempt(request_claim.key, "other", NOW, NOW, "success")
        assert [item["number"] for item in ledger.manifest().data["attempts"]] == [1, 2]
        connection = sqlite3.connect(ledger.path)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM attempts")
        connection.close()


@pytest.mark.parametrize(
    "disposition",
    [
        TaskDisposition.SUCCEEDED,
        TaskDisposition.CONFIRMED_EMPTY,
        TaskDisposition.NOT_APPLICABLE,
        TaskDisposition.DOMINATED,
    ],
)
def test_only_proven_terminal_states_satisfy_required_work(tmp_path: Path, disposition: TaskDisposition) -> None:
    with _open_ready(tmp_path / f"{disposition.value}.db") as ledger:
        task = _task()
        planned = ledger.plan_task(task)
        ledger.seal_plan([task])
        _finish_request_and_task(ledger, disposition)
        assert ledger.all_required_satisfied()
        with pytest.raises(ValueError, match="terminal"):
            ledger.finish_task(planned.key, "worker", TaskDisposition.RETRY_WAIT, NOW)


@pytest.mark.parametrize(
    "disposition",
    [
        TaskDisposition.RETRY_WAIT,
        TaskDisposition.MALFORMED,
        TaskDisposition.AUTHENTICATION_FAILED,
        TaskDisposition.SCHEMA_CHANGED,
        TaskDisposition.PERMANENT_FAILURE,
        TaskDisposition.CIRCUIT_OPEN,
        TaskDisposition.AMBIGUOUS,
        TaskDisposition.BLOCKED,
        TaskDisposition.UNKNOWN,
    ],
)
def test_failures_never_satisfy_required_work(tmp_path: Path, disposition: TaskDisposition) -> None:
    with _open_ready(tmp_path / f"{disposition.value}.db") as ledger:
        task = _task()
        ledger.plan_task(task)
        ledger.seal_plan([task])
        _finish_request_and_task(ledger, disposition)
        assert not ledger.all_required_satisfied()


def test_task_cannot_claim_success_from_unfinished_or_contradictory_request(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        ledger.plan_task(_task())
        task_claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
        assert task_claim
        with pytest.raises(ValueError, match="request disposition"):
            ledger.finish_task(task_claim.key, "worker", TaskDisposition.SUCCEEDED, NOW)
        request_claim = ledger.claim_request(task_claim.key, "worker", NOW, timedelta(minutes=1))
        assert request_claim
        ledger.finish_request(
            request_claim.key,
            "worker",
            TaskDisposition.SUCCEEDED,
            NOW,
            normalized_response={"result": "ok"},
        )
        with pytest.raises(ValueError, match="request disposition"):
            ledger.finish_task(task_claim.key, "worker", TaskDisposition.CONFIRMED_EMPTY, NOW, reason="empty")


def test_manifest_is_canonical_deterministic_and_records_checkpoint_and_publication(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        z_task = ledger.plan_task(_task("z-task"))
        a_task = ledger.plan_task(_task("a-task"))
        ledger.record_checkpoint(1, "c" * 64, "key-id", NOW)
        ledger.record_publication("candidate", "deadbeef", NOW)
        first = ledger.manifest()
        second = ledger.manifest()
        assert first == second
        assert json.dumps(first.data, sort_keys=True, separators=(",", ":")) == first.canonical_json
        assert len(first.digest) == 64
        assert [item["task_key"] for item in first.data["tasks"]] == sorted([a_task.key, z_task.key])
        assert first.data["checkpoints"][0]["sequence"] == 1
        assert [item["publication_key"] for item in first.data["publications"]] == ["pub-a-task", "pub-z-task"]
        assert first.data["publication_evidence"][0]["commit_sha"] == "deadbeef"


@pytest.mark.parametrize(
    "payload",
    [
        {"authorization": "Bearer secret"},
        {"api_key": "secret"},
        {"query": "https://example.test/?token=secret"},
        {"credential_path": "/private/key"},
    ],
)
def test_request_rejects_secret_material(payload: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="secret"):
        RequestSpec("x", "x", "GET", payload, (), "1", "epoch", "public")


def test_secret_guard_accepts_benign_token_count_fields() -> None:
    request = RequestSpec("x", "x", "POST", {"max_tokens": 256}, (), "1", "epoch", "public")
    assert request.canonical_content()["normalized_payload"] == {"max_tokens": 256}


@pytest.mark.parametrize(
    "fault_name",
    [
        "after_claim_commit",
        "after_attempt_commit",
        "after_response_commit",
        "after_task_terminalization",
        "after_manifest_commit",
    ],
)
def test_fault_injection_occurs_after_durable_boundary(tmp_path: Path, fault_name: str) -> None:
    path = tmp_path / f"{fault_name}.db"
    with _open_ready(path) as ledger:
        ledger.plan_task(_task())
        task_claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
        assert task_claim
        request_claim = ledger.claim_request(task_claim.key, "worker", NOW, timedelta(minutes=1))
        assert request_claim
        ledger.set_fault(fault_name)
        with pytest.raises(FaultInjectedError, match=fault_name):
            if fault_name == "after_claim_commit":
                ledger.claim_due("replacement", NOW + timedelta(minutes=2), timedelta(minutes=1))
            elif fault_name == "after_attempt_commit":
                ledger.record_attempt(request_claim.key, "worker", NOW, NOW, "success", http_status=200)
            elif fault_name == "after_response_commit":
                ledger.finish_request(
                    request_claim.key,
                    "worker",
                    TaskDisposition.SUCCEEDED,
                    NOW,
                    normalized_response={"title": "A durable response"},
                )
            elif fault_name == "after_task_terminalization":
                ledger.finish_request(
                    request_claim.key,
                    "worker",
                    TaskDisposition.SUCCEEDED,
                    NOW,
                    normalized_response={"title": "A durable response"},
                )
                ledger.finish_task(task_claim.key, "worker", TaskDisposition.SUCCEEDED, NOW, reason="proof")
            else:
                ledger.manifest()

    with Ledger.open(path) as reopened:
        assert reopened.pragma("integrity_check") == "ok"
        durable_manifest = reopened.manifest()
        manifest = durable_manifest.data
        if fault_name == "after_claim_commit":
            assert manifest["tasks"][0]["state"] == "leased"
            assert manifest["tasks"][0]["lease_owner"] == "replacement"
        if fault_name == "after_attempt_commit":
            assert manifest["attempts"] == [
                {
                    "finished_at": NOW.isoformat(timespec="microseconds"),
                    "http_status": 200,
                    "number": 1,
                    "outcome": "success",
                    "request_key": request_claim.key,
                    "response_digest": None,
                    "retry_delay_seconds": None,
                    "safe_diagnostic": "",
                    "started_at": NOW.isoformat(timespec="microseconds"),
                }
            ]
        if fault_name == "after_response_commit":
            assert manifest["requests"][0]["state"] == "succeeded"
            result = reopened.request_result(manifest["requests"][0]["request_key"])
            assert result is not None
            assert result.normalized_response == {"title": "A durable response"}
        if fault_name == "after_task_terminalization":
            assert manifest["tasks"][0]["state"] == "succeeded"
        if fault_name == "after_manifest_commit":
            connection = sqlite3.connect(path)
            stored = connection.execute(
                "SELECT digest, canonical_json FROM manifests WHERE generation_id = ? ORDER BY rowid LIMIT 1",
                (manifest["generation"]["generation_id"],),
            ).fetchone()
            connection.close()
            assert stored is not None
            assert len(stored[0]) == 64
            assert hashlib.sha256(stored[1].encode()).hexdigest() == stored[0]


def test_task_identity_is_canonical_and_caller_cannot_alias() -> None:
    first = _task("same")
    assert first.key == _task("same").key
    assert first.key != _task("other-publication").key
    assert first.key != _task("same", request=_request(freshness="2026-09")).key
    assert first.key != _task("same", applicability="not_applicable").key
    with pytest.raises(TypeError, match="key"):
        TaskSpec(  # type: ignore[call-arg]
            key="caller-alias",
            author_key="author-ada",
            publication_key="pub-same",
            provider="crossref",
            operation="lookup",
            request=_request(),
        )


def test_resume_detects_fk_valid_census_tamper(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    census = _census()
    spec = _generation(census)
    with Ledger.open(path) as ledger:
        ledger.create_or_resume(spec, census)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE authors SET normalized_name = ? WHERE generation_id = ? AND row_key = ?",
        ("tampered", spec.id, "author-ada"),
    )
    connection.commit()
    connection.close()
    with Ledger.open(path) as ledger, pytest.raises(ValueError, match="census"):
        ledger.create_or_resume(spec, census)


def test_plan_must_be_declared_and_sealed_exactly_once(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        first = _task("first")
        second = _task("second")
        ledger.plan_task(first)
        assert not ledger.all_required_satisfied()
        with pytest.raises(ValueError, match="expected obligations"):
            ledger.seal_plan([second])
        ledger.seal_plan([first])
        with pytest.raises(ValueError, match="sealed"):
            ledger.plan_task(second)
        with pytest.raises(ValueError, match="sealed"):
            ledger.seal_plan([first])


def test_empty_enabled_author_plan_requires_explicit_not_applicable_proof(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        with pytest.raises(ValueError, match="proof"):
            ledger.seal_plan([])
        ledger.seal_plan([], empty_author_proofs={"author-ada": "No applicable provider operation"})
        assert ledger.all_required_satisfied()


def test_manifest_holds_one_immediate_snapshot_against_independent_writer(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with _open_ready(path) as ledger:
        first = _task("first")
        second = _task("second")
        ledger.plan_task(first)

        def probe() -> None:
            writer = sqlite3.connect(path, timeout=0, isolation_level=None)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("BEGIN IMMEDIATE")
            writer.close()

        ledger._set_manifest_probe_for_test(probe)
        snapshot = ledger.manifest()
        ledger.plan_task(second)
        assert [task["task_key"] for task in snapshot.data["tasks"]] == [first.key]
        assert [task["task_key"] for task in ledger.manifest().data["tasks"]] == sorted([first.key, second.key])


def test_parallel_logical_alias_planning_collapses_to_one_identity(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    task = _task("same")
    with _open_ready(path):
        pass

    def plan() -> str:
        with Ledger.open(path) as ledger:
            return ledger.plan_task(task).key

    with ThreadPoolExecutor(max_workers=8) as executor:
        keys = list(executor.map(lambda _: plan(), range(16)))
    assert set(keys) == {task.key}
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM request_consumers").fetchone()[0] == 1
    connection.close()


def test_schema_v1_represents_every_architecture_evidence_class(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        connection = sqlite3.connect(ledger.path)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert {
            "field_provenance",
            "provider_state",
            "materializations",
            "validations",
            "plan_obligations",
            "census_obligations",
        } <= tables
        generation_columns = {row[1] for row in connection.execute("PRAGMA table_info(generations)")}
        assert {
            "base_commit",
            "input_digest",
            "policy_digest",
            "adapter_digest",
            "updated_at",
            "completed_at",
            "published_at",
            "checkpoint_sequence",
            "blocking_reason",
        } <= generation_columns
        publication_columns = {row[1] for row in connection.execute("PRAGMA table_info(publications)")}
        assert {"discovery_source", "normalized_title", "year", "exact_identifiers_json", "baseline_output_path"} <= (
            publication_columns
        )
        connection.close()


def test_generation_lifecycle_rejects_skips_and_terminal_reentry(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        with pytest.raises(ValueError, match="illegal"):
            ledger.transition_generation(GenerationState.PLANNING, GenerationState.PUBLISHED, NOW)
        task = _task()
        ledger.plan_task(task)
        ledger.seal_plan([task])
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        with pytest.raises(ValueError, match="illegal"):
            ledger.transition_generation(GenerationState.RUNNING, GenerationState.COMPLETE, NOW)
        ledger.transition_generation(
            GenerationState.RUNNING,
            GenerationState.BLOCKED,
            NOW,
            blocking_reason="Provider schema changed",
        )
        ledger.transition_generation(GenerationState.BLOCKED, GenerationState.SUPERSEDED, NOW)
        with pytest.raises(ValueError, match="illegal"):
            ledger.transition_generation(GenerationState.SUPERSEDED, GenerationState.RUNNING, NOW)


def test_complete_and_published_generation_are_forward_only(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        ledger.seal_plan([], empty_author_proofs={"author-ada": "No applicable provider operation"})
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        ledger.transition_generation(GenerationState.RUNNING, GenerationState.VALIDATING, NOW)
        ledger.record_validation("corpus", "succeeded", "e" * 64, "All checks passed")
        ledger.transition_generation(GenerationState.VALIDATING, GenerationState.COMPLETE, NOW)
        with pytest.raises(ValueError, match="illegal"):
            ledger.transition_generation(GenerationState.COMPLETE, GenerationState.PLANNING, NOW)
        ledger.record_publication("merged", "deadbeef", NOW)
        ledger.transition_generation(GenerationState.COMPLETE, GenerationState.PUBLISHED, NOW)
        with pytest.raises(ValueError, match="illegal"):
            ledger.transition_generation(GenerationState.PUBLISHED, GenerationState.RUNNING, NOW)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RequestSpec("authorization", "lookup", "GET", {}, (), "1", "epoch", "public"),
        lambda: RequestSpec("crossref", "lookup", "GET", {}, ("api_key",), "1", "epoch", "public"),
        lambda: RequestSpec("crossref", "lookup", "GET", {}, (), "1", "epoch", "access_token"),
        lambda: _task(applicability="credential=/private/key"),
    ],
)
def test_every_request_and_task_identity_string_rejects_secrets(factory: object) -> None:
    with pytest.raises(ValueError, match="secret"):
        factory()  # type: ignore[operator]
