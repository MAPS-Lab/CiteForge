# CiteForge

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml/badge.svg)](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml)

CiteForge is a comprehensive Python tool designed for automated bibliographic data collection and enrichment. It aggregates publication metadata from over 12 academic sources, validates and merges them using trust-based policies, and exports clean, high-quality BibTeX entries.

## Key Features

### Core Functionality

- **Multi-Source Aggregation**: Seamlessly fetches data from 12+ academic APIs including Google Scholar, Crossref, arXiv, PubMed, OpenAlex, Semantic Scholar, and more.
- **Standard Library Implementation**: Built entirely using the Python 3.10+ standard library—no external dependencies required.
- **Trust-Based Merging**: Implements intelligent metadata selection algorithms that prioritize data based on source reliability (DOI resolvers > curated databases > web scrapes).
- **DOI Validation**: Features a robust two-stage pipeline for early and late DOI discovery, with cross-validation against baseline metadata to prevent merging different papers.

### Data Quality & Intelligence

- **Smart Duplicate Detection**: Multi-stage deduplication using DOI matching, title similarity, and author overlap to prevent false positives while identifying true duplicates across sources.
- **Intelligent Entry Type Detection**: Automatically distinguishes between journal articles, conference proceedings, book chapters, preprints, and datasets based on venue metadata.
- **Automated Data Cleaning**: Normalizes metadata by removing markup artifacts, standardizing field formats, and validating data consistency across enrichment sources.
- **AI-Powered Title Generation**: Optional Google Gemini integration for generating concise, semantic citation key titles with intelligent fallback to algorithmic extraction.

### Consistency & Organization

- **Systematic File Naming**: Generates consistent citation keys and filenames based on author, year, and semantic title extraction.
- **Smart Caching**: Optimizes API usage by caching results and reusing existing files as enrichment seeds, dramatically reducing requests on subsequent runs.
- **Quality Assurance**: Generates detailed CSV reports tracking enrichment coverage and source contribution for every entry.

### Developer Experience

- **Semantic Logging**: Category-based logging system for clearer, context-rich output.
- **Comprehensive Testing**: Fully tested core functionality with 56+ automated tests covering duplicate detection, merging policies, and data validation.
- **Configurable Thresholds**: Fine-tune similarity matching, duplicate detection, and trust hierarchies via configuration files.

## Quick Start

### Prerequisites

- Python 3.10 or higher (no external packages required)
- A [SerpAPI](https://serpapi.com) key (required for Google Scholar data)

### Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/gabrielspadon/CiteForge.git
   cd CiteForge
   ```

2. Configure API Keys (create a `keys/` directory and add your SerpAPI key):

   ```bash
   mkdir -p keys
   echo "your_serpapi_key" > keys/SerpAPI.key
   ```

3. Optional API Keys for enhanced enrichment:

   ```bash
   # Semantic Scholar (recommended for CS/ML papers)
   echo "your_s2_key" > keys/Semantic.key

   # Google Gemini (for AI-powered short title generation)
   echo "your_gemini_key" > keys/Gemini.key

   # OpenReview (for ML conference papers)
   echo "username" > keys/OpenReview.key
   echo "password" >> keys/OpenReview.key
   ```

### Usage

1. Prepare input data (create a `data/input.csv` file with authors to process):

   ```bash
   echo "Name,Scholar Link,DBLP Link" > data/input.csv
   echo "Author Name,https://scholar.google.com/citations?user=SCHOLAR_ID,https://dblp.org/pid/DBLP_ID" >> data/input.csv
   ```

2. Run CiteForge:

   ```bash
   python3 main.py
   ```

3. View results in the `output/` directory, organized by author.

## Configuration

CiteForge is highly configurable via `src/config.py`. Key parameters include:

### Publication Fetching

- `CONTRIBUTION_WINDOW_YEARS`: Time window for fetching publications (default: 3 years)
- `PUBLICATIONS_PER_YEAR`: Target publications to fetch per year (default: 50)
- `MAX_PUBLICATIONS_PER_AUTHOR`: Calculated as PUBLICATIONS_PER_YEAR × CONTRIBUTION_WINDOW_YEARS (default: 150)
- `SKIP_SERPAPI_FOR_EXISTING_FILES`: Reuse existing BibTeX files as baseline to reduce API calls (default: True)

### Duplicate Detection & Matching

- `SIM_MERGE_DUPLICATE_THRESHOLD`: Similarity threshold for deduplicating entries (default: 0.9)
- `SIM_TITLE_WEIGHT`: Weight given to title similarity in matching (default: 0.7)
- `SIM_AUTHOR_BONUS`: Bonus score for author name matches (default: 0.2)
- `SIM_YEAR_BONUS`: Bonus score for year matches within window (default: 0.2)

### Performance & Rate Limiting

- `REQUEST_DELAY_BETWEEN_ARTICLES`: Delay between processing articles (default: 0.5s)
- `HTTP_TIMEOUT_SHORT`: Timeout for API requests (default: 10s)
- `HTTP_MAX_RETRIES`: Maximum retry attempts for failed requests (default: 2)

### Citation Key Generation

- `BIBTEX_KEY_MAX_WORDS`: Maximum words in short titles for citation keys (default: 4)
- `BIBTEX_FILENAME_MAX_LENGTH`: Maximum filename length before truncation (default: 60)

## Data Sources

CiteForge aggregates data from a wide range of sources, categorized by their primary domain:

### Primary Sources

- Google Scholar (via SerpAPI)
- DBLP
- Semantic Scholar
- Crossref
- arXiv
- OpenReview

### Biomedical & Life Sciences

- PubMed
- OpenAlex
- Europe PMC

### Utilities & Metadata

- ORCID
- DataCite
- Google Gemini (for automated short title generation)

## Architecture

The system operates on a strict **Trust Hierarchy**, ensuring that the most reliable data sources take precedence:

1. CSL-JSON via DOI (Highest Trust)
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

## Testing

The project includes a comprehensive test suite with 56+ automated tests covering:

- **Core Functionality**: BibTeX parsing, citation key generation, entry type detection
- **Duplicate Detection**: DOI-based matching, title similarity, cross-source deduplication
- **Merge Policies**: Trust hierarchy validation, field conflict resolution, metadata normalization
- **Data Validation**: Field sanitization, format validation, consistency checking
- **Integration**: API client behavior, end-to-end pipeline validation, file I/O operations

Run all tests:

```bash
pytest
```

Run specific test files:

```bash
pytest tests/test_core.py          # Core BibTeX functionality
pytest tests/test_integration.py   # Integration tests
pytest tests/test_pipeline.py      # End-to-end pipeline tests
```

Run with coverage:

```bash
pytest --cov=src --cov-report=html
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. For major changes, please open an issue first to discuss what you would like to change.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
