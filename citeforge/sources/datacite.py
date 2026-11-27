from collections.abc import Sequence

from citeforge.http import HOSTS, Batch, Http

HOST = HOSTS["api.datacite.org"]


def by_doi(http: Http, ids: Sequence[str]) -> Batch:
    """Look up doi: ids with /dois?ids=a,b, 100 per call, keyed by the same doi: ids."""
    raise NotImplementedError
