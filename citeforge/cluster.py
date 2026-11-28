import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence

from rapidfuzz import fuzz

from citeforge.curated import Curated
from citeforge.http import Http
from citeforge.ids import is_copy, is_copy_id, split_id, work_key
from citeforge.model import (
    REGISTRIES,
    SAME_OBJECT_RELS,
    VERSION_RELS,
    Claim,
    Event,
    EventKind,
    Fetch,
    Group,
    Identity,
    Kind,
    Record,
    Registries,
    Source,
)
from citeforge.sources import crossref
from citeforge.state import State
from citeforge.text import family_keys, fold, name_matches, ratio, split_literal, title_key
from citeforge.works import EXCLUDED_KINDS, build, doi_agrees

CROSSWALK_MIN = 0.90
FUZZY_LINK = 0.92
FUZZY_PROPOSAL = 0.80
FUZZY_MARGIN = 0.02
SEARCH_ACCEPT = 0.95
SEARCH_REVIEW = 0.85
PREPRINT_YEARS = 3
POSTPRINT_YEARS = 2
_NUMERAL = re.compile(r"\d+|x{0,3}(?:ix|iv|v?i{0,3})")

type Pair = tuple[str, Claim]
type Search = Callable[[Sequence[Group]], tuple[list[tuple[str, Record]], list[Event]]]


def _version_ids(ids: frozenset[str]) -> list[str]:
    return sorted(i for i in ids if split_id(i)[0] in ("doi", "arxiv"))


def _anchor(ids: frozenset[str]) -> str:
    return work_key(_version_ids(ids) or ids)


def _edge(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _strip_id(claim: Claim, work_id: str) -> Claim:
    record = claim.record
    if record is not None:
        record = dataclasses.replace(
            record, ids=record.ids - {work_id}, relations=frozenset(r for r in record.relations if r[1] != work_id)
        )
    return dataclasses.replace(claim, ids=claim.ids - {work_id}, record=record)


def verify(
    claims: Sequence[Pair], reg: Registries, identities: Mapping[str, Identity]
) -> tuple[list[Pair], list[Pair], list[Event], Registries]:
    """Detach dead and mismatched DOIs from claims, and drop the OpenAlex records that would rejoin them."""
    kept, held, events = [], [], []
    detached: set[tuple[str, str]] = set()
    for member, claim in claims:
        hold = False
        for work_id in sorted(i for i in claim.ids if split_id(i)[0] == "doi"):
            if work_id in reg.dead:
                detached |= {(work_id, i) for i in claim.ids - {work_id}}
                claim = _strip_id(claim, work_id)
                events.append(Event(EventKind.DOI_DEAD, member, work_id, claim.title))
                continue
            record = reg.found.get(work_id)
            if record is None:
                continue
            match doi_agrees(claim.title, identities[member], record):
                case "detach":
                    detached |= {(work_id, i) for i in claim.ids - {work_id}}
                    claim = _strip_id(claim, work_id)
                    events.append(Event(EventKind.DOI_DETACHED, member, work_id, f"{claim.title} | {record.title}"))
                case "review" | "group" as verdict:
                    hold = True
                    kind = EventKind.DOI_REVIEW if verdict == "review" else EventKind.GROUP_AUTHOR
                    events.append(Event(kind, member, work_id, f"{claim.title} | {record.title}"))
                case "ok":
                    pass
        if hold:
            held.append((member, claim))
        elif claim.ids:
            kept.append((member, claim))
    openalex = {k: r for k, r in reg.openalex.items() if not any((d, i) in detached for d in r.ids for i in r.ids)}
    return kept, held, events, dataclasses.replace(reg, openalex=openalex)


class _Forest:
    def __init__(self, copies: frozenset[str]) -> None:
        self.parent: dict[str, str] = {}
        self.dois: dict[str, frozenset[str]] = {}
        self.copies = copies

    def add(self, x: str) -> None:
        if x not in self.parent:
            self.parent[x] = x
            kind, value = split_id(x)
            published = kind == "doi" and not is_copy_id(x) and x not in self.copies
            self.dois[x] = frozenset({value}) if published else frozenset()

    def find(self, x: str) -> str:
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str, guard: bool = True) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if guard and self.dois[ra] and self.dois[rb]:
            return False
        if rb < ra:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.dois[ra] |= self.dois.pop(rb)
        return True


def _registry_title(reg: Registries, work_id: str) -> str | None:
    record = reg.found.get(work_id) or reg.openalex.get(work_id)
    return record.title if record else None


