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
    DominanceRule,
    FaultInjectedError,
    Ledger,
    PlannedTask,
    ProvenanceRule,
    ProviderObservation,
    PublicationMetadata,
    RequestSpec,
    TaskSpec,
    ValidationSpec,
    inventory_tasks,
)
from citeforge.refresh.types import GenerationSpec, GenerationState, PlanPhase, TaskDisposition

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
        response={"title": "Complete title", "year": 2026} if disposition is TaskDisposition.SUCCEEDED else {},
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
            else DominanceEvidence(
                (request_claim.key,), "stronger-current-observation", ("title", "year"), request_claim.key
            )
            if disposition is TaskDisposition.DOMINATED
            else None
        ),
        reason="informational",
    )


def _seal_and_run(ledger: Ledger, tasks: list[TaskSpec]) -> None:
    ledger.seal_plan(tasks, required_validations=())
    ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)


def _record_dominance_observations(
    ledger: Ledger,
    stronger: TaskSpec,
    stronger_response: dict[str, object],
    dominated: TaskSpec,
    dominated_response: dict[str, object],
) -> tuple[str, str, str, str]:
    claims = {}
    for index in range(2):
        owner = f"worker-{index}"
        claim = ledger.claim_due(owner, NOW, timedelta(minutes=1))
        assert claim and claim.request_key
        request_claim = ledger.claim_request(claim.key, owner, NOW, timedelta(minutes=1))
        assert request_claim
        task, response = (
            (stronger, stronger_response)
            if request_claim.key == stronger.request.key
            else (dominated, dominated_response)
        )
        ledger.finish_request(
            request_claim.key,
            owner,
            TaskDisposition.SUCCEEDED,
            NOW,
            observation=ProviderObservation(task.provider, "1", response),
        )
        claims[task.key] = (claim.key, request_claim.key, owner)
        if task is stronger:
            ledger.finish_task(claim.key, owner, TaskDisposition.SUCCEEDED, NOW)
    strong_request_key = claims[stronger.key][1]
    dominated_claim_key, dominated_request_key, dominated_owner = claims[dominated.key]
    return strong_request_key, dominated_request_key, dominated_claim_key, dominated_owner


def test_initial_phased_round_runs_before_structural_closure_and_blocks_completeness(tmp_path: Path) -> None:
    census = _census()
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        ledger.create_or_resume(_generation(census), census)
        inventory = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
        round_one = ledger.commit_initial_round(
            [PlannedTask(inventory, expands_plan=True)],
            source_evidence_digest="a" * 64,
            now=NOW,
        )
        assert round_one.sequence == 1 and round_one.phase is PlanPhase.INVENTORIES
        assert ledger.plan_status().revision == 1
        assert not ledger.plan_status().closed
        assert not ledger.all_required_satisfied()
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        assert ledger.claim_due("worker", NOW, timedelta(minutes=1)) is not None
        with pytest.raises(ValueError, match="discovery"):
            ledger.transition_generation(GenerationState.RUNNING, GenerationState.VALIDATING, NOW)


def test_initial_round_exact_replay_and_conflict(tmp_path: Path) -> None:
    census = _census()
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        ledger.create_or_resume(_generation(census), census)
        inventory = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
        declared = [PlannedTask(inventory, expands_plan=True)]
        first = ledger.commit_initial_round(declared, source_evidence_digest="a" * 64, now=NOW)
        assert ledger.commit_initial_round(declared, source_evidence_digest="a" * 64, now=NOW) == first
        with pytest.raises(ValueError, match="conflicting"):
            ledger.commit_initial_round(declared, source_evidence_digest="b" * 64, now=NOW)


def test_initial_round_rejects_missing_or_forged_mandatory_inventory(tmp_path: Path) -> None:
    census = _census()
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        ledger.create_or_resume(_generation(census), census)
        with pytest.raises(ValueError, match="canonical inventory"):
            ledger.commit_initial_round([], source_evidence_digest="a" * 64, now=NOW)
        canonical = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
        assert canonical.request is not None
        forged_request = replace(canonical.request, normalized_payload={"profile_id": "WrongProfile"})
        forged = replace(canonical, request=forged_request)
        with pytest.raises(ValueError, match="canonical inventory"):
            ledger.commit_initial_round(
                [PlannedTask(forged, expands_plan=True)], source_evidence_digest="a" * 64, now=NOW
            )


def test_initial_round_rejects_caller_disabling_mandatory_inventory_expansion(tmp_path: Path) -> None:
    census = _census()
    with Ledger.open(tmp_path / "inventory-expansion.db") as ledger:
        ledger.create_or_resume(_generation(census), census)
        inventory = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
        with pytest.raises(ValueError, match="expand"):
            ledger.commit_initial_round(
                [PlannedTask(inventory, expands_plan=False)],
                source_evidence_digest="a" * 64,
                now=NOW,
            )


def test_unbound_compatibility_task_is_inert_before_seal(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        ledger.plan_task(_task())
        with pytest.raises(ValueError, match="initial round"):
            ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)


def test_schema_v2_is_rejected_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
    connection.commit()
    before = path.read_bytes()
    connection.close()
    with pytest.raises(ValueError, match="schema version: 2"):
        Ledger.open(path)
    assert path.read_bytes() == before


def test_structurally_inconsistent_schema_v3_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken-v3.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '3')")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="structurally inconsistent"):
        Ledger.open(path)


def test_schema_v3_fingerprint_rejects_unrecognized_structure_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "altered-v3.db"
    with Ledger.open(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE unexpected_extension(value TEXT)")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="fingerprint"):
        Ledger.open(path)
    assert path.read_bytes() == before


def test_existing_database_without_schema_metadata_is_rejected_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "alien.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE alien(value TEXT)")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="nonempty"):
        Ledger.open(path)
    assert path.read_bytes() == before


