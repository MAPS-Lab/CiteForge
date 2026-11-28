import dataclasses
from collections.abc import Mapping, Sequence

from rapidfuzz import fuzz

from citeforge.curated import Curated
from citeforge.http import Http
from citeforge.ids import is_preprint_id, is_secondary_doi, split_id, work_key
from citeforge.model import (
    REGISTRIES,
    VERSION_RELS,
    Claim,
    Event,
    Fetch,
    Group,
    Identity,
    Kind,
    Record,
    Registries,
    Source,
)
from citeforge.sources import crossref
from citeforge.text import family_keys, fold, name_matches, ratio, title_key
from citeforge.works import doi_agrees

CROSSWALK_MIN = 0.90
FUZZY_LINK = 0.92
FUZZY_PROPOSAL = 0.80
SEARCH_ACCEPT = 0.95
SEARCH_REVIEW = 0.85

type Pair = tuple[str, Claim]


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
) -> tuple[list[Pair], list[Pair], list[Event]]:
    kept, held, events = [], [], []
    for member, claim in claims:
        hold = False
        for work_id in sorted(i for i in claim.ids if split_id(i)[0] == "doi"):
            if work_id in reg.dead:
                claim = _strip_id(claim, work_id)
                events.append(Event("doi_unresolved", member, work_id, claim.title))
                continue
            record = reg.found.get(work_id)
            if record is None:
                continue
            match doi_agrees(claim.title, identities[member], record):
                case "detach":
                    claim = _strip_id(claim, work_id)
                    events.append(Event("doi_mismatch", member, work_id, f"{claim.title} | {record.title}"))
                case "review":
                    hold = True
                    events.append(Event("doi_review", member, work_id, f"{claim.title} | {record.title}"))
                case "ok":
                    pass
        if hold:
            held.append((member, claim))
        elif claim.ids:
            kept.append((member, claim))
    return kept, held, events


class _Forest:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.dois: dict[str, frozenset[str]] = {}

    def add(self, x: str) -> None:
        if x not in self.parent:
            self.parent[x] = x
            kind, value = split_id(x)
            published = kind == "doi" and not is_preprint_id(x)
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
    ranked = sorted(records, key=lambda r: (r.source not in REGISTRIES, r.source is not Source.ARXIV, min(r.ids)))
    for r in ranked:
        if r.title:
            return _Profile(r.title, r.year, tuple(fold(a.family or a.literal or "") for a in r.authors))
    for _, c in sorted(claims, key=lambda p: (p[1].source.value, sorted(p[1].ids))):
        if c.title:
            return _Profile(c.title, c.year, ())
    return None


def _fuzzy_ok(pre: _Profile, pub: _Profile) -> bool:
    if not pre.families or not pub.families or pre.year is None or pub.year is None:
        return False
    if not 0 <= pub.year - pre.year <= 3:
        return False
    if not family_keys(pre.families[0]) & family_keys(pub.families[0]):
        return False
    a, b = set(pre.families), set(pub.families)
    return len(a & b) / len(a | b) >= 0.5


def cluster(claims: Sequence[Pair], reg: Registries, curated: Curated) -> tuple[list[Group], list[Event]]:
    forest = _Forest()
    events: list[Event] = []
    records: dict[tuple[Source, frozenset[str]], Record] = {}
    for r in (*reg.found.values(), *reg.openalex.values(), *curated.records):
        records.setdefault((r.source, r.ids), r)
    for _, c in claims:
        if c.record is not None:
            records.setdefault((c.record.source, c.record.ids), c.record)
    strong: set[tuple[str, str]] = set()
    crosswalk: set[tuple[str, str, str]] = set()
    for r in records.values():
        ids = sorted(r.ids)
        for x in ids:
            forest.add(x)
        if r.source in REGISTRIES or r.source in (Source.ARXIV, Source.CURATED):
            strong |= {_edge(ids[0], x) for x in ids[1:]}
            strong |= {_edge(ids[0], t) for rel, t in r.relations if rel in VERSION_RELS and ids}
        else:
            _crosswalk(r.ids, r.source.value, strong, crosswalk)
    for _, c in claims:
        for x in c.ids:
            forest.add(x)
        _crosswalk(c.ids, c.source.value, strong, crosswalk)
    for a, b in sorted(strong):
        if not forest.union(a, b):
            events.append(Event("link_refused", None, a, b))
    for a, b in curated.links:
        forest.union(a, b, guard=False)
    for a, b, source in sorted(crosswalk):
        ta, tb = _registry_title(reg, a), _registry_title(reg, b)
        if ta is None or tb is None or ratio(ta, tb) < CROSSWALK_MIN or not forest.union(a, b):
            events.append(Event("crosswalk_rejected", None, a, f"{b} via {source}"))
    groups = _groups(forest, records, claims)
    fuzzy = _fuzzy_links(groups, events)
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


