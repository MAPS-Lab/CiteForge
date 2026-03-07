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
DEFAULT_A2I2_INPUT = "data/a2i2.csv"
A2I2_OUTPUT_DIR = "a2i2"
CONTRIBUTION_WINDOW_YEARS = 7

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
CACHE_TTL_SEARCH_DAYS = 60
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

# Strings in journal/booktitle that indicate repositories/portals, not real venues.
# @article/@inproceedings with these → @misc.
REPOSITORY_AS_JOURNAL: frozenset[str] = frozenset({
    "tu/e research portal",
    "escholarship",
    "california digital library",
    "eyls",
    "dspace",
    "zenodo",
    "cern european organization",
    "figshare",
    "underline science",
    "research portal",
    "osti",
})

# Institutional repositories → entries should be @phdthesis (if thesis) or @misc
INSTITUTIONAL_REPOSITORIES: frozenset[str] = frozenset({
    "deep blue",
    "uwspace",
    "prism",
    "mspace",
    "tspace",
})

# Data repository DOI prefixes (deprioritized in DOI selection)
DATA_DOI_PREFIXES = (
    '10.6084/m9.figshare',  # Figshare (data/supplementary)
    '10.5281/zenodo',        # Zenodo (data/software)
)

# Relaxed title similarity for preprint/published pairs
SIM_PREPRINT_TITLE_THRESHOLD = 0.55

# Journals whose names contain "Proceedings" but are NOT conference proceedings.
# These override _is_conference_journal() to stay as @article.
JOURNALS_NAMED_PROCEEDINGS: frozenset[str] = frozenset({
    "proceedings of the national academy of sciences",
    "proceedings of the vldb endowment",
    "proceedings of the ieee",
    "proceedings of the royal society",
})

# Procedia/IFAC series: published as journal volumes but are conference proceedings.
# @article with these as journal → @inproceedings (journal → booktitle).
PROCEEDINGS_SERIES_AS_JOURNAL: frozenset[str] = frozenset({
    "procedia cirp",
    "procedia computer science",
    "procedia manufacturing",
    "procedia engineering",
    "ifac-papersonline",
    "ifac papersonline",
    "frontiers in artificial intelligence and applications",
})

# ACM PACM journals: named "Proceedings of the ACM" but are real journals.
# @inproceedings with these as booktitle → @article (booktitle → journal).
ACM_JOURNAL_PROCEEDINGS: frozenset[str] = frozenset({
    "proceedings of the acm on human-computer interaction",
    "proceedings of the acm on networking",
    "proceedings of the acm on software engineering",
    "proceedings of the acm on programming languages",
    "proceedings of the acm on interactive, mobile, wearable and ubiquitous technologies",
    "proceedings of the acm on management of data",
    "pacm on human-computer interaction",
    "pacm on networking",
    "pacm on software engineering",
    "pacm hci",
})

# Publisher corrections: {journal_lower_substring: correct_publisher}
PUBLISHER_CORRECTIONS: dict[str, str] = {
    "journal of computational biology": "Mary Ann Liebert",
    "computational and structural biotechnology journal": "Elsevier",
    "veterinary sciences": "MDPI AG",
}

# ALL-CAPS venue names → proper case (API sources sometimes return ALL-CAPS)
VENUE_CASE_CORRECTIONS: dict[str, str] = {
    "DIGITAL HEALTH": "Digital Health",
    "CEUR WORKSHOP PROCEEDINGS": "CEUR Workshop Proceedings",
    "AI \\& SOCIETY": "AI \\& Society",
    "Genome biology and evolution": "Genome Biology and Evolution",
}

# Acronym case corrections for title fields (API sources sometimes return
# incorrect casing for well-known acronyms). Keys are the wrong form, values
# are the correct form.  Applied via word-boundary regex on title fields.
ACRONYM_CASE_CORRECTIONS: dict[str, str] = {
    "Iot": "IoT",
    "Nims": "NIMS",
    "Ai": "AI",
}

