import dataclasses
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Literal

from rapidfuzz import fuzz

from citeforge.curated import Curated
from citeforge.http import Http, Result
from citeforge.ids import is_copy_id, split_id, work_key
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
    Rel,
    Source,
)
from citeforge.sources import crossref
from citeforge.state import Searched, State
from citeforge.text import family_keys, fold, name_matches, ratio, split_literal, title_key
from citeforge.works import EXCLUDED_KINDS, build, doi_agrees, page_span

CROSSWALK_MIN = 0.90
FUZZY_LINK = 0.92
FUZZY_PROPOSAL = 0.80
FUZZY_MARGIN = 0.02
SEARCH_ACCEPT = 0.95
SEARCH_REVIEW = 0.85
PREPRINT_YEARS = 3
POSTPRINT_YEARS = 2
SEARCH_REUSE_DAYS = 90
# a part, volume or edition number, or a year; a longer number is a count that changes between versions
_NUMERAL = re.compile(r"\d{1,2}|(?:19|20)\d\d|x{0,3}(?:ix|iv|v?i{0,3})")
_GROUPED_NUMBER = re.compile(r"(?<!\d)\d{1,3}(?:[,\u2009\u202f ]\d{3})+(?!\d)")
_SPRINGER_CHAPTER = re.compile(r"doi:10\.1007/(97[89][\d-]+)_(\d+)(?:-(\d+))?")

type Pair = tuple[str, Claim]
type Search = Callable[[Sequence[Group]], tuple[list[tuple[str, Record]], list[Event]]]
type RecordKey = tuple[Source, frozenset[str]]


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
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.dois: dict[str, frozenset[str]] = {}

    def add(self, x: str) -> None:
        if x not in self.parent:
            self.parent[x] = x
            kind, value = split_id(x)
            published = kind == "doi" and not is_copy_id(x)
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
            r.kind is Kind.PREPRINT or any(map(is_copy_id, r.ids)),
            min(r.ids),
        ),
    )
    for r in ranked:
        if r.title:
            return _Profile(r.title, r.year, _families(r))
    for _, c in sorted(claims, key=lambda p: (p[1].source.value, sorted(p[1].ids))):
        if c.title:
            return _Profile(c.title, c.year, ())
    return None


def _families(r: Record) -> tuple[str, ...]:
    # an organiser or consortium credited by name is no person, so it never counts as the first author
    people = [a.family for a in r.authors if a.family] or [a.literal for a in r.authors if a.literal]
    return tuple(map(fold, people))


def _shared_authors(a: Sequence[str], b: Sequence[str]) -> float:
    unmatched = [family_keys(f) for f in b]
    matched = 0
    for keys in map(family_keys, a):
        hit = next((n for n, other in enumerate(unmatched) if keys & other), None)
        if hit is not None:
            matched += 1
            del unmatched[hit]
    return matched / max(len(a), len(b))


def _byline_fit(pre: _Profile, pub: _Profile) -> Literal["link", "propose"] | None:
    """Whether two versions can carry one paper's byline: "link", "propose" when only the lead author differs."""
    if not pre.families or not pub.families or pre.year is None or pub.year is None:
        return None
    if not -POSTPRINT_YEARS <= pub.year - pre.year <= PREPRINT_YEARS:
        return None
    shared = _shared_authors(pre.families, pub.families)
    if shared < 0.5:
        return None
    # versions often reorder their byline, so a near-identical byline links whoever leads it
    if shared >= 0.8 or family_keys(pre.families[0]) & family_keys(pub.families[0]):
        return "link"
    return "propose"


