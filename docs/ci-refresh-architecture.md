# Correctness-First CI and Monthly Refresh Architecture

## Status

This document is the implementation contract for the CI and monthly refresh redesign requested on 2026-08-11. It incorporates the complete session transcript, all 341 available GitHub Actions runs, 1,062 retained jobs, every retained monthly log, repository history, provider documentation, and three independent reconciliation audits.

Correctness and completeness are hard gates. Time, request count, runner usage, cache efficiency, and cost are measurements. They never define completion.

## Problem statement

The current monthly workflow reruns the whole corpus up to 50 times and declares convergence when aggregate logical API-call counts stabilize. It can count failed authors as processed, exit zero after partial article failures, skip revalidation of complete records, overwrite a monolithic response cache, push directly to protected `main`, and dispatch the website without proving that the generated commit passed Required CI.

Historical evidence shows the failure is structural.

- 75 monthly run records contain 76 attempts.
- Monthly execution consumed 172.637 runner-hours.
- 53 runs were cancelled and 2 failed.
- Every scheduled run reached the six-hour runner boundary.
- August completed at least 41,243 logical API calls across three runs and published no data.
- Stable passes still repeated about 2,000 requests, mainly Semantic Scholar and OpenAlex.
- One author regularly determined the completion tail.
- The latest locally converged August commit was rejected by repository rules because publication requires a pull request and Required CI.

## Goals

The implemented system must provide all of the following.

1. Every input row has an explicit disposition.
2. Every enabled author has a fresh inventory observation for each generation.
3. Every in-window publication has a per-author identity and a complete set of required work items.
4. Successful, confirmed-empty, not-applicable, dominated, retryable, malformed, authentication, permanent-failure, and unresolved states remain distinct.
5. No unresolved required work can publish.
6. Completed work survives interruption and is not repeated unless its freshness policy requires it.
7. Exact requests are coalesced safely across authors. Fuzzy author and title identity remains author-specific.
8. Provider batching is used only where a documented exact-ID contract exists and every requested identifier receives an explicit disposition.
9. Output is built in an isolated stage and is never published from a partial generation.
10. Publication occurs through a GitHub App pull request, Required CI, verified merge, and post-merge website dispatch.
11. Pull-request CI retains all meaningful checks while eliminating duplicated instrumentation, serial matrix execution, and repeated wheel builds.
12. Structured evidence remains available after GitHub log expiry.

## Non-goals

The redesign does not create a global fuzzy publication identity. It does not operate Temporal, Prefect, Dagster, Redis, PostgreSQL, Kubernetes, serverless fan-out, Rust workers, or GPU jobs. It does not use wall time or request count as a completion threshold. It does not allow a second implementation to remain as a fallback after cutover.

## Domain invariants

The following existing behaviors remain load-bearing.

- Scholar and DBLP inventories form a union before deduplication.
- A DOI or arXiv identifier does not establish identity without title evidence at or above 0.55.
- Published metadata outranks preprint metadata, while standalone preprints remain represented.
- A fuller author list cannot be replaced by a weaker truncated list.
- Output order and serialization are deterministic and byte-idempotent.
- Orphan deletion requires duplicate-title evidence at or above 0.95.
- `a2i2` is a derived view and never an input source.
- The finalization order is summary flush, phantom reconciliation, safe orphan handling, contribution-window cleanup, canonicalization, superseded-preprint cleanup, `a2i2` rebuild, and baseline rebuild.
- A valid empty response is not equivalent to an error, timeout, malformed body, omitted batch member, or open circuit.
- POST retries are limited to operations whose provider contract proves safety.

## Generation model

A monthly refresh creates one immutable generation. Its identity is a SHA-256 digest over the normalized input census, contribution policy, adapter versions, and base Git commit.

A generation has one of these states.

| State | Meaning |
| --- | --- |
| `planning` | Input and existing corpus are being indexed. |
| `running` | Required work is available or leased. |
| `waiting` | No runnable work exists before a persisted retry deadline or asynchronous provider job completion. |
| `blocked` | Required work has a permanent or administratively actionable failure. |
| `validating` | All required work is terminal and staged output is being checked. |
| `complete` | All completeness and deterministic-output checks passed. |
| `published` | The exact complete generation commit was merged. |
| `superseded` | An explicit operator action replaced the generation before publication. |

