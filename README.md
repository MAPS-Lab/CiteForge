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

---

## How It Works

CiteForge starts by pulling every publication from Google Scholar via SerpAPI, then queries 13 scholarly APIs — Semantic Scholar, Crossref, arXiv, OpenAlex, PubMed, and others — for richer metadata on each paper. A trust-based merge combines fields by source reliability, always preferring authoritative sources over scraped data. Duplicates are caught through DOI normalization, external ID matching, and fuzzy title similarity. The pipeline also fixes common metadata problems: broken compound words, miscategorized entry types, invalid page numbers, and ALL-CAPS titles. On cache-hit runs, CiteForge produces byte-identical output, verified by SHA-256 checksums.

Authors are processed in parallel (12 workers by default) with per-API rate limiting. All tunable parameters — trust order, thresholds, rate limits, venue maps — live in [`src/config.py`](src/config.py).

---

## Data Sources

CiteForge queries 13 scholarly APIs. Only [SerpAPI](https://serpapi.com/) requires a key — everything else is free or optional:

- **Required:** [SerpAPI](https://serpapi.com/) (Google Scholar)
- **Recommended:** [Semantic Scholar](https://www.semanticscholar.org/)
- **Free (no key):** [Crossref](https://www.crossref.org/), [OpenAlex](https://openalex.org/), [arXiv](https://arxiv.org/), [PubMed](https://pubmed.ncbi.nlm.nih.gov/), [Europe PMC](https://europepmc.org/), [DataCite](https://datacite.org/), [ORCID](https://orcid.org/), [DBLP](https://dblp.org/)
- **Optional (key needed):** [Serply](https://serply.io/), [OpenReview](https://openreview.net/), [Google Gemini](https://ai.google.dev/)

Set `CROSSREF_MAILTO` to get into Crossref's polite pool (faster responses).

---

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
