from collections.abc import Sequence

from citeforge.http import HOSTS, Batch, Http, Result
from citeforge.model import Claim

HOST = HOSTS["api.openalex.org"]


def authors_by_orcid(http: Http, orcids: Sequence[str]) -> Result[dict[str, tuple[str, ...]]]:
    """Map each ORCID to the OpenAlex author ids that carry it, 50 ORCIDs per call."""
    raise NotImplementedError


def author(http: Http, aid: str) -> Result[tuple[str, str | None]]:
    """Return (current author id after any merge, ORCID or None) for one author id."""
    raise NotImplementedError


def list_works(http: Http, author_ids: Sequence[str], since_year: int) -> Result[tuple[Claim, ...]]:
    """Cursor-page every work of the authors from since_year; each Claim carries its Record."""
    raise NotImplementedError


def works_by_doi(http: Http, ids: Sequence[str]) -> Batch:
    """Look up doi: ids, 100 per call, keyed by the same doi: ids."""
    raise NotImplementedError
