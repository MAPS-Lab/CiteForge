"""Authoritative metadata for records that public scholarly APIs cannot resolve.

The catalog is a reviewed provider of last resort. It participates in the same
trust merge as network providers, so generated BibTeX remains derived output
and every correction has an explicit source outside the corpus.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .text_utils import extract_year_from_any, normalize_title

_CATALOG_PATH = Path(__file__).with_name("data") / "citation_corrections.json"
_ALLOWED_TYPES = frozenset({"article", "incollection", "inproceedings", "misc", "phdthesis"})


@dataclass(frozen=True)
class CitationCorrection:
    normalized_title: str
    year: int
    entry_type: str
    fields: dict[str, str]
    source: str


@lru_cache(maxsize=1)
def load_citation_corrections() -> tuple[CitationCorrection, ...]:
    """Load and strictly validate the bundled authoritative catalog."""
    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"version", "records"} or raw["version"] != 1:
        raise ValueError("citation correction catalog envelope is invalid")
    if not isinstance(raw["records"], list) or not raw["records"]:
        raise ValueError("citation correction catalog has no records")

    corrections: list[CitationCorrection] = []
    identities: set[tuple[str, int]] = set()
    for record in raw["records"]:
        if not isinstance(record, dict) or set(record) != {"match", "type", "fields", "source"}:
            raise ValueError("citation correction record is malformed")
        match = record["match"]
        fields = record["fields"]
        entry_type = record["type"]
        source = record["source"]
        if not isinstance(match, dict) or set(match) != {"title", "year"}:
            raise ValueError("citation correction match is malformed")
        normalized = normalize_title(str(match["title"]))
        year = extract_year_from_any(match["year"], fallback=0) or 0
        if not normalized or not year:
            raise ValueError("citation correction identity is incomplete")
        if entry_type not in _ALLOWED_TYPES:
            raise ValueError("citation correction type is unsupported")
        if not isinstance(fields, dict) or not fields or "note" in fields:
            raise ValueError("citation correction fields are invalid")
        if not all(isinstance(key, str) and isinstance(value, str) and value.strip() for key, value in fields.items()):
            raise ValueError("citation correction fields must be nonempty strings")
        if not isinstance(source, str) or not source.startswith("https://"):
            raise ValueError("citation correction source must be HTTPS")
        identity = (normalized, year)
        if identity in identities:
            raise ValueError("citation correction identity is duplicated")
        identities.add(identity)
        corrections.append(CitationCorrection(normalized, year, entry_type, dict(fields), source))
    return tuple(corrections)


def authoritative_correction(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Return the exact reviewed metadata matching an entry, if any."""
    fields = entry.get("fields") or {}
    identity = (
        normalize_title(str(fields.get("title") or "")),
        extract_year_from_any(fields.get("year"), fallback=0) or 0,
    )
    for correction in load_citation_corrections():
        if identity == (correction.normalized_title, correction.year):
            return {
                "type": correction.entry_type,
                "key": entry.get("key") or "Entry",
                "fields": dict(correction.fields),
                "source": correction.source,
            }
    return None
