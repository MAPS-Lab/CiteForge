# CI and monthly refresh evidence ledger

This document is the requirement-to-evidence record for the redesign specified in
[`ci-refresh-architecture.md`](ci-refresh-architecture.md) and sequenced in
[`ci-refresh-implementation-plan.md`](ci-refresh-implementation-plan.md). It records what has been
measured, where the measurement came from, and which requirements remain unevidenced.

Every figure below carries a provenance class. The distinction is load-bearing because roughly half
the redesign's justification rests on observations that this repository cannot reproduce.

| Class | Meaning |
|---|---|
| Repository-derived | Reproducible offline from this checkout by the command given alongside it. Anyone with the tree can re-derive the number. |
| Repository history | Read out of Git history or a committed artifact rather than the working tree. Reproducible offline, but describes a past state. |
| External observation | Measured against the GitHub Actions API, organization billing, or a provider dashboard. Recorded here with its source and date. Not reproducible from this checkout. |
| Wired, not executed | A mechanism for obtaining the evidence is written and committed to this tree, but it has never run. It may not even be reachable yet. This class exists so that the presence of a workflow is never read as a result produced by it. |
| Not evidenced | The requirement has no measurement yet, and no mechanism has been built to obtain one. Named explicitly rather than left implicit. |

The last two classes are deliberately separate. A requirement moves from "not evidenced" to "wired, not executed" the moment the mechanism lands, and it moves out of both only when a run can be cited by identifier and terminal state. Building the thing that would produce the evidence produces no evidence.

The ledger was compiled on 2026-08-13 against branch `audit/library-substitution` at commit `8a13d3f`
plus uncommitted work, and revised the same day. Tasks 7 through 10 were being written concurrently in
the same worktree during both passes, so every status row was re-derived against the working tree
immediately before each pass was finished, recording file presence and collected test counts rather
than recollection. Anything still uncommitted at that moment is marked as such.

The revision was prompted by two changes. Provider credentials turned out to be reachable through
GitHub Actions secrets, which makes a live shadow run possible and moved it from an unbuildable
requirement to a built and unexecuted one. And the two obsolete publication paths that acceptance
criterion 7 turns on were scheduled for deletion, so that row was re-read against the file rather than
carried forward.

## Historical baseline

These figures established that the failure was structural rather than incidental. They come from the
Actions API and organization billing, so they are external observations.

| Measurement | Value | Class |
|---|---|---|
| Monthly refresh run records | 75 | External observation |
| Runs cancelled at the six-hour job ceiling | 53 | External observation |
| Runs failed | 2 | External observation |
| Runs succeeded | 20 | External observation |
| Run wall-clock hours, created to updated | 173.65 | Measured, `gh run list` over 75 runs |
| Job execution hours, summed started to completed | 166.55 | Measured, `gh api .../jobs` over 189 jobs |
| Runner-hours recorded in the architecture document | 172.637 | External observation |
| Monthly refresh share of all Actions time in the repository | 95 percent | Measured |

Three numbers, three different quantities, and the earlier revision of this document left them
unreconciled. They are reconciled here.

Run wall clock is `createdAt` to `updatedAt` on the run record, so it includes queue time and the gaps
between jobs. Job execution is `started_at` to `completed_at` summed over every job in every run,
which is the time a runner was actually held. The difference, 7.10 hours across 75 runs, is queue and
inter-job time and is real: it is why the six-hour ceiling bites on a per-job basis while schedule
contention is a wall-clock property.

The architecture document's 172.637 sits between the two, within 0.6 percent of run wall clock and
3.7 percent above job execution, which places it in the wall-clock family rather than the billed-time
family. It is retained as an external observation rather than adjusted, because the exact run set
behind it is not recoverable.

### Operator requirement, checkpoint secret

`CACHE_ENCRYPTION_KEY` is the single input to the checkpoint key. It must be generated with
`openssl rand -base64 32` and never chosen by a human. scrypt at N=2^17 raises the cost of an offline
guess from roughly 1.7 million per second per core to about two, but no key-derivation function
rescues a guessable passphrase, and the ciphertext is world readable on the state branch for as long
as it exists. The code enforces a 16-byte floor only, which rejects a truncated secret and says
nothing about entropy, so this requirement is recorded here rather than left implicit.

