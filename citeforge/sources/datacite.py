import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from citeforge.http import HOSTS, Batch, Http, ParseError
from citeforge.ids import norm_orcid, split_id, to_id
from citeforge.model import Author, Fetch, Kind, Record, Rel, Source
from citeforge.text import is_affiliation, is_group, split_literal
from citeforge.works import DATACITE_KINDS

HOST = HOSTS["api.datacite.org"]
CHUNK = 100
RELATIONS: Mapping[str, Rel] = {
    "isversionof": Rel.VERSION_OF,
    "ispreviousversionof": Rel.VERSION_OF,
    "isnewversionof": Rel.VERSION_OF,
    "isidenticalto": Rel.VERSION_OF,
    "istranslationof": Rel.TRANSLATION_OF,
    "hastranslation": Rel.HAS_TRANSLATION,
}
# Schema 4.3 and older carry the real type only in the free-text resourceType of a Text record.
TEXT_TYPES = (
    (re.compile(r"thes[ie]s|dissertation", re.I), "Dissertation"),
    (re.compile(r"preprint", re.I), "Preprint"),
    (re.compile(r"conference", re.I), "ConferencePaper"),
    (re.compile(r"chapter", re.I), "BookChapter"),
    (re.compile(r"report|working paper", re.I), "Report"),
    (re.compile(r"article|journal", re.I), "JournalArticle"),
)
_PLACEHOLDER = re.compile(r"\(:[a-z]+\)", re.I)
_YEAR = re.compile(r"(\d{4})(?:\D.*)?")
_ROR = re.compile(r"(?:https?://ror\.org/)?(0[a-z0-9]{6}\d{2})", re.I)
_PARENTHESISED = re.compile(r".*?\(([^()]+)\)\s*")
_REPOSITORY = re.compile(r"repositor|archiv|librar|biblio|digital|commons|e-?prints?\b|scholarship|\w+space\b", re.I)


def by_doi(http: Http, ids: Sequence[str]) -> Batch:
    """Look up doi: ids with /dois?ids=a,b, 100 per call, keyed by the same doi: ids."""
    wanted = sorted({i for i in ids if split_id(i)[0] == "doi"})
    found: dict[str, Record] = {}
    missing = {i for i in ids if split_id(i)[0] != "doi"}
    failed: set[str] = set()
    listed = [i for i in wanted if "," not in i]
    chunks = [listed[n : n + CHUNK] for n in range(0, len(listed), CHUNK)]
    chunks += [[i] for i in wanted if "," in i]
    for chunk in chunks:
        values = [split_id(i)[1] for i in chunk]
        single = "," in values[0]
        if single:
            reply = http.get(HOST, f"/dois/{quote(values[0], safe='/')}", {"affiliation": "true"})
        else:
            params = {"ids": ",".join(values), "affiliation": "true", "page[size]": str(CHUNK)}
            reply = http.get(HOST, "/dois", params)
        if reply.fetch is Fetch.NOT_FOUND and single:
            missing.update(chunk)
            continue
        if reply.fetch is not Fetch.OK:
            failed.update(chunk)
            continue
        try:
            records, unreadable = parse_dois(reply.body)
        except ParseError:
            failed.update(chunk)
            continue
        for work_id in chunk:
            if work_id in records:
                found[work_id] = records[work_id]
            elif work_id in unreadable:
                failed.add(work_id)
            else:
                missing.add(work_id)
    return Batch(found=found, missing=frozenset(missing), failed=frozenset(failed))


def parse_dois(body: bytes) -> tuple[dict[str, Record], frozenset[str]]:
    """Read a /dois list or single-DOI body into records keyed by doi: id plus the doi: ids whose item is unusable."""
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise ParseError(f"datacite body is not JSON: {exc}") from exc
    data = doc.get("data") if isinstance(doc, dict) else None
    items = [data] if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ParseError("datacite body has no data")
    records: dict[str, Record] = {}
    unreadable: set[str] = set()
    for item in items:
        attrs = item.get("attributes") if isinstance(item, dict) else None
        if not isinstance(attrs, dict):
            raise ParseError("datacite item has no attributes")
        work_id = to_id("doi", str(attrs.get("doi") or item.get("id") or ""))
        if work_id is None:
            raise ParseError("datacite item has no DOI")
        try:
            records[work_id] = parse_work(work_id, attrs)
        except ParseError:
            unreadable.add(work_id)
    return records, frozenset(unreadable - records.keys())


