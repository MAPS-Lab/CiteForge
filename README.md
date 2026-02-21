<h1 align="center">CiteForge</h1>

<p align="center">
  <a href="https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml"><img src="https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License"></a>
</p>

---

## Why?

Researchers routinely maintain BibTeX files for their publications and collaborators, but keeping citation metadata accurate and complete is tedious. Google Scholar entries are often truncated, missing DOIs, or formatted inconsistently. Manually cross-referencing Crossref, Semantic Scholar, arXiv, PubMed, and other databases to fill in gaps is time-consuming and error-prone, especially for authors with large publication records spanning multiple venues and disciplines.

## What?

CiteForge automates this process by querying 13 academic APIs, deduplicating results through fuzzy title and author matching, and merging metadata according to a trust hierarchy that prioritizes authoritative sources (DOI resolvers, PubMed) over less reliable ones (web-scraped Scholar pages). The output is a set of clean, enriched BibTeX files organized by author, ready for use in LaTeX workflows.

## How?

Given a CSV of authors with their Google Scholar and DBLP identifiers, CiteForge fetches each author's publications and processes every article through a four-phase enrichment pipeline: DOI validation, parallel API enrichment, late DOI discovery, and trust-based metadata merging. Authors are processed concurrently using a thread pool, while API responses are cached locally to minimize redundant requests. The result is a per-author directory of BibTeX files alongside a CSV summary reporting enrichment coverage.

## Quick Start

You'll need Python 3.10+ and a [SerpAPI](https://serpapi.com/) key for Google Scholar access.

```bash
git clone https://github.com/gabrielspadon/CiteForge.git && cd CiteForge
pip install -e .
```

Set up API keys:

```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key          # Required
echo "your_semantic_key" > keys/Semantic.key        # Recommended
echo "your_gemini_key" > keys/Gemini.key            # Optional
```

Create `data/input.csv` with authors to process:

```csv
Name,Scholar Link,DBLP Link
John Smith,https://scholar.google.com/citations?user=ABC123,https://dblp.org/pid/smith/john
```

Run:

```bash
python3 main.py
```

Output is organized by author:

```
output/
├── run.log
├── summary.csv
└── John_Smith (ABC123)/
    ├── author.log
    ├── Smith2024-DeepLearning.bib
    └── ...
```

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
| `src/clients/scholar.py` | Google Scholar (SerpAPI) and DBLP clients |
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
| `src/api_utils.py` | API utility helpers |

## Data Sources

| Source | API Key |
|--------|---------|
| Google Scholar (SerpAPI) | Required |
| Semantic Scholar | Recommended |
| Crossref, OpenAlex, arXiv, PubMed, Europe PMC, DataCite, ORCID, DBLP, DOI resolver | None |
| OpenReview | Optional |
| Google Gemini (citation key generation) | Optional |

When merging, fields are prioritized by source reliability: DOI resolvers (CSL-JSON, BibTeX) > curated databases (DataCite, PubMed, Europe PMC) > broad registries (Crossref, OpenAlex, Semantic Scholar) > author-verified (ORCID) > community platforms (OpenReview, arXiv) > web-scraped (Scholar).

### Data Quality Rules

The merge engine applies several data quality rules beyond trust ordering:

- **DOI selection**: Published DOIs are preferred over preprint (arXiv, PsyArXiv) and data repository (Figshare, Zenodo) DOIs
- **Pages validation**: SAGE/Wiley article IDs (16+ digit numeric strings) are rejected; only real page numbers accepted
- **Journal protection**: Published journal names are never downgraded to preprint server names
- **Booktitle resolution**: Generic series names (e.g., "Lecture Notes in Computer Science") are replaced with actual conference names when available from enrichment sources
- **Title sanitization**: Publisher PDF artifacts (e.g., "Check for updates" prefix) and HTML entities are cleaned
- **Minimum title length**: Single-word titles (Scholar scraping artifacts) are rejected
- **arXiv consistency**: Pure arXiv preprints are consistently typed as @article with `journal = {arXiv e-prints}`
- **Multi-signal dedup**: Entries with strong author overlap (>=90%) and moderate title similarity are caught by composite scoring

## Configuration

All parameters live in `src/config.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CONTRIBUTION_WINDOW_YEARS` | `5` | Years of publications to fetch |
| `PUBLICATIONS_PER_YEAR` | `50` | Target publications per year |
| `SIM_MERGE_DUPLICATE_THRESHOLD` | `0.9` | Title similarity for deduplication |
| `REQUEST_DELAY_BETWEEN_ARTICLES` | `0.5` | Delay between API requests (seconds) |
| `SKIP_SERPAPI_FOR_EXISTING_FILES` | `True` | Reuse existing BibTeX files as seeds |
| `CACHE_ENABLED` | `True` | Enable local API response caching |
| `PAGES_MAX_DIGITS` | `8` | Max digits per page component (rejects article IDs) |
| `MIN_TITLE_WORDS` | `2` | Minimum words in title (rejects artifacts) |

## Testing

```bash
pip install -e .[dev]
pytest tests/ -v --tb=short
```

Test modules:

| Module | Coverage |
|--------|----------|
| `tests/test_core.py` | Core logic: normalization, parsing, matching, merge |
| `tests/test_apis.py` | API client connectivity and response building |
| `tests/test_pipeline.py` | DOI validation and pipeline processing |
| `tests/test_regression.py` | Regression tests: parser edge cases, data quality fixes |
| `tests/test_deduplication.py` | File-level deduplication |
| `tests/test_integration.py` | End-to-end enrichment pipeline |
| `tests/test_cache.py` | Disk cache operations and expiry |
| `tests/test_io_csv.py` | CSV I/O operations |
| `tests/test_config.py` | Configuration validation |

Integration tests that require API keys are automatically skipped when keys are unavailable.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Run quality gates: `ruff check src/ tests/ main.py`, `mypy src/ main.py`, `pytest tests/ -v --tb=short`
4. Submit a pull request

## Citation

If you use CiteForge in your research, please cite it:

```bibtex
@software{spadon_citeforge,
  author    = {Spadon, Gabriel},
  title     = {CiteForge},
  url       = {https://github.com/gabrielspadon/CiteForge},
  license   = {MIT}
}
```

## License

MIT License — see [LICENSE](LICENSE) for details.
