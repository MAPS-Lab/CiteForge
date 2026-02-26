from __future__ import annotations

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
SERPLY_BASE = "https://api.serply.io/v1/scholar"
SERPAPI_BASE = "https://serpapi.com/search"

DEFAULT_INPUT = "data/input.csv"
DEFAULT_SERPLY_KEY_FILE = "keys/Serply.key"
DEFAULT_SERPAPI_KEY_FILE = "keys/SerpAPI.key"
DEFAULT_S2_KEY_FILE = "keys/Semantic.key"
DEFAULT_OR_KEY_FILE = "keys/OpenReview.key"
DEFAULT_GEMINI_KEY_FILE = "keys/Gemini.key"

DEFAULT_OUT_DIR = "output"
CONTRIBUTION_WINDOW_YEARS = 8

# Publications per year to fetch from Scholar
PUBLICATIONS_PER_YEAR = 50

# Dynamic limit: PUBLICATIONS_PER_YEAR x CONTRIBUTION_WINDOW_YEARS
MAX_PUBLICATIONS_PER_AUTHOR = PUBLICATIONS_PER_YEAR * CONTRIBUTION_WINDOW_YEARS

# Skip Scholar citation fetch if BibTeX file already exists
SKIP_SCHOLAR_FOR_EXISTING_FILES = True

# Courtesy delay between articles to avoid hitting rate limits
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
    "openalex",     # OpenAlex (open metadata)
    "s2",           # Semantic Scholar (ML-enhanced metadata)
    "orcid",        # ORCID works (author-verified)
    "openreview",   # OpenReview (peer review platforms)
    "arxiv",        # arXiv (preprints, self-reported)
    "scholar_page",  # Scholar article page (web-scraped)
    "scholar_min",  # Scholar baseline (lowest trust, minimal data)
]

# Scoring weights for matching search results to target papers
SIM_TITLE_WEIGHT = 0.7
SIM_AUTHOR_BONUS = 0.2
SIM_YEAR_BONUS = 0.2
SIM_YEAR_MATCH_WINDOW = 1.0      # max year difference that counts as a match

# Similarity thresholds
SIM_TITLE_SIM_MIN = 0.8          # min title sim to consider a candidate
SIM_EXACT_PICK_THRESHOLD = 0.9   # auto-accept single strong candidate
SIM_BEST_ITEM_THRESHOLD = 0.8    # min score for best-of-N selection
SIM_SCHOLAR_FUZZY_ACCEPT = 0.9   # min sim for noisy Scholar data
SIM_MERGE_DUPLICATE_THRESHOLD = 0.95  # threshold for merge-level dedup

# DOI regex pattern
_DOI_REGEX = r'\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b'

# arXiv DOI patterns
ARXIV_DOI_CHECK_PATTERN = r'10\.48550/arxiv'
ARXIV_DOI_EXTRACT_PATTERN = r'(?i)10\.48550/arxiv\.([0-9]{4}\.[0-9]{4,5})'

# HTTP timeouts (seconds)
HTTP_TIMEOUT_FAST = 5.0
HTTP_TIMEOUT_DEFAULT = 10.0

# Exponential backoff
HTTP_BACKOFF_INITIAL = 0.25
HTTP_BACKOFF_MAX = 16.0
HTTP_MAX_RETRIES = 2
HTTP_RETRY_STATUS_CODES = (408, 429, 500, 502, 503, 504)

# BibTeX generation
BIBTEX_KEY_MAX_WORDS = 4
BIBTEX_FILENAME_MAX_LENGTH = 60

# Valid year range
VALID_YEAR_MIN = 1900
VALID_YEAR_MAX = 2099

# Response cache
CACHE_DIR = "data/api_cache"
CACHE_TTL_SEARCH_DAYS = 30
CACHE_TTL_DOI_DAYS = 90
CACHE_TTL_GEMINI_DAYS = 365
CACHE_ENABLED = True

# File-level dedup threshold (must be >= SIM_MERGE_DUPLICATE_THRESHOLD)
SIM_FILE_DUPLICATE_THRESHOLD = 0.95

