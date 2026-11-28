import pyexpat
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlsplit

from citeforge.http import HOSTS, Http, ParseError, Result, parsed
from citeforge.ids import norm_orcid, to_id
from citeforge.model import Author, Claim, Kind, Record, Rel, Source, Status
from citeforge.tables import DBLP_KINDS
from citeforge.text import split_literal

# Expat 2.6 bounds entity amplification and token size, which makes the stdlib parser safe on untrusted XML.
if pyexpat.version_info < (2, 6, 0):
    raise ImportError(f"expat {pyexpat.version_info} is too old to parse DBLP XML safely; 2.6.0 or newer is required")

HOST = HOSTS["dblp.org"]
MAX_BODY_BYTES = 50 * 1024 * 1024

_HOMONYM = re.compile(r"\s+\d{4}$")
_NON_LINK_HOSTS = frozenset({"doi.org", "dx.doi.org", "doi.ieeecomputersociety.org", "arxiv.org", "www.wikidata.org"})


@dataclass(frozen=True, slots=True)
class DblpPerson:
    names: tuple[str, ...]
    orcids: tuple[str, ...]
    claims: tuple[Claim, ...]


def person(http: Http, pid: str) -> Result[DblpPerson]:
    return parsed(http.get(HOST, f"/pid/{pid}.xml", follow_redirects=True), parse_person)


def parse_person(body: bytes) -> DblpPerson:
    if len(body) > MAX_BODY_BYTES:
        raise ParseError(f"DBLP body of {len(body)} bytes exceeds {MAX_BODY_BYTES}")
    try:
        root = ET.fromstring(body)  # noqa: S314
    except ET.ParseError as exc:
        raise ParseError(f"DBLP XML: {exc}") from exc
    if root.tag != "dblpperson":
        raise ParseError(f"DBLP root is <{root.tag}>, expected <dblpperson>")
    pid = root.get("pid")
    profile = root.find("person")
    raw_names = [root.get("name", "")]
    orcids: list[str | None] = []
    if profile is not None:
        raw_names += [_text(a) for a in profile.iterfind("author")]
        raw_names += [_text(n) for n in profile.iterfind("note") if n.get("type") == "uname"]
        orcids += [norm_orcid(_text(u)) for u in profile.iterfind("url")]
    entries = [e for r in root.iterfind("r") for e in r]
    orcids += [
        norm_orcid(a.get("orcid", ""))
        for e in entries
        for a in (*e.iterfind("author"), *e.iterfind("editor"))
        if pid and a.get("pid") == pid
    ]
    names = dict.fromkeys(n for n in (_HOMONYM.sub("", raw) for raw in raw_names) if n)
    claims = tuple(c for e in entries if (c := _claim(e)) is not None)
    return DblpPerson(tuple(names), tuple(sorted({o for o in orcids if o})), claims)


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _field(el: ET.Element, tag: str) -> str | None:
    return _text(el.find(tag)) or None


def _person(el: ET.Element) -> Author | None:
    name = _HOMONYM.sub("", _text(el))
    if not name:
        return None
    orcid = norm_orcid(el.get("orcid", ""))
    given, family = split_literal(name)
    if len(family) < 2:
        return Author("", "", literal=name, orcid=orcid)
    return Author(given, family, orcid=orcid)


def _link_ids(link: str) -> list[str | None]:
    host = (urlsplit(link).hostname or "").lower()
    if host in ("doi.org", "dx.doi.org", "doi.ieeecomputersociety.org"):
        return [to_id("doi", urlsplit(link).path.lstrip("/"))]
    if host == "arxiv.org":
        return [to_id("arxiv", link)]
    return []


def _claim(el: ET.Element) -> Claim | None:
    kind = DBLP_KINDS.get(el.tag)
    title = _text(el.find("title")).removesuffix(".").strip()
    if kind is None or not title:
        return None
    publtypes = set(el.get("publtype", "").split())
    authors = [_person(a) for a in el.iterfind("author")]
    editors = [_person(a) for a in el.iterfind("editor")]
    if None in authors or None in editors or not (authors or editors):
        return None
    journal = _field(el, "journal")
    volume = _field(el, "volume")
    links = [_text(e) for e in el.iterfind("ee")]
    found = [to_id("dblp", el.get("key", "")), *(i for link in links for i in _link_ids(link))]
    if journal == "CoRR":
        found.append(to_id("arxiv", (volume or "").removeprefix("abs/")))
        volume = None
    ids = frozenset(i for i in found if i)
    if "software" in publtypes:
        kind = Kind.SOFTWARE
    elif kind is Kind.ARTICLE and ("informal" in publtypes or journal == "CoRR"):
        kind = Kind.PREPRINT
    elif kind is Kind.BOOK and not authors:
        kind = Kind.PROCEEDINGS
    year_text = _field(el, "year")
    year = int(year_text) if year_text and year_text.isdecimal() else None
    container = journal or _field(el, "booktitle")
    relations = {
        (Rel.VERSION_OF, rel_id)
        for rel in el.iterfind("rel")
        if rel.get("type") == "versionOf" and (rel_id := next(iter(_link_ids(rel.get("uri", ""))), None))
    }
    url = next((link for link in links if (urlsplit(link).hostname or "").lower() not in _NON_LINK_HOSTS), None)
    thesis = kind in (Kind.PHDTHESIS, Kind.MASTERSTHESIS)
    record = Record(
        source=Source.DBLP,
        ids=ids,
        kind=kind,
        source_type=" ".join([el.tag, *sorted(publtypes)]),
        title=title,
        authors=tuple(a for a in authors if a),
        editors=tuple(a for a in editors if a),
        year=year,
        container=(container,) if container and kind is not Kind.BOOK else (),
        publisher=_field(el, "publisher"),
        volume=volume,
        number=_field(el, "number"),
        pages=_field(el, "pages"),
        url=url if url and not any(i.startswith("doi:") for i in ids) else None,
        school=_field(el, "school") if thesis else None,
        relations=frozenset(relations),
        status=Status.WITHDRAWN if "withdrawn" in publtypes else Status.OK,
    )
    return Claim(Source.DBLP, record.ids, title, year, record=record)
