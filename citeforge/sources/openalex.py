import json
import re
from collections.abc import Iterable, Mapping, Sequence
from itertools import batched
from typing import Any

from citeforge.http import HOSTS, Batch, Http, ParseError, Reply, Result, parsed
from citeforge.ids import is_preprint_doi, norm_openalex, norm_orcid, norm_ror, split_id, to_id
from citeforge.model import Author, Claim, Fetch, Kind, Record, Source, Status
from citeforge.tables import OPENALEX_KINDS
from citeforge.text import affiliation, clean, is_affiliation, is_group, split_literal

HOST = HOSTS["api.openalex.org"]
ORCIDS_PER_CALL = 50
IDS_PER_CALL = 100
PER_PAGE = "200"
WORK_FIELDS = (
    "id,doi,ids,title,publication_year,type,authorships,is_authors_truncated,primary_location,locations,biblio,"
    "language,is_retracted"
)
_PMID = re.compile(r"(\d+)/?$")


def _fail[T](reply: Reply) -> Result[T]:
    return Result(reply.fetch, None, reply.reason)


def _object(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ParseError(f"{what} is not an object")
    return value


def _list(value: object, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ParseError(f"{what} is not a list")
    return value


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ParseError(f"expected text, got {type(value).__name__}")
    return " ".join(str(value).split()) or None


def _load(body: bytes) -> Mapping[str, Any]:
    try:
        return _object(json.loads(body), "response")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"invalid JSON: {exc}") from exc


def parse_page(body: bytes) -> tuple[list[Mapping[str, Any]], str | None]:
    data = _load(body)
    if "results" not in data:
        raise ParseError("no results in a list response")
    items = [_object(i, "result") for i in _list(data["results"], "results")]
    cursor = _text(_object(data.get("meta") or {}, "meta").get("next_cursor"))
    return items, cursor


def _author_ids(item: Mapping[str, Any]) -> tuple[str, str | None]:
    aid = norm_openalex(_text(item.get("id")) or "")
    if aid is None or not aid.startswith("A"):
        raise ParseError("author has no OpenAlex id")
    return aid, norm_orcid(_text(item.get("orcid")) or "")


def parse_author(body: bytes) -> tuple[str, str | None]:
    return _author_ids(_load(body))


def _ids(item: Mapping[str, Any]) -> frozenset[str]:
    raw = _object(item.get("ids") or {}, "ids")
    work = to_id("openalex", _text(item.get("id")) or "")
    if work is None or not work.startswith("openalex:W"):
        raise ParseError("work has no OpenAlex id")
    ids = {work}
    if doi := to_id("doi", _text(item.get("doi")) or _text(raw.get("doi")) or ""):
        ids.add(doi)
    if (pmid := _PMID.search(_text(raw.get("pmid")) or "")) and (pmid_id := to_id("pmid", pmid.group(1))):
        ids.add(pmid_id)
    registered = any(split_id(i)[0] == "doi" for i in ids)
    arxiv = to_id("arxiv", _text(raw.get("arxiv")) or "")
    # an arXiv id OpenAlex attaches to a DOI work is its own crosswalk, kept only for a work with no DOI
    if arxiv and not registered:
        ids.add(arxiv)
    return frozenset(ids)


def _author(authorship: Mapping[str, Any]) -> Author:
    who = _object(authorship.get("author") or {}, "author")
    name = clean(_text(who.get("display_name")) or _text(authorship.get("raw_author_name")) or "")
    institutions = [_object(i, "institution") for i in _list(authorship.get("institutions"), "institutions")]
    rors = tuple(dict.fromkeys(r for i in institutions if (r := norm_ror(_text(i.get("ror")) or ""))))
    raw_affiliations = [_text(a) for a in _list(authorship.get("raw_affiliation_strings"), "affiliations")]
    names = [_text(i.get("display_name")) for i in institutions]
    affiliations = tuple(dict.fromkeys(f for a in (*raw_affiliations, *names) if a and (f := affiliation(a))))
    # the author entity's ORCID belongs to OpenAlex's cluster, not to this byline, so only its id is kept
    ids: dict[str, Any] = {
        "openalex": norm_openalex(_text(who.get("id")) or ""),
        "rors": rors,
        "affiliations": affiliations,
    }
    if not name:
        raise ParseError("author has no name")
    if is_group(name) or is_affiliation(name):
        return Author("", "", literal=name, **ids)
    given, family = split_literal(name)
    if len(family.strip(".")) < 2:
        return Author("", "", literal=name, **ids)
    return Author(given, family, **ids)


def _kind(source_type: str, ids: frozenset[str], venue_type: str | None) -> Kind:
    kinds = {split_id(i)[0] for i in ids}
    dois = [split_id(i)[1] for i in ids if split_id(i)[0] == "doi"]
    if "arxiv" in kinds or any(is_preprint_doi(d) for d in dois):
        return Kind.PREPRINT
    kind = OPENALEX_KINDS.get(source_type, Kind.OTHER)
    if kind is Kind.ARTICLE and venue_type == "conference":
        return Kind.INPROCEEDINGS
    return kind


def parse_work(item: Mapping[str, Any]) -> Record:
    ids = _ids(item)
    title = clean(_text(item.get("title")) or "")
    if not title:
        raise ParseError("work has no title")
    year = item.get("publication_year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise ParseError("publication_year is not an integer")
    location = _object(item.get("primary_location") or {}, "primary_location")
    venue = _object(location.get("source") or {}, "source")
    biblio = _object(item.get("biblio") or {}, "biblio")
    first, last = _text(biblio.get("first_page")), _text(biblio.get("last_page"))
    source_type = (_text(item.get("type")) or "").lower()
    kind = _kind(source_type, ids, _text(venue.get("type")))
    container = _text(venue.get("display_name"))
    authorships = [_object(a, "authorship") for a in _list(item.get("authorships"), "authorships")]
    return Record(
        source=Source.OPENALEX,
        ids=ids,
        kind=kind,
        source_type=source_type,
        title=title,
        authors=tuple(_author(a) for a in authorships),
        year=year,
        container=(clean(container),) if container else (),
        publisher=_text(venue.get("host_organization_name")),
        volume=_text(biblio.get("volume")),
        number=_text(biblio.get("issue")),
        pages=f"{first}-{last}" if first and last and first != last else first,
        url=_text(location.get("landing_page_url")),
        language=_text(item.get("language")),
        howpublished=clean(container) if kind is Kind.PREPRINT and container else None,
        status=Status.RETRACTED if item.get("is_retracted") is True else Status.OK,
    )


def _pages(http: Http, path: str, params: Mapping[str, str]) -> Result[list[Mapping[str, Any]]]:
    items: list[Mapping[str, Any]] = []
    cursor: str | None = "*"
    seen: set[str] = set()
    while cursor is not None:
        if cursor in seen:
            return Result(Fetch.FAILED, None, "parse_error")
        seen.add(cursor)
        reply = http.get(HOST, path, {**params, "per_page": PER_PAGE, "cursor": cursor})
        if reply.fetch is not Fetch.OK:
            return _fail(reply)
        try:
            page, cursor = parse_page(reply.body)
        except ParseError:
            return Result(Fetch.FAILED, None, "parse_error")
        if not page:
            break
        items += page
    return Result(Fetch.OK, items)


def _or(values: Iterable[str]) -> str:
    return "|".join(f'"{v}"' if re.search(r'[,|"]', v) else v for v in values)


def authors_by_orcid(http: Http, orcids: Sequence[str]) -> Result[dict[str, tuple[str, ...]]]:
    """Map each ORCID to the OpenAlex author ids that carry it, 50 ORCIDs per call."""
    wanted = sorted(set(orcids))
    found: dict[str, set[str]] = {}
    for chunk in batched(wanted, ORCIDS_PER_CALL, strict=False):
        res = _pages(http, "/authors", {"filter": "orcid:" + _or(chunk), "select": "id,orcid"})
        if res.value is None:
            return Result(res.fetch, None, res.reason)
        try:
            pairs = [_author_ids(item) for item in res.value]
        except ParseError:
            return Result(Fetch.FAILED, None, "parse_error")
        for aid, orcid in pairs:
            if orcid in chunk:
                found.setdefault(orcid, set()).add(aid)
    return Result(Fetch.OK, {o: tuple(sorted(a)) for o, a in sorted(found.items())})


def author(http: Http, aid: str) -> Result[tuple[str, str | None]]:
    """Return (current author id after any merge, ORCID or None) for one author id."""
    return parsed(http.get(HOST, f"/authors/{aid}", {"select": "id,orcid"}), parse_author)


def _claim(item: Mapping[str, Any]) -> Claim | None:
    try:
        record = parse_work(item)
    except ParseError:
        try:
            ids, title = _ids(item), clean(_text(item.get("title")) or "")
        except ParseError:
            return None
        year = item.get("publication_year")
        return Claim(Source.OPENALEX, ids, title, year if type(year) is int else None) if title else None
    return Claim(Source.OPENALEX, record.ids, record.title, record.year, record=record)


def _lists_author(item: Mapping[str, Any], author_ids: frozenset[str]) -> bool:
    authorships = item.get("authorships")
    for a in authorships if isinstance(authorships, list) else ():
        who = a.get("author") if isinstance(a, dict) else None
        if isinstance(who, dict) and isinstance(who.get("id"), str) and norm_openalex(who["id"]) in author_ids:
            return True
    return False


def _full_byline(http: Http, item: Mapping[str, Any], author_ids: frozenset[str]) -> Result[Mapping[str, Any]]:
    """List results stop at 100 authorships; refetch the single work when the listed author fell past that cut."""
    wid = norm_openalex(item["id"]) if isinstance(item.get("id"), str) else None
    if item.get("is_authors_truncated") is not True or wid is None or _lists_author(item, author_ids):
        return Result(Fetch.OK, item)
    reply = http.get(HOST, f"/works/{wid}", {"select": WORK_FIELDS})
    if reply.fetch is not Fetch.OK:
        return Result(Fetch.FAILED, None, reply.reason)
    try:
        return Result(Fetch.OK, _load(reply.body))
    except ParseError:
        return Result(Fetch.FAILED, None, "parse_error")


def list_works(http: Http, author_ids: Sequence[str], since_year: int) -> Result[tuple[Claim, ...]]:
    """Cursor-page every work of the authors from since_year; each Claim carries its Record."""
    claims: dict[str, Claim] = {}
    for chunk in batched(sorted(set(author_ids)), IDS_PER_CALL, strict=False):
        flt = f"authorships.author.id:{_or(chunk)},from_publication_date:{since_year}-01-01"
        res = _pages(http, "/works", {"filter": flt, "select": WORK_FIELDS})
        if res.value is None:
            return Result(res.fetch, None, res.reason)
        for listed in res.value:
            full = _full_byline(http, listed, frozenset(chunk))
            if full.value is None:
                return Result(full.fetch, None, full.reason)
            if (claim := _claim(full.value)) is not None:
                work = next(i for i in claim.ids if split_id(i)[0] == "openalex")
                claims.setdefault(work, claim)
    return Result(Fetch.OK, tuple(claims[k] for k in sorted(claims)))


def works_by_doi(http: Http, ids: Sequence[str]) -> Batch:
    """Look up doi: ids, 100 per call, keyed by the same doi: ids."""
    wanted = sorted({i for i in ids if split_id(i)[0] == "doi"})
    found: dict[str, Record] = {}
    failed: set[str] = set()
    for chunk in batched(wanted, IDS_PER_CALL, strict=False):
        res = _pages(http, "/works", {"filter": "doi:" + _or(split_id(i)[1] for i in chunk), "select": WORK_FIELDS})
        if res.value is None:
            failed |= set(chunk)
            continue
        good: dict[str, list[Record]] = {}
        bad: set[str] = set()
        for item in res.value:
            doi = to_id("doi", item["doi"]) if isinstance(item.get("doi"), str) else None
            if doi not in chunk:
                continue
            try:
                record = parse_work(item)
            except ParseError:
                bad.add(doi)
                continue
            good.setdefault(doi, []).append(record)
        found |= {doi: min(records, key=lambda r: sorted(r.ids)) for doi, records in good.items()}
        failed |= bad - good.keys()
    return Batch(found, frozenset(wanted) - found.keys() - failed, frozenset(failed))
