import dataclasses
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Literal

from citeforge.authorship import is_own
from citeforge.curated import Curated
from citeforge.ids import is_copy_id, split_id, work_key
from citeforge.model import (
    REGISTRIES,
    Author,
    Event,
    Group,
    Identity,
    Kind,
    Record,
    Rel,
    Source,
    Status,
    Work,
)
from citeforge.tables import (
    ACM_JOURNAL_PROCEEDINGS,
    CONFERENCE_AS_JOURNAL,
    INSTITUTIONAL_REPOSITORIES,
    JOURNALS_NAMED_PROCEEDINGS,
    PROCEEDINGS_SERIES_AS_JOURNAL,
    REPOSITORY_AS_JOURNAL,
)
from citeforge.text import (
    clean,
    family_keys,
    fold,
    is_affiliation,
    is_group,
    name_matches,
    ratio,
    title_key,
    token_set,
    word_prefix,
)

EXCLUDED_KINDS = frozenset(
    {Kind.PROCEEDINGS, Kind.DATASET, Kind.SOFTWARE, Kind.PATENT, Kind.EDITORIAL, Kind.FRONT_MATTER, Kind.ABSTRACT,
     Kind.NOTICE, Kind.PEER_REVIEW, Kind.COMPONENT, Kind.OTHER}
)  # fmt: skip
_EDITORIAL = re.compile(r"^((guest|series) )?editorial\b", re.IGNORECASE)
_FRONT = re.compile(
    r"^(preface\b|foreword\b|front matter\b|message from\b|table of contents\b"
    r"|welcome(\W*$| message\b| from\b| address\b| to the \d+(st|nd|rd|th)\b)"
    r"|conclusions?( and future works?)?$|dear |proceedings of\b)",
    re.IGNORECASE,
)
_NOTICE = re.compile(r"^(corrections? to|correction:|erratum|corrigendum|expression of concern)\b", re.I)
_RETRACTION = re.compile(r"^retraction\b", re.I)
_PAGE_RANGE = re.compile(r"(\d+)\s*[-\u2013\u2014]+\s*(\d+)")
_ABSTRACT = re.compile(r"^[A-Z]{0,2}\d{1,4}(?:\.\d{1,3}\.?|\.)\s+(?=[A-Z])")
_ABSTRACT_PAGES = re.compile(r"^(S\d|e?\d+$)")
_CONFERENCE_SUFFIX = re.compile(r"conference|workshop|symposium")
_CONFERENCE_JOURNAL = re.compile(r"^(proceedings of|proc\.|conference|international conference|workshop)\b")
_PRIMARY_KIND_RANK = (
    Kind.ARTICLE, Kind.INPROCEEDINGS, Kind.INCOLLECTION, Kind.BOOK, Kind.TECHREPORT, Kind.PHDTHESIS,
    Kind.MASTERSTHESIS,
)  # fmt: skip
_FILL_ORDER = (Source.CROSSREF, Source.DATACITE, Source.CSL, Source.DBLP, Source.ARXIV, Source.OPENALEX)
_FILL_FIELDS = (
    "container", "event", "publisher", "volume", "number", "pages", "article_number", "url", "language", "school",
    "institution", "editors", "primary_class", "degree",
)  # fmt: skip
_YEAR_ORDER = (Source.CROSSREF, Source.DATACITE, Source.CSL, Source.ARXIV, Source.DBLP, Source.OPENALEX, Source.CURATED)
_OVERRIDE_ATTR = {"journal": "container", "booktitle": "container"}


def _starts(value: str, prefixes: Iterable[str]) -> bool:
    return any(value.startswith(p) for p in prefixes)


def _named_proceedings(value: str) -> bool:
    for name in JOURNALS_NAMED_PROCEEDINGS:
        if value.startswith(name):
            return not _CONFERENCE_SUFFIX.search(value[len(name) :])
    return False


VENUE_RULES: tuple[tuple[frozenset[Kind], Callable[[str], bool], Kind], ...] = (
    (frozenset({Kind.ARTICLE}), lambda v: _starts(v, PROCEEDINGS_SERIES_AS_JOURNAL), Kind.INPROCEEDINGS),
    (frozenset({Kind.INPROCEEDINGS}), lambda v: _starts(v, ACM_JOURNAL_PROCEEDINGS), Kind.ARTICLE),
    (frozenset({Kind.INPROCEEDINGS}), _named_proceedings, Kind.ARTICLE),
    (frozenset({Kind.INPROCEEDINGS}), lambda v: any(r in v for r in INSTITUTIONAL_REPOSITORIES), Kind.PHDTHESIS),
    (
        frozenset({Kind.ARTICLE, Kind.INPROCEEDINGS}),
        lambda v: any(r in v for r in REPOSITORY_AS_JOURNAL),
        Kind.PREPRINT,
    ),
    (frozenset({Kind.ARTICLE}), lambda v: v in CONFERENCE_AS_JOURNAL, Kind.INPROCEEDINGS),
    (
        frozenset({Kind.ARTICLE}),
        lambda v: (
            bool(_CONFERENCE_JOURNAL.match(v)) and not _named_proceedings(v) and not _starts(v, ACM_JOURNAL_PROCEEDINGS)
        ),
        Kind.INPROCEEDINGS,
    ),
)


