from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from citeforge.refresh.census import AuthorCensus, AuthorCensusRow
from citeforge.refresh.ledger import (
    ApplicabilityReason,
    DominanceEvidence,
    EvidenceState,
    FaultInjectedError,
    Ledger,
    MaterializationEvidence,
    ProviderObservation,
    PublicationMetadata,
    RequestSpec,
    TaskSpec,
    ValidationSpec,
    inventory_tasks,
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


def _open_ready(path: Path, *, enabled: bool = False) -> Ledger:
    ledger = Ledger.open(path)
    census = _census(enabled=enabled)
    ledger.create_or_resume(_generation(census), census)
    return ledger


def _finish_request_and_task(ledger: Ledger, disposition: TaskDisposition) -> None:
    claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
    assert claim
    request_claim = ledger.claim_request(claim.key, "worker", NOW, timedelta(minutes=1))
    assert request_claim
    retry_at = NOW + timedelta(minutes=1) if disposition is TaskDisposition.RETRY_WAIT else None
    observation = ProviderObservation(
        provider="crossref",
        schema_version="1",
        response={"result": "ok"} if disposition is TaskDisposition.SUCCEEDED else {},
        authoritative_empty=disposition is TaskDisposition.CONFIRMED_EMPTY,
    )
    ledger.finish_request(
        request_claim.key,
        "worker",
        disposition,
        NOW,
        retry_at=retry_at,
        observation=observation,
        safe_diagnostic="classified outcome",
    )
    ledger.finish_task(
        claim.key,
        "worker",
        disposition,
        NOW,
        retry_at=retry_at,
        evidence=(
            ApplicabilityReason.NO_APPLICABLE_IDENTIFIER
            if disposition is TaskDisposition.NOT_APPLICABLE
            else DominanceEvidence((request_claim.key,), "stronger-current-observation", ("title", "year"))
            if disposition is TaskDisposition.DOMINATED
            else None
        ),
        reason="informational",
    )


def _seal_and_run(ledger: Ledger, tasks: list[TaskSpec]) -> None:
    ledger.seal_plan(tasks, required_validations=())
    ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)


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
        task = _task()
        ledger.plan_task(task)
        _seal_and_run(ledger, [task])

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
        task = _task()
        ledger.plan_task(task)
        _seal_and_run(ledger, [task])
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
        task = _task()
        ledger.plan_task(task)
        _seal_and_run(ledger, [task])
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
    ],
)
def test_only_proven_terminal_states_satisfy_required_work(tmp_path: Path, disposition: TaskDisposition) -> None:
    with _open_ready(tmp_path / f"{disposition.value}.db") as ledger:
        task = _task()
        planned = ledger.plan_task(task)
        _seal_and_run(ledger, [task])
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
        _seal_and_run(ledger, [task])
        _finish_request_and_task(ledger, disposition)
        assert not ledger.all_required_satisfied()


def test_task_cannot_claim_success_from_unfinished_or_contradictory_request(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        task = _task()
        ledger.plan_task(task)
        _seal_and_run(ledger, [task])
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
            observation=ProviderObservation("crossref", "1", {"result": "ok"}),
        )
        with pytest.raises(ValueError, match="request disposition"):
            ledger.finish_task(task_claim.key, "worker", TaskDisposition.CONFIRMED_EMPTY, NOW, reason="empty")


