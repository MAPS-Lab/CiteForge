<h1 align="center">CiteForge</h1>

<p align="center">
  <a href="https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml"><img src="https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License"></a>
</p>

<p align="center">
  <strong>Automated academic citation enrichment from multiple scholarly APIs.</strong><br>
  CiteForge fetches, validates, deduplicates, and merges bibliographic<br>
  metadata so you don't have to. Given a list of authors, it<br>
  produces clean BibTeX files ready for LaTeX.
</p>

---

## Why CiteForge?

If you've ever tried to build a comprehensive publication list for a research group, you know the pain. Google Scholar entries are often incomplete, missing DOIs, and have inconsistent formatting and broken author names. Manually cross-referencing Crossref, Semantic Scholar, arXiv, PubMed, and a handful of other databases gets tedious fast, especially when you're dealing with dozens of authors and hundreds of papers.

CiteForge takes care of this. Point it at a list of authors with their Google Scholar profiles, and it will:

- Pull every publication from Scholar via [SerpAPI](https://serpapi.com/) and enrich citation details with [Serply](https://serply.io/);
- Query **multiple academic APIs** for richer metadata on each paper;
- Deduplicate results using fuzzy title matching, DOI normalization, and author overlap;
- Merge fields using a **multi-level trust hierarchy** that prefers authoritative sources; and,
- Output clean, LaTeX-ready `.bib` files organized by author.

The result is deterministic — on cache-hit runs, CiteForge produces **byte-identical output** across consecutive runs, verified by SHA-256 checksums.

## Getting Started

You'll need **Python 3.10+** and a [SerpAPI](https://serpapi.com/) key (the free tier is sufficient for small runs).

```bash
git clone https://github.com/gabrielspadon/CiteForge.git && cd CiteForge
pip install -e .
```

Drop your API keys into the `keys/` directory:

```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key    # Required
echo "your_serply_key" > keys/Serply.key      # Required
echo "your_semantic_key" > keys/Semantic.key  # Recommended
echo "your_gemini_key" > keys/Gemini.key      # Optional
printf "user\npass" > keys/OpenReview.key     # Optional
```

Then create an input CSV and run:

```bash
python3 main.py                  # Default: data/input.csv
python3 main.py data/custom.csv  # Custom input
python3 main.py --force          # Force re-enrichment
```

The input CSV has three columns — name, Scholar link, and an optional DBLP link:

```csv
Name,Scholar Link,DBLP Link
Gabriel Spadon,https://scholar.google.com/citations?user=bfdGsGUAAAAJ,https://dblp.org/pid/192/1659
```

Output is organized per author, with a shared summary and deduplication log:

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

CiteForge begins by retrieving the complete list of publications from Google Scholar through SerpAPI. Each entry is then enriched by querying multiple scholarly services, including Semantic Scholar, Crossref, arXiv, OpenAlex, and PubMed, to gather complementary metadata.

A trust-based consolidation stage merges the collected records according to source reliability, consistently prioritizing authoritative registries over scraped content. Duplicate detection combines DOI normalization, external identifier matching, and fuzzy title similarity to identify overlapping entries across sources.

The pipeline further corrects recurrent metadata issues, such as fragmented compound words, misclassified publication types, invalid page ranges, and titles written entirely in capital letters. Deterministic caching ensures that cache-hit executions produce byte-identical outputs, verified through SHA-256 checksums.

To maintain efficiency and stability, author queries run in parallel while respecting per-API rate limits. All configurable parameters, including source trust order, similarity thresholds, rate limits, and venue mappings, are centralized in [`src/config.py`](src/config.py).

## Data Sources

[SerpAPI](https://serpapi.com/) and [Serply](https://serply.io/) require keys — everything else is free or optional:

- **Required:** [SerpAPI](https://serpapi.com/) (Google Scholar), [Serply](https://serply.io/) (citation details);
- **Recommended:** [Semantic Scholar](https://www.semanticscholar.org/);
- **Free (no key):** [Crossref](https://www.crossref.org/), [OpenAlex](https://openalex.org/), [arXiv](https://arxiv.org/), [PubMed](https://pubmed.ncbi.nlm.nih.gov/), [Europe PMC](https://europepmc.org/), [DataCite](https://datacite.org/), [ORCID](https://orcid.org/), [DBLP](https://dblp.org/); and,
- **Optional (key needed):** [OpenReview](https://openreview.net/), [Google Gemini](https://ai.google.dev/).

Set `CROSSREF_MAILTO` to get into Crossref's polite pool (faster responses).

## Citation

If you use CiteForge in your research or find it useful, please consider citing it:

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

This project is licensed under the **MIT [License](LICENSE)** — you're free to use, modify, and distribute it for any purpose.