The `data-cache` branch that the retired AES-CBC response cache wrote was deleted on 2026-08-13. It
held a 3,481,952-byte ciphertext sealed with the same secret under OpenSSL's 10,000-iteration PBKDF2
default, which made it a standing offline-attack target against the key the new checkpoints use. The
code path that wrote it had already been removed; the artifact had not.

None of the three is a cost. The repository is public and runs on standard runners, so the
organization billing API reports this repository at zero gross and zero net. The constraint these
numbers describe is the six-hour job ceiling and the multi-day wall clock to a complete refresh, not
money.

### The constraint is the job ceiling, not money

The binding constraint was misidentified during the original design. Actions minutes are free on this
repository, which is public and uses standard runners, and organization billing shows 0.00 dollars
gross and 0.00 dollars net for the period. There is no cost gate to optimize. The real limits are the
six-hour job ceiling, which cancelled 53 of 75 runs, and wall-clock time, which decides whether a
monthly generation finishes before the next one is due.

Paid provider quota is likewise not the binding constraint. SerpAPI recorded 94 calls across the whole
of August 2026. Across all three metered providers the month totalled roughly 5,350 calls, on the
order of two to three dollars. A redesign justified by API spend would be optimizing a rounding error.
This matters for the acceptance criteria, because it means request-count reduction is a metric and
never a gate, exactly as the architecture document requires.

### The convergence loop never converged

The August 2026 chain is the clearest single piece of evidence. Four consecutive runs reported these
aggregate logical API-call totals, with the deltas the workflow actually tested.

| Run | Total API calls | Delta against previous |
|---|---|---|
| 1 | 3,121 | not applicable |
| 2 | 2,686 | 435 |
| 3 | 2,447 | 239 |
| 4 | 2,233 | 214 |

The workflow declared convergence when the delta fell to 50 or below across consecutive runs. The
smallest observed delta was 214, more than four times the threshold, and the deltas were still falling
by hundreds when the runner was cancelled at the ceiling. The loop was not near convergence and had no
mechanism that would have reached it.

The threshold and the loop bound are repository history rather than assertion. At commit `0c6c7ba` the
workflow set `max_runs=50` and `CONVERGENCE_THRESHOLD=50`, tested `delta -le CONVERGENCE_THRESHOLD`
over three runs, and re-triggered itself when the result was not converged.

```bash
git show 0c6c7ba:.github/workflows/monthly-refresh.yml | grep -n 'max_runs\|CONVERGENCE_THRESHOLD'
```

The architecture document separately records at least 41,243 logical API calls across three August
runs. That figure and the four-run chain above are different measurements taken from different
retained logs, and they have not been reconciled against each other offline. Both are recorded rather
than silently merged.

### Publication has been blocked since 2026-07-30

Repository ruleset 10020080 requires a pull request and Required CI for changes to `main`. The legacy
workflow pushed directly. Since the ruleset took effect on 2026-07-30 no monthly refresh has published
data, and the last published corpus commit is `0c6c7ba`, dated 2026-07-05. Verify with
`git log --format='%h %ad %s' --date=short 0c6c7ba -1`.

The corpus in this branch is therefore approximately six weeks stale, and that staleness is a
publication-path defect rather than an enrichment defect.

## Corpus and census facts

These are repository-derived and reproducible from the checkout.

| Measurement | Value | Command |
|---|---|---|
| Physical input rows | 69 | `python3 -c "import csv;print(len(list(csv.DictReader(open('data/input.csv')))))"` |
| Rows enabled | 64 | Count `Enabled == "true"` over the same reader |
| Rows disabled with an exclusion reason | 5 | Count `Enabled == "false"` over the same reader |
| Authors with committed output | 57 | `output/baseline.json`, author keys excluding `a2i2` |
| Publications across those authors | 2,575 | Sum of the same author counts |
| Total committed `.bib` files | 3,669 | `find output -name '*.bib' \| wc -l` |
| Files in the derived `a2i2` view | 1,094 | `output/baseline.json`, the `a2i2` key |
| Largest single author | 267 | `Orji (-1cHtBQAAAAJ)` in `output/baseline.json` |

