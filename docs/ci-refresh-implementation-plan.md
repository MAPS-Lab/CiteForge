# Correctness-First CI and Monthly Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task by task. Every behavior change uses red-green-refactor. Each task receives an independent specification and quality review.

**Goal:** Replace CiteForge's unsafe whole-corpus convergence loop with one durable, resumable, completeness-gated refresh engine and an efficient artifact-reusing CI and PR publication system.

**Architecture:** A generation-scoped SQLite ledger is the sole work and completion authority. Existing Requests, Tenacity, thread pooling, merge policy, parser, serializer, and per-author identity remain subordinate execution mechanisms. Network work writes only to a generation staging tree. Authenticated checkpoints preserve current and previous state. Publication offers one bot PR only after the ledger and corpus validators prove completeness, then dispatches the website only after the exact candidate is merged.

**Tech stack:** Python 3.10 through 3.14, stdlib SQLite, Requests, Tenacity, `cryptography` AES-GCM, pytest, pytest-socket, Ruff, mypy, uv lock files, GitHub Actions.

## Global constraints

- Correctness and completeness are hard gates. Time and request counts are metrics only.
- Every input row and every required work item has an explicit durable disposition.
- Only `succeeded`, validated `confirmed_empty`, `not_applicable`, and proven `dominated` satisfy required work.
- Per-author identity remains authoritative. Only exact equivalent requests may be shared.
- The DOI and title-similarity threshold remains 0.55.
- Orphan deletion remains guarded by title similarity at or above 0.95.
- The established finalization order remains unchanged.
- Committed `output/`, `data/input.csv`, and `data/a2i2.csv` are protected during implementation and shadow validation.
- Existing Requests and thread execution remain unless equal-work profiling proves a replacement materially better.
- Batching is adopted only for a currently documented exact operation with replay-proven correlation and measured benefit.
- The old publisher becomes fail-closed before any shadow run.
- No legacy completion or publication fallback remains after cutover.
- No implementation artifacts are written under `docs/superpowers/` or tracked `.superpowers/`.

---

### Task 1: Contain unsafe publication and restore truthful gates

**Files:**
- Modify: `.github/workflows/monthly-refresh.yml`
- Modify: `citeforge/http_utils.py`
- Modify: `tests/test_http_utils.py`
- Modify: `requirements.lock`
- Modify: `requirements-dev.lock`
- Modify: `README.md`
- Modify: `citeforge/cache.py`
- Modify: `docs/library-substitution-audit.md`
- Test: `tests/test_workflow_contracts.py`

**Interfaces:**
- Produces a fail-closed legacy workflow that cannot push `main` or dispatch the website.
- Produces `decode_json_mapping(raw: bytes, url: str) -> dict[str, Any]` which rejects valid JSON of the wrong shape with `DecodeError`.

- [ ] Add a workflow contract test that loads monthly YAML and fails while any command pushes `HEAD:main`, while website dispatch is reachable from the legacy refresh, or while a false convergence value permits publication.
- [ ] Run the workflow test and verify the expected failure.
- [ ] Disable the schedule's publication and downstream dispatch while retaining manual shadow diagnostics. Add `concurrency.group: citeforge-refresh` and `cancel-in-progress: false`.
- [ ] Add wrong-shape JSON tests for list, null, string, and missing provider envelope boundaries. Verify red before implementation.
- [ ] Make the generic mapping decoder reject non-mappings. Keep provider-specific envelope checks in their adapters.
- [ ] Regenerate all three universal Python 3.10 hash locks and prove a second regeneration is byte-stable.
- [ ] Correct README, cache, and substitution-audit wording so entry completeness, response-cache freshness, materialization idempotence, and workflow completion are distinct.
- [ ] Run focused tests, lock freshness, Ruff, mypy, and `git diff --check`.
- [ ] Commit as `fix: fail closed before refresh redesign` and verify `git log -1`.

### Task 2: Add explicit input census and durable work types

**Files:**
- Create: `citeforge/refresh/__init__.py`
- Create: `citeforge/refresh/types.py`
- Create: `citeforge/refresh/census.py`
- Modify: `data/input.csv`
- Modify: `citeforge/io_utils.py`
- Modify: `pyproject.toml`
- Test: `tests/test_refresh_census.py`
- Test: `tests/test_data.py`