def _variants(records: Mapping[RecordKey, Record]) -> dict[RecordKey, Record]:
    """Mark the copies no registry relation names as variants of the record that leads.

    A repository deposit of a Crossref article carries the article's title, year and byline under another publisher.
    Springer registers each revision of a living-reference chapter as <isbn>_<n>-<v>: the print chapter
    <isbn>_<n> of the same title, byline and book leads, else the latest revision.
    """
    out = dict(records)
    articles: dict[tuple[str, int | None], list[Record]] = defaultdict(list)
    revisions: dict[str, list[tuple[int, RecordKey, Record]]] = defaultdict(list)
    prints: dict[tuple[str, str, tuple[str, ...], str], Record] = {}
    for k, r in sorted(records.items(), key=lambda kv: sorted(kv[0][1])):
        doi = _doi_of(r)
        if r.source is not Source.CROSSREF or doi is None:
            continue
        if r.kind is Kind.ARTICLE:
            articles[(title_key(r.title), r.year)].append(r)
        if (m := _SPRINGER_CHAPTER.fullmatch(doi)) is None:
            continue
        if m[3] is not None:
            revisions[f"{m[1]}_{m[2]}"].append((int(m[3]), k, r))
        elif r.container:
            prints.setdefault((m[2], title_key(r.title), _families(r), title_key(r.container[0])), r)
    for k, r in records.items():
        if r.source is not Source.DATACITE or r.kind is not Kind.ARTICLE:
            continue
        if any(rel in SAME_OBJECT_RELS for rel, _ in r.relations):
            continue
        article = next(
            (
                a
                for a in articles.get((title_key(r.title), r.year), ())
                if fold(a.publisher or "") != fold(r.publisher or "")
                and _shared_authors(_families(r), _families(a)) >= 0.8
            ),
            None,
        )
        if article is not None and (doi := _doi_of(article)) is not None:
            out[k] = dataclasses.replace(r, relations=r.relations | {(Rel.VARIANT_OF, doi)})
    for stem, revs in revisions.items():
        revs.sort(key=lambda t: -t[0])
        latest = revs[0][2]
        chapter = stem.rpartition("_")[2]
        container = title_key(latest.container[0]) if latest.container else ""
        lead = prints.get((chapter, title_key(latest.title), _families(latest), container)) or latest
        lead_doi = _doi_of(lead)
        for _, k, r in revs:
            if r is not lead and lead_doi is not None:
                out[k] = dataclasses.replace(r, relations=r.relations | {(Rel.VARIANT_OF, lead_doi)})
    return out


def cluster(
    claims: Sequence[Pair], reg: Registries, curated: Curated, search: Search | None = None
) -> tuple[list[Group], list[Event]]:
    forest = _Forest()
    events: list[Event] = []
    records: dict[tuple[Source, frozenset[str]], Record] = {}
    for r in (*reg.found.values(), *reg.openalex.values(), *curated.records):
        records.setdefault((r.source, r.ids), r)
    for _, c in claims:
        if c.record is not None:
            records.setdefault((c.record.source, c.record.ids), c.record)
    records = _variants(records)
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
        agree = ta is not None and tb is not None and ratio(ta, tb) >= CROSSWALK_MIN and not _distinct_titles(ta, tb)
        if not agree or not forest.union(a, b):
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
    fuzzy = _fuzzy_links(groups, events, curated)
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


def _listed_version(g: Group) -> bool:
    return any(r.source in (Source.DBLP, Source.CURATED) and r.kind is not Kind.PREPRINT for r in g.records)


def _published_group(g: Group) -> bool:
    # DBLP often files an SSRN or Zenodo DOI under the journal record it lists, which stays published
    return _listed_version(g) or any(split_id(i)[0] == "doi" and not is_copy_id(i) for i in g.ids)


def _copy_group(g: Group) -> bool:
    version = _version_ids(g.ids)
    return bool(version) and all(map(is_copy_id, version)) and not _published_group(g)