# Conference venues that lack standard keywords (proceedings, conference, etc.)
KNOWN_CONFERENCE_VENUES = frozenset({
    "neural information processing systems",
    "advances in neural information processing systems",
    "graphics interface",
})

# Keywords in venue strings that indicate conference proceedings (shared by
# bibtex_build.determine_entry_type and publication_parser pattern matching).
CONFERENCE_KEYWORDS: tuple[str, ...] = (
    "proceedings", "conference", "symposium", "workshop",
    "meeting", "summit", "congress", "colloquium",
    "chapter of the association",  # NAACL, EACL, AACL, etc.
    "findings of",  # ACL/EMNLP workshop findings
    "lecture notes in computer science",  # LNCS is a conference proceedings series
    "medinfo",  # Medical informatics (IOS Press SHTI series)
    "studies in health technology and informatics",  # IOS Press (SHTI)
)

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
    "nime": "New Interfaces for Musical Expression",
    "nime 2021": "New Interfaces for Musical Expression",
    "nime 2022": "New Interfaces for Musical Expression",
    "egu": "EGU General Assembly",
    "aimc": "AI Music Creativity Conference",
    "wnut": "Workshop on Noisy User-generated Text",
    "nlp4musa": "NLP for Music and Audio Workshop",
    # --- Session 26, Iteration 1: New venue expansions ---
    "collas": "Conference on Lifelong Learning Agents",
    "ahfe international": "Applied Human Factors and Ergonomics International",
    "bcss@persuasive": "Behavior Change Support Systems Workshop",
    "iberlef@sepln": "Iberian Languages Evaluation Forum",
    "master@pkdd/ecml": "MASTER Workshop on Multiple-Aspect Analysis of Semantic Trajectories",
    "interaccion": "Interacción",
    "robocup 2022:": "RoboCup 2022: Robot World Cup XXVI",
    "cvpr workshop": "CVPR Workshop on Generative Models for Computer Vision",
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
    "international journal of ",
    "journal of ",
    "ieee transactions on ",
    "ieee journal ",
    "acm transactions on ",
)

# Author name suffixes to strip when extracting last names
AUTHOR_NAME_SUFFIXES = frozenset({'jr', 'sr', 'ii', 'iii', 'iv', 'v'})

# Fused compound words: hyphens stripped by Google Scholar.
# Maps lowercased fused form → correctly hyphenated replacement.
# Suffixes that reliably form hyphenated compound adjectives in scientific text.
# Used by _fix_fused_compounds() as a fallback after dictionary lookup.
# The suffix approach matches words like "Knowledgedriven" → "Knowledge-Driven"
# when the prefix has ≥3 characters starting with an uppercase letter.
COMPOUND_SUFFIXES: tuple[str, ...] = (
    "based", "driven", "aware", "oriented", "informed", "powered", "defined",
    "assisted", "enriched", "preserved", "focused", "engaged", "organized",
    "grained", "specific", "dependent", "efficient", "adaptive", "cooperative",
    "free", "level", "dimensional", "sensitive", "agnostic",
    "centric", "intensive", "delivered",
    "enhanced", "enhancing", "induced", "enabled", "augmented", "conditioned",
    "embedded", "preserving", "centered", "aided",
)

