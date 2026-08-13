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
| Wall-clock hours consumed by monthly refresh | 173.65 | External observation |
| Monthly refresh share of all Actions time in the repository | 95 percent | External observation |
| Runner-hours consumed by monthly execution | 172.637 | External observation, recorded in the architecture document |

The wall-clock and runner-hour figures are two separate measurements of the same population and are
not expected to be identical. Wall clock counts elapsed time on the run record. Runner-hours counts
billed job time. Both are reported because the six-hour ceiling is a per-job limit while the schedule
contention is a wall-clock property.

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
article.

| Configuration | Makespan | Mean occupancy of the pool |
|---|---|---|
| Author-level pool, 16 workers | 30.49 s | 13.35 of 16 |
| Article-level pool, 16 workers | 16.27 s | 15.56 of 16 |
| Article-level pool, 24 workers | 10.94 s | 23.42 of 24 |

The two article-level rows agree within two percent on makespan, which is the expected cost of real
thread scheduling over an idealized schedule. The author-level rows agree on the direction and on the
conclusion but not on occupancy, where the idealized model predicts 9.64 and the threaded run measured
13.35. That gap is unexplained offline and is recorded as such rather than reconciled by adjusting one
of the models.

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
| 1. Contain unsafe publication and restore truthful gates | Complete, and deliberately superseded | Commit `2b6387e`. Replaced the publishing workflow with a non-publishing diagnostic shell and added `tests/test_workflow_contracts.py`. Task 8 removes this containment by design, so the containment test was rewritten with it. `tests/test_workflow_contracts.py` now collects 31 tests. Its three parametrized document tests run against every one of the five files in `.github/workflows/`, discovered by glob rather than named, so the shadow workflow Task 10 added was covered by them the moment it landed. |
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
| 4. A publication-disabled live shadow generation reaches complete | Wired, not executed | `.github/workflows/refresh-shadow.yml` runs `citeforge refresh --state-dir` in a bounded segment loop against live providers, reading `SEMANTICSCHOLAR`, `SERPAPI`, `SERPLY`, `GEMINI`, `OPENREVIEW` and `CROSSREF_MAILTO` from repository secrets. It is `workflow_dispatch` only, holds `permissions: contents: read`, mints no App token and runs no push, so publication is disabled structurally rather than by a flag. Its final step fails the job unless the run reports `complete` with zero unresolved tasks. The workflow has never been dispatched, so no run identifier or terminal ledger state can be cited here. |
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
untracked and no commit in this repository touches it. GitHub can only run a workflow that exists on
the remote, and `workflow_dispatch` additionally only surfaces once the workflow is on the default
branch, so non-execution follows from the file's state rather than from nobody remembering a run.
Both checks are repository-derived.

```bash
git ls-files --error-unmatch .github/workflows/refresh-shadow.yml   # exits non-zero, file is untracked
git log --all --oneline -- .github/workflows/refresh-shadow.yml     # prints nothing, no commit carries it
```

This also means the workflow is not yet dispatchable. It becomes so only after it is committed, merged
and present on the default branch, which is a further step and not a property this checkout has.

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
