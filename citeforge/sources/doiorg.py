from collections.abc import Sequence

from citeforge.http import HOSTS, Http, Result
from citeforge.model import Record

HOST = HOSTS["doi.org"]


def agency(http: Http, ids: Sequence[str]) -> Result[dict[str, str | None]]:
    """Map doi: ids to their registration agency name, 20 per call; None means the DOI does not exist."""
    raise NotImplementedError


def csl(http: Http, work_id: str) -> Result[Record]:
    """Fetch CSL JSON for a doi: id, following at most 3 redirects by hand through Http.get(url=...)."""
    raise NotImplementedError