def _distinct_titles(a: str, b: str) -> bool:
    """True when near-equal titles still name two papers: a part number differs, or one word is swapped for another."""
    wa, wb = (title_key(_GROUPED_NUMBER.sub(" ", t)).split() for t in (a, b))
    if any(_NUMERAL.fullmatch(t) for t in set(wa) ^ set(wb)):
        return True
    swaps = [(x, y) for x, y in zip(wa, wb, strict=True) if x != y] if len(wa) == len(wb) else []
    if len(swaps) != 1:
        return False
    x, y = swaps[0]
    # a same-length word on another stem (minimum, maximum) names a sibling paper; retitles swap unlike words,
    # and a number other than a part or a year is a count
    return len(x) == len(y) and x[:4] != y[:4] and not x.isdigit()


def _match(a: _Profile, b: _Profile) -> tuple[float, Literal["link", "propose"] | None]:
    score = fuzz.ratio(title_key(a.title), title_key(b.title), score_cutoff=FUZZY_PROPOSAL * 100) / 100
    fit = _byline_fit(a, b) if score >= FUZZY_PROPOSAL else None
    if fit is None:
        return score, None
    if fit == "link" and score >= FUZZY_LINK and not _distinct_titles(a.title, b.title):
        return score, "link"
    return score, "propose"


def _fuzzy_links(groups: Sequence[Group], events: list[Event], curated: Curated) -> list[tuple[str, str]]:
    profiles = {g.key: p for g in groups if (p := _profile(g.records, g.claims))}
    pre = [g for g in groups if g.key in profiles and _copy_group(g)]
    # a notice, an abstract or front matter never absorbs a preprint, however close its title
    pub = {
        g.key: w
        for g in groups
        if g.key in profiles and _published_group(g) and (w := build(g, curated)[0]).kind not in EXCLUDED_KINDS
    }
    links = []
    # copies of one paper on two servers join first, so the pair then links to its published version once
    copies = _Forest()
    for n, a in enumerate(pre):
        for b in pre[n + 1 :]:
            if copies.find(a.key) == copies.find(b.key):
                continue
            older, newer = sorted((profiles[a.key], profiles[b.key]), key=lambda p: p.year or 0)
            score, verdict = _match(older, newer)
            if verdict == "link":
                copies.union(a.key, b.key, guard=False)
                links.append((a.key, b.key))
                events.append(Event(EventKind.FUZZY_LINK, None, a.key, b.key))
            elif verdict == "propose":
                events.append(Event(EventKind.LINK_PROPOSAL, None, a.key, f"{b.key} ratio={score:.2f}"))
    joined: dict[str, list[str]] = defaultdict(list)
    for g in pre:
        joined[copies.find(g.key)].append(g.key)
    for keys in joined.values():
        scored: dict[str, float] = {}
        for key in keys:
            for q in pub:
                if q in keys:
                    continue
                score, verdict = _match(profiles[key], profiles[q])
                if verdict == "link":
                    scored[q] = max(score, scored.get(q, 0.0))
                elif verdict == "propose":
                    events.append(Event(EventKind.LINK_PROPOSAL, None, key, f"{q} ratio={score:.2f}"))
        if not scored:
            continue
        best = max(scored.values())
        tied = sorted(q for q, score in scored.items() if best - score < FUZZY_MARGIN)
        if len(tied) > 1:
            # a paper and its extended abstract, or one paper under two DOIs: the longest version absorbs the copy
            # and a person confirms which version is the record
            events.append(Event(EventKind.LINK_PROPOSAL, None, keys[0], " ".join(tied)))
        target = min(tied, key=lambda q: (-(page_span(pub[q].primary.pages) or 0), q))
        links.append((keys[0], target))
        events.append(Event(EventKind.FUZZY_LINK, None, keys[0], target))
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


def _dblp_listings(g: Group) -> frozenset[str]:
    listed = (r for r in g.records if r.source is Source.DBLP and r.kind is not Kind.PREPRINT)
    return frozenset(i for r in listed for i in r.ids if split_id(i)[0] == "dblp")