def test_recomputed_stored_fingerprint_cannot_bless_alien_schema(tmp_path: Path) -> None:
    path = tmp_path / "forged-fingerprint.db"
    with Ledger.open(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE alien(value TEXT)")
    objects = [
        {"name": row[1], "sql": row[3], "table": row[2], "type": row[0]}
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]
    forged = _canonical_digest(objects)
    connection.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_fingerprint'", (forged,))
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="fingerprint"):
        Ledger.open(path)
    assert path.read_bytes() == before


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def test_empty_reduction_receipt_enables_structural_closure(tmp_path: Path) -> None:
    census = _census()
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        ledger.create_or_resume(_generation(census), census)
        inventory = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
        ledger.commit_initial_round(
            [PlannedTask(inventory, expands_plan=True)], source_evidence_digest="a" * 64, now=NOW
        )
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
        assert claim is not None
        request_claim = ledger.claim_request(claim.key, "worker", NOW, timedelta(minutes=1))
        assert request_claim is not None
        observation = ProviderObservation("scholar", "1", {}, authoritative_empty=True)
        ledger.finish_request(
            request_claim.key, "worker", TaskDisposition.CONFIRMED_EMPTY, NOW, observation=observation
        )
        ledger.finish_task(claim.key, "worker", TaskDisposition.CONFIRMED_EMPTY, NOW)
        receipt = ledger.commit_reduction(
            inventory.key,
            source_evidence_digest=observation.digest,
            publications=(),
            tasks=(),
            now=NOW,
        )
        assert receipt.source_task_keys == (inventory.key,)
        assert (
            ledger.commit_reduction(
                inventory.key,
                source_evidence_digest=observation.digest,
                publications=(),
                tasks=(),
                now=NOW,
            )
            == receipt
        )
        assert ledger.plan_status().open_expanders == 0
        closure_digest = _canonical_digest(dict(ledger.closure_content()))
        status = ledger.close_plan(expected_closure_digest=closure_digest, now=NOW)
        assert status.closed and status.authority_mode == "phased_structural"
        assert ledger.close_plan(expected_closure_digest=closure_digest, now=NOW) == status
        with pytest.raises(ValueError, match="conflicting"):
            ledger.close_plan(expected_closure_digest="b" * 64, now=NOW)
        with pytest.raises(ValueError, match="open running plan"):
            ledger.commit_reduction(
                inventory.key,
                source_evidence_digest=observation.digest,
                publications=(),
                tasks=(),
                now=NOW,
            )
        assert not ledger.all_required_satisfied()
        with pytest.raises(ValueError, match="discovery"):
            ledger.transition_generation(GenerationState.RUNNING, GenerationState.VALIDATING, NOW)


def test_task5a_schema_rejects_raw_discovery_authority_update(tmp_path: Path) -> None:
    path = tmp_path / "blocked-discovery.db"
    with Ledger.open(path) as ledger:
        ledger.create_or_resume(_generation(_census()), _census())
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="Task 5A"):
        connection.execute("UPDATE generations SET discovery_closed = 1")
    with pytest.raises(sqlite3.IntegrityError, match="Task 5A"):
        connection.execute("UPDATE generations SET plan_authority_mode = 'phased_authoritative'")
    connection.close()
    with Ledger.open(path) as ledger:
        assert not ledger.plan_status().discovery_closed
        assert not ledger.all_required_satisfied()


def test_status_manifest_and_reopen_reject_authority_corruption_without_trigger(tmp_path: Path) -> None:
    path = tmp_path / "corrupt-discovery.db"
    ledger = _open_ready(path)
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER generations_task5a_authority_update")
    connection.execute("UPDATE generations SET discovery_closed = 1, plan_authority_mode = 'phased_authoritative'")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="Task 5A authority"):
        ledger.plan_status()
    with pytest.raises(ValueError, match="Task 5A authority"):
        ledger.manifest()
    ledger.close()
    with pytest.raises(ValueError, match="fingerprint"):
        Ledger.open(path)


def test_task5a_exposes_no_caller_self_attestation_surface(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "no-authority-api.db") as ledger:
        assert not hasattr(ledger, "commit_discovery_closure")


def test_arbitrary_publication_and_caller_hashes_cannot_authorize_discovery(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "self-attestation.db") as ledger:
        publication = PublicationMetadata(
            "author-ada", "pub-arbitrary", "scholar", "Arbitrary publication", 2026, {}, "", "monthly"
        )
        ledger.commit_initial_round([], publications=(publication,), source_evidence_digest="a" * 64, now=NOW)
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        caller_hash = _canonical_digest(dict(ledger.closure_content()))
        ledger.close_plan(expected_closure_digest=caller_hash, now=NOW)
        assert ledger.plan_status().closed
        assert not ledger.plan_status().discovery_closed
        assert not ledger.all_required_satisfied()
        with pytest.raises(ValueError, match="discovery"):
            ledger.transition_generation(GenerationState.RUNNING, GenerationState.VALIDATING, NOW)


def test_reduction_rejects_wrong_evidence_and_nonexpanding_source(tmp_path: Path) -> None:
    census = _census()
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        ledger.create_or_resume(_generation(census), census)
        inventory = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
        nonexpanding = TaskSpec("author-ada", None, "crossref", "discovery", replace(_request(), operation="discovery"))
        ledger.commit_initial_round(
            [PlannedTask(inventory, expands_plan=True), PlannedTask(nonexpanding, expands_plan=False)],
            source_evidence_digest="a" * 64,
            now=NOW,
        )
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        with pytest.raises(ValueError, match="expanding"):
            ledger.commit_reduction(
                nonexpanding.key,
                source_evidence_digest="b" * 64,
                publications=(),
                tasks=(),
                now=NOW,
            )


