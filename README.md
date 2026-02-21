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

## Data Sources

| Source | API Key |
|--------|---------|
| Google Scholar (SerpAPI) | Required |
| Semantic Scholar | Recommended |
| Crossref, OpenAlex, arXiv, PubMed, Europe PMC, DataCite, ORCID, DBLP, DOI resolver | None |
| OpenReview | Optional |
| Google Gemini (citation key generation) | Optional |

When merging, fields are prioritized by source reliability: DOI resolvers (CSL-JSON, BibTeX) > curated databases (DataCite, PubMed, Europe PMC) > broad registries (Crossref, OpenAlex, Semantic Scholar) > author-verified (ORCID) > community platforms (OpenReview, arXiv) > web-scraped (Scholar).

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

## Testing

```bash
pip install -e .[dev]
pytest
```

Integration tests that require API keys are automatically skipped when keys are unavailable.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Run quality gates: `ruff check .`, `mypy .`, `pytest`
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