The `baseline.json` total of 3,669 equals the on-disk `.bib` count, which is the invariant the
baseline rebuild step exists to maintain, and 2,575 plus the 1,094 derived `a2i2` files reproduces it
exactly. The 69 input rows against 57 authors with output is not a discrepancy. Five rows are
explicitly disabled, and the remainder are enabled rows that have not yet produced a committed
directory.

## Concurrency shape of the corpus

The tail-author problem the architecture document describes as "one author regularly determined the
completion tail" is measurable directly from the corpus shape. The largest author holds 267 of 2,575
publications, so under an author-level pool that one author sets a floor on the makespan no matter how
many workers are added.

Two derivations are recorded because they disagree in one place and the disagreement is informative.

The first is an idealized list-schedule over the committed per-author counts at 0.1 seconds per
article, computed from `output/baseline.json` with no threading overhead. It is exactly reproducible.

| Configuration | Makespan | Mean occupancy of the pool |
|---|---|---|
| Author-level pool, 16 workers | 26.70 s | 9.64 of 16 |
| Article-level pool, 16 workers | 16.10 s | 15.99 of 16 |
| Article-level pool, 24 workers | 10.80 s | 23.84 of 24 |

The second is the threaded execution measured on the same corpus shape at the same 0.1 seconds per
article, reported under both weightings.

| Configuration | Makespan | Time-weighted occupancy | Event-weighted occupancy |
|---|---|---|---|
| Author-level pool, 16 workers | 29.89 s | 8.66 of 16 | 12.88 of 16 |
| Article-level pool, 16 workers | 16.37 s | 15.91 of 16 | 15.36 of 16 |
| Article-level pool, 24 workers | 11.00 s | 23.75 of 24 | 23.18 of 24 |

An earlier revision of this document recorded the author-level occupancy as 13.35 against the model's
9.64 and called the gap unexplained. It is explained, and it was a measurement defect rather than a
disagreement about the system. The harness sampled concurrency once per article START, which weights
every article equally. Articles start rarely during the straggler tail, so the low-concurrency period
that defines the author-level case was precisely the period being undersampled. Integrating
concurrency over wall time instead answers the question actually being asked, which is how busy the
workers were.

Under time weighting the threaded run measures 8.66 against the model's 9.64, an eleven percent gap
attributable to thread scheduling, and the two agree. The identity that settles it is exact: mean
occupancy is total busy worker-seconds over elapsed time, so (2575 + 57) articles and inventory calls
at 0.1 s over 29.89 s gives 8.81 predicted against 8.66 measured. The article-level rows barely move
between weightings because their concurrency is near constant throughout, which is itself the finding.

The corrected figure makes the author-level case look worse, not better: the pool was 54 percent busy,
not 83 percent.

The conclusion both derivations support is the one that matters, and it does not depend on resolving
the gap. Moving the unit of parallelism from the author to the article raises pool occupancy from
roughly 60 percent to roughly 98 percent and cuts the makespan by about 40 percent at the same worker
count, because it removes the single-author floor. Raising the article-level pool from 16 to 24
workers buys a further 33 percent. These are shape measurements on a synthetic per-article cost and
are not a prediction of live runtime, which is provider-limited rather than CPU-limited.

## Task status

Tasks 1 through 6 are committed. Tasks 7 and 8 had landed as uncommitted working-tree changes at the
moment of verification, and Task 9 was partially applied. Nothing below is claimed from the plan's
checklist alone. Every row was checked against the file on disk and, where a test module exists,
against its collected test count.

