"""Strict author-input census with stable normalized identities."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from citeforge.models import Record

from .types import TaskDisposition

_REQUIRED_COLUMNS = ("Name", "Scholar Link", "DBLP Link", "Enabled", "Exclusion Reason")


def _normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _scholar_id(link: str) -> str:
    if not link:
        return ""
    values = parse_qs(urlparse(link).query).get("user", [])
    return values[0].strip() if values else ""


def _dblp_id(link: str) -> str:
    if not link:
        return ""
    match = re.search(r"/pid/(.+?)(?:\.[a-z0-9]+)?$", link)
    return match.group(1) if match else link


def _parse_enabled(value: str, row_number: int) -> bool:
    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"row {row_number}: Enabled must be explicitly true or false")


@dataclass(frozen=True)
class AuthorCensusRow:
    """One validated physical input row."""

    physical_row: int
    row_key: str
    name: str
    normalized_name: str
    scholar_id: str
    dblp_id: str
    enabled: bool
    exclusion_reason: str
    disposition: TaskDisposition

    def as_record(self) -> Record:
        return Record(name=self.name, scholar_id=self.scholar_id, dblp=self.dblp_id)

    def canonical_content(self) -> dict[str, object]:
        return {
            "dblp_id": self.dblp_id,
            "disposition": self.disposition.value,
            "enabled": self.enabled,
            "exclusion_reason": self.exclusion_reason,
            "normalized_name": self.normalized_name,
            "row_key": self.row_key,
            "scholar_id": self.scholar_id,
        }


@dataclass(frozen=True)
class AuthorCensus:
    """A fully classified, immutable author census."""

    rows: tuple[AuthorCensusRow, ...]

    @property
    def total_count(self) -> int:
        return len(self.rows)

    @property
    def enabled_count(self) -> int:
        return sum(row.enabled for row in self.rows)

    @property
    def excluded_count(self) -> int:
        return sum(not row.enabled for row in self.rows)

    @property
    def invalid_count(self) -> int:
        return 0

    @property
    def enabled_rows(self) -> tuple[AuthorCensusRow, ...]:
        return tuple(row for row in self.rows if row.enabled)

    def canonical_content(self) -> list[dict[str, object]]:
        return [row.canonical_content() for row in sorted(self.rows, key=lambda item: item.row_key)]


def _row_key(normalized_name: str, scholar_id: str, dblp_id: str) -> str:
    identity = {"dblp_id": dblp_id, "normalized_name": normalized_name, "scholar_id": scholar_id}
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_census(path: Path) -> AuthorCensus:
    """Load and validate one disposition for every physical CSV data row."""
    with path.open(newline="", encoding="utf-8") as csvfile:
        reader = csv.reader(csvfile)
        try:
            fieldnames = next(reader)
        except StopIteration as exc:
            raise ValueError("census CSV is empty") from exc
        missing = [column for column in _REQUIRED_COLUMNS if column not in fieldnames]
        if missing:
            raise ValueError(f"missing required census columns: {', '.join(missing)}")

        rows: list[AuthorCensusRow] = []
        seen_identities: dict[tuple[str, str, str], int] = {}
        for row_number, values in enumerate(reader, start=2):
            if len(values) != len(fieldnames):
                raise ValueError(f"row {row_number}: expected {len(fieldnames)} columns, found {len(values)}")
            raw = dict(zip(fieldnames, values, strict=True))
            name = (raw.get("Name") or "").strip()
            normalized_name = _normalize_name(name)
            scholar_id = _scholar_id((raw.get("Scholar Link") or "").strip())
            dblp_id = _dblp_id((raw.get("DBLP Link") or "").strip())
            enabled = _parse_enabled(raw.get("Enabled") or "", row_number)
            exclusion_reason = (raw.get("Exclusion Reason") or "").strip()

            if not normalized_name:
                raise ValueError(f"row {row_number}: Name must not be empty")
            if enabled and not (scholar_id or dblp_id):
                raise ValueError(f"row {row_number}: enabled author requires a usable Scholar or DBLP identifier")
            if not enabled and not exclusion_reason:
                raise ValueError(f"row {row_number}: disabled author requires an Exclusion Reason")
            if enabled and exclusion_reason:
                raise ValueError(f"row {row_number}: enabled author cannot have an Exclusion Reason")

            identity = (normalized_name, scholar_id, dblp_id)
            if identity in seen_identities:
                raise ValueError(
                    f"row {row_number}: duplicate normalized identity first seen at row {seen_identities[identity]}"
                )
            seen_identities[identity] = row_number
            rows.append(
                AuthorCensusRow(
                    physical_row=row_number,
                    row_key=_row_key(*identity),
                    name=name,
                    normalized_name=normalized_name,
                    scholar_id=scholar_id,
                    dblp_id=dblp_id,
                    enabled=enabled,
                    exclusion_reason=exclusion_reason,
                    disposition=TaskDisposition.PENDING if enabled else TaskDisposition.NOT_APPLICABLE,
                )
            )

    return AuthorCensus(tuple(rows))
