"""Bounded durable author-inventory execution."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from .inventory import (
    AdapterCapability,
    InventoryPolicy,
    RefreshCredentials,
    SnapshotContribution,
    build_claimed_inventory_operation,
    build_inventory_task,
    capability_for,
    plan_scholar_page_wave,
)
from .ledger import Ledger, PlannedTask
from .transport import ProviderTransport
from .types import GenerationSpec, GenerationState, RunResult, RunStatus


def _manifest_rows(value: object, name: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"manifest {name} is malformed")
    return value


class RefreshEngine:
    """Create a durable inventory plan without granting discovery authority."""

    def __init__(self, ledger: Ledger, policy: InventoryPolicy, transport: ProviderTransport | None = None) -> None:
        self._ledger = ledger
        self._policy = policy
        self._transport = transport
        self._owner = f"inventory-{secrets.token_hex(12)}"

    def run(
        self,
        spec: GenerationSpec,
        credentials: RefreshCredentials,
        stop_requested: Callable[[], bool],
    ) -> RunResult:
        self._ledger.create_or_resume(spec, spec.census)
        try:
            capabilities = self._preflight(spec, credentials)
        except ValueError as exc:
            return RunResult(RunStatus.INVALID_CONFIGURATION, spec.id, detail=str(exc))

        status = self._ledger.plan_status()
        digest = self._authority_digest(spec, capabilities)
        if status.revision == 0:
            epoch = datetime.now(timezone.utc).strftime("%Y-%m")
            tasks = []
            for row in spec.census.enabled_rows:
                for source in ("scholar", "dblp"):
                    if not getattr(row, f"{source}_id"):
                        continue
                    capability = capabilities[source]
                    tasks.append(
                        PlannedTask(build_inventory_task(row, capability, epoch, self._policy), expands_plan=True)
                    )
            self._ledger.commit_initial_round(tasks, source_evidence_digest=digest, now=datetime.now(timezone.utc))
            self._ledger.transition_generation(
                GenerationState.PLANNING, GenerationState.RUNNING, datetime.now(timezone.utc)
            )
        else:
            try:
                self._ledger.assert_initial_inventory_authority(digest)
            except ValueError as exc:
                return RunResult(RunStatus.INVALID_CONFIGURATION, spec.id, detail=str(exc))
        if self._ledger.generation_state() is GenerationState.BLOCKED:
            return RunResult(RunStatus.BLOCKED, spec.id, detail="inventory generation remains durably blocked")

        completed = 0
        if self._transport is not None:
            while True:
                manifest = self._ledger.manifest().data
                inventory_open = [
                    item
                    for item in _manifest_rows(manifest.get("tasks"), "tasks")
                    if item["operation"] == "inventory" and item["state"] in {"pending", "retry_wait", "leased"}
                ]
                if not inventory_open:
                    break
                if stop_requested():
                    break
                claim = self._ledger.claim_due(self._owner, datetime.now(timezone.utc), timedelta(minutes=5))
                if claim is None:
                    break
                operation = build_claimed_inventory_operation(
                    self._ledger,
                    claim,
                    credentials,
                    self._policy,
                    now=datetime.now(timezone.utc),
                )
                self._transport.send(operation, task_claim=claim)
                completed += 1
            try:
                self._commit_pending_page_wave(spec)
                if not self._inventory_work_open():
                    self._commit_ready_unions(spec)
            except ValueError as exc:
                self._ledger.transition_generation(
                    GenerationState.RUNNING,
                    GenerationState.BLOCKED,
                    datetime.now(timezone.utc),
                    blocking_reason="inventory planning or reduction rejected durable evidence",
                )
                return RunResult(RunStatus.BLOCKED, spec.id, completed_tasks=completed, detail=str(exc))

        manifest = self._ledger.manifest().data
        task_rows = manifest["tasks"]
        blocking_states = {
            "malformed",
            "authentication_failed",
            "schema_changed",
            "permanent_failure",
            "circuit_open",
            "ambiguous",
            "blocked",
            "unknown",
        }
        if isinstance(task_rows, list) and any(
            item["operation"] == "inventory" and item["state"] in blocking_states for item in task_rows
        ):
            if self._ledger.generation_state() in {GenerationState.RUNNING, GenerationState.WAITING}:
                self._ledger.transition_generation(
                    self._ledger.generation_state(),
                    GenerationState.BLOCKED,
                    datetime.now(timezone.utc),
                    blocking_reason="author inventory has durable blocking evidence",
                )
            return RunResult(
                RunStatus.BLOCKED,
                spec.id,
                completed_tasks=completed,
                detail="an author inventory has durable blocking evidence",
            )
        remaining = (
            sum(item["state"] not in {"succeeded", "confirmed_empty"} for item in task_rows)
            if isinstance(task_rows, list)
            else 0
        )
        if stop_requested():
            return RunResult(RunStatus.CONTINUATION, spec.id, completed_tasks=completed, remaining_tasks=remaining)
        return RunResult(
            RunStatus.CONTINUATION,
            spec.id,
            completed_tasks=completed,
            remaining_tasks=remaining,
            detail="inventory execution requires a configured durable transport",
        )

    def _commit_pending_page_wave(self, spec: GenerationSpec) -> None:
        raw = self._ledger.load_pending_scholar_wave()
        if not raw:
            return
        pages = {
            author_key: tuple(item for item in values if isinstance(item, SnapshotContribution))
            for author_key, values in raw.items()
        }
        manifest = self._ledger.manifest().data
        generation = manifest.get("generation")
        if not isinstance(generation, dict):
            raise ValueError("manifest generation is malformed")
        epoch = generation.get("inventory_freshness_epoch")
        wave = plan_scholar_page_wave(
            {row.row_key: row for row in spec.census.enabled_rows},
            pages,
            str(epoch),
            spec.adapter_versions["scholar"],
            self._policy,
        )
        evidence = {item.task_key: item.observation_digest for values in pages.values() for item in values}
        digest = (
            evidence[wave.source_task_keys[0]]
            if len(wave.source_task_keys) == 1
            else hashlib.sha256(
                json.dumps([evidence[key] for key in wave.source_task_keys], separators=(",", ":")).encode()
            ).hexdigest()
        )
        self._ledger.commit_reduction(
            wave.source_task_keys,
            source_evidence_digest=digest,
            publications=(),
            tasks=tuple(PlannedTask(task, expands_plan=True) for task in wave.tasks),
            now=datetime.now(timezone.utc),
            reducer_id="scholar_page_wave",
            reducer_version="1",
        )

    def _commit_ready_unions(self, spec: GenerationSpec) -> None:
        manifest = self._ledger.manifest().data
        generation = manifest.get("generation")
        if not isinstance(generation, dict) or not isinstance(generation.get("inventory_freshness_epoch"), str):
            raise ValueError("inventory freshness authority is missing")
        policy = InventoryPolicy(
            self._policy.min_year,
            self._policy.max_publications,
            self._policy.max_scholar_pages,
            spec.adapter_versions["doi_csl"],
            spec.adapter_versions["s2"],
            generation["inventory_freshness_epoch"],
        )
        for row in spec.census.enabled_rows:
            self._ledger.commit_inventory_union(row, policy, reducer_version="1", now=datetime.now(timezone.utc))

    def _inventory_work_open(self) -> bool:
        tasks = _manifest_rows(self._ledger.manifest().data.get("tasks"), "tasks")
        return any(
            item["operation"] == "inventory" and item["state"] not in {"succeeded", "confirmed_empty"} for item in tasks
        )

    def _authority_digest(self, spec: GenerationSpec, capabilities: dict[str, AdapterCapability]) -> str:
        content = {
            "capabilities": [
                dict(item.canonical_content())
                for item in sorted(capabilities.values(), key=lambda value: value.capability_id)
            ],
            "generation": spec.id,
            "policy": {
                "max_publications": self._policy.max_publications,
                "max_scholar_pages": self._policy.max_scholar_pages,
                "min_year": self._policy.min_year,
                "seed_adapter_versions": {
                    "doi_csl": spec.adapter_versions["doi_csl"],
                    "s2": spec.adapter_versions["s2"],
                },
            },
            "planner_version": "1",
            "reducer_version": "1",
        }
        return hashlib.sha256(json.dumps(content, separators=(",", ":"), sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _preflight(spec: GenerationSpec, credentials: RefreshCredentials) -> dict[str, AdapterCapability]:
        capabilities: dict[str, AdapterCapability] = {}
        for source, operation in (("doi_csl", "csl_lookup"), ("s2", "fuzzy_search")):
            version = spec.adapter_versions.get(source)
            if version is None:
                raise ValueError(f"missing adapter version for {source}")
            capabilities[source] = capability_for(source, operation, version)
        for row in spec.census.enabled_rows:
            for source, identifier in (("scholar", row.scholar_id), ("dblp", row.dblp_id)):
                if not identifier:
                    continue
                version = spec.adapter_versions.get(source)
                if version is None:
                    raise ValueError(f"missing adapter version for {source}")
                capabilities[source] = capability_for(source, "inventory", version)
                if source == "scholar" and not credentials.serpapi_key:
                    raise ValueError("missing SerpAPI credential")
        return capabilities


__all__ = ["RefreshEngine"]
