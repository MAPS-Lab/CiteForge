from collections.abc import Iterable

from citeforge.curated import Curated
from citeforge.model import REGISTRIES, Author, Claim, Decision, Identity, Source, Verdict, Work
from citeforge.text import fold, is_group, name_matches


def bylines(w: Work, claims: Iterable[Claim]) -> list[tuple[Author, ...]]:
    return [w.primary.authors, *(c.record.authors for c in claims if c.record is not None)]


def is_own(i: Identity, a: Author) -> bool:
    if a.orcid:
        return a.orcid in i.orcids
    return bool(a.openalex and a.openalex in i.openalex) or name_matches(i.names, a)


def own_slots(i: Identity, w: Work, claims: Iterable[Claim]) -> list[Author]:
    return [a for byline in bylines(w, claims) for a in byline if is_own(i, a)]


def _affil(i: Identity, slots: Iterable[Author]) -> tuple[bool, bool]:
    """Return (matched, contradicted); only a member whose ORCID names institutions can be contradicted."""
    stated = matched = False
    for a in slots:
        if a.rors or a.affiliations:
            stated = True
        texts = [f" {fold(aff)} " for aff in a.affiliations]
        if any(r in i.rors for r in a.rors) or any(f" {n} " in t for t in texts for n in i.inst_names):
            matched = True
    return matched, stated and not matched and bool(i.inst_names)


def _registry_slots(i: Identity, w: Work) -> list[Author]:
    """The member's slots on a Crossref or DataCite byline whose affiliations no other source filled in."""
    filled = dict(w.provenance).get("author_affiliations")
    registry = (Source.CROSSREF, Source.DATACITE)
    if w.primary.source not in registry or (filled is not None and filled not in registry):
        return []
    return [a for a in w.primary.authors if is_own(i, a)]


def decide(i: Identity, w: Work, claims: Iterable[Claim], curated: Curated) -> Decision:
    claims = list(claims)
    if (decision := curated.decision(i.member.dirname, w.aliases)) is not None:
        return decision
    if w.primary.source not in REGISTRIES | {Source.CURATED, Source.DBLP, Source.ARXIV}:
        return Decision(Verdict.REVIEW, "no_registry", frozenset({w.primary.source.value}))
    lines = bylines(w, claims)
    if any(a.orcid in i.orcids for byline in lines for a in byline if a.orcid):
        return Decision(Verdict.ACCEPT, "authorship_orcid", frozenset(i.orcids))
    sources = {c.source for c in claims}
    listed = sources & {Source.DBLP, Source.ORCID}
    named = [a for a in w.primary.authors if name_matches(i.names, a)]
    # OpenAlex bylines carry the author entity's name, so only the member's own listings may stand in.
    listed_named = any(
        name_matches(i.names, a)
        for c in claims
        if c.record is not None and c.source in listed
        for a in c.record.authors
    )
    persons = [a for a in w.primary.authors if a.family or (a.literal and not is_group(a.literal))]
    if not named and not listed_named and (persons or not listed):
        if listed and w.primary.source not in REGISTRIES | {Source.CURATED}:
            return Decision(Verdict.REVIEW, "not_named_unregistered", frozenset(s.value for s in listed))
        return Decision(Verdict.REJECT, "not_named", frozenset())
    if i.orcids and named and all(a.orcid and a.orcid not in i.orcids for a in named) and not _affil(i, named)[0]:
        return Decision(Verdict.REJECT, "orcid_contra", frozenset(a.orcid for a in named if a.orcid))
    if Source.DBLP in sources:
        return Decision(Verdict.ACCEPT, "dblp_listed", frozenset({i.dblp or ""}))
    if Source.ORCID in sources:
        registry_slots = _registry_slots(i, w)
        if _affil(i, registry_slots)[1]:
            return Decision(Verdict.REVIEW, "orcid_affil_contra", _affil_evidence(registry_slots))
        return Decision(Verdict.ACCEPT, "orcid_claim", frozenset())
    slots = own_slots(i, w, claims)
    if any(a.openalex in i.openalex for a in slots if a.openalex):
        if _affil(i, slots)[0]:
            return Decision(Verdict.ACCEPT, "openalex_affil", _affil_evidence(slots))
        return Decision(Verdict.REVIEW, "openalex_only", _affil_evidence(slots))
    return Decision(Verdict.REJECT, "no_evidence", frozenset())


def _affil_evidence(slots: Iterable[Author]) -> frozenset[str]:
    return frozenset({*(r for a in slots for r in a.rors), *(aff for a in slots for aff in a.affiliations)})
