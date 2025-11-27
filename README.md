# CiteForge

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml/badge.svg)](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml)

CiteForge collects publication metadata from multiple academic APIs, merges them based on source reliability, and outputs BibTeX files.

## Features

- Fetches data from 12+ academic APIs (Google Scholar, Crossref, arXiv, PubMed, Semantic Scholar, etc.)
- Merges metadata using a trust hierarchy (DOI resolvers > curated databases > web scrapes)
- Detects duplicates via DOI matching, title similarity, and author overlap
- Processes multiple authors in parallel
- Cleans LaTeX formatting (`\textit{}`, `\textbf{}`) and normalizes Unicode to ASCII
- Caches results to reduce API calls on subsequent runs
- Optional Gemini integration for generating citation key titles

## Installation

```bash
git clone https://github.com/gabrielspadon/CiteForge.git
cd CiteForge
pip install -e .
```

For development:

```bash
pip install -e .[dev]
```

## Usage

### API Keys

Create a `keys/` directory with your API keys:

```bash
mkdir -p keys
echo "your_serpapi_key" > keys/SerpAPI.key          # Required
echo "your_semantic_key" > keys/Semantic.key        # Optional
echo "your_gemini_key" > keys/Gemini.key            # Optional
```

### Input

Create `data/input.csv` with authors to process:

```csv
Name,Scholar Link,DBLP Link
John Smith,https://scholar.google.com/citations?user=ABC123,https://dblp.org/pid/smith/john
```

### Run

```bash
python3 main.py
```

### Output

```
output/
├── run.log                       # Execution log
├── summary.csv                   # Enrichment report
└── John_Smith (ABC123)/
    ├── author.log
    ├── Smith2024-DeepLearning.bib
    └── ...
```

## Configuration

Edit `src/config.py` to adjust settings:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CONTRIBUTION_WINDOW_YEARS` | 5 | Years of publications to fetch |
| `PUBLICATIONS_PER_YEAR` | 50 | Target publications per year |
| `SIM_MERGE_DUPLICATE_THRESHOLD` | 0.9 | Title similarity threshold for duplicates |
| `REQUEST_DELAY_BETWEEN_ARTICLES` | 0.5s | Delay between API requests |
| `SKIP_SERPAPI_FOR_EXISTING_FILES` | True | Reuse existing files as seeds |

## Data Sources

### APIs Used

| Source | API Key |
|--------|---------|
| Google Scholar (via SerpAPI) | Required |
| Semantic Scholar | Recommended |
| OpenReview | Optional |
| Google Gemini | Optional |

### Trust Hierarchy

When merging, sources are prioritized in this order:

1. CSL-JSON via DOI
2. BibTeX via DOI
3. DataCite
4. PubMed
5. Europe PMC
6. Crossref
7. OpenAlex
8. Semantic Scholar
9. ORCID
10. OpenReview
11. arXiv
12. Scholar Page Metadata
13. Scholar Baseline

## Architecture

### Pipeline

Each article goes through four phases:

1. **Early DOI Validation** - Validate DOI from baseline metadata
2. **API Enrichment** - Query academic APIs and validate matches
3. **Late DOI Discovery** - Find DOIs from matched sources
4. **Merge & Save** - Apply trust hierarchy and write BibTeX

### Parallel Processing

Authors are processed concurrently using a thread pool (default: 12 workers). Each author has isolated logging.

## Testing

```bash
# Run all tests
pytest

# Run specific modules
pytest tests/test_core.py
pytest tests/test_apis.py
pytest tests/test_pipeline.py
```

The test suite includes 60+ tests covering BibTeX parsing, LaTeX stripping, Unicode normalization, duplicate detection, and merge policies.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Run tests (`pytest`)
4. Submit a pull request

## License

MIT License - see [LICENSE](LICENSE) for details.
