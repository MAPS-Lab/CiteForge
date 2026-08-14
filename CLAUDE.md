# CLAUDE.md

Project overview and data sources: @README.md

## Build & Run

```bash
pip install -e .          # Install (editable)
pip install -e .[dev]     # Install with dev tools
python3 main.py           # Run pipeline (input: data/input.csv)
python3 main.py --force   # Force re-enrichment (ignore cache completeness)
```

For reproducible setup, install the three locked dependency sets instead of
resolving unpinned extras. `requirements-build.lock` pins the build backend and
`uv` compiler, `requirements.lock` pins runtime dependencies, and
`requirements-dev.lock` pins the runtime plus development toolchain.

```bash
python -m pip install --require-hashes -r requirements-build.lock -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps -e .
```

## Quality Gates

All three must pass before merge.

```bash
ruff check citeforge/ tests/ main.py      # Lint
mypy citeforge/ main.py                    # Type check (strict, ignore_missing_imports)
pytest tests/ -v --tb=short          # Tests (full suite, Python 3.10-3.14)
```

Run a single test with `pytest tests/test_core.py::test_function_name -v --tb=short`.

Ruff uses line-length 120 and rules E/F/W/I/N/UP/B/C4/SIM/RUF/S (see pyproject.toml for ignores).

## Architecture

`citeforge/cli.py` owns argument parsing, API-key and author-record loading, and
pipeline delegation. Root `main.py` is only a compatibility launcher.
`article.py` handles per-article enrichment, `scheduler.py` handles author-level
scheduling, and `postrun.py` handles finalization. Each article passes through
Phase 1 (DOI validation) → Phase 2 (multi-API enrichment) → Phase 2.5
(SerpAPI publication string fallback) → Phase 3 (late DOI inference) → Phase
4 (trust-based merge + save). Post-run steps run in order: flush CSV →
reconcile phantoms → remove duplicate orphans → year-window cleanup →
post-run fixup → remove superseded preprints → build a2i2 → rebuild
baseline.json. Eight steps, and the order is load-bearing. Only the first
three are gated on the summary CSV existing; the rest read `out_dir` and
`records` and always run. `finalize_run` returns a `FinalizationReport` of
counts and raises `FinalizationError` when a step cannot read or write a file
it owns, so a failed cleanup is never silently swallowed.

Trust hierarchy in `citeforge/merge_utils.py:merge_with_policy()` merges fields from active configured sources with special override rules for DOI (published > preprint), journal (never downgrade to preprint), title (prefer longer), pages (reject invalid), and booktitle (upgrade generic series to conference name).

## Refresh Engine (`citeforge/refresh/`, in construction)

A second, separate system that replaced the monthly workflow's whole-corpus
convergence loop with one durable, resumable, completeness-gated generation.
`.github/workflows/monthly-refresh.yml` now restores a sealed checkpoint, runs
exactly one segment, seals again, and opens a pull request only when the ledger
reports the generation complete. It maintains no encrypted response-cache
branch. Do not reintroduce a pass loop, a digest or request-count convergence
test, or a direct push to `main`; each was deleted deliberately and
`tests/test_workflow_contracts.py` asserts their absence.

The engine does not share enrichment code with the legacy pipeline above. The
two meet only at `citeforge/cli.py`, where the `refresh` subcommand
(`--state-dir`, `--checkpoint-dir`, `--generation`) drives `run_refresh`, the
sole production caller of `Ledger.open` and `RefreshEngine`. The bare
`citeforge` command still runs the legacy pipeline. Do not entangle them
further.

`census.py` classifies every physical input row. `ledger.py` is the sole work
and completion authority, one SQLite connection in rollback-journal mode.
`transport.py` performs classified provider sends with exact-request
coalescing. `discovery.py` and `publication_discovery.py` plan and reduce
waves. `checkpoint.py` seals process state under AES-GCM. `engine.py` drives a
segment. `staging.py` builds and validates a candidate tree, `publication.py`
offers it as a pull request. What remains is executing a live shadow
generation. `.github/workflows/refresh-shadow.yml` dispatches one against live
providers with publication structurally disabled, and it has never been run.
Wiring it proved nothing about live providers, so treat every provider
authentication, schema, quota and batch-semantics claim as unverified until a
run can be cited.

Contracts that are load-bearing and easy to break by accident:

- `checkpoint.py` seals an opaque directory and must not import `ledger`,
  `engine`, `corpus`, `authority`, or anything under `citeforge/pipeline/`. It
  binds a generation id, input digest, and policy digest as AAD strings, never
  as ledger rows. That is what lets it seal the SQLite file itself.
- AES-GCM encrypt and decrypt must pass the *same* associated data. Both call
  `binding_bytes()`. Using `to_bytes()` on either side breaks every restore,
  which is a defect that already shipped once and was caught by round-trip
  tests.