def _base_kind(r: Record) -> Kind:
    if r.kind is Kind.PHDTHESIS and r.degree and "master" in r.degree.lower():
        return Kind.MASTERSTHESIS
    return r.kind


def _page_span(pages: str | None) -> int | None:
    m = _PAGE_RANGE.fullmatch((pages or "").strip())
    if m is None:
        return None
    first, last = m.groups()
    if len(last) < len(first):
        last = first[: len(first) - len(last)] + last
    return int(last) - int(first) + 1


def _research_paper(r: Record, kind: Kind) -> bool:
    span = _page_span(r.pages)
    return kind in _PRIMARY_KIND_RANK and any(a.family for a in r.authors) and span is not None and span > 2


def classify(r: Record) -> Kind:
    kind = _base_kind(r)
    title = clean(r.title)
    if _EDITORIAL.match(title):
        return Kind.EDITORIAL
    if _FRONT.match(title):
        return Kind.FRONT_MATTER
    if any(rel in (Rel.CORRECTS, Rel.RETRACTS, Rel.WITHDRAWS) for rel, _ in r.relations) or _NOTICE.match(title):
        return Kind.NOTICE
    # a study of retractions starts its title the same way; notices run a page or two and research runs longer
    if _RETRACTION.match(title) and not _research_paper(r, kind):
        return Kind.NOTICE
    one_page = bool(r.pages and _ABSTRACT_PAGES.match(r.pages)) or "suppl" in (r.number or "").lower()
    if _ABSTRACT.match(title) and one_page:
        return Kind.ABSTRACT
    venue = (r.container[0] if r.container else "").strip().lower()
    if venue:
        for kinds, test, target in VENUE_RULES:
            if kind in kinds and test(venue):
                return target
    return kind


def year_of(records: Sequence[Record]) -> int | None:
    for source in _YEAR_ORDER:
        for r in sorted(records, key=_record_order):
            if r.source is source and r.year is not None:
                return r.year
    return None


def doi_agrees(claim_title: str, member: Identity, reg: Record) -> Literal["ok", "review", "group", "detach"]:
    titles = (reg.title, *reg.alt_titles)
    title_score = max(token_set(claim_title, t) for t in titles)
    title_ok = title_score >= 0.90 or any(ratio(claim_title, t) >= 0.85 or word_prefix(claim_title, t) for t in titles)
    people = [a for a in reg.authors if a.family or a.literal]
    # A foreign ORCID on a named slot is an authorship question for decide, not a sign of a wrong DOI.
    named = any(is_own(member, a) or name_matches(member.names, a) for a in people)
    if title_score < 0.60:
        return "review" if named else "detach"
    if people and not named:
        return "group" if title_ok and any(a.literal and is_group(a.literal) for a in people) else "detach"
    return "ok" if title_ok else "review"


def _record_order(r: Record) -> tuple[str, str]:
    return r.source.value, min(r.ids, default="")


def _doi(r: Record) -> str | None:
    dois = sorted(i for i in r.ids if split_id(i)[0] == "doi")
    return dois[0] if dois else None


def _primary(records: Sequence[Record]) -> Record:
    def rank(r: Record) -> tuple[int, int, int, str]:
        kind = classify(r)
        kind_rank = _PRIMARY_KIND_RANK.index(kind) if kind in _PRIMARY_KIND_RANK else len(_PRIMARY_KIND_RANK)
        # a DBLP listing carries the arXiv id of its paper, so only a copy DOI or a variant relation marks a copy
        secondary = (
            kind is Kind.PREPRINT
            or any(is_copy_id(i) for i in r.ids if split_id(i)[0] == "doi")
            or any(rel is Rel.VARIANT_OF for rel, _ in r.relations)
        )
        if r.source in REGISTRIES and not secondary:
            tier = 0
        elif r.source is Source.CURATED:
            tier = 1
        elif r.source is Source.DBLP and not secondary:
            tier = 2
        elif r.source in REGISTRIES:
            tier = 3
        elif r.source is Source.ARXIV:
            tier = 4
        elif r.source is Source.DBLP:
            tier = 5
        else:
            tier = 6
        return tier, kind_rank, r.year if r.year is not None else 9999, min(r.ids, default="")

    return min(records, key=rank)