| Task | Status | Evidence |
|---|---|---|
| 1. Contain unsafe publication and restore truthful gates | Complete, and deliberately superseded | Commit `2b6387e`. Replaced the publishing workflow with a non-publishing diagnostic shell and added `tests/test_workflow_contracts.py`. Task 8 removes this containment by design, so the containment test was rewritten with it. `tests/test_workflow_contracts.py` now collects 38 tests. Its three parametrized document tests run against every one of the five files in `.github/workflows/`, discovered by glob rather than named, so the shadow workflow Task 10 added was covered by them the moment it landed. |
| 2. Explicit input census and durable work types | Complete | `citeforge/refresh/types.py`, `citeforge/refresh/census.py`, `data/input.csv` carrying `Enabled` and `Exclusion Reason`. 86 tests in `tests/test_refresh_census.py`. |
| 3. Transactional generation ledger | Complete | `citeforge/refresh/ledger.py`. 150 tests in `tests/test_refresh_ledger.py`. |
| 4. Classified provider transport and exact request coalescing | Complete | `citeforge/refresh/transport.py`, `citeforge/refresh/decoders.py`, `citeforge/refresh/provider_adapters.py`. 130 tests in `tests/test_refresh_transport.py`, 60 in `tests/test_refresh_decoders.py`. |
| 5. Phased discovery and resumable generation execution | Complete | `citeforge/refresh/engine.py`, `discovery.py`, `inventory.py`, `authority.py`, `capabilities.py`, `corpus.py`, `publication_discovery.py`. 26 tests in `tests/test_refresh_engine.py` plus 149, 73, 44 and 36 in the corpus, publication-discovery, discovery and inventory modules. The CLI surface landed late in the sequence. `citeforge/cli.py` now registers a `refresh` subcommand with a required `--state-dir` and optional `--checkpoint-dir` and `--generation`, and `run_refresh` opens `Ledger.open` and constructs `RefreshEngine`, which is the first production caller of either. `--checkpoint-dir` requires `CHECKPOINT_KEY` in the environment and is what the monthly workflow passes to make a segment resumable. |
| 6. Authenticated checkpoints and interrupted resume | Complete for checkpoints, resume tests split | Commit `4dc7439`. `citeforge/refresh/checkpoint.py` with 33 tests in `tests/test_refresh_checkpoint.py`. The `cryptography 50.0.0` dependency audit record is in `docs/library-substitution-audit.md`. `tests/test_refresh_resume.py` does not exist, by the deliberate split described under open items below. |
| 7. Stage, validate, and deterministically materialize output | Landed, uncommitted | `citeforge/refresh/staging.py` exposing `CorpusManifest`, `prepare_stage`, `validate_stage` and `materialize_candidate`, with 25 tests in `tests/test_refresh_staging.py`. `citeforge/pipeline/postrun.py` now returns a `FinalizationReport`, raises `FinalizationError` instead of swallowing write failures, and runs the last five finalization steps outside the summary-CSV conditional. 16 tests in `tests/test_finalize_run.py`. |
| 8. PR-only publication and merge-gated website dispatch | Landed, uncommitted | `citeforge/refresh/publication.py` exposing `PublicationPort`, `CandidateOffer`, `MergeObservation`, `candidate_block_reason` and `merge_block_reason`, with 54 tests in `tests/test_refresh_publication.py`. `.github/workflows/monthly-refresh.yml` rewritten as the cutover workflow and `.github/workflows/citeforge-refresh-merged.yml` added. The deletions the checklist requires landed after the first pass of this ledger and are recorded under criterion 7. Covered by `tests/test_workflow_contracts.py`. |
| 9. Rebuild CI around one immutable wheel | Landed, uncommitted | `.github/workflows/tests.yml` carries uncommitted modifications. `tests/test_ci_artifact_contract.py`, which the task names as its contract test, now exists and collects 13 tests. |
| 10. Hosted shadow, cutover, and evidence reconciliation | Shadow wired, not executed | This document is the offline portion. `.github/workflows/refresh-shadow.yml` is the dispatch mechanism for the live shadow run and is covered by workflow contract tests, but it has never been dispatched. Hosted Required CI, the App pull request and the production cutover remain unevidenced and, unlike the shadow, unwired. |

The plan's own file list for Task 10 names `/tmp/citeforge-reconciliation-ledger.md` as a file to
modify. That path is outside the repository and does not survive a reboot, so it cannot hold
reconciliation evidence. This document replaces it. The plan step is recorded as a defect in the plan
rather than silently satisfied.

## Acceptance criteria

