# Each pattern is matched at the start of a lower-cased DOI.
PREPRINT_SERVERS: tuple[tuple[str, str], ...] = (
    (r"10\.48550/arxiv", "arXiv"),
    (r"10\.21203/rs\.", "Research Square"),
    (r"10\.31234/osf\.io", "OSF Preprints"),
    (r"10\.31219/osf\.io", "OSF Preprints"),
    # Cold Spring Harbor journals share 10.1101, so only the bioRxiv and medRxiv numbering marks a preprint
    (r"10\.1101/(?:\d{4}\.\d{2}\.\d{2}\.)?\d+(?:v\d+)?$", "bioRxiv"),
    (r"10\.26434/chemrxiv", "ChemRxiv"),
    (r"10\.20944/preprints", "Preprints.org"),
    (r"10\.2196/preprints\.", "JMIR Preprints"),
    (r"10\.2139/", "SSRN"),
    (r"10\.13140/", "ResearchGate"),
    (r"10\.64898/", "openRxiv"),
    (r"10\.36227/techrxiv", "TechRxiv"),
    (r"10\.33774/", "Cambridge Open Engage"),
    (r"10\.5194/egusphere", "EGU"),
    (r"10\.2172/", "OSTI"),
    (r"10\.31220/agrirxiv", "agriRxiv"),
    (r"10\.32388/", "Qeios"),
    (r"10\.32920/", "Toronto Metropolitan University"),
)

PREPRINT_ONLY_PUBLISHERS: frozenset[str] = frozenset(["authorea, inc.", "openrxiv", "research square", "techrxiv"])

CONFERENCE_AS_JOURNAL: frozenset[str] = frozenset(
    ["canada human-computer communications society", "ijcnlp-aacl", "software engineering"]
)

REPOSITORY_AS_JOURNAL: frozenset[str] = frozenset(
    [
        "california digital library",
        "cern european organization",
        "dspace",
        "escholarship",
        "eyls",
        "figshare",
        "osti",
        "research portal",
        "tu/e research portal",
        "underline science",
        "zenodo",
    ]
)

INSTITUTIONAL_REPOSITORIES: frozenset[str] = frozenset(["deep blue", "mspace", "prism", "tspace", "uwspace"])

JOURNALS_NAMED_PROCEEDINGS: frozenset[str] = frozenset(
    [
        "proceedings of the ieee",
        "proceedings of the national academy of sciences",
        "proceedings of the royal society",
        "proceedings of the vldb endowment",
    ]
)

PROCEEDINGS_SERIES_AS_JOURNAL: frozenset[str] = frozenset(
    [
        "frontiers in artificial intelligence and applications",
        "ifac papersonline",
        "ifac-papersonline",
        "procedia cirp",
        "procedia computer science",
        "procedia engineering",
        "procedia manufacturing",
    ]
)

ACM_JOURNAL_PROCEEDINGS: frozenset[str] = frozenset(
    [
        "pacm hci",
        "pacm on human-computer interaction",
        "pacm on networking",
        "pacm on software engineering",
        "proceedings of the acm on human-computer interaction",
        "proceedings of the acm on interactive, mobile, wearable and ubiquitous technologies",
        "proceedings of the acm on management of data",
        "proceedings of the acm on networking",
        "proceedings of the acm on programming languages",
        "proceedings of the acm on software engineering",
    ]
)

PUBLISHER_CORRECTIONS: dict[str, str] = {
    "computational and structural biotechnology journal": "Elsevier",
    "journal of computational biology": "Mary Ann Liebert",
    "veterinary sciences": "MDPI AG",
}

VENUE_CASE_CORRECTIONS: dict[str, str] = {
    "AI & SOCIETY": "AI & Society",
    "CEUR WORKSHOP PROCEEDINGS": "CEUR Workshop Proceedings",
    "DIGITAL HEALTH": "Digital Health",
    "Genome biology and evolution": "Genome Biology and Evolution",
}

ACRONYM_CASE_CORRECTIONS: dict[str, str] = {"Ai": "AI", "Iot": "IoT", "Nims": "NIMS"}

GENERIC_SERIES_NAMES: frozenset[str] = frozenset(
    [
        "advances in intelligent systems and computing",
        "communications in computer and information science",
        "dagstuhl seminar proceedings",
        "ifip advances in information and communication technology",
        "lecture notes in artificial intelligence",
        "lecture notes in business information processing",
        "lecture notes in computer science",
        "lecture notes in networks and systems",
        "leibniz international proceedings in informatics",
        "lipics: leibniz international proceedings in informatics",
        "oasics: open access series in informatics",
        "smart innovation, systems and technologies",
        "studies in health technology and informatics",
    ]
)

AUTHOR_NAME_SUFFIXES: frozenset[str] = frozenset(["ii", "iii", "iv", "jr", "sr", "v"])
NAME_HONORIFICS: frozenset[str] = frozenset(["dr", "prof", "mr", "mrs", "ms", "sir"])

STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "on",
        "for",
        "of",
        "and",
        "to",
        "in",
        "with",
        "using",
        "via",
        "from",
        "by",
        "at",
        "into",
        "through",
    }
)

CSL_HOSTS: frozenset[str] = frozenset({"data.medra.org", "japanlinkcenter.org", "ra.publications.europa.eu"})

DATA_DOI_PREFIXES: tuple[str, ...] = ("10.6084/m9.figshare", "10.5281/zenodo")