def _is_copy(work_id: str) -> bool:
    kind, value = split_id(work_id)
    return is_preprint_id(work_id) or (kind == "doi" and is_secondary_doi(value))


def _copy_group(ids: frozenset[str]) -> bool:
    version = _version_ids(ids)
    return bool(version) and all(_is_copy(i) for i in version)


def _listed_version(g: Group) -> bool:
    return any(r.source in (Source.DBLP, Source.CURATED) and r.kind is not Kind.PREPRINT for r in g.records)


def _published_group(g: Group) -> bool:
    version = _version_ids(g.ids)
    if any(split_id(i)[0] == "doi" and not _is_copy(i) for i in version):
        return True
    return not any(split_id(i)[0] == "doi" for i in version) and _listed_version(g)


def _fuzzy_links(groups: Sequence[Group], events: list[Event]) -> list[tuple[str, str]]:
    profiles = {g.key: _profile(g.records, g.claims) for g in groups}
    pre = [g for g in groups if _copy_group(g.ids) and profiles[g.key]]
    pub = [g for g in groups if _published_group(g) and profiles[g.key]]
    partners: dict[str, list[str]] = {}
    for p in pre:
        pp = profiles[p.key]
        assert pp is not None
        pk = title_key(pp.title)
        for q in pub:
            if q.key == p.key:
                continue
            qp = profiles[q.key]
            assert qp is not None
            score = fuzz.ratio(pk, title_key(qp.title), score_cutoff=FUZZY_PROPOSAL * 100) / 100
            if score < FUZZY_PROPOSAL or not _fuzzy_ok(pp, qp):
                continue
            if score >= FUZZY_LINK:
                partners.setdefault(p.key, []).append(q.key)
                partners.setdefault(q.key, []).append(p.key)
            else:
                events.append(Event("link_proposal", None, p.key, f"{q.key} ratio={score:.2f}"))
    links = []
    for a, bs in sorted(partners.items()):
        if len(bs) == 1 and _copy_group(frozenset({a})):
            links.append((a, bs[0]))
            events.append(Event("fuzzy_link", None, a, bs[0]))
    return links


def search_unidentified(
    groups: Sequence[Group], http: Http, identities: Mapping[str, Identity]
) -> tuple[list[Group], list[Event]]:
    events: list[Event] = []
    by_doi = {i: g.key for g in groups for i in g.ids if split_id(i)[0] == "doi"}
    merged: dict[str, Group] = {g.key: g for g in groups}
    for g in groups:
        kinds = {split_id(i)[0] for i in g.ids}
        if "doi" in kinds or ("arxiv" in kinds and not _listed_version(g)) or not g.claims:
            continue
        profile = _profile(g.records, g.claims)
        member = g.claims[0][0]
        identity = identities.get(member)
        if profile is None or identity is None or not identity.names:
            continue
        family = next(iter(sorted(identity.names[0].families, key=len, reverse=True)), "")
        found = crossref.search(http, profile.title, family, profile.year)
        if found.fetch is not Fetch.OK or not found.value:
            continue
        best = max(found.value, key=lambda r: (ratio(profile.title, r.title), sorted(r.ids)))
        score = ratio(profile.title, best.title)
        doi = next((i for i in sorted(best.ids) if split_id(i)[0] == "doi"), None)
        if doi is None or score < SEARCH_REVIEW:
            continue
        year_ok = profile.year is None or best.year is None or abs(best.year - profile.year) <= 1
        named = any(name_matches(identity.names, a) for a in best.authors)
        counts = [n for n in (len(profile.families), len(best.authors)) if n]
        count_ok = len(counts) < 2 or min(counts) / max(counts) >= 0.5
        if score >= SEARCH_ACCEPT and year_ok and named and count_ok:
            events.append(Event("search_linked", member, g.key, doi))
            target_key = by_doi.get(doi)
            if target_key is not None and target_key in merged and target_key != g.key:
                t = merged.pop(target_key)
                cur = merged.pop(g.key)
                merged[target_key] = dataclasses.replace(
                    t, ids=t.ids | cur.ids, records=t.records + cur.records, claims=t.claims + cur.claims
                )
            elif g.key in merged:
                cur = merged.pop(g.key)
                new = dataclasses.replace(cur, key=doi, ids=cur.ids | best.ids, records=(*cur.records, best))
                merged[doi] = new
                by_doi[doi] = doi
        else:
            events.append(Event("search_review", member, g.key, f"{doi} ratio={score:.2f}"))
    return sorted(merged.values(), key=lambda g: g.key), events