# Preprint detection
PREPRINT_SERVERS = frozenset({
    'arxiv', 'biorxiv', 'medrxiv', 'chemrxiv', 'research square',
    'ssrn', 'social science research network',
    'preprints', 'psyarxiv', 'socarxiv', 'edarxiv',
    'arxiv e-prints', 'e-prints', 'authorea', 'techrxiv',
    'preprints.org', 'preprint server',
    'zenodo', 'agrirxiv', 'qeios',
})
PREPRINT_DOI_PREFIXES = (
    '10.48550/arxiv',     # arXiv
    '10.21203/rs.',       # Research Square
    '10.31234/osf.io',    # PsyArXiv / SocArXiv / EdArXiv (OSF Preprints)
    '10.31219/osf.io',    # OSF Preprints (generic OSF DOI prefix)
    '10.1101/',           # bioRxiv / medRxiv (all manuscript IDs)
    '10.26434/chemrxiv',  # ChemRxiv
    '10.20944/preprints', # Preprints.org
    '10.2139/ssrn',       # SSRN
    '10.64898/',          # openRxiv
    '10.36227/techrxiv',  # TechRxiv (IEEE preprints)
    '10.33774/',          # Cambridge UP preprints (Authoria/MIIR)
)

# Publishers exclusively associated with preprint servers.
# Used to strip leaked preprint publishers from published journal entries.
PREPRINT_ONLY_PUBLISHERS = frozenset({
    'openrxiv',
    'cold spring harbor laboratory',  # bioRxiv / medRxiv
    'research square',
    'authorea, inc.',
    'techrxiv',
})

# Journal names that are actually conferences (Crossref registers them as journals).
# Used by merge_utils to reclassify @article→@inproceedings.
CONFERENCE_AS_JOURNAL: frozenset[str] = frozenset({
    "software engineering",  # German SE conference (Fachtagung Softwaretechnik)
    "ijcnlp-aacl",           # Int'l Joint Conf on NLP / Asia-Pacific ACL
    "canada human-computer communications society",  # Graphics Interface publisher
})

# Strings in journal field that indicate repositories/portals, not real journals.
# @article with these → @misc (or @inproceedings if conference-like).
REPOSITORY_AS_JOURNAL: frozenset[str] = frozenset({
    "tu/e research portal",
    "escholarship",
    "california digital library",
    "eyls",
    "dspace",
})

# Data repository DOI prefixes (deprioritized in DOI selection)
DATA_DOI_PREFIXES = (
    '10.6084/m9.figshare',  # Figshare (data/supplementary)
    '10.5281/zenodo',        # Zenodo (data/software)
)

# Relaxed title similarity for preprint/published pairs
SIM_PREPRINT_TITLE_THRESHOLD = 0.55

# Conference venues that lack standard keywords (proceedings, conference, etc.)
KNOWN_CONFERENCE_VENUES = frozenset({
    "neural information processing systems",
    "advances in neural information processing systems",
    "graphics interface",
})

# Abbreviated venue names → full conference names (for S2/DBLP expansion)
ABBREVIATED_VENUE_MAP: dict[str, str] = {
    "spire": "String Processing and Information Retrieval",
    "ircdl": "Italian Research Conference on Digital Libraries",
    "wabi": "Workshop on Algorithms in Bioinformatics",
    "sea": "Symposium on Experimental Algorithms",
    "cpm": "Annual Symposium on Combinatorial Pattern Matching",
    "esa": "European Symposium on Algorithms",
    "iwoca": "International Workshop on Combinatorial Algorithms",
    "latin": "Latin American Symposium on Theoretical Informatics",
    "recomb": "Research in Computational Molecular Biology",
    "isaac": "International Symposium on Algorithms and Computation",
    "mfcs": "Mathematical Foundations of Computer Science",
    "stacs": "Symposium on Theoretical Aspects of Computer Science",
    "dcc": "Data Compression Conference",
    "alenex": "Algorithm Engineering and Experiments",
    "soda": "Symposium on Discrete Algorithms",
    "focs": "Symposium on Foundations of Computer Science",
    "stoc": "Symposium on Theory of Computing",
    "icalp": "International Colloquium on Automata, Languages and Programming",
    "lics": "Logic in Computer Science",
    "pods": "Principles of Database Systems",
    "vldb": "Very Large Data Bases",
    "sigmod": "Management of Data",
    "sigir": "Research and Development in Information Retrieval",
    "kdd": "Knowledge Discovery and Data Mining",
    "www": "The Web Conference",
    "chi": "Human Factors in Computing Systems",
    "uist": "User Interface Software and Technology",
    "cscw": "Computer-Supported Cooperative Work and Social Computing",
    "ubicomp": "Ubiquitous Computing",
    "iui": "Intelligent User Interfaces",
    "dis": "Designing Interactive Systems",
}

