from __future__ import annotations

import csv
import json
import os
import re
import threading
from typing import Any

from .config import DEFAULT_GEMINI_KEY_FILE, DEFAULT_INPUT, DEFAULT_KEY_FILE, DEFAULT_OR_KEY_FILE, DEFAULT_S2_KEY_FILE
from .exceptions import CSV_ERRORS, FILE_READ_ERRORS
from .models import Record

# CSV fieldnames for summary export
_SUMMARY_CSV_FIELDNAMES = [
    "file_path",
    "trust_hits",
    "scholar_bib",
    "scholar_page",
    "s2",
    "crossref",
    "openreview",
    "arxiv",
    "openalex",
    "pubmed",
    "europepmc",
    "doi_csl",
    "doi_bibtex",
]

# Lock for synchronizing CSV writes
_CSV_LOCK = threading.Lock()


def _project_root() -> str:
    """
    Return the absolute path to the project root directory, inferred from the location of this module on disk.
    """
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _candidate_paths(primary: str, legacy: str | None = None) -> list[str]:
    """
    Build an ordered list of file paths to try for a given name, including the
    original path, a project-root-relative variant, and an optional legacy
    filename, while removing duplicates.
    """
    candidates: list[str] = [primary]
    if not os.path.isabs(primary):
        candidates.append(os.path.join(_project_root(), primary))
    if legacy:
        candidates.append(legacy)
        if not os.path.isabs(legacy):
            candidates.append(os.path.join(_project_root(), legacy))
    return list(dict.fromkeys(candidates))