A six-hour Actions segment may end while the generation remains `running` or `waiting`. Segment completion is not generation completion.

## Input census

The input format gains explicit `Enabled` and `Exclusion Reason` columns. Existing rows are migrated deterministically.

- A row with a Scholar or DBLP identifier defaults to enabled.
- A row without either identifier must be explicitly disabled with a non-empty reason.
- An empty name with an identifier is invalid.
- Duplicate normalized author and identifier combinations are invalid.
- Every input row is persisted in the generation ledger, including disabled rows.

The planner fails before network work when any row is unclassified.

## Durable ledger

The engine uses the standard-library `sqlite3` module. It uses rollback-journal mode, `synchronous=FULL`, foreign keys, explicit transactions, and one writer connection. WAL is not used because supported Python 3.10 environments may link SQLite versions older than the 2026 WAL fix and the workload does not need concurrent writers.

### Tables

`generations`

- generation ID
- base commit
- input digest
- policy digest
- adapter digest
- state
- created, updated, completed, and published timestamps
- checkpoint sequence
- blocking reason

`authors`

- generation ID
- stable author key
- original input row number
- name
- Scholar ID
- DBLP ID
- enabled flag
- exclusion reason
- inventory disposition

`publications`

- generation ID
- author key
- stable per-author publication key
- discovery source
- normalized title
- year
- exact identifiers
- baseline output path
- required freshness policy

`work_items`

- deterministic work key
- generation ID
- author key and optional publication key
- provider
- operation
- normalized request payload digest
- requested field set
- adapter version
- state
- applicability or dominance reason
- next attempt time
- lease owner and expiry
- attempt count
- last error class and safe diagnostic

`attempts`

- work key
- physical attempt number
- start and finish time
- outcome class
- HTTP status
- retry delay
- response digest
- safe diagnostic

`observations`

- work key
- provider
- observed time
- disposition
- normalized response
- response digest
- schema version

`field_provenance`

- generation ID
- publication key
- field name
- selected value digest
- provider
- observation key
- decision rule

`provider_state`

- provider and quota-pool key
- current concurrency
- rate-limit deadline
- circuit state
- rolling success and failure counts
- asynchronous job identifiers and request digests where applicable

`materializations`

- generation ID
- staged path
- manifest digest
- corpus counts
- validation state

`validations`

- generation ID
- check name
- state
- evidence digest
- safe detail

Every state transition is conditional on the previous state. Duplicate or stale continuations therefore cannot move work backward or overwrite newer evidence.

## Work states

Required work can publish only from these terminal states.

| State | Publication meaning |
| --- | --- |
| `succeeded` | A schema-valid observation was committed. |
| `confirmed_empty` | The provider returned a validated empty result under the current generation's confirmation policy. |
| `not_applicable` | The operation cannot apply and records a machine-readable reason. |
| `dominated` | A stronger current-generation observation fully satisfies the documented merge policy. |

The following states block publication.

- `pending`
- `leased`
- `retry_wait`
- `malformed`
- `authentication_failed`
- `schema_changed`
- `permanent_failed`
- `circuit_open`
- `ambiguous`

No catch block may convert a blocking state into `confirmed_empty`.

## Freshness planner

Each generation performs the following work.

1. Fetch Scholar and DBLP inventories for every enabled author whose source applies.
2. Persist inventory source dispositions independently before union and deduplication.
3. Create or update per-author publication identities from the validated inventory union.
4. Revalidate complete published DOI records through the current authoritative DOI path.
5. Revisit incomplete, preprint, uncertain, and identifier-free records through every applicable source.
6. Mark lower sources dominated only when a current-generation stronger observation supplies every field the lower source could contribute under the merge policy.
7. Confirm empty results through the provider-specific current-generation policy.
8. Create no work for disabled authors, but retain their census disposition.

Existing output completeness controls materialization work, not whether freshness work is due.

## Request identity and coalescing

The exact work key includes provider, operation, exact normalized identifiers or query, requested field set, adapter version, and freshness epoch.

Identical exact work is sent once and may be consumed by several per-author reducers. Every reducer re-evaluates the existing identity contract. Fuzzy searches include the author key and are never shared globally.

## Provider execution

