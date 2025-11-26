# CiteForge

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml/badge.svg)](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml)

CiteForge is a high-performance Python tool designed for automated bibliographic data collection and enrichment. It aggregates publication metadata from 12+ academic sources in parallel, validates and merges them using trust-based policies, and exports clean, high-quality BibTeX entries. With multi-threaded processing, intelligent caching, and a four-phase enrichment pipeline, CiteForge efficiently handles large-scale bibliography generation while minimizing API costs.

## Key Features

### Core Functionality

- **Multi-Source Aggregation**: Seamlessly fetches data from 12+ academic APIs including Google Scholar, Crossref, arXiv, PubMed, OpenAlex, Semantic Scholar, and more.
- **Standard Library Implementation**: Built entirely using the Python 3.10+ standard library—no external dependencies required.
- **Trust-Based Merging**: Implements intelligent metadata selection algorithms that prioritize data based on source reliability (DOI resolvers > curated databases > web scrapes).
- **DOI Validation**: Features a robust two-stage pipeline for early and late DOI discovery, with cross-validation against baseline metadata to prevent merging different papers.

### Data Quality & Intelligence

- **Smart Duplicate Detection**: Multi-stage deduplication using DOI matching, title similarity, and author overlap to prevent false positives while identifying true duplicates across sources.
- **Two-Phase DOI Validation**: Early DOI validation from baseline metadata followed by late DOI discovery across all enrichment sources, with cross-validation to prevent incorrect merges.
- **Intelligent Entry Type Detection**: Automatically distinguishes between journal articles, conference proceedings, book chapters, preprints, and datasets based on venue metadata.
- **Automated Data Cleaning**: Normalizes metadata by removing markup artifacts, standardizing field formats, and validating data consistency across enrichment sources.
- **AI-Powered Title Generation**: Optional Google Gemini integration for generating concise, semantic citation key titles with intelligent fallback to algorithmic extraction.

### Consistency & Organization

- **Systematic File Naming**: Generates consistent citation keys and filenames based on author, year, and semantic title extraction.
- **Smart Caching**: Optimizes API usage by caching results and reusing existing files as enrichment seeds, dramatically reducing requests on subsequent runs.
- **Quality Assurance**: Generates detailed CSV reports tracking enrichment coverage and source contribution for every entry.

### Performance & Scalability

- **Parallel Processing**: Multi-threaded execution processes up to 12 authors concurrently, dramatically reducing total runtime for large author lists.
- **Smart Work Distribution**: Authors are sorted by existing paper count to optimize completion time—high-volume authors finish first.
- **Per-Author Isolation**: Thread-local logging ensures each author's processing is tracked independently with dedicated log files.

### Developer Experience

- **Enhanced Logging System**: Multi-level logging with custom levels (STEP, SUCCESS), colored console output, source tagging (Scholar, Crossref, etc.), and semantic categories (FETCH, MATCH, SAVE) for crystal-clear progress tracking.
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

### Output Structure

CiteForge creates a structured output directory with detailed tracking:

```
output/
├── run.log                          # Main execution log with parallel processing summary
├── summary.csv                       # Enrichment coverage report for all entries
└── Author_Name_ID/
    ├── author.log                    # Per-author processing log
    ├── Author_2024_PaperTitle.bib    # Generated BibTeX entries
    └── ...
```

- **run.log**: Master log showing overall progress, author queuing, and completion status
- **summary.csv**: CSV report tracking which sources successfully enriched each paper
- **author.log**: Detailed per-author logs showing API calls, matches, and merge decisions

### Understanding the Logs

CiteForge uses an enhanced logging system with colored console output and semantic categories:

- **Log Levels**: INFO, STEP (workflow milestones), SUCCESS (completed actions), WARNING, ERROR
- **Source Tags**: Color-coded tags identify which API or system component generated each message (e.g., `[Scholar]`, `[Crossref]`, `[DOI]`)
- **Categories**: Semantic tags classify the type of operation (`[FETCH]`, `[SEARCH]`, `[MATCH]`, `[SAVE]`, `[SKIP]`)
- **Thread Isolation**: Console shows only main thread progress; worker threads write to per-author log files

Example log output:
```
2024-01-15 10:30:45 [STEP    ] [PLAN] Starting parallel execution with 12 workers
2024-01-15 10:30:46 [INFO    ] [Scholar] [FETCH] Requesting author publications
2024-01-15 10:30:47 [SUCCESS ] [Crossref] [MATCH] Match validated and added to enrichment
2024-01-15 10:30:48 [SUCCESS ] [System] [SAVE] Enriched: output/Author_Name/Author_2024_Title.bib
```

## Configuration

CiteForge is highly configurable via `src/config.py`. Key parameters include:

### Publication Fetching

- `CONTRIBUTION_WINDOW_YEARS`: Time window for fetching publications (default: 5 years)
- `PUBLICATIONS_PER_YEAR`: Target publications to fetch per year (default: 50)
- `MAX_PUBLICATIONS_PER_AUTHOR`: Calculated as PUBLICATIONS_PER_YEAR × CONTRIBUTION_WINDOW_YEARS (default: 250)
- `SKIP_SERPAPI_FOR_EXISTING_FILES`: Reuse existing BibTeX files as enrichment seeds to dramatically reduce SerpAPI usage—drops from 1+N to just 1 request per author (default: True, **highly recommended**)

