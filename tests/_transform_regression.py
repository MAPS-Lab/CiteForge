"""One-shot transformation of test_regression.py.

This script:
1. Removes the local _extract_bibtex_field definition
2. Adds extract_bibtex_field import from conftest
3. Replaces all _extract_bibtex_field( calls with extract_bibtex_field(
4. Hoists inline imports to module level
5. Removes dead test classes that reference non-existent functions
6. Adds missing imports for PREPRINT_SERVERS and _is_corrupted_title
7. Removes `import re` (only used by the removed _extract_bibtex_field)
"""

import re
import sys
from pathlib import Path

REGRESSION_FILE = Path(__file__).parent / "test_regression.py"

# Classes to remove entirely (test non-existent functions)
DEAD_CLASSES = {
    "TestGarbageTitleFilter",
    "TestGarbageTitleNonPaper",
    "TestStaleFileValidation",
    "TestReconcileSummaryCSV",
    "TestCollectOrphanFiles",
    "TestIsKnownSummaryPath",
}

# Inline imports to hoist to module level (deduped)
# These will be added to the import block and removed from function bodies
INLINE_IMPORTS_TO_HOIST = {
    "from src.api_generics import _resolve_dotted",
    "from src.api_generics import _resolve_dotted_str",
    "from src.bibtex_build import determine_entry_type",
    "from src.bibtex_utils import bibtex_from_dict",
    "from src.bibtex_utils import parse_bibtex_to_dict",
    "from src.clients.search_apis import bibtex_from_csl",
    "from src.clients.utility_apis import gemini_generate_short_title",
    "from src.clients.utility_apis import orcid_fetch_works",
    "from src.config import ABBREVIATED_VENUE_MAP",
    "from src.config import PREPRINT_SERVERS",
    "from src.http_utils import TokenBucketRateLimiter",
    "from src.http_utils import _THREAD_LOCAL",
    "from src.http_utils import _THREAD_LOCAL, _http_request",
    "from src.http_utils import _get_rate_limiter",
    "from src.http_utils import _http_request",
    "from src.http_utils import http_post_json",
    "from src.io_utils import read_records",
    "from src.merge_utils import merge_with_policy",
    "from src.merge_utils import save_entry_to_file",
    "from src.text_utils import author_name_matches",
    "from src.text_utils import author_overlap_ratio",
}


def main() -> None:
    content = REGRESSION_FILE.read_text(encoding="utf-8")
    lines = content.split("\n")

    # Step 1: Remove the _extract_bibtex_field function definition
    new_lines: list[str] = []
    i = 0
    while i < len(lines):
        # Skip the _extract_bibtex_field definition block
        if lines[i].startswith("def _extract_bibtex_field("):
            # Skip until next blank line followed by non-indented content
            i += 1
            while i < len(lines):
                if lines[i] == "" and (i + 1 >= len(lines) or not lines[i + 1].startswith(" ")):
                    i += 1  # skip the blank line too
                    break
                i += 1
            continue
        new_lines.append(lines[i])
        i += 1

    lines = new_lines

    # Step 2: Remove dead test classes
    new_lines = []
    i = 0
    while i < len(lines):
        # Check if this line starts a dead class
        stripped = lines[i].strip()
        is_dead_class = False
        for cls_name in DEAD_CLASSES:
            if stripped.startswith(f"class {cls_name}"):
                is_dead_class = True
                break

        if is_dead_class:
            # Skip until the next class definition or end of file
            i += 1
            while i < len(lines):
                s = lines[i].strip()
                # A new class at module level means end of dead class
                if s.startswith("class ") and not lines[i].startswith(" "):
                    break
                i += 1
            # Don't skip the blank lines before next class; just continue
            continue

        new_lines.append(lines[i])
        i += 1

    lines = new_lines

    # Step 3: Remove inline imports (lines that match hoisted patterns)
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped in INLINE_IMPORTS_TO_HOIST:
            continue
        # Also handle multi-line inline imports like "from src.io_utils import ("
        # These are already in dead classes we removed, so skip
        new_lines.append(line)

    lines = new_lines

    # Step 4: Clean up consecutive blank lines (max 2)
    new_lines = []
    blank_count = 0
    for line in lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 2:
                new_lines.append(line)
        else:
            blank_count = 0
            new_lines.append(line)

    lines = new_lines

    # Step 5: Replace _extract_bibtex_field with extract_bibtex_field
    content = "\n".join(lines)
    content = content.replace("_extract_bibtex_field(", "extract_bibtex_field(")

    # Step 6: Replace the import block
    # Current imports end right before the first class
    old_import_section = '''from __future__ import annotations

import os
import re
import time
from typing import Any
from unittest.mock import MagicMock, patch

from src import bibtex_utils as bt
from src import id_utils, merge_utils, text_utils
from src.api_generics import APISearchConfig, search_api_generic_multiple
from src.clients.scholar import _deduplicate_publication_list
from src.config import (
    MIN_TITLE_WORDS,
    OPENREVIEW_SESSION_TTL_SECS,
    PAGES_MAX_DIGITS,
    SIM_MERGE_DUPLICATE_THRESHOLD,
)
from src.doi_utils import validate_doi_candidate'''

    new_import_section = '''from __future__ import annotations

import csv
import os
import time
from typing import Any
from unittest.mock import MagicMock, patch

from src import bibtex_utils as bt
from src import id_utils, merge_utils, text_utils
from src.api_generics import (
    APISearchConfig,
    _resolve_dotted,
    _resolve_dotted_str,
    search_api_generic_multiple,
)
from src.bibtex_build import determine_entry_type
from src.bibtex_utils import bibtex_from_dict, parse_bibtex_to_dict
from src.clients.scholar import _deduplicate_publication_list
from src.clients.search_apis import bibtex_from_csl
from src.clients.utility_apis import gemini_generate_short_title, orcid_fetch_works
from src.config import (
    ABBREVIATED_VENUE_MAP,
    MIN_TITLE_WORDS,
    OPENREVIEW_SESSION_TTL_SECS,
    PAGES_MAX_DIGITS,
    PREPRINT_SERVERS,
    SIM_MERGE_DUPLICATE_THRESHOLD,
)
from src.doi_utils import validate_doi_candidate
from src.http_utils import (
    TokenBucketRateLimiter,
    _THREAD_LOCAL,
    _get_rate_limiter,
    _http_request,
    http_post_json,
)
from src.io_utils import read_records
from src.merge_utils import merge_with_policy, save_entry_to_file
from src.text_utils import author_name_matches, author_overlap_ratio
from tests.conftest import extract_bibtex_field'''

    if old_import_section not in content:
        print("ERROR: Could not find the old import section. Aborting.", file=sys.stderr)
        sys.exit(1)

    content = content.replace(old_import_section, new_import_section)

    # Step 7: Ensure file ends with single newline
    content = content.rstrip("\n") + "\n"

    REGRESSION_FILE.write_text(content, encoding="utf-8")
    print(f"Transformed {REGRESSION_FILE}")
    print(f"Dead classes removed: {', '.join(sorted(DEAD_CLASSES))}")


if __name__ == "__main__":
    main()
