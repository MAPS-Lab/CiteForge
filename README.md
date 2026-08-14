# CiteForge

[![Tests](https://github.com/MAPS-Lab/CiteForge/actions/workflows/tests.yml/badge.svg)](https://github.com/MAPS-Lab/CiteForge/actions/workflows/tests.yml)

CiteForge builds clean, per-author BibTeX files from scholarly APIs. Give it a CSV of
authors with Google Scholar or DBLP profiles and it retrieves each author's publications,
enriches every entry against active scholarly registries, deduplicates records, and merges
fields according to source trust.

Google Scholar entries are routinely incomplete. DOIs are missing, venue names are
inconsistent, author lists are malformed, and titles arrive in all capitals. Fixing that by
hand means cross-referencing Crossref, Semantic Scholar, arXiv, and PubMed for every entry,
which does not scale to a research group. CiteForge automates the cross-referencing and the
consolidation that follows it.

It runs on Python 3.10 or later with a small dependency footprint (requests, rapidfuzz,
unidecode, and a few helpers), and it is developed and maintained by the
[MAPS Lab](https://mapslab.tech/) at Dalhousie University.

## Features

- Per-author BibTeX generation from Google Scholar profiles through SerpAPI
- Enrichment across Semantic Scholar, Crossref, OpenAlex, arXiv, PubMed, Europe PMC, DBLP, OpenReview, and DOI resolvers
- Trust-based field merging that prefers authoritative registries over scraped content
- Deduplication combining DOI normalization, external-identifier matching, and fuzzy title similarity
- Metadata repair for fragmented compound words, misclassified entry types, invalid page ranges, and all-capitals titles
- Deterministic output, byte-identical across equivalent cache-hit inputs
- Parallel per-author and per-article processing under per-API rate limits
- Config-driven behaviour, with trust order, thresholds, rate limits, and venue mappings centralized in [`citeforge/config.py`](citeforge/config.py)

## Installation

Requires Python 3.10 or later.

```bash
git clone https://github.com/MAPS-Lab/CiteForge.git && cd CiteForge
pip install -e .
```

Place API keys in the `keys/` directory. Only SerpAPI is required.

```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key    # Required
echo "your_serply_key" > keys/Serply.key      # Recommended, citation detail is skipped without it
echo "your_semantic_key" > keys/Semantic.key  # Recommended
echo "your_gemini_key" > keys/Gemini.key      # Optional
printf "user\npass" > keys/OpenReview.key     # Optional
```

## Usage

Relative paths resolve from the current working directory.

```bash
citeforge                                      # data/input.csv -> output/
citeforge --force                              # Ignore cache completeness
citeforge --input authors.csv --output results # Explicit paths
```

The input CSV has five columns. Every row must set `Enabled` to `true` or `false`
explicitly. An enabled row needs at least one valid Google Scholar or DBLP profile and
cannot carry an exclusion reason. A disabled row needs a non-empty `Exclusion Reason`.
Invalid, unclassified, blank, or ambiguous profile rows stop the run before any API work,
rather than being skipped quietly.

```csv
Name,Scholar Link,DBLP Link,Enabled,Exclusion Reason
Gabriel Spadon,https://scholar.google.com/citations?user=bfdGsGUAAAAJ,https://dblp.org/pid/192/1659,true,
Example Excluded Author,,,false,No Scholar or DBLP profile configured
```

Output is organized per author with a shared summary and run log. API responses cache under
`data/api_cache/` with monthly expiry. A cache hit establishes response freshness only. It
says nothing about whether an entry is complete.

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

Each author's publication list comes from Google Scholar through SerpAPI. Every entry then
passes through four phases: DOI validation, multi-API enrichment, late DOI inference, and a
trust-based merge that writes the result. A SerpAPI publication-string fallback sits between
enrichment and inference for entries that reach it without a usable venue.

Merging is not a last-writer-wins union. `merge_with_policy()` ranks sources and applies
per-field override rules, so a published DOI beats a preprint DOI, a journal name is never
downgraded to a preprint server, the longer of two titles wins, invalid page ranges are
rejected, and a generic series name is upgraded to the actual conference name where one is
known. Duplicate detection combines DOI normalization, external identifier matching, and
fuzzy title similarity.

Equivalent cache-hit inputs produce byte-identical output. That is an idempotence property
and it is deliberately distinct from three things it is easy to confuse it with: whether an
entry is complete, whether the response cache is fresh, and whether a refresh run finished.

### Monthly refresh

The scheduled workflow runs the legacy pipeline (`python3 main.py`) in a bounded loop,
stopping when the corpus digest over the generated `.bib`, `summary.csv`, and `baseline.json`
files stops moving. It publishes by opening a bot pull request gated on Required CI, never by
pushing to `main`. The website sync fires from the merge, so nothing is dispatched before the
corpus is actually on the default branch.

### Durable refresh engine

A second, separate system lives in `citeforge/refresh/`. Its census, ledger, transport,
discovery, checkpoint, staging, and publication layers are implemented and covered by tests,
and `citeforge refresh --state-dir <dir>` drives one bounded, resumable generation. It shares
no enrichment code with the pipeline above; the two meet only at `citeforge/cli.py`.

It does not yet run in production, and two limits are worth stating plainly rather than
leaving to be discovered.

A full generation has never run against live providers. That run is wired as a manually
dispatched, publication-disabled shadow workflow, so every claim about live provider
authentication, schemas, and quotas is unverified until one can be cited.

The engine also cannot report a generation complete, and that is by design rather than an
unfinished edge. Doing so would require the ledger to record discovery as closed, and the
schema refuses it through triggers, an invariant re-checked on every read, and a schema
fingerprint. Declaring an author's publication list complete is an authority this stage of
the system does not hold, so nothing may gate on that status.
`tests/test_workflow_contracts.py::test_no_workflow_gates_on_a_status_the_ledger_forbids_producing`
enforces it.

## Data sources

SerpAPI requires a key. The rest are keyless, recommended, or optional. Set `CROSSREF_MAILTO`
to join Crossref's polite pool.

| Tier | Sources |
|------|---------|
| Required (key) | [SerpAPI](https://serpapi.com/) (Google Scholar) |
| Recommended (key) | [Serply](https://serply.io/) (citation details), [Semantic Scholar](https://www.semanticscholar.org/) |
| Free (no key) | [Crossref](https://www.crossref.org/), [OpenAlex](https://openalex.org/), [arXiv](https://arxiv.org/), [PubMed](https://pubmed.ncbi.nlm.nih.gov/), [Europe PMC](https://europepmc.org/), [DBLP](https://dblp.org/) |
| Optional (key) | [OpenReview](https://openreview.net/), [Google Gemini](https://ai.google.dev/) |

## Development

Use the checked-in hash-locked requirements files for a reproducible install.
`requirements-build.lock` pins the build backend and the `uv` lock compiler,
`requirements.lock` pins runtime dependencies, and `requirements-dev.lock` pins the runtime
plus the development toolchain.

```bash
# Runtime
python -m pip install --require-hashes -r requirements-build.lock -r requirements.lock
python -m pip install --no-build-isolation --no-deps -e .

# Development
python -m pip install --require-hashes -r requirements-build.lock -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps -e .
```

Three gates must pass before merge.

```bash
ruff check citeforge/ tests/ main.py   # Lint, line-length 120
mypy citeforge/ main.py                # Type check, strict
pytest tests/ -v --tb=short            # Full suite, Python 3.10 to 3.14
```

Run one test with `pytest tests/test_core.py::test_function_name -v --tb=short`. Tests never
make real API calls; use `monkeypatch` for HTTP. Integration tests that need keys skip
themselves when the keys are absent.

The installed command is implemented in `citeforge/cli.py`, and root `main.py` is only a
compatibility launcher. The CLI loads keys and author records, then delegates to `article.py`,
`scheduler.py`, and `postrun.py` under `citeforge/pipeline/`.

Two conventions carry more weight than their size suggests. Entry-type and text normalization
is single-sourced in `citeforge/canonicalize.py`, where callers pick one of four ordered
stages; a rule belongs to every stage whose path can emit the affected entry, so changing one
means checking all four. And thresholds, endpoints, trust order, rate limits, and venue
mappings belong in `citeforge/config.py`, never inline at a call site.

## Citation

Metadata is also provided in [CITATION.cff](CITATION.cff). If you use CiteForge in your work,
please cite it with the entry below.

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

## License

Distributed under the terms of the GNU Affero General Public License version 3 or later
(AGPL-3.0-or-later). See [LICENSE](LICENSE) for details.
