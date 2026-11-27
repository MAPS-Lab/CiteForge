import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from citeforge.ids import split_id, to_id
from citeforge.model import Author, Decision, Kind, Member, Record, Source, Verdict
from citeforge.text import clean, split_literal

OVERRIDE_FIELDS = frozenset(
    {
        "title",
        "year",
        "journal",
        "booktitle",
        "howpublished",
        "publisher",
        "volume",
        "number",
        "pages",
        "school",
        "institution",
        "language",
        "url",
    }
)
RECORD_KINDS = frozenset(
    {
        Kind.ARTICLE,
        Kind.INPROCEEDINGS,
        Kind.INCOLLECTION,
        Kind.BOOK,
        Kind.PHDTHESIS,
        Kind.MASTERSTHESIS,
        Kind.TECHREPORT,
        Kind.PREPRINT,
    }
)
_SECTIONS = {
    "lab": {"ror"},
    "accept": {"member", "id"},
    "reject": {"member", "id", "reason"},
    "link": {"preprint", "published"},
    "override": {"id", "field", "value"},
    "record": {
        "id",
        "kind",
        "title",
        "authors",
        "year",
        "members",
        "school",
        "institution",
        "url",
        "journal",
        "booktitle",
        "publisher",
        "howpublished",
    },
}
_REQUIRED = {
    "accept": {"member", "id"},
    "reject": {"member", "id"},
    "link": {"preprint", "published"},
    "override": {"id", "field", "value"},
    "record": {"id", "kind", "title", "authors", "year", "members"},
}


class CuratedError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Curated:
    lab_rors: frozenset[str] = frozenset()
    accept: frozenset[tuple[str, str]] = frozenset()
    reject: Mapping[tuple[str, str], str] = field(default_factory=dict)
    links: tuple[tuple[str, str], ...] = ()
    overrides: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    records: tuple[Record, ...] = ()
    record_members: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def decision(self, member: str, ids: frozenset[str]) -> Decision | None:
        for scope in (member, "*"):
            hits = sorted(i for i in ids if (scope, i) in self.reject)
            if hits:
                return Decision(Verdict.REJECT, "curated", frozenset({f"reject:{self.reject[(scope, hits[0])]}"}))
        if any((member, i) in self.accept for i in ids):
            return Decision(Verdict.ACCEPT, "curated", frozenset({"accept"}))
        if any(member in self.record_members.get(i, ()) for i in ids):
            return Decision(Verdict.ACCEPT, "curated", frozenset({"record"}))
        return None

    def excluded(self, ids: frozenset[str]) -> str | None:
        hits = sorted(i for i in ids if ("*", i) in self.reject)
        return f"curated:{self.reject[('*', hits[0])]}" if hits else None

    def check_members(self, members: Sequence[Member]) -> None:
        known = {m.dirname for m in members} | {"*"}
        named = {m for m, _ in self.accept} | {m for m, _ in self.reject}
        for ms in self.record_members.values():
            named |= ms
        if unknown := sorted(named - known):
            raise CuratedError(f"curated names unknown members: {', '.join(unknown)}")


def _id(raw: Any, where: str) -> str:
    if not isinstance(raw, str):
        raise CuratedError(f"{where}: id must be a string")
    kind, value = split_id(raw)
    try:
        norm = to_id(kind, value)
    except ValueError as exc:
        raise CuratedError(f"{where}: {exc}") from exc
    if norm is None:
        raise CuratedError(f"{where}: {raw!r} is not a valid id")
    return norm


