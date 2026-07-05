# CiteForge

[![Tests](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml/badge.svg)](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml)

CiteForge builds per-author BibTeX files from scholarly APIs. Given a CSV of authors with Google Scholar profiles, it retrieves their publications, enriches each entry from multiple registries, deduplicates, and merges fields according to source trust.

Google Scholar entries are often incomplete, with missing DOIs, inconsistent venue names, and malformed author lists. Correcting them requires cross-referencing registries such as Crossref, Semantic Scholar, arXiv, and PubMed, which does not scale to a research group. CiteForge automates that cross-referencing and consolidation.

## Installation

Requires Python 3.10 or later.

```bash
git clone https://github.com/gabrielspadon/CiteForge.git && cd CiteForge
pip install -e .
```

Place API keys in the `keys/` directory.

```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key    # Required
echo "your_serply_key" > keys/Serply.key      # Recommended (citation detail skipped without it)
echo "your_semantic_key" > keys/Semantic.key  # Recommended
echo "your_gemini_key" > keys/Gemini.key      # Optional
printf "user\npass" > keys/OpenReview.key     # Optional
```

## Usage

Create the input CSV and run the pipeline from the project root.

```bash
python3 main.py           # Input: data/input.csv
python3 main.py --force   # Force re-enrichment (ignore cache completeness)
```

The input CSV has three columns (name, Scholar link, and an optional DBLP link).

```csv
Name,Scholar Link,DBLP Link
Gabriel Spadon,https://scholar.google.com/citations?user=bfdGsGUAAAAJ,https://dblp.org/pid/192/1659
```

Output is organized per author, with a shared summary and run log.

```
output/
├── baseline.json
├── run.log
├── summary.csv
└── Spadon (bfdGsGUAAAAJ)/
    ├── author.log
    ├── Spadon2024-MaritimeTracking.bib
    └── ...
```

API responses are cached under `data/api_cache/` with monthly expiry.

## How It Works

CiteForge retrieves each author's publication list from Google Scholar through SerpAPI, then enriches every entry by querying scholarly services including Semantic Scholar, Crossref, arXiv, OpenAlex, and PubMed.

A trust-based consolidation stage merges the collected records according to source reliability, prioritizing authoritative registries over scraped content. Duplicate detection combines DOI normalization, external identifier matching, and fuzzy title similarity. The pipeline also corrects recurrent metadata issues such as fragmented compound words, misclassified publication types, invalid page ranges, and all-capitals titles.

Cache-hit runs produce byte-identical output. Author queries run in parallel under per-API rate limits. Configurable parameters (source trust order, similarity thresholds, rate limits, venue mappings) are centralized in [`citeforge/config.py`](citeforge/config.py).

## Data Sources

SerpAPI requires a key; the remaining sources are keyless, recommended, or optional.

| Tier | Sources |
|------|---------|
| Required (key) | [SerpAPI](https://serpapi.com/) (Google Scholar) |
| Recommended (key) | [Serply](https://serply.io/) (citation details), [Semantic Scholar](https://www.semanticscholar.org/) |
| Free (no key) | [Crossref](https://www.crossref.org/), [OpenAlex](https://openalex.org/), [arXiv](https://arxiv.org/), [PubMed](https://pubmed.ncbi.nlm.nih.gov/), [Europe PMC](https://europepmc.org/), [DataCite](https://datacite.org/), [ORCID](https://orcid.org/), [DBLP](https://dblp.org/) |
| Optional (key) | [OpenReview](https://openreview.net/), [Google Gemini](https://ai.google.dev/) |

Set `CROSSREF_MAILTO` to join Crossref's polite pool.

## Citation

```bibtex
@software{spadon_citeforge,
  author    = {Spadon, Gabriel},
  title     = {CiteForge: Trust-Based Metadata Aggregation for Scholarly Publications},
  year      = {2026},
  version   = {1.0.0},
  publisher = {GitHub},
  url       = {https://github.com/gabrielspadon/CiteForge},
  license   = {MIT}
}
```

## License

[MIT](LICENSE).
