<h1 align="center">CiteForge</h1>

<p align="center">
  <a href="https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml"><img src="https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License"></a>
  <br>
  <img src="https://img.shields.io/badge/Updated-2026--07-blue.svg" alt="Last Updated">
  <img src="https://img.shields.io/badge/Queries-8664-8A2BE2.svg" alt="Total Queries">
  <img src="https://img.shields.io/badge/Cache_Hit_Rate-76.7%25-2ea44f.svg" alt="Cache Hit Rate">
</p>

<p align="center">
  <strong>Automated academic citation enrichment from multiple scholarly APIs.</strong><br>
  Given a list of authors, CiteForge fetches, validates, deduplicates,<br>
  and merges bibliographic metadata into per-author BibTeX files.
</p>

---

## Motivation

Google Scholar profiles index an author's publications, but the entries are often incomplete, with missing DOIs, inconsistent venue names, and malformed author lists. Correcting them requires cross-referencing registries such as Crossref, Semantic Scholar, arXiv, and PubMed, which is impractical to do by hand at the scale of a research group. CiteForge automates this cross-referencing and consolidation.

Given an input CSV of authors with Google Scholar profiles, CiteForge

- pulls each author's publications from Scholar via [SerpAPI](https://serpapi.com/) and citation details via [Serply](https://serply.io/);
- queries additional academic APIs for metadata on each paper;
- deduplicates results using fuzzy title matching, DOI normalization, and author overlap;
- merges fields using a multi-level trust hierarchy that prefers authoritative sources; and,
- writes `.bib` files organized by author.

Output is deterministic on cache-hit runs.

## Getting Started

Requires **Python 3.10+** and a [SerpAPI](https://serpapi.com/) key (the free tier is sufficient for small runs).

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

Then create the input CSV and run the pipeline.

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

API responses are cached under `data/api_cache/` with monthly expiry, so subsequent runs are fast and deterministic.

## How It Works

CiteForge retrieves each author's publication list from Google Scholar through SerpAPI, then enriches every entry by querying scholarly services including Semantic Scholar, Crossref, arXiv, OpenAlex, and PubMed.

A trust-based consolidation stage merges the collected records according to source reliability, prioritizing authoritative registries over scraped content. Duplicate detection combines DOI normalization, external identifier matching, and fuzzy title similarity to identify overlapping entries across sources.

The pipeline also corrects recurrent metadata issues such as fragmented compound words, misclassified publication types, invalid page ranges, and all-capitals titles. Deterministic caching ensures that cache-hit executions produce byte-identical outputs, verified through SHA-256 checksums.

Author queries run in parallel under per-API rate limits. All configurable parameters, including source trust order, similarity thresholds, rate limits, and venue mappings, are centralized in [`citeforge/config.py`](citeforge/config.py).

## Data Sources

SerpAPI requires a key; the remaining sources are keyless, recommended, or optional.

- **Required (key needed):** [SerpAPI](https://serpapi.com/) (Google Scholar);
- **Recommended (key needed):** [Serply](https://serply.io/) (citation details), [Semantic Scholar](https://www.semanticscholar.org/);
- **Free (no key):** [Crossref](https://www.crossref.org/), [OpenAlex](https://openalex.org/), [arXiv](https://arxiv.org/), [PubMed](https://pubmed.ncbi.nlm.nih.gov/), [Europe PMC](https://europepmc.org/), [DataCite](https://datacite.org/), [ORCID](https://orcid.org/), [DBLP](https://dblp.org/); and,
- **Optional (key needed):** [OpenReview](https://openreview.net/), [Google Gemini](https://ai.google.dev/).

Set `CROSSREF_MAILTO` to join Crossref's polite pool (faster responses).

## Citation

If you use CiteForge in your research, cite it with the entry below.

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

This project is licensed under the [MIT License](LICENSE).
