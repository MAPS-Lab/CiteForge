"""Command-line interface for CiteForge."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from citeforge.config import (
    DEFAULT_GEMINI_KEY_FILE,
    DEFAULT_INPUT,
    DEFAULT_OR_KEY_FILE,
    DEFAULT_OUT_DIR,
    DEFAULT_S2_KEY_FILE,
    DEFAULT_SERPAPI_KEY_FILE,
    DEFAULT_SERPLY_KEY_FILE,
)
from citeforge.exceptions import FILE_IO_ERRORS, FILE_READ_ERRORS
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

T = TypeVar("T")


def _path_from_cwd(path: Path) -> Path:
    """Return an absolute path, interpreting relative paths from the invocation CWD."""
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI options and resolve all data paths from the current directory."""
    parser = argparse.ArgumentParser(description="Enrich author BibTeX records from scholarly APIs.")
    parser.add_argument("--force", action="store_true", help="Re-enrich records even when their cache is complete.")
    parser.add_argument(
        "--input", type=Path, default=Path(DEFAULT_INPUT), help="Author CSV file (default: data/input.csv)."
    )
    parser.add_argument(
        "--output", type=Path, default=Path(DEFAULT_OUT_DIR), help="Output directory (default: output)."
    )
    args = parser.parse_args(argv)
    args.input = _path_from_cwd(args.input)
    args.output = _path_from_cwd(args.output)
    return args


def _load_optional_key(reader: Callable[[], T], label: str, miss_note: str) -> T:
    """Load an optional credential, logging success or the degradation on a miss."""
    value = reader()
    if value:
        logger.success(f"{label} loaded", category=LogCategory.PLAN)
    else:
        logger.warn(f"{label} not found; {miss_note}", category=LogCategory.PLAN)
    return value


def run_pipeline(input_path: Path, output_path: Path, force_enrich: bool) -> int:
    """Load credentials and records, run enrichment, then finalize the result."""
    out_dir = str(output_path)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        logger.error(f"Cannot create output directory '{out_dir}': {e}", category=LogCategory.ERROR)
        return 2

    logger.set_log_file(os.path.join(out_dir, "run.log"))
    reset_api_call_counts()
    logger.step("CiteForge run started", category=LogCategory.PLAN)

    serpapi_key = read_serpapi_api_key(str(_path_from_cwd(Path(DEFAULT_SERPAPI_KEY_FILE))))
    if not serpapi_key:
        logger.error("SerpAPI key not found; cannot fetch author publications", category=LogCategory.PLAN)
        logger.close()
        return 2
    logger.success("SerpAPI key loaded", category=LogCategory.PLAN)

    serply_key = _load_optional_key(
        lambda: read_serply_api_key(str(_path_from_cwd(Path(DEFAULT_SERPLY_KEY_FILE)))),
        "Serply API key",
        "Scholar citation detail will be skipped",
    )
    s2_api_key = _load_optional_key(
        lambda: read_semantic_api_key(str(_path_from_cwd(Path(DEFAULT_S2_KEY_FILE)))),
        "Semantic Scholar key",
        "S2 enrichment disabled",
    )
    or_creds = _load_optional_key(
        lambda: read_openreview_credentials(str(_path_from_cwd(Path(DEFAULT_OR_KEY_FILE)))),
        "OpenReview credentials",
        "OpenReview enrichment may be limited",
    )
    gemini_api_key = _load_optional_key(
        lambda: read_gemini_api_key(str(_path_from_cwd(Path(DEFAULT_GEMINI_KEY_FILE)))),
        "Gemini API key",
        "short titles will use fallback algorithm",
    )

    try:
        records = read_records(str(input_path))
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run CiteForge with command-line options."""
    args = parse_args(argv)
    return run_pipeline(args.input, args.output, args.force)
