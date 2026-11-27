from collections.abc import Iterable

from citeforge.curated import Curated
from citeforge.model import REGISTRIES, Author, Claim, Decision, Identity, Source, Verdict, Work
from citeforge.text import fold, is_group, name_matches


def _bylines(w: Work, claims: Iterable[Claim]) -> list[tuple[Author, ...]]:
    return [w.primary.authors, *(c.record.authors for c in claims if c.record is not None)]


def own_slots(i: Identity, w: Work, claims: Iterable[Claim]) -> list[Author]:
    slots = []
    for byline in _bylines(w, claims):
        for a in byline:
            if (
                (a.orcid and a.orcid in i.orcids)
                or (a.openalex and a.openalex in i.openalex)
                or name_matches(i.names, a)
            ):
                slots.append(a)
    return slots


def _affil(i: Identity, slots: Iterable[Author]) -> tuple[bool, bool]:
    stated = matched = False
    for a in slots:
        if a.rors or a.affiliations:
            stated = True
        if any(r in i.rors for r in a.rors) or any(n in fold(aff) for aff in a.affiliations for n in i.inst_names):
            matched = True
    has_institutions = bool(i.rors or i.inst_names)
    return matched, stated and not matched and has_institutions


def decide(i: Identity, w: Work, claims: Iterable[Claim], curated: Curated) -> Decision:
    claims = list(claims)
    if (decision := curated.decision(i.member.dirname, w.aliases)) is not None:
        return decision
    if w.primary.source not in REGISTRIES | {Source.CURATED, Source.DBLP, Source.ARXIV}:
        return Decision(Verdict.REVIEW, "no_registry", frozenset({w.primary.source.value}))
    bylines = _bylines(w, claims)
    if any(a.orcid in i.orcids for byline in bylines for a in byline if a.orcid):
        return Decision(Verdict.ACCEPT, "authorship_orcid", frozenset(i.orcids))
    sources = {c.source for c in claims}
    named = [a for a in w.primary.authors if name_matches(i.names, a)]
    persons = [a for a in w.primary.authors if a.family or (a.literal and not is_group(a.literal))]
    if not named and (persons or not sources & {Source.DBLP, Source.ORCID}):
        return Decision(Verdict.REJECT, "not_named", frozenset())
    if i.orcids and named and all(a.orcid and a.orcid not in i.orcids for a in named):
        return Decision(Verdict.REJECT, "orcid_contra", frozenset(a.orcid for a in named if a.orcid))
    if Source.DBLP in sources:
        return Decision(Verdict.ACCEPT, "dblp_listed", frozenset({i.dblp or ""}))
    slots = own_slots(i, w, claims)
    affil_ok, affil_contra = _affil(i, slots)
    if Source.ORCID in sources:
        if affil_contra:
            return Decision(Verdict.REVIEW, "orcid_affil_contra", _affil_evidence(slots))
        return Decision(Verdict.ACCEPT, "orcid_claim", frozenset())
    if any(a.openalex in i.openalex for a in slots if a.openalex):
        if affil_ok:
            return Decision(Verdict.ACCEPT, "openalex_affil", _affil_evidence(slots))
        return Decision(Verdict.REVIEW, "openalex_only", _affil_evidence(slots))
    return Decision(Verdict.REJECT, "no_evidence", frozenset())


def _affil_evidence(slots: Iterable[Author]) -> frozenset[str]:
    return frozenset({*(r for a in slots for r in a.rors), *(aff for a in slots for aff in a.affiliations)})
