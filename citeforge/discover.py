from citeforge.http import Http, Result
from citeforge.model import Claim, Fetch, Identity, Listing, Source
from citeforge.sources import openalex
from citeforge.sources.dblp import DblpPerson
from citeforge.sources.orcid import OrcidRecord

SUSPECT_OTHER_MIN = 5


def _since(claims: tuple[Claim, ...], since_year: int) -> tuple[Claim, ...]:
    return tuple(c for c in claims if c.year is None or c.year >= since_year)


def discover(
    identity: Identity,
    orcid: Result[OrcidRecord] | None,
    dblp: Result[DblpPerson] | None,
    http: Http,
    since_year: int,
) -> Listing:
    claims: dict[Source, tuple[Claim, ...]] = {}
    fetch: dict[Source, Fetch] = {}
    if orcid is not None:
        fetch[Source.ORCID] = orcid.fetch
        claims[Source.ORCID] = _since(orcid.value.works, since_year) if orcid.value else ()
    if dblp is not None:
        fetch[Source.DBLP] = dblp.fetch
        claims[Source.DBLP] = _since(dblp.value.claims, since_year) if dblp.value else ()
    if identity.openalex:
        listed = openalex.list_works(http, sorted(identity.openalex), since_year)
        fetch[Source.OPENALEX] = listed.fetch
        claims[Source.OPENALEX] = _since(listed.value, since_year) if listed.value else ()
    most = max((len(c) for c in claims.values()), default=0)
    for source, found in claims.items():
        if not found and fetch[source] is not Fetch.FAILED and most >= SUSPECT_OTHER_MIN:
            fetch[source] = Fetch.FAILED
    ordered = tuple(c for source in sorted(claims) for c in claims[source])
    return Listing(identity.member.dirname, ordered, fetch)