### Duplicate Detection & Matching

- `SIM_MERGE_DUPLICATE_THRESHOLD`: Similarity threshold for deduplicating entries (default: 0.9)
- `SIM_TITLE_WEIGHT`: Weight given to title similarity in matching (default: 0.7)
- `SIM_AUTHOR_BONUS`: Bonus score for author name matches (default: 0.2)
- `SIM_YEAR_BONUS`: Bonus score for year matches within window (default: 0.2)

### Performance & Parallelization

- `REQUEST_DELAY_BETWEEN_ARTICLES`: Delay between processing articles within an author's queue (default: 0.5s)
- `max_workers`: Number of concurrent author-processing threads in `main()` (default: 12)
- `HTTP_TIMEOUT_SHORT`: Timeout for API requests (default: 10s)
- `HTTP_MAX_RETRIES`: Maximum retry attempts for failed requests with exponential backoff (default: 2)
- `HTTP_RETRY_STATUS_CODES`: HTTP status codes that trigger automatic retry (default: 408, 429, 500, 502, 503, 504)

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

### Utilities & Enhancement

- DOI.org (for resolving DOIs to CSL-JSON and BibTeX)
- DataCite (for dataset and software DOIs)
- ORCID (for author-verified publication lists)
- Google Gemini (for AI-powered short title generation in citation keys)

## Architecture

### Processing Pipeline

CiteForge uses a **four-phase enrichment pipeline** for each article:

1. **Phase 1: Early DOI Validation** - Validates DOI from baseline metadata (if present) to establish high-quality metadata early
2. **Phase 2: API Enrichment** - Queries 8+ academic APIs in parallel with candidate validation against baseline
3. **Phase 3: Late DOI Discovery** - Extracts DOI candidates from all matched sources and validates them
4. **Phase 4: Merge & Save** - Applies trust hierarchy to merge metadata and generates final BibTeX entry

### Trust Hierarchy

The merge policy operates on a strict **Trust Hierarchy**, ensuring that the most reliable data sources take precedence:

1. **CSL-JSON via DOI** (Highest Trust) - Structured metadata directly from DOI resolver
2. **BibTeX via DOI** - BibTeX format from DOI resolver
3. **DataCite** - DOIs for datasets and software
4. **PubMed** - Highly curated biomedical literature
5. **Europe PMC** - Biomedical + broader coverage
6. **Crossref** - Comprehensive academic metadata
7. **OpenAlex** - Open scholarly metadata
8. **Semantic Scholar** - ML-enhanced metadata
9. **ORCID** - Author-verified works
10. **OpenReview** - Peer review platforms
11. **arXiv** - Preprints (self-reported)
12. **Scholar Page Metadata** - Web-scraped citation data
13. **Scholar Baseline** (Lowest Trust) - Minimal metadata from search results

### Parallel Execution

CiteForge processes multiple authors concurrently using a **ThreadPoolExecutor with 12 workers**:

- Authors are sorted by existing paper count (descending) for optimal work distribution
- Each author gets isolated processing with thread-local logging
- Main thread coordinates execution and provides real-time progress updates
- Thread-safe file I/O ensures no conflicts when writing BibTeX files

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

Run specific test modules:

```bash
pytest tests/test_core.py          # Core BibTeX functionality (parsing, keys, types)
pytest tests/test_integration.py   # Integration tests (API clients, enrichment)
pytest tests/test_pipeline.py      # End-to-end pipeline tests (full workflow)
pytest tests/test_apis.py          # API client unit tests
pytest tests/test_data.py          # Data models and structures
pytest tests/test_io_csv.py        # CSV I/O operations
pytest tests/test_config.py        # Configuration validation
```

Run with coverage:

```bash
pytest --cov=src --cov-report=html
```

## Best Practices

### Optimizing API Usage

- **Enable SKIP_SERPAPI_FOR_EXISTING_FILES** (default: True) to reuse existing BibTeX files as enrichment seeds—this reduces SerpAPI calls from 1+N to just 1 per author
- **Use Semantic Scholar API key** for improved CS/ML paper metadata quality
- **Run incrementally**: Process a subset of authors first, then add more—existing papers are automatically reused

### Monitoring Progress

- **Watch run.log** for overall progress and parallel execution status
- **Check author.log** files for detailed per-author processing and troubleshooting
- **Review summary.csv** to identify papers with low enrichment coverage

### Handling Rate Limits

- Adjust `REQUEST_DELAY_BETWEEN_ARTICLES` if you encounter rate limiting (increase from 0.5s to 1-2s)
- Reduce `max_workers` in `main()` if processing too many authors causes API throttling
- Most APIs have generous rate limits, but SerpAPI has a monthly quota—enable file reuse to conserve credits

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. For major changes, please open an issue first to discuss what you would like to change.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
