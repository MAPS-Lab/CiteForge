import re
from collections.abc import Mapping

from citeforge.model import Kind, Rel

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

ENTRY_TYPES: Mapping[Kind, str] = {
    Kind.ARTICLE: "article",
    Kind.INPROCEEDINGS: "inproceedings",
    Kind.INCOLLECTION: "incollection",
    Kind.BOOK: "book",
    Kind.PHDTHESIS: "phdthesis",
    Kind.MASTERSTHESIS: "mastersthesis",
    Kind.TECHREPORT: "techreport",
}

BIBTEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
}

DBLP_KINDS = {
    "article": Kind.ARTICLE,
    "inproceedings": Kind.INPROCEEDINGS,
    "incollection": Kind.INCOLLECTION,
    "book": Kind.BOOK,
    "proceedings": Kind.PROCEEDINGS,
    "phdthesis": Kind.PHDTHESIS,
    "mastersthesis": Kind.MASTERSTHESIS,
    "data": Kind.DATASET,
}

OPENALEX_KINDS: Mapping[str, Kind] = {
    "article": Kind.ARTICLE,
    "review": Kind.ARTICLE,
    "letter": Kind.ARTICLE,
    "software-paper": Kind.ARTICLE,
    "conference-paper": Kind.INPROCEEDINGS,
    "book-chapter": Kind.INCOLLECTION,
    "book": Kind.BOOK,
    "dissertation": Kind.PHDTHESIS,
    "report": Kind.TECHREPORT,
    "preprint": Kind.PREPRINT,
    "dataset": Kind.DATASET,
    "editorial": Kind.EDITORIAL,
    "paratext": Kind.FRONT_MATTER,
    "erratum": Kind.NOTICE,
    "retraction": Kind.NOTICE,
    "peer-review": Kind.PEER_REVIEW,
    "supplementary-materials": Kind.COMPONENT,
}

CSL_KINDS: Mapping[str, Kind] = {
    "article-journal": Kind.ARTICLE,
    "article": Kind.ARTICLE,
    "paper-conference": Kind.INPROCEEDINGS,
    "chapter": Kind.INCOLLECTION,
    "book": Kind.BOOK,
    "report": Kind.TECHREPORT,
    "thesis": Kind.PHDTHESIS,
    "posted-content": Kind.PREPRINT,
    "dataset": Kind.DATASET,
    "software": Kind.SOFTWARE,
    "patent": Kind.PATENT,
    "review": Kind.PEER_REVIEW,
}

DATACITE_KINDS: Mapping[str, Kind] = {
    "journalarticle": Kind.ARTICLE,
    "conferencepaper": Kind.INPROCEEDINGS,
    "bookchapter": Kind.INCOLLECTION,
    "book": Kind.BOOK,
    "conferenceproceeding": Kind.PROCEEDINGS,
    "report": Kind.TECHREPORT,
    "dissertation": Kind.PHDTHESIS,
    "preprint": Kind.PREPRINT,
    "dataset": Kind.DATASET,
    "software": Kind.SOFTWARE,
    "component": Kind.COMPONENT,
    "peerreview": Kind.PEER_REVIEW,
}

DATACITE_RELATIONS: Mapping[str, Rel] = {
    "isversionof": Rel.VERSION_OF,
    "ispreviousversionof": Rel.VERSION_OF,
    "isnewversionof": Rel.VERSION_OF,
    "isidenticalto": Rel.IDENTICAL,
    "isvariantformof": Rel.VARIANT_OF,
    "isoriginalformof": Rel.IDENTICAL,
    "istranslationof": Rel.TRANSLATION_OF,
    "hastranslation": Rel.HAS_TRANSLATION,
}

# Schema 4.3 and older carry the real type only in the free-text resourceType of a Text record.
DATACITE_TEXT_TYPES = (
    (re.compile(r"thes[ie]s|dissertation", re.I), "Dissertation"),
    (re.compile(r"preprint", re.I), "Preprint"),
    (re.compile(r"conference", re.I), "ConferencePaper"),
    (re.compile(r"chapter", re.I), "BookChapter"),
    (re.compile(r"report|working paper", re.I), "Report"),
    (re.compile(r"article|journal", re.I), "JournalArticle"),
)

CROSSREF_KINDS: Mapping[str, Kind] = {
    "journal-article": Kind.ARTICLE,
    "proceedings-article": Kind.INPROCEEDINGS,
    "book-chapter": Kind.INCOLLECTION,
    "book-part": Kind.INCOLLECTION,
    "book-section": Kind.INCOLLECTION,
    "book": Kind.BOOK,
    "monograph": Kind.BOOK,
    "reference-book": Kind.BOOK,
    "edited-book": Kind.PROCEEDINGS,
    "proceedings": Kind.PROCEEDINGS,
    "journal-issue": Kind.PROCEEDINGS,
    "journal": Kind.PROCEEDINGS,
    "report": Kind.TECHREPORT,
    "dissertation": Kind.PHDTHESIS,
    "posted-content": Kind.PREPRINT,
    "dataset": Kind.DATASET,
    "component": Kind.COMPONENT,
    "peer-review": Kind.PEER_REVIEW,
    "editorial": Kind.EDITORIAL,
}

CROSSREF_RELATIONS: Mapping[str, Rel] = {
    "has-preprint": Rel.HAS_PREPRINT,
    "is-preprint-of": Rel.PREPRINT_OF,
    "is-version-of": Rel.VERSION_OF,
    "has-version": Rel.VERSION_OF,
    "is-identical-to": Rel.IDENTICAL,
    "is-same-as": Rel.IDENTICAL,
    "is-translation-of": Rel.TRANSLATION_OF,
    "has-translation": Rel.HAS_TRANSLATION,
}

# Crossmark update types as deposited, including the misspellings the registry holds.
CROSSREF_UPDATES: Mapping[str, Rel] = {
    "retraction": Rel.RETRACTS,
    "retration": Rel.RETRACTS,
    "retracion": Rel.RETRACTS,
    "withdrawal": Rel.WITHDRAWS,
    "removal": Rel.WITHDRAWS,
    "correction": Rel.CORRECTS,
    "erratum": Rel.CORRECTS,
    "err": Rel.CORRECTS,
    "corrigendum": Rel.CORRECTS,
    "addendum": Rel.CORRECTS,
    "clarification": Rel.CORRECTS,
    "expression_of_concern": Rel.CORRECTS,
    "partial_retraction": Rel.CORRECTS,
    "new_version": Rel.VERSION_OF,
}