@dataclasses.dataclass(frozen=True, slots=True)
class _Profile:
    title: str
    year: int | None
    families: tuple[str, ...]


def _profile(records: Sequence[Record], claims: Sequence[Pair]) -> _Profile | None:
    ranked = sorted(
        records,
        key=lambda r: (
            r.source not in REGISTRIES,
            r.source is not Source.ARXIV,
            r.kind is Kind.PREPRINT or any(map(is_copy_id, r.ids)) or is_copy(r),
            min(r.ids),
        ),
    )
    for r in ranked:
        if r.title:
            return _Profile(r.title, r.year, tuple(fold(a.family or a.literal or "") for a in r.authors))
    for _, c in sorted(claims, key=lambda p: (p[1].source.value, sorted(p[1].ids))):
        if c.title:
            return _Profile(c.title, c.year, ())
    return None


def _shared_authors(a: Sequence[str], b: Sequence[str]) -> float:
    unmatched = [family_keys(f) for f in b]
    matched = 0
    for keys in map(family_keys, a):
        hit = next((n for n, other in enumerate(unmatched) if keys & other), None)
        if hit is not None:
            matched += 1
            del unmatched[hit]
    return matched / max(len(a), len(b))


def _fuzzy_ok(pre: _Profile, pub: _Profile) -> bool:
    if not pre.families or not pub.families or pre.year is None or pub.year is None:
        return False
    if not -POSTPRINT_YEARS <= pub.year - pre.year <= PREPRINT_YEARS:
        return False
    if not family_keys(pre.families[0]) & family_keys(pub.families[0]):
        return False
    return _shared_authors(pre.families, pub.families) >= 0.5


def cluster(
    claims: Sequence[Pair], reg: Registries, curated: Curated, search: Search | None = None
) -> tuple[list[Group], list[Event]]:
    events: list[Event] = []
    records: dict[tuple[Source, frozenset[str]], Record] = {}
    for r in (*reg.found.values(), *reg.openalex.values(), *curated.records):
        records.setdefault((r.source, r.ids), r)
    for _, c in claims:
        if c.record is not None:
            records.setdefault((c.record.source, c.record.ids), c.record)
    copies = frozenset(i for r in records.values() if is_copy(r) for i in r.ids if split_id(i)[0] == "doi")
    forest = _Forest(copies)
    strong: set[tuple[str, str]] = set()
    identical: set[tuple[str, str]] = set()
    crosswalk: set[tuple[str, str, str]] = set()
    for r in records.values():
        ids = sorted(r.ids)
        for x in ids:
            forest.add(x)
        if r.source in REGISTRIES or r.source in (Source.ARXIV, Source.CURATED):
            strong |= {_edge(ids[0], x) for x in ids[1:]}
            strong |= {_edge(ids[0], t) for rel, t in r.relations if rel in VERSION_RELS and ids}
            identical |= {_edge(ids[0], t) for rel, t in r.relations if rel in SAME_OBJECT_RELS and ids}
        else:
            _crosswalk(r.ids, r.source.value, strong, crosswalk)
    for _, c in claims:
        for x in c.ids:
            forest.add(x)
        _crosswalk(c.ids, c.source.value, strong, crosswalk)
    for a, b in sorted(identical):
        forest.union(a, b, guard=False)
    for a, b in sorted(strong):
        if not forest.union(a, b):
            events.append(Event(EventKind.LINK_REFUSED, None, a, b))
    for a, b in curated.links:
        forest.union(a, b, guard=False)
    for a, b, source in sorted(crosswalk):
        ta, tb = _registry_title(reg, a), _registry_title(reg, b)
        if ta is None or tb is None or ratio(ta, tb) < CROSSWALK_MIN or not forest.union(a, b):
            events.append(Event(EventKind.CROSSWALK_REJECTED, None, a, f"{b} via {source}"))
    groups = _groups(forest, records, claims)
    if search is not None:
        found, ev = search(groups)
        events += ev
        for key, r in found:
            records.setdefault((r.source, r.ids), r)
            for x in sorted(r.ids):
                if not forest.union(key, x):
                    events.append(Event(EventKind.LINK_REFUSED, None, key, x))
        groups = _groups(forest, records, claims)
    fuzzy = _fuzzy_links(groups, events, curated, copies)
    for a, b in fuzzy:
        forest.union(a, b, guard=False)
    if fuzzy:
        joined = {forest.find(a) for a, _ in fuzzy}
        groups = [dataclasses.replace(g, fuzzy=forest.find(g.key) in joined) for g in _groups(forest, records, claims)]
    return groups, events