# Reject digit-only pages longer than this (SAGE/Wiley article IDs)
PAGES_MAX_DIGITS = 8

# Minimum word count for a valid title
MIN_TITLE_WORDS = 2

# Generic series names replaced with actual conference name during merge
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

# Booktitle prefixes that indicate a journal, not a conference (e.g., Frontiers)
JOURNAL_ONLY_PREFIXES = (
    "frontiers in ",
)

# Author name suffixes to strip when extracting last names
AUTHOR_NAME_SUFFIXES = frozenset({'jr', 'sr', 'ii', 'iii', 'iv', 'v'})

# Multi-signal dedup thresholds
SIM_DEDUP_COMPOSITE_THRESHOLD = 0.60   # composite score to treat as same paper
SIM_DEDUP_MULTI_SIGNAL_MIN = 0.35     # floor below which no signals trigger a match

# Internal BibTeX fields for dedup, stripped before final output
DEDUP_INTERNAL_FIELDS = frozenset({
    "x_scholar_cluster_id",
    "x_scholar_citation_id",
    "x_s2_paper_id",
    "x_openalex_id",
})

# Scoring tolerance for floating-point precision
SIM_THRESHOLD_TOLERANCE = 0.01

# Title merge: keep longer title if ratio below this
TITLE_LENGTH_KEEP_RATIO = 0.7

# Min trust rank difference to override longer title
TRUST_DIFF_OVERRIDE_THRESHOLD = 3

# Maximum parallel workers for author processing
MAX_WORKERS = 12

# Per-API rate limits: (tokens_per_second, burst_size)
RATE_LIMITS: dict[str, tuple[float, int]] = {
    "arxiv": (0.33, 1),           # arXiv asks for <=3 req/s; we use ~1 per 3s
    "pubmed": (0.33, 1),          # NCBI rate limit (no API key): 3 req/s
    "europepmc": (0.5, 2),        # Europe PMC is lenient
    "crossref": (1.0, 3),         # Crossref polite pool (with mailto) is generous
    "openalex": (1.0, 3),         # OpenAlex is generous
    "s2": (1.0, 3),               # S2 with API key
    "doi": (1.0, 2),              # DOI resolver
    "gemini": (0.5, 2),           # Gemini rate limit (burst=2 for retry headroom)
    "orcid": (1.0, 2),            # ORCID public API
    "datacite": (1.0, 2),         # DataCite API
    "dblp": (1.0, 2),             # DBLP search/person API
    "serply": (1.0, 2),            # Serply REST API (conservative: 1 req/s, burst 2)
    "serpapi": (1.0, 2),            # SerpAPI (conservative: 1 req/s, burst 2)
}

# Global concurrency: max simultaneous in-flight API requests
GLOBAL_CONCURRENCY_LIMIT = 16

# Session rotation interval (requests)
SESSION_ROTATION_THRESHOLD = 50

# Inter-article delay range (seconds, randomized)
REQUEST_DELAY_MIN = 0.3
REQUEST_DELAY_MAX = 1.0

# OpenReview session TTL (seconds)
OPENREVIEW_SESSION_TTL_SECS = 3600
