"""Shared data models.

Defines the small data structures passed across the pipeline, currently the
author `Record` read from the input CSV.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Record:
    """One author row from the input CSV, with platform identifiers used by
    the pipeline to look up publications and metadata."""

    name: str
    scholar_id: str = ""  # Google Scholar author ID (optional)
    dblp: str = ""  # DBLP person ID (optional)
