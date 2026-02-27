# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

---

## Project Overview

**CiteForge** is a Python-based academic citation enrichment pipeline that automates collection, validation, deduplication, and merging of bibliographic metadata from 13 scholarly APIs. Given a CSV of authors (with Google Scholar and DBLP identifiers), it fetches publications, enriches each entry by querying multiple academic databases, and produces clean, deduplicated BibTeX files per author alongside an enrichment summary CSV.

**Maintained by**: MAPS Lab, Dalhousie University
**License**: MIT

---

## Build Commands

```bash
# Install (editable with dev dependencies)
pip install -e .[dev]

# Run pipeline
python3 main.py

# Quality gates
ruff check src/ tests/ main.py             # Lint (strict mode)
ruff check src/ tests/ main.py --fix       # Auto-fix lint
mypy src/ main.py                           # Type check (strict)
pytest tests/ -v --tb=short                 # Tests (verbose)
pytest tests/ -v --tb=short -x             # Tests (stop on first failure)
```

---

## Architecture

```
data/input.csv → main.py (ThreadPoolExecutor, 12 workers)
  → per-author: fetch Scholar + DBLP publications
    → per-article: 4-phase pipeline
      Phase 1: DOI Validation (CSL → BibTeX fallback)
      Phase 2: API Enrichment (S2, Crossref, arXiv, OpenReview, OpenAlex, PubMed, Europe PMC)
      Phase 3: Late DOI Discovery (published DOIs preferred over preprint/data DOIs)
      Phase 4: Trust-Based Merge → Save BibTeX (with file-level deduplication)
  → output/{author_id}/*.bib + output/summary.csv
```

### Module Layout

| Module | Purpose |
|--------|---------|
| `main.py` | Orchestrator: ThreadPoolExecutor, 4-phase pipeline, CLI entry |
| `src/clients/scholar.py` | Google Scholar facade (SerpAPI + Serply) |
| `src/clients/serpapi_scholar.py` | Author publications via SerpAPI REST API (serpapi.com) |
| `src/clients/serply_scholar.py` | Citation details via Serply REST API (api.serply.io) |
| `src/clients/search_apis.py` | Search API clients (S2, Crossref, arXiv, OpenReview, OpenAlex, PubMed, Europe PMC) |
| `src/clients/utility_apis.py` | Utility API clients (DataCite, ORCID, DOI resolvers) |
| `src/clients/helpers.py` | Shared client helpers (scoring, deduplication) |
| `src/api_generics.py` | Generic search/build abstractions (APISearchConfig, APIFieldMapping) |
| `src/api_configs.py` | Per-API field mapping configurations |
| `src/merge_utils.py` | Trust hierarchy merge, save_entry_to_file, file-level dedup |
| `src/bibtex_utils.py` | BibTeX parse/serialize, citation keys, entry-level dedup |
| `src/bibtex_build.py` | Entry building, scoring factory, type determination |
| `src/text_utils.py` | LaTeX/Unicode normalization, similarity, author parsing |
| `src/doi_utils.py` | DOI validation, CSL/BibTeX fallback chain |
| `src/id_utils.py` | DOI normalization/classification, arXiv ID extraction |
| `src/http_utils.py` | HTTP session, retry, exponential backoff, rate-limit |
| `src/cache.py` | Disk-based API response cache with monthly expiry |
| `src/config.py` | All thresholds, trust order, HTTP params, API URLs |
| `src/io_utils.py` | CSV I/O, key loading, thread-safe file helpers |
| `src/log_utils.py` | Thread-local logging, per-author log files |
| `src/models.py` | Record dataclass, EnrichmentSource enum |
| `src/exceptions.py` | Error hierarchy tuples |

---

## Key Conventions

### Trust Hierarchy (14-level, highest to lowest)

CSL > doi_bibtex > datacite > pubmed > europepmc > crossref > openalex > s2 > orcid > openreview > arxiv > scholar_page > scholar_min

Higher-ranked sources override lower per-field, with special rules:
- **DOI**: Prefer published DOIs; preprint (arXiv, PsyArXiv) and data repository (Figshare, Zenodo) DOIs deprioritized via `is_secondary_doi()`
- **Journal**: Never downgrade published journal to preprint server name
- **Title**: Prefer longer unless new source 3+ ranks higher; "Check for updates" artifacts stripped
- **Pages**: Must be numeric; SAGE/Wiley article IDs (>8 digits per component) rejected
- **Booktitle**: Generic series names (LNCS, etc.) replaced by actual conference name from any enricher, regardless of trust rank

### Data Quality Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `DATA_DOI_PREFIXES` | Figshare, Zenodo | DOI prefixes deprioritized in selection |
| `PAGES_MAX_DIGITS` | 8 | Max digits per page component (rejects article IDs) |
| `MIN_TITLE_WORDS` | 2 | Minimum words for valid title (rejects artifacts) |
| `GENERIC_SERIES_NAMES` | LNCS, LNAI, LNBIP, LNNS, CCIS, AISC | Series names replaced with conference names |

### Similarity Thresholds

| Constant | Value | Used For |
|----------|-------|----------|
| `SIM_TITLE_WEIGHT` | 0.7 | Title component in scoring |
| `SIM_AUTHOR_BONUS` | 0.2 | Author match bonus |
| `SIM_YEAR_BONUS` | 0.2 | Year match bonus (within +/-1) |
| `SIM_TITLE_SIM_MIN` | 0.8 | Minimum title sim to consider |
| `SIM_EXACT_PICK_THRESHOLD` | 0.9 | Auto-accept candidate |
| `SIM_MERGE_DUPLICATE_THRESHOLD` | 0.95 | Merge-level dedup |
| File-level dedup | 0.95 | save_entry_to_file dedup |

