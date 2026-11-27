import dataclasses
import re
from collections import Counter
from collections.abc import Iterable

from citeforge.http import Http, Result
from citeforge.model import Author, Event, Fetch, Identity, Member, NameKey, Work
from citeforge.sources import dblp, openalex, orcid
from citeforge.sources.dblp import DblpPerson
from citeforge.sources.orcid import OrcidRecord
from citeforge.text import fold, name_key, name_matches, split_literal

LEARN_MIN_WORKS = 3
STRONG_RULES = frozenset({"curated", "authorship_orcid", "dblp_listed"})
_HOMONYM = re.compile(r"\s+\d{4}$")


def _merge_orcid(results: list[Result[OrcidRecord]]) -> Result[OrcidRecord] | None:
    if not results:
        return None
    values = [r.value for r in results if r.value is not None]
    failed = next((r for r in results if r.fetch is Fetch.FAILED), None)
    if not values:
        return failed or results[0]
    first = values[0]
    merged = dataclasses.replace(
        first,
        other_names=tuple(
            dict.fromkeys(n for v in values for n in (v.other_names + ((v.credit,) if v.credit else ())))
        ),
        affiliations=tuple(dict.fromkeys(a for v in values for a in v.affiliations)),
        works=tuple(w for v in values for w in v.works),
    )
    if failed is not None:
        return Result(Fetch.FAILED, merged, failed.reason)
    return Result(Fetch.OK, merged)


def _names(m: Member, rec: OrcidRecord | None, person: DblpPerson | None) -> tuple[NameKey, ...]:
    raw = [split_literal(m.name)]
    if rec is not None:
        raw.append((rec.given, rec.family))
        raw += [split_literal(n) for n in (*rec.other_names, *((rec.credit,) if rec.credit else ()))]
    if person is not None:
        raw += [split_literal(_HOMONYM.sub("", n)) for n in person.names]
    keys = [name_key(g, f) for g, f in raw if f]
    return tuple(dict.fromkeys(k for k in keys if k.families))


def resolve(
    m: Member, http: Http, lab_rors: frozenset[str]
) -> tuple[Identity, Result[OrcidRecord] | None, Result[DblpPerson] | None, tuple[Event, ...]]:
    events: list[Event] = []
    orcid_result = _merge_orcid([orcid.record(http, o) for o in m.orcids])
    person = dblp.person(http, m.dblp) if m.dblp else None
    author_ids: set[str] = set()
    if m.orcids:
        found = openalex.authors_by_orcid(http, m.orcids)
        if found.value:
            author_ids |= {a for ids in found.value.values() for a in ids}
    for pinned in m.openalex:
        res = openalex.author(http, pinned)
        if res.fetch is Fetch.NOT_FOUND:
            events.append(Event("openalex_id_deleted", m.dirname, pinned, ""))
            continue
        if res.value is None:
            author_ids.add(pinned)
            continue
        current, author_orcid = res.value
        if author_orcid and author_orcid not in m.orcids:
            events.append(Event("openalex_orcid_conflict", m.dirname, pinned, author_orcid))
            continue
        if current != pinned:
            events.append(Event("openalex_id_merged", m.dirname, pinned, current))
        author_ids.add(current)
    rec = orcid_result.value if orcid_result else None
    affiliations = rec.affiliations if rec else ()
    identity = Identity(
        member=m,
        orcids=frozenset(m.orcids),
        openalex=frozenset(author_ids),
        dblp=m.dblp,
        names=_names(m, rec, person.value if person else None),
        rors=lab_rors | {a.ror for a in affiliations if a.ror},
        inst_names=frozenset(f for a in affiliations if (f := fold(a.name))),
    )
    return identity, orcid_result, person, tuple(events)


def _own(identity: Identity, a: Author) -> bool:
    if a.orcid:
        return a.orcid in identity.orcids
    return bool(a.openalex and a.openalex in identity.openalex) or name_matches(identity.names, a)


def learn_institutions(identity: Identity, accepted: Iterable[tuple[Work, str]]) -> Identity:
    counts: Counter[str] = Counter()
    for work, rule in accepted:
        if rule not in STRONG_RULES:
            continue
        counts.update({r for a in work.primary.authors if _own(identity, a) for r in a.rors})
    learned = {ror for ror, n in counts.items() if n >= LEARN_MIN_WORKS}
    return dataclasses.replace(identity, rors=identity.rors | learned) if learned - identity.rors else identity
