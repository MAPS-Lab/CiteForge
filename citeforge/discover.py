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
    openalex_lookup: Fetch | None,
    http: Http,
    since_year: int,
) -> Listing:
    raw: dict[Source, tuple[Claim, ...]] = {}
    fetch: dict[Source, Fetch] = {}
    if orcid is not None:
        fetch[Source.ORCID] = orcid.fetch
        raw[Source.ORCID] = orcid.value.works if orcid.value else ()
    if dblp is not None:
        fetch[Source.DBLP] = dblp.fetch
        raw[Source.DBLP] = dblp.value.claims if dblp.value else ()
    if identity.openalex:
        listed = openalex.list_works(http, sorted(identity.openalex), since_year)
        fetch[Source.OPENALEX] = listed.fetch
        raw[Source.OPENALEX] = listed.value or ()
    if openalex_lookup is Fetch.FAILED:
        fetch[Source.OPENALEX] = Fetch.FAILED
    # OpenAlex is listed from since_year on the server, so the others are compared over the same window.
    most = max((len(_since(c, since_year)) for c in raw.values()), default=0)
    for source, found in raw.items():
        # A parsed ORCID record with no works is the owner's own empty list, not a silent failure.
        if not found and source is not Source.ORCID and fetch[source] is not Fetch.FAILED and most >= SUSPECT_OTHER_MIN:
            fetch[source] = Fetch.FAILED
    ordered = tuple(c for source in sorted(raw) for c in _since(raw[source], since_year))
    return Listing(identity.member.dirname, ordered, fetch)