**Interfaces:**
- Produces `TaskDisposition`, `GenerationState`, `RunStatus`, `GenerationSpec`, and `RunResult`.
- Produces `load_census(path: Path) -> AuthorCensus` with one disposition for every physical CSV row.
- `GenerationSpec.id` is a deterministic SHA-256 digest of normalized census, policy, adapter versions, and base commit.

- [ ] Characterize all current input rows, including every row without Scholar and DBLP identifiers.
- [ ] Add failing tests for unclassified rows, empty names with identifiers, duplicate normalized identity, explicit exclusion, stable row keys, and deterministic generation identity.
- [ ] Add `Enabled` and `Exclusion Reason` columns to the source CSV. Migrate every current row explicitly without deleting a row.
- [ ] Implement immutable enums and dataclasses plus census validation.
- [ ] Make legacy `read_records()` consume enabled census rows without silently filtering physical input rows.
- [ ] Prove current enabled, excluded, and invalid counts exactly in `tests/test_data.py`.
- [ ] Run focused tests, protected input diff review, Ruff, mypy, and `git diff --check`.
- [ ] Commit as `feat: make author census explicit` and verify `git log -1`.

### Task 3: Implement the transactional generation ledger

**Files:**
- Create: `citeforge/refresh/ledger.py`
- Test: `tests/test_refresh_ledger.py`

**Interfaces:**
- Produces `Ledger.open(path: Path) -> Ledger`.
- Produces `create_or_resume(spec)`, `plan_task(task)`, `claim_due(owner, now, lease_for)`, `finish_task(...)`, `record_attempt(...)`, `claim_request(...)`, `finish_request(...)`, `all_required_satisfied()`, and `manifest()`.
- Uses rollback-journal mode, `synchronous=FULL`, foreign keys, explicit transactions, and conditional state transitions.

- [ ] Write failing schema tests for generation identity, every census disposition, task uniqueness, exact request uniqueness, multiple consumers, physical attempt history, and foreign-key enforcement.
- [ ] Write failing transition tests for one claimant, expired lease reclaim, retry deadline, stale owner rejection, terminal-state immutability, generation mismatch, and sole completeness predicate.
- [ ] Implement the smallest schema that preserves generation, census, work, exact request, consumer, attempt, checkpoint, and publication evidence.
- [ ] Use deterministic JSON and digests. Never persist credentials.
- [ ] Add named fault injection immediately after claim, attempt, response, terminalization, and manifest transitions.
- [ ] Run focused tests on Python 3.10 and current Python, Ruff, mypy, SQLite integrity check, and `git diff --check`.
- [ ] Commit as `feat: add durable refresh ledger` and verify `git log -1`.

### Task 4: Add classified provider transport and exact request coalescing

**Files:**
- Create: `citeforge/refresh/transport.py`
- Modify: `citeforge/http_utils.py`
- Modify: `citeforge/api_generics.py`
- Modify: `citeforge/clients/*.py`
- Modify: `citeforge/doi_utils.py`
- Test: `tests/test_refresh_transport.py`
- Test: `tests/test_api_adapter.py`
- Test: `tests/test_http_utils.py`

**Interfaces:**
- Produces `RequestSpec`, `ProviderResponse`, `ProviderTransport`, `LedgerTransport`, and `ScriptedTransport`.
- An exact request key includes provider, operation, method, normalized non-secret payload, requested fields, adapter version, freshness epoch, and quota scope when semantic.
- Provider adapters validate their own envelopes and return classified dispositions.