def _str(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry[key]
    if not isinstance(value, str) or not value.strip():
        raise CuratedError(f"{where}: {key} must be a non-empty string")
    return value.strip()


def _entries(data: Mapping[str, Any], section: str) -> list[tuple[str, Mapping[str, Any]]]:
    raw = data.get(section, [])
    if not isinstance(raw, list):
        raise CuratedError(f"[[{section}]] must be an array of tables")
    out: list[tuple[str, Mapping[str, Any]]] = []
    for n, entry in enumerate(raw, start=1):
        where = f"[[{section}]] #{n}"
        if not isinstance(entry, dict):
            raise CuratedError(f"{where}: must be a table")
        if extra := set(entry) - _SECTIONS[section]:
            raise CuratedError(f"{where}: unknown keys {sorted(extra)}")
        if missing := _REQUIRED[section] - set(entry):
            raise CuratedError(f"{where}: missing keys {sorted(missing)}")
        out.append((where, entry))
    return out


def _record(where: str, entry: Mapping[str, Any]) -> tuple[Record, frozenset[str]]:
    work_id = _id(entry["id"], where)
    try:
        kind = Kind(entry["kind"])
    except ValueError as exc:
        raise CuratedError(f"{where}: kind {entry['kind']!r} is not supported") from exc
    if kind not in RECORD_KINDS:
        raise CuratedError(f"{where}: kind {kind} is not a publishable kind")
    authors = entry["authors"]
    if not isinstance(authors, list) or not authors or not all(isinstance(a, str) and a.strip() for a in authors):
        raise CuratedError(f"{where}: authors must be a non-empty list of names")
    members = entry["members"]
    if not isinstance(members, list) or not members or not all(isinstance(m, str) for m in members):
        raise CuratedError(f"{where}: members must be a non-empty list of folder names")
    year = entry["year"]
    if not isinstance(year, int) or isinstance(year, bool) or not 1900 <= year <= 2100:
        raise CuratedError(f"{where}: year must be an integer between 1900 and 2100")
    optional: dict[str, Any] = {
        k: _str(entry, k, where) for k in ("school", "institution", "url", "publisher", "howpublished") if k in entry
    }
    container = tuple(_str(entry, k, where) for k in ("journal", "booktitle") if k in entry)
    record = Record(
        source=Source.CURATED,
        ids=frozenset({work_id}),
        kind=kind,
        source_type="curated",
        title=clean(_str(entry, "title", where)),
        authors=tuple(Author(*split_literal(clean(a))) for a in authors),
        year=year,
        dates=(year,),
        container=container,
        **optional,
    )
    return record, frozenset(members)


def load(path: Path) -> Curated:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise CuratedError(f"{path}: {exc}") from exc
    if extra := set(data) - set(_SECTIONS):
        raise CuratedError(f"{path}: unknown sections {sorted(extra)}")
    lab = data.get("lab", {})
    if not isinstance(lab, dict) or set(lab) - {"ror"}:
        raise CuratedError("[lab] must hold only ror")
    rors = lab.get("ror", [])
    if not isinstance(rors, list) or not all(isinstance(r, str) and r.startswith("https://ror.org/") for r in rors):
        raise CuratedError("[lab] ror must be a list of https://ror.org/ URLs")
    accept = frozenset((_str(e, "member", w), _id(e["id"], w)) for w, e in _entries(data, "accept"))
    reject: dict[tuple[str, str], str] = {}
    for w, e in _entries(data, "reject"):
        reject[(_str(e, "member", w), _id(e["id"], w))] = _str(e, "reason", w) if "reason" in e else "curated"
    if both := sorted(set(reject) & accept):
        raise CuratedError(f"curated both accepts and rejects {both[0]}")
    links = tuple(sorted((_id(e["preprint"], w), _id(e["published"], w)) for w, e in _entries(data, "link")))
    overrides: dict[str, list[tuple[str, str]]] = {}
    for w, e in _entries(data, "override"):
        name = _str(e, "field", w)
        if name not in OVERRIDE_FIELDS:
            raise CuratedError(f"{w}: field {name!r} cannot be overridden")
        overrides.setdefault(_id(e["id"], w), []).append((name, _str(e, "value", w)))
    records, record_members = [], {}
    for w, e in _entries(data, "record"):
        record, members = _record(w, e)
        (work_id,) = record.ids
        if work_id in record_members:
            raise CuratedError(f"{w}: record {work_id} is defined twice")
        records.append(record)
        record_members[work_id] = members
    return Curated(
        lab_rors=frozenset(r.rstrip("/") for r in rors),
        accept=accept,
        reject=reject,
        links=links,
        overrides={k: tuple(v) for k, v in overrides.items()},
        records=tuple(records),
        record_members=record_members,
    )