The architecture document lists eight acceptance conditions. Their state follows.

| Criterion | State | Evidence or blocker |
|---|---|---|
| 1. Offline contract tests pass on every supported version | Partially met | The suite passes locally on Python 3.14 and the CI matrix covers 3.10 through 3.14 in `.github/workflows/tests.yml`. Tasks 7, 8 and 9 have all contributed their contract tests. "Passes on every supported version" is still a local-machine claim, because only 3.14 has been exercised here and the other four rest on the matrix rather than on an observed run. |
| 2. Full static, dependency, security, packaging and test gates pass | Met locally at each landed commit | Ruff, mypy and pyrefly are clean and the suite is green apart from two live-network tests that pytest-socket blocks offline. They are `tests/test_apis.py::test_openalex_search_live` and `tests/test_apis.py::test_crossref_multiple_candidates`, named here so a third failure is never absorbed into a round "two are expected". Reproduce with `.venv/bin/python -m pytest tests/test_apis.py -q -p no:randomly`. |
| 3. Frozen production corpus unchanged except reviewed differences | Met | 3,669 committed `.bib` files and the `baseline.json` total agree. No task through 6 wrote to `output/`. |
| 4. A publication-disabled live shadow generation reaches complete | **Unattainable as written, and the criterion is the thing that is wrong** | The workflow exists and is wired. `.github/workflows/refresh-shadow.yml` runs `citeforge refresh --state-dir` in a bounded segment loop against live providers, reading `SEMANTICSCHOLAR`, `SERPAPI`, `SERPLY`, `GEMINI`, `OPENREVIEW` and `CROSSREF_MAILTO` from repository secrets. It is `workflow_dispatch` only, holds `permissions: contents: read`, mints no App token and runs no push, so publication is disabled structurally rather than by a flag. What it cannot do is reach `complete`. `RunStatus.COMPLETE` requires `Ledger.all_required_satisfied()`, which requires `generations.discovery_closed = 1`, and the ledger schema refuses that four ways: a `BEFORE UPDATE` trigger, a `BEFORE INSERT` trigger, `_assert_task5a_authority_invariant` re-checked on every status read, manifest read and reopen, and a schema fingerprint that rejects a database whose triggers were dropped. Discovery authority is the claim that an author's publication list is complete, and Task 5A is deliberately not entitled to make it. This criterion therefore cannot be met by dispatching anything; it can only be met by lifting that invariant, which is a separate design decision. An earlier revision of the shadow workflow gated its own job on `complete`, so every dispatch would have ended red no matter what it found. That gate now tests provider reachability, which is what a shadow run can actually settle, and `tests/test_workflow_contracts.py::test_no_workflow_gates_on_a_status_the_ledger_forbids_producing` prevents the pattern returning. |
| 5. Checkpoint resume proven across a clean runner boundary | Partially met, and the gap is not what the shadow workflow closes | The cryptographic and fallback properties are proven offline by the 33 checkpoint tests. The shadow workflow's segment loop crosses a process boundary, not a runner boundary, because its `STATE_DIR` is `runner.temp` and no step restores that directory on a later dispatch. Resume across a clean runner boundary therefore remains unevidenced and unwired. |
| 6. A bot pull request triggers Required CI and cannot bypass it | Not evidenced | Requires the GitHub App and a hosted run. The shadow workflow does not wire this. It deliberately opens no pull request, so it exercises no part of the App permission path. |
| 7. Production workflow carries no obsolete convergence, cache-monolith, direct-push or pre-merge dispatch path | Met, verified against the file | All four paths are absent from `.github/workflows/monthly-refresh.yml`. This row was recorded as unmet on the first pass and re-read on the second. The detail, the replacements and the reproducing commands are below the table. |
| 8. Before and after evidence for correctness, work, requests, retries, checkpoints, runner usage and cost | Partially met, collection wired | The before side is recorded above. The after side is collected by the shadow workflow, which uploads per-segment logs and the ledger manifest as a 90-day artifact and exports the manifest digest rather than the ledger database. No artifact exists yet because no run has happened. |

### Criterion 7 in detail, and what replaced each deleted path

