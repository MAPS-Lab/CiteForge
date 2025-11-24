from __future__ import annotations

import csv
import json
import os
from typing import Any, Dict, List, Optional

from .config import DEFAULT_KEY_FILE, DEFAULT_S2_KEY_FILE, DEFAULT_INPUT, DEFAULT_OR_KEY_FILE, DEFAULT_GEMINI_KEY_FILE
from .exceptions import CSV_ERRORS, FILE_IO_ERRORS, JSON_ERRORS, FILE_READ_ERRORS
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


def _project_root() -> str:
    """
    Return the absolute path to the project root directory, inferred from the location of this module on disk.
    """
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _candidate_paths(primary: str, legacy: Optional[str] = None) -> List[str]:
    """
    Build an ordered list of file paths to try for a given name, including the
    original path, a project-root-relative variant, and an optional legacy
    filename, while removing duplicates.
    """
    candidates: List[str] = [primary]
    if not os.path.isabs(primary):
        candidates.append(os.path.join(_project_root(), primary))
    if legacy:
        candidates.append(legacy)
        if not os.path.isabs(legacy):
            candidates.append(os.path.join(_project_root(), legacy))
    # remove duplicates, keep order
    seen = set()
    uniq: List[str] = []
    for p in candidates:
        if p not in seen:
            uniq.append(p)
            seen.add(p)
    return uniq


def _read_key_file(
    path: str,
    legacy: Optional[str] = None,
    required: bool = True,
    expected_lines: int = 1
) -> Optional[List[str]]:
    """
    Generic key file reader that handles common patterns for loading API keys and
    credentials from configuration files with optional fallback to legacy filenames.
    """
    candidates = _candidate_paths(path, legacy)
    last_err: Optional[Exception] = None

    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
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


def read_semantic_api_key(path: str = DEFAULT_S2_KEY_FILE) -> Optional[str]:
    """
    Look for a Semantic Scholar API key in the usual locations and return it if
    present, or None when no key file is found.
    """
    lines = _read_key_file(path, legacy="Semantic.key", required=False, expected_lines=1)
    return lines[0] if lines else None


def read_openreview_credentials(path: str = DEFAULT_OR_KEY_FILE) -> Optional[tuple]:
    """
    Read OpenReview credentials from a small text file where the first non-empty
    line is the username and the second is the password, returning them as a
    tuple.
    """
    lines = _read_key_file(path, legacy=None, required=False, expected_lines=2)
    return (lines[0], lines[1]) if lines and len(lines) >= 2 else None


def read_gemini_api_key(path: str = DEFAULT_GEMINI_KEY_FILE) -> Optional[str]:
    """
    Look for a Gemini API key in the usual locations and return it if
    present, or None when no key file is found.
    """
    lines = _read_key_file(path, legacy="Gemini.key", required=False, expected_lines=1)
    return lines[0] if lines else None


def read_records(path: str = DEFAULT_INPUT) -> List[Record]:
    """
    Load author records from a CSV file, skip empty rows, and keep only entries
    with a Scholar ID so the main pipeline has usable input.
    """
    records: List[Record] = []
    candidates = _candidate_paths(path)
    for p in candidates:
        try:
            with open(p, newline="", encoding="utf-8") as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    # skip empty rows
                    if not any(row.values()):
                        continue
                    records.append(
                        Record(
                            name=(row.get("Name") or "").strip(),
                            email=(row.get("Email") or "").strip(),
                            scholar_id=(row.get("Scholar") or "").strip(),
                            orcid=(row.get("ORCID") or "").strip(),
                            dblp=(row.get("DBLP") or "").strip(),
                        )
                    )
            break
        except FileNotFoundError:
            continue
    else:
        raise FileNotFoundError(f"Input file not found (tried: {', '.join(candidates)})")

    # need Scholar IDs to do anything useful
    records = [r for r in records if r.scholar_id]
    if not records:
        raise ValueError("No valid records with Scholar author_id found in input file.")
    return records


def safe_read_file(path: str, encoding: str = "utf-8") -> Optional[str]:
    """
    Safely read a file and return its contents, returning None on error.
    """
    try:
        with open(path, "r", encoding=encoding) as f:
            return f.read()
    except FILE_READ_ERRORS:
        return None


def safe_read_json(path: str, default: Any = None) -> Any:
    """
    Safely read a JSON file and return its parsed contents, returning a default value on error.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
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


def safe_write_json(path: str, data: Any, makedirs: bool = True, indent: Optional[int] = 2) -> bool:
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


def init_summary_csv(csv_path: str, preserve_existing: bool = False) -> None:
    """
    Initialize the summary CSV file with proper headers, creating the parent directory if needed.

    If preserve_existing is True and the file already exists, it will be left intact.
    Otherwise, the file will be created or overwritten with just the header.
    """
    # Create parent directory if csv_path contains a directory component
    parent_dir = os.path.dirname(csv_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    # If preserving and file exists, leave it alone
    if preserve_existing and os.path.exists(csv_path):
        return

    with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=_SUMMARY_CSV_FIELDNAMES)
        writer.writeheader()


def _read_existing_summary(csv_path: str) -> Dict[str, Dict[str, Any]]:
    """
    Read existing summary CSV and return a dict mapping file_path to row data.
    """
    if not os.path.exists(csv_path):
        return {}

    existing = {}
    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                file_path = row.get("file_path")
                if file_path:
                    existing[file_path] = row
    except CSV_ERRORS:
        return {}

    return existing


def append_summary_to_csv(csv_path: str, file_path: str, trust_hits: int, flags: Dict[str, bool]) -> None:
    """
    Append a summary row to the CSV file with enrichment statistics for a single BibTeX entry.

    If an entry with the same file_path already exists in the CSV, it will be updated
    instead of creating a duplicate row.
    """
    # Read existing entries
    existing_entries = _read_existing_summary(csv_path)

    # Build new row
    new_row = {
        "file_path": file_path,
        "trust_hits": trust_hits,
        "scholar_bib": 1 if flags.get("scholar_bib", False) else 0,
        "scholar_page": 1 if flags.get("scholar_page", False) else 0,
        "s2": 1 if flags.get("s2", False) else 0,
        "crossref": 1 if flags.get("crossref", False) else 0,
        "openreview": 1 if flags.get("openreview", False) else 0,
        "arxiv": 1 if flags.get("arxiv", False) else 0,
        "openalex": 1 if flags.get("openalex", False) else 0,
        "pubmed": 1 if flags.get("pubmed", False) else 0,
        "europepmc": 1 if flags.get("europepmc", False) else 0,
        "doi_csl": 1 if flags.get("doi_csl", False) else 0,
        "doi_bibtex": 1 if flags.get("doi_bibtex", False) else 0,
    }

    # Update or add the entry
    existing_entries[file_path] = new_row

    # Rewrite entire CSV with updated data (ensures no duplicates)
    with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=_SUMMARY_CSV_FIELDNAMES)
        writer.writeheader()
        for row in existing_entries.values():
            writer.writerow(row)