def _merge_author_ids(primary: tuple[Author, ...], donor: tuple[Author, ...]) -> tuple[Author, ...] | None:
    if len(primary) != len(donor) or not primary:
        return None
    out = []
    for a, d in zip(primary, donor, strict=True):
        if a.family and d.family and not family_keys(a.family) & family_keys(d.family):
            return None
        out.append(
            dataclasses.replace(
                a,
                orcid=a.orcid or d.orcid,
                openalex=a.openalex or d.openalex,
                rors=a.rors or d.rors,
                affiliations=a.affiliations or d.affiliations,
            )
        )
    return tuple(out)


def _is_sorted_byline(authors: tuple[Author, ...]) -> bool:
    families = [fold(a.family) for a in authors]
    return len(families) >= 3 and all(families) and families == sorted(families)


def _repair_order(primary: Record, candidates: Sequence[Record]) -> tuple[tuple[Author, ...], Source] | None:
    if primary.source is not Source.CROSSREF or not _is_sorted_byline(primary.authors):
        return None
    current = [fold(a.family) for a in primary.authors]
    for d in candidates:
        order = [fold(a.family) for a in d.authors]
        if d.source not in (Source.ARXIV, Source.DBLP) or sorted(order) != sorted(current) or order == current:
            continue
        pool = list(primary.authors)
        ordered = []
        for family in order:
            match = next(p for p in pool if fold(p.family) == family)
            pool.remove(match)
            ordered.append(match)
        return tuple(ordered), d.source
    return None


def clean_byline(authors: tuple[Author, ...]) -> tuple[tuple[Author, ...], list[str]]:
    persons = any(a.family for a in authors)
    seen: set[tuple[str, str, str | None, tuple[str, ...]]] = set()
    out, dropped = [], []
    for a in authors:
        if a.literal and not a.family and persons and is_affiliation(a.literal):
            dropped.append(f"author_affiliation_dropped:{a.literal}")
            continue
        key = (fold(a.given), fold(a.family or a.literal or ""), a.orcid, a.rors)
        if key in seen and (a.orcid or len(authors) >= 50):
            dropped.append(f"author_repeat_dropped:{a.given} {a.family}".strip())
            continue
        seen.add(key)
        out.append(a)
    return tuple(out), dropped


def _apply_override(r: Record, name: str, value: str) -> Record:
    if name == "year":
        return dataclasses.replace(r, year=int(value), dates=(int(value),))
    attr = _OVERRIDE_ATTR.get(name, name)
    change: dict[str, Any] = {attr: (value,) if attr == "container" else value}
    return dataclasses.replace(r, **change)


