import pyexpat
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from itertools import batched

from citeforge.http import HOSTS, Batch, Http, ParseError
from citeforge.ids import norm_arxiv, split_id, to_id
from citeforge.model import Author, Fetch, Kind, Record, Rel, Source, Status
from citeforge.text import affiliation, clean, is_affiliation, is_group, split_literal

if pyexpat.version_info < (2, 6, 0):
    raise ImportError(f"expat {pyexpat.version_info} is too old to parse arXiv XML safely; 2.6.0 or newer is required")

HOST = HOSTS["export.arxiv.org"]
CHUNK = 100
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "x": "http://arxiv.org/schemas/atom",
    "o": "http://a9.com/-/spec/opensearch/1.1/",
}
_ABS = re.compile(r"^https?://arxiv\.org/abs/(.+?)(?:v\d+)?$")
_OLD_CLASS = re.compile(r"^([a-z-]+)\.[A-Z]{2}/")
_YEAR = re.compile(r"^(\d{4})-\d{2}-\d{2}T")
_WITHDRAWN = re.compile(r"\bwithdrawn\b", re.IGNORECASE)
_REJECTED = re.compile(r"/api/errors#incorrect_id_format_for_(\S+)$")


class RejectedIdError(ParseError):
    """The API refused one id of the batch and answered for none of the others."""

    def __init__(self, form: str) -> None:
        super().__init__(f"arxiv: incorrect id format for {form}")
        self.form = form


def query_form(arxiv_id: str) -> str:
    """The id the API indexes, with an old-style subject class dropped: math.DG/0303109 is math/0303109."""
    return _OLD_CLASS.sub(r"\1/", split_id(arxiv_id)[1])


def _text(node: ET.Element, path: str) -> str:
    return clean(node.findtext(path, "", NS))


def _year(stamp: str) -> int | None:
    m = _YEAR.match(stamp)
    return int(m.group(1)) if m else None


def _author(node: ET.Element) -> Author:
    name = _text(node, "a:name")
    affiliations = tuple(a for e in node.findall("x:affiliation", NS) if (a := affiliation(e.text or "")))
    if not name:
        raise ParseError("arxiv: author has no name")
    if is_group(name) or is_affiliation(name):
        return Author("", "", literal=name, affiliations=affiliations)
    given, family = split_literal(name)
    if len(family.rstrip(".")) < 2:
        return Author("", "", literal=name, affiliations=affiliations)
    return Author(given, family, affiliations=affiliations)


def _form(node: ET.Element) -> str | None:
    m = _ABS.match(node.findtext("a:id", "", NS).strip())
    return norm_arxiv(m.group(1)) if m else None


def parse_entry(node: ET.Element, ids: frozenset[str]) -> Record:
    """One Atom entry as a Record carrying ids, which name the same paper as the entry's abs id."""
    form = _form(node)
    if form is None:
        raise ParseError("arxiv: entry without an arXiv abs id")
    work_id = f"arxiv:{form}"
    title = _text(node, "a:title")
    if not title:
        raise ParseError(f"arxiv: {work_id} has no title")
    authors = tuple(_author(a) for a in node.findall("a:author", NS))
    if not authors:
        raise ParseError(f"arxiv: {work_id} has no authors")
    year = _year(node.findtext("a:published", "", NS).strip())
    published = to_id("doi", node.findtext("x:doi", "", NS))
    primary = node.find("x:primary_category", NS)
    return Record(
        Source.ARXIV,
        ids | {work_id},
        Kind.PREPRINT,
        "arxiv",
        title,
        authors=authors,
        year=year,
        url=f"https://arxiv.org/abs/{form}",
        howpublished="arXiv",
        primary_class=(primary.get("term") or None) if primary is not None else None,
        relations=frozenset({(Rel.PUBLISHED_AS, published)}) if published and published != work_id else frozenset(),
        status=Status.WITHDRAWN if _WITHDRAWN.search(_text(node, "x:comment")) else Status.OK,
    )


def parse_feed(body: bytes) -> list[ET.Element]:
    """Entries of one Atom feed. An error entry, a wrong root or a totalResults mismatch is a ParseError."""
    try:
        root = ET.fromstring(body)  # noqa: S314
    except ET.ParseError as e:
        raise ParseError(f"arxiv: {e}") from e
    if root.tag != f"{{{NS['a']}}}feed":
        raise ParseError(f"arxiv: unexpected root {root.tag}")
    entries = root.findall("a:entry", NS)
    for e in entries:
        entry_id = e.findtext("a:id", "", NS).strip()
        if m := _REJECTED.search(entry_id):
            raise RejectedIdError(m.group(1))
        if "/api/errors" in entry_id:
            raise ParseError(f"arxiv: {_text(e, 'a:summary') or 'error entry'}")
    total = root.findtext("o:totalResults", "", NS).strip()
    if not total.isdecimal() or int(total) != len(entries):
        raise ParseError(f"arxiv: totalResults {total!r} for {len(entries)} entries")
    return entries


def by_id(http: Http, ids: Sequence[str]) -> Batch:
    """Look up arxiv: ids with id_list, 100 per call, keyed by the same arxiv: ids."""
    wanted: dict[str, list[str]] = {}
    missing: set[str] = set()
    for work_id in sorted(set(ids)):
        kind, value = split_id(work_id)
        if kind != "arxiv" or norm_arxiv(value) != value:
            missing.add(work_id)
        else:
            wanted.setdefault(query_form(work_id), []).append(work_id)
    found: dict[str, Record] = {}
    failed: set[str] = set()
    forms = list(wanted)
    chunks = [list(c) for c in batched(forms, CHUNK, strict=False)]
    while chunks:
        chunk = chunks.pop(0)
        asked = [i for f in chunk for i in wanted[f]]
        reply = http.get(HOST, "/api/query", {"id_list": ",".join(chunk), "max_results": str(CHUNK)})
        try:
            entries = parse_feed(reply.body) if reply.fetch is Fetch.OK else None
        except RejectedIdError as e:
            if e.form not in chunk:
                failed.update(asked)
                continue
            # the refused id would fail its batch on every run, so it is missing and the rest is asked again
            missing.update(wanted[e.form])
            if rest := [f for f in chunk if f != e.form]:
                chunks.insert(0, rest)
            continue
        except ParseError:
            entries = None
        if entries is None or (not entries and len(chunk) > 1):
            failed.update(asked)
            continue
        seen: set[str] = set()
        for node in entries:
            form = _form(node)
            if form not in wanted or form in seen:
                continue
            seen.add(form)
            try:
                record = parse_entry(node, frozenset(wanted[form]))
            except ParseError:
                failed.update(wanted[form])
                continue
            for work_id in wanted[form]:
                found[work_id] = record
        missing.update(i for f in chunk if f not in seen for i in wanted[f])
    return Batch(found, frozenset(missing), frozenset(failed))