def _crosswalk(
    ids: frozenset[str], source: str, strong: set[tuple[str, str]], crosswalk: set[tuple[str, str, str]]
) -> None:
    if not ids:
        return
    anchor = _anchor(ids)
    version = _version_ids(ids)
    strong |= {_edge(anchor, x) for x in ids if x not in version and x != anchor}
    crosswalk |= {(*_edge(anchor, x), source) for x in version if x != anchor}


def _groups(
    forest: _Forest, records: Mapping[tuple[Source, frozenset[str]], Record], claims: Sequence[Pair]
) -> list[Group]:
    members: dict[str, set[str]] = {}
    for x in forest.parent:
        members.setdefault(forest.find(x), set()).add(x)
    rec_by_root: dict[str, list[Record]] = {}
    for r in records.values():
        if r.ids:
            rec_by_root.setdefault(forest.find(_anchor(r.ids)), []).append(r)
    claims_by_root: dict[str, list[Pair]] = {}
    for member, c in claims:
        claims_by_root.setdefault(forest.find(_anchor(c.ids)), []).append((member, c))
    out = []
    for root, ids in members.items():
        recs = rec_by_root.get(root, [])
        cl = claims_by_root.get(root, [])
        if not recs and not cl:
            continue
        uniq = sorted({(m, c) for m, c in cl}, key=lambda p: (p[0], p[1].source.value, sorted(p[1].ids), p[1].title))
        out.append(
            Group(
                key=work_key(ids),
                ids=frozenset(ids),
                records=tuple(sorted(recs, key=lambda r: (r.source.value, sorted(r.ids)))),
                claims=tuple(uniq),
            )
        )
    return sorted(out, key=lambda g: g.key)


def _copy_group(ids: frozenset[str], copies: frozenset[str]) -> bool:
    version = _version_ids(ids)
    return bool(version) and all(is_copy_id(i) or i in copies for i in version)


def _listed_version(g: Group) -> bool:
    return any(r.source in (Source.DBLP, Source.CURATED) and r.kind is not Kind.PREPRINT for r in g.records)


def _published_group(g: Group, copies: frozenset[str]) -> bool:
    version = _version_ids(g.ids)
    if any(split_id(i)[0] == "doi" and not is_copy_id(i) and i not in copies for i in version):
        return True
    return not any(split_id(i)[0] == "doi" for i in version) and _listed_version(g)


def _distinct_titles(a: str, b: str) -> bool:
    """True when near-equal titles still name two papers: a part number differs, or one word is swapped for another."""
    wa, wb = title_key(a).split(), title_key(b).split()
    if any(_NUMERAL.fullmatch(t) for t in set(wa) ^ set(wb)):
        return True
    swaps = [(x, y) for x, y in zip(wa, wb, strict=True) if x != y] if len(wa) == len(wb) else []
    if len(swaps) != 1:
        return False
    x, y = swaps[0]
    # a same-length word on another stem (minimum, maximum) names a sibling paper; retitles swap unlike words
    return len(x) == len(y) and x[:4] != y[:4]


def _fuzzy_links(
    groups: Sequence[Group], events: list[Event], curated: Curated, copies: frozenset[str]
) -> list[tuple[str, str]]:
    profiles = {g.key: _profile(g.records, g.claims) for g in groups}
    pre = [g for g in groups if _copy_group(g.ids, copies) and profiles[g.key]]
    # a notice, an abstract or front matter never absorbs a preprint, however close its title
    pub = [
        g
        for g in groups
        if _published_group(g, copies) and profiles[g.key] and build(g, curated)[0].kind not in EXCLUDED_KINDS
    ]
    links = []
    for p in pre:
        pp = profiles[p.key]
        assert pp is not None
        pk = title_key(pp.title)
        scored: list[tuple[float, str]] = []
        for q in pub:
            if q.key == p.key:
                continue
            qp = profiles[q.key]
            assert qp is not None
            score = fuzz.ratio(pk, title_key(qp.title), score_cutoff=FUZZY_PROPOSAL * 100) / 100
            if score < FUZZY_PROPOSAL or not _fuzzy_ok(pp, qp):
                continue
            if score >= FUZZY_LINK and not _distinct_titles(pp.title, qp.title):
                scored.append((score, q.key))
            else:
                events.append(Event(EventKind.LINK_PROPOSAL, None, p.key, f"{q.key} ratio={score:.2f}"))
        if not scored:
            continue
        best = max(score for score, _ in scored)
        tied = sorted(key for score, key in scored if best - score < FUZZY_MARGIN)
        if len(tied) == 1:
            links.append((p.key, tied[0]))
            events.append(Event(EventKind.FUZZY_LINK, None, p.key, tied[0]))
        else:
            events.append(Event(EventKind.LINK_PROPOSAL, None, p.key, " ".join(tied)))
    return links


