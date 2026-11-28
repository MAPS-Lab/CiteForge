import argparse
import dataclasses
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from citeforge import authorship, cluster, discover, enrich, publish, report, resolve, works
from citeforge.bibtex import assign_names
from citeforge.curated import Curated
from citeforge.http import HOSTS, Http
from citeforge.ids import split_id
from citeforge.model import (
    REGISTRIES,
    Claim,
    Decision,
    Event,
    EventKind,
    Fetch,
    Group,
    Identity,
    Listing,
    Member,
    Names,
    Source,
    Verdict,
    Work,
)
from citeforge.sources import pubmed
from citeforge.state import State, StateRow

MAX_WORKERS = 8
SINCE_SLACK_YEARS = 2
ROLLING_WINDOW_YEARS = 7
PER_YEAR_CAP = 50
SHRINK_SHARE = 0.20
SHRINK_MIN = 5
MISSING_RUNS = 2
EMPTY_NAMES = Names("", "")


class ConfigError(ValueError):
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class Settings:
    input: Path
    output: Path
    group: Path
    curated: Path
    state: Path
    run: Path
    min_year: int
    today: date
    openalex_key: str

    @classmethod
    def from_args(cls, ns: argparse.Namespace, env: Mapping[str, str], today: date) -> Settings:
        raw = env.get("CITEFORGE_MIN_YEAR")
        if raw is None:
            min_year = 2020
        elif not raw.strip():
            min_year = today.year - (ROLLING_WINDOW_YEARS - 1)
        elif raw.strip().isdecimal() and 1900 <= int(raw) <= today.year + 1:
            min_year = int(raw)
        else:
            raise ConfigError(f"CITEFORGE_MIN_YEAR={raw!r} is not a year")
        key = env.get("OPENALEX_API_KEY", "").strip()
        key_file = Path("keys/OpenAlex.key")
        if not key and key_file.is_file():
            key = key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise ConfigError("set OPENALEX_API_KEY or write keys/OpenAlex.key")
        for path in (ns.input, ns.group, ns.curated):
            if not Path(path).is_file():
                raise ConfigError(f"{path} does not exist")
        return cls(
            input=Path(ns.input),
            output=Path(ns.output),
            group=Path(ns.group),
            curated=Path(ns.curated),
            state=Path(ns.state),
            run=Path(ns.run),
            min_year=min_year,
            today=today,
            openalex_key=key,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class RunResult:
    state: dict[str, StateRow]
    files: dict[Path, str]
    stale: frozenset[Path]
    report: dict
    exit_code: int


def _claim_order(pair: tuple[str, Claim]) -> tuple[str, str, list[str], str]:
    member, c = pair
    return member, c.source.value, sorted(c.ids), c.title


def _pairs(w: Work, g: Group, identities: Mapping[str, Identity], curated: Curated) -> dict[str, list[Claim]]:
    out: dict[str, list[Claim]] = defaultdict(list)
    for member, c in g.claims:
        out[member].append(c)
    by_orcid = {o: m for m, i in identities.items() for o in i.orcids}
    by_openalex = {a: m for m, i in identities.items() for a in i.openalex}
    for byline in authorship.bylines(w, (c for _, c in g.claims)):
        for a in byline:
            for m in (by_orcid.get(a.orcid or ""), by_openalex.get(a.openalex or "")):
                if m is not None:
                    out.setdefault(m, [])
    for member, work_id in curated.accept | set(curated.reject):
        if work_id in w.aliases and member in identities:
            out.setdefault(member, [])
    for work_id, members in curated.record_members.items():
        if work_id in w.aliases:
            for member in members & identities.keys():
                out.setdefault(member, [])
    return out


def _with_pubmed_dois(pairs: list[tuple[str, Claim]], http: Http) -> tuple[list[tuple[str, Claim]], frozenset[str]]:
    def doi_less(c: Claim) -> bool:
        return not any(split_id(i)[0] == "doi" for i in c.ids)

    pmids = sorted({i for _, c in pairs if doi_less(c) for i in c.ids if split_id(i)[0] == "pmid"})
    if not pmids:
        return pairs, frozenset()
    res = pubmed.dois(http, pmids)
    found = res.value or {}
    failed = frozenset(pmids) - found.keys() if res.fetch is Fetch.FAILED else frozenset()
    return [
        (m, dataclasses.replace(c, ids=c.ids | {found[i] for i in c.ids if i in found})) if doi_less(c) else (m, c)
        for m, c in pairs
    ], failed


def _windowed(d: Decision, w: Work, s: Settings) -> Decision:
    if d.verdict is Verdict.REJECT:
        return d
    if w.year is not None and w.year < s.min_year:
        return Decision(Verdict.REJECT, "out_of_window", d.evidence | {d.rule})
    if d.verdict is Verdict.REVIEW:
        return d
    if w.year is None:
        return Decision(Verdict.REVIEW, "no_year", d.evidence | {d.rule})
    if w.year > s.today.year + 1 and w.primary.source not in REGISTRIES:
        return Decision(Verdict.REVIEW, "future_year", d.evidence | {d.rule})
    return d


def _held_because(listing: Listing, registry_failed: bool) -> str | None:
    """Why removals are held for this member this run, or None when every listing and registry lookup came back."""
    failed = sorted(s.value for s, f in listing.fetch.items() if f is Fetch.FAILED)
    if registry_failed:
        failed.append("registry")
    if failed:
        return "partial:" + ",".join(failed)
    if Fetch.OK not in listing.fetch.values():
        return "no_profile"
    return None


@dataclasses.dataclass(slots=True)
class _Kept:
    decisions: dict[str, Decision] = dataclasses.field(default_factory=dict)
    works: dict[str, Work] = dataclasses.field(default_factory=dict)
    removals: dict[str, str] = dataclasses.field(default_factory=dict)
    missing: dict[str, int] = dataclasses.field(default_factory=dict)
    shrink_held: int = 0


def _judge_previous(
    name: str,
    prev: Mapping[str, StateRow],
    mine: Mapping[str, Decision],
    by_key: Mapping[str, Work],
    alias_owner: Mapping[str, str],
    unreadable: frozenset[str],
    held_ids: frozenset[str],
    hold: bool,
) -> _Kept:
    """Keep or remove each work the member had last run; `unreadable` aliases failed a registry lookup."""
    out = _Kept()
    for k, row in sorted(prev.items()):
        if row.work.aliases & unreadable or (k in by_key and by_key[k].aliases & unreadable):
            out.decisions[k] = row.members[name]
            out.works[k] = row.work
            continue
        now = mine.get(k)
        w = by_key.get(k)
        if publish.publishable(now, w):
            continue
        heir = next((alias_owner[a] for a in sorted(row.work.aliases) if a in alias_owner), None)
        if w is None and heir is not None and publish.publishable(mine.get(heir), by_key[heir]):
            reason = f"merged_into:{heir}"
        elif hold:
            reason = None
        elif w is not None and w.excluded is not None:
            reason = w.excluded
        elif w is None and k in alias_owner:
            reason = f"merged_into:{alias_owner[k]}"
        elif now is not None and now.verdict is Verdict.REJECT:
            reason = now.rule
        elif now is None and row.work.aliases & held_ids:
            reason = None
        elif now is None:
            count = row.missing.get(name, 0) + 1
            out.missing[k] = count
            reason = "missing" if count >= MISSING_RUNS else None
        else:
            reason = None
        if reason is None:
            out.decisions[k] = row.members[name]
            if w is None:
                out.works[k] = row.work
        else:
            out.removals[k] = reason
    if len(out.removals) >= SHRINK_MIN and len(out.removals) > SHRINK_SHARE * len(prev):
        out.shrink_held = len(out.removals)
        for k in out.removals:
            out.decisions[k] = prev[k].members[name]
            if k not in by_key:
                out.works[k] = prev[k].work
        out.removals = {}
    return out


def run(
    s: Settings,
    members: Sequence[Member],
    group: Sequence[Member],
    curated: Curated,
    http: Http,
    previous: State,
    existing: frozenset[Path],
) -> RunResult:
    """Build the next state and its files; `existing` is every .bib on disk in the member and group folders."""
    started = time.monotonic()
    stages: dict[str, float] = {}

    def stage(name: str, t0: float) -> float:
        now = time.monotonic()
        stages[name] = round(now - t0, 3)
        return now

    members = sorted(members, key=lambda m: m.dirname)
    events: list[Event] = []

    def per_member(m: Member) -> tuple[Identity, Listing, tuple[Event, ...]]:
        identity, orcid_result, dblp_result, openalex_lookup, ev = resolve.resolve(m, http, curated.lab_rors)
        since = s.min_year - SINCE_SLACK_YEARS
        listing = discover.discover(identity, orcid_result, dblp_result, openalex_lookup, http, since)
        return identity, listing, ev

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        resolved = list(pool.map(per_member, members))
    t = stage("resolve_discover", started)
    identities = {i.member.dirname: i for i, _, _ in resolved}
    listings = {listing.member: listing for _, listing, _ in resolved}
    for _, _, ev in resolved:
        events += ev
    pairs = sorted(((lst.member, c) for lst in listings.values() for c in lst.claims), key=_claim_order)
    pairs, pubmed_failed = _with_pubmed_dois(pairs, http)
    linked = cluster.previous_links(previous)
    wanted = {i for _, c in pairs for i in c.ids if split_id(i)[0] in ("doi", "arxiv")}
    wanted |= {i for link in curated.links for i in link}
    wanted |= {i for _, c in pairs for a in c.ids & linked.keys() for i in linked[a].ids if split_id(i)[0] == "doi"}
    have = {
        i: c.record
        for _, c in pairs
        if c.source is Source.OPENALEX and c.record is not None
        for i in c.record.ids
        if split_id(i)[0] == "doi"
    }
    reg = enrich.fetch(wanted, http, have)
    reg = dataclasses.replace(reg, failed=reg.failed | pubmed_failed)
    t = stage("enrich", t)
    kept, held, ev, reg = cluster.verify(pairs, reg, identities)
    events += ev
    held_ids: dict[str, set[str]] = defaultdict(set)
    for member, c in held:
        held_ids[member] |= c.ids
    groups, ev = cluster.cluster(
        kept, reg, curated, lambda gs: cluster.search_unidentified(gs, http, identities, s.min_year, linked, reg)
    )
    events += ev
    for name in sorted({e.member for e in ev if e.kind is EventKind.SEARCH_FAILED and e.member in listings}):
        listing = listings[name]
        listings[name] = dataclasses.replace(listing, fetch={**listing.fetch, Source.CROSSREF: Fetch.FAILED})
    t = stage("cluster", t)
    built = [works.build(g, curated) for g in groups]
    for _, ev in built:
        events += ev
    current = works.apply_links([w for w, _ in built])
    group_of = {w.key: g for w, g in zip(current, groups, strict=True)}
    by_key = {w.key: w for w in current}
    if len(by_key) != len(current):
        raise RuntimeError("two groups built the same work key")
    pair_claims = {w.key: _pairs(w, group_of[w.key], identities, curated) for w in current}

    def decide_all(ids: Mapping[str, Identity]) -> dict[str, dict[str, Decision]]:
        out: dict[str, dict[str, Decision]] = defaultdict(dict)
        for key, per_member_claims in pair_claims.items():
            for member, claims in per_member_claims.items():
                out[member][key] = authorship.decide(ids[member], by_key[key], claims, curated)
        return out

    first = decide_all(identities)
    learned = {
        m: resolve.learn_institutions(
            i, ((by_key[k], d.rule) for k, d in first[m].items() if d.verdict is Verdict.ACCEPT)
        )
        for m, i in identities.items()
    }
    decisions = decide_all(learned)
    for m in decisions:
        decisions[m] = {k: _windowed(d, by_key[k], s) for k, d in decisions[m].items()}
    t = stage("decide", t)

    registry_held = {w.key for w in current if w.aliases & reg.failed}
    failed_registry = {k for k in registry_held if by_key[k].primary.source not in REGISTRIES}
    window_years = s.today.year - s.min_year + 1
    final: dict[str, dict[str, Decision]] = {}
    missing: dict[str, dict[str, int]] = defaultdict(dict)
    carried_works: dict[str, Work] = {}
    statuses: dict[str, str] = {}
    held_any = guard = False
    alias_owner = {a: w.key for w in current for a in w.aliases}
    member_changes: dict[str, dict[str, list]] = {}
    for m in members:
        name = m.dirname
        mine = dict(decisions.get(name, {}))
        prev = {k: row for k, row in previous.items() if publish.emitted(row, name)}
        registry_failed = bool(registry_held & (mine.keys() | prev.keys())) or any(
            row.work.aliases & reg.failed for row in prev.values()
        )
        held_because = _held_because(listings[name], registry_failed)
        held_any |= held_because is not None
        accepted = [k for k, d in mine.items() if publish.publishable(d, by_key[k])]
        over_cap = len(accepted) > PER_YEAR_CAP * window_years
        if over_cap:
            guard = True
            mine = {k: row.members[name] for k, row in previous.items() if name in row.members}
            for k, row in previous.items():
                if name in row.members and k not in by_key:
                    carried_works[k] = row.work
        for k in sorted(failed_registry & mine.keys()):
            if k in previous:
                carried_works[k] = previous[k].work
                if name in previous[k].members:
                    mine[k] = previous[k].members[name]
            else:
                mine.pop(k)
                events.append(Event(EventKind.PENDING, name, k, "pending:registry"))
        kept = _judge_previous(
            name, prev, mine, by_key, alias_owner, reg.failed, frozenset(held_ids[name]),
            held_because is not None or over_cap,
        )  # fmt: skip
        if kept.shrink_held:
            guard = True
            events.append(Event(EventKind.SHRINK_GUARD, name, None, f"{kept.shrink_held} of {len(prev)} removals held"))
        mine.update(kept.decisions)
        carried_works.update(kept.works)
        for k, count in kept.missing.items():
            missing[k][name] = count
        final[name] = mine
        member_changes[name] = {"removed": sorted(kept.removals.items())}
        emitted = sum(publish.publishable(d, carried_works.get(k) or by_key[k]) for k, d in mine.items())
        statuses[name] = "over_cap" if over_cap else held_because or ("complete" if emitted else "no_works_in_window")
    t = stage("failure_rules", t)

    all_works = {**by_key, **carried_works}
    for k in failed_registry:
        if k in previous:
            all_works[k] = previous[k].work
    emitted_keys = {k for mine in final.values() for k, d in mine.items() if publish.publishable(d, all_works[k])}
    prev_names = {k: row.names for k, row in previous.items() if row.names.stem}
    names = assign_names([all_works[k] for k in sorted(emitted_keys)], prev_names)
    new_state: dict[str, StateRow] = {}
    for k in sorted(all_works):
        row_members = {name: mine[k] for name, mine in final.items() if k in mine}
        if not row_members:
            continue
        new_state[k] = StateRow(
            work=all_works[k],
            names=names.get(k) or prev_names.get(k) or EMPTY_NAMES,
            members=dict(sorted(row_members.items())),
            missing=dict(sorted(missing.get(k, {}).items())),
        )
    files = publish.plan(new_state, members, group, s.output.name or "output")
    problems = publish.check(files)
    if problems:
        raise publish.PlanError("; ".join(problems[:20]))
    owned = publish.bib_paths(previous, members, group).keys()
    stale = frozenset(owned - files.keys())
    # a file no state row ever emitted (a first run, a lost state/, a hand-placed file) is kept and reported
    untracked = sorted(p.as_posix() for p in existing - owned - files.keys())
    t = stage("publish_plan", t)

    for name in member_changes:
        before = {k: row.names.stem for k, row in previous.items() if publish.emitted(row, name)}
        after = {k: row.names.stem for k, row in new_state.items() if publish.emitted(row, name)}
        member_changes[name]["added"] = sorted((k, after[k]) for k in after.keys() - before.keys())
        member_changes[name]["renamed"] = sorted(
            (k, before[k], after[k]) for k in after.keys() & before.keys() if before[k] != after[k]
        )
    down = {h: r for h in sorted(HOSTS) if (r := http.down(HOSTS[h]))}
    exit_code = 3 if held_any or down or guard or reg.failed or untracked else 0
    data = report.RunData(
        min_year=s.min_year,
        stages=stages,
        sources=http.stats(),
        down=down,
        statuses=statuses,
        listings={n: dict(Counter(c.source.value for c in lst.claims)) for n, lst in listings.items()},
        fetch={n: {src.value: f.value for src, f in lst.fetch.items()} for n, lst in listings.items()},
        decisions=final,
        changes=member_changes,
        works=all_works,
        state=new_state,
        held=held,
        events=sorted(events, key=lambda e: (e.kind, e.member or "", e.id or "", e.detail)),
        orcid_claims={
            n: [c for c in lst.claims if c.source is Source.ORCID and (c.year is None or c.year >= s.min_year)]
            for n, lst in listings.items()
        },
        registry_failed=reg.failed,
        untracked=untracked,
        exit_code=exit_code,
    )
    stage("total", started)
    return RunResult(new_state, files, stale, report.build(data), exit_code)
