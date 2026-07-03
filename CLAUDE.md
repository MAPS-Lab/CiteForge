# CLAUDE.md

Project overview and data sources: @README.md

## Build & Run

```bash
pip install -e .          # Install (editable)
pip install -e .[dev]     # Install with dev tools
python3 main.py           # Run pipeline (default: data/input.csv)
python3 main.py data/custom.csv   # Custom input
python3 main.py --force   # Force re-enrichment (ignore cache completeness)
```

## Quality Gates

All three must pass before merge:

```bash
ruff check citeforge/ tests/ main.py      # Lint
mypy citeforge/ main.py                    # Type check (strict, ignore_missing_imports)
pytest tests/ -v --tb=short          # Tests (full suite, Python 3.10-3.13)
```

Single test: `pytest tests/test_merge.py::test_function_name -v --tb=short`

Ruff config: line-length 120, rules E/F/W/I/N/UP/B/C4/SIM/RUF/S (see pyproject.toml for ignores).

## Architecture

`main.py` is a thin command-line entry point (~120 LOC) that loads API keys, reads author records, and delegates to the `citeforge/pipeline/` package (`article.py` for per-article enrichment, `scheduler.py` for author-level scheduling, `postrun.py` for the post-run tail). Each article passes through Phase 1 (DOI validation) → Phase 2 (multi-API enrichment) → Phase 2.5 (SerpAPI publication string fallback) → Phase 3 (late DOI inference) → Phase 4 (trust-based merge + save). Post-run steps run in order: flush CSV → reconcile phantoms → remove orphans → year-window cleanup → post-run fixup → build a2i2 → rebuild baseline.json → rebuild badges.json.

Trust hierarchy in `citeforge/merge_utils.py:merge_with_policy()` merges fields from 13 ranked sources with special override rules for DOI (published > preprint), journal (never downgrade to preprint), title (prefer longer), pages (reject invalid), and booktitle (upgrade generic series to conference name).

## Three-Way Fix Pattern (CRITICAL)

**Fixes for entry types, titles, and booktitles MUST be applied in three places or oscillation occurs between consecutive runs:**

1. `_fixup_bib_entry()` — initial fixup on load
2. Existing-file fixup in `process_article()` — before enrichment
3. Phase 4 post-merge — after trust-based merge

Consolidated helpers `_fix_title_text()` and `_apply_booktitle_fixups()` are called from all three. When adding a new text or type fix, grep for these functions and add the fix to all call sites.

The following fixes are also applied in all 3 locations:
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
- **Orphan safety**: Never blindly delete orphan .bib files — verify as duplicates via `title_similarity >= 0.95`.
- **CSV paths**: Relative to CWD (must run from project root).
- **Fused compounds**: Never use `---` (em-dash) or accented characters in `FUSED_COMPOUND_WORDS` or `ABBREVIATED_VENUE_MAP` — serializer strips them.

## Testing Patterns

- Tests in `tests/` mirror `citeforge/` modules (e.g., `test_merge.py` tests `merge_utils.py`)
- `tests/conftest.py` + `tests/fixtures.py` provide shared fixtures
- Integration tests requiring API keys auto-skip when keys unavailable
- Use `monkeypatch` for HTTP mocking; never make real API calls in unit tests
- Do NOT create automated audit modules. Fix issues via pipeline code or direct .bib edits.
