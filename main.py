"""CiteForge command-line entry point.

Loads the API keys and author records from ``data/input.csv``, runs the
parallel enrichment scheduler over every author, then finalizes the run.
Accepts a ``--force`` flag that re-enriches every record regardless of cache
completeness.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import TypeVar

from citeforge.canonicalize import _fixup_bib_entry  # noqa: F401  # re-exported for test imports
from citeforge.config import (
    DEFAULT_INPUT,
    DEFAULT_OUT_DIR,
    DEFAULT_S2_KEY_FILE,
    DEFAULT_SERPAPI_KEY_FILE,
    DEFAULT_SERPLY_KEY_FILE,
)
from citeforge.exceptions import (
    FILE_IO_ERRORS,
    FILE_READ_ERRORS,
)
from citeforge.http_utils import reset_api_call_counts
from citeforge.io_utils import (
    init_summary_csv,
    read_gemini_api_key,
    read_openreview_credentials,
    read_records,
    read_semantic_api_key,
    read_serpapi_api_key,
    read_serply_api_key,
)
from citeforge.log_utils import LogCategory, logger
from citeforge.pipeline.postrun import finalize_run
from citeforge.pipeline.scheduler import prioritize_records, run_all
from citeforge.textnorm import _is_corrupted_title, _is_garbage_title  # noqa: F401  # re-exported for test imports

T = TypeVar("T")


def _load_optional_key(reader: Callable[[], T], label: str, miss_note: str) -> T:
    """Load an optional credential, logging success or the degradation on a miss."""
    value = reader()
    if value:
        logger.success(f"{label} loaded", category=LogCategory.PLAN)
    else:
        logger.warn(f"{label} not found; {miss_note}", category=LogCategory.PLAN)
    return value


def main() -> int:
    """Set up the run, load API keys and author records, and process all authors in parallel.

    Returns an exit code suitable for use as a command-line entry point.
    """
    force_enrich = "--force" in sys.argv[1:]
    out_dir = os.path.join(os.path.dirname(__file__), DEFAULT_OUT_DIR)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        logger.error(f"Cannot create output directory '{out_dir}': {e}", category=LogCategory.ERROR)
        return 2

    # Set main thread log file
    logger.set_log_file(os.path.join(out_dir, "run.log"))
    reset_api_call_counts()
    logger.step("CiteForge run started", category=LogCategory.PLAN)

    serpapi_key = read_serpapi_api_key(DEFAULT_SERPAPI_KEY_FILE)
    if not serpapi_key:
        logger.error("SerpAPI key not found; cannot fetch author publications", category=LogCategory.PLAN)
        logger.close()
        return 2
    logger.success("SerpAPI key loaded", category=LogCategory.PLAN)

    serply_key = _load_optional_key(
        lambda: read_serply_api_key(DEFAULT_SERPLY_KEY_FILE),
        "Serply API key",
        "Scholar citation detail will be skipped",
    )
    s2_api_key = _load_optional_key(
        lambda: read_semantic_api_key(DEFAULT_S2_KEY_FILE),
        "Semantic Scholar key",
        "S2 enrichment disabled",
    )
    or_creds = _load_optional_key(
        read_openreview_credentials,
        "OpenReview credentials",
        "OpenReview enrichment may be limited",
    )
    gemini_api_key = _load_optional_key(
        read_gemini_api_key,
        "Gemini API key",
        "short titles will use fallback algorithm",
    )

    try:
        records = read_records(DEFAULT_INPUT)
        logger.success(f"Input loaded: {len(records)} record(s)", category=LogCategory.PLAN)
    except FILE_READ_ERRORS as e:
        logger.error(f"Error reading input file: {e}", category=LogCategory.ERROR)
        logger.close()
        return 2

    records = prioritize_records(records, out_dir)

    csv_path = os.path.join(out_dir, "summary.csv")
    summary_csv_path: str | None = csv_path
    try:
        init_summary_csv(csv_path, preserve_existing=True)
        logger.success(f"Summary CSV initialized: {csv_path}", category=LogCategory.PLAN)
    except FILE_IO_ERRORS as e:
        logger.warn(f"Could not initialize summary CSV: {e}", category=LogCategory.ERROR)
        summary_csv_path = None

    total_saved, processed = run_all(
        serpapi_key,
        serply_key,
        s2_api_key,
        or_creds,
        gemini_api_key,
        records,
        out_dir,
        summary_csv_path,
        force_enrich,
    )

    try:
        finalize_run(out_dir, records, total_saved, processed, summary_csv_path)
    finally:
        logger.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
