# CiteForge

[![Tests](https://github.com/MAPS-Lab/CiteForge/actions/workflows/tests.yml/badge.svg)](https://github.com/MAPS-Lab/CiteForge/actions/workflows/tests.yml)

CiteForge is a Python tool that builds clean, per-author BibTeX files from scholarly APIs. Given a CSV of authors with Google Scholar profiles, it retrieves each author's publications, enriches every entry against active scholarly registries and services, deduplicates records, and merges fields according to source trust. It runs on Python 3.10 or later with a small dependency footprint (requests, rapidfuzz, unidecode, and a few helpers), and it is developed and maintained by the [MAPS Lab](https://mapslab.tech/) at Dalhousie University.

## Features

- Per-author BibTeX generation from Google Scholar profiles through SerpAPI
- Multi-API enrichment across Semantic Scholar, Crossref, OpenAlex, arXiv, PubMed, Europe PMC, DBLP, OpenReview, and DOI resolvers
- Trust-based field merging that prioritizes authoritative registries over scraped content
- Deduplication combining DOI normalization, external-identifier matching, and fuzzy title similarity (rapidfuzz)
- Metadata correction for fragmented compound words, misclassified publication types, invalid page ranges, and all-capitals titles
- Deterministic materialization, with byte-identical results for equivalent cache-hit inputs
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

The input CSV has five columns. Every physical author row must explicitly set
`Enabled` to `true` or `false`. An enabled row needs at least one valid Google
Scholar or DBLP profile. A disabled row needs a non-empty `Exclusion Reason`.
Enabled rows cannot carry an exclusion reason. Invalid, unclassified, blank,
or ambiguous profile rows stop the run before any API work.

```csv
Name,Scholar Link,DBLP Link,Enabled,Exclusion Reason
Gabriel Spadon,https://scholar.google.com/citations?user=bfdGsGUAAAAJ,https://dblp.org/pid/192/1659,true,
Example Excluded Author,,,false,No Scholar or DBLP profile configured
```

Output is organized per author, with a shared summary and run log. API responses are cached under `data/api_cache/` with monthly expiry. A cache hit establishes only response-cache freshness. It does not establish entry completeness or refresh completion.

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

Equivalent cache-hit inputs produce byte-identical materialized output. That idempotence result is distinct from entry completeness, response-cache freshness, and workflow completion. Author queries run in parallel under per-API rate limits, and configurable parameters (source trust order, similarity thresholds, rate limits, venue mappings) are centralized in [`citeforge/config.py`](citeforge/config.py).

The monthly workflow runs one generation segment per Actions run rather than sweeping the whole corpus
repeatedly. A run restores the previous sealed checkpoint, drives `citeforge refresh` once, and seals
its progress again before exiting, so a segment that hits the job ceiling resumes where it stopped
instead of restarting the month. Whether the generation is finished is read from the durable ledger,
not inferred from a request count or a corpus digest that stopped moving. Only a generation the ledger
reports as complete is published, and publication is a bot pull request gated on Required CI, never a
direct push. The website sync fires from the merge, so nothing is dispatched before the corpus is
actually on `main`. No encrypted response-cache branch is maintained; the only state carried between
runs is the sealed checkpoint.

The durable refresh engine behind it lives in `citeforge/refresh/`. Its census, ledger, transport,
discovery, checkpoint, staging, and publication layers are implemented and covered by tests, and
`citeforge refresh --state-dir <dir>` runs one bounded generation. What is not yet proven is a full
generation against live providers. That run is wired as a manually dispatched, publication-disabled
shadow workflow and has not been executed, so every claim about live provider authentication, schemas,
and quotas remains unverified. [`docs/ci-refresh-architecture.md`](docs/ci-refresh-architecture.md) is
the contract it is built against, [`docs/ci-refresh-implementation-plan.md`](docs/ci-refresh-implementation-plan.md)
is the task breakdown, and [`docs/ci-refresh-evidence.md`](docs/ci-refresh-evidence.md) records which
requirements have evidence, which do not, and which can only be settled by that run.

## Data sources

SerpAPI requires a key; the remaining sources are keyless, recommended, or optional. Set `CROSSREF_MAILTO` to join Crossref's polite pool.

| Tier | Sources |
|------|---------|
| Required (key) | [SerpAPI](https://serpapi.com/) (Google Scholar) |
| Recommended (key) | [Serply](https://serply.io/) (citation details), [Semantic Scholar](https://www.semanticscholar.org/) |
| Free (no key) | [Crossref](https://www.crossref.org/), [OpenAlex](https://openalex.org/), [arXiv](https://arxiv.org/), [PubMed](https://pubmed.ncbi.nlm.nih.gov/), [Europe PMC](https://europepmc.org/), [DBLP](https://dblp.org/) |
| Optional (key) | [OpenReview](https://openreview.net/), [Google Gemini](https://ai.google.dev/) |

## Development

For reproducible installs, use the checked-in hash-locked requirements files.
`requirements-build.lock` pins the build backend and the `uv` lock compiler,
`requirements.lock` pins runtime dependencies, and `requirements-dev.lock`
pins the runtime and development toolchain. Use the runtime lock for ordinary
use, or the development lock when contributing.

```bash
# Runtime setup
python -m pip install --require-hashes -r requirements-build.lock -r requirements.lock
python -m pip install --no-build-isolation --no-deps -e .

# Development setup
python -m pip install --require-hashes -r requirements-build.lock -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps -e .
```

Then run the three quality gates that must pass before merge.

```bash
ruff check citeforge/ tests/ main.py         # Lint (line-length 120)
mypy citeforge/ main.py                       # Type check (strict, ignore_missing_imports)
pytest tests/ -v --tb=short                   # Full test suite (Python 3.10-3.14)
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