- [ ] Add failing tests for one physical call across simultaneous identical consumers, restart reuse, timeout, connection failure, transient 5xx, 429 with numeric and date `Retry-After`, authentication failure, permanent invalid request, malformed JSON, valid wrong-shape JSON, valid authoritative empty, and schema change.
- [ ] Add failing correlation tests for every adopted exact batch operation. Missing, duplicate, malformed, and unexpected members cannot close successfully.
- [ ] Instrument physical attempts inside the actual send boundary. Keep logical work counts separate.
- [ ] Implement persisted retry deadlines with bounded exponential backoff and jitter. POST retryability is operation-specific.
- [ ] Route every production provider adapter through the transport seam. Remove decorators or catches that collapse errors into empty values.
- [ ] Implement exact in-flight coalescing with ledger ownership. Fuzzy author and title searches remain author-scoped.
- [ ] Evaluate current documented batch candidates with replay fixtures. Adopt only those with equivalent behavior and measured request reduction. Record every rejected candidate as inapplicable.
- [ ] Run focused adapter and transport tests on Python 3.10 and 3.14, Ruff, mypy, and `git diff --check`.
- [ ] Commit as `feat: persist classified provider work` and verify `git log -1`.

### Task 5: Add phased discovery and resumable generation execution

**Files:**
- Create: `citeforge/refresh/engine.py`
- Modify: `citeforge/refresh/ledger.py`
- Modify: `citeforge/refresh/types.py`
- Modify: `citeforge/pipeline/scheduler.py`
- Modify: `citeforge/pipeline/article.py`
- Modify: `citeforge/cli.py`
- Modify: `main.py`
- Test: `tests/test_refresh_ledger.py`
- Test: `tests/test_refresh_engine.py`
- Test: `tests/test_scheduler_retry.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces `RefreshEngine.run(spec, credentials, stop_requested) -> RunResult`.
- CLI adds `refresh`, `--state-dir`, and `--generation` while preserving a compatibility invocation. Task 7 adds staging options.
- `RunResult` distinguishes ready-to-materialize, continuation, blocked, and invalid configuration. Task 5 never marks a generation complete.

- [ ] Replace the one-shot plan seal with a fixed forward-only phase machine backed by append-only immutable rounds. Exact requests remain generation-global and logical tasks remain author- and publication-scoped.
- [ ] Add an atomic reduction commit which rechecks an immutable snapshot digest, persists reduction evidence and publications, inserts and seals the complete next round, and is idempotent across crashes and resume.
- [ ] Add a final discovery-closure transaction. Inventory-only terminal work must never satisfy generation completeness. Closure proves every applicable inventory, union member, publication, policy-required operation, reduction input, round, and final zero-unseen-task planner pass.
- [ ] Separate logical evidence source from physical transport provider through typed adapter capabilities. Every planner-emittable operation must have exactly one durable adapter and reducer contract.
- [ ] Add a failing end-to-end fixture with two authors, shared exact work, complete and incomplete existing entries, successful and failing provider work, and one explicitly excluded input row.
- [ ] Prove an exhausted provider, malformed response, reducer exception, or failed author blocks readiness and yields nonzero status without losing successful tasks.
- [ ] Prove complete existing DOI records receive current-generation inventory and authoritative revalidation.
- [ ] Implement version-bound planner phases for census, applicable author inventories, per-author union/dedup, publication work, late identifiers, and authoritative revalidation. Expansion outside the fixed graph or policy bound blocks.
- [ ] Extract pure, socket-free inventory and publication planners and reducers from article processing. Reducers consume only immutable normalized observations and existing entries, and return typed publication sets or materialization intents without writing files.
- [ ] Convert scheduler execution to claim ledger tasks and persist classified outcomes. Failed futures are never counted as completed.
- [ ] Stop leasing on `stop_requested`, drain bounded in-flight work, close every shared consumer from durable request evidence, and resume without repeating success.
- [ ] Remove the old `(saved, processed)` completion authority and reverse tests that approved swallowed failure.
- [ ] Prove planners and reducers cannot open sockets or mutate committed output. Task 7 alone applies materialization intents to a stage and transitions through validation to complete.
- [ ] Run ledger, engine, scheduler, CLI, provider, and pipeline end-to-end tests on Python 3.10 and 3.14 plus the full hermetic suite, Ruff, mypy, and `git diff --check`.
- [ ] Commit as `feat: execute resumable refresh generations` and verify `git log -1`.

### Task 6: Add authenticated checkpoints and interrupted resume

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.lock`
- Modify: `requirements-dev.lock`
- Create: `citeforge/refresh/checkpoint.py`
- Test: `tests/test_refresh_checkpoint.py`
- Test: `tests/test_refresh_resume.py`
- Modify: `docs/library-substitution-audit.md`

