import dataclasses
import json
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

import httpx

from citeforge.http import HOSTS, Http, ParseError, Result
from citeforge.ids import norm_doi, norm_orcid, split_id, to_id
from citeforge.model import Author, Fetch, Kind, Record, Source
from citeforge.text import clean

HOST = HOSTS["doi.org"]
RA_BATCH = 20
MAX_HOPS = 3
CSL_ACCEPT = {"Accept": "application/vnd.citationstyles.csl+json"}
_NO_DOI = frozenset({"doi does not exist", "invalid doi"})
_YEAR = re.compile(r"\b(\d{4})\b")


def _path(doi: str) -> str:
    return quote(doi, safe="/")


def parse_agencies(body: bytes) -> dict[str, str | None]:
    try:
        rows = json.loads(body)
    except ValueError as e:
        raise ParseError(f"ra: {e}") from e
    if not isinstance(rows, list):
        raise ParseError("ra: expected a list")
    out: dict[str, str | None] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("DOI"), str):
            raise ParseError("ra: row without DOI")
        doi = norm_doi(row["DOI"])
        if doi is None:
            continue
        ra, status = row.get("RA"), row.get("status")
        if isinstance(ra, str) and ra.strip():
            out[f"doi:{doi}"] = ra.strip()
        elif isinstance(status, str) and status.strip().lower() in _NO_DOI:
            out[f"doi:{doi}"] = None
    return out


def agency(http: Http, ids: Sequence[str]) -> Result[dict[str, str | None]]:
    """Map doi: ids to their registration agency name, 20 per call; None means the DOI does not exist.

    An id whose chunk failed, or that the reply leaves out, is absent from the mapping.
    """
    wanted = sorted({i for i in ids if split_id(i)[0] == "doi"})
    found: dict[str, str | None] = {}
    reason = "ok"
    for start in range(0, len(wanted), RA_BATCH):
        chunk = wanted[start : start + RA_BATCH]
        reply = http.get(HOST, "/ra/" + ",".join(_path(split_id(i)[1]) for i in chunk))
        if reply.fetch is not Fetch.OK:
            reason = reply.reason
            continue
        try:
            parsed = parse_agencies(reply.body)
        except ParseError:
            reason = "parse_error"
            continue
        found |= {i: parsed[i] for i in chunk if i in parsed}
    if reason == "ok":
        return Result(Fetch.OK, found)
    return Result(Fetch.FAILED, found or None, reason)


def _text(value: Any) -> str | None:
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str) and v.strip()), None)
    if isinstance(value, int | float) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    return clean(value) or None


def _year(date: Any) -> int | None:
    if not isinstance(date, dict):
        return None
    parts = date.get("date-parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
        first = parts[0][0]
        if isinstance(first, int) and not isinstance(first, bool):
            return first if 1000 <= first <= 9999 else None
        if isinstance(first, str) and first.strip().isdigit():
            return _year({"date-parts": [[int(first)]]})
    for key in ("raw", "literal"):
        if isinstance(date.get(key), str) and (m := _YEAR.search(date[key])):
            return int(m.group(1))
    return None


def _people(value: Any) -> tuple[Author, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ParseError("csl: name list is not a list")
    people = []
    for p in value:
        if not isinstance(p, dict):
            raise ParseError("csl: name is not an object")
        literal = _text(p.get("literal"))
        if literal is not None:
            people.append(Author("", "", literal=literal))
            continue
        family = " ".join(filter(None, (_text(p.get("non-dropping-particle")), _text(p.get("family")))))
        given = " ".join(filter(None, (_text(p.get("given")), _text(p.get("dropping-particle")))))
        if not family:
            raise ParseError("csl: name has no family")
        orcid = norm_orcid(p["ORCID"]) if isinstance(p.get("ORCID"), str) else None
        people.append(Author(given, family, orcid=orcid))
    return tuple(people)


def parse_csl(body: bytes) -> Record:
    try:
        item = json.loads(body)
    except ValueError as e:
        raise ParseError(f"csl: {e}") from e
    if not isinstance(item, dict):
        raise ParseError("csl: expected an object")
    title = _text(item.get("title"))
    if title is None:
        raise ParseError("csl: no title")
    source_type = item.get("type") if isinstance(item.get("type"), str) else ""
    doi = to_id("doi", item["DOI"]) if isinstance(item.get("DOI"), str) else None
    years = [_year(item.get(k)) for k in ("issued", "published-print", "published-online")]
    thesis = source_type == "thesis"
    publisher = _text(item.get("publisher"))
    return Record(
        source=Source.CSL,
        ids=frozenset({doi} if doi else ()),
        kind=Kind.OTHER,
        source_type=source_type,
        title=title,
        authors=_people(item.get("author")),
        editors=_people(item.get("editor")),
        year=years[0],
        dates=tuple(sorted({y for y in years if y is not None})),
        container=tuple(filter(None, (_text(item.get("container-title")), _text(item.get("collection-title"))))),
        event=_text(item.get("event-title")) or _text(item.get("event")),
        publisher=None if thesis else publisher,
        volume=_text(item.get("volume")),
        number=_text(item.get("issue")),
        pages=_text(item.get("page")),
        article_number=_text(item.get("article-number")),
        url=_text(item.get("URL")),
        language=_text(item.get("language")),
        school=publisher if thesis else None,
        degree=_text(item.get("genre")) if thesis else None,
    )


def csl(http: Http, work_id: str) -> Result[Record]:
    """Fetch CSL JSON for a doi: id, following at most 3 redirects by hand through Http.get(url=...).

    Only a 404 from doi.org itself means the DOI does not exist; a 404 further along the chain is FAILED.
    """
    start = url = httpx.URL(HOST.base + "/" + _path(split_id(work_id)[1]))
    for _ in range(MAX_HOPS + 1):
        reply = http.get(HOST, "", headers=CSL_ACCEPT, url=str(url))
        if reply.reason != "redirect":
            break
        if not reply.location:
            return Result(Fetch.FAILED, None, "parse_error")
        url = url.join(reply.location)
    else:
        return Result(Fetch.FAILED, None, "redirect_limit")
    if reply.fetch is Fetch.NOT_FOUND:
        return Result(Fetch.NOT_FOUND if url == start else Fetch.FAILED, None, "not_found")
    if reply.fetch is not Fetch.OK:
        return Result(Fetch.FAILED, None, reply.reason)
    try:
        rec = parse_csl(reply.body)
    except ParseError:
        return Result(Fetch.FAILED, None, "parse_error")
    return Result(Fetch.OK, dataclasses.replace(rec, ids=rec.ids | {work_id}))