def test_reduction_rejects_disallowed_phase_edge(tmp_path: Path) -> None:
    with Ledger.open(tmp_path / "invalid-edge.db") as ledger:
        inventory, observation = _ready_expanding_inventory(ledger)
        with pytest.raises(ValueError, match="phase edge"):
            ledger.commit_reduction(
                inventory.key,
                source_evidence_digest=observation.digest,
                publications=(),
                tasks=(),
                phase=PlanPhase.REDUCERS,
                now=NOW,
            )


@pytest.mark.parametrize(
    "fault",
    [
        "after_initial_round_publications",
        "after_initial_round_tasks",
        "after_initial_round_obligations",
        "after_initial_round_round",
    ],
)
def test_initial_round_fault_rolls_back_atomically(tmp_path: Path, fault: str) -> None:
    census = _census()
    with Ledger.open(tmp_path / f"{fault}.db") as ledger:
        ledger.create_or_resume(_generation(census), census)
        inventory = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
        ledger.set_fault(fault)
        with pytest.raises(FaultInjectedError):
            ledger.commit_initial_round(
                [PlannedTask(inventory, expands_plan=True)], source_evidence_digest="a" * 64, now=NOW
            )
        assert ledger.plan_status().revision == 0
        manifest = ledger.manifest().data
        assert manifest["tasks"] == []
        assert manifest["plan_rounds"] == []


def test_initial_round_postcommit_interruption_exposes_complete_round(tmp_path: Path) -> None:
    census = _census()
    with Ledger.open(tmp_path / "initial-postcommit.db") as ledger:
        ledger.create_or_resume(_generation(census), census)
        inventory = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
        ledger.set_fault("after_initial_round_commit")
        with pytest.raises(FaultInjectedError):
            ledger.commit_initial_round(
                [PlannedTask(inventory, expands_plan=True)], source_evidence_digest="a" * 64, now=NOW
            )
        assert ledger.plan_status().revision == 1
        assert len(ledger.manifest().data["plan_rounds"]) == 1


def _ready_expanding_inventory(ledger: Ledger) -> tuple[TaskSpec, ProviderObservation]:
    census = _census()
    ledger.create_or_resume(_generation(census), census)
    inventory = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
    ledger.commit_initial_round([PlannedTask(inventory, expands_plan=True)], source_evidence_digest="a" * 64, now=NOW)
    ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
    claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
    assert claim is not None
    request_claim = ledger.claim_request(claim.key, "worker", NOW, timedelta(minutes=1))
    assert request_claim is not None
    observation = ProviderObservation("scholar", "1", {}, authoritative_empty=True)
    ledger.finish_request(request_claim.key, "worker", TaskDisposition.CONFIRMED_EMPTY, NOW, observation=observation)
    ledger.finish_task(claim.key, "worker", TaskDisposition.CONFIRMED_EMPTY, NOW)
    return inventory, observation


@pytest.mark.parametrize(
    "fault",
    [
        "after_reduction_publications",
        "after_reduction_tasks",
        "after_reduction_obligations",
        "after_reduction_round",
        "after_reduction_receipt",
    ],
)
def test_reduction_fault_exposes_only_old_round(tmp_path: Path, fault: str) -> None:
    with Ledger.open(tmp_path / f"{fault}.db") as ledger:
        inventory, observation = _ready_expanding_inventory(ledger)
        ledger.set_fault(fault)
        with pytest.raises(FaultInjectedError):
            ledger.commit_reduction(
                inventory.key,
                source_evidence_digest=observation.digest,
                publications=(),
                tasks=(),
                now=NOW,
            )
        assert ledger.plan_status().revision == 1
        manifest = ledger.manifest().data
        assert len(manifest["plan_rounds"]) == 1
        assert manifest["reduction_receipts"] == []


def test_reduction_postcommit_interruption_exposes_complete_new_round(tmp_path: Path) -> None:
    with Ledger.open(tmp_path / "reduction-postcommit.db") as ledger:
        inventory, observation = _ready_expanding_inventory(ledger)
        ledger.set_fault("after_reduction_commit")
        with pytest.raises(FaultInjectedError):
            ledger.commit_reduction(
                inventory.key,
                source_evidence_digest=observation.digest,
                publications=(),
                tasks=(),
                now=NOW,
            )
        assert ledger.plan_status().revision == 2
        manifest = ledger.manifest().data
        assert len(manifest["plan_rounds"]) == 2
        assert len(manifest["reduction_receipts"]) == 1


def test_reduction_replay_rejects_changed_phase_version_publication_task_or_expansion(tmp_path: Path) -> None:
    with Ledger.open(tmp_path / "replay-conflict.db") as ledger:
        inventory, observation = _ready_expanding_inventory(ledger)
        derived = TaskSpec("author-ada", None, "crossref", "discovery", replace(_request(), operation="discovery"))
        base_tasks = (PlannedTask(derived, expands_plan=True),)
        ledger.commit_reduction(
            inventory.key,
            source_evidence_digest=observation.digest,
            publications=(),
            tasks=base_tasks,
            now=NOW,
        )
        conflicts = (
            {"phase": PlanPhase.AUTHORITATIVE},
            {"reducer_version": "2"},
            {"reducer_id": "alternate_reducer"},
            {"tasks": ()},
            {"tasks": (PlannedTask(derived, expands_plan=False),)},
            {
                "publications": (
                    PublicationMetadata("author-ada", "pub-new", "scholar", "New publication", 2026, {}, "", "monthly"),
                )
            },
        )
        for changed in conflicts:
            arguments = {
                "source_evidence_digest": observation.digest,
                "publications": (),
                "tasks": base_tasks,
                "now": NOW,
                **changed,
            }
            with pytest.raises(ValueError, match="conflicting"):
                ledger.commit_reduction(inventory.key, **arguments)