def _read_key_file(
    path: str,
    legacy: str | None = None,
    required: bool = True,
    expected_lines: int = 1
) -> list[str] | None:
    """
    Generic key file reader that handles common patterns for loading API keys and
    credentials from configuration files with optional fallback to legacy filenames.
    """
    candidates = _candidate_paths(path, legacy)
    last_err: Exception | None = None

    for p in candidates:
        try:
            with open(p, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
                if not lines:
                    last_err = ValueError(f"{os.path.basename(p)} is empty")
                    continue
                if len(lines) < expected_lines:
                    last_err = ValueError(
                        f"{os.path.basename(p)} has {len(lines)} line(s), expected {expected_lines}"
                    )
                    continue
                return lines
        except FileNotFoundError as e:
            last_err = e
            continue

    # File not found in any location
    if required:
        if last_err:
            raise last_err
        raise FileNotFoundError(f"Key file not found (tried: {', '.join(candidates)})")

    return None


def read_api_key(path: str = DEFAULT_KEY_FILE) -> str:
    """
    Load the SerpAPI key from a configuration file, trying the preferred path
    and an older legacy filename in a few common locations.
    """
    lines = _read_key_file(path, legacy="src.key", required=True, expected_lines=1)
    return lines[0] if lines else ""


def read_semantic_api_key(path: str = DEFAULT_S2_KEY_FILE) -> str | None:
    """
    Look for a Semantic Scholar API key in the usual locations and return it if
    present, or None when no key file is found.
    """
    lines = _read_key_file(path, legacy="Semantic.key", required=False, expected_lines=1)
    return lines[0] if lines else None


def read_openreview_credentials(path: str = DEFAULT_OR_KEY_FILE) -> tuple | None:
    """
    Read OpenReview credentials from a small text file where the first non-empty
    line is the username and the second is the password, returning them as a
    tuple.
    """
    lines = _read_key_file(path, legacy=None, required=False, expected_lines=2)
    return (lines[0], lines[1]) if lines and len(lines) >= 2 else None


def read_gemini_api_key(path: str = DEFAULT_GEMINI_KEY_FILE) -> str | None:
    """
    Look for a Gemini API key in the usual locations and return it if
    present, or None when no key file is found.
    """
    lines = _read_key_file(path, legacy="Gemini.key", required=False, expected_lines=1)
    return lines[0] if lines else None


def read_records(path: str = DEFAULT_INPUT) -> list[Record]:
    """
    Load author records from a CSV file, skip empty rows, and keep only entries
    with at least one valid identifier (Scholar or DBLP).
    """
    records: list[Record] = []
    candidates = _candidate_paths(path)
    for p in candidates:
        try:
            with open(p, newline="", encoding="utf-8") as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    # skip empty rows
                    if not any(row.values()):
                        continue

                    name = (row.get("Name") or "").strip()
                    scholar_link = (row.get("Scholar Link") or "").strip()
                    dblp_link = (row.get("DBLP Link") or "").strip()

                    # Extract Scholar ID
                    scholar_id = ""
                    if scholar_link:
                        m = re.search(r"user=([^&]+)", scholar_link)
                        if m:
                            scholar_id = m.group(1)

                    # Extract DBLP ID
                    dblp_id = ""
                    if dblp_link:
                        # Handle full URL or just ID if user provided that
                        if "/pid/" in dblp_link:
                            m = re.search(r"/pid/(.+?)(?:\.[a-z0-9]+)?$", dblp_link)
                            if m:
                                dblp_id = m.group(1)
                        else:
                            dblp_id = dblp_link

                    records.append(
                        Record(
                            name=name,
                            scholar_id=scholar_id,
                            dblp=dblp_id,
                        )
                    )
            break
        except FileNotFoundError:
            continue
    else:
        raise FileNotFoundError(f"Input file not found (tried: {', '.join(candidates)})")

    # need at least one ID to do anything useful
    records = [r for r in records if r.scholar_id or r.dblp]
    if not records:
        raise ValueError("No valid records with Scholar ID or DBLP ID found in input file.")
    return records


def safe_read_file(path: str, encoding: str = "utf-8") -> str | None:
    """
    Safely read a file and return its contents, returning None on error.
    """
    try:
        with open(path, encoding=encoding) as f:
            return f.read()
    except FILE_READ_ERRORS:
        return None


def safe_read_json(path: str, default: Any = None) -> Any:
    """
    Safely read a JSON file and return its parsed contents, returning a default value on error.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FILE_READ_ERRORS:
        return default


def safe_write_file(path: str, content: str, encoding: str = "utf-8", makedirs: bool = True) -> bool:
    """
    Safely write content to a file, optionally creating parent directories.
    """
    if makedirs:
        parent_dir = os.path.dirname(path)
        if parent_dir:
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except OSError:
                return False

    try:
        with open(path, "w", encoding=encoding) as f:
            f.write(content)
        return True
    except OSError:
        return False


def safe_write_json(path: str, data: Any, makedirs: bool = True, indent: int | None = 2) -> bool:
    """
    Safely write data to a JSON file, optionally creating parent directories.
    """
    if makedirs:
        parent_dir = os.path.dirname(path)
        if parent_dir:
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except OSError:
                return False

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
        return True
    except (OSError, TypeError):
        return False


# In-memory tracking for append-only CSV writes
_SUMMARY_KNOWN_PATHS: set[str] = set()
_SUMMARY_UPDATES: dict[str, dict[str, Any]] = {}
_SUMMARY_INITIALIZED = False


def init_summary_csv(csv_path: str, preserve_existing: bool = False) -> None:
    """
    Initialize the summary CSV file with proper headers, creating the parent directory if needed.
    Loads existing entries into memory for O(1) dedup on appends.
    """
    global _SUMMARY_INITIALIZED

    parent_dir = os.path.dirname(csv_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    with _CSV_LOCK:
        _SUMMARY_KNOWN_PATHS.clear()
        _SUMMARY_UPDATES.clear()

        if preserve_existing and os.path.exists(csv_path):
            # Load existing file_paths into the in-memory set
            try:
                with open(csv_path, newline="", encoding="utf-8") as csvfile:
                    reader = csv.DictReader(csvfile)
                    for row in reader:
                        fp = row.get("file_path")
                        if fp:
                            _SUMMARY_KNOWN_PATHS.add(fp)
            except CSV_ERRORS:
                pass
        else:
            with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=_SUMMARY_CSV_FIELDNAMES)
                writer.writeheader()

        _SUMMARY_INITIALIZED = True


def append_summary_to_csv(csv_path: str, file_path: str, trust_hits: int, flags: dict[str, bool]) -> None:
    """
    Append a summary row to the CSV file. New entries are appended in O(1).
    Updated entries (same file_path) are tracked in memory and flushed at end of run.
    Thread-safe via _CSV_LOCK.
    """
    flag_fields = [f for f in _SUMMARY_CSV_FIELDNAMES if f not in ("file_path", "trust_hits")]
    new_row: dict[str, Any] = {"file_path": file_path, "trust_hits": trust_hits}
    new_row.update({f: int(bool(flags.get(f))) for f in flag_fields})

    with _CSV_LOCK:
        if file_path in _SUMMARY_KNOWN_PATHS:
            # Track update for flush
            _SUMMARY_UPDATES[file_path] = new_row
        else:
            # True append — O(1)
            _SUMMARY_KNOWN_PATHS.add(file_path)
            try:
                with open(csv_path, "a", newline="", encoding="utf-8") as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=_SUMMARY_CSV_FIELDNAMES)
                    writer.writerow(new_row)
            except OSError:
                pass


def flush_summary_csv(csv_path: str) -> None:
    """
    Rewrite the summary CSV only if updates to existing entries occurred during the run.
    Called once at the end of main().
    """
    with _CSV_LOCK:
        if not _SUMMARY_UPDATES:
            return

        # Read current file, apply updates, rewrite
        existing: dict[str, dict[str, Any]] = {}
        try:
            with open(csv_path, newline="", encoding="utf-8") as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    fp = row.get("file_path")
                    if fp:
                        existing[fp] = dict(row)
        except CSV_ERRORS:
            return

        existing.update(_SUMMARY_UPDATES)
        _SUMMARY_UPDATES.clear()

        with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=_SUMMARY_CSV_FIELDNAMES)
            writer.writeheader()
            for row in existing.values():
                writer.writerow(row)
