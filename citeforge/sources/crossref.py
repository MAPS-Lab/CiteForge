from collections.abc import Mapping, Sequence
from typing import Any

from citeforge.http import HOSTS, Batch, Http, Result
from citeforge.model import Record

HOST = HOSTS["api.crossref.org"]


def by_doi(http: Http, ids: Sequence[str]) -> Batch:
    """Look up doi: ids with filter=doi:a,doi:b, 50 per call, keyed by the same doi: ids."""
    raise NotImplementedError


def search(http: Http, title: str, family: str, year: int | None) -> Result[tuple[Record, ...]]:
    """query.bibliographic plus query.author, rows=5, publication dates within one year of year."""
    raise NotImplementedError


def parse_work(item: Mapping[str, Any]) -> Record:
    """Map one Crossref work message to a Record; raises ParseError."""
    raise NotImplementedError