The engine uses durable provider work queues with bounded synchronous workers. The existing Requests sessions, connection pools, token buckets, and Tenacity retry boundary remain because they are already mature, measured, and compatible with every adapter. The scheduler owns work rather than an author's call stack, so provider waits no longer cause whole-corpus repetition. An async transport rewrite is not an acceptance requirement and would combine two high-risk migrations without evidence that it improves the provider-limited critical path.

Each provider owns:

- connection and pool limits
- request and response timeouts
- documented rate limit
- bounded concurrency and persisted retry deadlines
- retryable status and exception classes
- retry budget
- optional circuit state when provider failure history proves it beneficial
- response schema validator
- an exact batching contract when current primary documentation and replay tests prove equivalent semantics

Physical attempts are counted inside the transport. Logical work and physical requests are separate metrics.

### Retry policy

- Connection errors, timeouts, 408, 429, and selected 5xx responses use bounded exponential backoff with decorrelated jitter.
- `Retry-After` seconds and HTTP dates are honored up to the provider's maximum safe wait.
- Authentication failures and invalid requests fail fast.
- Malformed successful responses are classified separately and never cached as empty.
- When a circuit is configured, opening it pauses only that provider and never creates a negative observation.
- GET operations are idempotent.
- POST operations are retried only when the provider documents idempotency or the response status proves that processing did not begin.

### Evaluated batching

Batching is adopted per operation only when current primary documentation, replay fixtures, and equivalent result correlation prove it safe. The evaluated candidates are Semantic Scholar exact paper batches, OpenAlex exact DOI filters, PubMed exact fetch groups, Europe PMC exact identifier queries, and arXiv `id_list`. Crossref, SerpAPI author inventories, and Serply remain singleton. OpenReview may group only by a documented invitation, venue, or forum filter. Gemini asynchronous batching stays off the critical path unless measured eligible volume justifies it and ambiguous job creation can be reconciled durably.

Every batch response is correlated back to every requested input. Missing, duplicate, malformed, and unexpected members make the affected inputs ambiguous or malformed, never successful or empty.

## Segment execution and checkpointing

One workflow-level concurrency group serializes refresh generations with `cancel-in-progress: false`.

Each Actions segment:

1. Restores and verifies the newest checkpoint matching the generation.
2. Falls back to the previous verified checkpoint when the newest is corrupt.
3. Reclaims expired leases.
4. Runs available work until the planned drain boundary.
5. Stops leasing new work before the runner ceiling.
6. Drains bounded in-flight work.
7. Commits and validates SQLite state.
8. Creates an authenticated encrypted checkpoint.
9. Persists the checkpoint and cleartext non-secret manifest.
10. Dispatches a continuation only when required work remains runnable or waiting.

Checkpoints occur periodically, after billed asynchronous job creation, and at planned segment exit. The encrypted payload contains the ledger, staged generation data, and reusable validated observations. The manifest contains only schema version, generation ID, input and policy digests, checkpoint sequence, creation time, ciphertext checksum, and key identifier.

The current and previous checkpoint remain recoverable. Restore failure never silently restarts from zero.

## Staged materialization

The engine copies the current committed output to a generation-specific staging directory. Network workers never write to committed `output/`.

After all required work reaches an acceptable terminal state, the reducer materializes per-author output and executes the existing finalization sequence. It then validates:

- every input row disposition
- every enabled author inventory
- every required work item
- publication identity and DOI-title evidence
- author-list preservation
- preprint and published coexistence rules
- contribution window
- summary-to-file agreement
- safe deletion evidence
- exact `a2i2` derivation
- baseline agreement
- absence of unresolved work
- byte-identical second materialization

Only a successful validation transaction moves the generation to `complete`.

## Publication

The workflow mints a short-lived GitHub App token after a generation becomes complete.

1. Rebase or recreate the publication branch from current `main`.
2. Reapply the staged generation.
3. Rerun corpus validation when `main` changed.
4. Push `bot/citeforge-refresh-YYYY-MM` with force-with-lease.
5. Create or update one App-authored pull request.
6. Enable auto-merge when repository rules permit it.
7. Require Required CI and the refresh completeness check on the exact head SHA.
8. Observe the merge event and verify the merged SHA.
9. Dispatch website synchronization only from the verified merge path.

The App may bypass a human-review-only rule when organization policy allows unattended publication. It must not bypass Required CI. Direct push to `main` is removed.