def test_close_postcommit_interruption_exposes_closed_plan(tmp_path: Path) -> None:
    with Ledger.open(tmp_path / "close-postcommit.db") as ledger:
        inventory, observation = _ready_expanding_inventory(ledger)
        ledger.commit_reduction(
            inventory.key,
            source_evidence_digest=observation.digest,
            publications=(),
            tasks=(),
            now=NOW,
        )
        digest = _canonical_digest(dict(ledger.closure_content()))
        ledger.set_fault("after_plan_close_commit")
        with pytest.raises(FaultInjectedError):
            ledger.close_plan(expected_closure_digest=digest, now=NOW)
        assert ledger.plan_status().closed
        assert not ledger.all_required_satisfied()


def test_close_precommit_interruption_rolls_back_validation_declaration(tmp_path: Path) -> None:
    with Ledger.open(tmp_path / "close-precommit.db") as ledger:
        inventory, observation = _ready_expanding_inventory(ledger)
        ledger.commit_reduction(
            inventory.key,
            source_evidence_digest=observation.digest,
            publications=(),
            tasks=(),
            now=NOW,
        )
        required = (ValidationSpec("corpus"),)
        digest = _canonical_digest(dict(ledger.closure_content(required)))
        ledger.set_fault("after_plan_close_validations")
        with pytest.raises(FaultInjectedError):
            ledger.close_plan(expected_closure_digest=digest, required_validations=required, now=NOW)
        assert not ledger.plan_status().closed
        assert ledger.manifest().data["validation_obligations"] == []


def test_aggregate_reduction_consumes_each_inventory_source_exactly_once(tmp_path: Path) -> None:
    row = replace(_census().rows[0], dblp_id="dblp/ada")
    census = AuthorCensus((row,))
    generation = GenerationSpec(census, "policy-v1", {"scholar": "1", "dblp": "1"}, "abc123")
    with Ledger.open(tmp_path / "aggregate.db") as ledger:
        ledger.create_or_resume(generation, census)
        inventories = inventory_tasks(census, {"scholar": "1", "dblp": "1"}, "2026-08")
        ledger.commit_initial_round(
            [PlannedTask(task, expands_plan=True) for task in inventories],
            source_evidence_digest="a" * 64,
            now=NOW,
        )
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        evidence: dict[str, str] = {}
        for index in range(2):
            owner = f"worker-{index}"
            claim = ledger.claim_due(owner, NOW, timedelta(minutes=1))
            assert claim is not None
            request_claim = ledger.claim_request(claim.key, owner, NOW, timedelta(minutes=1))
            assert request_claim is not None
            provider = next(task.provider for task in inventories if task.key == claim.key)
            observation = ProviderObservation(provider, "1", {}, authoritative_empty=True)
            ledger.finish_request(
                request_claim.key, owner, TaskDisposition.CONFIRMED_EMPTY, NOW, observation=observation
            )
            ledger.finish_task(claim.key, owner, TaskDisposition.CONFIRMED_EMPTY, NOW)
            evidence[claim.key] = observation.digest
        keys = tuple(sorted(task.key for task in inventories))
        aggregate = _canonical_digest([evidence[key] for key in keys])
        receipt = ledger.commit_reduction(
            tuple(reversed(keys)), source_evidence_digest=aggregate, publications=(), tasks=(), now=NOW
        )
        assert receipt.source_task_keys == keys
        assert ledger.plan_status().open_expanders == 0
        with pytest.raises(ValueError, match="conflicting"):
            ledger.commit_reduction(
                keys[0], source_evidence_digest=evidence[keys[0]], publications=(), tasks=(), now=NOW
            )


def test_closed_phased_manifest_is_byte_identical_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "reopen.db"
    census = _census()
    with Ledger.open(path) as ledger:
        inventory, observation = _ready_expanding_inventory(ledger)
        ledger.commit_reduction(
            inventory.key,
            source_evidence_digest=observation.digest,
            publications=(),
            tasks=(),
            now=NOW,
        )
        digest = _canonical_digest(dict(ledger.closure_content()))
        ledger.close_plan(expected_closure_digest=digest, now=NOW)
        before = ledger.manifest().canonical_json
    connection = sqlite3.connect(path)
    for statement in (
        "UPDATE plan_rounds SET planner_version = 'tampered'",
        "UPDATE observations SET response_digest = '" + "b" * 64 + "'",
        "DELETE FROM reduction_receipts",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)
        connection.rollback()
    connection.close()
    with Ledger.open(path) as ledger:
        ledger.create_or_resume(_generation(census), census)
        assert ledger.manifest().canonical_json == before


def test_structural_close_blocks_publication_identity_insert_via_api_and_sql(tmp_path: Path) -> None:
    path = tmp_path / "closed-publications.db"
    with Ledger.open(path) as ledger:
        inventory, observation = _ready_expanding_inventory(ledger)
        ledger.commit_reduction(
            inventory.key,
            source_evidence_digest=observation.digest,
            publications=(),
            tasks=(),
            now=NOW,
        )
        digest = _canonical_digest(dict(ledger.closure_content()))
        ledger.close_plan(expected_closure_digest=digest, now=NOW)
        before = ledger.manifest().canonical_json
        metadata = PublicationMetadata("author-ada", "pub-late", "scholar", "Late publication", 2026, {}, "", "monthly")
        with pytest.raises(ValueError, match="closed"):
            ledger.record_publication_metadata(metadata)
    connection = sqlite3.connect(path)
    generation_id = _generation(_census()).id
    with pytest.raises(sqlite3.IntegrityError, match="closed"):
        connection.execute(
            "INSERT INTO publications(generation_id, author_key, publication_key) VALUES (?, ?, ?)",
            (generation_id, "author-ada", "pub-raw"),
        )
    connection.close()
    with Ledger.open(path) as ledger:
        assert ledger.manifest().canonical_json == before


