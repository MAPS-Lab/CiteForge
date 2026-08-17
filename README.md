# CiteForge

[![Tests](https://github.com/MAPS-Lab/CiteForge/actions/workflows/tests.yml/badge.svg)](https://github.com/MAPS-Lab/CiteForge/actions/workflows/tests.yml)

CiteForge builds clean per-author BibTeX files from scholarly APIs. Point it at a CSV of
authors and it retrieves each one's publications, enriches every entry against the major
metadata registries, removes duplicates, and merges fields by source reliability.

Google Scholar is a good index and a poor bibliography. DOIs are missing, venue names are
inconsistent, author lists are mangled, titles arrive in block capitals. Fixing that by hand
means checking Crossref, Semantic Scholar, arXiv and PubMed for every entry, which does not
scale past a handful of people. CiteForge does the cross-referencing.

Built and maintained by the [MAPS Lab](https://mapslab.tech/) at Dalhousie University.

## Install

Python 3.10 or later.

```bash
git clone https://github.com/MAPS-Lab/CiteForge.git && cd CiteForge
pip install -e .
```

API keys go in `keys/`. Only SerpAPI is required.

```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key    # required
echo "your_serply_key" > keys/Serply.key      # recommended, citation counts
echo "your_semantic_key" > keys/Semantic.key  # recommended, higher rate limit
echo "your_gemini_key" > keys/Gemini.key      # optional, shorter filenames
printf "user\npass" > keys/OpenReview.key     # optional
```

## Usage

```bash
citeforge                                      # data/input.csv -> output/
citeforge --force                              # re-enrich complete entries
citeforge --input authors.csv --output results
```

The input CSV needs five columns. Set `Enabled` explicitly on every row: an enabled row needs
a Google Scholar or DBLP profile, a disabled one needs a reason. Rows that are ambiguous stop
the run before any API calls, rather than being skipped silently.

```csv
Name,Scholar Link,DBLP Link,Enabled,Exclusion Reason
Gabriel Spadon,https://scholar.google.com/citations?user=bfdGsGUAAAAJ,https://dblp.org/pid/192/1659,true,
Example Author,,,false,No Scholar or DBLP profile
```

Output is one directory per author, plus a summary CSV recording which sources contributed to
each entry:

```
output/
├── baseline.json
├── summary.csv
└── Spadon (bfdGsGUAAAAJ)/
    ├── Spadon2024-MaritimeTracking.bib
    └── ...
```

Responses cache under `data/api_cache/` for a month. A cache hit means the response is fresh,
not that the entry is complete.

## How it works

Each publication starts from its Scholar record and goes through four phases: DOI validation,
enrichment against the registries, late DOI inference, and a trust-based merge.

The merge is the interesting part. `merge_with_policy()` ranks sources and applies per-field
rules rather than letting the last writer win, so a published DOI beats a preprint DOI, a
journal name is never replaced by a preprint server, and a generic series name gives way to
the real conference name. Duplicates are matched on normalized DOIs, external identifiers and
title similarity together, never on the title alone.

Given the same cached responses, a run produces byte-identical output.

### Monthly refresh

A scheduled workflow re-runs the pipeline until the corpus digest stops changing, then opens
a pull request. Publication always goes through review and Required CI; nothing is pushed to
`main` directly, and the website sync only fires once the corpus has actually merged.

### Refresh engine

`citeforge/refresh/` holds a second, resumable implementation with a durable ledger, reachable
as `citeforge refresh --state-dir <dir>`. It shares no enrichment code with the pipeline above
and is not yet used in production. Two things to know before relying on it: no generation has
run against live providers, and the ledger deliberately refuses to mark a generation complete,
because declaring an author's publication list finished is not an authority it holds.

## Sources

| | |
|---|---|
| Required | [SerpAPI](https://serpapi.com/) (Google Scholar) |
| Recommended | [Serply](https://serply.io/), [Semantic Scholar](https://www.semanticscholar.org/) |
| No key needed | [Crossref](https://www.crossref.org/), [OpenAlex](https://openalex.org/), [arXiv](https://arxiv.org/), [PubMed](https://pubmed.ncbi.nlm.nih.gov/), [Europe PMC](https://europepmc.org/), [DBLP](https://dblp.org/) |
| Optional | [OpenReview](https://openreview.net/), [Google Gemini](https://ai.google.dev/) |

Set `CROSSREF_MAILTO` to use Crossref's polite pool.

## Development

Install from the hash-locked requirements for a reproducible environment:

```bash
python -m pip install --require-hashes -r requirements-build.lock -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps -e .
```

Three checks gate a merge:

```bash
ruff check citeforge/ tests/ main.py
mypy citeforge/ main.py
pytest tests/ -m 'not live'
```

Tests never touch the network; patch HTTP with `monkeypatch`. Tests needing real keys skip
themselves when the keys are absent.

`citeforge/cli.py` is the entry point (`main.py` is a thin launcher) and delegates to
`article.py`, `scheduler.py` and `postrun.py` under `citeforge/pipeline/`.

Two conventions matter more than they look:

- Entry-type and text normalization lives only in `citeforge/canonicalize.py`, split across
  four ordered stages. A rule belongs in every stage that can emit the affected entry, so
  adding one means checking all four.
- Thresholds, endpoints, trust order, rate limits and venue mappings belong in
  `citeforge/config.py`, never inline.

## Citation

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

See also [CITATION.cff](CITATION.cff).

## License

GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).
