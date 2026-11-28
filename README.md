# CiteForge

[![Tests](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml/badge.svg)](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml)

CiteForge builds one BibTeX file per publication for every person on a roster. It lists each
person's works from ORCID, OpenAlex and DBLP, takes the metadata from the registry that
issued the identifier (Crossref, DataCite, the doi.org content service for other agencies,
and arXiv, with PubMed supplying the DOI of a work listed only by PMID), joins preprints
to their published versions, and decides authorship from
ORCID, DBLP and affiliation evidence. Every decision and every request is recorded.

Built and maintained by the [MAPS Lab](https://mapslab.tech/) at Dalhousie University.

## Install

Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/gabrielspadon/CiteForge.git && cd CiteForge
uv sync
```

OpenAlex needs an API key, read from `OPENALEX_API_KEY` or from `keys/OpenAlex.key`
(gitignored). No other source needs a key.

## Input

`data/input.csv` has one row per person.

```csv
Name,Scholar Link,DBLP Link,ORCID,OpenAlex,Enabled,Exclusion Reason
Gabriel Spadon,https://scholar.google.com/citations?user=bfdGsGUAAAAJ,https://dblp.org/pid/192/1659,0000-0001-8437-4349,,true,
Example Author,,,,,false,Left the lab
```

- `ORCID` and `OpenAlex` take `;`-separated lists. ORCID iDs are checksum-validated, and an
  OpenAlex id looks like `A5073978680`.
- An enabled row needs an ORCID or a DBLP pid. A disabled row needs a reason.
- The Scholar link is used only to name the output folder.
- Any malformed row stops the run before a request is sent.

`data/a2i2.csv` lists the A2I2 group by name. Its folder holds the union of its members' files.

`citeforge identify` prints proposed ORCID and OpenAlex ids for rows that lack them, from
DBLP and OpenAlex evidence, and writes nothing. A person checks each proposal before it goes
into the roster.

## Run

```bash
uv run citeforge                            # data/input.csv -> output/
uv run citeforge --input authors.csv --output results
```

`CITEFORGE_MIN_YEAR` sets the first year kept (default 2020). An empty value keeps the last 7
years.

| Exit | Meaning |
|---|---|
| 0 | complete |
| 1 | unexpected error, traceback on stderr |
| 2 | bad input, curated file or key, before any request |
| 3 | written, but a person was partial, a source was down or a guard held removals |

## Outputs

```
output/
├── baseline.json                       {"total": N, "authors": {folder: count}}
├── summary.csv                         one row per file with the sources that contributed
├── a2i2/
└── Spadon (bfdGsGUAAAAJ)/
    └── Spadon2024-PhysicsInformed.bib
state/
├── works.jsonl                         every work, its names and per-person decisions
└── report.json                         sources, statuses, review list, links, coverage
run/                                    requests.jsonl and report.md, not committed
```

A work has the same file name and the same bytes in every folder it appears in. A name, once
given, is kept while the first author's family name and the year stay the same.

`state/works.jsonl` is the memory between runs. When a source fails, the person is marked
partial: works can be added but none are removed. A file is removed only with complete
listings and a named reason, and never when a person would lose more than 20% of their
entries at once.

## Curated decisions

`data/curated.toml` holds every human decision, keyed by work id (`doi:`, `arxiv:`, `pmid:`,
`dblp:`, `openalex:` or `manual:`).

```toml
[lab]
ror = ["https://ror.org/01e6qks80"]

[[accept]]
member = "Ye (4OQaVGUAAAAJ)"
id = "doi:10.1109/ojvt.2021.3097560"

[[reject]]                 # member "*" excludes the work for everyone
member = "*"
id = "doi:10.2172/1991087"
reason = "namesake"

[[link]]
preprint = "arxiv:2310.18948"
published = "doi:10.1016/j.oceaneng.2024.117234"

[[override]]
id = "doi:10.1234/example"
field = "booktitle"
value = "Proceedings of the Example Conference"

[[record]]                 # a work no registry holds
id = "manual:spadon-2021-thesis"
kind = "phdthesis"
title = "Thesis title"
authors = ["Gabriel Spadon"]
year = 2021
school = "Universidade de São Paulo"
members = ["Spadon (bfdGsGUAAAAJ)"]
```

Each run's `run/report.md` lists the cases held for review with a ready `[[accept]]` and
`[[reject]]` line to paste.

## Monthly refresh

`.github/workflows/refresh.yml` runs on the 2nd of each month, checks the committed corpus and
opens a `data: monthly refresh YYYY-MM` pull request with `run/report.md` as its body.

## Development

```bash
just test     # uv run pytest
just lint     # ruff check, ruff format --check, pyrefly check
```

Tests never open a socket. HTTP replies come from recorded responses under
`tests/fixtures/http/` and hand-made failure payloads under `tests/fixtures/synthetic/`.

## Citation

```bibtex
@software{CiteForge2026:GSpadon,
  author    = {Spadon, Gabriel},
  title     = {CiteForge: Trust-Based Metadata Aggregation for Scholarly Publications},
  year      = {2026},
  version   = {1.0.0},
  publisher = {MAPS Lab, Dalhousie University},
  url       = {https://github.com/gabrielspadon/CiteForge},
  license   = {AGPL-3.0-or-later}
}
```

See also [CITATION.cff](CITATION.cff).

## License

GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE).
