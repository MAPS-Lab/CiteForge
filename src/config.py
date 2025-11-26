from __future__ import annotations

SERPAPI_BASE = "https://serpapi.com/search"
S2_BASE = "https://api.semanticscholar.org/graph/v1"
CROSSREF_BASE = "https://api.crossref.org/works"
ARXIV_BASE = "https://export.arxiv.org/api/query"
OPENREVIEW_BASE = "https://api.openreview.net"
DBLP_BASE = "https://dblp.org/search/author/api"
DBLP_PERSON_BASE = "https://dblp.org/pid"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
OPENALEX_BASE = "https://api.openalex.org/works"
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EUROPEPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DATACITE_BASE = "https://api.datacite.org/dois"
ORCID_BASE = "https://pub.orcid.org/v3.0"

DEFAULT_INPUT = "data/input.csv"
DEFAULT_KEY_FILE = "keys/SerpAPI.key"
DEFAULT_S2_KEY_FILE = "keys/Semantic.key"
DEFAULT_OR_KEY_FILE = "keys/OpenReview.key"
DEFAULT_GEMINI_KEY_FILE = "keys/Gemini.key"
DEFAULT_DICTIONARY_FILE = "data/cache.json"

DEFAULT_OUT_DIR = "output"
PAPERS_DIR = "papers"
CONTRIBUTION_WINDOW_YEARS = 5

# Publications per year to fetch from Scholar
# Adjust this if authors in your field publish more or fewer papers per year
PUBLICATIONS_PER_YEAR = 50

# Maximum publications to fetch from Scholar in initial bulk request
# Calculated dynamically: 50 publications/year × contribution window
# For CONTRIBUTION_WINDOW_YEARS=1, this fetches 50 publications
# For CONTRIBUTION_WINDOW_YEARS=3, this fetches 150 publications
# For CONTRIBUTION_WINDOW_YEARS=5 (default), this fetches 250 publications
MAX_PUBLICATIONS_PER_AUTHOR = PUBLICATIONS_PER_YEAR * CONTRIBUTION_WINDOW_YEARS

# Skip SerpAPI citation fetch if BibTeX file already exists
# This dramatically reduces SerpAPI usage (from 1+N to just 1 request per author)
# Set to False to always fetch fresh metadata from Scholar citation page
SKIP_SERPAPI_FOR_EXISTING_FILES = True

# Enable selective Scholar re-fetch for incomplete data
# Set to False to completely disable Scholar refetch (fastest, but may miss data)
ENABLE_SCHOLAR_REFETCH_FOR_INCOMPLETE = True

# Truncation score threshold for considering data "incomplete"
# 0.5 = 50% or more fields have truncation markers (e.g., "...", "et al.")
# Higher = more tolerant (fewer refetches), Lower = stricter (more refetches)
INCOMPLETE_DATA_THRESHOLD = 0.5

# wait between processing articles to avoid hitting rate limits
# This now applies mainly to non-Scholar enrichment sources
REQUEST_DELAY_BETWEEN_ARTICLES = 0.5

# Trust hierarchy for merging metadata from different sources.
# Sources earlier in the list are more reliable than those later.
# This ordering reflects data quality, completeness, and standardization.
TRUST_ORDER = [
    "csl",          # DOI → CSL-JSON (highest trust, structured metadata)
    "doi_bibtex",   # DOI → BibTeX (direct from DOI resolver)
    "datacite",     # DataCite DOIs (datasets/software, structured)
    "pubmed",       # PubMed/NIH (biomedical, highly curated)
    "europepmc",    # Europe PMC (biomedical + broader coverage)
    "crossref",     # Crossref API (broad academic coverage)
    "openalex",     # OpenAlex (comprehensive, open metadata)
    "s2",           # Semantic Scholar (ML-enhanced metadata)
    "orcid",        # ORCID works (author-verified)
    "openreview",   # OpenReview (peer review platforms)
    "arxiv",        # arXiv (preprints, self-reported)
    "scholar_page",  # Scholar article page (web-scraped)
    "scholar_min",  # Scholar baseline (lowest trust, minimal data)
]

# scoring configuration for matching search results to target papers
# these values describe how much we care about titles, authors, and years when
# deciding if a result is a good match
# title similarity has the strongest influence because noticeably different
# titles usually indicate different publications
SIM_TITLE_WEIGHT = 0.7

# extra score awarded when author names line up, which helps distinguish
# between papers with similar or overlapping titles
SIM_AUTHOR_BONUS = 0.2

# extra score awarded when publication years are close enough to be considered
# the same edition or version of a work
SIM_YEAR_BONUS = 0.2

# maximum year difference that still counts as a match, which allows for
# preprints and final publications appearing in adjacent years
SIM_YEAR_MATCH_WINDOW = 1.0

# lower bound on title similarity; results below this value are treated as
# unrelated even if other fields match
SIM_TITLE_SIM_MIN = 0.8

# confidence threshold for accepting a single strong candidate as the correct
# match without further ambiguity
SIM_EXACT_PICK_THRESHOLD = 0.9

# confidence threshold for picking the best match among several candidates;
# results below this are considered too uncertain
SIM_BEST_ITEM_THRESHOLD = 0.8

# minimum similarity required when working with noisy Scholar-derived data, to
# avoid accepting weak or misleading matches
SIM_SCHOLAR_FUZZY_ACCEPT = 0.9

# similarity level above which two records are treated as the same publication
# when merging duplicate entries from different sources
SIM_MERGE_DUPLICATE_THRESHOLD = 0.9

# pattern for finding DOIs in text
# DOIs start with "10." then have a directory code and a suffix
_DOI_REGEX = r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b'

# HTTP request configuration
# Default timeout for HTTP requests (in seconds)
HTTP_TIMEOUT_DEFAULT = 5.0
HTTP_TIMEOUT_SHORT = 10.0

# Exponential backoff configuration for retries
HTTP_BACKOFF_INITIAL = 0.25  # Initial backoff delay in seconds
HTTP_BACKOFF_MAX = 16.0      # Maximum backoff delay in seconds
HTTP_MAX_RETRIES = 2         # Maximum number of retry attempts

# HTTP status codes that should trigger retries
HTTP_RETRY_STATUS_CODES = (408, 429, 500, 502, 503, 504)

# BibTeX generation configuration
# Maximum words to use from title for citation key generation
BIBTEX_KEY_MAX_WORDS = 4

# Maximum length for filename truncation
BIBTEX_FILENAME_MAX_LENGTH = 60

# Valid year range for publications
VALID_YEAR_MIN = 1900
VALID_YEAR_MAX = 2099
