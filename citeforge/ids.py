import re
from collections.abc import Iterable
from urllib.parse import unquote

from citeforge.model import WORK_KEY_ORDER
from citeforge.tables import DATA_DOI_PREFIXES, PREPRINT_SERVERS

_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_DOI_SHAPE = re.compile(r"10\.\d{4,9}/[^\s\x00-\x1f\x7f-\x9f]+")
_TEX_ESCAPE = re.compile(r"\\([_&%#$])")
_ARXIV_PREFIX = re.compile(r"^(?:https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", re.IGNORECASE)
_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?", re.IGNORECASE)
_ARXIV_OLD = re.compile(r"([a-z-]+)(\.[a-z]{2})?/(\d{7})(?:v\d+)?(?:\.pdf)?", re.IGNORECASE)
_ORCID = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")
_ORCID_PREFIX = re.compile(r"^(?:https?://(?:www\.)?orcid\.org/|orcid:\s*)", re.IGNORECASE)
_ROR = re.compile(r"(?:https?://(?:www\.)?ror\.org/)?(0[0-9a-hjkmnp-tv-z]{6}\d{2})/?")
_OPENALEX = re.compile(r"[AW]\d{4,12}")
_OPENALEX_PREFIX = re.compile(r"^(?:https?://(?:api\.)?openalex\.org/(?:authors/|works/)?)", re.IGNORECASE)
_PMID = re.compile(r"[1-9]\d{0,8}")
_DBLP_KEY = re.compile(r"[a-z]+(?:/[A-Za-z0-9._-]+)+")
_MANUAL = re.compile(r"[a-z0-9][a-z0-9._-]*")
_ARXIV_DOI = "10.48550/arxiv."
_PREPRINT_DOI_PREFIXES = tuple(prefix for prefix, _ in PREPRINT_SERVERS)


def norm_doi(s: str) -> str | None:
    d = _DOI_PREFIX.sub("", s.strip()).strip()
    d = _TEX_ESCAPE.sub(r"\1", unquote(d)).lower().removesuffix(".")
    return d if _DOI_SHAPE.fullmatch(d) else None


def norm_arxiv(s: str) -> str | None:
    t = _ARXIV_PREFIX.sub("", s.strip()).strip()
    if m := _ARXIV_NEW.fullmatch(t):
        return m.group(1)
    if m := _ARXIV_OLD.fullmatch(t):
        return f"{m.group(1).lower()}{(m.group(2) or '').upper()}/{m.group(3)}"
    return None


def _orcid_checksum_ok(orcid: str) -> bool:
    total = 0
    for ch in orcid.replace("-", "")[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return orcid[-1] == ("X" if check == 10 else str(check))


def norm_orcid(s: str) -> str | None:
    o = _ORCID_PREFIX.sub("", s.strip()).strip().upper()
    return o if _ORCID.fullmatch(o) and _orcid_checksum_ok(o) else None


def norm_ror(s: str) -> str | None:
    m = _ROR.fullmatch(s.strip().lower())
    return f"https://ror.org/{m.group(1)}" if m else None


def norm_openalex(s: str) -> str | None:
    o = _OPENALEX_PREFIX.sub("", s.strip()).strip().upper()
    return o if _OPENALEX.fullmatch(o) else None


def to_id(kind: str, raw: str) -> str | None:
    match kind:
        case "doi":
            doi = norm_doi(raw)
            if doi and doi.startswith(_ARXIV_DOI):
                return to_id("arxiv", doi.removeprefix(_ARXIV_DOI))
            value = doi
        case "arxiv":
            value = norm_arxiv(raw)
        case "pmid":
            value = raw.strip() if _PMID.fullmatch(raw.strip()) else None
        case "dblp":
            value = raw.strip() if _DBLP_KEY.fullmatch(raw.strip()) else None
        case "openalex":
            value = norm_openalex(raw)
        case "manual":
            value = raw.strip().lower() if _MANUAL.fullmatch(raw.strip().lower()) else None
        case _:
            raise ValueError(f"unknown id kind {kind!r}")
    return f"{kind}:{value}" if value else None


def split_id(work_id: str) -> tuple[str, str]:
    kind, _, value = work_id.partition(":")
    return kind, value


def is_preprint_doi(doi: str) -> bool:
    return doi.lower().startswith(_PREPRINT_DOI_PREFIXES)


def work_key(ids: Iterable[str]) -> str:
    def rank(work_id: str) -> tuple[int, bool, str]:
        kind, value = split_id(work_id)
        return WORK_KEY_ORDER.index(kind), kind == "doi" and is_secondary_doi(value), value

    return min(ids, key=rank)


def is_secondary_doi(doi: str) -> bool:
    return doi.lower().startswith(_PREPRINT_DOI_PREFIXES + DATA_DOI_PREFIXES)


def is_copy_id(work_id: str) -> bool:
    """True for an id that names a preprint or repository copy, never a version of record."""
    kind, value = split_id(work_id)
    return kind == "arxiv" or (kind == "doi" and is_secondary_doi(value))