def test_two_forward_reductions_are_contiguous_and_close_only_after_derived_expander(tmp_path: Path) -> None:
    census = _census()
    with Ledger.open(tmp_path / "rounds.db") as ledger:
        ledger.create_or_resume(_generation(census), census)
        inventory = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
        ledger.commit_initial_round(
            [PlannedTask(inventory, expands_plan=True)], source_evidence_digest="a" * 64, now=NOW
        )
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        inventory_claim = ledger.claim_due("inventory-worker", NOW, timedelta(minutes=1))
        assert inventory_claim is not None
        inventory_request = ledger.claim_request(inventory_claim.key, "inventory-worker", NOW, timedelta(minutes=1))
        assert inventory_request is not None
        inventory_observation = ProviderObservation("scholar", "1", {}, authoritative_empty=True)
        ledger.finish_request(
            inventory_request.key,
            "inventory-worker",
            TaskDisposition.CONFIRMED_EMPTY,
            NOW,
            observation=inventory_observation,
        )
        ledger.finish_task(inventory_claim.key, "inventory-worker", TaskDisposition.CONFIRMED_EMPTY, NOW)
        derived_request = replace(_request(), operation="discovery")
        derived = TaskSpec("author-ada", None, "crossref", "discovery", derived_request)
        first = ledger.commit_reduction(
            inventory.key,
            source_evidence_digest=inventory_observation.digest,
            publications=(),
            tasks=(PlannedTask(derived, expands_plan=True),),
            phase=PlanPhase.DISCOVERY,
            now=NOW,
        )
        assert first.source_task_keys == (inventory.key,)
        with pytest.raises(ValueError, match="open or blocking work"):
            ledger.close_plan(expected_closure_digest=_canonical_digest(dict(ledger.closure_content())), now=NOW)
        derived_claim = ledger.claim_due("derived-worker", NOW, timedelta(minutes=1))
        assert derived_claim is not None and derived_claim.key == derived.key
        derived_request_claim = ledger.claim_request(derived_claim.key, "derived-worker", NOW, timedelta(minutes=1))
        assert derived_request_claim is not None
        derived_observation = ProviderObservation("crossref", "1", {"title": "Complete", "year": 2026})
        ledger.finish_request(
            derived_request_claim.key,
            "derived-worker",
            TaskDisposition.SUCCEEDED,
            NOW,
            observation=derived_observation,
        )
        ledger.finish_task(derived_claim.key, "derived-worker", TaskDisposition.SUCCEEDED, NOW)
        second = ledger.commit_reduction(
            derived.key,
            source_evidence_digest=derived_observation.digest,
            publications=(),
            tasks=(),
            phase=PlanPhase.AUTHORITATIVE,
            now=NOW,
        )
        assert second.source_task_keys == (derived.key,)
        assert [item["sequence"] for item in ledger.manifest().data["plan_rounds"]] == [1, 2, 3]
        digest = _canonical_digest(dict(ledger.closure_content()))
        ledger.close_plan(expected_closure_digest=digest, now=NOW)
        assert not ledger.all_required_satisfied()


def test_schema_pragmas_and_generation_resume_are_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "refresh.sqlite3"
    census = _census()
    with Ledger.open(path) as ledger:
        ledger.create_or_resume(_generation(census), census)
        assert ledger.manifest().data["generation"]["state"] == "planning"
        ledger.commit_initial_round(
            [PlannedTask(inventory_tasks(census, {"scholar": "1"}, "2026-08")[0], expands_plan=True)],
            source_evidence_digest="a" * 64,
            now=NOW,
        )
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


def test_reconstruct_claimed_task_rejects_same_owner_stale_reclaimed_lease(tmp_path: Path) -> None:
    census = _census()
    with Ledger.open(tmp_path / "fence.sqlite3") as ledger:
        ledger.create_or_resume(_generation(census), census)
        task = inventory_tasks(census, {"scholar": "1"}, "2026-08")[0]
        ledger.commit_initial_round([PlannedTask(task, expands_plan=True)], source_evidence_digest="a" * 64, now=NOW)
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        old = ledger.claim_due("same-owner", NOW, timedelta(minutes=1))
        assert old is not None
        reclaimed = ledger.claim_due("same-owner", NOW + timedelta(minutes=2), timedelta(minutes=5))
        assert reclaimed is not None
        with pytest.raises(ValueError, match="fencing"):
            ledger.reconstruct_claimed_task(old, NOW + timedelta(minutes=3))
        assert ledger.reconstruct_claimed_task(reclaimed, NOW + timedelta(minutes=3)).key == task.key


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
        assert not ledger.all_required_satisfied()
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
            observation=ProviderObservation("crossref", "1", {"title": "Complete title", "year": 2026}),
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
                    observation=ProviderObservation("crossref", "1", {"title": "A durable response", "year": 2026}),
                )
            elif fault_name == "after_task_terminalization":
                ledger.finish_request(
                    request_claim.key,
                    "worker",
                    TaskDisposition.SUCCEEDED,
                    NOW,
                    observation=ProviderObservation("crossref", "1", {"title": "A durable response", "year": 2026}),
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
            assert result.normalized_response == {"title": "A durable response", "year": 2026}
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
        assert not ledger.all_required_satisfied()


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


def test_legacy_compatibility_cannot_enter_production_validation(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        ledger.seal_plan([], required_validations=(ValidationSpec("corpus"),))
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        with pytest.raises(ValueError, match="discovery"):
            ledger.transition_generation(GenerationState.RUNNING, GenerationState.VALIDATING, NOW)


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
            ledger.seal_plan(derived[:-1], required_validations=(), inventory_freshness_epoch="2026-08")


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


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"title": "A complete title"},
        {"title": "", "year": 2026},
        {"title": "unknown", "year": 2026},
        {"title": "A complete title", "year": []},
    ],
)
def test_success_requires_nonempty_nonplaceholder_values_for_every_requested_field(
    tmp_path: Path, response: dict[str, object]
) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        task = _task()
        ledger.plan_task(task)
        _seal_and_run(ledger, [task])
        claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
        assert claim
        request_claim = ledger.claim_request(claim.key, "worker", NOW, timedelta(minutes=1))
        assert request_claim
        with pytest.raises(ValueError, match="requested field"):
            ledger.finish_request(
                request_claim.key,
                "worker",
                TaskDisposition.SUCCEEDED,
                NOW,
                observation=ProviderObservation("crossref", "1", response),
            )


