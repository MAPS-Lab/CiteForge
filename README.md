# CiteForge

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-56%20passing-brightgreen.svg)](tests/)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-success.svg)](requirements.txt)

A zero-dependency Python tool for automated bibliographic data collection and enrichment. CiteForge aggregates publication metadata from 12 academic sources, validates and merges them using trust-based policies, and exports clean BibTeX entries.

## Features

- **Multi-source aggregation**: Fetches from 12 academic APIs (Scholar, Crossref, arXiv, PubMed, OpenAlex, etc.)
- **Zero dependencies**: Pure Python 3.10+ with no external packages
- **Trust-based merging**: Intelligent metadata selection based on source reliability
- **DOI validation**: Two-stage pipeline for early and late DOI discovery
- **Smart caching**: Reduces API calls from 1+N to 1 per author on subsequent runs
- **Quality tracking**: CSV reports showing enrichment coverage per entry
- **Fuzzy matching**: Validates metadata consistency across sources
- **56 tests**: All core functionality is tested

## Quick Start

```bash
# Clone and navigate
git clone https://github.com/gabrielspadon/CiteForge.git
cd CiteForge

# Set up SerpAPI key
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key

# Create input file
echo "Name,Email,Scholar,ORCID,DBLP" > data/data.csv
echo "Gabriel Spadon,spadon@dal.ca,bfdGsGUAAAAJ,0000-0001-8437-4349,/192/1659" >> data/data.csv

# Run
python3 main.py
```

Output will be in the `output/` directory with individual BibTeX files per publication.

## Installation

**Requirements:**
- Python 3.10+
- SerpAPI key (required)

**Optional API keys** (for enhanced enrichment):
- Semantic Scholar
- Google Gemini (for automated title generation)
- OpenReview

Create a `keys/` directory and add your API keys:
```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key
echo "your_semantic_key" > keys/Semantic.key  # Optional
```

## Usage

### Input Format

Create `data/data.csv` with author information:

```csv
Name,Email,Scholar,ORCID,DBLP
Gabriel Spadon,spadon@dal.ca,bfdGsGUAAAAJ,0000-0001-8437-4349,/192/1659
```

**Required fields:**
- `Name`: Author's full name
- `Scholar`: Google Scholar profile ID

**Optional fields:**
- `Email`: Contact email
- `ORCID`: ORCID identifier
- `DBLP`: DBLP person ID

### Output Structure

```
output/
├── run.log                          # Execution log
├── summary.csv                      # Enrichment statistics
└── LastName (ScholarID)/
    ├── Author2024-Title.bib
    └── ...
```

The `summary.csv` file tracks which sources enriched each entry, with columns for each API (scholar_bib, crossref, arxiv, etc.) and a `trust_hits` count showing total enrichments.

### Configuration

Edit `CiteForge/config.py` to customize:

```python
CONTRIBUTION_WINDOW_YEARS = 3           # Time window for publications
PUBLICATIONS_PER_YEAR = 50              # Publications per year
SIM_MERGE_DUPLICATE_THRESHOLD = 0.85    # Deduplication threshold
REQUEST_DELAY_BETWEEN_ARTICLES = 0.5    # Rate limiting
SKIP_SERPAPI_FOR_EXISTING_FILES = True  # Smart caching
```

## Data Sources

**Primary:**
- Google Scholar (via SerpAPI)
- DBLP
- Semantic Scholar
- Crossref
- arXiv
- OpenReview

**Biomedical:**
- PubMed
- OpenAlex
- Europe PMC

**Utilities:**
- ORCID
- DataCite
- Google Gemini (short title generation)

## Architecture

### Trust Hierarchy

Fields are selected based on source reliability (highest to lowest):
1. CSL-JSON via DOI
2. BibTeX via DOI
3. DataCite
4. PubMed
5. Europe PMC
6. Crossref
7. OpenAlex
8. Scholar page metadata
9. Semantic Scholar
10. arXiv
11. OpenReview
12. DBLP

### Pipeline Flow

1. **Author Processing**: Fetch from Scholar/DBLP, merge and deduplicate
2. **Article Processing**: Establish baseline, validate DOI, enrich from sources, merge
3. **Output**: Generate citekey, render BibTeX, save to file

## Testing

Run all tests:
```bash
pytest
```

Run specific test suites:
```bash
pytest tests/test_core.py       # Core utilities
pytest tests/test_apis.py       # API integrations
pytest tests/test_pipeline.py   # Pipeline components
pytest tests/test_integration.py # End-to-end integration
```

## Troubleshooting

**"SerpAPI key not found"**
- Ensure `keys/SerpAPI.key` exists with a valid key from https://serpapi.com

**"Rate limit exceeded"**
- Increase `REQUEST_DELAY_BETWEEN_ARTICLES` in `config.py`

**"No publications found"**
- Verify Google Scholar ID is correct
- Check publications are within the contribution window
- Review `output/run.log`

**Low enrichment quality**
- Check `output/summary.csv` for entries with low `trust_hits`
- Consider enabling optional API keys (Semantic Scholar, Gemini)

## License

[MIT License](LICENSE) - Copyright (c) 2025 Gabriel Spadon