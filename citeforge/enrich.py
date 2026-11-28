from collections.abc import Iterable, Mapping, Sequence

from citeforge.http import Http
from citeforge.ids import ids_of
from citeforge.model import VERSION_RELS, Fetch, Record, Registries
from citeforge.sources import arxiv, crossref, datacite, doiorg, openalex

REGISTERED = frozenset({"crossref", "datacite"})


def _by_doi(http: Http, dois: Sequence[str]) -> tuple[dict[str, Record], set[str], set[str], set[str]]:
    found: dict[str, Record] = {}
    dead: set[str] = set()
    failed: set[str] = set()
    unlisted: set[str] = set()
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
                elif agency.strip().lower() == "datacite":
                    # the DataCite API serves findable DOIs only, so a registered or draft one stays absent
                    unlisted.add(work_id)
                elif agency.strip().lower() in REGISTERED or not agency:
                    failed.add(work_id)
                else:
                    res = doiorg.csl(http, work_id)
                    if res.value is not None:
                        found[work_id] = res.value
                    elif res.fetch is Fetch.NOT_FOUND:
                        dead.add(work_id)
                    elif res.reason != "redirect_blocked":
                        # an agency whose CSL host is off the allow-list fails the same way on every run
                        failed.add(work_id)
    return found, dead, failed, unlisted


def _versions(records: Iterable[Record], kind: str) -> set[str]:
    return set(ids_of((t for r in records for rel, t in r.relations if rel in VERSION_RELS), kind))


def fetch(ids: Iterable[str], http: Http, have: Mapping[str, Record]) -> Registries:
    ids = sorted(set(ids))
    dois = ids_of(ids, "doi")
    found, dead, failed, unlisted = _by_doi(http, dois)
    ax = arxiv.by_id(http, sorted({*ids_of(ids, "arxiv"), *_versions(found.values(), "arxiv")}))
    found |= ax.found
    dead |= ax.missing
    failed |= ax.failed
    # the version of record an arXiv entry names may be listed nowhere else
    later = sorted(_versions(ax.found.values(), "doi") - set(dois))
    if later:
        more = _by_doi(http, later)
        found |= more[0]
        dead |= more[1]
        failed |= more[2]
        unlisted |= more[3]
    oa = openalex.works_by_doi(http, [d for d in (*dois, *later) if d not in have and d not in dead])
    return Registries(
        found=found,
        dead=frozenset(dead),
        failed=frozenset(failed | oa.failed),
        unlisted=frozenset(unlisted),
        openalex={**have, **oa.found},
    )
