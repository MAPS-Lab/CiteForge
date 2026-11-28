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
    IDENTICAL = "identical"
    VARIANT_OF = "variant_of"
    PUBLISHED_AS = "published_as"
    TRANSLATION_OF = "translation_of"
    HAS_TRANSLATION = "has_translation"
    CORRECTS = "corrects"
    RETRACTS = "retracts"
    WITHDRAWS = "withdraws"


VERSION_RELS = frozenset(
    {Rel.HAS_PREPRINT, Rel.PREPRINT_OF, Rel.VERSION_OF, Rel.PUBLISHED_AS, Rel.IDENTICAL, Rel.VARIANT_OF}
)
# A registry that calls two DOIs one object, or one DOI a copy of the other, joins them past the two-DOI guard.
SAME_OBJECT_RELS = frozenset({Rel.IDENTICAL, Rel.VARIANT_OF})


class Fetch(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class Verdict(StrEnum):
    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"


WORK_KEY_ORDER = ("doi", "arxiv", "pmid", "dblp", "openalex", "manual", "orcidwork")


@dataclass(frozen=True, slots=True)
class Member:
    name: str
    dirname: str
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
    alt_titles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Claim:
    source: Source
    ids: frozenset[str]
    title: str
    year: int | None
    record: Record | None = None


@dataclass(frozen=True, slots=True)
class NameKey:
    families: frozenset[str]
    initials: str
    whole: frozenset[str]
    first: str


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


class EventKind(StrEnum):
    AUTHOR_AFFILIATION_DROPPED = "author_affiliation_dropped"
    AUTHOR_REPEAT_DROPPED = "author_repeat_dropped"
    CROSSWALK_REJECTED = "crosswalk_rejected"
    DOI_DEAD = "doi_dead"
    DOI_DETACHED = "doi_detached"
    DOI_REVIEW = "doi_review"
    DOI_UNLISTED = "doi_unlisted"
    FUZZY_LINK = "fuzzy_link"
    GROUP_AUTHOR = "group_author"
    LINK_PROPOSAL = "link_proposal"
    LINK_REFUSED = "link_refused"
    OPENALEX_ID_DELETED = "openalex_id_deleted"
    OPENALEX_ID_MERGED = "openalex_id_merged"
    OPENALEX_ORCID_CONFLICT = "openalex_orcid_conflict"
    ORCID_STATUS = "orcid_status"
    PENDING = "pending"
    SEARCH_FAILED = "search_failed"
    SEARCH_LINKED = "search_linked"
    SEARCH_REVIEW = "search_review"
    SHRINK_GUARD = "shrink_guard"


@dataclass(frozen=True, slots=True)
class Event:
    kind: EventKind
    member: str | None
    id: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class Registries:
    found: Mapping[str, Record] = field(default_factory=dict)
    dead: frozenset[str] = frozenset()
    failed: frozenset[str] = frozenset()
    unlisted: frozenset[str] = frozenset()
    openalex: Mapping[str, Record] = field(default_factory=dict)