- A module returns evidence and the engine writes it to the ledger, never the
  reverse. `record_checkpoint` and `record_materialization` open
  `BEGIN IMMEDIATE` on the one connection, so calling them from inside a module
  that is holding that file is the deadlock shape.
- No code path may produce a push whose refspec resolves to the default branch.
  `tests/test_workflow_contracts.py` asserts this for the workflow, and the
  same assertion belongs on any Python publisher.
- Time and request counts are metrics. Only the ledger decides completeness.

Design contract is `docs/ci-refresh-architecture.md`, task breakdown is
`docs/ci-refresh-implementation-plan.md`, and `docs/ci-refresh-evidence.md`
records per-requirement evidence including what is still unproven.

## Four-Stage Canonicalization Contract (CRITICAL)

Entry-type and text normalization is single-sourced in `citeforge/canonicalize.py`. Callers select one of four ordered `CanonicalStage` rule sets.

1. `LOAD_REPAIR` runs in `process_article()` after an existing file is loaded and before enrichment.
2. `COMPLETE_SKIP_FINALIZE` runs in `process_article()` immediately before writing a complete entry that skips enrichment.
3. `POST_MERGE` runs in `process_article()` after the Phase 4 trust-based merge.
4. `POSTRUN_ORPHAN_REPAIR` runs during the post-run fixup through `_fixup_bib_entry()`.

Each rule belongs to every stage whose path can emit the affected entry. Shared behavior must use one `_rule_*` helper referenced by the applicable stage tuples, not copied logic in callers. When changing a rule, inspect all four stage tuples and add tests for each affected path.

The following fixes share rule helpers across their applicable stages.
- Abbreviated venue expansion (`ABBREVIATED_VENUE_MAP`)
- Venue case correction (`VENUE_CASE_CORRECTIONS`)
- Publisher-duplicate-container stripping (publisher == journal/booktitle → remove publisher)
- `JOURNALS_NAMED_PROCEEDINGS` guard (conference-keyword suffix check before reclassifying)
- `ACM_JOURNAL_PROCEEDINGS` guard (PACM journals excluded from conference-as-journal reclassification)

## Key Conventions

- **Config-driven**: All thresholds, API endpoints, trust order, rate limits, compound word dictionaries live in `citeforge/config.py`. Never hardcode these values elsewhere.
- **Determinism**: Pipeline produces byte-identical output across consecutive cache-hit runs. Use `sorted()` for all directory/file iterations. No randomization in output-affecting code.
- **DOI normalization**: Always use `_norm_doi()` from `citeforge/id_utils.py`. Always pair DOI matches with `title_similarity >= 0.55` check.
- **Preprint detection**: Uses `PREPRINT_SERVERS`, `PREPRINT_DOI_PREFIXES`, and `PREPRINT_ONLY_PUBLISHERS`. Check all three for completeness.
- **Container fields**: `@article` → `journal`, `@inproceedings`/`@incollection` → `booktitle`, `@misc` → `howpublished`. See `get_container_field()`.
- **Repository guard**: `REPOSITORY_AS_JOURNAL` (Zenodo, OSTI, Figshare, etc.) prevents @misc→@inproceedings oscillation.
- **Thesis detection**: @article with "university"/"institut" in journal → @phdthesis.
- **Book-chapter DOI**: `.ch\d+` in DOI → Wiley book chapter → reclassify @article → @incollection.
- **Content comparison guard**: Post-run fixup compares serialized output to existing file before writing, preventing phantom writes from serializer normalization.
- **Cache defensive copy**: `ResponseCache.get()` returns `dict(...)` copy to prevent mutation.
- **Orphan safety**: Never blindly delete orphan .bib files; verify as duplicates via `title_similarity >= 0.95`.
- **CSV paths**: Relative to CWD (must run from project root).
- **Fused compounds**: Never use `---` (em-dash) or accented characters in `FUSED_COMPOUND_WORDS` or `ABBREVIATED_VENUE_MAP` (the serializer strips them).

## Testing Patterns

- Tests in `tests/` mostly mirror `citeforge/` modules (e.g., `test_canonicalize.py` tests `canonicalize.py`); merge coverage lives in `test_deduplication.py`, `test_save_entry.py`, and `test_core.py`
- `tests/conftest.py` owns shared fixtures and the independent BibTeX field extractor
- Legacy-pipeline modules use flat test functions. The newer refresh modules group with `class Test*` (see `test_refresh_checkpoint.py`, `test_refresh_staging.py`, `test_refresh_publication.py`). Follow the neighbouring file rather than mixing both styles in one module
- Refresh tests use module-local `_`-prefixed builders instead of adding conftest fixtures, and take only `tmp_path` and `monkeypatch` from pytest
- Integration tests requiring API keys auto-skip when keys unavailable
- Use `monkeypatch` for HTTP mocking; never make real API calls in unit tests
- Do NOT create automated audit modules. Fix issues via pipeline code or direct .bib edits.
