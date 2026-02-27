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
  CiteForge fetches, validates, deduplicates, and merges bibliographic metadata<br>
  so you don't have to. Given a list of authors, it produces clean BibTeX files<br>
  ready for LaTeX.
</p>

---

## Overview

Google Scholar entries are often incomplete, missing DOIs, or formatted inconsistently. Manually cross-referencing Crossref, Semantic Scholar, arXiv, PubMed, and other databases is tedious and error-prone, especially for authors with large publication records across multiple venues.

CiteForge automates this process. It retrieves author publications via [SerpAPI](https://serpapi.com/) and [Serply](https://serply.io/), queries 13 academic APIs for metadata, deduplicates results through fuzzy title and author matching, and merges fields using a 14-level trust hierarchy that prefers authoritative sources over less reliable ones.

---

## Getting Started

**Requirements:** Python 3.10+ and a [SerpAPI](https://serpapi.com/) key.

```bash
git clone https://github.com/gabrielspadon/CiteForge.git && cd CiteForge
pip install -e .
```

Place API keys in the `keys/` directory:

```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key        # Required
echo "your_serply_key" > keys/Serply.key          # Optional
echo "your_semantic_key" > keys/Semantic.key      # Recommended
echo "your_gemini_key" > keys/Gemini.key          # Optional
printf "user\npass" > keys/OpenReview.key          # Optional
```

Create an input CSV and run the pipeline:

```bash
python3 main.py                     # Default: data/input.csv
python3 main.py data/custom.csv     # Custom input
python3 main.py --force             # Force re-enrichment
```

The input CSV has three columns:

```csv
Name,Scholar Link,DBLP Link
John Smith,https://scholar.google.com/citations?user=ABC123,https://dblp.org/pid/smith/john
```

Output is organized per author:

```
output/
├── baseline.json
├── run.log
├── summary.csv
└── Smith (ABC123)/
    ├── author.log
    ├── Smith2024-DeepLearning.bib
    └── ...
```

API responses are cached under `data/api_cache/` with monthly expiry. Subsequent runs reuse cached responses for deterministic output.

---

## Pipeline

Each article passes through four phases:

| Phase | Name | Description |
|:-----:|------|-------------|
| 1 | DOI Validation | Resolve DOIs via CSL-JSON with BibTeX fallback |
| 2 | API Enrichment | Query 7 APIs, score and select best candidate per source |
| 3 | Late DOI Discovery | Collect DOI candidates; prefer published over preprint |
| 4 | Trust-Based Merge | Combine fields by source rank, then deduplicate on disk |

Authors are processed in parallel with 12 workers. Per-API token-bucket rate limiting and session rotation prevent throttling.

### Trust Hierarchy

Fields from higher-ranked sources override lower ones:

```
CSL > DOI BibTeX > DataCite > PubMed > Europe PMC > Crossref >
OpenAlex > Semantic Scholar > ORCID > OpenReview > arXiv >
Scholar Page > Scholar Minimal
```

Special rules override raw rank for specific fields:

| Field | Rule |
|-------|------|
| DOI | Published DOIs always beat preprint/data repository DOIs |
| Journal | Never downgraded from published journal to preprint server |
| Title | Prefer longer unless new source is 3+ ranks higher |
| Booktitle | Generic series names (LNCS, etc.) replaced with conference names |

### Deduplication

Multi-level deduplication prevents duplicate entries:

| Signal | Threshold |
|--------|:---------:|
| Identical normalized DOIs | exact |
| DOI version variants (.v1 / .v2) | exact |
| Matching external IDs (arXiv, PubMed, OpenAlex) | exact |
| High title similarity (rapidfuzz token_set_ratio) | 0.95 |
| Preprint/published pair with title match | 0.55 |
| Strong author overlap with moderate title match | 0.60 |

Candidate DOIs discovered during enrichment are verified against existing files via title similarity before acceptance. Mis-attributed DOIs are reverted to the Phase-1-validated DOI.

### Data Quality

The merge engine enforces additional rules:

| Rule | Purpose |
|------|---------|
| Page number validation | Reject article IDs masquerading as pages (> 8 digits) |
| Series name expansion | Replace generic names with actual conference names |
| Artifact stripping | Remove "Check for updates" prefixes, decode HTML entities |
| Title filtering | Reject single-word and garbage titles from Scholar scraping |
| Type reclassification | Fix conference-as-journal entries from Crossref |
| Casing normalization | Fix ALL-CAPS titles and lowercase author names |
| DOI reversion | Undo mis-attributed DOIs when title similarity check fails |

On cache-hit runs, CiteForge produces byte-identical output across consecutive runs.

---

## Data Sources

| Source | Key | Purpose |
|--------|:---:|---------|
| Google Scholar via [SerpAPI](https://serpapi.com/) | Required | Author publication lists |
| [Serply](https://serply.io/) | Optional | Citation detail lookups |
| [Semantic Scholar](https://www.semanticscholar.org/) | Recommended | Paper metadata |
| [Crossref](https://www.crossref.org/) | No | DOI metadata, container titles |
| [OpenAlex](https://openalex.org/) | No | Open scholarly metadata |
| [arXiv](https://arxiv.org/) | No | Preprint metadata, eprint IDs |
| [PubMed](https://pubmed.ncbi.nlm.nih.gov/) | No | Biomedical citations |
| [Europe PMC](https://europepmc.org/) | No | European biomedical citations |
| [DataCite](https://datacite.org/) | No | Dataset and software DOIs |
| [ORCID](https://orcid.org/) | No | Author publication lists |
| [DBLP](https://dblp.org/) | No | CS bibliography supplement |
| [OpenReview](https://openreview.net/) | Optional | Conference paper metadata |
| [Google Gemini](https://ai.google.dev/) | Optional | Citation key generation |

Set the `CROSSREF_MAILTO` environment variable for Crossref polite pool access.

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
| `REQUEST_DELAY_MIN` / `_MAX` | 0.1 / 0.5s | Courtesy delay between API requests |
| `CACHE_ENABLED` | True | Local API response caching |
| `TRUST_ORDER` | 14 levels | Source priority for field-by-field merge |

---

## Testing

```bash
pip install -e .[dev]
pytest tests/ -v --tb=short
```

318 tests across 14 modules. Integration tests requiring API keys are automatically skipped when keys are unavailable. CI runs on Python 3.10, 3.11, 3.12, and 3.13.

All three quality gates must pass before merge:

```bash
ruff check src/ tests/ main.py      # Lint
mypy src/ main.py                    # Type check
pytest tests/ -v --tb=short          # Tests
```

---

<details>
<summary><strong>Module Layout</strong></summary>

| Module | Purpose |
|--------|---------|
| `main.py` | Orchestrator: thread pool, 4-phase pipeline, CLI entry |
| `src/config.py` | Thresholds, trust order, HTTP params, API URLs |
| `src/merge_utils.py` | Trust hierarchy merge, file save, multi-level dedup |
| `src/bibtex_utils.py` | BibTeX parsing, serialization, entry matching |
| `src/bibtex_build.py` | Entry building, scoring factory, type determination |
| `src/text_utils.py` | LaTeX/Unicode normalization, similarity, author parsing |
| `src/doi_utils.py` | DOI validation, CSL/BibTeX fallback chain |
| `src/id_utils.py` | DOI classification, arXiv extraction, version matching |
| `src/api_generics.py` | Generic search/build abstractions |
| `src/api_configs.py` | Per-API field mapping configurations |
| `src/cache.py` | Disk-based API response cache with monthly expiry |
| `src/http_utils.py` | HTTP session, retry, backoff, token bucket rate limiting |
| `src/io_utils.py` | CSV I/O, key loading, phantom/orphan reconciliation |
| `src/log_utils.py` | Thread-local logging, per-author log files |
| `src/models.py` | Record dataclass, EnrichmentSource enum |
| `src/exceptions.py` | Error hierarchy tuples |
| `src/clients/scholar.py` | Google Scholar facade (SerpAPI + Serply) |
| `src/clients/serpapi_scholar.py` | SerpAPI author publication retrieval |
| `src/clients/serply_scholar.py` | Serply citation detail lookups |
| `src/clients/search_apis.py` | S2, Crossref, arXiv, OpenReview, OpenAlex, PubMed, Europe PMC |
| `src/clients/utility_apis.py` | DataCite, ORCID, DOI resolvers |
| `src/clients/helpers.py` | Shared scoring and deduplication helpers |

</details>

<details>
<summary><strong>Audit Logging</strong></summary>

Every pipeline decision is logged at DEBUG level in per-author log files (`output/{Author}/author.log`). Console output stays at INFO level.

| Category | Content |
|----------|---------|
| `AUDIT` | Phase transitions, field source tracking |
| `MERGE` | Field-level merge decisions |
| `DEDUP` | Duplicate detection, DOI matching, file scan results |
| `CLEANUP` | File deletion/rename, phantom/orphan reconciliation |
| `CACHE` | Cache hits and misses per namespace |
| `SCORE` | Candidate scoring, similarity computations |
| `DOI_VAL` | DOI validation attempts and results |
| `CITEKEY` | Citation key generation |

</details>

---

## Contributing

1. Fork the repository
2. Create a feature branch
3. Pass all quality gates
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
