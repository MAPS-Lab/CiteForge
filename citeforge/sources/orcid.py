from dataclasses import dataclass

from citeforge.http import HOSTS, Http, Result
from citeforge.model import Claim

HOST = HOSTS["pub.orcid.org"]


@dataclass(frozen=True, slots=True)
class Affiliation:
    ror: str | None
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class OrcidRecord:
    given: str
    family: str
    credit: str | None
    other_names: tuple[str, ...]
    affiliations: tuple[Affiliation, ...]
    works: tuple[Claim, ...]


def record(http: Http, orcid: str) -> Result[OrcidRecord]:
    """Fetch /v3.0/{orcid}/record; Claim.by_self is True when the work summary source is the iD itself."""
    raise NotImplementedError
