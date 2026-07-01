from __future__ import annotations

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from src.clients.helpers import get_article_year, strip_html_tags
from src.clients.scholar import (
    fetch_author_publications,
    merge_publication_lists,
    sort_articles_by_year_current_first,
)
from src.clients.search_apis import (
    dblp_fetch_for_author,
)
from src.config import (
    MAX_PUBLICATIONS_PER_AUTHOR,
    MAX_WORKERS,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    SIM_MERGE_DUPLICATE_THRESHOLD,
    get_min_year,
)
from src.exceptions import (
    FULL_OPERATION_ERRORS,
)
from src.fsscan import iter_author_bibs
from src.log_utils import LogCategory, LogSource, logger
from src.models import Record
from src.pipeline.article import process_article
from src.text_utils import (
    format_author_dirname,
    trim_title_default,
)


def process_record(
    serpapi_key: str,
    serply_key: str | None,
    rec: Record,
    out_dir: str,
    max_pubs: int | None = 1,
    s2_api_key: str | None = None,
    or_creds: tuple[str, str] | None = None,
    delay: float = 0.0,
    gemini_api_key: str | None = None,
    summary_csv_path: str | None = None,
    force_enrich: bool = False,
) -> int:
    """Fetch, deduplicate, and enrich recent publications for one author.

    Returns the number of BibTeX files successfully written.
    """
    # Setup thread-local logging for this author
    effective_id = rec.scholar_id or rec.dblp or ""
    author_dirname = format_author_dirname(rec.name, effective_id)
    author_log_path = os.path.join(out_dir, author_dirname, "author.log")

    logger.set_log_file(author_log_path)

    try:
        logger.step(
            f"Author: {rec.name} (Scholar={rec.scholar_id or 'N/A'}, DBLP={rec.dblp or 'N/A'})",
            category=LogCategory.AUTHOR,
            source=LogSource.SYSTEM,
        )

        min_year = get_min_year()

        scholar_windowed = []
        if rec.scholar_id:
            logger.info("Request author publications", category=LogCategory.FETCH, source=LogSource.SCHOLAR)

            scholar_articles: list[dict[str, Any]] = []
            max_fetch_retries = 3

            # SerpAPI call — pagination handled internally by serpapi_scholar
            data = {}
            for attempt in range(1, max_fetch_retries + 1):
                data = fetch_author_publications(
                    serpapi_key,
                    rec.scholar_id,
                    rec.name,
                    num=MAX_PUBLICATIONS_PER_AUTHOR,
                    min_year=min_year,
                )
                if data.get("articles"):
                    break  # Got articles -- valid response
                if attempt < max_fetch_retries:
                    logger.warn(
                        f"Scholar API returned empty (attempt {attempt}/{max_fetch_retries}), retrying...",
                        category=LogCategory.FETCH,
                        source=LogSource.SCHOLAR,
                    )
                    time.sleep(2.0 * attempt)

            if not data.get("articles"):
                logger.warn(
                    f"Scholar API failed after {max_fetch_retries} attempts; continuing with DBLP only",
                    category=LogCategory.ERROR,
                    source=LogSource.SCHOLAR,
                )
            else:
                status = (data.get("search_metadata") or {}).get("status", "")
                if status.lower() == "error":
                    raise RuntimeError(
                        f"CiteForge error for author {rec.scholar_id}: {data.get('error') or 'Unknown error'}"
                    )

                scholar_articles = data.get("articles", [])
                logger.debug(
                    f"SCHOLAR_FETCH | articles={len(scholar_articles)}",
                    category=LogCategory.AUDIT,
                )

            if not scholar_articles:
                logger.warn("No articles returned from Scholar", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
            else:
                # Pre-clean titles to handle trailing periods consistently
                for a in scholar_articles:
                    try:
                        if a.get("title"):
                            a["title"] = trim_title_default(strip_html_tags(a["title"]))
                    except (TypeError, AttributeError):
                        pass
                logger.info(
                    f"{len(scholar_articles)} article(s) fetched",
                    category=LogCategory.FETCH,
                    source=LogSource.SCHOLAR,
                )

            scholar_windowed = [a for a in scholar_articles if (get_article_year(a) or 0) >= min_year]
            logger.debug(
                f"YEAR_WINDOW | total={len(scholar_articles)} | windowed={len(scholar_windowed)} | min_year={min_year}",
                category=LogCategory.AUDIT,
            )
            logger.info(
                f"{len(scholar_windowed)}/{len(scholar_articles)} within year window (>= {min_year})",
                category=LogCategory.FETCH,
                source=LogSource.SCHOLAR,
            )
        else:
            logger.info("Skipped (no ID)", category=LogCategory.SKIP, source=LogSource.SCHOLAR)

        dblp_items = []
        if rec.dblp:
            try:
                dblp_items = dblp_fetch_for_author(rec.name, rec.dblp, min_year)
                logger.info(
                    f"{len(dblp_items)} item(s) fetched within window",
                    category=LogCategory.FETCH,
                    source=LogSource.DBLP,
                )
            except FULL_OPERATION_ERRORS as e:
                logger.warn(f"Fetch failed: {e}", category=LogCategory.ERROR, source=LogSource.DBLP)
        else:
            logger.info("Skipped (no ID)", category=LogCategory.SKIP, source=LogSource.DBLP)

        if not scholar_windowed and not dblp_items:
            logger.info(f"No articles within year window (>= {min_year})", category=LogCategory.SKIP)
            return 0

        # merge Scholar and DBLP with full deduplication (within and across sources)
        merged_list = merge_publication_lists(scholar_windowed, dblp_items, target_author=rec.name)
        dedup_removed = len(scholar_windowed) + len(dblp_items) - len(merged_list)
        logger.debug(
            f"PUB_MERGE | scholar={len(scholar_windowed)} | dblp={len(dblp_items)} "
            f"| merged={len(merged_list)} | dedup_removed={dedup_removed}",
            category=LogCategory.AUDIT,
        )
        logger.info(
            f"Union: Scholar={len(scholar_windowed)}, DBLP={len(dblp_items)} "
            f"→ {len(merged_list)} unique publications (threshold={SIM_MERGE_DUPLICATE_THRESHOLD})",
            category=LogCategory.PLAN,
        )

        articles_sorted = sort_articles_by_year_current_first(merged_list)
        total_entries = len(articles_sorted) if max_pubs is None else min(len(articles_sorted), max_pubs)
        logger.info(
            f"Plan: process {total_entries}/{len(articles_sorted)} item(s) "
            f"(limit={'all' if max_pubs is None else max_pubs})",
            category=LogCategory.PLAN,
        )

        saved = 0
        for idx, art in enumerate(articles_sorted):
            if max_pubs is not None and idx >= max_pubs:
                break
            try:
                saved += process_article(
                    rec,
                    art,
                    serply_key,
                    out_dir,
                    s2_api_key,
                    or_creds,
                    idx=idx + 1,
                    total=total_entries,
                    gemini_api_key=gemini_api_key,
                    summary_csv_path=summary_csv_path,
                    min_year=min_year,
                    force_enrich=force_enrich,
                )
            except FULL_OPERATION_ERRORS as e:
                logger.error(f"Article error: {e}", category=LogCategory.ERROR)
            if delay > 0:
                jittered = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
                time.sleep(jittered)
        logger.info(f"Author done: saved {saved} file(s)", category=LogCategory.PLAN)
        return saved
    finally:
        # Close the thread-local log file handler
        logger.close()


def count_existing_papers(rec: Record, out_dir: str) -> int:
    """Count existing .bib files in the author's output directory."""
    effective_id = rec.scholar_id or rec.dblp or ""
    author_dirname = format_author_dirname(rec.name, effective_id)
    author_dir = os.path.join(out_dir, author_dirname)
    try:
        return len(iter_author_bibs(author_dir))
    except OSError:
        return 0


def prioritize_records(records: list[Record], out_dir: str) -> list[Record]:
    """Sort authors by existing paper count (descending) so authors with more
    papers finish first; ties break on (name, id) for deterministic ordering.

    Emits the PLAN log lines and returns the count-sorted list. Kept separate
    from run_all so the caller can log/sort before initializing the summary CSV,
    matching the original run.log line ordering.
    """
    logger.info(
        "Sorting authors by existing paper count (authors with more papers will be processed first)",
        category=LogCategory.PLAN,
    )
    records_with_counts = [(rec, count_existing_papers(rec, out_dir)) for rec in records]
    records_with_counts.sort(key=lambda x: (-x[1], x[0].name.lower(), x[0].scholar_id or x[0].dblp or ""))

    # Log sorting results
    if records_with_counts:
        max_papers = records_with_counts[0][1]
        min_papers = records_with_counts[-1][1]
        logger.info(f"Author range: {max_papers} papers (max) to {min_papers} papers (min)", category=LogCategory.PLAN)

    return [rec for rec, _ in records_with_counts]


def run_all(
    serpapi_key: str,
    serply_key: str | None,
    s2_api_key: str | None,
    or_creds: tuple[str, str] | None,
    gemini_api_key: str | None,
    records: list[Record],
    out_dir: str,
    summary_csv_path: str | None,
    force_enrich: bool,
) -> tuple[int, int]:
    """Enrich every (already count-sorted) author in parallel worker threads.

    Prioritizes authors without an output directory, installs a thread
    excepthook, and drives a ThreadPoolExecutor over process_record. Expects
    ``records`` to be the count-sorted list from prioritize_records. Returns
    (total_saved, processed).
    """
    total_saved = 0
    processed = 0

    # Prioritize new authors (no existing output dir) so they get browser/API
    # resources first, before cached authors consume worker slots
    def _has_output(r: Record) -> bool:
        eid = r.scholar_id or r.dblp or ""
        return os.path.isdir(os.path.join(out_dir, format_author_dirname(r.name, eid)))

    records_sorted = [r for _, r in sorted(enumerate(records), key=lambda ir: (_has_output(ir[1]), ir[0]))]

    logger.step(f"Starting parallel execution with {MAX_WORKERS} workers", category=LogCategory.PLAN)

    # Install thread exception hook to log uncaught exceptions in worker threads
    _orig_excepthook = threading.excepthook

    def _thread_excepthook(args: Any) -> None:
        logger.error(
            f"Thread '{args.thread.name if args.thread else '?'}' died: {args.exc_type.__name__}: {args.exc_value}",
            category=LogCategory.ERROR,
        )
        _orig_excepthook(args)

    threading.excepthook = _thread_excepthook

    # Per-author timeout: 30 minutes per author to handle large publication lists
    # Each article takes ~60-90s across all API calls, so 24 articles ≈ 36 minutes
    author_timeout = 1800  # seconds

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all tasks and track them
        future_to_author = {}
        for idx, rec in enumerate(records_sorted, 1):
            effective_id = rec.scholar_id or rec.dblp or "N/A"
            logger.info(f"[{idx}/{len(records)}] Queued: {rec.name} (ID: {effective_id})", category=LogCategory.PLAN)

            future = executor.submit(
                process_record,
                serpapi_key,
                serply_key,
                rec,
                out_dir,
                max_pubs=None,
                s2_api_key=s2_api_key,
                or_creds=or_creds,
                delay=REQUEST_DELAY_MIN,
                gemini_api_key=gemini_api_key,
                summary_csv_path=summary_csv_path,
                force_enrich=force_enrich,
            )
            future_to_author[future] = rec

        logger.step(f"All {len(records)} authors queued for processing", category=LogCategory.PLAN)

        try:
            for future in as_completed(future_to_author, timeout=author_timeout * len(records)):
                rec = future_to_author[future]
                try:
                    saved = future.result(timeout=30)
                    total_saved += saved
                    processed += 1
                    logger.success(
                        f"[{processed}/{len(records)}] Completed: {rec.name} ({saved} files saved)",
                        category=LogCategory.AUTHOR,
                    )
                except TimeoutError:
                    processed += 1
                    logger.error(
                        f"[{processed}/{len(records)}] Timeout retrieving result for {rec.name}",
                        category=LogCategory.ERROR,
                    )
                except Exception as e:
                    processed += 1
                    logger.error(
                        f"[{processed}/{len(records)}] Error processing {rec.name} ({rec.scholar_id or rec.dblp}): {e}",
                        category=LogCategory.ERROR,
                    )
        except TimeoutError:
            remaining = [r.name for f, r in future_to_author.items() if not f.done()]
            logger.error(
                f"Pipeline timed out with {len(remaining)} author(s) still pending: " + ", ".join(remaining[:5]),
                category=LogCategory.ERROR,
            )
    return total_saved, processed