def _other_listing(listings: Mapping[str, frozenset[str]], own: frozenset[str], doi: str) -> str | None:
    """The DBLP key the group of doi lists, when it differs from the searched group's own listing.

    DBLP lists two keys as two publications, so a title search never merges them.
    """
    other = sorted(listings.get(doi, frozenset()) - own)
    return other[0] if own and other else None


def search_unidentified(
    groups: Sequence[Group],
    http: Http,
    identities: Mapping[str, Identity],
    min_year: int,
    linked: Mapping[str, Record],
    reg: Registries,
    searched: Mapping[str, Searched],
    today: date,
) -> tuple[list[tuple[str, Record]], list[Event], dict[str, Searched]]:
    """Find the Crossref DOI of each DOI-less listed group, reusing what an earlier run found for it.

    A reused DOI is judged on the record fetched this run, so later metadata and retractions apply; the stored
    record stands in only while the registry fails for that DOI. A search that linked nothing is kept by group
    key and repeated once its query changes or SEARCH_REUSE_DAYS have passed. Returns the links, the events and
    the searches to keep.
    """
    events: list[Event] = []
    found: list[tuple[str, Record]] = []
    keep: dict[str, Searched] = {}
    results: dict[tuple[str, str, int | None], Result[tuple[Record, ...]]] = {}
    listings = {i: keys for g in groups if (keys := _dblp_listings(g)) for i in g.ids}
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
        own = _dblp_listings(g)
        prior = next((linked[i] for i in sorted(g.ids) if i in linked), None)
        if prior is not None and (doi := _doi_of(prior)) is not None:
            prior = reg.found.get(doi) or (prior if doi in reg.failed else None)
        doi = _doi_of(prior) if prior is not None else None
        if prior is not None and _search_ok(profile, prior, identity) and not _other_listing(listings, own, doi or ""):
            events.append(Event(EventKind.SEARCH_LINKED, member, g.key, doi or ""))
            found.append((g.key, prior))
            continue
        # Crossref requires every query.author word, so a hyphenated surname goes as its words, never joined
        query = (profile.title, fold(split_literal(identity.member.name)[1]), profile.year)
        before = next((searched[i] for i in sorted(g.ids) if i in searched), None)
        if before is not None and before.query == query and 0 <= (today - before.on).days < SEARCH_REUSE_DAYS:
            keep[g.key] = before
            if before.review is not None:
                events.append(Event(EventKind.SEARCH_REVIEW, member, g.key, before.review))
            continue
        if query not in results:
            results[query] = crossref.search(http, *query)
        result = results[query]
        if result.fetch is not Fetch.OK or result.value is None:
            events += [
                Event(EventKind.SEARCH_FAILED, m, g.key, result.reason) for m in sorted({m for m, _ in g.claims})
            ]
            if before is not None:
                keep[g.key] = before
            continue
        scored = sorted(
            ((ratio(profile.title, r.title), doi, r) for r in result.value if (doi := _doi_of(r))),
            key=lambda t: (-t[0], is_copy_id(t[1]), t[1]),
        )
        close = [(score, doi) for score, doi, _ in scored if score >= SEARCH_REVIEW]
        review = None
        if close:
            score, doi, best = scored[0]
            rivals = {d for _, d in close if not is_copy_id(d)} - {doi}
            if rivals:
                review = f"{doi} ratio={score:.2f} rivals={' '.join(sorted(rivals))}"
            elif (listed := _other_listing(listings, own, doi)) is not None:
                review = f"{doi} ratio={score:.2f} listed={listed}"
            elif _search_ok(profile, best, identity):
                events.append(Event(EventKind.SEARCH_LINKED, member, g.key, doi))
                found.append((g.key, best))
                continue
            else:
                review = f"{doi} ratio={score:.2f}"
            events.append(Event(EventKind.SEARCH_REVIEW, member, g.key, review))
        keep[g.key] = Searched(query, review, today)
    return found, events, keep
