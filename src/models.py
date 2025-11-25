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
    scholar_id: str = ""  # Google Scholar author ID (optional)
    dblp: str = ""  # DBLP person ID (optional)