def build(g: Group, curated: Curated) -> tuple[Work, tuple[Event, ...]]:
    records = sorted(g.records, key=_record_order)
    if not records:
        claim = min((c for _, c in g.claims), key=lambda c: (c.source.value, sorted(c.ids)))
        records = [Record(claim.source, claim.ids, Kind.OTHER, "claim", claim.title, year=claim.year)]
    primary = _primary(records)
    own_authors = primary.authors
    provenance: dict[str, Source] = {"title": primary.source, "authors": primary.source}
    doi = _doi(primary)
    donors = sorted(
        (r for r in records if r is not primary and doi and doi in r.ids),
        key=lambda r: (_FILL_ORDER.index(r.source) if r.source in _FILL_ORDER else 99, _record_order(r)),
    )
    updates: dict[str, Any] = {}
    for name in _FILL_FIELDS:
        if getattr(primary, name):
            provenance[name] = primary.source
            continue
        donor = next((d for d in donors if getattr(d, name)), None)
        if donor is not None:
            updates[name] = getattr(donor, name)
            provenance[name] = donor.source
    people = [a for a in primary.authors if a.family]
    if not people:
        donor = next((d for d in donors if any(a.family for a in d.authors)), None)
        if donor is not None:
            updates["authors"] = donor.authors
            provenance["authors"] = donor.source
    authors = updates.get("authors", primary.authors)
    assert isinstance(authors, tuple)
    authors, dropped = clean_byline(authors)
    if dropped:
        updates["authors"] = authors
    if (repair := _repair_order(dataclasses.replace(primary, authors=authors), records)) is not None:
        authors, provenance["author_order"] = repair
        updates["authors"] = authors
    for d in donors:
        if (merged := _merge_author_ids(authors, d.authors)) is not None and merged != authors:
            if any((m.rors, m.affiliations) != (a.rors, a.affiliations) for m, a in zip(merged, authors, strict=True)):
                provenance.setdefault("author_affiliations", d.source)
            authors = updates["authors"] = merged
            provenance.setdefault("author_ids", d.source)
    primary = dataclasses.replace(primary, **updates)
    same_version = [primary, *(r for r in records if doi and doi in r.ids and r is not primary)]
    year = year_of(same_version) if primary.source is not Source.ARXIV else primary.year
    if year is not None:
        provenance["year"] = next(r.source for r in same_version if r.year == year)
    for work_id in sorted(g.ids):
        for name, value in curated.overrides.get(work_id, ()):
            primary = _apply_override(primary, name, value)
            provenance[name] = Source.CURATED
            if name == "year":
                year = int(value)
    eprint = None
    if not g.fuzzy and not any(split_id(i)[0] == "arxiv" for i in primary.ids):
        arxiv = [r for r in records if r is not primary and any(split_id(i)[0] == "arxiv" for i in r.ids)]
        arxiv.sort(key=lambda r: (r.source is not Source.ARXIV, _record_order(r)))
        eprint = arxiv[0] if arxiv else None
    # kind rules read the primary record's own byline, never one filled in from another source
    kind = classify(dataclasses.replace(primary, authors=own_authors))
    if kind is Kind.OTHER and primary.container:
        kind = next((k for d in donors if d.source is Source.OPENALEX and (k := classify(d)) is not Kind.OTHER), kind)
    status = primary.status
    excluded = None
    if kind in EXCLUDED_KINDS:
        excluded = f"kind:{kind.value}"
    elif status is not Status.OK:
        excluded = f"status:{status.value}"
    if reason := curated.excluded(g.ids):
        excluded = reason
    work = Work(
        key=work_key(i for i in primary.ids if split_id(i)[0] != "openalex") if _keyable(primary) else g.key,
        primary=primary,
        eprint=eprint,
        linked=frozenset(),
        kind=kind,
        status=status,
        year=year,
        excluded=excluded,
        provenance=tuple(sorted(provenance.items())),
        aliases=g.ids,
        sources=frozenset(r.source for r in records) | frozenset(c.source for _, c in g.claims),
    )
    return work, tuple(Event(d.partition(":")[0], None, work.key, d.partition(":")[2]) for d in dropped)


def _keyable(r: Record) -> bool:
    return any(split_id(i)[0] != "openalex" for i in r.ids)


def _families(w: Work) -> frozenset[str]:
    return frozenset(fold(a.family) for a in w.primary.authors if a.family)


def apply_links(works: Sequence[Work]) -> list[Work]:
    by_alias = {alias: w.key for w in works for alias in w.aliases}
    linked: dict[str, set[str]] = {}
    status: dict[str, Status] = {}
    excluded: dict[str, str] = {}
    for w in works:
        for rel, target in sorted(w.primary.relations):
            target_key = by_alias.get(target)
            if target_key is None or target_key == w.key:
                continue
            if rel in (Rel.CORRECTS, Rel.RETRACTS, Rel.WITHDRAWS):
                linked.setdefault(target_key, set()).add(w.key)
                if rel is Rel.RETRACTS:
                    status[target_key] = Status.RETRACTED
                elif rel is Rel.WITHDRAWS:
                    status[target_key] = Status.WITHDRAWN
            elif rel is Rel.TRANSLATION_OF:
                linked.setdefault(target_key, set()).add(w.key)
                excluded.setdefault(w.key, f"translation_of:{target_key}")
    books = {title_key(w.primary.title): w for w in works if w.kind is Kind.BOOK and w.excluded is None}
    for w in works:
        book = (
            books.get(title_key(w.primary.container[0]))
            if w.kind is Kind.INCOLLECTION and w.primary.container
            else None
        )
        if book is not None and book.key != w.key and _families(w) and _families(w) <= _families(book):
            linked.setdefault(book.key, set()).add(w.key)
            excluded.setdefault(w.key, f"chapter_of:{book.key}")
    out = []
    for w in works:
        new_status = status.get(w.key, w.status)
        reason = w.excluded or excluded.get(w.key)
        if new_status is not Status.OK and (reason is None or reason.startswith("translation_of")):
            reason = f"status:{new_status.value}"
        out.append(
            dataclasses.replace(
                w, linked=w.linked | frozenset(linked.get(w.key, ())), status=new_status, excluded=reason
            )
        )
    return out