**Interfaces:**
- Produces `CheckpointStore.save(...)`, `load_latest_valid(...)`, and `CheckpointManifest`.
- Uses `cryptography` AES-GCM with a random nonce, explicit associated data, key identifier, ciphertext SHA-256, and current plus previous immutable sequences.

- [ ] Verify the selected exact `cryptography` version's provenance, license, maintainers, release activity, advisories, transitives, Python 3.10 through 3.14 wheels, and AGPL compatibility. Record it in the dependency audit.
- [ ] Add failing checkpoint tests for round trip, wrong key, tampering, manifest mismatch, generation mismatch, newest corruption fallback, all invalid fail-closed, and no blank restart.
- [ ] Add failing resume tests after planning, request claim, response commit, task terminalization, stage manifest, checkpoint, and candidate record.
- [ ] Implement authenticated snapshots and retain two verified sequences.
- [ ] Prove resumed execution does not repeat a durable successful request and reclaims an expired lease once.
- [ ] Regenerate all locks twice and prove byte-stability.
- [ ] Run focused tests on Python 3.10 and 3.14, dependency audit, Ruff, mypy, and `git diff --check`.
- [ ] Commit as `feat: checkpoint refresh generations safely` and verify `git log -1`.

### Task 7: Stage, validate, and deterministically materialize output

**Files:**
- Create: `citeforge/refresh/staging.py`
- Modify: `citeforge/pipeline/postrun.py`
- Modify: `citeforge/io_utils.py`
- Test: `tests/test_refresh_staging.py`
- Test: `tests/test_finalize_run.py`
- Test: `tests/test_pipeline_e2e.py`

**Interfaces:**
- Produces `prepare_stage()`, `validate_stage()`, `materialize_candidate()`, and `CorpusManifest`.
- Finalization returns structured validation evidence and fails rather than silently skipping when summary setup fails.

- [ ] Add failing tests proving committed output remains unchanged during incomplete work and every blocking ledger state prevents materialization.
- [ ] Add failing tests for input census, author inventories, task closure, summary agreement, deletion evidence, contribution window, author preservation, identity guards, exact `a2i2`, baseline counts, and two byte-identical materializations.
- [ ] Move finalization outside the conditional summary-exists block while preserving the established order. Propagate write and validation failures.
- [ ] Build only inside a generation stage copied from committed output.
- [ ] Hash every staged file deterministically and bind the corpus manifest to the generation.
- [ ] Compare the full protected corpus against frozen legacy responses and review every intentional difference.
- [ ] Run staging, finalization, corpus, regression, and full hermetic tests plus Ruff, mypy, and `git diff --check`.
- [ ] Commit as `feat: validate staged refresh output` and verify `git log -1`.

### Task 8: Implement PR-only publication and merge-gated website dispatch

**Files:**
- Create: `citeforge/refresh/publication.py`
- Replace: `.github/workflows/monthly-refresh.yml`
- Create: `.github/workflows/citeforge-refresh-merged.yml`
- Test: `tests/test_refresh_publication.py`
- Test: `tests/test_workflow_contracts.py`

**Interfaces:**
- Produces `PublicationPort`, `RecordingPublicationPort`, and a subprocess-backed GitHub publisher.
- No interface can push to `main`.
- Website dispatch is idempotent by verified merge SHA.

- [ ] Add failing tests proving incomplete, unresolved, wrong-manifest, wrong-head, failed-CI, and unmerged states create no publication or dispatch intent.
- [ ] Add failing tests for one deterministic candidate branch, one PR, exact candidate SHA, verified merge SHA, and exactly one website dispatch across retries.
- [ ] Test the production publisher against temporary bare Git repositories and fake `gh` executables.
- [ ] Replace monthly YAML with one serialized generation segment, authenticated checkpoint restore/save, planned drain, continuation, candidate PR offer, and structured artifact upload.
- [ ] Add the merged-PR workflow with candidate and manifest verification before website dispatch.
- [ ] Delete the 50-pass loop, API-count convergence, workflow retrigger, AES-CBC monolith, data-cache force push, direct-main push, and pre-merge dispatch.
- [ ] Run workflow contract tests, actionlint, YAML parse, least-permission review, secret-output scan, Ruff, mypy, and `git diff --check`.
- [ ] Commit as `ci: publish complete refreshes through pull requests` and verify `git log -1`.

