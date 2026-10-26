"""Bounded durable author-inventory execution."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from citeforge.clients.search_apis import OpenReviewRuntimeSession, OpenReviewSessionBroker
from citeforge.log_utils import LogCategory, logger

from .checkpoint import CheckpointError, CheckpointStore
from .discovery import (
    DiscoveryCredentials,
    DiscoveryPolicy,
    build_claimed_discovery_operation,
    resolve_discovery_authority,
)
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
from .types import GenerationSpec, GenerationState, RunResult, RunStatus, TaskDisposition

_DISCOVERY_BLOCKING = frozenset(
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


def _manifest_rows(value: object, name: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"manifest {name} is malformed")
    return value


class RefreshEngine:
    """Create a durable inventory plan without granting discovery authority."""

    def __init__(
        self,
        ledger: Ledger,
        policy: InventoryPolicy,
        transport: ProviderTransport | None = None,
        openreview_broker: OpenReviewSessionBroker | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        if checkpoint_store is not None and checkpoint_store.root.resolve().is_relative_to(
            ledger.path.parent.resolve()
        ):
            # The store would otherwise seal its own previous sequences into
            # every new one, growing each checkpoint by the size of the last.
            raise ValueError("checkpoint store root must live outside the sealed state directory")
        self._ledger = ledger
        self._policy = policy
        self._transport = transport
        self._checkpoint_store = checkpoint_store
        self._owner = f"inventory-{secrets.token_hex(12)}"
        self._discovery_owner = f"discovery-{secrets.token_hex(12)}"
        self._openreview_broker = openreview_broker or OpenReviewSessionBroker()

    def run(
        self,
        spec: GenerationSpec,
        credentials: RefreshCredentials,
        stop_requested: Callable[[], bool],
        *,
        discovery_policy: DiscoveryPolicy | None = None,
        discovery_credentials: DiscoveryCredentials | None = None,
    ) -> RunResult:
        run_started_at = datetime.now(timezone.utc)
        self._ledger.create_or_resume(spec, spec.census)
        try:
            capabilities = self._preflight(spec, credentials)
            if self._policy.min_year > run_started_at.year:
                raise ValueError("inventory minimum year exceeds the code-owned refresh year")
            if (discovery_policy is None) != (discovery_credentials is None):
                raise ValueError("discovery policy and credentials must be supplied together")
            if discovery_policy is None and set(spec.adapter_versions) >= {
                "arxiv",
                "crossref",
                "doi_bibtex",
                "doi_csl",
                "europepmc",
                "gemini",
                "openalex",
                "openreview",
                "pubmed",
                "s2",
                "serply",
            }:
                try:
                    self._ledger.load_discovery_authority()
                except ValueError as exc:
                    raise ValueError("full discovery generations require preflight before inventory planning") from exc
            if discovery_policy is not None and discovery_credentials is not None:
                if discovery_policy.freshness_epoch != run_started_at.strftime("%Y-%m"):
                    raise ValueError("discovery freshness does not match the code-owned inventory epoch")
                if discovery_policy.max_scholar_pages != self._policy.max_scholar_pages:
                    raise ValueError("discovery Scholar bound does not match inventory policy")
                if self._ledger.generation_state() is GenerationState.BLOCKED:
                    try:
                        bound_authority = self._ledger.load_discovery_authority()
                    except ValueError:
                        bound_authority = None
                    if bound_authority is not None and bound_authority != resolve_discovery_authority(
                        discovery_policy, discovery_credentials
                    ):
                        raise ValueError("discovery wave policy does not match bound authority")
                    return RunResult(
                        RunStatus.BLOCKED,
                        spec.id,
                        detail="inventory generation remains durably blocked",
                    )
                self._ledger.bind_discovery_policy(discovery_policy, discovery_credentials)
        except ValueError as exc:
            return RunResult(RunStatus.INVALID_CONFIGURATION, spec.id, detail=str(exc))

        status = self._ledger.plan_status()
        authority = self._authority_content(spec, capabilities)
        digest = hashlib.sha256(json.dumps(authority, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        if status.revision == 0:
            epoch = run_started_at.strftime("%Y-%m")
            tasks = []
            for row in spec.census.enabled_rows:
                for source in ("scholar", "dblp"):
                    if not getattr(row, f"{source}_id"):
                        continue
                    capability = capabilities[source]
                    tasks.append(
                        PlannedTask(build_inventory_task(row, capability, epoch, self._policy), expands_plan=True)
                    )
            self._ledger.commit_initial_round(
                tasks,
                source_evidence_digest=digest,
                now=run_started_at,
                inventory_authority=authority,
            )
            self._ledger.transition_generation(GenerationState.PLANNING, GenerationState.RUNNING, run_started_at)
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
                # Bounded lease stop, not a drain. The check sits after the
                # emptiness test and before claim_due so a stop takes no new
                # lease, and there is nothing left to await once it fires:
                # transport.send below is synchronous and the engine keeps no
                # in-flight set. Moving this past claim_due would lease work the
                # segment has no time to send, parking it until the lease expires.
                if stop_requested():
                    break
                claim = self._ledger.claim_due(self._owner, datetime.now(timezone.utc), timedelta(minutes=5))
                if claim is None:
                    break
                try:
                    operation = build_claimed_inventory_operation(
                        self._ledger,
                        claim,
                        credentials,
                        self._policy,
                        now=datetime.now(timezone.utc),
                    )
                except ValueError as exc:
                    state = self._ledger.generation_state()
                    if state in {GenerationState.RUNNING, GenerationState.WAITING}:
                        self._ledger.transition_generation(
                            state,
                            GenerationState.BLOCKED,
                            datetime.now(timezone.utc),
                            blocking_reason="claimed inventory failed durable authority validation",
                        )
                    self._save_checkpoint(spec)
                    return RunResult(RunStatus.BLOCKED, spec.id, completed_tasks=completed, detail=str(exc))
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
                self._save_checkpoint(spec)
                return RunResult(RunStatus.BLOCKED, spec.id, completed_tasks=completed, detail=str(exc))

        self._save_checkpoint(spec)
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

    def run_discovery(
        self,
        spec: GenerationSpec,
        policy: DiscoveryPolicy,
        credentials: DiscoveryCredentials,
        stop_requested: Callable[[], bool],
    ) -> RunResult:
        """Advance only the earliest incomplete C4 phase with exact scoped claims."""
        try:
            generation = self._ledger.manifest().data.get("generation")
            if not isinstance(generation, dict) or generation.get("generation_id") != spec.id:
                raise ValueError("discovery generation identity does not match the supplied specification")
            self._ledger.create_or_resume(spec, spec.census)
            if self._ledger.generation_state() is GenerationState.BLOCKED:
                try:
                    authority = self._ledger.load_discovery_authority()
                except ValueError:
                    authority = None
                if authority is not None and authority.policy != policy:
                    raise ValueError("discovery wave policy does not match bound authority")
                return RunResult(
                    RunStatus.BLOCKED,
                    spec.id,
                    detail="discovery generation remains durably blocked",
                )
            self._ledger.assert_c3_discovery_ready()
            self._ledger.bind_discovery_policy(policy, credentials)
            authority = self._ledger.load_discovery_authority()
        except (TypeError, ValueError) as exc:
            return RunResult(RunStatus.INVALID_CONFIGURATION, spec.id, detail=str(exc))

        completed = 0
        for pass_id in (
            "known_doi",
            "broad_discovery",
            "dynamic_expansion",
            "venue_fallback",
            "late_identifiers",
            "html_probe",
        ):
            non_openreview: deque[str] = deque()
            openreview: deque[str] = deque()
            while True:
                now = datetime.now(timezone.utc)
                status = (
                    "pending"
                    if non_openreview or openreview
                    else self._ledger.discovery_phase_status(pass_id, now=now)
                )
                if status == "uncommitted":
                    try:
                        if pass_id == "venue_fallback":  # noqa: S105
                            self._ledger.execute_and_commit_venue_fallback(policy, now=now)
                        elif pass_id == "late_identifiers":  # noqa: S105
                            self._ledger.execute_and_commit_late_identifiers(policy, now=now)
                        elif pass_id == "html_probe":  # noqa: S105
                            self._ledger.execute_and_commit_html_probe(policy, now=now)
                        else:
                            self._ledger.execute_and_commit_discovery_wave(pass_id, policy, now=now)
                    except ValueError as exc:
                        return self._block_discovery(spec.id, completed, str(exc), now, spec)
                    continue
                if status == "blocking":
                    return self._block_discovery(
                        spec.id,
                        completed,
                        f"{pass_id} has durable blocking evidence",
                        now,
                        spec,
                    )
                if status == "complete":
                    break

                if not non_openreview and not openreview:
                    due = self._ledger.discovery_wave_due_tasks(pass_id, now=now)
                    non_openreview.extend(key for key, provider in due.items() if provider != "openreview")
                    openreview.extend(key for key, provider in due.items() if provider == "openreview")
                if not non_openreview and not openreview and pass_id in {"known_doi", "venue_fallback"}:
                    try:
                        if pass_id == "venue_fallback":  # noqa: S105
                            self._ledger.execute_and_commit_venue_fallback(policy, now=now)
                        else:
                            self._ledger.execute_and_commit_discovery_wave(pass_id, policy, now=now)
                    except ValueError as exc:
                        return self._block_discovery(spec.id, completed, str(exc), now, spec)
                    refreshed = datetime.now(timezone.utc)
                    if self._ledger.discovery_phase_status(pass_id, now=refreshed) != "pending":
                        continue
                    due = self._ledger.discovery_wave_due_tasks(pass_id, now=refreshed)
                    non_openreview.extend(key for key, provider in due.items() if provider != "openreview")
                    openreview.extend(key for key, provider in due.items() if provider == "openreview")
                    now = refreshed
                if not non_openreview and not openreview and pass_id == "html_probe":  # noqa: S105
                    if stop_requested():
                        return RunResult(
                            RunStatus.CONTINUATION,
                            spec.id,
                            completed_tasks=completed,
                            detail="HTML probe work remains pending",
                        )
                    try:
                        self._ledger.execute_and_commit_html_probe(policy, now=now)
                    except ValueError as exc:
                        return self._block_discovery(spec.id, completed, str(exc), now, spec)
                    refreshed = datetime.now(timezone.utc)
                    if (
                        self._ledger.discovery_phase_status(pass_id, now=refreshed) == "pending"
                        and not self._ledger.discovery_wave_due_tasks(pass_id, now=refreshed)
                    ):
                        return RunResult(
                            RunStatus.CONTINUATION,
                            spec.id,
                            completed_tasks=completed,
                            detail="HTML probe work remains pending",
                        )
                    continue
                if (not non_openreview and not openreview) or stop_requested() or self._transport is None:
                    return RunResult(
                        RunStatus.CONTINUATION,
                        spec.id,
                        completed_tasks=completed,
                        remaining_tasks=len(non_openreview) + len(openreview),
                        detail="discovery work remains pending",
                    )

                openreview_session: OpenReviewRuntimeSession | None = None
                if non_openreview:
                    claim_key = non_openreview.popleft()
                    is_openreview = False
                else:
                    claim_key = openreview.popleft()
                    is_openreview = True
                if policy.openreview_mode == "authenticated" and is_openreview:
                    identity = (credentials.openreview_username, credentials.openreview_password)
                    if not all(isinstance(value, str) for value in identity):
                        return self._block_discovery(
                            spec.id,
                            completed,
                            "authenticated OpenReview credentials are unavailable",
                            now,
                            spec,
                        )
                    try:
                        openreview_session = self._openreview_broker.acquire((str(identity[0]), str(identity[1])))
                    except ValueError as exc:
                        return self._block_discovery(spec.id, completed, str(exc), now, spec)
                    if openreview_session is None:
                        return self._block_discovery(
                            spec.id,
                            completed,
                            "authenticated OpenReview login failed",
                            now,
                            spec,
                        )

                claim = self._ledger.claim_due_for_operations(
                    self._discovery_owner,
                    now,
                    timedelta(minutes=5),
                    frozenset({claim_key}),
                )
                if claim is None:
                    continue
                try:
                    operation = build_claimed_discovery_operation(
                        self._ledger,
                        claim,
                        credentials,
                        authority,
                        now=datetime.now(timezone.utc),
                        openreview_session=openreview_session,
                    )
                except ValueError as exc:
                    return self._block_discovery(spec.id, completed, str(exc), datetime.now(timezone.utc), spec)
                response = self._transport.send(operation, task_claim=claim)
                completed += 1
                if getattr(response, "disposition", None) in _DISCOVERY_BLOCKING:
                    return self._block_discovery(
                        spec.id,
                        completed,
                        f"{pass_id} has durable blocking evidence",
                        datetime.now(timezone.utc),
                        spec,
                    )
                if getattr(response, "disposition", None) is TaskDisposition.LEASED:
                    eligible_status = self._ledger.discovery_phase_status(
                        pass_id, now=datetime.now(timezone.utc)
                    )
                    if eligible_status == "blocking":
                        return self._block_discovery(
                            spec.id,
                            completed,
                            f"{pass_id} has durable blocking evidence",
                            datetime.now(timezone.utc),
                            spec,
                        )

        return RunResult(
            RunStatus.CONTINUATION,
            spec.id,
            completed_tasks=completed,
            detail="bounded publication discovery waves are complete",
        )

    def _block_discovery(
        self, generation_id: str, completed: int, detail: str, now: datetime, spec: GenerationSpec | None = None
    ) -> RunResult:
        # Seal first. Discovery does real provider work, and a segment that
        # blocks after doing it would otherwise discard the evidence and make
        # the next segment re-fetch, which is the restart the ledger exists to
        # prevent.
        #
        # The seal cannot raise past the block, because a generation too broken
        # to read its own manifest must still report WHY it blocked rather than
        # dying with a second, less informative error. But it is reported, not
        # swallowed: a failed seal means the next segment silently redoes this
        # work, and an operator reading only the block reason would never learn
        # that. The detail carries it too, so the reason survives into the
        # RunResult the workflow parses, not just into a log nobody reads.
        if spec is not None:
            try:
                self._save_checkpoint(spec)
            except (CheckpointError, ValueError, OSError) as seal_error:
                logger.error(
                    f"CHECKPOINT_SEAL_FAILED | generation={generation_id} | completed={completed} "
                    f"| error={seal_error}",
                    category=LogCategory.ERROR,
                )
                detail = f"{detail} (checkpoint seal also failed: {seal_error}; the next segment will repeat this work)"
        state = self._ledger.generation_state()
        if state in {GenerationState.RUNNING, GenerationState.WAITING}:
            self._ledger.transition_generation(
                state,
                GenerationState.BLOCKED,
                now,
                blocking_reason="discovery execution rejected durable evidence",
            )
        return RunResult(RunStatus.BLOCKED, generation_id, completed_tasks=completed, detail=detail)

    def _save_checkpoint(self, spec: GenerationSpec) -> None:
        """Seal the durable state directory, then record the seal in the ledger.

        Order is load-bearing in both directions. The seal is taken first
        because no archive can contain the row that describes itself, so the
        restored ledger is always one checkpoint row behind the blob that
        carries it, and the sequence below is chosen to survive that gap. The
        ledger write is made from here rather than from
        :mod:`citeforge.refresh.checkpoint` because ``record_checkpoint`` opens
        ``BEGIN IMMEDIATE`` on the single connection the seal has just copied.

        Retention is asymmetric by design. ``CheckpointStore`` keeps the current
        and previous sequences on disk; the ``checkpoints`` table is append-only
        and keeps every one.
        """
        if self._checkpoint_store is None:
            return
        generation = self._ledger.manifest().data.get("generation")
        if not isinstance(generation, dict):
            raise ValueError("manifest generation is malformed")
        # Both sources are needed. The ledger sequence alone regresses after a
        # restore, because the restored ledger predates the row describing its
        # own seal, and a regressed sequence overwrites the very blob the
        # segment resumed from. The store sequence alone cannot be trusted
        # either, since retention prunes it. Counting saves in memory is worse
        # than both: the primary key on (generation_id, sequence) turns a
        # replayed number into a sqlite3.IntegrityError, not a ValueError.
        retained = self._checkpoint_store.available_sequences()
        sequence = max([int(generation["checkpoint_sequence"]), *retained]) + 1
        created_at = datetime.now(timezone.utc)
        checkpoint = self._checkpoint_store.save(
            generation_id=spec.id,
            input_digest=str(generation["input_digest"]),
            policy_digest=str(generation["policy_digest"]),
            sequence=sequence,
            created_at=created_at,
            state_dir=self._ledger.path.parent,
        )
        self._ledger.record_checkpoint(sequence, checkpoint.ciphertext_digest, checkpoint.key_id, created_at)

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
        self._ledger.commit_inventory_union_wave(
            spec.census.enabled_rows,
            policy,
            reducer_version="1",
            now=datetime.now(timezone.utc),
        )

    def _inventory_work_open(self) -> bool:
        tasks = _manifest_rows(self._ledger.manifest().data.get("tasks"), "tasks")
        return any(
            item["operation"] == "inventory" and item["state"] not in {"succeeded", "confirmed_empty"} for item in tasks
        )

    def _authority_content(self, spec: GenerationSpec, capabilities: dict[str, AdapterCapability]) -> dict[str, object]:
        return {
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