## Pull-request CI

CI keeps all supported-version test coverage while removing repeated setup and instrumentation.

- One build job verifies locks, builds the wheel once, smoke-tests it outside the checkout, and uploads the immutable wheel.
- One quality job runs Ruff, format, mypy, pre-commit, dependency audit, license checks, and workflow validation once.
- Compatibility jobs download and install the same wheel, then run the full hermetic suite on Python 3.10 through 3.14.
- Coverage instrumentation runs on one canonical Python version only.
- Independent matrix jobs run concurrently.
- Dependency downloads use lock-digest caches.
- Refresh pull requests run the corpus completeness and deterministic-materialization gate.
- Required CI aggregates every required job and fails on unexpected skips.

No docs-only or path-only shortcut may skip a correctness gate required for the changed surface.

## Observability

Every segment writes a structured manifest and safe event stream. Secrets, raw credentials, and sensitive raw responses are absent.

Metrics include:

- input, enabled, excluded, confirmed-zero, blocked, and completed author counts
- publications by disposition
- work items by provider and state
- logical operations and physical requests
- retry amplification and error class
- batch count and fill when batching applies
- latency and quota headroom
- cache and exact-request coalescing effectiveness
- circuit-open duration when a circuit applies
- checkpoint sequence, age, size, restore source, and replayed work
- staged output additions, changes, deletions, and manifest digest
- runner wall time and runner-hours
- publication branch, pull request, checked SHA, merge SHA, and website dispatch result

The manifest is uploaded as a retained Actions artifact and committed with a successful data generation in a compact non-secret form.

## Validation contract

The offline suite must prove:

- successful complete generation
- timeout and transient 5xx retry
- 429 with numeric and date `Retry-After`
- valid empty response confirmation
- syntactically invalid and valid-wrong-shape payload rejection
- reordered, truncated, duplicated, omitted, and unexpected members for every adopted batch operation
- partial provider completion
- authentication failure and schema drift
- circuit open and half-open recovery when a circuit is configured
- interruption at every transaction boundary
- lease expiry and duplicate continuation
- newest-checkpoint corruption with previous-checkpoint recovery
- no restart when both checkpoints are invalid
- main advancement before publication
- failed or unresolved work blocking materialization and publication
- two byte-identical materializations
- frozen-response output comparison with the legacy corpus
- wheel artifact reuse and full supported-version CI selection

A publication-disabled live shadow generation must additionally prove current provider authentication, schemas, quotas, batch semantics, GitHub checkpoint persistence, App pull-request creation permissions, Required CI triggering, and non-publication cleanup. Production cutover requires this external evidence.

## Cutover

The first containment change disables the old direct publication and website-dispatch paths. The durable engine then proceeds behind an explicit shadow-only command while the old workflow may continue only as a non-publishing data-gathering reference. This temporary parallelism exists only for differential validation. Neither path can publish until the durable engine passes its shadow gate.

Cutover occurs in one commit after the shadow generation passes. That commit removes:

- the 50-pass API-call-delta loop
- whole-workflow convergence retriggers
- monolithic JSON cache branch publication
- unauthenticated AES-CBC checkpointing
- direct `main` push
- pre-merge website dispatch
- response-cache paths superseded by the durable exact-request ledger
- tests that assert swallowed author or article failures

After cutover, only the durable generation engine may publish.

## Acceptance

The redesign is complete only when:

1. Every offline contract test passes on every supported Python version where applicable.
2. The repository's full static, dependency, security, packaging, and test gates pass.
3. The frozen production corpus is unchanged except for reviewed intentional differences.
4. A publication-disabled live shadow generation reaches `complete` with no unresolved work.
5. Checkpoint resume is proven across a clean runner boundary.
6. A bot test pull request triggers Required CI and is not able to bypass it.
7. The production workflow no longer contains any obsolete convergence, cache-monolith, direct-push, or pre-merge dispatch path.
8. Before and after evidence reports normalized correctness, work, physical requests, retries, batch efficiency, checkpoint replay, runner usage, and estimated cost.

Material runtime improvement is expected from eliminating whole-corpus reruns, exact-request coalescing, batching, connection pooling, and parallel CI. A run may take longer when required to obtain complete and correct evidence. That is not a regression unless equal work is slower without a correctness benefit.