This row moved. The first pass recorded it unmet, because the `data-cache` orphan branch was still
restored and force-pushed and the whole-corpus loop still ran, reduced from 50 passes on an API-count
delta to 10 passes on a corpus digest rather than replaced. Both were then deleted. The row is
recorded here from a re-read of the file rather than from that earlier reading, and these are the
searches that establish it.

```bash
for p in data-cache openssl aes-256-cbc api_cache max_runs 'seq 1' digest convergence; do
  printf '%-14s %s\n' "$p" "$(grep -c -- "$p" .github/workflows/monthly-refresh.yml)"
done
```

Every one of those returns 0. Each deleted path has a named replacement in the same file.

| Deleted path | What stands in its place |
|---|---|
| Whole-corpus pass loop, `max_runs=10` over a corpus digest | Step "Run one generation segment". It runs `citeforge refresh --state-dir --checkpoint-dir` exactly once under a 300-minute step timeout inside the job's 360, and fails closed when the command exits 0 without reporting `complete` or `continuation`. |
| Corpus-digest and API-delta convergence tests | The ledger's terminal status, parsed from the line `citeforge.cli` writes as `Refresh <status>: generation=…`. Completeness is read from the authority, never inferred from a count that stopped moving. |
| `data-cache` orphan branch, AES-CBC tarball, force push | Nothing carries the response cache between runs. Steps "Restore the sealed checkpoint from the state branch" and "Save the sealed checkpoint to the state branch" keep sealed AES-GCM checkpoints on a `refresh-state` branch instead, which is a different artifact with a different threat model, and it is disclosed rather than counted as a clean removal. |
| Direct push to `main` | Step "Open or update the refresh pull request" pushes a `data/refresh-${MONTH}` branch with `--force-with-lease` and offers it through `gh pr create`. Both it and the token step are gated on `steps.segment.outputs.status == 'complete'`, so a continuation's partial corpus cannot be offered. |
| Pre-merge website dispatch | `.github/workflows/citeforge-refresh-merged.yml`, which fires on a merged pull request whose head matches `data/refresh-`. |
| Re-trigger on non-convergence | Step "Dispatch the continuation segment", fired on `continuation` or `cancelled` and pinning the generation identifier so the next segment resumes rather than re-derives. It keeps the 12-run monthly runaway cap. |

Steps are named rather than cited by line, because the file was still being edited while this was
written and a line number would have been stale before the paragraph was finished.

Two honest qualifications on this row. The secret is still named `CACHE_ENCRYPTION_KEY` and is now read
as `CHECKPOINT_KEY`, so the name outlived the mechanism it was created for, and a reader grepping for
the retired cache by secret name will find live references in the checkpoint steps. And a force-pushed
state branch still exists. What was removed is the response-cache monolith on it, not the practice of
keeping run state on a branch.

This is a property of the workflow file, which is the whole of what the criterion asks. It is not
evidence that the segment, the checkpoint restore or the pull request work when run. No run of this
file exists.

## What cannot be evidenced offline

Seven properties require a live publication-disabled shadow generation with real provider credentials
and a hosted runner. None of them can be established from this checkout, and no offline test
substitutes for them. They are listed separately so that a green local suite is never mistaken for
readiness to cut over.

The workflow that would settle them now exists. `.github/workflows/refresh-shadow.yml` reads live
credentials from repository secrets, which removes the reason each of these was previously deferred.
It does not shorten this list by one entry. Every property below is a claim about what live providers
and a hosted runner actually do, and a workflow that has not run has observed nothing about either.
The list contracts when run identifiers and artifacts can be cited against it, not when the dispatch
button becomes available.

That the shadow has never run is itself a claim, so it is evidenced rather than asserted. The file is
now tracked and present on the default branch, which merged as `7df0c8b`, so the earlier form of this
evidence no longer holds: the checks below previously showed an untracked file and now show a tracked
one. Non-execution is therefore established from the run list rather than from the file's absence.

```bash
git log --all --oneline -- .github/workflows/refresh-shadow.yml   # 7df0c8b and later, the file is tracked
gh run list --workflow "Live Shadow Refresh" -R MAPS-Lab/CiteForge   # empty, no dispatch has occurred
```

