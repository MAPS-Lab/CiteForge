from collections.abc import Iterable, Mapping

from citeforge.http import Http
from citeforge.ids import split_id
from citeforge.model import VERSION_RELS, Fetch, Record, Registries
from citeforge.sources import arxiv, crossref, datacite, doiorg, openalex

REGISTERED = frozenset({"crossref", "datacite"})


def fetch(ids: Iterable[str], http: Http, have: Mapping[str, Record]) -> Registries:
    ids = sorted(set(ids))
    dois = [i for i in ids if split_id(i)[0] == "doi"]
    arxivs = [i for i in ids if split_id(i)[0] == "arxiv"]
    found: dict[str, Record] = {}
    dead: set[str] = set()
    failed: set[str] = set()
    cr = crossref.by_doi(http, dois)
    found |= cr.found
    rest = sorted(cr.missing | cr.failed)
    dc = datacite.by_doi(http, rest)
    found |= dc.found
    failed |= (cr.failed - dc.found.keys()) | (dc.failed - cr.failed)
    unknown = sorted(dc.missing - cr.failed)
    if unknown:
        agencies = doiorg.agency(http, unknown)
        if agencies.value is None:
            failed |= set(unknown)
        else:
            for work_id in unknown:
                agency = agencies.value.get(work_id, "")
                if agency is None:
                    dead.add(work_id)
                elif agency.strip().lower() in REGISTERED or not agency:
                    failed.add(work_id)
                else:
                    res = doiorg.csl(http, work_id)
                    if res.value is not None:
                        found[work_id] = res.value
                    elif res.fetch is Fetch.NOT_FOUND:
                        dead.add(work_id)
                    else:
                        failed.add(work_id)
    arxivs = sorted(
        set(arxivs)
        | {t for r in found.values() for rel, t in r.relations if rel in VERSION_RELS and split_id(t)[0] == "arxiv"}
    )
    ax = arxiv.by_id(http, arxivs)
    found |= ax.found
    dead |= ax.missing
    failed |= ax.failed
    oa = openalex.works_by_doi(http, [d for d in dois if d not in have and d not in dead])
    return Registries(
        found=found,
        dead=frozenset(dead),
        failed=frozenset(failed),
        openalex={**have, **oa.found},
    )
