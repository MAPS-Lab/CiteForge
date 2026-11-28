import json
from collections.abc import Iterable, Mapping, Sequence
from itertools import batched
from typing import Any
from urllib.parse import quote

from citeforge.http import HOSTS, Batch, Http, ParseError, Result
from citeforge.ids import norm_orcid, norm_ror, split_id, to_id
from citeforge.model import Author, Fetch, Kind, Record, Rel, Source, Status
from citeforge.text import affiliation, clean, fold
from citeforge.works import CROSSREF_KINDS

HOST = HOSTS["api.crossref.org"]
CHUNK = 50
SEARCH_ROWS = 5
_RELATIONS: Mapping[str, Rel] = {
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
_UPDATES: Mapping[str, Rel] = {
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
_STATUS = {Rel.RETRACTS: Status.RETRACTED, Rel.WITHDRAWS: Status.WITHDRAWN}
# issued is Crossref's earliest publication date; approved and posted stand in when a deposit leaves it empty.
_YEAR_FIELDS = (("published-print",), ("journal-issue", "published-print"), ("issued",), ("approved",), ("posted",))
_DATE_FIELDS = (
    *_YEAR_FIELDS,
    ("published-online",),
    ("published",),
    ("journal-issue", "published-online"),
    ("accepted",),
)


def by_doi(http: Http, ids: Sequence[str]) -> Batch:
    found: dict[str, Record] = {}
    missing: set[str] = set()
    failed: set[str] = set()
    wanted = sorted(set(ids))
    singles = [i for i in wanted if "," in split_id(i)[1]]
    for chunk in batched([i for i in wanted if i not in singles], CHUNK, strict=False):
        params = {"filter": ",".join(f"doi:{split_id(i)[1]}" for i in chunk), "rows": str(2 * CHUNK)}
        reply = http.get(HOST, "/works", params)
        if reply.status == 400 and len(chunk) > 1:
            singles.extend(chunk)
            continue
        if reply.fetch is not Fetch.OK:
            failed.update(chunk)
            continue
        try:
            items, complete = _items(reply.body)
        except ParseError:
            failed.update(chunk)
            continue
        got, bad = _match(items, set(chunk))
        found |= got
        unmatched = set(chunk) - got.keys() - bad
        failed |= bad
        (missing if complete else failed).update(unmatched)
    for work_id in singles:
        res = _one(http, work_id)
        if res.value is not None:
            found[work_id] = res.value
        elif res.fetch is Fetch.NOT_FOUND:
            missing.add(work_id)
        else:
            failed.add(work_id)
    return Batch(found=found, missing=frozenset(missing), failed=frozenset(failed))


def search(http: Http, title: str, family: str, year: int | None) -> Result[tuple[Record, ...]]:
    query = clean(title)
    if not query:
        return Result(Fetch.OK, ())
    params = {"query.bibliographic": query, "rows": str(SEARCH_ROWS)}
    if family.strip():
        params["query.author"] = family.strip()
    if year is not None:
        params["filter"] = f"from-pub-date:{year - 1},until-pub-date:{year + 1}"
    reply = http.get(HOST, "/works", params)
    if reply.fetch is not Fetch.OK:
        return Result(reply.fetch, None, reply.reason)
    try:
        items, _ = _items(reply.body)
    except ParseError:
        return Result(Fetch.FAILED, None, "parse_error")
    records = []
    for item in items:
        try:
            records.append(parse_work(item))
        except ParseError:
            continue
    return Result(Fetch.OK, tuple(records))


def parse_work(item: Mapping[str, Any]) -> Record:
    if not isinstance(item, Mapping):
        raise ParseError("work is not an object")
    doi = to_id("doi", _str(item.get("DOI")))
    if doi is None:
        raise ParseError(f"work has no valid DOI: {item.get('DOI')!r}")
    source_type = _str(item.get("type")).strip().lower()
    if not source_type:
        raise ParseError(f"{doi} has no type")
    title = _title(item)
    if not title:
        raise ParseError(f"{doi} has no title")
    relations = {*_relations(item.get("relation"), doi), *_updates(item.get("update-to"), doi)}
    institution = _first_name(item.get("institution"))
    year = next((y for path in _YEAR_FIELDS if (y := _year(_dig(item, path))) is not None), None)
    dates = {y for path in _DATE_FIELDS if (y := _year(_dig(item, path))) is not None}
    return Record(
        source=Source.CROSSREF,
        ids=frozenset({doi}),
        kind=CROSSREF_KINDS.get(source_type, Kind.OTHER),
        source_type=source_type,
        title=title,
        authors=_people(item.get("author"), doi),
        editors=_people(item.get("editor"), doi),
        year=year,
        dates=tuple(sorted(dates)),
        container=tuple(dict.fromkeys(c for c in map(clean, _strs(item.get("container-title"))) if c)),
        event=_first_name([item.get("event")]),
        publisher=clean(_str(item.get("publisher"))) or None,
        volume=_field(item, "volume"),
        number=_field(item, "issue"),
        pages=_field(item, "page"),
        article_number=_field(item, "article-number"),
        language=_field(item, "language"),
        school=institution if source_type == "dissertation" else None,
        institution=institution if source_type == "report" else None,
        degree=next((d for d in map(clean, _strs(item.get("degree"))) if d), None),
        relations=frozenset(relations),
        status=_status(item.get("updated-by")),
    )


def _items(body: bytes) -> tuple[list[Mapping[str, Any]], bool]:
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise ParseError(f"invalid JSON: {e}") from e
    if not isinstance(doc, dict) or doc.get("status") != "ok" or doc.get("message-type") not in ("work-list", "work"):
        raise ParseError("not a Crossref work response")
    message = doc.get("message")
    if doc["message-type"] == "work":
        if not isinstance(message, dict):
            raise ParseError("work message is not an object")
        return [message], True
    if not isinstance(message, dict) or not isinstance(items := message.get("items"), list):
        raise ParseError("work-list has no items")
    total = message.get("total-results")
    return items, not isinstance(total, int) or total <= len(items)


def _match(items: Iterable[Mapping[str, Any]], wanted: set[str]) -> tuple[dict[str, Record], set[str]]:
    got: dict[str, Record] = {}
    bad: set[str] = set()
    for item in items:
        try:
            record = parse_work(item)
        except ParseError:
            doi = to_id("doi", _str(item.get("DOI"))) if isinstance(item, Mapping) else None
            bad |= ({doi} & wanted) if doi else (wanted - got.keys())
            continue
        for work_id in record.ids & wanted:
            got[work_id] = record
    return got, bad - got.keys()


def _one(http: Http, work_id: str) -> Result[Record]:
    reply = http.get(HOST, "/works/" + quote(split_id(work_id)[1], safe="/"))
    if reply.status in (400, 404):
        return Result(Fetch.NOT_FOUND, None, "not_found")
    if reply.fetch is not Fetch.OK:
        return Result(reply.fetch, None, reply.reason)
    try:
        items, _ = _items(reply.body)
        record = parse_work(items[0])
    except ParseError:
        return Result(Fetch.FAILED, None, "parse_error")
    return Result(Fetch.OK, record)


def _title(item: Mapping[str, Any]) -> str:
    title = next((t for t in map(clean, _strs(item.get("title"))) if t), "")
    subtitle = next((t for t in map(clean, _strs(item.get("subtitle"))) if t), "")
    if not title or not subtitle or fold(subtitle) in fold(title):
        return title or subtitle
    return f"{title}: {subtitle}"


def _people(value: object, doi: str) -> tuple[Author, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ParseError(f"{doi} has a malformed byline")
    people = [p for p in (_person(raw, doi) for raw in value) if p is not None]
    firsts = [i for i, (_, seq) in enumerate(people) if seq == "first"]
    if len(firsts) == 1 and firsts[0] > 0:
        people.insert(0, people.pop(firsts[0]))
    return tuple(a for a, _ in people)


def _person(raw: object, doi: str) -> tuple[Author, str] | None:
    if not isinstance(raw, Mapping):
        raise ParseError(f"{doi} has a malformed author")
    given, family, name = (clean(_str(raw.get(k))) for k in ("given", "family", "name"))
    if not family and not (name or given):
        return None
    rors: list[str] = []
    affiliations: list[str] = []
    for aff in raw.get("affiliation") or ():
        if not isinstance(aff, Mapping):
            continue
        if aff_name := affiliation(_str(aff.get("name"))):
            affiliations.append(aff_name)
        for ident in aff.get("id") or ():
            is_ror = isinstance(ident, Mapping) and _str(ident.get("id-type")).upper() == "ROR"
            if is_ror and (ror := norm_ror(_str(ident.get("id")))):
                rors.append(ror)
    author = Author(
        given=given if family else "",
        family=family,
        literal=None if family else (name or given),
        orcid=norm_orcid(_str(raw.get("ORCID"))),
        rors=tuple(dict.fromkeys(rors)),
        affiliations=tuple(dict.fromkeys(affiliations)),
    )
    return author, _str(raw.get("sequence"))


def _relations(value: object, own: str) -> set[tuple[Rel, str]]:
    out: set[tuple[Rel, str]] = set()
    if not isinstance(value, Mapping):
        return out
    for name, targets in value.items():
        rel = _RELATIONS.get(str(name).lower())
        if rel is None or not isinstance(targets, list):
            continue
        for t in targets:
            if isinstance(t, Mapping) and (target := _target(t)) and target != own:
                out.add((rel, target))
    return out


def _target(t: Mapping[str, Any]) -> str | None:
    raw = _str(t.get("id"))
    match _str(t.get("id-type")).lower():
        case "doi":
            return to_id("doi", raw)
        case "arxiv":
            return to_id("arxiv", raw)
        case "uri":
            return to_id("doi", raw) or to_id("arxiv", raw)
        case _:
            return None


def _update_kind(u: object) -> tuple[Rel, str] | None:
    if not isinstance(u, Mapping):
        return None
    rel = _UPDATES.get(_str(u.get("type")).strip().lower().replace("-", "_"))
    target = to_id("doi", _str(u.get("DOI")))
    return (rel, target) if rel and target else None


def _updates(value: object, own: str) -> set[tuple[Rel, str]]:
    pairs = (_update_kind(u) for u in value) if isinstance(value, list) else ()
    return {p for p in pairs if p is not None and p[1] != own}


def _status(value: object) -> Status:
    rels = {p[0] for p in (_update_kind(u) for u in value) if p} if isinstance(value, list) else set()
    for rel in (Rel.RETRACTS, Rel.WITHDRAWS):
        if rel in rels:
            return _STATUS[rel]
    return Status.OK


def _dig(item: Mapping[str, Any], path: tuple[str, ...]) -> object:
    node: object = item
    for key in path:
        node = node.get(key) if isinstance(node, Mapping) else None
    return node


def _year(date: object) -> int | None:
    parts = date.get("date-parts") if isinstance(date, Mapping) else None
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list) or not parts[0]:
        return None
    y = parts[0][0]
    if isinstance(y, str) and y.isdigit():
        y = int(y)
    return y if isinstance(y, int) and not isinstance(y, bool) and 1000 <= y <= 9999 else None


def _first_name(value: object) -> str | None:
    entries = value if isinstance(value, list) else ()
    return next((n for e in entries if isinstance(e, Mapping) and (n := clean(_str(e.get("name"))))), None)


def _field(item: Mapping[str, Any], key: str) -> str | None:
    value = item.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return (clean(value) or None) if isinstance(value, str) else None


def _strs(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""
