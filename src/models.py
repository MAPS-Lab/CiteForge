from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Record:
    """
    Store a single author's contact details together with their identifiers on
    major academic platforms. This allows the rest of the pipeline to look up
    publications and metadata in a consistent way.
    """
    name: str
    email: str
    scholar_id: str  # Google Scholar author ID
    orcid: str  # ORCID identifier
    dblp: str  # DBLP person ID
