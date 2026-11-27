from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class Source(StrEnum):
    OPENALEX = "openalex"
    ORCID = "orcid"
    DBLP = "dblp"
    CROSSREF = "crossref"
    DATACITE = "datacite"
    CSL = "csl"
    ARXIV = "arxiv"
    CURATED = "curated"


REGISTRIES = frozenset({Source.CROSSREF, Source.DATACITE, Source.CSL})


class Kind(StrEnum):
    ARTICLE = "article"
    INPROCEEDINGS = "inproceedings"
    INCOLLECTION = "incollection"
    BOOK = "book"
    PHDTHESIS = "phdthesis"
    MASTERSTHESIS = "mastersthesis"
    TECHREPORT = "techreport"
    PREPRINT = "preprint"
    PROCEEDINGS = "proceedings"
    DATASET = "dataset"
    SOFTWARE = "software"
    PATENT = "patent"
    EDITORIAL = "editorial"
    FRONT_MATTER = "front_matter"
    ABSTRACT = "abstract"
    NOTICE = "notice"
    PEER_REVIEW = "peer_review"
    COMPONENT = "component"
    OTHER = "other"


class Status(StrEnum):
    OK = "ok"
    RETRACTED = "retracted"
    WITHDRAWN = "withdrawn"


class Rel(StrEnum):
    HAS_PREPRINT = "has_preprint"
    PREPRINT_OF = "preprint_of"
    VERSION_OF = "version_of"
    PUBLISHED_AS = "published_as"
    TRANSLATION_OF = "translation_of"
    HAS_TRANSLATION = "has_translation"
    CORRECTS = "corrects"
    RETRACTS = "retracts"
    WITHDRAWS = "withdraws"


VERSION_RELS = frozenset({Rel.HAS_PREPRINT, Rel.PREPRINT_OF, Rel.VERSION_OF, Rel.PUBLISHED_AS})


class Fetch(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class Verdict(StrEnum):
    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"


WORK_KEY_ORDER = ("doi", "arxiv", "pmid", "dblp", "openalex", "manual")


@dataclass(frozen=True, slots=True)
class Member:
    row: int
    name: str
    dirname: str
    scholar: str | None
    dblp: str | None
    orcids: tuple[str, ...]
    openalex: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Author:
    given: str
    family: str
    literal: str | None = None
    orcid: str | None = None
    openalex: str | None = None
    rors: tuple[str, ...] = ()
    affiliations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Record:
    source: Source
    ids: frozenset[str]
    kind: Kind
    source_type: str
    title: str
    authors: tuple[Author, ...] = ()
    editors: tuple[Author, ...] = ()
    year: int | None = None
    dates: tuple[int, ...] = ()
    container: tuple[str, ...] = ()
    event: str | None = None
    publisher: str | None = None
    volume: str | None = None
    number: str | None = None
    pages: str | None = None
    article_number: str | None = None
    url: str | None = None
    language: str | None = None
    school: str | None = None
    institution: str | None = None
    howpublished: str | None = None
    primary_class: str | None = None
    degree: str | None = None
    relations: frozenset[tuple[Rel, str]] = frozenset()
    status: Status = Status.OK


@dataclass(frozen=True, slots=True)
class Claim:
    source: Source
    ids: frozenset[str]
    title: str
    year: int | None
    by_self: bool | None = None
    record: Record | None = None


@dataclass(frozen=True, slots=True)
class NameKey:
    families: frozenset[str]
    initials: str


@dataclass(frozen=True, slots=True)
class Identity:
    member: Member
    orcids: frozenset[str]
    openalex: frozenset[str]
    dblp: str | None
    names: tuple[NameKey, ...]
    rors: frozenset[str]
    inst_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class Listing:
    member: str
    claims: tuple[Claim, ...]
    fetch: Mapping[Source, Fetch]


@dataclass(frozen=True, slots=True)
class Group:
    key: str
    ids: frozenset[str]
    records: tuple[Record, ...]
    claims: tuple[tuple[str, Claim], ...]
    fuzzy: bool = False


@dataclass(frozen=True, slots=True)
class Work:
    key: str
    primary: Record
    eprint: Record | None
    linked: frozenset[str]
    kind: Kind
    status: Status
    year: int | None
    excluded: str | None
    provenance: tuple[tuple[str, Source], ...] = ()
    aliases: frozenset[str] = frozenset()
    sources: frozenset[Source] = frozenset()


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    rule: str
    evidence: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Names:
    stem: str
    citekey: str


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    member: str | None
    id: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class Registries:
    found: Mapping[str, Record] = field(default_factory=dict)
    dead: frozenset[str] = frozenset()
    failed: frozenset[str] = frozenset()
    openalex: Mapping[str, Record] = field(default_factory=dict)
