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
CONTRIBUTION_WINDOW_YEARS = 5

# Publications per year to fetch from Scholar
# Adjust this if authors in your field publish more or fewer papers per year
PUBLICATIONS_PER_YEAR = 50

# Maximum publications to fetch from Scholar in initial bulk request
# Calculated dynamically: 50 publications/year x contribution window
# For CONTRIBUTION_WINDOW_YEARS=1, this fetches 50 publications
# For CONTRIBUTION_WINDOW_YEARS=3, this fetches 150 publications
# For CONTRIBUTION_WINDOW_YEARS=5 (default), this fetches 250 publications
MAX_PUBLICATIONS_PER_AUTHOR = PUBLICATIONS_PER_YEAR * CONTRIBUTION_WINDOW_YEARS

# Skip SerpAPI citation fetch if BibTeX file already exists
# This dramatically reduces SerpAPI usage (from 1+N to just 1 request per author)
# Set to False to always fetch fresh metadata from Scholar citation page
SKIP_SERPAPI_FOR_EXISTING_FILES = True

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

# arXiv DOI patterns (arXiv uses DOI prefix 10.48550)
# Simple check pattern for whether a DOI is an arXiv DOI
ARXIV_DOI_CHECK_PATTERN = r'10\.48550/arxiv'
# Extraction pattern to capture the arXiv ID from an arXiv DOI
ARXIV_DOI_EXTRACT_PATTERN = r'(?i)10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5})'

# HTTP request configuration
# Timeout for fast/lightweight API requests (in seconds)
HTTP_TIMEOUT_FAST = 5.0
# Default timeout for standard API requests (in seconds)
HTTP_TIMEOUT_DEFAULT = 10.0

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

# Response cache configuration
CACHE_DIR = "data/api_cache"
CACHE_TTL_SEARCH_DAYS = 30       # Stored in cache files (expiry is monthly boundary)
CACHE_TTL_DOI_DAYS = 90          # Stored in cache files (expiry is monthly boundary)
CACHE_TTL_GEMINI_DAYS = 365      # Stored in cache files (expiry is monthly boundary)
CACHE_ENABLED = True             # Master switch for response caching

# File-level deduplication threshold (used in save_entry_to_file)
# Must be >= SIM_MERGE_DUPLICATE_THRESHOLD to avoid entries passing merge but failing file save
SIM_FILE_DUPLICATE_THRESHOLD = 0.95

# Preprint detection: servers and DOI prefixes shared across modules
PREPRINT_SERVERS = frozenset({
    'arxiv', 'biorxiv', 'medrxiv', 'chemrxiv', 'research square',
    'ssrn', 'preprints', 'psyarxiv', 'socarxiv', 'edarxiv',
    'arxiv e-prints', 'e-prints', 'preprint', 'authorea',
})
PREPRINT_DOI_PREFIXES = (
    '10.48550/arxiv',     # arXiv
    '10.21203/rs.',       # Research Square
    '10.31234/osf.io',    # PsyArXiv / SocArXiv / EdArXiv (OSF Preprints)
    '10.1101/20',         # bioRxiv / medRxiv (date-prefixed manuscript IDs)
    '10.26434/chemrxiv',  # ChemRxiv
    '10.20944/preprints', # Preprints.org
    '10.2139/ssrn',       # SSRN
)

# Data repository DOI prefixes (deprioritize in DOI selection — supplementary, not the paper)
DATA_DOI_PREFIXES = (
    '10.6084/m9.figshare',  # Figshare (data/supplementary)
    '10.5281/zenodo',        # Zenodo (data/software)
)

# Relaxed title similarity for preprint/published pairs (titles may differ)
SIM_PREPRINT_TITLE_THRESHOLD = 0.5

# Known conference venue names that lack standard conference keywords
# ("proceedings", "conference", "symposium", "workshop") in their names.
# Used by determine_entry_type as a fallback when keyword detection fails.
KNOWN_CONFERENCE_VENUES = frozenset({
    "neural information processing systems",
    "advances in neural information processing systems",
    "graphics interface",
})

# Reject digit-only pages strings longer than this (SAGE/Wiley article IDs)
PAGES_MAX_DIGITS = 8

# Minimum word count for a valid publication title (reject Scholar artifacts)
MIN_TITLE_WORDS = 2

# Known generic series names that should be replaced with actual conference name
GENERIC_SERIES_NAMES = frozenset({
    "lecture notes in computer science",
    "lecture notes in artificial intelligence",
    "lecture notes in business information processing",
    "lecture notes in networks and systems",
    "communications in computer and information science",
    "advances in intelligent systems and computing",
    "studies in health technology and informatics",
    "leibniz international proceedings in informatics",
    "lipics: leibniz international proceedings in informatics",
    "dagstuhl seminar proceedings",
    "oasics: open access series in informatics",
})

# Known journal-only publisher prefixes: if a booktitle starts with one of these,
# it's actually a journal name misplaced in the booktitle field.
# Frontiers Media SA publishes only journals (no conference proceedings).
JOURNAL_ONLY_PREFIXES = (
    "frontiers in ",
)

# Author name suffixes to strip when extracting last names (e.g., "Jr", "III")
AUTHOR_NAME_SUFFIXES = frozenset({'jr', 'sr', 'ii', 'iii', 'iv', 'v'})

# Multi-signal dedup: composite score threshold
# When title sim < 0.95 but multiple weaker signals align, treat as same paper
SIM_DEDUP_COMPOSITE_THRESHOLD = 0.60

# Minimum title similarity for multi-signal dedup to even consider
# Below this, no combination of other signals should trigger a match
SIM_DEDUP_MULTI_SIGNAL_MIN = 0.35

# Internal BibTeX fields used for dedup, stripped before final output
DEDUP_INTERNAL_FIELDS = frozenset({
    "x_scholar_cluster_id",
    "x_scholar_citation_id",
    "x_s2_paper_id",
    "x_openalex_id",
})

# Threshold tolerance for floating-point precision in scoring (api_generics.py)
SIM_THRESHOLD_TOLERANCE = 0.01

# Title length ratio below which we keep the longer title (merge_utils.py)
TITLE_LENGTH_KEEP_RATIO = 0.7

# Minimum trust rank difference to override longer title (merge_utils.py)
TRUST_DIFF_OVERRIDE_THRESHOLD = 3

# Maximum parallel workers for author processing (main.py)
MAX_WORKERS = 12

# Browser-based Scholar scraping configuration
SCHOLAR_BROWSER_HEADLESS = True
SCHOLAR_BROWSER_MIN_DELAY = 2.0          # minimum delay between page loads (seconds)
SCHOLAR_BROWSER_MAX_DELAY = 5.0          # maximum delay between page loads (seconds)
SCHOLAR_BROWSER_PAGE_TIMEOUT = 30_000    # element wait timeout (milliseconds)
SCHOLAR_BROWSER_CIRCUIT_THRESHOLD = 10   # open circuit after N consecutive browser blocks
SCHOLAR_BROWSER_BACKOFF_BASE = 3.0       # back-off = base * error_count seconds
SCHOLAR_BROWSER_BACKOFF_CAP = 30.0       # maximum back-off delay (seconds)
