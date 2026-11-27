from collections.abc import Sequence

from citeforge.http import HOSTS, Batch, Http

HOST = HOSTS["export.arxiv.org"]


def by_id(http: Http, ids: Sequence[str]) -> Batch:
    """Look up arxiv: ids with id_list, 100 per call, keyed by the same arxiv: ids."""
    raise NotImplementedError
