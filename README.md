# CiteForge

[![Tests](https://github.com/MAPS-Lab/CiteForge/actions/workflows/tests.yml/badge.svg)](https://github.com/MAPS-Lab/CiteForge/actions/workflows/tests.yml)

CiteForge is a Python tool that builds clean, per-author BibTeX files from scholarly APIs. Given a CSV of authors with Google Scholar profiles, it retrieves each author's publications, enriches every entry against active scholarly registries and services, deduplicates records, and merges fields according to source trust. It runs on Python 3.10 or later with a small dependency footprint (requests, rapidfuzz, unidecode, and a few helpers), and it is developed and maintained by the [MAPS Lab](https://mapslab.tech/) at Dalhousie University.

## Features

- Per-author BibTeX generation from Google Scholar profiles through SerpAPI
- Multi-API enrichment across Semantic Scholar, Crossref, OpenAlex, arXiv, PubMed, Europe PMC, DBLP, OpenReview, and DOI resolvers
- Trust-based field merging that prioritizes authoritative registries over scraped content
- Deduplication combining DOI normalization, external-identifier matching, and fuzzy title similarity (rapidfuzz)
- Metadata correction for fragmented compound words, misclassified publication types, invalid page ranges, and all-capitals titles
- Deterministic output, with byte-identical results on cache-hit runs
- Parallel per-author processing under per-API rate limits, backed by a response cache with monthly expiry
- Config-driven behavior, with trust order, similarity thresholds, rate limits, and venue mappings centralized in [`citeforge/config.py`](citeforge/config.py)

Google Scholar entries are often incomplete, carrying missing DOIs, inconsistent venue names, and malformed author lists. Correcting them by hand requires cross-referencing registries such as Crossref, Semantic Scholar, arXiv, and PubMed, which does not scale to a research group. CiteForge automates that cross-referencing and consolidation.

## Installation

Requires Python 3.10 or later.

```bash
git clone https://github.com/MAPS-Lab/CiteForge.git && cd CiteForge
pip install -e .
```

Place API keys in the `keys/` directory. Only SerpAPI is required; the rest are recommended or optional.

```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key    # Required
echo "your_serply_key" > keys/Serply.key      # Recommended (citation detail skipped without it)
echo "your_semantic_key" > keys/Semantic.key  # Recommended
echo "your_gemini_key" > keys/Gemini.key      # Optional
printf "user\npass" > keys/OpenReview.key     # Optional
```

## Usage

Create the input CSV and run the pipeline. Relative paths resolve from the
current working directory.

```bash
citeforge                                      # data/input.csv -> output/
citeforge --force                              # Ignore cache completeness
citeforge --input authors.csv --output results # Explicit paths
```

The input CSV has three columns (name, Scholar link, and an optional DBLP link).

```csv
Name,Scholar Link,DBLP Link
Gabriel Spadon,https://scholar.google.com/citations?user=bfdGsGUAAAAJ,https://dblp.org/pid/192/1659
```

Output is organized per author, with a shared summary and run log. API responses are cached under `data/api_cache/` with monthly expiry.

```
output/
├── baseline.json
├── run.log
├── summary.csv
├── a2i2/
│   └── ...
└── Spadon (bfdGsGUAAAAJ)/
    ├── author.log
    ├── Spadon2024-MaritimeTracking.bib
    └── ...
```

## How it works

CiteForge retrieves each author's publication list from Google Scholar through SerpAPI, then enriches every entry by querying scholarly services including Semantic Scholar, Crossref, arXiv, OpenAlex, and PubMed. A trust-based consolidation stage merges the collected records according to source reliability, prioritizing authoritative registries over scraped content. Duplicate detection combines DOI normalization, external identifier matching, and fuzzy title similarity. The pipeline also corrects recurrent metadata issues such as fragmented compound words, misclassified publication types, invalid page ranges, and all-capitals titles.

Cache-hit runs produce byte-identical output, author queries run in parallel under per-API rate limits, and configurable parameters (source trust order, similarity thresholds, rate limits, venue mappings) are centralized in [`citeforge/config.py`](citeforge/config.py).

## Data sources

SerpAPI requires a key; the remaining sources are keyless, recommended, or optional. Set `CROSSREF_MAILTO` to join Crossref's polite pool.

| Tier | Sources |
|------|---------|
| Required (key) | [SerpAPI](https://serpapi.com/) (Google Scholar) |
| Recommended (key) | [Serply](https://serply.io/) (citation details), [Semantic Scholar](https://www.semanticscholar.org/) |
| Free (no key) | [Crossref](https://www.crossref.org/), [OpenAlex](https://openalex.org/), [arXiv](https://arxiv.org/), [PubMed](https://pubmed.ncbi.nlm.nih.gov/), [Europe PMC](https://europepmc.org/), [DBLP](https://dblp.org/) |
| Optional (key) | [OpenReview](https://openreview.net/), [Google Gemini](https://ai.google.dev/) |

## Development

Install the development extras, then run the three quality gates that must pass before merge.

```bash
pip install -e .[dev]                        # Install with dev tools
ruff check citeforge/ tests/ main.py         # Lint (line-length 120)
mypy citeforge/ main.py                       # Type check (strict, ignore_missing_imports)
pytest tests/ -v --tb=short                   # Full test suite (Python 3.10-3.13)
```

Run a single test with `pytest tests/test_core.py::test_function_name -v --tb=short`.
The installed command is implemented in `citeforge/cli.py`; root `main.py` is
only a compatibility launcher. The CLI loads keys and author records, then
delegates to `article.py`, `scheduler.py`, and `postrun.py` in
`citeforge/pipeline/`.

## Citation

Citation metadata is also provided in [CITATION.cff](CITATION.cff). If you use CiteForge in your work, please cite it with the BibTeX entry below.

```bibtex
@software{CiteForge2026:GSpadon,
  author    = {Spadon, Gabriel},
  title     = {CiteForge: Trust-Based Metadata Aggregation for Scholarly Publications},
  year      = {2026},
  version   = {1.0.0},
  publisher = {MAPS Lab, Dalhousie University},
  url       = {https://github.com/MAPS-Lab/CiteForge},
  license   = {AGPL-3.0-or-later}
}
```

## Related projects

CiteForge is one of the research tools from the [MAPS Lab](https://mapslab.tech/) at Dalhousie University. Explore the group's other open-source work on the [MAPS-Lab GitHub organization](https://github.com/MAPS-Lab).

## License

This project is distributed under the terms of the GNU Affero General Public
License version 3 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE) for
details.
