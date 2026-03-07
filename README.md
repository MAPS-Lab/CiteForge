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

## Why CiteForge?

If you've ever tried to build a comprehensive publication list for a research group, you know the pain. Google Scholar entries are often incomplete — missing DOIs, inconsistent formatting, broken author names. Manually cross-referencing Crossref, Semantic Scholar, arXiv, PubMed, and a handful of other databases gets tedious fast, especially when you're dealing with dozens of authors and hundreds of papers.

CiteForge takes care of this. Point it at a list of authors with their Google Scholar profiles, and it will:

- Pull every publication from Scholar via [SerpAPI](https://serpapi.com/) (with [Serply](https://serply.io/) as backup)
- Query **13 academic APIs** for richer metadata on each paper
- Deduplicate results using fuzzy title matching, DOI normalization, and author overlap
- Merge fields using a **13-level trust hierarchy** that prefers authoritative sources
- Output clean, LaTeX-ready `.bib` files organized by author

The result is deterministic — on cache-hit runs, CiteForge produces **byte-identical output** across consecutive runs, verified by SHA-256 checksums.

---

## Getting Started

You'll need **Python 3.10+** and a [SerpAPI](https://serpapi.com/) key (free tier works for small runs).

```bash
git clone https://github.com/gabrielspadon/CiteForge.git && cd CiteForge
pip install -e .
```

Drop your API keys into the `keys/` directory:

```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key        # Required
echo "your_serply_key" > keys/Serply.key          # Optional
echo "your_semantic_key" > keys/Semantic.key      # Recommended
echo "your_gemini_key" > keys/Gemini.key          # Optional
printf "user\npass" > keys/OpenReview.key          # Optional
```

Then create an input CSV and run:

```bash
python3 main.py                     # Default: data/input.csv
python3 main.py data/custom.csv     # Custom input
python3 main.py --force             # Force re-enrichment
```

The input CSV has three columns — name, Scholar link, and an optional DBLP link:

```csv
Name,Scholar Link,DBLP Link
John Smith,https://scholar.google.com/citations?user=ABC123,https://dblp.org/pid/smith/john
```

Output is organized per author, with a shared summary and deduplication log:

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

API responses are cached under `data/api_cache/` with monthly expiry, so subsequent runs are fast and deterministic.

---

## How It Works

Each article passes through five enrichment phases:

| Phase | What happens |
|:-----:|--------------|
| 1 | **DOI Validation** — resolve DOIs via CSL-JSON, fall back to BibTeX |
| 2 | **API Enrichment** — query Semantic Scholar, Crossref, arXiv, OpenAlex, PubMed, Europe PMC, DBLP, and OpenReview |
| 2.5 | **Venue Enrichment** — parse Scholar publication strings to search by venue name (fallback when Phase 2 finds nothing) |
| 3 | **Late DOI Discovery** — collect DOI candidates from arXiv eprints, bioRxiv strings, and URLs; prefer published over preprint |
| 4 | **Trust-Based Merge** — combine fields by source rank, apply type corrections, deduplicate on disk |

Authors are processed in parallel (12 workers by default), with per-API rate limiting and session rotation to avoid throttling. Publications outside a configurable year window (default: last 7 years) are filtered out automatically.

### Trust Hierarchy

Not all metadata sources are created equal. When two sources disagree on a field, CiteForge picks the more trustworthy one:

```
CSL > DOI BibTeX > DataCite > PubMed > Europe PMC > Crossref >
OpenAlex > Semantic Scholar > ORCID > OpenReview > arXiv >
Scholar Page > Scholar Minimal
```

A few field-specific rules sit on top of this ranking:

| Field | Rule |
|-------|------|
| DOI | A published DOI always wins over a preprint or data-repository DOI |
| Journal | Never downgrade from a published journal to a preprint server name |
| Title | Prefer the longer version unless the new source is 3+ ranks higher |
| Pages | Reject non-numeric values, dot-containing strings, and oversized ranges |
| Booktitle | Replace generic series names (like LNCS) with the actual conference name |

### Deduplication

Duplicate entries are caught at multiple levels before they reach your `.bib` files:

| Signal | Threshold |
|--------|:---------:|
| Identical normalized DOIs | exact |
| DOI version variants (.v1 / .v2) | exact |
| Matching external IDs (arXiv, PubMed, OpenAlex) | exact |
| High title similarity (rapidfuzz token_set_ratio) | 0.95 |
| Preprint/published pair with title match | 0.55 |
| Strong author overlap with moderate title match | 0.60 |

When a candidate DOI is discovered during enrichment, it's verified against existing files by title similarity before acceptance. If the match is poor, CiteForge reverts to the DOI validated in Phase 1.

### Data Quality

Beyond merging and deduplication, CiteForge cleans up common metadata problems:

| What it fixes | Why it matters |
|---------------|----------------|
| Page number validation | Rejects article IDs pretending to be page ranges |
| Series name expansion | Replaces generic names with real conference names |
| Artifact stripping | Removes "Check for updates" prefixes and HTML entities |
| Title filtering | Drops single-word and garbage titles from Scholar scraping |
| Type reclassification | Fixes conference papers incorrectly labeled as journal articles by Crossref |
| Casing normalization | Fixes ALL-CAPS titles and lowercase author names |
| DOI reversion | Undoes mis-attributed DOIs when title similarity fails |
| Fused compound repair | Splits ~376 Scholar-broken compounds like "DeepLearning" back to "Deep Learning" |
| Acronym case correction | Restores proper casing for IoT, NIMS, AI, and similar terms |
| Booktitle cleanup | Fixes duplicate prepositions, expands abbreviations, corrects NeurIPS spelling |

---

## Data Sources

CiteForge queries the following APIs. Only SerpAPI requires a key — everything else either needs no authentication or is optional:

| Source | Key | What it provides |
|--------|:---:|------------------|
| Google Scholar via [SerpAPI](https://serpapi.com/) | Required | Author publication lists |
| [Serply](https://serply.io/) | Optional | Citation detail lookups |
| [Semantic Scholar](https://www.semanticscholar.org/) | Recommended | Paper metadata and abstracts |
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

Set the `CROSSREF_MAILTO` environment variable to get into Crossref's polite pool (faster responses).

---

## Configuration

All tunable parameters live in [`src/config.py`](src/config.py). Here are the most important ones:

| Parameter | Default | What it controls |
|-----------|:-------:|------------------|
| `MAX_WORKERS` | 12 | Parallel author processing threads |
| `CONTRIBUTION_WINDOW_YEARS` | 7 | How many years of publications to fetch |
| `PUBLICATIONS_PER_YEAR` | 50 | Target publications per year per author |
| `SIM_MERGE_DUPLICATE_THRESHOLD` | 0.95 | Title similarity needed for file-level dedup |
| `SIM_PREPRINT_TITLE_THRESHOLD` | 0.55 | Relaxed threshold for preprint/published pairs |
| `REQUEST_DELAY_MIN` / `_MAX` | 0.3 / 1.0s | Courtesy delay between API requests |
| `CACHE_ENABLED` | True | Whether to cache API responses locally |
| `TRUST_ORDER` | 13 levels | Source priority for the field-by-field merge |
| `FUSED_COMPOUND_WORDS` | 376 entries | Dictionary for compound word repair |
| `COMPOUND_SUFFIXES` | 37 entries | Regex patterns for compound suffix repair |
| `ABBREVIATED_VENUE_MAP` | 46 entries | Venue abbreviation to full name expansion |

---

## Testing

```bash
pip install -e .[dev]
pytest tests/ -v --tb=short
```

The test suite has 384 tests across 12 modules. Integration tests that need API keys are automatically skipped when keys aren't available. CI runs on Python 3.10, 3.11, 3.12, and 3.13.

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
| `main.py` | Orchestrator: thread pool, 5-phase pipeline, CLI entry (~3,200 LOC) |
| `src/config.py` | Thresholds, trust order, HTTP params, API URLs, compound words, venue maps |
| `src/merge_utils.py` | Trust hierarchy merge, file save, multi-level dedup |
| `src/bibtex_utils.py` | BibTeX parsing, serialization, entry matching |
| `src/bibtex_build.py` | Entry building, scoring factory, type determination |
| `src/text_utils.py` | LaTeX/Unicode normalization, similarity, author parsing |
| `src/doi_utils.py` | DOI validation, CSL/BibTeX fallback chain |
| `src/id_utils.py` | DOI classification, arXiv extraction, version matching |
| `src/api_generics.py` | Generic search/build abstractions |
| `src/api_configs.py` | Per-API field mapping configurations |
| `src/publication_parser.py` | SerpAPI publication string parsing, venue/type inference |
| `src/cache.py` | Disk-based API response cache with monthly expiry |
| `src/http_utils.py` | HTTP session, retry, backoff, token bucket rate limiting |
| `src/io_utils.py` | CSV I/O, key loading, phantom/orphan reconciliation, a2i2 build |
| `src/log_utils.py` | Thread-local logging, per-author log files |
| `src/models.py` | Record dataclass, EnrichmentSource enum |
| `src/exceptions.py` | Error hierarchy tuples |
| `src/clients/scholar.py` | Google Scholar facade (SerpAPI + Serply + stale cache detection) |
| `src/clients/serpapi_scholar.py` | SerpAPI author publication retrieval |
| `src/clients/serply_scholar.py` | Serply citation detail lookups |
| `src/clients/scholarly_scholar.py` | Google Scholar web-scraping fallback |
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

Contributions are welcome! Here's the quick version:

1. Fork the repository
2. Create a feature branch
3. Make sure all three quality gates pass (lint, type check, tests)
4. Submit a pull request

## Citation

If you use CiteForge in your research or find it useful, please consider citing it:

```bibtex
@software{spadon_citeforge,
  author  = {Spadon, Gabriel},
  title   = {CiteForge},
  url     = {https://github.com/gabrielspadon/CiteForge},
  license = {MIT}
}
```

## License

This project is licensed under the **MIT License** — you're free to use, modify, and distribute it for any purpose. See the [LICENSE](LICENSE) file for the full text.
