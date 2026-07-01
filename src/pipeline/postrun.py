from __future__ import annotations

import csv
import json
import os
import re
import time

from src import bibtex_utils as bt
from src.cache import get_cache_hit_counts
from src.canonicalize import (
    _fixup_bib_entry,
)
from src.config import (
    DEFAULT_A2I2_INPUT,
    SIM_MERGE_DUPLICATE_THRESHOLD,
    get_min_year,
)
from src.fsscan import iter_author_bibs, iter_output_dirs
from src.http_utils import get_api_call_counts
from src.io_utils import (
    build_a2i2_folder,
    collect_orphan_files,
    flush_summary_csv,
    reconcile_summary_csv,
    safe_write_file,
)
from src.log_utils import LogCategory, logger
from src.models import Record
from src.text_utils import (
    extract_year_from_any,
    title_similarity,
)

_FILENAME_YEAR_RE = re.compile(r"/[A-Za-z]+(\d{4})-")


def finalize_run(
    out_dir: str,
    records: list[Record],
    total_saved: int,
    processed: int,
    summary_csv_path: str | None,
) -> None:
    """Run the strict-ordered post-run finalization tail.

    Logs run stats, then (when the summary CSV exists) flushes it, reconciles
    phantom rows, removes duplicate orphans, deletes out-of-window files, applies
    the post-run fixup, builds the a2i2 folder, and rewrites baseline.json and
    badges.json. Order is load-bearing.
    """
    counts = get_api_call_counts()
    logger.step("Run complete", category=LogCategory.PLAN)
    logger.info(f"Records processed: {processed}", category=LogCategory.PLAN)
    logger.info(f"BibTeX files saved: {total_saved}", category=LogCategory.PLAN)
    if counts:
        logger.info(f"API calls: {counts}", category=LogCategory.PLAN)
    logger.info(f"Total API calls: {sum(counts.values()) if counts else 0}", category=LogCategory.PLAN)
    cache_counts = get_cache_hit_counts()
    logger.info(
        f"Cache: {cache_counts['positive']} positive, {cache_counts['negative']} negative, {cache_counts['miss']} miss",
        category=LogCategory.PLAN,
    )
    logger.info(f"Log file: {logger.log_file_path or 'n/a'}", category=LogCategory.PLAN)

    if summary_csv_path and os.path.exists(summary_csv_path):
        flush_summary_csv(summary_csv_path)

        # Remove phantom CSV entries
        phantoms = reconcile_summary_csv(summary_csv_path)
        if phantoms:
            logger.info(f"Reconciled summary CSV: removed {phantoms} phantom entries", category=LogCategory.CLEANUP)

        # Safe orphan removal (duplicates only)
        orphans = collect_orphan_files(summary_csv_path, out_dir)
        if orphans:
            csv_titles = _load_csv_titles(summary_csv_path)
            removed = 0
            for orphan in orphans:
                try:
                    with open(orphan, encoding="utf-8") as of:
                        orphan_entry = bt.parse_bibtex_to_dict(of.read())
                    orphan_title = (orphan_entry or {}).get("fields", {}).get("title", "")
                except (OSError, ValueError):
                    orphan_title = ""

                author_dir_path = os.path.dirname(orphan)
                tracked_titles = csv_titles.get(author_dir_path, [])
                is_dup = (
                    any(title_similarity(orphan_title, t) >= SIM_MERGE_DUPLICATE_THRESHOLD for t in tracked_titles)
                    if orphan_title
                    else False
                )

                if is_dup:
                    os.remove(orphan)
                    removed += 1
                    logger.info(
                        f"Removed duplicate orphan: {os.path.basename(orphan)}",
                        category=LogCategory.CLEANUP,
                    )
                else:
                    logger.warn(
                        f"Orphan kept (no duplicate found): {os.path.basename(orphan)}",
                        category=LogCategory.CLEANUP,
                    )
            if removed:
                logger.info(
                    f"Removed {removed}/{len(orphans)} orphan .bib files (duplicates only)",
                    category=LogCategory.CLEANUP,
                )

        # Remove .bib files outside the contribution window
        window_min = get_min_year()
        window_removed = 0
        for entry in os.listdir(out_dir):
            d = os.path.join(out_dir, entry)
            if not os.path.isdir(d) or entry == "a2i2":
                continue
            for fname in os.listdir(d):
                if not fname.endswith(".bib"):
                    continue
                fpath = os.path.join(d, fname)
                # Try filename year first
                m = _FILENAME_YEAR_RE.search(f"/{fname}")
                if m:
                    if int(m.group(1)) < window_min:
                        logger.debug(
                            f"YEAR_WINDOW | removing {fname} (year={m.group(1)} < {window_min})",
                            category=LogCategory.CLEANUP,
                        )
                        os.remove(fpath)
                        window_removed += 1
                    continue
                # Fallback: read BibTeX year field for non-standard filenames
                try:
                    with open(fpath, encoding="utf-8") as bf:
                        parsed = bt.parse_bibtex_to_dict(bf.read())
                    bib_year = extract_year_from_any((parsed or {}).get("fields", {}).get("year"), fallback=0) or 0
                    if 0 < bib_year < window_min:
                        logger.debug(
                            f"YEAR_WINDOW | removing {fname} (bib_year={bib_year} < {window_min})",
                            category=LogCategory.CLEANUP,
                        )
                        os.remove(fpath)
                        window_removed += 1
                except (OSError, ValueError):
                    pass
        if window_removed:
            logger.info(
                f"Removed {window_removed} out-of-window files (year < {window_min})",
                category=LogCategory.CLEANUP,
            )

        # Post-run fixup: apply entry type and field corrections to ALL .bib files
        # This catches orphans (files not processed during enrichment) and any
        # entries where Phase 4 corrections were undone by Tier 2 filling.
        postrun_fixed = 0
        for pr_entry_name in iter_output_dirs(out_dir):
            pr_dir = os.path.join(out_dir, pr_entry_name)
            if pr_entry_name == "a2i2":
                continue
            for pr_fname in sorted(os.listdir(pr_dir)):
                if not pr_fname.endswith(".bib"):
                    continue
                pr_fpath = os.path.join(pr_dir, pr_fname)
                try:
                    with open(pr_fpath, encoding="utf-8") as prf:
                        pr_content = prf.read()
                    pr_parsed = bt.parse_bibtex_to_dict(pr_content)
                    if pr_parsed and _fixup_bib_entry(pr_parsed):
                        bib_str = bt.bibtex_from_dict(pr_parsed)
                        if bib_str != pr_content:
                            safe_write_file(pr_fpath, bib_str)
                            postrun_fixed += 1
                except (OSError, ValueError):
                    pass
        if postrun_fixed:
            logger.info(
                f"Post-run fixup: corrected {postrun_fixed} .bib files",
                category=LogCategory.CLEANUP,
            )

        # Build a2i2 joint output folder
        a2i2_count = build_a2i2_folder(DEFAULT_A2I2_INPUT, records, out_dir)
        if a2i2_count:
            logger.info(
                f"Built a2i2 folder: {a2i2_count} deduplicated files",
                category=LogCategory.CLEANUP,
            )

        # Write per-author baseline counts
        baseline: dict[str, int] = {}
        for entry in iter_output_dirs(out_dir):
            d = os.path.join(out_dir, entry)
            baseline[entry] = len(iter_author_bibs(d))
        baseline_path = os.path.join(out_dir, "baseline.json")
        try:
            with open(baseline_path, "w", encoding="utf-8") as bf:
                json.dump({"total": sum(baseline.values()), "authors": baseline}, bf, indent=2)
        except OSError:
            pass

        # Write badge data for README workflow updates
        badges_path = os.path.join(out_dir, "badges.json")
        try:
            with open(badges_path, "w", encoding="utf-8") as bf:
                total = cache_counts["positive"] + cache_counts["negative"] + cache_counts["miss"]
                hit_rate = ((cache_counts["positive"] + cache_counts["negative"]) / total * 100) if total else 0
                json.dump(
                    {
                        "last_updated": time.strftime("%Y-%m"),
                        "cache_positive_hits": cache_counts["positive"],
                        "cache_negative_hits": cache_counts["negative"],
                        "cache_misses": cache_counts["miss"],
                        "total_queries": total,
                        "hit_rate": round(hit_rate, 1),
                    },
                    bf,
                    indent=2,
                )
        except OSError:
            pass

        logger.info(f"Summary CSV: {summary_csv_path}", category=LogCategory.PLAN)


def _load_csv_titles(csv_path: str) -> dict[str, list[str]]:
    """Load titles from CSV-tracked .bib files, grouped by author directory."""
    result: dict[str, list[str]] = {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                fp = row.get("file_path", "")
                abs_fp = os.path.abspath(fp)
                author_dir_path = os.path.dirname(abs_fp)
                try:
                    with open(abs_fp, encoding="utf-8") as bf:
                        entry = bt.parse_bibtex_to_dict(bf.read())
                    t = (entry or {}).get("fields", {}).get("title", "")
                    if t:
                        result.setdefault(author_dir_path, []).append(t)
                except (OSError, ValueError):
                    pass
    except (OSError, ValueError):
        pass
    return result
