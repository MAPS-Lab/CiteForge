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

CiteForge queries 13 academic APIs, deduplicates results through fuzzy title and author matching, and merges metadata using a trust hierarchy that prefers authoritative sources (DOI resolvers, PubMed) over less reliable ones (web-scraped Scholar pages).

---

## Getting Started

**Requirements:** Python 3.10+ and a [SerpAPI](https://serpapi.com/) key.

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

Create `data/input.csv`:

```csv
Name,Scholar Link,DBLP Link
John Smith,https://scholar.google.com/citations?user=ABC123,https://dblp.org/pid/smith/john
```

Run the pipeline:

```bash
python3 main.py
```

Output:

```
output/
├── run.log
├── summary.csv
└── John_Smith (ABC123)/
    ├── author.log
    ├── Smith2024-DeepLearning.bib
    └── ...
```

---

## How It Works

Each article goes through a **four-phase enrichment pipeline**:

1. **DOI Validation** — Resolve DOIs via CSL-JSON and BibTeX fallback
2. **API Enrichment** — Query Semantic Scholar, Crossref, arXiv, OpenReview, OpenAlex, PubMed, and Europe PMC
3. **Late DOI Discovery** — Collect DOIs from matched sources, preferring published over preprint
4. **Trust-Based Merge** — Combine fields using a 14-level source hierarchy, then deduplicate

Authors are processed in parallel (12 workers) and API responses are cached locally.

### Trust Hierarchy

Fields from higher-ranked sources override lower ones:

> CSL > DOI BibTeX > DataCite > PubMed > Europe PMC > Crossref > OpenAlex > Semantic Scholar > ORCID > OpenReview > arXiv > Scholar Page > Scholar Minimal

### Data Quality

The merge engine enforces additional rules:

- Prefer published DOIs over preprint/data repository DOIs (arXiv, Figshare, Zenodo)
- Reject SAGE/Wiley article IDs masquerading as page numbers
- Never downgrade a published journal name to a preprint server
- Replace generic series names ("Lecture Notes in Computer Science") with actual conference names
- Strip publisher PDF artifacts ("Check for updates") and decode HTML entities
- Reject single-word titles (Scholar scraping artifacts)
- Catch near-duplicates via composite scoring (title similarity + author overlap)

---

## Data Sources

| Source | Key Required |
|--------|:---:|
| Google Scholar (via SerpAPI) | Yes |
| Semantic Scholar | Recommended |
| Crossref, OpenAlex, arXiv, PubMed, Europe PMC, DataCite, ORCID, DBLP | No |
| OpenReview | Optional |
| Google Gemini (citation key generation) | Optional |

---

## Configuration

All parameters live in [`src/config.py`](src/config.py):

| Parameter | Default | Description |
|-----------|:-------:|-------------|
| `CONTRIBUTION_WINDOW_YEARS` | 5 | Years of publications to fetch |
| `PUBLICATIONS_PER_YEAR` | 50 | Target publications per year |
| `SIM_MERGE_DUPLICATE_THRESHOLD` | 0.9 | Title similarity for deduplication |
| `REQUEST_DELAY_BETWEEN_ARTICLES` | 0.5s | Courtesy delay between API requests |
| `CACHE_ENABLED` | True | Enable local API response caching |

---

## Testing

```bash
pip install -e .[dev]
pytest tests/ -v --tb=short
```

116 tests across 9 modules. Integration tests that require API keys are automatically skipped when keys are unavailable. CI runs on Python 3.10, 3.11, 3.12, and 3.13.

---

<details>
<summary><strong>Module Layout</strong></summary>

| Module | Purpose |
|--------|---------|
| `main.py` | Orchestrator: thread pool, 4-phase pipeline, CLI entry |
| `src/clients/scholar.py` | Google Scholar (SerpAPI) and DBLP clients |
| `src/clients/search_apis.py` | Search API clients (S2, Crossref, arXiv, OpenReview, OpenAlex, PubMed, Europe PMC) |
| `src/clients/utility_apis.py` | Utility API clients (DataCite, ORCID, DOI resolvers) |
| `src/clients/helpers.py` | Shared client helpers (scoring, deduplication) |
| `src/api_generics.py` | Generic search/build abstractions |
| `src/api_configs.py` | Per-API field mapping configurations |
| `src/merge_utils.py` | Trust hierarchy merge, file save, deduplication |
| `src/bibtex_utils.py` | BibTeX parsing, serialization, citation keys |
| `src/bibtex_build.py` | Entry building, scoring, type determination |
| `src/text_utils.py` | LaTeX/Unicode normalization, similarity, author parsing |
| `src/doi_utils.py` | DOI validation, CSL/BibTeX fallback chain |
| `src/id_utils.py` | DOI classification, arXiv ID extraction |
| `src/cache.py` | Disk-based API response cache |
| `src/config.py` | Thresholds, trust order, HTTP params, API URLs |
| `src/http_utils.py` | HTTP session, retry, exponential backoff |
| `src/io_utils.py` | CSV I/O, key loading, thread-safe file helpers |
| `src/log_utils.py` | Thread-local logging, per-author log files |

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