### Multi-Signal Deduplication

`bibtex_entries_match_strict` (bibtex_utils.py) uses composite scoring for duplicate detection. The gate allows composite scoring when:
- Preprint/published pair detected, OR
- External IDs match (DOI, arXiv, PubMed), OR
- Strong author overlap (>=90%) with moderate title similarity (>=0.6) and multi-author entries (>=2 authors on both sides)

File-level dedup in `save_entry_to_file` (merge_utils.py) applies the same strong-author-overlap gate as a second safety layer. Additional file-level checks:
- **DOI version matching**: DOIs with version suffixes (.v1/.v2) are matched as the same work via `doi_bases_match()` in id_utils.py
- **Candidate DOI guard**: Before accepting a Phase-3 DOI candidate on disk, title similarity is verified (>= `SIM_PREPRINT_TITLE_THRESHOLD`). Mis-attributed DOIs are reverted to the Phase-1-validated DOI via `_revert_misattributed_doi()` in main.py

### Threading Model

- `ThreadPoolExecutor(max_workers=12)` for author-level parallelism
- Per-author directory isolation (no cross-thread file conflicts)
- `_CSV_LOCK` (threading.Lock) for thread-safe summary.csv appends
- Thread-local logging via `ThreadLocalFileHandler`
- `REQUEST_DELAY_BETWEEN_ARTICLES = 0.5s` courtesy delay

### BibTeX Citation Keys

Format: `LastnameYear:ShortTitle` (e.g., `Smith2024:MachineLearning`)
- Gemini-generated CamelCase short titles (cached via ResponseCache)
- Fallback: algorithmic stop-word filtering
- Collision retry with increasing word count

### arXiv Entry Consistency

Pure arXiv preprints (no published DOI) are typed as `@misc` — arXiv is a preprint server, not a journal. Conference papers that have arXiv preprints retain their `@inproceedings` type with conference booktitle when a published DOI exists. Handled by `normalize_arxiv_metadata()` in id_utils.py and the `article_no_journal→misc` downgrade in main.py.

### Audit Logging

Every pipeline decision produces a DEBUG-level log entry in per-author log files (`output/{Author}/author.log`). Console output stays at INFO level. Categories: AUDIT, MERGE, CLEANUP, DEDUP, CACHE, SCORE, DOI_VAL, ARXIV, PARSE, SERIAL, CITEKEY. An auditor can trace every field value back to its source, every cache hit/miss, every dedup decision, every DOI validation attempt, and every file write/skip.

---

## Quality Gates

All gates must pass before merge:

1. **ruff** — Strict lint (E, F, W, I, N, UP, B, C4, SIM, RUF, S rules)
2. **mypy** — Strict type checking (no implicit optional, warn redundant casts)
3. **pytest** — All tests pass with `--tb=short -v`

---

## API Keys

Stored as plaintext files in `keys/` (gitignored):

| File | Service |
|------|---------|
| `keys/SerpAPI.key` | Author publications via SerpAPI (required) |
| `keys/Serply.key` | Citation details via Serply REST API (optional) |
| `keys/Semantic.key` | Semantic Scholar |
| `keys/OpenReview.key` | OpenReview (username\npassword) |
| `keys/Gemini.key` | Google Gemini (title generation) |

Environment variable: `CROSSREF_MAILTO` for Crossref polite pool.

---

## Key Files

| File | Purpose |
|------|---------|
| `src/config.py` | All thresholds, trust order, HTTP params — single source of truth |
| `src/api_generics.py:68-132` | APISearchConfig + APIFieldMapping dataclasses |
| `src/merge_utils.py:60-608` | merge_with_policy (trust hierarchy engine) |
| `src/merge_utils.py:610-1164` | save_entry_to_file (file-level dedup + write) |
| `src/text_utils.py:130-165` | normalize_title (LaTeX/Unicode pipeline) |
| `src/text_utils.py:171-198` | trim_title_default (artifact stripping) |
| `src/text_utils.py:340-352` | title_similarity (rapidfuzz-based) |
| `src/bibtex_utils.py:128-257` | parse_bibtex_to_dict (dual single/multi-line parser) |
| `src/bibtex_utils.py:690-850` | bibtex_entries_match_strict (multi-signal dedup) |
| `src/id_utils.py:65-78` | is_secondary_doi (DOI classification) |
| `src/id_utils.py:49-57` | doi_bases_match (DOI version variant matching) |
| `main.py:199-229` | _read_doi_from_file + _revert_misattributed_doi helpers |
| `main.py:232-290` | _try_multiple_candidates pattern |
| `main.py:1478-1540` | CANDIDATE_DOI_DEDUP with title guard + DOI revert |
| `main.py:1820-1960` | main() — ThreadPoolExecutor orchestration |

---

## Testing

```bash
pytest tests/ -v --tb=short          # Full suite (318 tests)
pytest tests/test_core.py -v         # Core logic tests
pytest tests/test_regression.py -v   # Regression + data quality tests
pytest tests/test_deduplication.py   # Dedup tests
pytest tests/test_pipeline.py        # Pipeline integration
pytest tests/test_apis.py            # API client tests
pytest tests/test_cache.py           # Cache tests
```

CI runs on Python 3.10, 3.11, 3.12, 3.13 via GitHub Actions.

---

## Compaction Guidance

When context is auto-compacted, preserve: current task description, list of modified files, architectural decisions made this session, and any error patterns being investigated.