The workflow is dispatchable as of that merge, because `workflow_dispatch` surfaces once the file is on
the default branch. Dispatching it spends real SerpAPI and Serply quota, so it is an operator action
and not something this checkout performs.

Current provider authentication is unverified. The offline suite drives every adapter against scripted
transports, which proves the classification logic and proves nothing about whether the credentials in
repository secrets are still valid or whether a provider has changed its authentication scheme.

Current provider schemas are unverified. The decoders are tested against frozen fixtures. A provider
that changed a response shape since those fixtures were captured would be caught by the schema-drift
classification at runtime, but whether any provider has actually drifted is unknown offline.

Current provider quotas and rate limits are unverified. The configured limits are the documented ones.
Whether the account's live headroom matches the documentation, and whether the corpus fits inside a
monthly window at those limits, is a live measurement.

Batch semantics are unverified. The architecture document permits batching only where primary
documentation and replay correlation prove equivalent semantics. Replay fixtures can prove correlation
handling. Only a live run proves the provider still honours the batch contract those fixtures were
recorded against.

The GitHub App pull-request permission path is unverified. Whether the App can open a pull request,
whether it can enable auto-merge under ruleset 10020080, and whether it is correctly unable to bypass
Required CI are all properties of the live repository configuration. Offline tests can prove the code
never constructs a push to `main`. They cannot prove the ruleset behaves as expected.

Checkpoint persistence across a real Actions segment boundary is unverified. The offline tests prove
the seal, the authentication, the fallback to the previous sequence and the fail-closed behaviour when
both are invalid. They do not prove that the artifact survives the transition between two hosted jobs.

Runner-hour and wall-clock improvement is unmeasured. The before figures are recorded above. The after
figures require the shadow run, and per the architecture document they remain metrics and never gates.

## Open items carried forward

The Task 6 checklist asks for resume tests taken "after stage manifest and candidate record". Those
two artifacts are Task 7 and Task 8 outputs and did not exist when Task 6 landed. The checklist was
split rather than stubbed. The checkpoint-only resume properties are proven in
`tests/test_refresh_checkpoint.py`, and the stage-manifest and candidate resume tests belong to the
task that creates the artifact. `tests/test_refresh_resume.py` is therefore absent on purpose.

Checkpoint retention is asymmetric between the filesystem and the ledger, and the asymmetry is
deliberate. `CheckpointStore` retains the current and previous sequence on disk. The ledger's
`checkpoints` table is append-only and nothing deletes a row, so it keeps the full history. The
"current plus previous" requirement is a filesystem property only.

Both `postrun.py` docstrings previously listed seven finalization steps and omitted the
superseded-preprint cleanup that runs between the post-run fixup and the `a2i2` rebuild, as did
`CLAUDE.md`. All three were corrected. The code order at `_remove_superseded_preprints` is the
authority, and it is eight steps.

`tests/test_ci_artifact_contract.py` was absent when this ledger was first compiled and now exists,
collecting 13 tests. That closes the open item as it was written. It leaves the narrower one behind
it, which is that these tests read the workflow file rather than a run of it, so they establish the
shape `tests.yml` declares and not that a hosted run installs the one wheel the shape describes.

## Reproducing the repository-derived figures

```bash
# Census, corpus and author counts
python3 -c "import csv;r=list(csv.DictReader(open('data/input.csv')));print(len(r))"
python3 -c "import json;b=json.load(open('output/baseline.json'));a={k:v for k,v in b['authors'].items() if k!='a2i2'};print(len(a),sum(a.values()),b['total'])"
find output -name '*.bib' | wc -l

# Legacy convergence threshold and loop bound
git show 0c6c7ba:.github/workflows/monthly-refresh.yml | grep -n 'max_runs\|CONVERGENCE_THRESHOLD'

# Last published corpus commit
git log --format='%h %ad %s' --date=short 0c6c7ba -1

# Test counts for a module
.venv/bin/python -m pytest tests/test_refresh_ledger.py -o addopts= --collect-only -q | grep -c '^tests/'
```
