from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO

from citeforge.http import Http
from citeforge.ids import split_id
from citeforge.model import Fetch, Member
from citeforge.sources import dblp, openalex, orcid

DBLP_OVERLAP = 3
EMPLOYMENT_OVERLAP = 5


@dataclass(frozen=True, slots=True)
class Proposal:
    member: str
    orcid: str | None
    openalex: tuple[str, ...]
    dblp_lists: bool
    overlap: int
    employment: bool
    accept: bool
    note: str


def _dois(claims: Sequence) -> set[str]:
    return {i for c in claims for i in c.ids if split_id(i)[0] == "doi"}


def propose(members: Sequence[Member], http: Http, lab_rors: frozenset[str]) -> list[Proposal]:
    out: list[Proposal] = []
    for m in sorted(members, key=lambda m: m.dirname):
        person = dblp.person(http, m.dblp) if m.dblp else None
        if person is not None and person.fetch is Fetch.FAILED:
            out.append(Proposal(m.dirname, None, (), False, 0, False, False, f"dblp {person.reason}"))
            continue
        dblp_orcids = set(person.value.orcids) if person and person.value else set()
        dblp_dois = _dois(person.value.claims) if person and person.value else set()
        candidates = sorted(set(m.orcids) | dblp_orcids)
        if not candidates:
            out.append(Proposal(m.dirname, None, (), False, 0, False, False, "no ORCID candidate"))
            continue
        for candidate in candidates:
            rec = orcid.record(http, candidate)
            if rec.value is None:
                out.append(Proposal(m.dirname, candidate, (), candidate in dblp_orcids, 0, False, False,
                                    f"orcid {rec.reason}"))  # fmt: skip
                continue
            overlap = len(_dois(rec.value.works) & dblp_dois)
            employment = any(a.kind == "employment" and a.ror in lab_rors for a in rec.value.affiliations)
            listed = candidate in dblp_orcids
            accept = (
                candidate in m.orcids
                or (listed and overlap >= DBLP_OVERLAP)
                or (overlap >= EMPLOYMENT_OVERLAP and employment)
            )
            authors = openalex.authors_by_orcid(http, [candidate]) if accept else None
            ids = authors.value.get(candidate, ()) if authors and authors.value else ()
            note = "pinned" if candidate in m.orcids else ("accepted" if accept else "below threshold")
            out.append(Proposal(m.dirname, candidate, tuple(sorted(ids)), listed, overlap, employment, accept, note))
    return out


def print_table(props: Sequence[Proposal], out: TextIO) -> None:
    out.write("member\torcid\topenalex\tdblp_lists\toverlap\temployment\taccept\tnote\n")
    for p in props:
        cells = [p.member, p.orcid or "", ";".join(p.openalex), str(p.dblp_lists).lower(), str(p.overlap),
                 str(p.employment).lower(), str(p.accept).lower(), p.note]  # fmt: skip
        out.write("\t".join(cells) + "\n")