def _search_ok(profile: _Profile, found: Record, identity: Identity) -> bool:
    year_ok = profile.year is None or found.year is None or abs(found.year - profile.year) <= 1
    named = any(name_matches(identity.names, a) for a in found.authors)
    counts = [n for n in (len(profile.families), len(found.authors)) if n]
    count_ok = len(counts) < 2 or min(counts) / max(counts) >= 0.5
    title_ok = ratio(profile.title, found.title) >= SEARCH_ACCEPT and not _distinct_titles(profile.title, found.title)
    return title_ok and year_ok and named and count_ok


def _doi_of(r: Record) -> str | None:
    return next((i for i in sorted(r.ids) if split_id(i)[0] == "doi"), None)


def previous_links(previous: State) -> dict[str, Record]:
    """Map each alias of a work an earlier run linked by search to the Crossref record of its DOI."""
    out: dict[str, Record] = {}
    for key, row in sorted(previous.items()):
        primary = row.work.primary
        if split_id(key)[0] == "doi" and primary.source is Source.CROSSREF and key in primary.ids:
            for alias in sorted(row.work.aliases - {key}):
                out.setdefault(alias, primary)
    return out


def search_unidentified(
    groups: Sequence[Group],
    http: Http,
    identities: Mapping[str, Identity],
    min_year: int,
    linked: Mapping[str, Record],
    reg: Registries,
) -> tuple[list[tuple[str, Record]], list[Event]]:
    """Find the Crossref DOI of each DOI-less listed group, reusing the DOI an earlier run linked it to.

    A reused DOI is judged on the record fetched this run, so later metadata and retractions apply; the stored
    record stands in only while the registry fails for that DOI.
    """
    events: list[Event] = []
    found: list[tuple[str, Record]] = []
    for g in groups:
        kinds = {split_id(i)[0] for i in g.ids}
        if "doi" in kinds or ("arxiv" in kinds and not _listed_version(g)) or not g.claims:
            continue
        profile = _profile(g.records, g.claims)
        member = g.claims[0][0]
        identity = identities.get(member)
        if profile is None or identity is None or not identity.names:
            continue
        # a match must land within a year of the group, and anything before min_year is cut anyway
        if profile.year is not None and profile.year < min_year - 1:
            continue
        prior = next((linked[i] for i in sorted(g.ids) if i in linked), None)
        if prior is not None and (doi := _doi_of(prior)) is not None:
            prior = reg.found.get(doi) or (prior if doi in reg.failed else None)
        if prior is not None and _search_ok(profile, prior, identity):
            events.append(Event(EventKind.SEARCH_LINKED, member, g.key, _doi_of(prior) or ""))
            found.append((g.key, prior))
            continue
        # Crossref requires every query.author word, so a hyphenated surname goes as its words, never joined
        family = fold(split_literal(identity.member.name)[1])
        result = crossref.search(http, profile.title, family, profile.year)
        if result.fetch is not Fetch.OK or result.value is None:
            events += [
                Event(EventKind.SEARCH_FAILED, m, g.key, result.reason) for m in sorted({m for m, _ in g.claims})
            ]
            continue
        scored = sorted(
            ((ratio(profile.title, r.title), doi, r) for r in result.value if (doi := _doi_of(r))),
            key=lambda t: (-t[0], is_copy_id(t[1]), t[1]),
        )
        close = [(score, doi) for score, doi, _ in scored if score >= SEARCH_REVIEW]
        if not close:
            continue
        score, doi, best = scored[0]
        rivals = {d for _, d in close if not is_copy_id(d)} - {doi}
        if rivals:
            events.append(
                Event(
                    EventKind.SEARCH_REVIEW, member, g.key, f"{doi} ratio={score:.2f} rivals={' '.join(sorted(rivals))}"
                )
            )
        elif _search_ok(profile, best, identity):
            events.append(Event(EventKind.SEARCH_LINKED, member, g.key, doi))
            found.append((g.key, best))
        else:
            events.append(Event(EventKind.SEARCH_REVIEW, member, g.key, f"{doi} ratio={score:.2f}"))
    return found, events
