# CiteForge

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml/badge.svg)](https://github.com/gabrielspadon/CiteForge/actions/workflows/tests.yml)

CiteForge is a comprehensive Python tool designed for automated bibliographic data collection and enrichment. It aggregates publication metadata from over 12 academic sources, validates and merges them using trust-based policies, and exports clean, high-quality BibTeX entries.

## Key Features

- **Multi-Source Aggregation**: Seamlessly fetches data from major academic APIs including Google Scholar, Crossref, arXiv, PubMed, OpenAlex, and more.
- **Standard Library Implementation**: Built entirely using the Python 3.10+ standard library, ensuring stability and ease of deployment.
- **Trust-Based Merging**: Implements intelligent metadata selection algorithms that prioritize data based on source reliability (e.g., DOI resolvers > web scrapes).
- **DOI Validation**: Features a robust two-stage pipeline for both early and late DOI discovery and verification.
- **Smart Caching**: Optimizes API usage by caching results, significantly reducing the number of requests on subsequent runs.
- **Quality Assurance**: Generates detailed CSV reports (`summary.csv`) tracking enrichment coverage and source contribution for every entry.
- **Fuzzy Matching**: Utilizes advanced matching logic to validate metadata consistency across different sources.
- **Semantic Logging**: Replaces traditional indentation with a category-based logging system (`[FETCH]`, `[SEARCH]`, `[MATCH]`) for clearer, context-rich output.
- **Comprehensive Testing**: Fully tested core functionality with a suite of 56 automated tests.

## Quick Start

### Prerequisites

- Python 3.10 or higher
- A [SerpAPI](https://serpapi.com) key (required for Google Scholar data)

### Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/gabrielspadon/CiteForge.git
    cd CiteForge
    ```

2.  **Configure API Keys:**
    Create a `keys/` directory and add your SerpAPI key.
    ```bash
    mkdir -p keys
    echo "your_serpapi_key" > keys/SerpAPI.key
    ```

### Usage

1.  **Prepare Input Data:**
    Create a `data/input.csv` file with the authors you wish to process.
    ```bash
    echo "Name,Scholar Link,DBLP Link" > data/input.csv
    echo "Gabriel Spadon,https://scholar.google.com/citations?user=bfdGsGUAAAAJ,https://dblp.org/pid/192/1659" >> data/input.csv
    ```

2.  **Run CiteForge:**
    ```bash
    python3 main.py
    ```

3.  **View Results:**
    Output files will be generated in the `output/` directory, organized by author.

## Configuration

CiteForge is highly configurable via `src/config.py`. Key parameters include:

- `CONTRIBUTION_WINDOW_YEARS`: Time window for fetching publications (default: 3 years).
- `PUBLICATIONS_PER_YEAR`: Target number of publications to fetch per year.
- `SIM_MERGE_DUPLICATE_THRESHOLD`: Threshold for deduplicating entries.
- `REQUEST_DELAY_BETWEEN_ARTICLES`: Rate limiting to respect API usage policies.
- `HTTP_TIMEOUT_SHORT`: Timeout for short API requests (e.g., DBLP), set to 60s for reliability.

## Data Sources

CiteForge aggregates data from a wide range of sources, categorized by their primary domain:

**Primary Sources:**
- Google Scholar (via SerpAPI)
- DBLP
- Semantic Scholar
- Crossref
- arXiv
- OpenReview

**Biomedical & Life Sciences:**
- PubMed
- OpenAlex
- Europe PMC

**Utilities & Metadata:**
- ORCID
- DataCite
- Google Gemini (for automated short title generation)

## Architecture

The system operates on a strict **Trust Hierarchy**, ensuring that the most reliable data sources take precedence:

1.  **CSL-JSON via DOI** (Highest Trust)
2.  **BibTeX via DOI**
3.  **DataCite**
4.  **PubMed**
5.  **Europe PMC**
6.  **Crossref**
7.  **OpenAlex**
8.  **Semantic Scholar**
9.  **ORCID**
10. **OpenReview**
11. **arXiv**
12. **Scholar Page Metadata**
13. **Scholar Baseline**

## Testing

The project includes a comprehensive test suite. To run the tests:

```bash
pytest
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

Copyright (c) 2025 Gabriel Spadon