"""Strict author-input census with stable normalized identities."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote_plus, urlsplit

from citeforge.models import Record

from .types import TaskDisposition

_REQUIRED_COLUMNS = ("Name", "Scholar Link", "DBLP Link", "Enabled", "Exclusion Reason")
_SCHOLAR_HOSTS = frozenset({"scholar.google.com", "scholar.google.ca"})
_DBLP_HOSTS = frozenset({"dblp.org", "dblp.uni-trier.de"})
_DBLP_EXPORT_SUFFIXES = frozenset({"bib", "html", "nt", "rdf", "ris", "rss", "xml"})
_SCHOLAR_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,64}")
_DBLP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")


def _normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _has_unsafe_characters(value: str) -> bool:
    return any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)


def _validated_url(link: str, row_number: int, field: str) -> tuple[str, str, str]:
    if _has_unsafe_characters(link):
        raise ValueError(f"row {row_number}: {field} contains whitespace or control characters")
    try:
        parsed = urlsplit(link)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"row {row_number}: {field} is malformed") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ValueError(f"row {row_number}: {field} must be an uncredentialed HTTPS URL on the default port")
    if parsed.fragment:
        raise ValueError(f"row {row_number}: {field} must not contain a fragment")
    return parsed.hostname or "", parsed.path, parsed.query


def _scholar_id(link: str, row_number: int) -> str:
    if not link:
        return ""
    host, path, query = _validated_url(link, row_number, "Scholar Link")
    if host not in _SCHOLAR_HOSTS or path not in {"/citations", "/citations/"}:
        raise ValueError(f"row {row_number}: Scholar Link is not a supported Google Scholar citation profile")
    for raw_item in query.split("&"):
        raw_key, _, raw_value = raw_item.partition("=")
        if unquote_plus(raw_key) == "user" and ("%" in raw_key or "%" in raw_value):
            raise ValueError(f"row {row_number}: Scholar Link user key and ID must not be encoded")
    try:
        query_items = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: Scholar Link query is malformed") from exc
    user_values = [value for key, value in query_items if key == "user"]
    if len(user_values) != 1 or not _SCHOLAR_ID_RE.fullmatch(user_values[0]):
        raise ValueError(f"row {row_number}: Scholar Link requires exactly one safe user ID")
    return user_values[0]


def _dblp_id(link: str, row_number: int) -> str:
    if not link:
        return ""
    if _has_unsafe_characters(link):
        raise ValueError(f"row {row_number}: DBLP Link contains whitespace or control characters")
    identifier = link
    if "://" in link or link.startswith("//"):
        host, path, query = _validated_url(link, row_number, "DBLP Link")
        if host not in _DBLP_HOSTS or query:
            raise ValueError(f"row {row_number}: DBLP Link is not a supported DBLP profile URL")
        if not path.startswith("/pid/"):
            raise ValueError(f"row {row_number}: DBLP Link must use the /pid/ path")
        identifier = path.removeprefix("/pid/")
        stem, dot, suffix = identifier.rpartition(".")
        if dot:
            if suffix not in _DBLP_EXPORT_SUFFIXES:
                raise ValueError(f"row {row_number}: DBLP Link has an unsupported profile suffix")
            identifier = stem
    elif identifier.startswith("pid:"):
        identifier = identifier.removeprefix("pid:")
        if identifier.endswith((".html", ".xml")):
            raise ValueError(f"row {row_number}: bare DBLP Link person ID must not contain a profile suffix")
    if not _DBLP_ID_RE.fullmatch(identifier):
        raise ValueError(f"row {row_number}: DBLP Link does not contain a safe supported person ID")
    return identifier


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
        seen_provider_ids: dict[tuple[str, str], int] = {}
        for row_number, values in enumerate(reader, start=2):
            if len(values) != len(fieldnames):
                raise ValueError(f"row {row_number}: expected {len(fieldnames)} columns, found {len(values)}")
            raw = dict(zip(fieldnames, values, strict=True))
            name = (raw.get("Name") or "").strip()
            normalized_name = _normalize_name(name)
            scholar_id = _scholar_id((raw.get("Scholar Link") or "").strip(), row_number)
            dblp_id = _dblp_id((raw.get("DBLP Link") or "").strip(), row_number)
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
            # One authoritative provider profile cannot safely identify two census rows,
            # even when their display names or other provider identifiers differ.
            for provider, provider_id in (("Scholar", scholar_id), ("DBLP", dblp_id)):
                provider_key = (provider, provider_id)
                if provider_id and provider_key in seen_provider_ids:
                    raise ValueError(
                        f"row {row_number}: duplicate {provider} provider identity first seen at "
                        f"row {seen_provider_ids[provider_key]}"
                    )
                if provider_id:
                    seen_provider_ids[provider_key] = row_number
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