def test_confirmed_empty_rejects_nonempty_response(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        task = _task()
        ledger.plan_task(task)
        _seal_and_run(ledger, [task])
        claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
        assert claim
        request_claim = ledger.claim_request(claim.key, "worker", NOW, timedelta(minutes=1))
        assert request_claim
        with pytest.raises(ValueError, match="empty response"):
            ledger.finish_request(
                request_claim.key,
                "worker",
                TaskDisposition.CONFIRMED_EMPTY,
                NOW,
                observation=ProviderObservation("crossref", "1", {"title": "Unexpected"}, True),
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
        assert not ledger.all_required_satisfied()
        with pytest.raises(ValueError, match="dominance"):
            ledger.finish_task(
                claim.key,
                "worker",
                TaskDisposition.DOMINATED,
                NOW,
                evidence=DominanceEvidence(("0" * 64,), "rule", (), "1" * 64),
            )


def test_structural_closure_cannot_enter_validation_without_task5b_authority(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        ledger.commit_initial_round([], source_evidence_digest="a" * 64, now=NOW)
        ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, NOW)
        snapshot = _canonical_digest(dict(ledger.closure_content((ValidationSpec("corpus"),))))
        ledger.close_plan(
            expected_closure_digest=snapshot,
            required_validations=(ValidationSpec("corpus"),),
            now=NOW,
        )
        assert ledger.plan_status().closed
        assert not ledger.plan_status().discovery_closed
        with pytest.raises(ValueError, match="discovery"):
            ledger.transition_generation(GenerationState.RUNNING, GenerationState.VALIDATING, NOW)


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
        manifest = ledger.manifest().data
        typed_publication = next(item for item in manifest["publications"] if item["publication_key"] == "pub-typed")
        assert typed_publication["normalized_title"] == "typed title"
        assert manifest["provider_state"][0]["current_concurrency"] == 2


def test_claims_require_sealed_running_generation(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        task = _task()
        ledger.plan_task(task)
        assert ledger.claim_due("worker", NOW, timedelta(minutes=1)) is None
        ledger.seal_plan([task], required_validations=())
        assert ledger.plan_status().authority_mode == "legacy_compatibility"
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


def test_seal_rejects_forged_inventory_request_identity(tmp_path: Path) -> None:
    census = _census(enabled=True)
    spec = _generation(census)
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        ledger.create_or_resume(spec, census)
        forged = TaskSpec(
            "author-ada",
            None,
            "scholar",
            "inventory",
            RequestSpec(
                "scholar",
                "inventory",
                "POST",
                {"profile_id": "WrongProfile"},
                ("title",),
                "wrong-adapter",
                "stale",
                "wrong-quota",
            ),
        )
        ledger.plan_task(forged)
        with pytest.raises(ValueError, match="canonical inventory"):
            ledger.seal_plan([forged], inventory_freshness_epoch="2026-08")


def test_seal_requires_request_backed_applicable_inventory(tmp_path: Path) -> None:
    census = _census(enabled=True)
    spec = _generation(census)
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        ledger.create_or_resume(spec, census)
        requestless = TaskSpec(
            "author-ada",
            None,
            "scholar",
            "inventory",
            None,
            applicability="not_applicable",
        )
        ledger.plan_task(requestless)
        with pytest.raises(ValueError, match="canonical inventory"):
            ledger.seal_plan([requestless], inventory_freshness_epoch="2026-08")


def test_dominance_requires_succeeded_allowed_provider_and_exact_fields(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        stronger = _task("stronger")
        dominated = _task("dominated", request=_request(query="doi:10.1/dominated"))
        ledger.plan_task(stronger)
        ledger.plan_task(dominated)
        _seal_and_run(ledger, [stronger, dominated])
        strong_claim = ledger.claim_due("strong", NOW, timedelta(minutes=1))
        assert strong_claim
        strong_request = ledger.claim_request(strong_claim.key, "strong", NOW, timedelta(minutes=1))
        assert strong_request
        ledger.finish_request(
            strong_request.key,
            "strong",
            TaskDisposition.CONFIRMED_EMPTY,
            NOW,
            observation=ProviderObservation("crossref", "1", {}, True),
        )
        dominated_claim = ledger.claim_due("weak", NOW, timedelta(minutes=1))
        assert dominated_claim
        with pytest.raises(ValueError, match="succeeded observation"):
            ledger.finish_task(
                dominated_claim.key,
                "weak",
                TaskDisposition.DOMINATED,
                NOW,
                evidence=DominanceEvidence(
                    (strong_request.key,),
                    DominanceRule.PUBLISHED_OVER_PREPRINT,
                    ("title", "year"),
                    strong_request.key,
                ),
            )


def test_dominance_rejects_wrong_fields_and_provider_for_rule(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        openalex_request = RequestSpec(
            "openalex", "lookup", "GET", {"query": "paper"}, ("title", "year"), "1", "epoch", "openalex"
        )
        stronger = TaskSpec("author-ada", "pub-strong", "openalex", "lookup", openalex_request)
        weak_request = RequestSpec(
            "scholar_min", "lookup", "GET", {"query": "paper"}, ("title", "year"), "1", "epoch", "scholar"
        )
        dominated = TaskSpec("author-ada", "pub-dominated", "scholar_min", "lookup", weak_request)
        ledger.plan_task(stronger)
        ledger.plan_task(dominated)
        _seal_and_run(ledger, [stronger, dominated])
        strong_claim = ledger.claim_due("strong", NOW, timedelta(minutes=1))
        assert strong_claim
        strong_request = ledger.claim_request(strong_claim.key, "strong", NOW, timedelta(minutes=1))
        assert strong_request
        ledger.finish_request(
            strong_request.key,
            "strong",
            TaskDisposition.SUCCEEDED,
            NOW,
            observation=ProviderObservation("openalex", "1", {"title": "Strong", "year": 2026}),
        )
        dominated_claim = ledger.claim_due("weak", NOW, timedelta(minutes=1))
        assert dominated_claim
        dominated_request = ledger.claim_request(dominated_claim.key, "weak", NOW, timedelta(minutes=1))
        assert dominated_request
        ledger.finish_request(
            dominated_request.key,
            "weak",
            TaskDisposition.SUCCEEDED,
            NOW,
            observation=ProviderObservation("scholar_min", "1", {"title": "Weak title", "year": 2025}),
        )
        with pytest.raises(ValueError, match="covered fields"):
            ledger.finish_task(
                dominated_claim.key,
                "weak",
                TaskDisposition.DOMINATED,
                NOW,
                evidence=DominanceEvidence(
                    (strong_request.key,), DominanceRule.AUTHORITATIVE_METADATA, ("title",), dominated_request.key
                ),
            )
        ledger.finish_task(
            dominated_claim.key,
            "weak",
            TaskDisposition.DOMINATED,
            NOW,
            evidence=DominanceEvidence(
                (strong_request.key,),
                DominanceRule.AUTHORITATIVE_METADATA,
                ("title", "year"),
                dominated_request.key,
            ),
        )


def test_dominance_rejects_unprovable_scholar_observation(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        scholar_request = RequestSpec(
            "scholar", "lookup", "GET", {"query": "paper"}, ("title", "year"), "1", "epoch", "scholar"
        )
        stronger = TaskSpec("author-ada", "pub-strong", "scholar", "lookup", scholar_request)
        dominated = _task("dominated", request=_request(query="doi:10.1/dominated"))
        ledger.plan_task(stronger)
        ledger.plan_task(dominated)
        _seal_and_run(ledger, [stronger, dominated])
        strong_claim = ledger.claim_due("strong", NOW, timedelta(minutes=1))
        assert strong_claim
        strong_request = ledger.claim_request(strong_claim.key, "strong", NOW, timedelta(minutes=1))
        assert strong_request
        ledger.finish_request(
            strong_request.key,
            "strong",
            TaskDisposition.SUCCEEDED,
            NOW,
            observation=ProviderObservation("scholar", "1", {"title": "Strong", "year": 2026}),
        )
        dominated_claim = ledger.claim_due("weak", NOW, timedelta(minutes=1))
        assert dominated_claim
        with pytest.raises(ValueError, match="task request"):
            ledger.finish_task(
                dominated_claim.key,
                "weak",
                TaskDisposition.DOMINATED,
                NOW,
                evidence=DominanceEvidence(
                    (strong_request.key,),
                    DominanceRule.AUTHORITATIVE_METADATA,
                    ("title", "year"),
                    strong_request.key,
                ),
            )


@pytest.mark.parametrize(
    ("field_name", "stronger_provider", "lower_value", "stronger_value"),
    [
        ("title", "arxiv", "A substantially longer incumbent paper title", "Short title"),
        ("pages", "crossref", "1-10", "2025.11.07.685935"),
        ("booktitle", "crossref", "Specific Systems Conference", "Lecture Notes in Computer Science"),
    ],
)
def test_dominance_rejects_values_blocked_by_live_merge_guards(
    tmp_path: Path, field_name: str, stronger_provider: str, lower_value: str, stronger_value: str
) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        stronger_request = RequestSpec(
            stronger_provider,
            "lookup",
            "GET",
            {"query": "stronger"},
            (field_name,),
            "1",
            "epoch",
            stronger_provider,
        )
        lower_request = RequestSpec(
            "scholar_min",
            "lookup",
            "GET",
            {"query": "lower"},
            (field_name,),
            "1",
            "epoch",
            "scholar",
        )
        stronger = TaskSpec("author-ada", "pub-strong", stronger_provider, "lookup", stronger_request)
        dominated = TaskSpec("author-ada", "pub-lower", "scholar_min", "lookup", lower_request)
        ledger.plan_task(stronger)
        ledger.plan_task(dominated)
        _seal_and_run(ledger, [stronger, dominated])
        strong_key, lower_key, claim_key, owner = _record_dominance_observations(
            ledger, stronger, {field_name: stronger_value}, dominated, {field_name: lower_value}
        )
        with pytest.raises(ValueError, match="live merge policy"):
            ledger.finish_task(
                claim_key,
                owner,
                TaskDisposition.DOMINATED,
                NOW,
                evidence=DominanceEvidence(
                    (strong_key,), DominanceRule.AUTHORITATIVE_METADATA, (field_name,), lower_key
                ),
            )


def test_published_over_preprint_requires_actual_values_and_live_merge_selection(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        fields = ("doi", "journal")
        stronger_request = RequestSpec(
            "crossref", "lookup", "GET", {"query": "published"}, fields, "1", "epoch", "public"
        )
        lower_request = RequestSpec(
            "scholar_min", "lookup", "GET", {"query": "preprint"}, fields, "1", "epoch", "scholar"
        )
        stronger = TaskSpec("author-ada", "pub-published", "crossref", "lookup", stronger_request)
        dominated = TaskSpec("author-ada", "pub-preprint", "scholar_min", "lookup", lower_request)
        ledger.plan_task(stronger)
        ledger.plan_task(dominated)
        _seal_and_run(ledger, [stronger, dominated])
        strong_key, lower_key, claim_key, owner = _record_dominance_observations(
            ledger,
            stronger,
            {"doi": "10.1000/published", "journal": "Journal of Sound Results"},
            dominated,
            {"doi": "10.48550/arxiv.2601.12345", "journal": "arXiv e-prints"},
        )
        ledger.finish_task(
            claim_key,
            owner,
            TaskDisposition.DOMINATED,
            NOW,
            evidence=DominanceEvidence((strong_key,), DominanceRule.PUBLISHED_OVER_PREPRINT, fields, lower_key),
        )
        manifest = ledger.manifest().data
        task_states = {item["task_key"]: item["state"] for item in manifest["tasks"]}
        assert task_states[dominated.key] == "dominated"
        assert manifest["dominance_evidence"][0]["dominated_observation_key"] == lower_key


@pytest.mark.parametrize("foreign_publication", ["pub-other", "pub-target"])
def test_dominance_rejects_same_provider_observation_from_another_logical_request(
    tmp_path: Path, foreign_publication: str
) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        fields = ("doi", "journal")
        stronger_request = RequestSpec(
            "crossref", "lookup", "GET", {"query": "published"}, fields, "1", "epoch", "public"
        )
        target_request = RequestSpec(
            "scholar_min", "lookup", "GET", {"query": "target"}, fields, "1", "epoch", "scholar"
        )
        foreign_request = RequestSpec(
            "scholar_min", "lookup", "GET", {"query": "foreign"}, fields, "1", "epoch", "scholar"
        )
        stronger = TaskSpec("author-ada", "pub-published", "crossref", "lookup", stronger_request)
        target = TaskSpec("author-ada", "pub-target", "scholar_min", "lookup", target_request)
        foreign = TaskSpec("author-ada", foreign_publication, "scholar_min", "lookup", foreign_request)
        tasks = [stronger, target, foreign]
        for task in tasks:
            ledger.plan_task(task)
        _seal_and_run(ledger, tasks)
        by_key = {task.key: task for task in tasks}
        held_target = None
        observations = {}
        for index in range(3):
            owner = f"worker-{index}"
            claim = ledger.claim_due(owner, NOW, timedelta(minutes=1))
            assert claim
            task = by_key[claim.key]
            if task is target:
                held_target = (claim, owner)
                continue
            request_claim = ledger.claim_request(claim.key, owner, NOW, timedelta(minutes=1))
            assert request_claim
            response = (
                {"doi": "10.1000/published", "journal": "Journal of Sound Results"}
                if task is stronger
                else {"doi": "10.48550/arxiv.2601.12345", "journal": "arXiv e-prints"}
            )
            ledger.finish_request(
                request_claim.key,
                owner,
                TaskDisposition.SUCCEEDED,
                NOW,
                observation=ProviderObservation(task.provider, "1", response),
            )
            ledger.finish_task(claim.key, owner, TaskDisposition.SUCCEEDED, NOW)
            observations[task.key] = request_claim.key
        assert held_target
        target_claim, target_owner = held_target
        with pytest.raises(ValueError, match="task request"):
            ledger.finish_task(
                target_claim.key,
                target_owner,
                TaskDisposition.DOMINATED,
                NOW,
                evidence=DominanceEvidence(
                    (observations[stronger.key],),
                    DominanceRule.PUBLISHED_OVER_PREPRINT,
                    fields,
                    observations[foreign.key],
                ),
            )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE tasks SET provider = 'dblp'",
        "UPDATE tasks SET request_key = NULL",
        "DELETE FROM tasks",
        "UPDATE requests SET identity_json = '{}'",
        "UPDATE requests SET request_key = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
        "DELETE FROM requests",
    ],
)
def test_sealed_task_and_request_identity_mutation_is_blocked(tmp_path: Path, statement: str) -> None:
    path = tmp_path / "ledger.db"
    with _open_ready(path) as ledger:
        task = _task()
        ledger.plan_task(task)
        ledger.seal_plan([task])
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        connection.execute(statement)
    connection.close()


def test_field_provenance_requires_succeeded_matching_observation(tmp_path: Path) -> None:
    with _open_ready(tmp_path / "ledger.db") as ledger:
        publication = PublicationMetadata(
            "author-ada", "pub-provenance", "scholar", "title", 2026, {}, "output/p.bib", "monthly"
        )
        ledger.record_publication_metadata(publication)
        task = _task("provenance")
        ledger.plan_task(task)
        with pytest.raises(ValueError, match="succeeded observation"):
            ledger.record_field_provenance(
                "author-ada",
                "pub-provenance",
                "title",
                "a" * 64,
                "crossref",
                task.request.key,
                ProvenanceRule.TRUST_POLICY,
            )
        _seal_and_run(ledger, [task])
        claim = ledger.claim_due("worker", NOW, timedelta(minutes=1))
        assert claim
        request_claim = ledger.claim_request(claim.key, "worker", NOW, timedelta(minutes=1))
        assert request_claim
        ledger.finish_request(
            request_claim.key,
            "worker",
            TaskDisposition.SUCCEEDED,
            NOW,
            observation=ProviderObservation("crossref", "1", {"title": "title", "year": 2026}),
        )
        ledger.record_field_provenance(
            "author-ada",
            "pub-provenance",
            "title",
            "a" * 64,
            "crossref",
            task.request.key,
            ProvenanceRule.TRUST_POLICY,
        )
        assert ledger.manifest().data["field_provenance"][0]["provider"] == "crossref"