def parse_work(work_id: str, attrs: Mapping[str, Any]) -> Record:
    """Map the attributes of one DataCite DOI to a Record; raises ParseError."""
    types = _dict(attrs.get("types"))
    source_type = _text(types.get("resourceTypeGeneral")) or "Text"
    resource_type = _text(types.get("resourceType"))
    if source_type == "Text" and resource_type:
        source_type = next((t for pattern, t in TEXT_TYPES if pattern.search(resource_type)), source_type)
    title = _title(attrs.get("titles"))
    if not title:
        raise ParseError(f"{work_id}: no title")
    years = [_year(attrs.get("publicationYear"))] + [_year(d.get("date")) for d in _dicts(attrs.get("dates"))]
    container = _dict(attrs.get("container"))
    first, last = _text(container.get("firstPage")), _text(container.get("lastPage"))
    raw_publisher = attrs.get("publisher")
    publisher = _text(raw_publisher.get("name") if isinstance(raw_publisher, dict) else raw_publisher)
    thesis = source_type == "Dissertation"
    return Record(
        source=Source.DATACITE,
        ids=frozenset({work_id}),
        kind=DATACITE_KINDS.get(source_type.lower(), Kind.OTHER),
        source_type=source_type,
        title=title,
        authors=_people(attrs.get("creators")),
        editors=_people([c for c in _dicts(attrs.get("contributors")) if c.get("contributorType") == "Editor"]),
        year=years[0],
        dates=tuple(sorted({y for y in years if y is not None})),
        container=tuple(t for t in [_text(container.get("title"))] if t),
        publisher=publisher,
        volume=_text(container.get("volume")),
        number=_text(container.get("issue")),
        pages="-".join(p for p in (first, last) if p) or None,
        url=_text(attrs.get("url")),
        language=_text(attrs.get("language")),
        school=_school(publisher) if thesis and publisher else None,
        degree=resource_type if thesis else None,
        relations=frozenset(_relations(work_id, attrs.get("relatedIdentifiers"))),
    )


def _school(publisher: str) -> str | None:
    m = _PARENTHESISED.fullmatch(publisher)
    name = m.group(1).strip() if m else publisher
    return name if is_affiliation(name) and not _REPOSITORY.search(name) else None


def _people(creators: Any) -> tuple[Author, ...]:
    return tuple(a for c in _dicts(creators) if (a := _person(c)) is not None)


def _person(c: Mapping[str, Any]) -> Author | None:
    name, given, family = _text(c.get("name")), _text(c.get("givenName")), _text(c.get("familyName"))
    raw = c.get("affiliation")
    affiliations = [a if isinstance(a, dict) else {"name": a} for a in raw] if isinstance(raw, list) else []
    rors = tuple(dict.fromkeys(r for a in affiliations if (r := _ror(a.get("affiliationIdentifier")))))
    names = tuple(dict.fromkeys(n for a in affiliations if (n := _text(a.get("name")))))
    if not (name or family):
        return None
    if c.get("nameType") == "Organizational" or (not family and name and (is_group(name) or is_affiliation(name))):
        return Author("", "", literal=name or family, rors=rors, affiliations=names)
    if not family:
        given, family = split_literal(name or "")
    if len(family) < 2:
        raise ParseError(f"creator {name!r} has no usable family name")
    orcids = (
        norm_orcid(str(i.get("nameIdentifier") or ""))
        for i in _dicts(c.get("nameIdentifiers"))
        if str(i.get("nameIdentifierScheme") or "").upper() == "ORCID"
    )
    return Author(given or "", family, orcid=next((o for o in orcids if o), None), rors=rors, affiliations=names)


def _relations(work_id: str, related: Any) -> set[tuple[Rel, str]]:
    out = set()
    for r in _dicts(related):
        rel = RELATIONS.get(str(r.get("relationType") or "").lower())
        raw = str(r.get("relatedIdentifier") or "")
        kind = {"DOI": "doi", "ARXIV": "arxiv", "PMID": "pmid"}.get(str(r.get("relatedIdentifierType") or "").upper())
        target = to_id(kind, raw) if kind else to_id("doi", raw) or to_id("arxiv", raw)
        if rel is not None and target is not None and target != work_id:
            out.add((rel, target))
    return out


def _title(titles: Any) -> str | None:
    entries = [(t.get("titleType"), _text(t.get("title"))) for t in _dicts(titles)]
    main = next((v for kind, v in entries if not kind and v), None) or next((v for _, v in entries if v), None)
    subtitle = next((v for kind, v in entries if kind == "Subtitle" and v), None)
    return f"{main}: {subtitle}" if main and subtitle and subtitle.lower() not in main.lower() else main


def _text(value: Any) -> str | None:
    if not isinstance(value, str | int) or isinstance(value, bool):
        return None
    s = " ".join(str(value).split())
    return None if not s or _PLACEHOLDER.fullmatch(s) else s


def _year(value: Any) -> int | None:
    m = _YEAR.fullmatch(_text(value) or "")
    return int(m.group(1)) if m and 1000 <= int(m.group(1)) <= 2999 else None


def _ror(value: Any) -> str | None:
    m = _ROR.fullmatch(_text(value) or "")
    return f"https://ror.org/{m.group(1).lower()}" if m else None


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    return (
        [v for v in value if isinstance(v, dict)]
        if isinstance(value, list | tuple)
        else (
            [v for v in value if isinstance(v, dict)] if value is not None and not isinstance(value, str | dict) else []
        )
    )
