import json
import re
from dataclasses import dataclass

from citeforge.http import HOSTS, Http, ParseError, Reply, Result
from citeforge.ids import norm_orcid, norm_ror, to_id
from citeforge.model import Claim, Fetch, Source
from citeforge.text import clean

HOST = HOSTS["pub.orcid.org"]
ID_KINDS = ("doi", "arxiv", "pmid")
AFFILIATION_SECTIONS = (
    ("employments", "employment-summary", "employment"),
    ("educations", "education-summary", "education"),
)
STATUS_REASONS = {9007: "deprecated", 9018: "locked", 9036: "unclaimed", 9044: "deactivated"}
_YEAR = re.compile(r"\d{4}")
_ORCID_IN_TEXT = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")


@dataclass(frozen=True, slots=True)
class Affiliation:
    ror: str | None
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class OrcidRecord:
    given: str
    family: str
    credit: str | None
    other_names: tuple[str, ...]
    affiliations: tuple[Affiliation, ...]
    works: tuple[Claim, ...]


def _at(node: object, *path: str) -> object:
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _text(node: object, *path: str) -> str | None:
    value = _at(node, *path)
    return (value.strip() or None) if isinstance(value, str) else None


def _items(node: object, *path: str) -> list[object]:
    value = _at(node, *path)
    return value if isinstance(value, list) else []


def _ror(organization: object) -> str | None:
    if _text(organization, "disambiguated-organization", "disambiguation-source") != "ROR":
        return None
    return norm_ror(_text(organization, "disambiguated-organization", "disambiguated-organization-identifier") or "")


def _affiliations(activities: object) -> tuple[Affiliation, ...]:
    out: list[Affiliation] = []
    for section, key, kind in AFFILIATION_SECTIONS:
        for group in _items(activities, section, "affiliation-group"):
            for summary in _items(group, "summaries"):
                organization = _at(summary, key, "organization")
                if name := _text(organization, "name"):
                    out.append(Affiliation(_ror(organization), name, kind))
    return tuple(dict.fromkeys(out))


def _ids(summary: object) -> set[str]:
    found: set[str] = set()
    for ext in _items(summary, "external-ids", "external-id"):
        kind = (_text(ext, "external-id-type") or "").lower()
        if kind not in ID_KINDS or _text(ext, "external-id-relationship") != "self":
            continue
        raw = (_text(ext, "external-id-value"), _text(ext, "external-id-normalized", "value"))
        if work_id := next((i for r in raw if r and (i := to_id(kind, r))), None):
            found.add(work_id)
    return found


def _year(summary: object) -> int | None:
    value = _text(summary, "publication-date", "year", "value")
    return int(value) if value and _YEAR.fullmatch(value) else None


def _claim(group: object, orcid: str) -> Claim | None:
    summaries = [s for s in _items(group, "work-summary") if isinstance(s, dict)]
    ids = frozenset(i for s in summaries for i in _ids(s))
    title = next((t for s in summaries if (t := clean(_text(s, "title", "title", "value") or ""))), "")
    codes = sorted(c for s in summaries if isinstance(c := s.get("put-code"), int) and not isinstance(c, bool))
    if not ids and title and codes:
        # a work with no identifier is named by its put-code, so a title search can still find its DOI
        ids = frozenset({f"orcidwork:{orcid}/{codes[0]}"})
    if not ids and not title:
        return None
    year = next((y for s in summaries if (y := _year(s)) is not None), None)
    return Claim(Source.ORCID, ids, title, year)


def parse_record(body: bytes, orcid: str) -> OrcidRecord:
    try:
        doc = json.loads(body)
    except ValueError, RecursionError:
        raise ParseError("orcid record is not JSON") from None
    if _text(doc, "orcid-identifier", "path") != orcid:
        raise ParseError(f"orcid record root is not {orcid}")
    activities = _at(doc, "activities-summary")
    groups = _at(activities, "works", "group")
    if not isinstance(groups, list):
        raise ParseError("orcid record has no works section")
    name = _at(doc, "person", "name")
    others = (_text(o, "content") for o in _items(doc, "person", "other-names", "other-name"))
    return OrcidRecord(
        given=_text(name, "given-names", "value") or "",
        family=_text(name, "family-name", "value") or "",
        credit=_text(name, "credit-name", "value"),
        other_names=tuple(dict.fromkeys(o for o in others if o)),
        affiliations=_affiliations(activities),
        works=tuple(c for g in groups if (c := _claim(g, orcid)) is not None),
    )


def _status_reason(reply: Reply) -> str:
    try:
        code = _at(json.loads(reply.body), "error-code")
    except ValueError, RecursionError:
        code = None
    reason = STATUS_REASONS.get(code) if isinstance(code, int) else None
    if reason == "deprecated":
        primary = _ORCID_IN_TEXT.findall(reply.location or "") or _ORCID_IN_TEXT.findall(
            reply.body.decode("utf-8", "replace")
        )
        return f"deprecated:{primary[0]}" if primary else reason
    return reason or reply.reason


def record(http: Http, orcid: str) -> Result[OrcidRecord]:
    """Fetch /v3.0/{orcid}/record."""
    oid = norm_orcid(orcid)
    if oid is None:
        return Result(Fetch.NOT_FOUND, None, "invalid_orcid")
    reply = http.get(HOST, f"/v3.0/{oid}/record", headers={"Accept": "application/json"})
    if reply.fetch is Fetch.NOT_FOUND:
        return Result(Fetch.NOT_FOUND, None, "not_found")
    if reply.fetch is Fetch.FAILED:
        return Result(Fetch.FAILED, None, _status_reason(reply))
    try:
        return Result(Fetch.OK, parse_record(reply.body, oid))
    except ParseError:
        return Result(Fetch.FAILED, None, "parse_error")
