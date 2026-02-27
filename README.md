<h1 align="center">CiteForge</h1>

<p align="center">
  <strong>Automated academic citation enrichment from 13 scholarly APIs.</strong>
</p>

<p align="center">
  <a href="https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml"><img src="https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License"></a>
</p>

<p align="center">
  CiteForge fetches, validates, deduplicates, and merges bibliographic metadata so you don't have to.<br>
  Given a list of authors, it produces clean BibTeX files ready for LaTeX.
</p>

---

## The Problem

Google Scholar entries are often truncated, missing DOIs, or formatted inconsistently. Manually cross-referencing Crossref, Semantic Scholar, arXiv, PubMed, and other databases is tedious and error-prone — especially for authors with large publication records across multiple venues.

## The Solution

CiteForge uses [SerpAPI](https://serpapi.com/) for author publication retrieval and [Serply](https://serply.io/) for citation detail, queries 13 academic APIs, deduplicates results through fuzzy title and author matching, and merges metadata using a trust hierarchy that prefers authoritative sources (DOI resolvers, PubMed) over less reliable ones (web-scraped Scholar pages).

---

## Getting Started

**Requirements:** Python 3.10+. A [SerpAPI](https://serpapi.com/) key is required for Google Scholar author profiles.

```bash
git clone https://github.com/gabrielspadon/CiteForge.git && cd CiteForge
pip install -e .
```

Set up API keys:

```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key           # Required (Scholar profiles)
echo "your_serply_key" > keys/Serply.key             # Optional (citation detail)
echo "your_semantic_key" > keys/Semantic.key         # Recommended
echo "your_gemini_key" > keys/Gemini.key             # Optional (citation key generation)
printf "username\npassword" > keys/OpenReview.key     # Optional
```

Create `data/input.csv`:

```csv
Name,Scholar Link,DBLP Link
John Smith,https://scholar.google.com/citations?user=ABC123,https://dblp.org/pid/smith/john
```

Run the pipeline:

```bash
python3 main.py                    # Process all authors
python3 main.py --force            # Force re-enrichment of existing files
python3 main.py data/custom.csv    # Use a custom input file
```

Output:

```
output/
├── baseline.json              # Per-author file counts for regression checks
├── run.log                    # Pipeline execution log
├── summary.csv                # Enrichment summary (sources used per file)
└── Smith (ABC123)/
    ├── author.log             # Per-author debug log (AUDIT, MERGE, DEDUP, ...)
    ├── Smith2024-DeepLearning.bib
    └── ...
```

### Cache and Output

API responses are cached locally under `data/api_cache/` with monthly expiry (resets on the 1st of each month, AST). Both `data/api_cache/` and `output/` are populated on first run. Subsequent runs reuse cached responses for deterministic output.

---

## How It Works

Each article goes through a **four-phase enrichment pipeline**:

```
data/input.csv --> main.py (ThreadPoolExecutor, 12 workers)
  --> per author: fetch Scholar + DBLP publications
    --> per article: 4-phase pipeline
      Phase 1: DOI Validation (CSL-JSON --> BibTeX fallback)
      Phase 2: API Enrichment (S2, Crossref, arXiv, OpenReview, OpenAlex, PubMed, Europe PMC)
      Phase 3: Late DOI Discovery (published DOIs preferred over preprint/data DOIs)
      Phase 4: Trust-Based Merge --> Save BibTeX (with file-level deduplication)
  --> output/{author_id}/*.bib + output/summary.csv
```

1. **DOI Validation** — Resolve DOIs via CSL-JSON and BibTeX fallback
2. **API Enrichment** — Query Semantic Scholar, Crossref, arXiv, OpenReview, OpenAlex, PubMed, and Europe PMC; score and select best candidate per API via composite scoring (title similarity + author overlap + year match)
3. **Late DOI Discovery** — Collect DOI candidates from all enrichment sources, prefer published over preprint; guard against mis-attributed DOIs via title similarity verification
4. **Trust-Based Merge** — Combine fields using a 14-level source hierarchy, then deduplicate at file level via multi-signal matching (DOI exact, DOI version, external IDs, title similarity, author overlap)

### Trust Hierarchy

Fields from higher-ranked sources override lower ones:

> CSL > DOI BibTeX > DataCite > PubMed > Europe PMC > Crossref > OpenAlex > Semantic Scholar > ORCID > OpenReview > arXiv > Scholar Page > Scholar Minimal

Special field rules override raw trust rank:
- **DOI**: Published DOIs always beat preprint/data repository DOIs (arXiv, Figshare, Zenodo)
- **Journal**: Never downgraded from a published journal to a preprint server name
- **Title**: Prefer longer unless new source is 3+ ranks higher
- **Booktitle**: Generic series names (LNCS, LNAI, etc.) always replaced with actual conference names

### Deduplication

CiteForge applies multi-level deduplication:

| Level | Signal | Threshold |
|-------|--------|:---------:|
| DOI exact | Identical normalized DOIs | exact |
| DOI version | Same DOI base with version suffix (.v1/.v2) | exact |
| External IDs | Matching arXiv eprint, PubMed ID, or OpenAlex ID | exact |
| High title similarity | rapidfuzz token_set_ratio | >= 0.95 |
| Preprint/published pair | One preprint DOI + one published DOI + title match | >= 0.55 |
| Strong author overlap | >= 90% author Jaccard + moderate title match | >= 0.60 |
| Candidate DOI guard | Title verification before accepting API-discovered DOIs | >= 0.55 |

### Data Quality

The merge engine enforces additional rules:

- Reject SAGE/Wiley article IDs masquerading as page numbers (> 8 digits per component)
- Replace generic series names ("Lecture Notes in Computer Science") with actual conference names
- Strip publisher PDF artifacts ("Check for updates") and decode HTML entities
- Reject single-word and garbage titles (Scholar scraping artifacts, institutional addresses)
- Reclassify conference-as-journal entries (Crossref registers some proceedings as journals)
- Normalize ALL-CAPS titles to title case
- Fix lowercase author name capitalization
- Revert mis-attributed DOIs when title similarity check fails against existing files

### Determinism

On cache-hit runs (all API responses cached), CiteForge produces byte-identical output. Verified via SHA256 hashing of all output files across 8+ consecutive runs with 0 differences.

---

## Data Sources

| Source | Key Required | Used For |
|--------|:---:|---------|
| Google Scholar ([SerpAPI](https://serpapi.com/)) | Required | Author publication lists |
| [Serply](https://serply.io/) | Optional | Citation detail lookups |
| [Semantic Scholar](https://www.semanticscholar.org/) | Recommended | Paper metadata, references |
| [Crossref](https://www.crossref.org/) | No (mailto recommended) | DOI metadata, container titles |
| [OpenAlex](https://openalex.org/) | No | Open scholarly metadata |
| [arXiv](https://arxiv.org/) | No | Preprint metadata, eprint IDs |
| [PubMed](https://pubmed.ncbi.nlm.nih.gov/) | No | Biomedical citations |
| [Europe PMC](https://europepmc.org/) | No | European biomedical citations |
| [DataCite](https://datacite.org/) | No | Dataset/software DOIs |
| [ORCID](https://orcid.org/) | No | Author publication lists |
| [DBLP](https://dblp.org/) | No | CS bibliography supplement |
| [OpenReview](https://openreview.net/) | Optional | Conference paper metadata |
| [Google Gemini](https://ai.google.dev/) | Optional | CamelCase citation key generation |

Set `CROSSREF_MAILTO` environment variable for Crossref polite pool access.

---

## Configuration

All parameters live in [`src/config.py`](src/config.py):

| Parameter | Default | Description |
|-----------|:-------:|-------------|
| `MAX_WORKERS` | 12 | Parallel author processing threads |
| `CONTRIBUTION_WINDOW_YEARS` | 5 | Years of publications to fetch |
| `PUBLICATIONS_PER_YEAR` | 50 | Target publications per year |
| `SIM_MERGE_DUPLICATE_THRESHOLD` | 0.95 | Title similarity for file-level dedup |
| `SIM_PREPRINT_TITLE_THRESHOLD` | 0.55 | Relaxed threshold for preprint/published pairs |
| `SIM_PREPRINT_TOKEN_OVERLAP_MIN` | 0.30 | Minimum distinctive-token overlap for preprint pair matching |
| `REQUEST_DELAY_MIN` / `_MAX` | 0.1 / 0.5s | Courtesy delay range between API requests |
| `CACHE_ENABLED` | True | Enable local API response caching |
| `TRUST_ORDER` | 14 levels | Source priority for field-by-field merge |

---

## Testing

```bash
pip install -e .[dev]
pytest tests/ -v --tb=short          # Full suite
pytest tests/test_core.py -v         # Core logic
pytest tests/test_regression.py -v   # Regression + data quality
pytest tests/test_deduplication.py   # Dedup tests
pytest tests/test_pipeline.py        # Pipeline integration
```

318 tests across 14 modules. Integration tests that require API keys are automatically skipped when keys are unavailable. CI runs on Python 3.10, 3.11, 3.12, and 3.13.

### Quality Gates

All gates must pass before merge:

```bash
ruff check src/ tests/ main.py             # Lint (strict mode)
mypy src/ main.py                           # Type check (strict)
pytest tests/ -v --tb=short                 # Tests
```

---

<details>
<summary><strong>Module Layout</strong></summary>

| Module | Purpose |
|--------|---------|
| `main.py` | Orchestrator: thread pool, 4-phase pipeline, CLI entry |
| `src/clients/scholar.py` | Google Scholar facade (SerpAPI + Serply backends) |
| `src/clients/serpapi_scholar.py` | SerpAPI client for author publication retrieval |
| `src/clients/serply_scholar.py` | Serply REST API client for citation detail |
| `src/clients/search_apis.py` | Search API clients (S2, Crossref, arXiv, OpenReview, OpenAlex, PubMed, Europe PMC) |
| `src/clients/utility_apis.py` | Utility API clients (DataCite, ORCID, DOI resolvers) |
| `src/clients/helpers.py` | Shared client helpers (scoring, deduplication) |
| `src/api_generics.py` | Generic search/build abstractions (APISearchConfig, APIFieldMapping) |
| `src/api_configs.py` | Per-API field mapping configurations |
| `src/merge_utils.py` | Trust hierarchy merge, file save, multi-level deduplication |
| `src/bibtex_utils.py` | BibTeX parsing, serialization, multi-signal entry matching |
| `src/bibtex_build.py` | Entry building, scoring factory, type determination |
| `src/text_utils.py` | LaTeX/Unicode normalization, title similarity, author parsing |
| `src/doi_utils.py` | DOI validation, CSL/BibTeX fallback chain |
| `src/id_utils.py` | DOI normalization/classification, arXiv ID extraction, DOI version matching |
| `src/cache.py` | Disk-based API response cache with monthly expiry |
| `src/config.py` | All thresholds, trust order, HTTP params, API URLs |
| `src/http_utils.py` | HTTP session, retry, exponential backoff, token bucket rate limiting |
| `src/io_utils.py` | CSV I/O, key loading, thread-safe file helpers, phantom/orphan reconciliation |
| `src/log_utils.py` | Thread-local logging, per-author log files, category-based filtering |
| `src/models.py` | Record dataclass, EnrichmentSource enum |
| `src/exceptions.py` | Error hierarchy tuples |

</details>

<details>
<summary><strong>Audit Logging</strong></summary>

Every pipeline decision produces a DEBUG-level log entry in per-author log files (`output/{Author}/author.log`). Console output stays at INFO level. Log categories:

| Category | What is logged |
|----------|---------------|
| `AUDIT` | Phase transitions, field source tracking |
| `MERGE` | Field-level merge decisions (accept/keep/replace) |
| `DEDUP` | Duplicate detection, DOI matching, file scan results |
| `CLEANUP` | File deletion/rename, phantom/orphan reconciliation |
| `CACHE` | Cache hits/misses per namespace |
| `SCORE` | Candidate scoring, similarity computations |
| `DOI_VAL` | DOI validation attempts and results |
| `CITEKEY` | Citation key generation (Gemini/algorithmic) |

An auditor can trace every field value back to its source, every cache hit/miss, every dedup decision, every DOI validation attempt, and every file write/skip.

</details>

---

## Contributing

1. Fork the repository
2. Create a feature branch
3. Run quality gates: `ruff check src/ tests/ main.py`, `mypy src/ main.py`, `pytest`
4. Submit a pull request

## Citation

```bibtex
@software{spadon_citeforge,
  author  = {Spadon, Gabriel},
  title   = {CiteForge},
  url     = {https://github.com/gabrielspadon/CiteForge},
  license = {MIT}
}
```

## License

[MIT](LICENSE)