def test_manifest_is_canonical_deterministic_and_records_checkpoint_and_publication(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        z_task = ledger.plan_task(_task("z-task"))
        a_task = ledger.plan_task(_task("a-task"))
        ledger.record_checkpoint(1, "c" * 64, "key-id", NOW)
        binding = ledger.current_manifest_binding()
        ledger.record_publication("candidate", "deadbeef", NOW, candidate_digest=binding, manifest_digest=binding)
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
        task = _task()
        ledger.plan_task(task)
        _seal_and_run(ledger, [task])
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
                    observation=ProviderObservation("crossref", "1", {"title": "A durable response"}),
                )
            elif fault_name == "after_task_terminalization":
                ledger.finish_request(
                    request_claim.key,
                    "worker",
                    TaskDisposition.SUCCEEDED,
                    NOW,
                    observation=ProviderObservation("crossref", "1", {"title": "A durable response"}),
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
    not_applicable = TaskSpec("author-ada", "pub-same", "crossref", "lookup", None, applicability="not_applicable")
    assert first.key != not_applicable.key
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


def test_all_disabled_census_permits_empty_plan(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        ledger.seal_plan([])
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
            "validation_obligations",
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
        ledger.seal_plan([], required_validations=(ValidationSpec("corpus"),))
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        ledger.transition_generation(GenerationState.RUNNING, GenerationState.VALIDATING, NOW)
        ledger.record_validation("corpus", EvidenceState.SUCCEEDED, "e" * 64, "All checks passed")
        binding = ledger.current_manifest_binding()
        ledger.record_materialization(MaterializationEvidence("stage", binding, {}, EvidenceState.VALIDATED))
        ledger.transition_generation(GenerationState.VALIDATING, GenerationState.COMPLETE, NOW)
        with pytest.raises(ValueError, match="illegal"):
            ledger.transition_generation(GenerationState.COMPLETE, GenerationState.PLANNING, NOW)
        ledger.record_publication("verified_merge", "deadbeef", NOW, candidate_digest=binding, manifest_digest=binding)
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


def test_schema_v1_database_fails_closed_at_open(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="schema version: 1"):
        Ledger.open(path)


def test_inventory_obligations_derive_exactly_from_enabled_census_sources(tmp_path: Path) -> None:
    scholar = _census().rows[0]
    both = replace(
        scholar,
        row_key="author-both",
        name="Grace Hopper",
        normalized_name="grace hopper",
        scholar_id="Scholar456",
        dblp_id="12/345",
    )
    disabled = replace(
        scholar,
        row_key="author-disabled",
        name="Disabled",
        normalized_name="disabled",
        scholar_id="",
        enabled=False,
        exclusion_reason="No configured profile",
        disposition=TaskDisposition.NOT_APPLICABLE,
    )
    census = AuthorCensus((scholar, both, disabled))
    derived = inventory_tasks(census, {"scholar": "1", "dblp": "1"}, "2026-08")
    assert [(task.author_key, task.provider, task.operation) for task in derived] == [
        ("author-ada", "scholar", "inventory"),
        ("author-both", "dblp", "inventory"),
        ("author-both", "scholar", "inventory"),
    ]
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        spec = GenerationSpec(census, "policy-v1", {"scholar": "1", "dblp": "1"}, "abc123")
        ledger.create_or_resume(spec, census)
        for task in derived[:-1]:
            ledger.plan_task(task)
        with pytest.raises(ValueError, match="inventory obligation"):
            ledger.seal_plan(derived[:-1], required_validations=())


def test_typed_terminal_evidence_is_required(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        task = _task()
        ledger.plan_task(task)
        _seal_and_run(ledger, [task])
        claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
        assert claim
        request_claim = ledger.claim_request(claim.key, "worker", NOW, timedelta(minutes=1))
        assert request_claim
        with pytest.raises(ValueError, match="observation"):
            ledger.finish_request(request_claim.key, "worker", TaskDisposition.SUCCEEDED, NOW)
        with pytest.raises(ValueError, match="authoritative empty"):
            ledger.finish_request(
                request_claim.key,
                "worker",
                TaskDisposition.CONFIRMED_EMPTY,
                NOW,
                observation=ProviderObservation("crossref", "1", {}, False),
            )


def test_not_applicable_and_dominated_require_typed_matching_evidence(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        applicable = _task("applicable")
        ledger.plan_task(applicable)
        _seal_and_run(ledger, [applicable])
        claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
        assert claim
        with pytest.raises(ValueError, match="applicability"):
            ledger.finish_task(
                claim.key,
                "worker",
                TaskDisposition.NOT_APPLICABLE,
                NOW,
                evidence=ApplicabilityReason.NO_APPLICABLE_IDENTIFIER,
            )


def test_planned_not_applicable_task_has_no_request_and_closes_with_typed_reason(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        task = TaskSpec(
            "author-ada",
            "pub-na",
            "crossref",
            "lookup",
            None,
            applicability="not_applicable",
        )
        claim = ledger.plan_task(task)
        ledger.seal_plan([task])
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        assert claim.request_key is None
        assert ledger.claim_due("worker", NOW, timedelta(minutes=1)) is None
        ledger.finish_task(
            task.key,
            "planner",
            TaskDisposition.NOT_APPLICABLE,
            NOW,
            evidence=ApplicabilityReason.NO_APPLICABLE_IDENTIFIER,
        )
        assert ledger.all_required_satisfied()
        with pytest.raises(ValueError, match="dominance"):
            ledger.finish_task(
                claim.key,
                "worker",
                TaskDisposition.DOMINATED,
                NOW,
                evidence=DominanceEvidence(("0" * 64,), "rule", ()),
            )


def test_complete_requires_exact_validations_and_bound_materialization(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        ledger.seal_plan([], required_validations=(ValidationSpec("corpus"),))
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        ledger.transition_generation(GenerationState.RUNNING, GenerationState.VALIDATING, NOW)
        with pytest.raises(ValueError, match="sealed obligation"):
            ledger.record_validation("undeclared", EvidenceState.SUCCEEDED, "d" * 64)
        with pytest.raises(ValueError, match="validation"):
            ledger.transition_generation(GenerationState.VALIDATING, GenerationState.COMPLETE, NOW)
        ledger.record_validation("corpus", EvidenceState.SUCCEEDED, "e" * 64)
        with pytest.raises(ValueError, match="materialization"):
            ledger.transition_generation(GenerationState.VALIDATING, GenerationState.COMPLETE, NOW)
        binding = ledger.current_manifest_binding()
        ledger.record_materialization(MaterializationEvidence("stage", binding, {"files": 1}, EvidenceState.VALIDATED))
        ledger.transition_generation(GenerationState.VALIDATING, GenerationState.COMPLETE, NOW)
        ledger.record_publication("candidate", "deadbeef", NOW, candidate_digest=binding, manifest_digest=binding)
        with pytest.raises(ValueError, match="verified merge"):
            ledger.transition_generation(GenerationState.COMPLETE, GenerationState.PUBLISHED, NOW)
        ledger.record_publication(
            "verified_merge",
            "feedface",
            NOW,
            candidate_digest=binding,
            manifest_digest=binding,
        )
        ledger.transition_generation(GenerationState.COMPLETE, GenerationState.PUBLISHED, NOW)


@pytest.mark.parametrize(
    "payload",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": object()},
        {"value": {1, 2}},
    ],
)
def test_canonical_payload_rejects_non_json_values(payload: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        RequestSpec("crossref", "lookup", "GET", payload, (), "1", "epoch", "public")


@pytest.mark.parametrize(
    "value",
    [
        "https://user:password@example.test/data",
        "/home/person/.aws/credentials",
        "/home/person/.ssh/id_ed25519",
        "/safe/key.pem",
        "/safe/service-credentials.json",
    ],
)
def test_secret_paths_and_url_userinfo_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        RequestSpec("crossref", "lookup", "GET", {"path": value}, (), "1", "epoch", "public")


def test_typed_evidence_writers_are_manifested(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        publication = PublicationMetadata(
            "author-ada",
            "pub-typed",
            "scholar",
            "typed title",
            2026,
            {"doi": "10.1/example"},
            "output/a.bib",
            "monthly",
        )
        ledger.record_publication_metadata(publication)
        ledger.record_provider_state("crossref", "public", 2, "closed", 3, 1)
        task = _task("provenance")
        ledger.plan_task(task)
        ledger.record_field_provenance(
            "author-ada",
            "pub-typed",
            "title",
            "a" * 64,
            "crossref",
            task.request.key,
            "trust-policy",
        )
        manifest = ledger.manifest().data
        typed_publication = next(item for item in manifest["publications"] if item["publication_key"] == "pub-typed")
        assert typed_publication["normalized_title"] == "typed title"
        assert manifest["provider_state"][0]["current_concurrency"] == 2
        assert manifest["field_provenance"][0]["field_name"] == "title"


def test_claims_require_sealed_running_generation(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        task = _task()
        ledger.plan_task(task)
        assert ledger.claim_due("worker", NOW, timedelta(minutes=1)) is None
        ledger.seal_plan([task], required_validations=())
        assert ledger.claim_due("worker", NOW, timedelta(minutes=1)) is None
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        assert ledger.claim_due("worker", NOW, timedelta(minutes=1)) is not None


@pytest.mark.parametrize("mutation", ["update", "delete", "insert"])
def test_sealed_obligation_tamper_is_blocked_and_detected(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / f"{mutation}.db"
    with _open_ready(path) as ledger:
        task = _task()
        ledger.plan_task(task)
        ledger.seal_plan([task], required_validations=(ValidationSpec("corpus"),))
    connection = sqlite3.connect(path)
    statement = {
        "update": "UPDATE plan_obligations SET provider = 'dblp'",
        "delete": "DELETE FROM plan_obligations",
        "insert": (
            "INSERT INTO validation_obligations(generation_id, check_name, required) "
            "SELECT generation_id, 'extra', 1 FROM generations"
        ),
    }[mutation]
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        connection.execute(statement)
    connection.close()
    with Ledger.open(path) as ledger:
        census = _census(enabled=False)
        ledger.create_or_resume(_generation(census), census)