# Dictionary of fused compound words for cases NOT caught by suffix-based detection:
# - Acronym prefixes (AI, FM, EEG, 6G, D2D, DNS, etc.)
# - Short prefixes (In, E, Low, etc.)
# - Irregular patterns (realtime, objectoriented, etc.)
# - Multi-word compounds (stateoftheart, endtoend, etc.)
FUSED_COMPOUND_WORDS: dict[str, str] = {
    # --- Multi-word compounds ---
    "stateoftheart": "State-of-the-Art",
    "endtoend": "End-to-End",
    "end-toend": "End-to-End",
    # --- Acronym prefixes (not caught by suffix regex: [A-Z][a-z]{2,}) ---
    "aidriven": "AI-Driven",
    "aipowered": "AI-Powered",
    "aibased": "AI-Based",
    "eegbased": "EEG-Based",
    "eegdriven": "EEG-Driven",
    "dnsbased": "DNS-Based",
    "fmindex": "FM-Index",
    "solariasensor": "SOLARIA-SensOr",
    "d2dassisted": "D2D-Assisted",
    "6gempowered": "6G-Empowered",
    "llmbased": "LLM-Based",
    "llmpowered": "LLM-Powered",
    "sdnbased": "SDN-Based",
    "ganbased": "GAN-Based",
    "drlbased": "DRL-Based",
    "iotbased": "IoT-Based",
    "gnntransformerbased": "GNN-Transformer-Based",
    "chatgptbased": "ChatGPT-Based",
    "rnaitractable": "RNAi-Tractable",
    # --- Short prefixes (< 3 lowercase chars after initial capital) ---
    "innetwork": "In-Network",
    "ecommerce": "E-Commerce",
    # --- "Real-Time" and similar non-suffix patterns ---
    "realtime": "Real-Time",
    "longterm": "Long-Term",
    "shortterm": "Short-Term",
    "lowcost": "Low-Cost",
    "lowpower": "Low-Power",
    "lowlatency": "Low-Latency",
    "lowcomplexity": "Low-Complexity",
    "lowresource": "Low-Resource",
    "lowdimensional": "Low-Dimensional",
    "lowfrequency": "Low-Frequency",
    "highlevel": "High-Level",
    "highperformance": "High-Performance",
    "higherorder": "Higher-Order",
    # --- Irregular patterns (suffix not in COMPOUND_SUFFIXES) ---
    "realworld": "Real-World",
    "objectoriented": "Object-Oriented",
    "firstorder": "First-Order",
    "worstcase": "Worst-Case",
    "breadthfirst": "Breadth-First",
    "longread": "Long-Read",
    "shortread": "Short-Read",
    "prefixfree": "Prefix-Free",
    "donorrecipient": "Donor-Recipient",
    "arabicenglish": "Arabic-English",
    "physicianpatient": "Physician-Patient",
    "dialoguenote": "Dialogue-Note",
    "microcluster": "Micro-Cluster",
    "earlystage": "Early-Stage",
    "performanceenergy": "Performance-Energy",
    "transportlayer": "Transport-Layer",
    "conceptlevel": "Concept-Level",
    "countylevel": "County-Level",
    "sessionlevel": "Session-Level",
    "selforganizing": "Self-Organizing",
    "selftracking": "Self-Tracking",
    "multiaccess": "Multi-Access",
    "multiagent": "Multi-Agent",
    "multiarmed": "Multi-Armed",
    "multiobjective": "Multi-Objective",
    "multistage": "Multi-Stage",
    "multitask": "Multi-Task",
    "multimodal": "Multi-Modal",
    "multiscale": "Multi-Scale",
    "multirequest": "Multi-Request",
    "crosslingual": "Cross-Lingual",
    "crossdomain": "Cross-Domain",
    "crossspecies": "Cross-Species",
    "crosslayer": "Cross-Layer",
    "crossentropy": "Cross-Entropy",
    "crossdevice": "Cross-Device",
    "noncooperative": "Non-Cooperative",
    "nonshared": "Non-Shared",
    "metalearning": "Meta-Learning",
    "metaanalysis": "Meta-Analysis",
    "testtime": "Test-Time",
    "obsessivecompulsive": "Obsessive-Compulsive",
    "genomewide": "Genome-Wide",
    "selfsupervised": "Self-Supervised",
    "largescale": "Large-Scale",
    "finetuning": "Fine-Tuning",
    "finetuned": "Fine-Tuned",
    "noninvasive": "Non-Invasive",
    "semisupervised": "Semi-Supervised",
    "braincomputer": "Brain-Computer",
    "hybridpointing": "Hybrid-Pointing",
    "sexdependent": "Sex-Dependent",
    "geneenvironment": "Gene-Environment",
    "yalebrown": "Yale-Brown",
    "interdataset": "Inter-Dataset",
    "eyetracking": "Eye-Tracking",
    "zeroshot": "Zero-Shot",
    "fewshot": "Few-Shot",
    "pretrained": "Pre-Trained",
    "pretraining": "Pre-Training",
    "fullduplex": "Full-Duplex",
    "halfduplex": "Half-Duplex",
    "deeplearning": "Deep-Learning",
    "reinforcementlearning": "Reinforcement-Learning",
    "machinelearning": "Machine-Learning",
    "openaccess": "Open-Access",
    "opensource": "Open-Source",
    "energyefficient": "Energy-Efficient",
    "costeffective": "Cost-Effective",
    "softwaredefined": "Software-Defined",
    "timecritical": "Time-Critical",
    "phonebased": "Phone-Based",
    "populationbased": "Population-Based",
    "taskspecific": "Task-Specific",
    # --- Layer/tracking compounds (not in COMPOUND_SUFFIXES due to false positives) ---
    "physicallayer": "Physical-Layer",
    # --- Bio/Medical compounds ---
    "sarscov": "SARS-CoV",
    "posttraumatic": "Post-Traumatic",
    "attentiondeficit": "Attention-Deficit",
    "treatmentemergent": "Treatment-Emergent",
    "nonpharmaceutical": "Non-Pharmaceutical",
    "genderneutral": "Gender-Neutral",
    "disorderspecific": "Disorder-Specific",
    # --- Math/Physics/Engineering compounds ---
    "mixedinteger": "Mixed-Integer",
    "nonorthogonal": "Non-Orthogonal",
    "nonstochastic": "Non-Stochastic",
    "twosample": "Two-Sample",
    "twostep": "Two-Step",
    "foursided": "Four-Sided",
    "dualactivebridge": "Dual-Active-Bridge",
    # --- Computing/Systems compounds ---
    "cyberphysical": "Cyber-Physical",
    "cybertwinenabled": "Cyber-Twin-Enabled",
    "offchain": "Off-Chain",
    "edgecloud": "Edge-Cloud",
    "blackbox": "Black-Box",
    "freeform": "Free-Form",
    "narrowbandiot": "Narrowband-IoT",
    "singlebranch": "Single-Branch",
    "qlearning": "Q-Learning",
    "neuralnetworkbased": "Neural-Network-Based",
    "deepmodelbased": "Deep-Model-Based",
    "reinforcementlearningbased": "Reinforcement-Learning-Based",
    "machinelearningbased": "Machine-Learning-Based",
    "deeplearningbased": "Deep-Learning-Based",
    # --- Network/Communications compounds ---
    "uavenabled": "UAV-Enabled",
    "uavspecific": "UAV-Specific",
    "risenabled": "RIS-Enabled",
    "cellfree": "Cell-Free",
    "modelfree": "Model-Free",
    "modelbased": "Model-Based",
    "satelliteground": "Satellite-Ground",
    "revenuemaximizing": "Revenue-Maximizing",
    "nextgeneration": "Next-Generation",
    "multitenant": "Multi-Tenant",
    "multiuser": "Multi-User",
    "multiview": "Multi-View",
    # --- General compounds ---
    "insitu": "In-Situ",
    "selfmanagement": "Self-Management",
    "decisionmaking": "Decision-Making",
    "questionanswering": "Question-Answering",
    "highfrequency": "High-Frequency",
    "hightech": "High-Tech",
    "spatialtemporal": "Spatio-Temporal",
    "spatiotemporal": "Spatio-Temporal",
    "graphbased": "Graph-Based",
    "failureaware": "Failure-Aware",
    "datainformed": "Data-Informed",
    "datadriven": "Data-Driven",
    "delaysensitive": "Delay-Sensitive",
    # --- Iteration 3: Multi-* compounds ---
    "multisource": "Multi-Source",
    "multisensor": "Multi-Sensor",
    "multiclass": "Multi-Class",
    "multirobot": "Multi-Robot",
    "multioperator": "Multi-Operator",
    "multihead": "Multi-Head",
    "multiantenna": "Multi-Antenna",
    "multitimescale": "Multi-Timescale",
    "multiservice": "Multi-Service",
    "multisatellite": "Multi-Satellite",
    "multimask": "Multi-Mask",
    "multilayered": "Multi-Layered",
    "multigranular": "Multi-Granular",
    "multiclient": "Multi-Client",
    "multichatbot": "Multi-Chatbot",
    "multicell": "Multi-Cell",
    "multiband": "Multi-Band",
    "multiattribute": "Multi-Attribute",
    "multilabel": "Multi-Label",
    "multiphase": "Multi-Phase",
    # --- Iteration 3: Cross-* compounds ---
    "crossencoder": "Cross-Encoder",
    "crosstier": "Cross-Tier",
    "crossplatform": "Cross-Platform",
    # --- Iteration 3: Self-* compounds ---
    "selfefficacy": "Self-Efficacy",
    "selfsampled": "Self-Sampled",
    "selfregulated": "Self-Regulated",
    "selfimproving": "Self-Improving",
    "selfimprovement": "Self-Improvement",
    # --- Iteration 3: Non-* compounds ---
    "nonvisual": "Non-Visual",
    "nonuniform": "Non-Uniform",
    "nonresponsive": "Non-Responsive",
    "nonreproducible": "Non-Reproducible",
    "nonindigenous": "Non-Indigenous",
    "nondifferentiable": "Non-Differentiable",
    "noncardiac": "Non-Cardiac",
    # --- Iteration 3: Post-/Pre-/Semi- compounds ---
    "postquantum": "Post-Quantum",
    "postketamine": "Post-Ketamine",
    "preservice": "Pre-Service",
    "semistructured": "Semi-Structured",
    "semishuffling": "Semi-Shuffling",
    # --- Iteration 3: Co-* compounds ---
    "codesigning": "Co-Designing",
    "comonitoring": "Co-Monitoring",
    "coembeddings": "Co-Embeddings",
    "copresence": "Co-Presence",
    "colourblending": "Colour-Blending",
    # --- Iteration 3: Suffix compounds not caught by COMPOUND_SUFFIXES ---
    "highfidelity": "High-Fidelity",
    "highresolution": "High-Resolution",
    "quantumsafe": "Quantum-Safe",
    "proteomewide": "Proteome-Wide",
    "gametheoretical": "Game-Theoretical",
    "dataperturbed": "Data-Perturbed",
    "genderaffirming": "Gender-Affirming",
    "traderelated": "Trade-Related",
    "opioidrelated": "Opioid-Related",
    # --- Iteration 3: Acronym-prefix compounds ---
    "irbased": "IR-Based",
    "itbased": "IT-Based",
    "dagbased": "DAG-Based",
    "arbased": "AR-Based",
    "risassisted": "RIS-Assisted",
    "ailiteracy": "AI-Literacy",
    # --- Iteration 3: Miscellaneous compounds ---
    "twoway": "Two-Way",
    "ultradense": "Ultra-Dense",
    "polynomialtime": "Polynomial-Time",
    "workingclass": "Working-Class",
    "actorcritic": "Actor-Critic",
    "sinewave": "Sine-Wave",
    "reallife": "Real-Life",
    "batteryless": "Battery-Less",
    "zeroday": "Zero-Day",
    "openweight": "Open-Weight",
    "openmindedness": "Open-Mindedness",
    "interfunctional": "Inter-Functional",
    "dualtrack": "Dual-Track",
    "netzero": "Net-Zero",
    "cuttingedge": "Cutting-Edge",
    "subsaharan": "Sub-Saharan",
    "superresolution": "Super-Resolution",
    "audiohaptic": "Audio-Haptic",
    "singleuse": "Single-Use",
    "timeseries": "Time-Series",
    "timespace": "Time-Space",
    "timediscretized": "Time-Discretized",
    "graphevolution": "Graph-Evolution",
    "keyphraseitem": "Keyphrase-Item",
    "overdisclosure": "Over-Disclosure",
    # --- Iteration 3: Partially-fused multi-word compounds ---
    "over-theair": "Over-the-Air",
    "out-ofdistribution": "Out-of-Distribution",
    "internet-ofthingsenabled": "Internet-of-Things-Enabled",
    "internet-ofthings": "Internet-of-Things",
    "text-tomusic": "Text-to-Music",
    "digitaltwinenabled": "Digital-Twin-Enabled",
    "spaceairmarine": "Space-Air-Marine",
    "aerialmarine": "Aerial-Marine",
    # --- Session 25, Iteration 1: Partially-fused multi-word compounds ---
    "state-of-theart": "State-of-the-Art",
    "out-ofview": "Out-of-View",
    "out-ofdomain": "Out-of-Domain",
    "sum-oflocaleffects": "Sum-of-Local-Effects",
    "first-inhuman": "First-in-Human",
    "llms-asjudges": "LLMs-as-Judges",
    "sequence-tosequence": "Sequence-to-Sequence",
    "text-toimage": "Text-to-Image",
    "earth-tomars": "Earth-to-Mars",
    "modelintheloop": "Model-in-the-Loop",
    "learntorecommend": "Learn-to-Recommend",
    "delaytrajectoryaccuracy": "Delay-Trajectory-Accuracy",
    # --- Session 25, Iteration 1: Acronym-prefix compounds ---
    "iotenabled": "IoT-Enabled",
    "aienabled": "AI-Enabled",
    "aiassisted": "AI-Assisted",
    "aiadaptive": "AI-Adaptive",
    "llmgenerated": "LLM-Generated",
    "nfvenabled": "NFV-Enabled",
    "uavcooperative": "UAV-Cooperative",
    "fhircompliant": "FHIR-Compliant",
    "dsgvocompliance": "DSGVO-Compliance",
    "bwtruns": "BWT-Runs",
    "slpcompressed": "SLP-Compressed",
    "krakenlike": "KRAKEN-Like",
    "smemfinding": "SMEM-Finding",
    "enigmaataxia": "ENIGMA-Ataxia",
    # --- Session 25, Iteration 1: Regular compound adjectives ---
    "whitebox": "White-Box",
    "communityacquired": "Community-Acquired",
    "allcause": "All-Cause",
    "humancomputer": "Human-Computer",
    "humananimalcomputer": "Human-Animal-Computer",
    "referenceguided": "Reference-Guided",
    "motivationalcognitive": "Motivational-Cognitive",
    "misogynymotivated": "Misogyny-Motivated",
    "impulsivitycompulsivity": "Impulsivity-Compulsivity",
    "informationgeometric": "Information-Geometric",
    "focaluncertainty": "Focal-Uncertainty",
    "keypointtrajectory": "Keypoint-Trajectory",
    "metaaugmentation": "Meta-Augmentation",
    "metadimensionality": "Meta-Dimensionality",
    "minimumlength": "Minimum-Length",
    # --- Session 25, Iteration 2: Fused words at word boundaries ---
    "burrowswheeler": "Burrows-Wheeler",
    "populationscale": "Population-Scale",
    "visionlanguage": "Vision-Language",
    "driftaligned": "Drift-Aligned",
    "safetycritical": "Safety-Critical",
    "doubleedge": "Double-Edge",
    "grammarbased": "Grammar-Based",
    "ehealth": "eHealth",
    "satelliteterrestrial": "Satellite-Terrestrial",
    "twotier": "Two-Tier",
    "stackelbergbargaining": "Stackelberg-Bargaining",
    "multipleaspect": "Multiple-Aspect",
    "energyawareness": "Energy-Awareness",
    "scalesecond": "Scale-Second",
    "zerotargetassumption": "Zero-Target-Assumption",
    "multipsych": "MULTI-PSYCH",
    # --- Session 25, Iteration 2: Partially-fused multi-word compounds ---
    "model-in-theloop": "Model-in-the-Loop",
    "learn-torecommend": "Learn-to-Recommend",
    "diffuse-anddenoise": "Diffuse-and-Denoise",
    # --- Session 25, Iteration 3: Acronym-prefix compounds ---
    "sdiot": "SD-IoT",
    "gaoptimized": "GA-Optimized",
    "gptwritingprompts": "GPT-WritingPrompts",
    "legoprover": "LEGO-Prover",
    "ligdoctor": "LIG-Doctor",
    "stxvote": "STX-Vote",
    "tsdetector": "TS-Detector",
    "oodprobe": "OOD-Probe",
    "veremiextension": "VeReMi-Extension",
    # --- Session 25, Iteration 3: Fused words at word boundaries ---
    "emotionsemantic": "Emotion-Semantic",
    "resumejob": "Resume-Job",
    # --- Session 25, Iteration 3: Em-dash loss ---
    "chatgptis": "ChatGPT---Is",
    # --- Session 26, Iteration 1: Space-loss fix ---
    "eventdata": "Event Data",
    # --- Session 26, Iteration 2: Fused compounds ---
    "postprocessing": "Post-Processing",
    "welfareintegrating": "Welfare---Integrating",
    "wellbeing": "Well-Being",
    # --- Session 26, Iteration 3: Fused compound adjectives ---
    "climatesmart": "Climate-Smart",
    "convergencerate": "Convergence-Rate",
    "earlywarning": "Early-Warning",
    "errorbounded": "Error-Bounded",
    "factcheckers": "Fact-Checkers",
    "factchecks": "Fact-Checks",
    "faulttolerance": "Fault-Tolerance",
    "fixedparameter": "Fixed-Parameter",
    "groundtruth": "Ground-Truth",
    "hyperconnected": "Hyper-Connected",
    "largedisplay": "Large-Display",
    "machinereadable": "Machine-Readable",
    "machinetype": "Machine-Type",
    "mixedmethods": "Mixed-Methods",
    "motioncaptured": "Motion-Captured",
    "oneclass": "One-Class",
    "partiallyobservable": "Partially-Observable",
    "pointline": "Point-Line",
    "quasicomponent": "Quasi-Component",
    "ridehailing": "Ride-Hailing",
    "sharedencoder": "Shared-Encoder",
    "statevector": "State-Vector",
    "terabytesized": "Terabyte-Sized",
    "transhierarchy": "Trans-Hierarchy",
    "treechild": "Tree-Child",
    "variancegated": "Variance-Gated",
    "weightloss": "Weight-Loss",
    # --- Session 26, Iteration 3: Space-loss / name compounds ---
    "wolffacial": "Wolf Facial",
    "eulertour": "Euler-Tour",
    "glyphfield": "Glyph-Field",
    # --- Session 26, Iteration 3: Em-dash loss ---
    "emotionssensor": "Emotions---Sensor",
    "farmingadvancing": "Farming---Advancing",
    "livestockartificial": "Livestock---Artificial",
    "managementpart": "Management---Part",
    "servicespart": "Services---Part",
    # --- Session 30, Iteration 1: Colon/slash/hyphen loss ---
    "recognitionthe": "recognition: The",
    "methanethe": "Methane: The",
    "frequencylow": "Frequency/Low",
    "spacetime": "Space-Time",
    "elearning": "E-Learning",
    "eprescription": "e-Prescription",
    # --- Session 30, Iteration 2: Fused compounds ---
    "runlength": "Run-Length",
    "incontext": "In-Context",
    # --- Session 30, Iteration 3 (deep agent scan): Fused compounds ---
    "sexdifferentiated": "Sex-Differentiated",
    "sizefractionated": "Size-Fractionated",
    "frontohippocampal": "Fronto-Hippocampal",
    "tradeoff": "Trade-Off",
    # --- Session 33, Iteration 1: Fused compounds ---
    "nbody": "N-Body",
    # --- Session 33, Iteration 1: Space-loss fix ---
    "ofthe": "of the",
}

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

# SerpAPI publication string parsing thresholds
PUB_PARSE_TIER1_MIN_CONFIDENCE = 0.5   # Minimum confidence for venue-based API search
PUB_PARSE_TIER2_MIN_CONFIDENCE = 0.7   # Minimum confidence for direct field population
