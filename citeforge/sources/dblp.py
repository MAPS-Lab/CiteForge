from dataclasses import dataclass

from citeforge.http import HOSTS, Http, Result
from citeforge.model import Claim

HOST = HOSTS["dblp.org"]


@dataclass(frozen=True, slots=True)
class DblpPerson:
    names: tuple[str, ...]
    orcids: tuple[str, ...]
    claims: tuple[Claim, ...]


def person(http: Http, pid: str) -> Result[DblpPerson]:
    """Fetch /pid/{pid}.xml; each Claim carries a DBLP Record with ids from ee (CoRR maps to arxiv:)."""
    raise NotImplementedError