### Task 9: Rebuild CI around one immutable wheel

**Files:**
- Replace: `.github/workflows/tests.yml`
- Modify: `.pre-commit-config.yaml`
- Test: `tests/test_ci_artifact_contract.py`
- Test: `tests/test_workflow_contracts.py`

**Interfaces:**
- One package producer uploads a wheel and digest manifest.
- Python 3.10 through 3.14 consumers install the same verified wheel.
- One canonical job owns coverage.
- Required CI includes every package, quality, coverage, compatibility, dependency, security, and refresh-corpus gate.

- [ ] Add failing workflow tests for more than one wheel producer, editable consumer installs, repeated coverage owners, serialized independent matrix jobs, missing digest validation, stale locks, and aggregate-gate omissions.
- [ ] Implement package, quality, canonical coverage, parallel compatibility, and dependency/security jobs with lock-digest caching.
- [ ] Preserve the full hermetic test selection on all supported versions. Only coverage instrumentation is deduplicated.
- [ ] Upload structured failure and timing artifacts with bounded retention.
- [ ] Run workflow tests, actionlint, lock freshness, local artifact simulation, wheel smoke, full suite, Ruff, mypy, pre-commit, dependency and security scans.
- [ ] Compare modeled runner work with the measured historical baseline and record the normalized estimate.
- [ ] Commit as `ci: reuse one verified package artifact` and verify `git log -1`.

### Task 10: Run hosted shadow, cut over, and reconcile all evidence

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/library-substitution-audit.md`
- Create: `docs/ci-refresh-evidence.md`
- Modify: `/tmp/citeforge-reconciliation-ledger.md`

**Interfaces:**
- Produces the final requirement-to-evidence ledger, before and after measurements, and live-shadow record.

- [ ] Re-run all independent forward, reverse, code, integration, contradiction, and regression audits against the candidate SHA.
- [ ] Run every offline success, transient, 429, timeout, empty, malformed, partial, authentication, schema, lease, interruption, checkpoint, staging, publication, and CI artifact scenario.
- [ ] Run the complete local static, type, formatting, dependency, license, security, packaging, and full test suite on all supported Python versions.
- [ ] Prove protected corpus and input changes are exactly the intentional census migration and reviewed output differences.
- [ ] Push the candidate branch and run hosted Required CI.
- [ ] Run a publication-disabled monthly shadow with real providers and checkpoints. Verify current schemas, quotas, physical attempts, recovery, exact-request reuse, complete author census, zero unresolved work, staged checksum, and no publication mutation. Note that "complete author census, zero unresolved work" is not reachable while the Task 5A authority invariant stands, since `generations.discovery_closed` cannot be set; a dispatch settles the provider-facing half of this item only. See acceptance criterion 4 in [`ci-refresh-evidence.md`](ci-refresh-evidence.md).
- [ ] Exercise a test App PR and prove Required CI cannot be bypassed. Verify exact merge-SHA gating without dispatching production website sync prematurely.
- [ ] Enable production cutover only after the shadow evidence passes. Verify the next production PR, merge, and exactly one website dispatch end to end.
- [ ] Record normalized before and after wall time, runner-hours, logical work, physical requests, retry amplification, batching where applicable, exact coalescing, checkpoints, recovery, output changes, quota, and estimated cost. Do not turn timing into a completeness gate.
- [ ] Remove shadow-only compatibility code, obsolete documentation, old cache state references, dead tests, ignored residuals, and all TODO or placeholder text.
- [ ] Run a final fresh independent review wave. Repeat remediation until every actionable finding is closed or proved inapplicable.
- [ ] Commit the final evidence record, verify `git log -1`, verify a clean worktree, and reconcile the exact remote branch, PR, merge, ruleset, workflow, website, and local main states.
